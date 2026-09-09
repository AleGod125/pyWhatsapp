"""Volver a leer el historial que YA esta en casa.

EL HALLAZGO
-----------
Medido sobre la instalacion real el 8 de septiembre de 2026:

    mensajes en los blobs archivados = 6504  (60 conversaciones)
    mensajes en PostgreSQL           =  537
    -----------------------------------------
    perdidos                         = 5967

No es que WhatsApp no los entregara. Los entrego, se archivaron en
``data/history/*.pb`` --551 archivos, 8,9 MB-- y la base se quedo sin ellos.
Una conversacion con 1994 mensajes en disco tenia 31 guardados, y encima
marcada ``exhausted``: el telefono habia dicho "no queda mas", con razon, y el
sistema lo daba por completo mientras el historial estaba ahi al lado sin leer.

POR QUE PASO
------------
Los blobs se archivan ANTES de interpretarlos, a proposito: un fallo de
normalizacion no puede costar historial. Pero el archivo era la unica copia
que sobrevivio a un borrado de la base, y nadie volvia a leerlo.

``BlobSeedScanner`` si los recorre, y lo dice en su docstring: *"NO REINGIERE
MENSAJES. Los mensajes de esos blobs ya estan donde tienen que estar."* Era
verdad mientras nadie vaciara PostgreSQL. Al vaciarlo, esa frase se convirtio
en la razon por la que 5967 mensajes seguian en disco sin que nadie fuera a
por ellos: se les sacaba el identificador para usarlos de ancla y se tiraba el
cuerpo.

QUE HACE ESTO
-------------
Recorre los blobs y vuelve a pasarlos por ``ingest_history_sync``, la MISMA
funcion que usa la llegada en vivo. No reinterpreta nada por su cuenta.

Es idempotente: ``bulk_upsert_messages`` deduplica por
``(chat_id, whatsapp_message_id)``, asi que la segunda pasada inserta cero.
No borra, no pide nada al telefono y no gasta una sola peticion de red.

A QUIEN SE LE ATRIBUYE CADA CONVERSACION
----------------------------------------
Aqui esta el cuidado. Los blobs se archivan en una carpeta COMUN --
``data_dir/history``-- sin nada en el nombre que diga de quien son. Con dos
cuentas vinculadas en la misma maquina, meterlos todos en la que pulsa el
boton le daria una copia de las conversaciones de la otra persona.

Asi que se atribuye por evidencia, no por conveniencia:

* la conversacion ya es un chat de ESTA cuenta -> se ingiere;
* la conversacion es un chat de OTRA cuenta    -> se salta, siempre;
* la conversacion no la conoce nadie           -> se ingiere solo si en este
  dispositivo hay UNA sola cuenta vinculada, porque entonces no hay ninguna
  otra a la que pudiera pertenecer. Con dos o mas se salta y se cuenta.

Saltarse una conversacion se informa. Adivinar, no.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.core.logging_setup import get_logger
from app.models import Chat, WhatsAppAccount

log = get_logger("SYNC")


@dataclass
class ResultadoDeReingesta:
    """Lo que se recupero, y lo que se dejo fuera y por que."""

    blobs: int = 0
    blobs_ilegibles: int = 0
    conversaciones: int = 0
    #: Conversaciones saltadas por ser de otra cuenta.
    ajenas: int = 0
    #: Conversaciones saltadas por no poder atribuirlas con dos o mas cuentas.
    sin_atribuir: int = 0
    mensajes_vistos: int = 0
    #: Mensajes que NO estaban y ahora si. Es el numero que importa.
    mensajes_nuevos: int = 0
    media_detectada: int = 0
    chats_nuevos: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        partes = [
            f"blobs={self.blobs}",
            f"conversaciones={self.conversaciones}",
            f"mensajes_nuevos={self.mensajes_nuevos}",
            f"vistos={self.mensajes_vistos}",
        ]
        if self.ajenas:
            partes.append(f"de_otra_cuenta={self.ajenas}")
        if self.sin_atribuir:
            partes.append(f"sin_atribuir={self.sin_atribuir}")
        if self.blobs_ilegibles:
            partes.append(f"ilegibles={self.blobs_ilegibles}")
        return " ".join(partes)


def carpeta_de_blobs(settings: Any) -> Path:
    """La misma que usa ``BlobSeedScanner``: una sola definicion.

    Es la carpeta de la EPOCA pywhats (protobuf, ``*.pb``). Se conserva para
    los blobs que ya estan ahi archivados; nada nuevo escribe en ella.
    """
    return Path(settings.data_dir) / "history"


def carpeta_de_blobs_baileys(settings: Any) -> Path:
    """Donde el worker de Baileys archiva sus blobs, en JSON (``*.json``).

    Misma idea que ``carpeta_de_blobs``, mismo motivo: un blob se archiva
    ANTES de interpretarlo, para que un fallo de normalizacion --o, medido de
    verdad, un INSERT que supera el limite de parametros de PostgreSQL en un
    lote grande-- no pueda costar historial. Debe coincidir con
    ``WA_BAILEYS_HISTORY_DIR`` (``app/wa/baileys_client.py:_entorno``).
    """
    return Path(settings.data_dir) / "history_baileys"


def _cuentas_vinculadas(session: Any) -> int:
    return int(
        session.execute(
            select(func.count()).select_from(WhatsAppAccount)
        ).scalar_one_or_none()
        or 0
    )


def _duenos_de_cada_jid(session: Any) -> dict[str, set[Any]]:
    """Que cuenta(s) tienen ya un chat con cada JID.

    Es la unica evidencia disponible sobre de quien es una conversacion de un
    blob: el archivo no lo dice.
    """
    duenos: dict[str, set[Any]] = {}
    for jid, cuenta in session.execute(select(Chat.jid, Chat.whatsapp_account_id)):
        if jid:
            duenos.setdefault(jid, set()).add(cuenta)
    return duenos


def _es_mia(jid: str, cuenta: Any, duenos: dict[str, set[Any]], sola: bool) -> bool | None:
    """``True`` mia, ``False`` de otra, ``None`` no se puede atribuir."""
    de_quien = duenos.get(jid)
    if de_quien is None:
        # Nadie la tiene. Sin ninguna otra cuenta en la maquina no hay a quien
        # mas pueda pertenecer; con otras, no se adivina.
        return True if sola else None
    if cuenta in de_quien:
        return True
    return False


def reingerir_blobs(
    database: Any,
    settings: Any,
    *,
    account_id: Any = None,
    own_jid: str | None = None,
    publish: Any = None,
) -> ResultadoDeReingesta:
    """Vuelve a ingerir los blobs archivados. Nunca lanza.

    :param publish: si se pasa, se avisa una vez al terminar para que la
        pantalla recargue: pueden haber aparecido miles de mensajes de golpe.
    """
    import json

    from app.wa.historial import parse_full, parse_full_json
    from app.services.history_service import ingest_history_sync

    resultado = ResultadoDeReingesta()

    # Las DOS epocas, cada una con su formato y su carpeta. Un blob de
    # Baileys que no se pudo persistir --se midio: un INSERT de 4887
    # mensajes de golpe supero el limite de parametros de PostgreSQL y el
    # bootstrap entero se perdio-- se recupera exactamente igual que un
    # ``.pb`` de pywhats: no gasta red y no pide nada al telefono, porque el
    # blob ya esta en casa.
    rutas: list[tuple[Path, str]] = []
    carpeta_pb = carpeta_de_blobs(settings)
    if carpeta_pb.exists():
        rutas += [(r, "pb") for r in sorted(carpeta_pb.glob("*.pb"))]
    carpeta_json = carpeta_de_blobs_baileys(settings)
    if carpeta_json.exists():
        rutas += [(r, "json") for r in sorted(carpeta_json.glob("*.json"))]
    if not rutas:
        return resultado

    with database.transaction() as sesion:
        duenos = _duenos_de_cada_jid(sesion)
        sola = _cuentas_vinculadas(sesion) <= 1

    log.info(
        "[HISTORY] releyendo %d blob(s) archivados antes de pedirle nada al "
        "telefono (no gasta red y no borra nada)",
        len(rutas),
    )

    for ruta, formato in rutas:
        try:
            if formato == "pb":
                sync = parse_full(ruta.read_bytes())
            else:
                sync = parse_full_json(json.loads(ruta.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 - un blob roto no para a los demas
            resultado.blobs_ilegibles += 1
            log.debug("Blob ilegible: %s", ruta.name, exc_info=True)
            continue
        resultado.blobs += 1

        # Se filtran las conversaciones ANTES de ingerir: `ingest_history_sync`
        # crea el chat de cada una, asi que dejar pasar una ajena ya seria
        # haberla creado en la cuenta equivocada.
        mias = []
        for conversacion in sync.conversations:
            veredicto = _es_mia(conversacion.jid, account_id, duenos, sola)
            if veredicto is True:
                mias.append(conversacion)
            elif veredicto is False:
                resultado.ajenas += 1
            else:
                resultado.sin_atribuir += 1
        if not mias:
            continue

        sync.conversations = mias
        try:
            with database.transaction() as sesion:
                parcial = ingest_history_sync(
                    sesion,
                    sync,
                    own_jid=own_jid,
                    whatsapp_account_id=account_id,
                )
        except Exception:  # noqa: BLE001 - un blob no puede tumbar la recuperacion
            log.exception("No se pudo reingerir %s", ruta.name)
            continue

        resultado.conversaciones += parcial.conversations
        resultado.mensajes_vistos += parcial.messages_seen
        resultado.mensajes_nuevos += parcial.messages_inserted
        resultado.media_detectada += parcial.media_detected
        for jid in parcial.new_chat_jids:
            if jid not in resultado.chats_nuevos:
                resultado.chats_nuevos.append(jid)

    if resultado.mensajes_nuevos:
        log.info(
            "[HISTORY] recuperados %d mensaje(s) que ya estaban en disco y no "
            "en la base (%s)",
            resultado.mensajes_nuevos,
            resultado,
        )
        if publish is not None:
            # Un solo aviso al final. Uno por blob serian cientos, y la
            # pantalla no necesita el detalle: necesita saber que hay mas.
            publish(
                "history_ingested",
                {
                    "messages_new": resultado.mensajes_nuevos,
                    "chat_jids": list(resultado.chats_nuevos),
                    "source": "blobs_archivados",
                },
            )
    else:
        log.info("[HISTORY] los blobs archivados no tenian nada que no estuviera ya")

    if resultado.ajenas or resultado.sin_atribuir:
        log.info(
            "[HISTORY] %d conversacion(es) de otra cuenta y %d sin poder "
            "atribuir se dejaron fuera: los blobs se archivan en una carpeta "
            "comun y el archivo no dice de quien es",
            resultado.ajenas,
            resultado.sin_atribuir,
        )
    return resultado


def reabrir_agotados_con_ancla_mas_vieja(database: Any, account_id: Any = None) -> int:
    """Reabre las conversaciones ``exhausted`` cuyo ancla se ha movido atras.

    POR QUE ESTO SI Y UN BOTON NO
    -----------------------------
    ``exhausted`` significa que el telefono contesto
    ``COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY``. Es una respuesta, no un
    corte, y por eso ningun boton la reabre: insistir gasta una peticion para
    recibir cero mensajes.

    Pero esa respuesta contestaba a UNA pregunta concreta: *"¿queda algo antes
    del 8 de septiembre?"*. Al releer el archivo aparecieron mensajes de agosto
    en esa misma conversacion, asi que el ancla ya no esta en septiembre: esta
    en agosto. La pregunta que toca ahora --*"¿queda algo antes del 9 de
    agosto?"*-- es otra, y el telefono no la ha contestado nunca.

    Eso es evidencia nueva, que es justo lo que el resto del sistema exige
    para reabrir una conversacion agotada. Se comprueba de verdad, no se
    supone: solo se reabre si el ancla de ahora es ANTERIOR al cursor con el
    que el telefono respondio la ultima vez.
    """
    from sqlalchemy import and_, exists, select, update

    from app.models import Chat, ChatHistoryState, HistoryRequest

    try:
        with database.transaction() as sesion:
            # La ultima peticion que SI obtuvo respuesta para ese chat. Si su
            # cursor es mas reciente que el ancla de ahora, el "no queda mas"
            # se refiere a un tramo que ya no es el que tenemos delante.
            respondida_mas_arriba = exists().where(
                and_(
                    HistoryRequest.chat_id == ChatHistoryState.chat_id,
                    HistoryRequest.status == "received",
                    HistoryRequest.cursor_timestamp.isnot(None),
                    HistoryRequest.cursor_timestamp
                    > ChatHistoryState.oldest_message_timestamp,
                )
            )
            donde = [
                ChatHistoryState.history_status == "exhausted",
                ChatHistoryState.oldest_message_timestamp.isnot(None),
                respondida_mas_arriba,
            ]
            if account_id is not None:
                donde.append(
                    ChatHistoryState.chat_id.in_(
                        select(Chat.id).where(
                            Chat.whatsapp_account_id == account_id
                        )
                    )
                )
            cuantos = int(
                sesion.execute(
                    update(ChatHistoryState)
                    .where(*donde)
                    .values(
                        history_status="pending",
                        next_retry_at=None,
                        attempt_count=0,
                        consecutive_no_progress=0,
                        last_error=None,
                    )
                ).rowcount
                or 0
            )
    except Exception:  # noqa: BLE001 - no poder reabrir no tumba la recuperacion
        log.exception("No se pudieron reabrir las conversaciones agotadas")
        return 0

    if cuantos:
        log.info(
            "[HISTORY] %d conversacion(es) agotadas vuelven a la cola: al "
            "releer el archivo su ancla se movio mas atras, asi que lo que el "
            "telefono dio por terminado ya no es la misma pregunta",
            cuantos,
        )
    return cuantos
