"""Recuperacion historica mediante HISTORY_SYNC_ON_DEMAND.

Arquitectura (seccion 27 del brief). La peticion NO se envia al contacto cuya
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
esta capa NO puede darse por funcionando (seccion 60).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update

from app import repository as repo
from app.config import Settings
from app.database import Database
from app.logging_setup import get_logger
from app.models import Chat, ChatHistoryState, HistoryRequest

log = get_logger("BACKFILL")

# Valor del enum ProtocolMessage.Type. Lo define pywhats, no se inventa.
PEER_DATA_OPERATION_REQUEST_MESSAGE = 16

# Bandera persistida en app_state la primera vez que ON_DEMAND devuelve algo.
CAPABILITY_KEY = "ondemand_capability_confirmed"

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

    from app.proto import HISTORY_SYNC_ON_DEMAND, OnDemandMessage

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
        self._settings = settings
        self._database = database
        # El cliente de pywhats no existe hasta que el hilo arranca y la
        # sesion queda conectada, asi que se recibe en run().
        self._client: Any = None
        self._pending: dict[str, _Pending] = {}
        self.stats = BackfillStats()
        self._stop = False
        # endOfHistoryTransferType de la ultima respuesta, para decidir si el
        # chat esta agotado de verdad o solo limitado por ahora.
        self._last_end_type: int | None = None
        # Identidad propia (PN y LID). Se rellena con set_own_identity().
        self._own_jids: set[str] = set()

    # -- Correlacion de respuestas -------------------------------------------

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
        self._client = client
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

        log.warning(
            "CANARY sin exito: nuevos=%d retrocedio=%s. No se activa el backfill global.",
            gained,
            went_back,
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
        self._client = client

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
                "Pasada %d/%d: %d chats candidatos", pass_number, max_passes, len(chats)
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

    def stop(self) -> None:
        self._stop = True

    async def _process_chat(self, chat_id: int, chat_jid: str, max_rounds: int) -> None:
        """Pide historial de un chat hasta agotarlo DE VERDAD.

        Condiciones de parada, todas basadas en evidencia (seccion 14):

        * no hay cursor real que anclar           -> no_valid_cursor
        * el telefono dice que no le queda nada   -> exhausted
        * respuestas validas seguidas sin aportar -> exhausted
        * la peticion no obtiene respuesta        -> timeout

        Lo que NO detiene un chat: recibir 47, 48 o 49 en vez de 50. Lo que
        cuenta es si entraron mensajes nuevos, no cuantos vinieron. Y si el
        telefono responde COMPLETE_BUT_MORE_MESSAGES_REMAIN_ON_PRIMARY se
        sigue pidiendo: el propio servidor esta diciendo que hay mas.
        """
        for round_number in range(1, max_rounds + 1):
            if self._stop:
                return

            with self._database.transaction() as session:
                # El cursor se RECALCULA en cada vuelta desde PostgreSQL: tras
                # insertar un bloque puede existir un mensaje mas antiguo con
                # ID real que el usado la vez anterior (seccion 13).
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
            received = await self._request_once(chat_id, chat_jid, cursor)
            end_type = self._last_end_type

            with self._database.transaction() as session:
                after = repo.count_messages(session, chat_jid)
            gained = after - before
            self.stats.messages_new += gained

            log.info(
                "%s ronda %d: respuesta=%s nuevos=%d (total %d)",
                _short(chat_jid),
                round_number,
                "si" if received else "no",
                gained,
                after,
            )

            if not received:
                self._set_status(chat_jid, "timeout", "sin respuesta ON_DEMAND")
                self.stats.timeouts += 1
                return

            if gained > 0:
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
            received = True
        except (TimeoutError, asyncio.TimeoutError):
            received = False
            log.warning(
                "TIMEOUT: no llego HISTORY_SYNC_NOTIFICATION en %.0fs. "
                "El ACK solo confirmaba la entrega de la stanza.",
                self._settings.history_request_timeout,
            )
        finally:
            self._pending.pop(chat_jid, None)

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
        raw = f"{device.jid.user}:{device.jid.server}:{getattr(device, 'device_id', '')}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def capability_confirmed(self) -> bool:
        """``True`` si ESTA sesion ya demostro que ON_DEMAND funciona.

        No basta con un booleano eterno: tras revincular hay otra sesion y la
        capacidad debe volver a comprobarse (seccion 35).
        """
        fingerprint = self.session_fingerprint()
        if fingerprint is None:
            return False
        with self._database.transaction() as session:
            stored = repo.get_app_state(session, CAPABILITY_KEY)
        if not isinstance(stored, dict) or not stored.get("confirmed"):
            return False
        saved = stored.get("session")
        if saved is None:
            # Confirmacion antigua, sin sesion asociada. Se acepta y se
            # reetiqueta con la sesion actual en la proxima confirmacion.
            log.debug("Capability confirmada sin sesion asociada; se acepta")
            return True
        return saved == fingerprint

    def _confirm_capability(self) -> None:
        """Una vez que ON_DEMAND funciona, se deja constancia permanente.

        A partir de ahi un timeout es un problema DE ESE CHAT, y no vuelve a
        declararse que "WhatsApp no soporta historial" (seccion 30).
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
