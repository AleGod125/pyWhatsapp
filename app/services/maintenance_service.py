"""Mantenimiento automatico y NO destructivo de la copia local.

POR QUE EXISTE
--------------
Hasta ahora habia que acordarse de ejecutar a mano ``inspect_db.py``,
``repair_db.py`` o ``probe_chat.py`` para que el sidebar tuviera vistas
previas, los cursores estuvieran al dia o los adjuntos rotos se reintentaran.
Eso no es una aplicacion, es un juego de herramientas. Este servicio hace
automaticamente todo lo que se puede hacer SIN RIESGO.

LA LINEA QUE NO SE CRUZA
-------------------------------------
Aqui SOLO hay reconciliacion: recalcular lo que se deriva de los datos.

    permitido      contadores, cursores, ultimo mensaje, estado derivado,
                   alias de identidad, estado terminal de multimedia
    PROHIBIDO      DELETE, merges destructivos, borrar 'unknown',
                   borrar raw_proto, tocar la sesion

No es una convencion escrita en un comentario: este modulo no importa
``delete`` de SQLAlchemy y hay una prueba que lo verifica. Cualquier operacion
destructiva sigue viviendo en ``repair_db.py``, que se ejecuta a mano y con
autorizacion explicita.

IDEMPOTENCIA
------------
Ejecutarlo dos veces seguidas da el mismo resultado y la segunda no cambia
nada. Las pruebas lo comprueban ejecutandolo dos veces y comparando.

ESCALABILIDAD
--------------------------
Ninguna operacion trae la tabla de mensajes a memoria. Todo son ``UPDATE ...
FROM`` o ``DISTINCT ON`` que devuelven, como mucho, una fila por chat.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.core.logging_setup import get_logger
from app.models import Chat, ChatHistoryState, Contact, MediaFile, Message

log = get_logger("DB")


@dataclass
class ReconcileReport:
    """Que ha cambiado cada reconciliacion. Todo son actualizaciones."""

    history_states_created: int = 0
    history_states_updated: int = 0
    cursors_updated: int = 0
    media_missing_file: int = 0
    media_terminal: int = 0
    media_registered: int = 0
    reclassified: int = 0
    aliases_linked: int = 0
    usernames_filled: int = 0
    previews_updated: int = 0
    seeds_waiting: int = 0
    # Chats que YA tenian una semilla real y seguian marcados como dormidos.
    # Es lo que recupera a los que despertaron mientras la automatizacion
    # estaba rota, sin tener que pedirle al usuario que escriba otra vez.
    seeds_recovered: int = 0
    # Chats que se quedaron a medio excavar porque el proceso murio.
    stuck_fetching_reset: int = 0
    seeds_recovered_chats: list[str] = field(default_factory=list)
    duration_ms: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return (
            self.history_states_created
            + self.history_states_updated
            + self.cursors_updated
            + self.media_missing_file
            + self.media_terminal
            + self.media_registered
            + self.reclassified
            + self.aliases_linked
            + self.usernames_filled
            + self.previews_updated
            + self.seeds_waiting
        )

    def __str__(self) -> str:
        return (
            f"estados +{self.history_states_created}/~{self.history_states_updated} "
            f"cursores={self.cursors_updated} "
            f"media(reintentables={self.media_missing_file} "
            f"terminales={self.media_terminal} nuevos={self.media_registered}) "
            f"reclasificados={self.reclassified} "
            f"alias={self.aliases_linked} usuarios={self.usernames_filled} "
            f"previas={self.previews_updated} "
            f"esperando_semilla={self.seeds_waiting} "
            f"despertados={self.seeds_recovered} ({self.duration_ms} ms)"
        )


# Prefijo SQL de los identificadores fabricados localmente. Se mantiene
# alineado con repository.SYNTHETIC_PREFIXES.
def _real_wamid_filter() -> Any:
    from app.services.repository import SYNTHETIC_PREFIXES

    condiciones = [
        Message.whatsapp_message_id.is_not(None),
        Message.whatsapp_message_id != "",
    ]
    for prefijo in SYNTHETIC_PREFIXES:
        condiciones.append(~func.lower(Message.whatsapp_message_id).like(f"{prefijo}%"))
    return and_(*condiciones)


class MaintenanceService:
    """Reconciliacion segura. Se ejecuta al arrancar y de forma periodica."""

    def __init__(self, database: Any, settings: Any = None) -> None:
        self._database = database
        self._settings = settings

    # -- Punto de entrada ----------------------------------------------------

    def run_all(self) -> ReconcileReport:
        """Ejecuta las cinco reconciliaciones. Ninguna borra nada.

        Un fallo en una no impide las demas: se anota y se sigue. Perder una
        vista previa no puede impedir que se arreglen los cursores.
        """
        started = time.monotonic()
        report = ReconcileReport()

        for nombre, paso in (
            ("chat_stats", self.reconcile_chat_stats),
            ("history_states", self.reconcile_history_states),
            # Antes que multimedia y que las previas: reinterpretar un mensaje
            # puede descubrir un adjunto que registrar y cambiar la previa.
            ("message_types", self.reconcile_message_types),
            ("media_states", self.reconcile_media_states),
            # Antes de clasificar: un chat atascado en "fetching" no puede
            # quedarse ahi, porque ademas lo hace inalcanzable para la cola.
            ("stuck_fetching", self.reconcile_stuck_fetching),
            ("seed_states", self.reconcile_seed_states),
            ("identity_aliases", self.reconcile_identity_aliases),
            ("sidebar_previews", self.reconcile_sidebar_previews),
        ):
            try:
                paso(report)
            except Exception as exc:  # noqa: BLE001 - una parte no tumba el resto
                log.exception("Reconciliacion '%s' fallo", nombre)
                report.errors.append(f"{nombre}: {exc}")

        report.duration_ms = int((time.monotonic() - started) * 1000)
        if report.changed or report.errors:
            log.info("Mantenimiento: %s", report)
        else:
            log.info("Mantenimiento: nada que reconciliar (%d ms)", report.duration_ms)
        return report

    # -- 1. Contadores por chat ---------------------------------------------

    def reconcile_chat_stats(self, report: ReconcileReport) -> ReconcileReport:
        """Crea el estado historico que falte y recalcula ``message_count``.

        Un chat sin fila en ``chat_history_state`` es invisible para el
        backfill: nunca se le pide historial. Crearla es lo que permite que
        una extraccion interrumpida se reanude al volver a abrir la
        aplicacion.
        """
        with self._database.transaction() as session:
            faltantes = session.execute(
                select(Chat.id, Chat.jid).where(
                    ~Chat.jid.in_(select(ChatHistoryState.chat_jid))
                )
            ).all()
            for chat_id, chat_jid in faltantes:
                session.execute(
                    insert(ChatHistoryState)
                    .values(chat_id=chat_id, chat_jid=chat_jid, history_status="pending")
                    .on_conflict_do_nothing(index_elements=[ChatHistoryState.chat_jid])
                )
            report.history_states_created += len(faltantes)

            # Conteo real por chat, en una sola pasada agregada.
            counts = (
                select(Message.chat_jid, func.count().label("total"))
                .group_by(Message.chat_jid)
                .subquery()
            )
            cambiados = session.execute(
                update(ChatHistoryState)
                .where(
                    ChatHistoryState.chat_jid == counts.c.chat_jid,
                    ChatHistoryState.message_count.is_distinct_from(counts.c.total),
                )
                .values(message_count=counts.c.total)
            ).rowcount
            report.history_states_updated += cambiados or 0
        return report

    # -- 2. Cursores historicos ---------------------------------------------

    def reconcile_history_states(self, report: ReconcileReport) -> ReconcileReport:
        """Recalcula el ancla ON_DEMAND y los extremos temporales de cada chat.

        El ancla NO es el mensaje mas antiguo: es el mas antiguo con un ID
        REAL de WhatsApp. Un mensaje sin ID no sirve como cursor, el servidor
        lo acepta con un ACK y despues no envia nada.

        ``DISTINCT ON`` devuelve una fila por chat, no la tabla entera.
        """
        with self._database.transaction() as session:
            anclas = (
                select(
                    Message.chat_jid,
                    Message.whatsapp_message_id.label("wamid"),
                    Message.timestamp.label("ts"),
                )
                .where(_real_wamid_filter())
                .distinct(Message.chat_jid)
                .order_by(Message.chat_jid, Message.timestamp.asc(), Message.id.asc())
                .subquery()
            )
            actualizados = session.execute(
                update(ChatHistoryState)
                .where(
                    ChatHistoryState.chat_jid == anclas.c.chat_jid,
                    or_(
                        ChatHistoryState.oldest_message_id.is_distinct_from(
                            anclas.c.wamid
                        ),
                        ChatHistoryState.oldest_message_timestamp.is_distinct_from(
                            anclas.c.ts
                        ),
                    ),
                )
                .values(
                    oldest_message_id=anclas.c.wamid,
                    oldest_message_timestamp=anclas.c.ts,
                )
            ).rowcount
            report.cursors_updated += actualizados or 0

            extremos = (
                select(
                    Message.chat_jid,
                    func.max(Message.timestamp).label("newest"),
                )
                .group_by(Message.chat_jid)
                .subquery()
            )
            session.execute(
                update(ChatHistoryState)
                .where(
                    ChatHistoryState.chat_jid == extremos.c.chat_jid,
                    ChatHistoryState.newest_message_timestamp.is_distinct_from(
                        extremos.c.newest
                    ),
                )
                .values(newest_message_timestamp=extremos.c.newest)
            )
        return report

    # -- 2b. Reinterpretacion de mensajes ------------------------------------

    def reconcile_message_types(
        self, report: ReconcileReport, *, limit: int = 5000
    ) -> ReconcileReport:
        """Vuelve a interpretar los mensajes que quedaron como ``unknown``.

        ``raw_proto`` guarda el ``WebMessageInfo`` original justamente para
        esto: cuando el parser aprende algo nuevo, lo ya guardado se puede
        reinterpretar sin volver a pedir nada al servidor.

        El caso que lo motiva es real y esta medido: los mensajes que el
        usuario se envia a si mismo llegan envueltos en un
        ``deviceSentMessage``, cuyo mensaje interno va en el campo 2 y no en
        el 1. Hasta que se corrigio, cuatro imagenes del chat propio quedaron
        como ``unknown`` y el sidebar las resumia como ``[unknown]``
       .

        Es una operacion de MEJORA, no de limpieza:

        * solo toca filas cuyo tipo es ``unknown``;
        * solo escribe si el nuevo tipo es DISTINTO de ``unknown``;
        * nunca borra una fila, ni ``raw_proto``, ni un texto ya guardado.

        Un mensaje que sigue sin entenderse se queda exactamente como estaba.
        """
        from app.core.message_parser import interpret_message_bytes, parse_web_message_info

        def reinterpretar(raw: bytes) -> tuple[str, str | None, Any, dict | None] | None:
            """``(tipo, texto, media, metadata)`` o ``None`` si sigue sin saberse.

            Se prueban las DOS formas en que ``raw_proto`` puede estar
            guardado, porque conviven en la base: History Sync guarda el
            ``WebMessageInfo`` completo y el receptor en vivo guarda el
            ``Message`` E2E pelado.
            """
            parsed = parse_web_message_info(raw)
            if parsed is not None:
                # Los bytes SI son un WebMessageInfo. Que siga sin entenderse
                # significa que el tipo interno no se conoce, y ahi se para:
                # releerlos como si fueran un Message pelado interpretaria el
                # campo 1 (``key``) como si fuera ``conversation`` y guardaria
                # basura binaria como si fuera texto del usuario. Se probo, y
                # producia exactamente eso.
                if parsed.message_type == "unknown":
                    return None
                return (
                    parsed.message_type,
                    parsed.text,
                    parsed.media,
                    parsed.metadata or None,
                )
            suelto = interpret_message_bytes(raw)
            if suelto is not None and suelto.message_type != "unknown":
                meta: dict[str, Any] = {}
                if suelto.wrappers:
                    meta["wrappers"] = suelto.wrappers
                if suelto.proto_type:
                    meta["proto_type"] = suelto.proto_type
                return (suelto.message_type, suelto.text, suelto.media, meta or None)
            return None

        with self._database.transaction() as session:
            filas = session.execute(
                select(Message.id, Message.chat_id, Message.chat_jid, Message.raw_proto)
                .where(
                    Message.message_type == "unknown",
                    Message.raw_proto.is_not(None),
                )
                .order_by(Message.id)
                .limit(limit)
            ).all()
            if not filas:
                return report

            wamids = dict(
                session.execute(
                    select(Message.id, Message.whatsapp_message_id).where(
                        Message.id.in_([fila[0] for fila in filas])
                    )
                ).all()
            )

            mejorados: list[dict[str, Any]] = []
            con_media: list[tuple[int, int, str | None, Any]] = []
            for message_id, chat_id, chat_jid, raw in filas:
                resultado = reinterpretar(bytes(raw))
                if resultado is None:
                    continue
                tipo, texto, media, meta = resultado
                mejorados.append(
                    {"id_": message_id, "tipo": tipo, "texto": texto, "meta": meta}
                )
                if media is not None:
                    con_media.append(
                        (message_id, chat_id, wamids.get(message_id), media)
                    )

            if not mejorados:
                return report

            from sqlalchemy import Text, bindparam
            from sqlalchemy.dialects.postgresql import JSONB

            session.execute(
                update(Message.__table__)
                .where(Message.__table__.c.id == bindparam("id_"))
                .values(
                    message_type=bindparam("tipo"),
                    # El texto solo se rellena si no habia; nunca se pisa.
                    text=func.coalesce(
                        Message.__table__.c.text, bindparam("texto", type_=Text)
                    ),
                    # El tipo se declara explicitamente: psycopg no sabe por si
                    # solo que un dict de Python va a una columna JSONB.
                    raw_metadata=func.coalesce(
                        bindparam("meta", type_=JSONB), Message.__table__.c.raw_metadata
                    ),
                ),
                mejorados,
            )
            report.reclassified += len(mejorados)
            log.info(
                "Reinterpretados %d mensajes que estaban sin clasificar", len(mejorados)
            )

            report.media_registered += self._register_recovered_media(session, con_media)
        return report

    @staticmethod
    def _register_recovered_media(session: Any, recovered: list[tuple]) -> int:
        """Crea la fila ``media_files`` de un adjunto descubierto al reparsear.

        Se inserta en ``pending`` para que el worker de descargas lo recoja
        solo, sin reiniciar la aplicacion. El ``ON CONFLICT DO
        NOTHING`` la hace idempotente: si el adjunto ya estaba registrado no
        se duplica ni se pierde lo ya descargado.
        """
        if not recovered:
            return 0
        payload = [
            {
                "message_id": message_id,
                "chat_id": chat_id,
                "whatsapp_message_id": wamid,
                "media_type": media.media_type,
                "mime_type": media.mime_type,
                "file_name": media.file_name,
                "file_size": media.file_size,
                "duration_seconds": media.duration_seconds,
                "width": media.width,
                "height": media.height,
                "direct_path": media.direct_path,
                "media_key": media.media_key,
                "file_sha256": media.file_sha256,
                "file_enc_sha256": media.file_enc_sha256,
                "download_status": "pending",
            }
            for message_id, chat_id, wamid, media in recovered
        ]
        # RETURNING en vez de rowcount: con ON CONFLICT DO NOTHING psycopg
        # devuelve -1 y el informe acababa contando adjuntos negativos.
        insertadas = len(
            session.execute(
                insert(MediaFile)
                .values(payload)
                .on_conflict_do_nothing(constraint="uq_media_files_message_type")
                .returning(MediaFile.id)
            ).all()
        )
        if insertadas:
            log.info("%d adjuntos recuperados y encolados para descarga", insertadas)
        return insertadas

    # -- 3. Estado de multimedia --------------------------------------------

    def reconcile_media_states(self, report: ReconcileReport) -> ReconcileReport:
        """Pone al dia el estado de los adjuntos frente a lo que hay en disco.

        Dos correcciones, ambas sin perder informacion:

        1. Marcado ``downloaded`` pero el archivo ya no esta en disco. La GUI
           lo pintaria como abrible y al pulsar no pasaria nada; eso es un
           BUG. Vuelve a ``pending`` para que el worker lo
           reintente.
        2. ``pending`` sin ``direct_path`` o sin ``media_key``. No hay nada
           con que descargarlo: ni URL ni clave. Se marca ``unavailable``,
           que es TERMINAL y honesto, en vez de dejarlo eternamente
           "pendiente": marcar multimedia como terminal si esta permitido.

        El mensaje y su metadata NUNCA se tocan: que el archivo no se pueda
        recuperar no invalida el mensaje.
        """
        media_root = getattr(self._settings, "media_dir", None)

        with self._database.transaction() as session:
            if media_root is not None:
                descargados = session.execute(
                    select(MediaFile.id, MediaFile.local_path).where(
                        MediaFile.download_status == "downloaded",
                        MediaFile.local_path.is_not(None),
                    )
                ).all()
                perdidos = [
                    media_id
                    for media_id, local_path in descargados
                    if not (Path(media_root) / local_path).exists()
                ]
                if perdidos:
                    session.execute(
                        update(MediaFile)
                        .where(MediaFile.id.in_(perdidos))
                        .values(
                            download_status="pending",
                            local_path=None,
                            last_error="el archivo local ya no existe; se reintentara",
                        )
                    )
                    report.media_missing_file += len(perdidos)
                    log.warning(
                        "%d adjuntos marcados como descargados no estaban en disco; "
                        "se reintentaran",
                        len(perdidos),
                    )

            sin_material = session.execute(
                update(MediaFile)
                .where(
                    MediaFile.download_status == "pending",
                    or_(
                        MediaFile.direct_path.is_(None),
                        MediaFile.media_key.is_(None),
                    ),
                )
                .values(
                    download_status="unavailable",
                    last_error="el mensaje no incluye direct_path o media_key",
                )
            ).rowcount
            report.media_terminal += sin_material or 0
        return report

    # -- 3a bis. Excavaciones que se quedaron a medias -----------------------

    def reconcile_stuck_fetching(self, report: ReconcileReport) -> ReconcileReport:
        """``fetching`` es un estado TRANSITORIO: nadie puede quedarse ahi.

        Lo marca el backfill justo antes de pedir y lo cambia al recibir la
        respuesta. Si el proceso muere entre esas dos cosas, el chat queda en
        ``fetching`` para siempre, y ademas se vuelve inalcanzable: la cola de
        semillas salta los chats que ya se estan excavando, asi que nunca se
        le vuelve a pedir nada.

        Se midio con "Tia Diana": quedo en ``fetching`` tras cortarse el
        proceso, con dos mensajes y sin historial.

        Se devuelve a ``pending``, que es lo que era antes de pedir. No se
        marca como agotado ni como completo: no se sabe si el telefono habria
        respondido.
        """
        from app.models import ChatHistoryState

        with self._database.transaction() as session:
            atascados = session.execute(
                select(ChatHistoryState.chat_jid).where(
                    ChatHistoryState.history_status == "fetching"
                )
            ).scalars().all()
            if not atascados:
                return report
            session.execute(
                update(ChatHistoryState)
                .where(ChatHistoryState.chat_jid.in_(atascados))
                .values(history_status="pending", last_error=None)
            )
        report.stuck_fetching_reset += len(atascados)
        log.info(
            "%d chat(s) atascados en 'fetching' vuelven a 'pending'", len(atascados)
        )
        return report

    # -- 3b. Estados honestos de los chats sin ancla -------------------------

    def reconcile_seed_states(self, report: ReconcileReport) -> ReconcileReport:
        """Separa "no se puede excavar" de "ya no queda nada".

        Un chat con cero mensajes que en el telefono SI los tiene no esta
        sincronizado: espera una semilla. Decirle "historial sincronizado"
        seria mentir, y es lo que pasaba con 32 chats.
        """
        from app.services.seed_recovery import SeedRecovery

        recuperacion = SeedRecovery(self._database)
        informe = recuperacion.classify()
        report.seeds_waiting += informe.marcados_waiting

        # Y al reves: un chat marcado como dormido puede tener YA un ancla
        # real, porque llego actividad mientras la siembra automatica no
        # funcionaba. Se comprueba con lo que hay en la base; no se pide nada
        # ni se inventa ningun cursor.
        despertados = recuperacion.seed_from_messages(recuperacion.pending_seedless())
        report.seeds_recovered += despertados.sembrados
        report.seeds_recovered_chats.extend(despertados.chats)
        return report

    # -- 4. Alias de identidad ----------------------------------------------

    def reconcile_identity_aliases(self, report: ReconcileReport) -> ReconcileReport:
        """Empareja telefono y LID a partir de lo que ya envio WhatsApp.

        Los blobs de History Sync archivados en ``data/history/`` traen dos
        registros que hasta ahora se ignoraban:

            campo 15  phoneNumberToLIDMapping  {1: pnJID, 2: lidJID}
            campo 18  {1: lidJID, 2: username, 3: pais}

        El primero es exactamente la correspondencia que hace falta para que
        un chat identificado con ``@lid`` encuentre el nombre guardado bajo su
        numero de telefono. El segundo permite ensenar un nombre de usuario en
        lugar de un LID crudo cuando no hay ningun otro nombre.

        No se pide NADA al servidor: los blobs ya estan en disco.
        """
        blobs = self._history_blobs()
        if not blobs:
            return report

        mapeos, usuarios = _read_identity_records(blobs)
        if not mapeos and not usuarios:
            return report

        with self._database.transaction() as session:
            # 1) PN -> LID. Solo se rellena cuando falta o difiere; nunca se
            #    borra un LID ya conocido poniendo NULL.
            for pn_jid, lid_jid in mapeos.items():
                actualizado = session.execute(
                    update(Contact)
                    .where(
                        Contact.jid == pn_jid,
                        Contact.lid.is_distinct_from(lid_jid),
                    )
                    .values(lid=lid_jid)
                ).rowcount
                report.aliases_linked += actualizado or 0

            # 2) LID -> username, solo si el contacto no tiene ningun nombre.
            #    Un usuario de WhatsApp es un identificador real, no una
            #    invencion, y se prefiere a ensenar "49577042411710@lid".
            for lid_jid, username in usuarios.items():
                actualizado = session.execute(
                    update(Contact)
                    .where(
                        Contact.lid == lid_jid,
                        Contact.display_name.is_(None),
                        Contact.push_name.is_(None),
                    )
                    .values(push_name=username)
                ).rowcount
                report.usernames_filled += actualizado or 0
        return report

    def _history_blobs(self) -> list[Path]:
        data_dir = getattr(self._settings, "data_dir", None)
        if data_dir is None:
            return []
        carpeta = Path(data_dir) / "history"
        if not carpeta.is_dir():
            return []
        return sorted(carpeta.glob("*.pb"))

    # -- 5. Vistas previas del sidebar --------------------------------------

    def reconcile_sidebar_previews(self, report: ReconcileReport) -> ReconcileReport:
        """Recalcula ``last_message`` con el vocabulario de :mod:`app.previews`."""
        from app.services import repository as repo

        with self._database.transaction() as session:
            jids = list(session.execute(select(Chat.jid)).scalars())
            if jids:
                report.previews_updated += repo.refresh_chat_previews(session, jids)
        return report


# ---------------------------------------------------------------------------
# Lectura de los blobs archivados
# ---------------------------------------------------------------------------

_MAPPING_FIELD = 15   # phoneNumberToLIDMapping
_USERNAME_FIELD = 18  # LID + nombre de usuario + pais


def _read_identity_records(
    blobs: list[Path],
) -> tuple[dict[str, str], dict[str, str]]:
    """``({pn_jid: lid_jid}, {lid_jid: username})`` de los blobs archivados.

    Se lee con el escaner de protobuf ya existente: no hace falta definir
    descriptores nuevos para dos campos de cadena. Un blob ilegible se salta.
    """
    from app.core.message_parser import top_level_fields

    mapeos: dict[str, str] = {}
    usuarios: dict[str, str] = {}

    for ruta in blobs:
        try:
            raw = ruta.read_bytes()
        except OSError:
            continue
        for numero, _wire, payload in top_level_fields(raw):
            if not payload:
                continue
            if numero == _MAPPING_FIELD:
                partes = {n: p for n, _w, p in top_level_fields(payload) if p}
                pn, lid = partes.get(1), partes.get(2)
                if pn and lid:
                    try:
                        mapeos[pn.decode()] = lid.decode()
                    except UnicodeDecodeError:
                        continue
            elif numero == _USERNAME_FIELD:
                partes = {n: p for n, _w, p in top_level_fields(payload) if p}
                lid, nombre = partes.get(1), partes.get(2)
                if lid and nombre:
                    try:
                        usuarios[lid.decode()] = nombre.decode()
                    except UnicodeDecodeError:
                        continue
    return mapeos, usuarios
