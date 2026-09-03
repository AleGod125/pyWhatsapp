"""Recuperacion historica mediante HISTORY_SYNC_ON_DEMAND.

Arquitectura. La peticion NO se envia al contacto cuya
conversacion queremos: el destinatario es NUESTRO PROPIO telefono principal, y
el chat objetivo viaja dentro del mensaje::

    PC companion -> Peer Data Operation -> telefono principal propio
                 -> el telefono genera un History Sync
                 -> la PC recibe la notificacion, descarga el blob
                 -> PostgreSQL

La respuesta no vuelve por el mismo canal: llega despues como un
HISTORY_SYNC_NOTIFICATION de tipo ON_DEMAND, que ya captura
``app.compat.history_compat``. Aqui solo se emite la peticion y se espera a que
aparezca el chat en algun blob.

LIMITACION CONOCIDA
-------------------
whatsmeow envia esta peticion con ``Peer: true``, que cifra solo para el
dispositivo principal y marca la stanza como ``category="peer"``.
``Sender.send_message`` de pywhats 0.2.0 no ofrece ese modo, asi que aqui se
envia como un mensaje normal dirigido a la propia cuenta. Puede que el
servidor no lo acepte. Mientras no se observe una respuesta ON_DEMAND real,
esta capa NO puede darse por funcionando.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update

from app.services import repository as repo
from app.core.config import Settings
from app.core.database import Database
from app.core.logging_setup import get_logger
from app.models import Chat, ChatHistoryState, HistoryRequest

log = get_logger("BACKFILL")

# Valor del enum ProtocolMessage.Type. Lo define pywhats, no se inventa.
PEER_DATA_OPERATION_REQUEST_MESSAGE = 16

# Bandera persistida en app_state la primera vez que ON_DEMAND devuelve algo.
CAPABILITY_KEY = "ondemand_capability_confirmed"
# Huella de la sesion cuyo veredicto de extraccion esta vigente.
SESSION_KEY = "backfill_session_fingerprint"

# Campo 11 de Conversation. El 0 es el caso que hay que seguir pidiendo:
# el telefono dice "he terminado este bloque, pero AUN ME QUEDAN mensajes".
_END_TYPES = {
    0: "COMPLETE_BUT_MORE_MESSAGES_REMAIN_ON_PRIMARY",
    1: "COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY",
    2: "COMPLETE_ON_DEMAND_SYNC_BUT_MORE_MSG_REMAIN_ON_PRIMARY",
    3: "COMPLETE_ON_DEMAND_SYNC_WITH_MORE_MSG_ON_PRIMARY_BUT_NO_ACCESS",
}
# Tipos que significan "sigue pidiendo": quedan mensajes en el telefono.
_MORE_REMAINS = frozenset({0, 2})


@dataclass
class BackfillStats:
    chats_processed: int = 0
    requests_sent: int = 0
    responses_received: int = 0
    messages_new: int = 0
    timeouts: int = 0
    no_cursor: int = 0
    errors: int = 0

    def absorber(self, otra: "BackfillStats") -> None:
        """Suma otra tanda a este acumulado."""
        self.chats_processed += otra.chats_processed
        self.requests_sent += otra.requests_sent
        self.responses_received += otra.responses_received
        self.messages_new += otra.messages_new
        self.timeouts += otra.timeouts
        self.no_cursor += otra.no_cursor
        self.errors += otra.errors

    def __str__(self) -> str:
        return (
            f"chats={self.chats_processed} peticiones={self.requests_sent} "
            f"respuestas={self.responses_received} mensajes_nuevos={self.messages_new} "
            f"timeouts={self.timeouts} sin_cursor={self.no_cursor} errores={self.errors}"
        )


@dataclass
class _Pending:
    """Peticion en vuelo, esperando a que su chat aparezca en un blob."""

    chat_jid: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    messages: int = 0
    end_of_history_type: int | None = None
    # Se levanta cuando el transporte muere mientras se esperaba. No es lo
    # mismo que un timeout: el telefono no tuvo ocasion de contestar, asi que
    # la peticion no demuestra nada sobre ese chat.
    transport_lost: bool = False


def build_on_demand_message(
    *,
    chat_jid: str,
    oldest_message_id: str,
    oldest_from_me: bool,
    oldest_timestamp: int,
    count: int,
    account_lid: str | None = None,
) -> Any:
    """Construye el ``Message`` con la peticion ON_DEMAND.

    Se arma con el descriptor propio (``app/proto``) y se reparsea con el
    ``Message`` de pywhats. Protobuf conserva los campos que no conoce al
    reserializar, asi que el campo 16 llega intacto sin parchear el paquete.
    Verificado: el round-trip es identico byte a byte.
    """
    from pywhats.proto import Message as PyWhatsMessage

    from app.models.proto import HISTORY_SYNC_ON_DEMAND, OnDemandMessage

    outer = OnDemandMessage()
    protocol = outer.protocolMessage
    protocol.type = PEER_DATA_OPERATION_REQUEST_MESSAGE

    operation = protocol.peerDataOperationRequestMessage
    operation.peerDataOperationRequestType = HISTORY_SYNC_ON_DEMAND

    request = operation.historySyncOnDemandRequest
    request.chatJID = chat_jid
    request.oldestMsgID = oldest_message_id
    request.oldestMsgFromMe = oldest_from_me
    request.onDemandMsgCount = count
    # SEGUNDOS, pese a que el campo del protobuf se llame "...MS".
    #
    # Lo dice la bitacora de la implementacion anterior, que SI recupero
    # historial real: enviaba el epoch en segundos. Multiplicar por 1000
    # produce un ancla ~56.000 anos en el futuro, y el telefono no encuentra
    # nada anterior a esa fecha: stanza aceptada, ACK, y ninguna respuesta.
    # Es exactamente el sintoma observado.
    request.oldestMsgTimestampMS = oldest_timestamp
    if account_lid:
        request.accountLid = account_lid

    message = PyWhatsMessage()
    message.ParseFromString(outer.SerializeToString())
    return message


class BackfillService:
    """Recorre los chats pidiendo historial antiguo, por tandas."""

    def __init__(self, settings: Settings, database: Database) -> None:
        # Lo que ha insertado la INGESTA mientras hay una peticion en vuelo.
        # ``None`` cuando no se esta esperando ninguna respuesta.
        self._ingest_watch: dict[str, int] | None = None
        # Un solo backfill a la vez, y un solo ON_DEMAND por chat.
        #
        # Se midio el fallo: mientras el backfill automatico excavaba, un
        # POST /sync/run lanzo un SEGUNDO backfill sobre el mismo chat. Dos
        # peticiones en vuelo sobre la misma conversacion no solo duplican
        # trabajo: enredan la correlacion entre peticion y respuesta, que es
        # lo unico que permite saber que trajo cada una.
        self._busy = False
        self._in_flight: set[str] = set()
        # Si la ultima espera murio por el transporte, el chat NO queda como
        # "sin respuesta": queda pendiente de reintento.
        self._last_transport_lost = False
        # Timeouts REALES seguidos (transporte sano). Al llegar al tope, la
        # capacidad deja de darse por buena: "funciono una vez" no es
        # "funciona siempre", y se midio justo eso -- dos timeouts seguidos
        # con la capacidad marcada CONFIRMED y el canary omitido.
        self._timeouts_seguidos = 0
        self._settings = settings
        self._database = database
        # El cliente de pywhats no existe hasta que el hilo arranca y la
        # sesion queda conectada, asi que se recibe en run().
        self._client: Any = None
        self._pending: dict[str, _Pending] = {}
        self.stats = BackfillStats()
        # Lo acumulado desde que arranco el proceso. Separado de ``stats``,
        # que es SIEMPRE la tanda en curso.
        self.lifetime = BackfillStats()
        self._stop = False
        # endOfHistoryTransferType de la ultima respuesta, para decidir si el
        # chat esta agotado de verdad o solo limitado por ahora.
        self._last_end_type: int | None = None
        # Identidad propia (PN y LID). Se rellena con set_own_identity().
        self._own_jids: set[str] = set()

    # -- Correlacion de respuestas -------------------------------------------

    def note_history_ingest(self, inserted: int, blob_messages: int) -> None:
        """Cuantos mensajes metio en la base el blob que acaba de llegar.

        Lo llama la ingesta, que es la UNICA via por la que entran mensajes de
        History Sync. Los mensajes en vivo NO pasan por aqui: los guarda
        ``LiveMessageService``. Esa separacion es justo lo que arregla la
        contabilidad.

        Antes se calculaba ``mensajes_nuevos`` restando el numero de filas del
        chat antes y despues de la peticion. Con el receptor corriendo a la
        vez, los mensajes que llegaban durante la espera se apuntaban como si
        los hubiera traido el backfill: se midio un ``respuesta=no nuevos=6``
        sobre una peticion que habia dado TIMEOUT. Los seis eran mensajes
        nuevos del chat, no historial recuperado.
        """
        observador = self._ingest_watch
        if observador is None:
            return
        observador["inserted"] += int(inserted or 0)
        observador["blob_messages"] += int(blob_messages or 0)

    def notify_history(self, sync: Any) -> None:
        """Avisa de que llego un History Sync. Lo llama la capa de ingesta.

        Se correlaciona por JID de chat: el protocolo no garantiza devolver un
        identificador de peticion utilizable, asi que se usa lo que si es
        fiable, que es el contenido de la respuesta.
        """
        for conversation in sync.conversations:
            waiting = self._pending.get(conversation.jid)
            if waiting is None:
                continue
            waiting.messages += len(conversation.messages)
            waiting.end_of_history_type = conversation.end_of_history_type
            if conversation.end_of_history_type is not None:
                log.info(
                    "endOfHistoryTransferType=%d (%s)",
                    conversation.end_of_history_type,
                    _END_TYPES.get(
                        conversation.end_of_history_type, "desconocido"
                    ),
                )
            waiting.event.set()

    # -- Seleccion de trabajo ------------------------------------------------

    def chats_to_process(self, limit: int = 500) -> list[tuple[int, str]]:
        """Chats que aun pueden dar mas historial.

        Se excluyen los agotados y los que no tienen cursor utilizable. El
        orden pone delante los que ya tienen mensajes: son los que mas
        probabilidad tienen de devolver algo.
        """
        with self._database.transaction() as session:
            rows = session.execute(
                select(Chat.id, Chat.jid)
                .outerjoin(ChatHistoryState, ChatHistoryState.chat_id == Chat.id)
                .where(
                    (ChatHistoryState.history_status.is_(None))
                    | (
                        ChatHistoryState.history_status.notin_(
                            ("exhausted", "no_valid_cursor")
                        )
                    )
                )
                .order_by(Chat.last_message_timestamp.desc().nulls_last())
                .limit(limit)
            ).all()
            return [
                (row[0], row[1])
                for row in rows
                if self.is_backfill_candidate(row[1])
            ]

    # -- Bucle principal -----------------------------------------------------

    def set_own_identity(self, own_pn: str | None, own_lid: str | None) -> None:
        """Registra la identidad propia para excluirla del backfill."""
        self._own_jids = {j for j in (own_pn, own_lid) if j}

    def refresh_own_identity(self) -> None:
        """Relee la identidad propia del cliente CONECTADO.

        La version anterior la tomaba del ``device.json`` en el momento de
        cablear, antes de arrancar el cliente. Tras un pairing nuevo ese
        archivo aun tenia la identidad anterior (o ninguna), asi que el self
        no quedaba excluido y aparecia como candidato del backfill.
        """
        device = getattr(self._client, "device", None)
        if device is None:
            return
        jids = set(self._own_jids)
        jid = getattr(device, "jid", None)
        if jid is not None and getattr(jid, "user", None):
            jids.add(f"{jid.user}@{getattr(jid, 'server', 's.whatsapp.net')}")
        lid = getattr(device, "lid", None)
        if isinstance(lid, str) and lid:
            jids.add(f"{lid.split('@')[0].split('.')[0]}@lid")
        if jids != self._own_jids:
            self._own_jids = jids
            log.info("Identidad propia actualizada: %d identificadores", len(jids))

    def is_backfill_candidate(self, chat_jid: str) -> bool:
        """Un chat al que tiene sentido pedirle historial.

        Se rechaza la propia cuenta: el ultimo canary la eligio (73 filas que
        en realidad eran ecos de nuestras propias peticiones ON_DEMAND) y
        acabo en ACK + timeout. Pedirle historial a uno mismo no tiene
        sentido y ademas envenena el diagnostico.
        """
        if chat_jid in getattr(self, "_own_jids", set()):
            return False
        if chat_jid.startswith("status@") or chat_jid.endswith("@broadcast"):
            return False
        if chat_jid.endswith("@newsletter"):
            return False
        return True

    def pick_canary(self) -> tuple[int, str] | None:
        """Un solo chat para la prueba: con mensajes y con cursor real.

        Se prefiere un chat individual y con varios mensajes, que es el caso
        mas facil de verificar a mano.
        """
        for chat_id, chat_jid in self.chats_to_process():
            if chat_jid.endswith("@g.us"):
                continue
            if not self.is_backfill_candidate(chat_jid):
                continue
            with self._database.transaction() as session:
                if repo.get_oldest_valid_history_cursor(session, chat_jid) is None:
                    continue
                if repo.count_messages(session, chat_jid) < 2:
                    continue
            return chat_id, chat_jid
        return None

    async def run_canary(self, client: Any, *, max_rounds: int = 3) -> bool:
        """Prueba UN solo chat. No avanza al resto hasta que funcione.

        Devuelve ``True`` si ON_DEMAND devolvio mensajes de verdad.
        """
        if self._busy:
            log.warning("Ya hay una excavacion en marcha; el canary no lanza otra")
            return False
        self._busy = True
        try:
            return await self._canary_locked(client, max_rounds)
        finally:
            # Si esto no se suelta, un fallo a mitad deja el backfill
            # bloqueado para el resto de la vida del proceso.
            self._busy = False

    async def _canary_locked(self, client: Any, max_rounds: int) -> bool:
        """El cuerpo de :meth:`run_canary`, ya con la excavacion reservada."""
        self._client = client
        self.refresh_own_identity()
        # La revalidacion va ANTES de elegir el canary: si no, todos los chats
        # de la sesion anterior siguen marcados 'exhausted' y no hay ninguno
        # con cursor que probar ("CANARY: no hay ningun chat con cursor valido").
        self.revalidate_for_new_session()
        target = self.pick_canary()
        if target is None:
            log.error("CANARY: no hay ningun chat con cursor valido")
            return False

        chat_id, chat_jid = target
        with self._database.transaction() as session:
            before = repo.count_messages(session, chat_jid)
            oldest_before = repo.get_oldest_stored_timestamp(session, chat_jid)

        log.info("CANARY START")
        log.info("Chat=%s", _short(chat_jid))
        log.info("Before count=%d", before)
        log.info("Before oldest=%s", _fmt_ts(oldest_before))

        await self._process_chat(chat_id, chat_jid, max_rounds)

        with self._database.transaction() as session:
            after = repo.count_messages(session, chat_jid)
            oldest_after = repo.get_oldest_stored_timestamp(session, chat_jid)

        log.info("After count=%d", after)
        log.info("After oldest=%s", _fmt_ts(oldest_after))

        gained = after - before
        went_back = (
            oldest_after is not None
            and oldest_before is not None
            and oldest_after < oldest_before
        )

        if gained > 0 and went_back:
            log.info("CANARY SUCCESS: +%d mensajes, historial retrocedio", gained)
            self._confirm_capability()
            return True

        # Una respuesta VALIDA con 0 mensajes agota ESE chat; no dice nada
        # malo del protocolo. Si el telefono contesto, ON_DEMAND funciona y el
        # backfill global debe continuar con los demas chats.
        if self.stats.responses_received > 0:
            log.info(
                "CANARY: el protocolo responde (respuestas=%d) pero este chat no "
                "tenia mas historial. Se da la capacidad por confirmada y se "
                "continua con el resto.",
                self.stats.responses_received,
            )
            self._confirm_capability()
            return True

        log.warning(
            "CANARY sin respuesta (timeouts=%d): no se puede confirmar la capacidad. "
            "Comprueba que el telefono este encendido y con datos.",
            self.stats.timeouts,
        )
        return False

    async def run(
        self,
        client: Any,
        *,
        max_rounds_per_chat: int = 200,
        max_passes: int = 10,
    ) -> BackfillStats:
        """Pide historial chat por chat hasta agotarlo o quedarse sin margen.

        La concurrencia es 1 a proposito (``MAX_ON_DEMAND_CONCURRENCY``): las
        peticiones las atiende el telefono principal y no conviene
        bombardearlo.
        """
        if self._busy:
            log.warning(
                "Ya hay un backfill en marcha; esta peticion no lanza otro"
            )
            return self.stats
        self._busy = True
        # Las estadisticas son DE ESTA tanda. Antes se acumulaban entre
        # sincronizaciones y el log decia "chats=64" o "chats=105" despues de
        # pulsar el boton dos veces, como si una sola pasada hubiera recorrido
        # el doble de conversaciones. Lo acumulado vive aparte.
        self.lifetime.absorber(self.stats)
        self.stats = BackfillStats()
        try:
            return await self._run_locked(client, max_rounds_per_chat, max_passes)
        finally:
            # Si esto no se suelta, un fallo a mitad deja el backfill
            # bloqueado para el resto de la vida del proceso.
            self._busy = False

    async def _run_locked(
        self, client: Any, max_rounds_per_chat: int, max_passes: int
    ) -> BackfillStats:
        """El cuerpo de :meth:`run`, ya con la excavacion reservada."""
        self._client = client
        self.refresh_own_identity()
        # Si la vinculacion cambio, los "exhausted" de la sesion anterior se
        # reabren para una comprobacion conservadora.
        self.revalidate_for_new_session()

        # Pasadas sucesivas: un chat puede quedarse a medias por el tope de
        # rondas o por un timeout puntual, y la siguiente pasada lo retoma
        # desde el cursor que quedo guardado. Se repite mientras alguna pasada
        # siga aportando mensajes; cuando una pasada entera no aporta nada, no
        # queda nada mas que pedir.
        for pass_number in range(1, max_passes + 1):
            if self._stop:
                break

            chats = self.chats_to_process()
            if not chats:
                log.info("No queda ningun chat pendiente")
                break

            before_total = self.stats.messages_new
            log.info(
                "Pasada %d/%d: candidatos reales=%d (self excluido)",
                pass_number,
                max_passes,
                len(chats),
            )

            for chat_id, chat_jid in chats:
                if self._stop:
                    break
                try:
                    await self._process_chat(chat_id, chat_jid, max_rounds_per_chat)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - un chat no arrastra al resto
                    log.exception("Fallo procesando %s", chat_jid)
                    self._set_status(chat_jid, "error", str(exc)[:400])
                    self.stats.errors += 1
                self.stats.chats_processed += 1

            gained = self.stats.messages_new - before_total
            log.info("Pasada %d: %d mensajes nuevos", pass_number, gained)
            if gained == 0:
                log.info(
                    "Una pasada completa sin mensajes nuevos: no queda mas que pedir"
                )
                break

        log.info("Backfill terminado: %s", self.stats)
        return self.stats

    def abort_pending(self, reason: str = "conexion perdida") -> int:
        """Despierta a todo el que espera una respuesta que ya no puede llegar.

        Si el socket muere con peticiones en vuelo, sus esperas se quedan
        contando hasta agotar el tiempo (45 s cada una) por algo que es
        imposible. Peor aun: ese agotamiento se apuntaria como "el telefono no
        contesto", que es una conclusion falsa sobre ese chat.

        Devuelve cuantas esperas se han cortado.
        """
        pendientes = list(self._pending.values())
        for pending in pendientes:
            pending.transport_lost = True
            pending.event.set()
        if pendientes:
            log.warning(
                "%s: se cortan %d espera(s) de historial en vuelo; el cursor "
                "se conserva y el chat se reintentara al reconectar",
                reason,
                len(pendientes),
            )
        return len(pendientes)

    @property
    def busy(self) -> bool:
        """``True`` si hay una excavacion en marcha, venga de donde venga."""
        return self._busy or bool(self._in_flight)

    @property
    def in_flight(self) -> frozenset[str]:
        """Chats con una peticion ON_DEMAND viva ahora mismo."""
        return frozenset(self._in_flight)

    def stop(self) -> None:
        self._stop = True

    async def _process_chat(self, chat_id: int, chat_jid: str, max_rounds: int) -> None:
        """Pide historial de un chat hasta agotarlo DE VERDAD.

        Condiciones de parada, todas basadas en evidencia:

        * no hay cursor real que anclar           -> no_valid_cursor
        * el telefono dice que no le queda nada   -> exhausted
        * respuestas validas seguidas sin aportar -> exhausted
        * la peticion no obtiene respuesta        -> timeout

        Lo que NO detiene un chat: recibir 47, 48 o 49 en vez de 50. Lo que
        cuenta es si entraron mensajes nuevos, no cuantos vinieron. Y si el
        telefono responde COMPLETE_BUT_MORE_MESSAGES_REMAIN_ON_PRIMARY se
        sigue pidiendo: el propio servidor esta diciendo que hay mas.
        """
        if chat_jid in self._in_flight:
            # Ya hay una peticion viva para este chat. Se descarta esta en vez
            # de encadenar una segunda: el telefono atiende de una en una y
            # dos respuestas cruzadas no se pueden atribuir.
            log.debug("%s ya tiene una peticion en vuelo; se omite", _short(chat_jid))
            return
        self._in_flight.add(chat_jid)
        try:
            await self._process_chat_locked(chat_id, chat_jid, max_rounds)
        finally:
            self._in_flight.discard(chat_jid)

    async def _process_chat_locked(
        self, chat_id: int, chat_jid: str, max_rounds: int
    ) -> None:
        """El cuerpo de :meth:`_process_chat`, ya con el chat reservado."""
        for round_number in range(1, max_rounds + 1):
            if self._stop:
                return

            with self._database.transaction() as session:
                # El cursor se RECALCULA en cada vuelta desde PostgreSQL: tras
                # insertar un bloque puede existir un mensaje mas antiguo con
                # ID real que el usado la vez anterior.
                cursor = repo.get_oldest_valid_history_cursor(session, chat_jid)
                before = repo.count_messages(session, chat_jid)

            if cursor is None:
                self._set_status(chat_jid, "no_valid_cursor", None)
                self.stats.no_cursor += 1
                log.info("%s: sin cursor valido, se omite", _short(chat_jid))
                return

            with self._database.transaction() as session:
                oldest_stored = repo.get_oldest_stored_timestamp(session, chat_jid)
                from app.models import Message as MessageRow

                from_me = session.execute(
                    select(MessageRow.from_me).where(
                        MessageRow.chat_jid == chat_jid,
                        MessageRow.whatsapp_message_id == cursor.message_id,
                    )
                ).scalar_one_or_none()

            log.info("Cursor chat=%s ronda=%d", _short(chat_jid), round_number)
            log.info("almacenado_desde=%s", _fmt_ts(oldest_stored))
            log.info(
                "cursor_id=%s (%s)",
                cursor.message_id,
                "valido" if repo.is_valid_history_cursor_id(cursor.message_id) else "INVALIDO",
            )
            log.info(
                "cursor_timestamp=%d unit=seconds (%s)",
                cursor.timestamp,
                _fmt_ts(cursor.timestamp),
            )
            log.info("from_me=%s", bool(from_me))
            log.info("count=%d", self._settings.history_on_demand_count)

            self._set_status(chat_jid, "fetching", None)

            # Se abre la ventana de observacion JUSTO antes de pedir. Lo que
            # cuente aqui viene de la ingesta de History Sync, que es la unica
            # via por la que entra historial; los mensajes en vivo los guarda
            # otro servicio y no tocan este contador.
            self._ingest_watch = {"inserted": 0, "blob_messages": 0}
            try:
                received = await self._request_once(chat_id, chat_jid, cursor)
            finally:
                observado = self._ingest_watch or {"inserted": 0, "blob_messages": 0}
                self._ingest_watch = None
            end_type = self._last_end_type

            with self._database.transaction() as session:
                after = repo.count_messages(session, chat_jid)

            # Lo que trajo ESTA peticion, no lo que crecio la tabla.
            gained = int(observado["inserted"]) if received else 0
            # Solo para el diagnostico: cuanto crecio el chat por otras vias
            # (mensajes nuevos que entraron mientras se esperaba).
            en_vivo = max(0, (after - before) - gained)
            self.stats.messages_new += gained

            log.info(
                "%s ronda %d: respuesta=%s blob=%d insertados_del_blob=%d "
                "en_vivo_durante_la_espera=%d (total %d)",
                _short(chat_jid),
                round_number,
                "si" if received else "no",
                observado["blob_messages"],
                gained,
                en_vivo,
                after,
            )

            if not received:
                if self._last_transport_lost:
                    # La linea se cayo: el chat sigue siendo excavable y su
                    # cursor sirve. Se deja en 'pending' para que el proximo
                    # ciclo lo retome, no en 'timeout', que diria algo falso
                    # sobre el telefono.
                    self._set_status(
                        chat_jid, "pending", "transporte perdido; se reintentara"
                    )
                    log.info(
                        "%s: se reintentara al reconectar (cursor conservado)",
                        _short(chat_jid),
                    )
                    return
                self._set_status(chat_jid, "timeout", "sin respuesta ON_DEMAND")
                self.stats.timeouts += 1
                self._anotar_timeout_real()
                return

            if gained > 0:
                self._timeouts_seguidos = 0
                self._reset_no_progress(chat_jid)
                self._confirm_capability()
                # Aun puede quedar mas: se sigue con el cursor recalculado.
                continue

            # Respuesta valida que no aporto nada nuevo.
            if end_type is not None and end_type not in _MORE_REMAINS:
                self._set_status(chat_jid, "exhausted", _END_TYPES.get(end_type))
                log.info("%s: agotado (%s)", _short(chat_jid), _END_TYPES.get(end_type))
                return

            if self._bump_no_progress(chat_jid) >= 3:
                # Tres respuestas validas seguidas sin un solo mensaje nuevo.
                # Se da por agotado aunque el telefono siga diciendo que le
                # queda algo, para no girar en vacio indefinidamente.
                self._set_status(chat_jid, "exhausted", "sin progreso tras 3 rondas")
                log.info("%s: agotado (sin progreso)", _short(chat_jid))
                return

            log.info(
                "%s: bloque sin novedades, pero el telefono indica que queda mas; se reintenta",
                _short(chat_jid),
            )

        # Se acabaron las rondas permitidas, NO el historial.
        self._set_status(chat_jid, "server_limited", f"tope de {max_rounds} rondas")
        log.info(
            "%s: alcanzado el tope de %d rondas; queda historial pendiente",
            _short(chat_jid),
            max_rounds,
        )

    async def _request_once(self, chat_id: int, chat_jid: str, cursor: Any) -> bool:
        """Emite una peticion y espera su respuesta. ``True`` si llego."""
        with self._database.transaction() as session:
            from app.models import Message as MessageRow

            from_me = session.execute(
                select(MessageRow.from_me).where(
                    MessageRow.chat_jid == chat_jid,
                    MessageRow.whatsapp_message_id == cursor.message_id,
                )
            ).scalar_one_or_none()

        message = build_on_demand_message(
            chat_jid=chat_jid,
            oldest_message_id=cursor.message_id,
            oldest_from_me=bool(from_me),
            oldest_timestamp=cursor.timestamp,
            count=self._settings.history_on_demand_count,
            account_lid=self._account_lid(),
        )

        pending = _Pending(chat_jid=chat_jid)
        self._pending[chat_jid] = pending
        request_row = self._record_request(chat_id, chat_jid, cursor)

        try:
            from app.compat.peer_message import peer_mode

            sender = self._client._sender
            # category="peer": marca la stanza como operacion entre
            # dispositivos de la misma cuenta. Sin esto el servidor la
            # confirma con un ACK y la descarta sin responder.
            target = self._target_jid()
            log.info("chat_jid=%s", _short(chat_jid))
            log.info(
                "peer_destination=%s***@%s",
                target.user[:6],
                target.server,
            )
            log.info(
                "destination_type=%s device=%d",
                "LID" if target.server == "lid" else "PN",
                target.device,
            )
            log.info("category=peer shape=bare")
            with peer_mode(sender):
                sent = await sender.send_message(target, message)
            self.stats.requests_sent += 1
            self._mark_request(request_row, status="sent", protocol_id=getattr(sent, "id", None))
            # Estado 1 de 3. El ACK del transporte solo dice que la stanza
            # llego; no dice nada del contenido de la operacion.
            log.info(
                "REQUEST_SENT id=%s shape=bare target=pn category=peer destino=%s",
                getattr(sent, "id", "?"),
                f"{self._target_jid().user[:6]}***@s.whatsapp.net",
            )
            log.info("ACK_RECEIVED id=%s (la stanza llego; aun no es respuesta)",
                     getattr(sent, "id", "?"))
            # Con que enc_type sale la peticion depende de si hay sesion con
            # nuestro telefono. Las 73 que funcionaron salieron como 'msg'.
            sesiones = self.peer_session_state()
            log.info(
                "PEER_SESSION sesion_por_pn=%s sesion_por_lid=%s "
                "(sin sesion por PN la peticion sale como pkmsg)",
                sesiones.get("pn"),
                sesiones.get("lid"),
            )
            log.info(
                "Esperando HISTORY_SYNC_NOTIFICATION hasta %.0fs...",
                self._settings.history_request_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            self._pending.pop(chat_jid, None)
            self._mark_request(request_row, status="error", error=str(exc)[:400])
            raise

        try:
            await asyncio.wait_for(
                pending.event.wait(), timeout=self._settings.history_request_timeout
            )
            received = not pending.transport_lost
            if pending.transport_lost:
                log.warning(
                    "TRANSPORTE PERDIDO mientras se esperaba la respuesta. NO "
                    "es que el telefono no contestara: no tuvo ocasion. El "
                    "cursor se conserva."
                )
        except (TimeoutError, asyncio.TimeoutError):
            received = False
            log.warning(
                "TIMEOUT: no llego HISTORY_SYNC_NOTIFICATION en %.0fs. "
                "El ACK solo confirmaba la entrega de la stanza.",
                self._settings.history_request_timeout,
            )
        finally:
            self._pending.pop(chat_jid, None)

        self._last_transport_lost = pending.transport_lost

        self._last_end_type = pending.end_of_history_type
        if received:
            # Estado 3 de 3: esto SI es la respuesta.
            log.info(
                "ON_DEMAND_RECEIVED chat=%s mensajes_en_el_blob=%d",
                _short(chat_jid),
                pending.messages,
            )
            self.stats.responses_received += 1
            self._mark_request(
                request_row, status="received", response_count=pending.messages
            )
            # El limite real del servidor se aprende observandolo, no
            # suponiendolo: si devuelve mucho menos de lo pedido, ahi esta.
            requested = self._settings.history_on_demand_count
            if pending.messages and pending.messages < requested:
                log.info(
                    "El servidor devolvio %d de los %d pedidos (limite del servidor)",
                    pending.messages,
                    requested,
                )
        else:
            self._mark_request(request_row, status="timeout")
        return received

    # -- Direccionamiento ----------------------------------------------------

    def _target_jid(self) -> Any:
        """Nuestro propio dispositivo PRINCIPAL (device 0), no el companion."""
        from pywhats.events import JID

        device = self._client.device
        return JID(user=device.jid.user, server=device.jid.server, device=0)

    def peer_session_state(self) -> dict[str, Any]:
        """Si hay sesion Signal con nuestro telefono, y por que direccion.

        Importa MUCHO, y se descubrio de la peor manera. Las 73 peticiones que
        funcionaron salieron con ``enc_type=msg``; las que dan timeout salen
        con ``pkmsg``. Un ``pkmsg`` significa que NO hay sesion establecida con
        el destinatario y hay que hacer X3DH primero.

        Y hay un motivo para que la sesion desaparezca. Al llegar un mensaje
        nuestro desde el LID, pywhats migra la sesion del telefono a la
        direccion LID, y ``migrate_pn_session_to_lid`` termina con::

            sessions.delete(pn_key)
            identity_store.delete(pn_key)

        Esa es justo la direccion a la que va el ON_DEMAND.

        Aqui solo se MIRA, para poder registrarlo antes de cada peticion. No se
        crea ninguna sesion, no se copia nada y no se toca criptografia.
        """
        import sqlite3

        estado: dict[str, Any] = {"pn": None, "lid": None}
        try:
            destino = self._target_jid()
            usuario_pn = destino.user
            lid = self._account_lid() or ""
            usuario_lid = lid.split("@")[0].split(".")[0] if lid else None

            uri = f"file:{self._settings.signal_store_file.as_posix()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=3.0) as conexion:
                filas = [
                    fila[0]
                    for fila in conexion.execute("SELECT session_id FROM sessions")
                ]
            estado["pn"] = any(usuario_pn in f for f in filas)
            if usuario_lid:
                estado["lid"] = any(usuario_lid in f for f in filas)
        except Exception:  # noqa: BLE001 - mirar no puede cortar la peticion
            pass
        return estado

    def _account_lid(self) -> str | None:
        device = self._client.device
        lid = getattr(device, "lid", None)
        return lid if isinstance(lid, str) and lid else None

    # -- Estado persistente --------------------------------------------------

    def _set_status(self, chat_jid: str, status: str, error: str | None) -> None:
        with self._database.transaction() as session:
            session.execute(
                update(ChatHistoryState)
                .where(ChatHistoryState.chat_jid == chat_jid)
                .values(history_status=status, last_error=error)
            )

    def _bump_no_progress(self, chat_jid: str) -> int:
        with self._database.transaction() as session:
            state = session.execute(
                select(ChatHistoryState).where(ChatHistoryState.chat_jid == chat_jid)
            ).scalar_one_or_none()
            if state is None:
                return 0
            state.consecutive_no_progress += 1
            return state.consecutive_no_progress

    def _reset_no_progress(self, chat_jid: str) -> None:
        with self._database.transaction() as session:
            session.execute(
                update(ChatHistoryState)
                .where(ChatHistoryState.chat_jid == chat_jid)
                .values(consecutive_no_progress=0, history_status="pending")
            )

    def _record_request(self, chat_id: int, chat_jid: str, cursor: Any) -> int:
        with self._database.transaction() as session:
            row = HistoryRequest(
                chat_id=chat_id,
                chat_jid=chat_jid,
                cursor_message_id=cursor.message_id,
                cursor_timestamp=cursor.timestamp,
                requested_count=self._settings.history_on_demand_count,
                status="sent",
            )
            session.add(row)
            session.flush()
            return row.id

    def _mark_request(
        self,
        request_id: int,
        *,
        status: str,
        protocol_id: str | None = None,
        response_count: int | None = None,
        error: str | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status}
        if protocol_id:
            values["protocol_request_id"] = protocol_id
        if response_count is not None:
            values["response_count"] = response_count
            values["received_at"] = _now()
        if error:
            values["error"] = error
        with self._database.transaction() as session:
            session.execute(
                update(HistoryRequest).where(HistoryRequest.id == request_id).values(**values)
            )

    def session_fingerprint(self) -> str | None:
        """Identificador NO sensible de la sesion actual.

        Se deriva del JID y el device_id del companion, que identifican la
        vinculacion sin ser material criptografico. Si se hace un pairing
        nuevo cambia, y la capability vuelve a comprobarse.
        """
        import hashlib

        device = getattr(self._client, "device", None)
        if device is None or getattr(device, "jid", None) is None:
            return None
        # ``registration_id`` entra a proposito. ``device_id`` es un NUMERO DE
        # RANURA que el servidor reutiliza: al desvincular todos los
        # dispositivos la numeracion puede volver atras, y dos identidades
        # distintas acabarian con la misma huella. Entonces se daria por
        # confirmado el historial inicial de una sesion que ya no existe y la
        # espera del bootstrap se saltaria sin que nada lo delatara. El
        # registration_id se genera nuevo en cada vinculacion.
        raw = (
            f"{device.jid.user}:{device.jid.server}:"
            f"{getattr(device, 'device_id', '')}:"
            f"{getattr(device, 'registration_id', '')}"
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def capability_confirmed(self) -> bool:
        """``True`` si ESTA sesion ya demostro que ON_DEMAND funciona.

        No basta con un booleano eterno: tras revincular hay otra sesion y la
        capacidad debe volver a comprobarse.
        """
        fingerprint = self.session_fingerprint()
        if fingerprint is None:
            return False
        with self._database.transaction() as session:
            stored = repo.get_app_state(session, CAPABILITY_KEY)
        if not isinstance(stored, dict) or not stored.get("confirmed"):
            return False
        if stored.get("state") == "SUSPECT":
            # Se confirmo en su dia, pero ha vuelto a fallar. Se comprueba
            # otra vez con un canary en vez de darlo por bueno.
            return False
        saved = stored.get("session")
        if saved is None:
            # Confirmacion antigua, sin sesion asociada. NO se acepta: fue
            # justo esta ruta la que hizo que un pairing nuevo (device .77 ->
            # .78) siguiera diciendo "capability ya confirmada" y se saltara
            # el canary. Sin evidencia de a que sesion pertenece, es UNKNOWN.
            log.info(
                "[SESSION] capability guardada sin sesion asociada -> UNKNOWN; "
                "se comprobara con un canary"
            )
            return False

        same = saved == fingerprint
        log.info(
            "[SESSION] old_fingerprint=%s new_fingerprint=%s same_session=%s "
            "ondemand_capability=%s",
            saved,
            fingerprint,
            same,
            "CONFIRMED" if same else "UNKNOWN",
        )
        return same

    def revalidate_for_new_session(self) -> int:
        """Reabre los chats agotados si la vinculacion ha cambiado.

        ``exhausted`` describe lo que respondio el telefono EN AQUELLA sesion.
        Una vinculacion nueva puede traer cursores distintos y mas historial
        disponible, asi que ese veredicto no puede ser eterno.

        Se hace de forma conservadora: los chats vuelven a ``pending`` una
        sola vez por sesion. Cada uno recibira UNA peticion; si responde 0 o
        final, vuelve a ``exhausted`` por su cuenta. La deduplicacion por
        ``whatsapp_message_id`` hace que reconsultar historial ya conocido no
        pueda duplicar nada.

        Los mensajes NO se tocan: solo el estado de extraccion.
        """
        fingerprint = self.session_fingerprint()
        if fingerprint is None:
            return 0

        with self._database.transaction() as session:
            previous = repo.get_app_state(session, SESSION_KEY)
            saved = previous.get("fingerprint") if isinstance(previous, dict) else None
            if saved == fingerprint:
                log.debug("Misma sesion de extraccion; no hace falta revalidar")
                return 0

            reopened = session.execute(
                update(ChatHistoryState)
                .where(ChatHistoryState.history_status.in_(("exhausted", "server_limited")))
                .values(
                    history_status="pending",
                    consecutive_no_progress=0,
                    last_error="revalidacion por sesion nueva",
                )
            ).rowcount or 0
            repo.set_app_state(
                session, SESSION_KEY, {"fingerprint": fingerprint, "at": int(time.time())}
            )

        if reopened:
            log.info(
                "Sesion de extraccion nueva: %d chats agotados vuelven a revisarse "
                "(los mensajes no se tocan)",
                reopened,
            )
        return reopened

    # Timeouts reales seguidos antes de dudar de la capacidad.
    MAX_TIMEOUTS_ANTES_DE_DUDAR = 2

    def _anotar_timeout_real(self) -> None:
        """Un timeout con transporte sano pone la capacidad en entredicho.

        ``CONFIRMED`` significaba "esta sesion demostro que ON_DEMAND
        funciona", y se guardaba para siempre. Eso hacia que se omitiera el
        canary aunque acabaran de fallar dos peticiones seguidas: el sistema
        seguia dando por buena una capacidad que ya no se estaba cumpliendo.
        """
        self._timeouts_seguidos += 1
        if self._timeouts_seguidos < self.MAX_TIMEOUTS_ANTES_DE_DUDAR:
            return
        if self.capability_state() != "CONFIRMED":
            return

        log.warning(
            "%d timeouts reales seguidos con el transporte sano: la capacidad "
            "ON_DEMAND pasa de CONFIRMED a SUSPECT. En el proximo ciclo se "
            "volvera a comprobar con un canary. NO se borra nada.",
            self._timeouts_seguidos,
        )
        self._set_capability_state("SUSPECT")

    def capability_state(self) -> str:
        """``CONFIRMED`` / ``SUSPECT`` / ``UNKNOWN`` para ESTA sesion."""
        fingerprint = self.session_fingerprint()
        if fingerprint is None:
            return "UNKNOWN"
        with self._database.transaction() as session:
            stored = repo.get_app_state(session, CAPABILITY_KEY)
        if not isinstance(stored, dict) or stored.get("session") != fingerprint:
            return "UNKNOWN"
        if stored.get("state") == "SUSPECT":
            return "SUSPECT"
        return "CONFIRMED" if stored.get("confirmed") else "UNKNOWN"

    def _set_capability_state(self, estado: str) -> None:
        """Marca el estado SIN borrar la confirmacion original."""
        fingerprint = self.session_fingerprint()
        with self._database.transaction() as session:
            stored = repo.get_app_state(session, CAPABILITY_KEY)
            datos = dict(stored) if isinstance(stored, dict) else {}
            datos["session"] = fingerprint
            datos["state"] = estado
            repo.set_app_state(session, CAPABILITY_KEY, datos)

    def _confirm_capability(self) -> None:
        """Una vez que ON_DEMAND funciona, se deja constancia permanente.

        A partir de ahi un timeout es un problema DE ESE CHAT, y no vuelve a
        declararse que "WhatsApp no soporta historial".
        """
        fingerprint = self.session_fingerprint()
        with self._database.transaction() as session:
            stored = repo.get_app_state(session, CAPABILITY_KEY)
            ya = isinstance(stored, dict) and stored.get("session") == fingerprint
            if not ya:
                repo.set_app_state(
                    session,
                    CAPABILITY_KEY,
                    {
                        "confirmed": True,
                        "at": int(time.time()),
                        # Huella de la vinculacion, no material sensible.
                        "session": fingerprint,
                    },
                )
                log.info("ON_DEMAND confirmado para esta sesion")


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _fmt_ts(value: int | None) -> str:
    if not value:
        return "-"
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value, tz=timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _short(jid: str) -> str:
    user, _, server = jid.partition("@")
    return f"{user[:6]}...@{server}"
