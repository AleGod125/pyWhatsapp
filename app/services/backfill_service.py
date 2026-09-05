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

from sqlalchemy import func, select, update

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
    """Peticion en vuelo, esperando a que su chat aparezca en un blob.

    Se registra ANTES de enviar, nunca despues. Una respuesta puede llegar en
    menos de un segundo -- en las peticiones que si funcionaron se midieron
    latencias de ~1 s -- y un waiter creado despues del envio se perderia esa
    respuesta y esperaria los 45 s completos para nada.
    """

    chat_jid: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    messages: int = 0
    end_of_history_type: int | None = None
    #: Identificador de la stanza enviada. Es lo que el telefono devuelve en
    #: ``peerDataRequestSessionID`` (campo 12 de HistorySyncNotification), asi
    #: que permite correlacionar sin depender del contenido.
    request_id: str | None = None
    #: Como se resolvio: ``session_id`` o ``chat_jid``.
    correlacion: str | None = None
    #: Cuando arranco la espera, para medir la latencia real.
    started_at: float = field(default_factory=time.monotonic)
    latency: float | None = None
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
        #: Los identificadores que entraron en el ultimo bloque. Hacen falta
        #: para saber si el borde reciente ya empalmo con lo que habia.
        self._ultimos_wamids: list[str] = []
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
        #: A quien avisar de que un chat cambio de estado. Lo cablea el
        #: runtime; sin el, todo sigue funcionando y la pantalla se entera al
        #: recargar, que es como estaba antes.
        self.publish: Any = None
        # El cliente de pywhats no existe hasta que el hilo arranca y la
        # sesion queda conectada, asi que se recibe en run().
        self._client: Any = None
        self._pending: dict[str, _Pending] = {}
        # UNA peticion ON_DEMAND viva en todo el proceso, venga de donde venga.
        #
        # ``_busy`` protege ``run()`` y ``run_canary()``, pero la cola de
        # despertados llama a ``_process_chat`` directamente y se salta esa
        # bandera. Se midio: el 2026-09-04 a las 14:45:26 salio una peticion
        # por el camino "desperto: se le pide historial ahora" y a las 14:45:28
        # otra por el ciclo automatico -- 2 s de diferencia, las dos en vuelo.
        # El telefono atiende de una en una.
        #
        # El Lock se crea perezosamente: el servicio se construye fuera del
        # bucle donde luego se usa.
        self._ondemand_lock: asyncio.Lock | None = None
        # Mientras esto vale True, los blobs ON_DEMAND se observan pero NO se
        # persisten. Solo los ON_DEMAND: un INITIAL_BOOTSTRAP que llegue a la
        # vez se guarda como siempre, porque no es lo que se esta probando y
        # perderlo seria peor que no probar nada.
        #
        # El blob queda archivado en ``data/history/`` de todas formas, asi
        # que nada se pierde: se puede ingerir despues sin volver a pedirlo.
        self.diagnostico_sin_persistir = False
        # Respuestas ON_DEMAND que llegaron sin nadie esperandolas.
        self.respuestas_sin_waiter = 0
        self._ultimo_aviso_sin_waiter = 0.0
        # ``enc.type`` de la ultima peticion: ``msg`` (sesion establecida) o
        # ``pkmsg`` (sesion nueva). Es la diferencia medida entre las 73
        # peticiones que respondieron y las cuatro que no.
        self.ultimo_enc_type: str | None = None
        self.ultima_latencia: float | None = None
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
        # Por que fallo el ultimo canary. ``"sin_candidato"`` significa que no
        # llego a probarse nada, y eso NO puede bloquear la excavacion: el
        # canary es un diagnostico, no un permiso.
        self.last_canary_reason: str | None = None

        # -- La prueba de capacidad ------------------------------------------
        #
        # Un timeout aislado NO es una incapacidad: puede ser el telefono
        # apagado, sin datos o todavia colocandose tras reconectar. Se vuelve a
        # probar con espera creciente, y mientras tanto la capacidad queda en
        # SUSPECT -- que frena la excavacion, pero no borra nada.
        self._canary_intentos = 0
        self._proximo_canary: float | None = None
        self.metricas_de_capacidad: dict[str, int] = {
            "canary_attempts": 0,
            "canary_timeouts": 0,
            "canary_confirmed": 0,
            "confirmed_by_canary": 0,
            "confirmed_by_real_backfill": 0,
        }

    # -- Correlacion de respuestas -------------------------------------------

    def note_history_ingest(
        self,
        inserted: int,
        blob_messages: int,
        wa_msg_ids: Any = None,
    ) -> None:
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
        # Y CUALES vinieron, no solo cuantos. Para cerrar un hueco reciente
        # hace falta saber si el bloque ya alcanzo un mensaje que se tenia: un
        # duplicado no suma a `inserted`, y es justo la senal de que se
        # empalmo. Parametro opcional: quien no lo pase sigue igual.
        if wa_msg_ids:
            self._ultimos_wamids.extend(str(w) for w in wa_msg_ids if w)

    def notify_history(self, sync: Any) -> None:
        """Avisa de que llego un History Sync. Lo llama la capa de ingesta.

        DOS reglas, y las dos importan.

        Primera: solo un ``ON_DEMAND`` resuelve una espera de ON_DEMAND. Un
        ``INITIAL_BOOTSTRAP`` o un ``RECENT`` pueden traer el mismo chat
        mientras hay una peticion en vuelo, y darlos por respuesta convierte
        historial que venia solo en "el protocolo funciona".

        Segunda: se correlaciona primero por ``peerDataRequestSessionID`` --
        el campo 12, donde el telefono devuelve el identificador de NUESTRA
        stanza -- y solo despues por JID de chat. El identificador es exacto;
        el JID es lo unico que queda cuando el telefono no lo manda.
        """
        tipo = str(getattr(sync, "sync_type", "") or "")
        if tipo and tipo != "ON_DEMAND":
            # No es un descarte: la ingesta guarda el blob igual. Aqui solo se
            # decide que no despierta a nadie.
            log.debug(
                "HistorySync type=%s: no resuelve ninguna espera ON_DEMAND", tipo
            )
            return

        sesion = getattr(sync, "peer_session_id", None)
        despertados: set[str] = set()

        # 1) Por identificador de peticion, que es exacto.
        if sesion:
            for pendiente in self._pending.values():
                if pendiente.request_id and pendiente.request_id == sesion:
                    pendiente.correlacion = "session_id"
                    despertados.add(pendiente.chat_jid)

        # 2) Y por el contenido, que es lo que siempre ha funcionado.
        for conversation in sync.conversations:
            waiting = self._pending.get(conversation.jid)
            if waiting is None:
                continue
            waiting.messages += len(conversation.messages)
            waiting.end_of_history_type = conversation.end_of_history_type
            if waiting.correlacion is None:
                waiting.correlacion = "chat_jid"
            despertados.add(conversation.jid)
            if conversation.end_of_history_type is not None:
                log.info(
                    "endOfHistoryTransferType=%d (%s)",
                    conversation.end_of_history_type,
                    _END_TYPES.get(
                        conversation.end_of_history_type, "desconocido"
                    ),
                )

        for chat_jid in despertados:
            pendiente = self._pending.get(chat_jid)
            if pendiente is None:
                continue
            pendiente.latency = time.monotonic() - pendiente.started_at
            self.ultima_latencia = pendiente.latency
            if self._settings.protocol_debug:
                log.debug(
                    "[ON_DEMAND] correlation_ok chat=%s via=%s latencia=%.2fs",
                    _short(chat_jid),
                    pendiente.correlacion,
                    pendiente.latency,
                )
            pendiente.event.set()

        if not despertados:
            # Una respuesta ON_DEMAND sin nadie esperandola es informacion, no
            # ruido: dice que el telefono SI contesto y que lo que falla es la
            # correlacion. Se limita el ritmo para no inundar el log.
            self._avisar_sin_waiter(sesion, len(sync.conversations))

    #: Cada cuanto, como mucho, se repite el aviso de respuesta sin waiter.
    _SEGUNDOS_ENTRE_AVISOS = 60.0

    def _avisar_sin_waiter(self, sesion: str | None, conversaciones: int) -> None:
        """Avisa de un ON_DEMAND que llego sin nadie esperandolo."""
        ahora = time.monotonic()
        if ahora - self._ultimo_aviso_sin_waiter < self._SEGUNDOS_ENTRE_AVISOS:
            return
        self._ultimo_aviso_sin_waiter = ahora
        self.respuestas_sin_waiter += 1
        log.warning(
            "[BACKFILL] HistorySync ON_DEMAND recibido sin waiter correlacionable "
            "session=%s conversaciones=%d esperando=%d. La respuesta llego: lo "
            "que fallo es la correlacion.",
            (sesion[:8] + "...") if sesion else "ausente",
            conversaciones,
            len(self._pending),
        )

    # -- Seleccion de trabajo ------------------------------------------------

    def chats_to_process(self, limit: int = 500) -> list[tuple[int, str]]:
        """Chats que aun pueden dar mas historial.

        Se excluyen los agotados, los que no tienen cursor utilizable y los
        que estan cumpliendo su espera de reintento. El orden pone delante los
        que ya tienen mensajes: son los que mas probabilidad tienen de
        devolver algo.

        Lo de la espera importa: un chat en ``timeout`` conserva su cursor y
        vuelve a ser candidato, asi que sin ella se reintentaba en la pasada
        siguiente, y en la siguiente, ocupando la unica ranura de peticiones
        sin que nada hubiera cambiado.
        """
        from app.history.cursor import espera_cumplida

        with self._database.transaction() as session:
            rows = session.execute(
                select(Chat.id, Chat.jid, ChatHistoryState.next_retry_at)
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
            listos = []
            en_espera = 0
            for chat_id, chat_jid, proximo in rows:
                if not self.is_backfill_candidate(chat_jid):
                    continue
                if not espera_cumplida(proximo):
                    en_espera += 1
                    continue
                listos.append((chat_id, chat_jid))
        if en_espera:
            log.debug("%d chat(s) esperan su turno de reintento", en_espera)
        return listos

    def chats_with_cursor(self, limit: int = 500) -> list[tuple[int, str, Any]]:
        """Los candidatos que ADEMAS tienen un ancla real, con ella.

        Una sola definicion de "tiene cursor", la de
        :func:`app.history.cursor.get_valid_history_cursor`. Es lo que usan el
        canary y la excavacion, y por eso ya no pueden discrepar.
        """
        from app.history.cursor import get_valid_history_cursor

        con_ancla: list[tuple[int, str, Any]] = []
        candidatos = self.chats_to_process(limit)
        with self._database.transaction() as session:
            for chat_id, chat_jid in candidatos:
                cursor = get_valid_history_cursor(
                    session, chat_id=chat_id, chat_jid=chat_jid
                )
                if cursor is not None:
                    con_ancla.append((chat_id, chat_jid, cursor))
        return con_ancla

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
        """Un solo chat para la prueba, con cursor real.

        Se PREFIERE un chat individual con varios mensajes, que es el mas
        facil de verificar a mano. Pero eso es una preferencia, no un
        requisito: antes lo era, y por eso el canary decia "no hay ningun chat
        con cursor valido" mientras la excavacion encontraba uno y le mandaba
        una peticion que el servidor confirmaba. Se midio sobre la base real:
        tres chats con cursor, uno solo que cumplia los extras.

        El cursor se decide con la MISMA funcion que usa la excavacion. Que
        estas dos discreparan era el bug.
        """
        candidatos = self.chats_with_cursor()
        if not candidatos:
            return None

        # Quien ya contesto, y quien ya fallo. Se lee una vez, no por chat.
        historial = self._historial_de_respuestas()

        def orden(fila: tuple[int, str, Any]) -> tuple[int, int, int, int]:
            _, chat_jid, _ = fila
            with self._database.transaction() as session:
                mensajes = repo.count_messages(session, chat_jid)
            respondidas, fallidas = historial.get(chat_jid, (0, 0))
            # 1. El que YA contesto en esta base. Es una PRUEBA: para probar
            #    que el protocolo funciona hay que elegir el objetivo con mas
            #    probabilidad de contestar, no uno cualquiera.
            #
            #    Se midio el caso contrario: la prueba eligio un chat que ya
            #    habia agotado dos esperas (`intento=3`), volvio a no
            #    contestar, y de ahi salio un SUSPECT que dejo parada la
            #    recuperacion entera de una cuenta con 89 respuestas buenas.
            probado = 0 if respondidas > 0 else 1
            # 2. Y entre los que nunca contestaron, el que menos ha fallado.
            # 3. Despues, la preferencia de siempre: individual con >=2.
            preferido = 0 if (not chat_jid.endswith("@g.us") and mensajes >= 2) else 1
            return probado, fallidas, preferido, -mensajes

        mejor = min(candidatos, key=orden)
        return mejor[0], mejor[1]

    def _historial_de_respuestas(self) -> dict[str, tuple[int, int]]:
        """Por chat: cuantas peticiones respondio y cuantas se le agotaron.

        Sale de ``history_requests``, que es el registro de lo que de verdad
        paso. No se deduce del estado del chat: un chat en ``timeout`` puede
        haber respondido noventa veces antes.
        """
        try:
            with self._database.transaction() as session:
                filas = session.execute(
                    select(
                        HistoryRequest.chat_jid,
                        HistoryRequest.status,
                        func.count(),
                    ).group_by(HistoryRequest.chat_jid, HistoryRequest.status)
                ).all()
        except Exception:  # noqa: BLE001 - sin historial se elige como antes
            log.debug("No se pudo leer el historial de peticiones", exc_info=True)
            return {}

        resumen: dict[str, tuple[int, int]] = {}
        for chat_jid, estado, cuantas in filas:
            if not chat_jid:
                continue
            respondidas, fallidas = resumen.get(chat_jid, (0, 0))
            if estado == "received":
                respondidas += int(cuantas or 0)
            elif estado == "timeout":
                fallidas += int(cuantas or 0)
            resumen[chat_jid] = (respondidas, fallidas)
        return resumen

    def diagnostico_de_seleccion(self) -> dict[str, int]:
        """Por que no hay canary, en numeros. Para no tener que adivinarlo."""
        candidatos = self.chats_to_process()
        with self._database.transaction() as session:
            total_chats = int(
                session.execute(select(func.count()).select_from(Chat)).scalar() or 0
            )
            esperando = int(
                session.execute(
                    select(func.count())
                    .select_from(ChatHistoryState)
                    .where(ChatHistoryState.history_status == "waiting_seed")
                ).scalar()
                or 0
            )
        return {
            "chats": total_chats,
            "candidatos": len(candidatos),
            "con_cursor": len(self.chats_with_cursor()),
            "esperando_ancla": esperando,
        }

    async def run_canary(self, client: Any, *, max_rounds: int = 3) -> bool:
        """Prueba UN solo chat. No avanza al resto hasta que funcione.

        Devuelve ``True`` si ON_DEMAND devolvio mensajes de verdad.
        """
        if self._busy:
            # La prueba y el trabajo normal piden al MISMO telefono. Lanzar
            # las dos a la vez no confirma antes: solo compite consigo mismo.
            log.warning("Ya hay una excavacion en marcha; el canary no lanza otra")
            return False
        self._busy = True
        self.metricas_de_capacidad["canary_attempts"] += 1
        log.info("[CAPABILITY] canary enviado")
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
            # El motivo exacto, no una conclusion. "No hay cursor valido" se
            # decia tambien cuando si lo habia y lo que fallaba eran los
            # filtros extra del canary.
            numeros = self.diagnostico_de_seleccion()
            self.last_canary_reason = "sin_candidato"
            log.warning(
                "CANARY sin objetivo: %d conversacion(es), %d candidata(s), "
                "%d con ancla real, %d esperando ancla. No se ha probado nada, "
                "asi que esto NO dice que ON_DEMAND no funcione.",
                numeros["chats"],
                numeros["candidatos"],
                numeros["con_cursor"],
                numeros["esperando_ancla"],
            )
            return False
        self.last_canary_reason = None

        chat_id, chat_jid = target
        with self._database.transaction() as session:
            before = repo.count_messages(session, chat_jid)
            oldest_before = repo.get_oldest_stored_timestamp(session, chat_jid)

        log.debug("CANARY START")
        log.debug("Chat=%s", _short(chat_jid))
        log.debug("Before count=%d", before)
        log.debug("Before oldest=%s", _fmt_ts(oldest_before))

        await self._process_chat(chat_id, chat_jid, max_rounds)

        with self._database.transaction() as session:
            after = repo.count_messages(session, chat_jid)
            oldest_after = repo.get_oldest_stored_timestamp(session, chat_jid)

        log.debug("After count=%d", after)
        log.debug("After oldest=%s", _fmt_ts(oldest_after))

        gained = after - before
        went_back = (
            oldest_after is not None
            and oldest_before is not None
            and oldest_after < oldest_before
        )

        if gained > 0 and went_back:
            log.info(
                "[CAPABILITY] canary con respuesta: +%d mensajes, historial "
                "retrocedio; CONFIRMED",
                gained,
            )
            self.metricas_de_capacidad["canary_confirmed"] += 1
            self.metricas_de_capacidad["confirmed_by_canary"] += 1
            self._canary_intentos = 0
            self._proximo_canary = None
            self._confirm_capability()
            return True

        # Una respuesta VALIDA con 0 mensajes agota ESE chat; no dice nada
        # malo del protocolo. Si el telefono contesto, ON_DEMAND funciona y el
        # backfill global debe continuar con los demas chats.
        if self.stats.responses_received > 0:
            log.info(
                "[CAPABILITY] el protocolo responde (respuestas=%d) pero este "
                "chat no tenia mas historial; CONFIRMED y se continua con el "
                "resto.",
                self.stats.responses_received,
            )
            self.metricas_de_capacidad["canary_confirmed"] += 1
            self.metricas_de_capacidad["confirmed_by_canary"] += 1
            self._canary_intentos = 0
            self._proximo_canary = None
            self._confirm_capability()
            return True

        # Sin respuesta. NO se borra la sesion, ni se toca Signal, ni se marca
        # una incapacidad definitiva: se deja SUSPECT y se vuelve a probar.
        self.metricas_de_capacidad["canary_timeouts"] += 1
        log.warning(
            "[CAPABILITY] canary sin respuesta (timeouts=%d): no se puede "
            "confirmar la capacidad. Comprueba que el telefono este encendido "
            "y con datos.",
            self.stats.timeouts,
        )
        self.programar_reintento_de_canary()
        return False

    # -- El borde reciente ---------------------------------------------------
    #
    # Segunda dimension, no una variante del backfill de siempre. Aquel excava
    # HACIA ATRAS desde el ancla mas antigua; esto parte de una referencia MAS
    # NUEVA y baja hasta empalmar con lo que ya hay.
    #
    # Lo unico que cambia es de que ancla se parte. La peticion se construye y
    # se correlaciona con `_request_once`, igual que todo lo demas: mismo turno
    # unico en todo el proceso, mismo waiter antes del envio, misma huella de
    # sesion. Duplicar ese camino seria duplicar la parte que costo mas
    # entender de este proyecto.

    async def rellenar_borde_reciente(
        self, client: Any, ancla: Any, *, db_mas_nuevo: int | None
    ) -> Any:
        """Pide bloques desde ``ancla`` hacia atras hasta empalmar.

        NO toca el cursor historico, NO reabre un ``exhausted`` y NO escribe
        ningun ancla: los mensajes entran por la ingesta de siempre, que ya
        deduplica por ``whatsapp_message_id``.
        """
        from app.history.recent_gap import (
            COMPLETO,
            RELLENANDO,
            AGOTADO,
            ResultadoDeRelleno,
            decidir_siguiente,
            empalmo,
        )

        self._client = client
        resultado = ResultadoDeRelleno(estado=RELLENANDO)
        conocidos = self._wamids_de(ancla.chat_jid)
        rondas_sin_avance = 0

        while True:
            self._ingest_watch = {"inserted": 0, "blob_messages": 0}
            self._ultimos_wamids = []
            try:
                recibido = await self._request_once(
                    ancla.chat_id, ancla.chat_jid, ancla
                )
            finally:
                observado = self._ingest_watch or {"inserted": 0, "blob_messages": 0}
                self._ingest_watch = None
            resultado.bloques += 1

            if not recibido:
                # Un timeout aislado NO es un fallo permanente del hueco: la
                # espera creciente la lleva el motor de siempre.
                resultado.estado = AGOTADO
                resultado.motivo = "sin respuesta"
                self._anotar_timeout_real()
                return resultado

            # Respuesta valida y correlacionada: eso demuestra la capacidad,
            # venga de donde venga la peticion.
            self._confirmar_por_respuesta_real()

            nuevos = int(observado["inserted"])
            resultado.mensajes += nuevos
            recibidos = list(self._ultimos_wamids or [])

            marca = None
            if recibidos:
                marca = self._marca_mas_antigua(recibidos)
            como = empalmo(
                wamids_recibidos=recibidos,
                wamids_conocidos=conocidos,
                marca_mas_antigua_recibida=marca,
                db_mas_nuevo=db_mas_nuevo,
            )
            if como is not None:
                resultado.estado = COMPLETO
                resultado.empalme = como
                return resultado

            rondas_sin_avance = 0 if nuevos else rondas_sin_avance + 1
            seguir, estado, motivo = decidir_siguiente(
                mensajes_del_bloque=nuevos,
                rondas_sin_avance=rondas_sin_avance,
                tipo_de_fin=self._last_end_type,
                bloques=resultado.bloques,
            )
            if not seguir:
                resultado.estado = estado
                resultado.motivo = motivo
                return resultado

            # El ancla del siguiente bloque es el mensaje mas antiguo que
            # acaba de llegar: seguir con la misma pediria lo mismo otra vez.
            siguiente = self._ancla_siguiente(ancla, recibidos)
            if siguiente is None:
                resultado.estado = estado
                resultado.motivo = "no llego ninguna referencia con la que seguir"
                return resultado
            ancla = siguiente
            conocidos = self._wamids_de(ancla.chat_jid)

    def _wamids_de(self, chat_jid: str) -> set[str]:
        """Los identificadores que YA estan guardados de esa conversacion."""
        with self._database.transaction() as sesion:
            return {
                w
                for w in sesion.execute(
                    select(Message.whatsapp_message_id).where(
                        Message.chat_jid == chat_jid
                    )
                ).scalars()
                if w
            }

    def _marca_mas_antigua(self, wamids: list[str]) -> int | None:
        with self._database.transaction() as sesion:
            return sesion.execute(
                select(func.min(Message.timestamp)).where(
                    Message.whatsapp_message_id.in_(wamids)
                )
            ).scalar()

    def _ancla_siguiente(self, ancla: Any, wamids: list[str]) -> Any:
        """El mensaje mas antiguo del bloque que acaba de entrar.

        Se lee de la base, no del blob: asi el ancla siguiente es un mensaje
        que existe de verdad, con su marca y su direccion tal y como quedaron
        guardadas. Nada se fabrica.
        """
        import dataclasses

        if not wamids:
            return None
        with self._database.transaction() as sesion:
            fila = sesion.execute(
                select(
                    Message.whatsapp_message_id,
                    Message.timestamp,
                    Message.from_me,
                )
                .where(Message.whatsapp_message_id.in_(wamids))
                .order_by(Message.timestamp.asc())
                .limit(1)
            ).first()
        if fila is None or not fila[0]:
            return None
        return dataclasses.replace(
            ancla,
            wa_msg_id=fila[0],
            timestamp=int(fila[1]),
            from_me=bool(fila[2]),
            source="recent_gap",
        )

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
            peticiones_antes = self.stats.requests_sent
            # ``candidatos`` y ``con_cursor`` son cosas DISTINTAS: a los
            # primeros se les mira, a los segundos se les puede pedir. Que el
            # segundo sea 0 no dice nada sobre cuantos esperan.
            con_cursor = len(self.chats_with_cursor())
            log.info(
                "candidatos=%d con_cursor=%d",
                len(chats),
                con_cursor,
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
            log.info(
                "pasada %d/%d: peticiones=%d mensajes_nuevos=%d",
                pass_number,
                max_passes,
                self.stats.requests_sent - peticiones_antes,
                gained,
            )
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

            from app.history.cursor import get_valid_history_cursor, persist_cursor

            with self._database.transaction() as session:
                # El cursor se RECALCULA en cada vuelta: tras insertar un
                # bloque puede existir un mensaje mas antiguo con ID real que
                # el usado la vez anterior. La funcion es la MISMA que usa el
                # canary; dos definiciones distintas era el bug.
                cursor = get_valid_history_cursor(
                    session, chat_id=chat_id, chat_jid=chat_jid
                )
                before = repo.count_messages(session, chat_jid)
                oldest_stored = repo.get_oldest_stored_timestamp(session, chat_jid)
                if cursor is not None:
                    # Se guarda ANTES de pedir: si el proceso muere a mitad,
                    # el ancla sigue ahi y el chat se retoma sin volver a
                    # buscarla.
                    persist_cursor(session, chat_jid, cursor)

            if cursor is None:
                self._set_status(chat_jid, "no_valid_cursor", None)
                self.stats.no_cursor += 1
                # A DEBUG: una linea por conversacion sin ancla son 26 lineas
                # que dicen lo mismo. El recuento va en el resumen de la pasada.
                log.debug("%s: sin ancla real, se omite", _short(chat_jid))
                return

            # Detalle de protocolo: a DEBUG. Eran seis lineas por ronda y por
            # chat, y con cuarenta conversaciones enterraban el resultado.
            # PROTOCOL_DEBUG=true las devuelve todas.
            log.debug(
                "Cursor chat=%s ronda=%d origen=%s almacenado_desde=%s "
                "cursor_ts=%d (%s) from_me=%s count=%d",
                _short(chat_jid),
                round_number,
                cursor.source,
                _fmt_ts(oldest_stored),
                cursor.timestamp,
                _fmt_ts(cursor.timestamp),
                cursor.from_me,
                self._settings.history_on_demand_count,
            )

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

            log.debug(
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
                # El cursor NO se toca. Un timeout dice que el telefono no
                # contesto, no que el ancla sea mala; borrarla convertiria un
                # chat recuperable en uno que vuelve a esperar una semilla que
                # ya tiene.
                self._set_status(chat_jid, "timeout", "sin respuesta ON_DEMAND")
                intento, proximo = self._anotar_reintento(chat_jid)
                self.stats.timeouts += 1
                self._anotar_timeout_real()
                log.info(
                    "[BACKFILL] chat=%s timeout intento=%d; se reintenta en %s "
                    "(ancla conservada)",
                    _short(chat_jid),
                    intento,
                    _fmt_ts(int(proximo.timestamp())) if proximo else "?",
                )
                return

            if received:
                # LA respuesta llego y estaba correlacionada: eso es la prueba
                # de que ON_DEMAND funciona en esta sesion, traiga mensajes
                # nuevos o no.
                #
                # Antes solo confirmaba si ademas venian mensajes. Un chat que
                # ya estaba al dia contestaba correctamente, se marcaba
                # `exhausted` y la capacidad seguia en SUSPECT: hacia falta que
                # el canary acertara con un chat que ademas tuviera historial
                # por recuperar. Una respuesta valida es una respuesta valida.
                self._confirmar_por_respuesta_real()

            if gained > 0:
                self._limpiar_reintentos(chat_jid)
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

    async def request_diagnostico(
        self,
        client: Any,
        *,
        chat_jid: str,
        message_id: str,
        timestamp: int,
        from_me: bool,
    ) -> dict[str, Any]:
        """UNA peticion con un cursor DADO, sin tocar el estado del chat.

        Es el camino aislado. Usa el mismo constructor, el mismo destino, el
        mismo waiter y la misma correlacion que la excavacion normal -- no
        puede haber un camino especial que funcione aqui y falle alli -- pero
        no escribe cursor, ni ``history_status``, ni contador de intentos, ni
        fila en ``history_requests``, ni mensajes.

        Lo unico que puede cambiar es la capacidad, y solo si llega una
        respuesta ON_DEMAND correlacionada de verdad. Un ACK no basta.
        """
        if self._busy or self._in_flight:
            return {"error": "busy", "in_flight": sorted(self._in_flight)}

        self._client = client
        self._busy = True  # ni ciclo automatico ni canary mientras tanto
        try:
            async with self._lock_ondemand():
                return await self._diagnostico_locked(
                    chat_jid, message_id, timestamp, from_me
                )
        finally:
            self._busy = False

    async def _diagnostico_locked(
        self, chat_jid: str, message_id: str, timestamp: int, from_me: bool
    ) -> dict[str, Any]:
        from app.compat.peer_message import peer_mode, ultimo_enc_type

        capacidad_antes = self.capability_state()
        sesiones = self.peer_session_state()
        mensaje = build_on_demand_message(
            chat_jid=chat_jid,
            oldest_message_id=message_id,
            oldest_from_me=from_me,
            oldest_timestamp=timestamp,
            count=self._settings.history_on_demand_count,
            account_lid=self._account_lid(),
        )
        bytes_peticion = len(mensaje.SerializeToString())

        # El waiter, ANTES de enviar.
        pendiente = _Pending(chat_jid=chat_jid)
        self._pending[chat_jid] = pendiente
        self._in_flight.add(chat_jid)

        resultado: dict[str, Any] = {
            "chat_type": "group" if chat_jid.endswith("@g.us") else "individual",
            "cursor_timestamp": timestamp,
            "from_me": from_me,
            "request_bytes": bytes_peticion,
            "signal_session_pn": sesiones.get("pn"),
            "signal_session_lid": sesiones.get("lid"),
            "destination_primary": True,
            "device": 0,
            "count": self._settings.history_on_demand_count,
            "capability_before": capacidad_antes,
            # Se dice explicitamente lo que este camino NO hace.
            "cursor_written": False,
            "history_status_changed": False,
            "attempts_incremented": False,
            "messages_persisted": False,
        }

        # Los blobs ON_DEMAND de esta prueba se observan y no se guardan.
        self.diagnostico_sin_persistir = True
        try:
            sender = self._client._sender
            destino = self._target_jid()
            enviado_en = time.monotonic()
            with peer_mode(sender):
                enviada = await sender.send_message(destino, mensaje)
            resultado["ack"] = True
            resultado["ack_ms"] = round((time.monotonic() - enviado_en) * 1000.0)
            resultado["request_sent"] = True
            protocolo = getattr(enviada, "id", None)
            pendiente.request_id = protocolo
            pendiente.started_at = time.monotonic()
            self.ultimo_enc_type = ultimo_enc_type(sender)
            resultado["enc_type"] = self.ultimo_enc_type
            resultado["device_identity"] = self.ultimo_enc_type == "pkmsg"
            log.info(
                "[ON_DEMAND_TEST] enviada chat=%s enc=%s ack_ms=%s",
                _short(chat_jid),
                resultado["enc_type"],
                resultado["ack_ms"],
            )

            try:
                await asyncio.wait_for(
                    pendiente.event.wait(),
                    timeout=self._settings.history_request_timeout,
                )
                recibida = not pendiente.transport_lost
            except (TimeoutError, asyncio.TimeoutError):
                recibida = False
        except Exception as exc:  # noqa: BLE001 - se reporta, no se traga
            resultado["error"] = str(exc)[:300]
            resultado["request_sent"] = resultado.get("request_sent", False)
            log.exception("[ON_DEMAND_TEST] la peticion de diagnostico fallo")
            return resultado
        finally:
            self.diagnostico_sin_persistir = False
            self._pending.pop(chat_jid, None)
            self._in_flight.discard(chat_jid)

        resultado["history_response"] = recibida
        resultado["messages"] = pendiente.messages if recibida else 0
        resultado["latency_seconds"] = (
            round(pendiente.latency, 2) if pendiente.latency is not None else None
        )
        resultado["correlation"] = pendiente.correlacion
        fin = pendiente.end_of_history_type
        resultado["end_of_history_type"] = fin
        resultado["result"] = (
            None if fin is None else ("MORE" if fin in _MORE_REMAINS else "FINAL")
        )
        resultado["transport_lost"] = pendiente.transport_lost

        if recibida:
            # Solo aqui. Un ACK no confirma nada.
            log.info(
                "[ON_DEMAND_TEST] respuesta latency=%ss result=%s mensajes=%d",
                resultado["latency_seconds"],
                resultado["result"],
                resultado["messages"],
            )
            self._confirm_capability()
        else:
            log.warning(
                "[ON_DEMAND_TEST] timeout tras %.0fs (enc=%s). La capacidad se "
                "queda como estaba; no se borra nada.",
                self._settings.history_request_timeout,
                resultado.get("enc_type"),
            )
        resultado["capability_after"] = self.capability_state()
        return resultado

    def _lock_ondemand(self) -> asyncio.Lock:
        """El candado que serializa TODAS las peticiones ON_DEMAND."""
        if self._ondemand_lock is None:
            self._ondemand_lock = asyncio.Lock()
        return self._ondemand_lock

    async def _request_once(self, chat_id: int, chat_jid: str, cursor: Any) -> bool:
        """Emite una peticion y espera su respuesta. ``True`` si llego.

        Una cada vez, en todo el proceso. No basta con ``_busy``: la cola de
        despertados entra por ``_process_chat`` sin pasar por ``run()``, y se
        midieron dos peticiones en vuelo con 2 s de diferencia. El telefono
        atiende de una en una y dos respuestas cruzadas no se pueden atribuir.
        """
        async with self._lock_ondemand():
            return await self._request_once_locked(chat_id, chat_jid, cursor)

    async def _request_once_locked(
        self, chat_id: int, chat_jid: str, cursor: Any
    ) -> bool:
        """El cuerpo de :meth:`_request_once`, ya con el turno reservado.

        La FORMA de la peticion no se toca: destino el propio telefono
        (dispositivo 0), ``category=peer``, mensaje desnudo, ``count`` de la
        configuracion y la marca en segundos. Eso es lo unico que funciona.
        """
        # ``from_me`` viaja YA con el cursor. Antes se volvia a consultar el
        # mensaje justo aqui, y si ese mensaje no estaba —un ancla que viene
        # del catalogo de semillas, por ejemplo— salia un False por defecto
        # sin que nada lo dijera.
        from_me = bool(getattr(cursor, "from_me", False))

        message = build_on_demand_message(
            chat_jid=chat_jid,
            oldest_message_id=cursor.message_id,
            oldest_from_me=from_me,
            oldest_timestamp=cursor.timestamp,
            count=self._settings.history_on_demand_count,
            account_lid=self._account_lid(),
        )

        # EL WAITER, ANTES DE ENVIAR. Nunca al reves: las respuestas que
        # funcionaron llegaron en ~1 s, y una registrada despues del envio se
        # perderia la suya y agotaria los 45 s por nada.
        pending = _Pending(chat_jid=chat_jid)
        self._pending[chat_jid] = pending
        request_row = self._record_request(chat_id, chat_jid, cursor)
        if self._settings.protocol_debug:
            log.debug("[ON_DEMAND] waiter_registered chat=%s", _short(chat_jid))

        # El estado de la sesion Signal se mira AHORA, antes de enviar. Mirarlo
        # despues contaba la sesion que acababa de crear el propio envio, de
        # modo que el diagnostico decia "sesion_por_pn=True" justo cuando la
        # peticion salia como pkmsg por no haberla.
        sesiones = self.peer_session_state()

        try:
            from app.compat.peer_message import peer_mode, ultimo_enc_type

            sender = self._client._sender
            # category="peer": marca la stanza como operacion entre
            # dispositivos de la misma cuenta. Sin esto el servidor la
            # confirma con un ACK y la descarta sin responder.
            target = self._target_jid()
            # Todo esto es detalle de protocolo: a DEBUG. Eran nueve lineas
            # por peticion. PROTOCOL_DEBUG=true las devuelve enteras.
            log.debug(
                "REQUEST chat=%s destino=%s***@%s tipo=%s device=%d "
                "category=peer shape=bare",
                _short(chat_jid),
                target.user[:6],
                target.server,
                "LID" if target.server == "lid" else "PN",
                target.device,
            )
            enviado_en = time.monotonic()
            with peer_mode(sender):
                sent = await sender.send_message(target, message)
            ack_ms = (time.monotonic() - enviado_en) * 1000.0
            self.stats.requests_sent += 1
            protocol_id = getattr(sent, "id", None)
            pending.request_id = protocol_id
            pending.started_at = time.monotonic()
            self._mark_request(request_row, status="sent", protocol_id=protocol_id)

            # `pkmsg` significa que NO habia sesion Signal con el telefono y
            # esta peticion abre una nueva. Se anota porque es exactamente la
            # linea que separa las peticiones que respondieron de las que no.
            self.ultimo_enc_type = ultimo_enc_type(sender)
            if self.ultimo_enc_type == "pkmsg":
                log.info(
                    "ON_DEMAND sobre sesion NUEVA con el telefono (enc=pkmsg). "
                    "La stanza lleva <device-identity> para que pueda validarla."
                )

            # El ACK del transporte solo dice que la stanza llego; no dice
            # nada del contenido de la operacion. Por eso no es una respuesta
            # y por eso no se anuncia como si lo fuera.
            if self._settings.protocol_debug:
                log.debug(
                    "[ON_DEMAND] request_sent chat=%s enc=%s",
                    _short(chat_jid),
                    self.ultimo_enc_type,
                )
                log.debug("[ON_DEMAND] ack_received ack_ms=%.0f", ack_ms)
            log.debug(
                "ACK id=%s ack_ms=%.0f enc=%s sesion_por_pn=%s sesion_por_lid=%s; "
                "esperando HISTORY_SYNC_NOTIFICATION hasta %.0fs",
                protocol_id or "?",
                ack_ms,
                self.ultimo_enc_type,
                sesiones.get("pn"),
                sesiones.get("lid"),
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
                "TIMEOUT: no llego HISTORY_SYNC_NOTIFICATION en %.0fs (enc=%s). "
                "El ACK solo confirmaba la entrega de la stanza.",
                self._settings.history_request_timeout,
                self.ultimo_enc_type or "?",
            )
        finally:
            self._pending.pop(chat_jid, None)

        self._last_transport_lost = pending.transport_lost

        self._last_end_type = pending.end_of_history_type
        if received:
            # Estado 3 de 3: esto SI es la respuesta.
            log.info(
                "ON_DEMAND_RECEIVED chat=%s mensajes_en_el_blob=%d latencia=%.2fs "
                "correlacion=%s",
                _short(chat_jid),
                pending.messages,
                pending.latency if pending.latency is not None else -1.0,
                pending.correlacion or "?",
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
        """Cambia el estado del chat Y lo cuenta.

        Antes solo escribia en la base. La pantalla se enteraba al recargar, y
        mientras tanto ensenaba un estado congelado: un chat podia decir
        "Recuperando historial" durante horas cuando ya estaba terminado, o
        seguir en "Esperando referencia" con tres mil mensajes dentro.
        """
        with self._database.transaction() as session:
            session.execute(
                update(ChatHistoryState)
                .where(ChatHistoryState.chat_jid == chat_jid)
                .values(history_status=status, last_error=error)
            )
        self._avisar_estado(chat_jid, status)

    def _id_de_chat(self, chat_jid: str) -> int | None:
        """El identificador numerico, para que la pantalla no tenga que buscarlo.

        El JID basta para encontrar la fila, pero el frontend indexa por
        ``id``: sin esto tiene que recorrer la lista comparando cadenas, y en
        un chat que llego por LID el JID de la lista puede ser el del
        telefono. Se manda ``None`` si no se puede resolver -- el JID sigue
        yendo, asi que el aviso no se pierde.
        """
        try:
            from app.models import Chat

            with self._database.transaction() as sesion:
                return sesion.execute(
                    select(Chat.id).where(Chat.jid == chat_jid)
                ).scalar_one_or_none()
        except Exception:  # noqa: BLE001 - no saberlo no invalida el aviso
            return None

    def _avisar_estado(self, chat_jid: str, status: str) -> None:
        """Publica el cambio. Avisar nunca puede cortar la excavacion."""
        publicar = getattr(self, "publish", None)
        if publicar is None:
            return
        try:
            publicar(
                "chat_history_status",
                {
                    "chat_jid": chat_jid,
                    "chat_id": self._id_de_chat(chat_jid),
                    "history_status": status,
                },
            )
        except Exception:  # noqa: BLE001
            log.debug("No se pudo publicar el cambio de estado del chat")

    def _anotar_reintento(self, chat_jid: str) -> tuple[int, Any]:
        """Suma un intento fallido y fija cuando se puede volver a probar."""
        from app.history.cursor import anotar_intento_fallido

        with self._database.transaction() as session:
            return anotar_intento_fallido(session, chat_jid)

    def _limpiar_reintentos(self, chat_jid: str) -> None:
        """La peticion respondio: la espera de reintento deja de aplicar."""
        from app.history.cursor import limpiar_reintentos

        with self._database.transaction() as session:
            limpiar_reintentos(session, chat_jid)

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
        self._avisar_estado(chat_jid, "pending")

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

    #: Cuanto se espera antes de volver a probar, tras un intento sin
    #: respuesta. Creciente: si el telefono esta apagado, insistir cada treinta
    #: segundos no lo enciende. El ultimo valor se repite indefinidamente.
    ESPERAS_DE_CANARY = (30.0, 60.0, 300.0)

    def _confirmar_por_respuesta_real(self) -> None:
        """Confirma por una peticion normal, no por la prueba.

        Se lleva la cuenta aparte para poder responder a la pregunta de si el
        diagnostico hace falta siquiera: si casi todo se confirma por trabajo
        normal, la prueba solo esta gastando una peticion.
        """
        if self.capability_state() == "CONFIRMED":
            return
        self.metricas_de_capacidad["confirmed_by_real_backfill"] += 1
        log.info(
            "[CAPABILITY] respuesta ON_DEMAND valida y correlacionada; CONFIRMED"
        )
        self._confirm_capability()

    def programar_reintento_de_canary(self) -> float:
        """Deja apuntado cuando se vuelve a probar. Devuelve los segundos."""
        indice = min(
            self._canary_intentos, len(self.ESPERAS_DE_CANARY) - 1
        )
        espera = self.ESPERAS_DE_CANARY[indice]
        self._canary_intentos += 1
        self._proximo_canary = time.monotonic() + espera
        log.info("[CAPABILITY] timeout; queda SUSPECT. Reintento en %.0fs", espera)
        return espera

    def toca_reintentar_canary(self) -> bool:
        """Si ya se puede volver a probar.

        No se prueba si la capacidad ya esta confirmada, si hay una excavacion
        en marcha —la prueba y el trabajo normal piden al MISMO telefono— o si
        todavia no ha pasado la espera.
        """
        if self.capability_state() == "CONFIRMED":
            return False
        if self._busy:
            return False
        if self._proximo_canary is None:
            return False
        return time.monotonic() >= self._proximo_canary

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
        """ON_DEMAND respondio: se deja constancia y se levanta la sospecha.

        A partir de ahi un timeout es un problema DE ESE CHAT, y no vuelve a
        declararse que "WhatsApp no soporta historial".

        SUSPECT tiene que ser reversible, y no lo era. Se guardaba
        ``{"confirmed": True, "session": ..., "state": "SUSPECT"}`` y esta
        funcion solo escribia cuando NO habia registro para la sesion, asi que
        un canary que funcionara despues no borraba el ``state`` y
        ``capability_state()`` seguia devolviendo SUSPECT para siempre. La
        salida solo llegaba desvinculando.
        """
        fingerprint = self.session_fingerprint()
        # Ha respondido: la racha de timeouts deja de contar.
        self._timeouts_seguidos = 0
        with self._database.transaction() as session:
            stored = repo.get_app_state(session, CAPABILITY_KEY)
            previo = dict(stored) if isinstance(stored, dict) else {}
            ya_confirmada = (
                previo.get("session") == fingerprint
                and previo.get("confirmed")
                and previo.get("state") != "SUSPECT"
            )
            if ya_confirmada:
                return

            datos = {
                "confirmed": True,
                "at": int(time.time()),
                # Huella de la vinculacion, no material sensible.
                "session": fingerprint,
            }
            repo.set_app_state(session, CAPABILITY_KEY, datos)

        if previo.get("state") == "SUSPECT":
            log.info(
                "[BACKFILL] capability=CONFIRMED: ON_DEMAND respondio y la "
                "sospecha queda levantada"
            )
        else:
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
