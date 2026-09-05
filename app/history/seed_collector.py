"""Recoger anclas de historial de todas las fuentes que WhatsApp ya entrega.

EL PROBLEMA
-----------
``HISTORY_SYNC_ON_DEMAND`` va anclado: hay que decirle desde que mensaje
seguir hacia atras. Una conversacion sin ninguna referencia se queda en
``waiting_seed`` — existe, se ve, y no se le puede pedir nada.

LA IDEA
-------
Las anclas llegan por muchos caminos y en momentos distintos: el bootstrap
inicial, un mensaje en vivo, la descarga de pendientes al reconectar, un
reenvio tras un fallo de descifrado. Antes se aprovechaban unas pocas y por
casualidad. Aqui pasan TODAS por el mismo sitio, se validan igual y se anotan.

LO QUE NO HACE
--------------
No envia peticiones. Observa, valida, guarda y —si la conversacion estaba
esperando— la pone en la cola del motor de siempre. Ese motor no se toca:
funciona.

LA REGLA QUE NO SE ROMPE
------------------------
Nunca se fabrica un identificador, ni una marca de tiempo, ni un cursor. Un
ancla inventada recibe confirmacion del servidor y despues silencio, y eso es
lo mas caro de diagnosticar que tiene este proyecto. Si no hay ancla real, la
conversacion se queda esperando y se dice tal cual.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from app.core.logging_setup import get_logger
from app.models import Chat, HistorySeed

log = get_logger("PLAN_E")

#: Forma de un identificador de mensaje de WhatsApp: hexadecimal, 16-32.
#: Es un filtro grueso; el fino lo aplica ``is_valid_history_cursor_id``, que
#: es el mismo que usa el motor de excavacion.
FORMA_DE_ID = re.compile(r"^[0-9A-Fa-f]{16,32}$")

#: Servidores que nunca son una conversacion. Anclar en uno de estos pediria
#: el historial de algo que no lo tiene.
SERVIDORES_EXCLUIDOS = frozenset({"broadcast", "newsletter"})

#: Tipos que no sirven de ancla aunque lleven identificador: no son mensajes
#: de la conversacion, son senalizacion del protocolo.
TIPOS_INUTILES = frozenset(
    {
        "protocol",
        "senderKeyDistributionMessage",
        "sender_key_distribution",
        "ephemeral_setting",
        "history_sync_notification",
        "peer_data_operation",
        "unknown",
    }
)


@dataclass
class SeedCandidate:
    """Un ancla propuesta. Todavia sin validar."""

    chat_jid: str
    wa_msg_id: str | None
    timestamp: int | None
    from_me: bool = False
    source: str = "live"
    message_type: str | None = None
    participant: str | None = None


@dataclass
class ResultadoDeSemilla:
    """Que paso con un candidato."""

    aceptada: bool
    motivo: str = ""
    chat_id: int | None = None
    desperto: bool = False


@dataclass
class MetricasPlanE:
    """Lo que hay que poder mirar para saber si esto sirve de algo."""

    observadas: int = 0
    validas: int = 0
    rechazadas: int = 0
    duplicadas: int = 0
    despertados: int = 0
    por_fuente: dict[str, int] = field(default_factory=dict)
    motivos_rechazo: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "seeds_observed": self.observadas,
            "seeds_valid": self.validas,
            "seeds_rejected": self.rechazadas,
            "seeds_duplicate": self.duplicadas,
            "chats_awakened": self.despertados,
            "by_source": dict(self.por_fuente),
            "rejection_reasons": dict(self.motivos_rechazo),
        }


def validar(candidato: SeedCandidate) -> str | None:
    """El motivo por el que NO sirve, o ``None`` si sirve.

    Todo lo que pase de aqui se va a usar como ancla de una peticion real al
    telefono del usuario. Es preferible rechazar de mas.
    """
    if not candidato.chat_jid:
        return "sin chat"

    servidor = candidato.chat_jid.split("@")[-1] if "@" in candidato.chat_jid else ""
    if servidor in SERVIDORES_EXCLUIDOS:
        return "no es una conversacion"

    if not candidato.wa_msg_id:
        return "sin identificador"
    if not FORMA_DE_ID.match(candidato.wa_msg_id):
        return "identificador con forma inesperada"

    # El mismo filtro que usa el motor de excavacion. Si el aceptara algo que
    # aqui se rechaza —o al reves— las anclas guardadas no servirian.
    try:
        from app.services.backfill_service import is_valid_history_cursor_id

        if not is_valid_history_cursor_id(candidato.wa_msg_id):
            return "identificador no valido como ancla"
    except ImportError:  # pragma: no cover
        pass

    if not candidato.timestamp or candidato.timestamp <= 0:
        return "sin marca de tiempo"
    # Una marca en milisegundos daria un cursor absurdo. ON_DEMAND los quiere
    # en segundos pese al nombre del campo.
    if candidato.timestamp > 4_000_000_000:
        return "marca de tiempo en milisegundos"

    if candidato.message_type and candidato.message_type in TIPOS_INUTILES:
        return f"tipo sin valor de ancla ({candidato.message_type})"

    return None


class RecentSeedCollector:
    """Observa, valida y anota anclas. No pide nada al servidor."""

    def __init__(
        self,
        database: Any,
        *,
        user_id: Any = None,
        account_id: Any = None,
        seed_queue: Any = None,
    ) -> None:
        self._database = database
        self.user_id = user_id
        self.account_id = account_id
        #: La cola del motor de siempre. Aqui solo se le entrega trabajo.
        self._seed_queue = seed_queue
        #: A quien avisar de que un chat desperto. Opcional.
        self.publish: Any = None
        self.metricas = MetricasPlanE()

    @property
    def listo(self) -> bool:
        """Sin dueno no se puede anotar nada: toda ancla pertenece a alguien."""
        return self.user_id is not None and self.account_id is not None

    # -- Entrada -------------------------------------------------------------

    def observe(self, candidato: SeedCandidate) -> ResultadoDeSemilla:
        """Un candidato. Devuelve que se hizo con el."""
        self.metricas.observadas += 1

        if not self.listo:
            return self._rechazar("sin dueno todavia")

        motivo = validar(candidato)
        if motivo is not None:
            return self._rechazar(motivo)

        with self._database.transaction() as sesion:
            chat_id = self._resolver_chat(sesion, candidato.chat_jid)
            if chat_id is None:
                return self._rechazar("no se pudo resolver el chat")

            ya = sesion.execute(
                select(HistorySeed).where(
                    HistorySeed.whatsapp_account_id == self.account_id,
                    HistorySeed.chat_id == chat_id,
                    HistorySeed.wa_msg_id == candidato.wa_msg_id,
                )
            ).scalar_one_or_none()

            if ya is not None:
                # La misma ancla llega por dos caminos —en vivo y en un blob—
                # a menudo. Es una sola.
                ya.last_seen_at = _ahora()
                sesion.flush()
                self.metricas.duplicadas += 1
                return ResultadoDeSemilla(True, "ya conocida", chat_id=chat_id)

            sesion.add(
                HistorySeed(
                    user_id=self.user_id,
                    whatsapp_account_id=self.account_id,
                    chat_id=chat_id,
                    chat_jid=candidato.chat_jid,
                    wa_msg_id=candidato.wa_msg_id,
                    timestamp=candidato.timestamp,
                    from_me=bool(candidato.from_me),
                    source=candidato.source,
                )
            )
            sesion.flush()

        self.metricas.validas += 1
        self.metricas.por_fuente[candidato.source] = (
            self.metricas.por_fuente.get(candidato.source, 0) + 1
        )

        desperto = self.promote_waiting_chat(chat_id)
        return ResultadoDeSemilla(True, "nueva", chat_id=chat_id, desperto=desperto)

    def observe_many(self, candidatos: list[SeedCandidate]) -> MetricasPlanE:
        for candidato in candidatos:
            self.observe(candidato)
        return self.metricas

    def _rechazar(self, motivo: str) -> ResultadoDeSemilla:
        self.metricas.rechazadas += 1
        self.metricas.motivos_rechazo[motivo] = (
            self.metricas.motivos_rechazo.get(motivo, 0) + 1
        )
        return ResultadoDeSemilla(False, motivo)

    # -- Resolucion de la conversacion ---------------------------------------

    def _resolver_chat(self, sesion: Any, jid: str) -> int | None:
        """El chat al que pertenece ese identificador.

        Un contacto aparece por telefono y por LID, y son la MISMA
        conversacion. Se usa el resolutor de siempre para no crear un chat
        duplicado con la mitad del historial en cada uno.

        En grupos, el ancla pertenece al grupo, no a quien escribio: el
        participante no es una conversacion.
        """
        from app.services.chat_alias import canonical_chat_jid

        canonico = canonical_chat_jid(sesion, jid) or jid
        return sesion.execute(
            select(Chat.id).where(Chat.jid == canonico)
        ).scalar_one_or_none()

    # -- Promocion -----------------------------------------------------------

    def promote_waiting_chat(self, chat_id: int) -> bool:
        """Si ese chat estaba esperando, se le da el ancla y se encola.

        Automatico: no hace falta pulsar nada ni recargar. Que una
        conversacion despierte no puede depender de que alguien este mirando.
        """
        from app.history.cursor import get_valid_history_cursor, persist_cursor
        from app.models import ChatHistoryState

        with self._database.transaction() as sesion:
            estado = sesion.execute(
                select(ChatHistoryState).where(ChatHistoryState.chat_id == chat_id)
            ).scalar_one_or_none()
            if estado is None or estado.history_status != "waiting_seed":
                return False

            # La MISMA funcion que usa el motor para pedir. Si aqui se
            # eligiera un ancla y alli otra, el chat pasaria a 'pending' con
            # un cursor y se pediria con otro.
            ancla = get_valid_history_cursor(
                sesion, chat_id=chat_id, chat_jid=estado.chat_jid
            )
            if ancla is None:
                return False

            # El ORDEN importa: primero el cursor, despues el estado. Si el
            # proceso muere entre las dos cosas, queda un chat que sigue
            # esperando con su ancla ya guardada, que es recuperable. Al reves
            # quedaria uno marcado como listo para excavar sin nada con que
            # hacerlo.
            persist_cursor(sesion, estado.chat_jid, ancla)
            sesion.flush()

            estado.history_status = "pending"
            estado.attempt_count = 0
            estado.next_retry_at = None
            estado.last_seed_attempt_at = _ahora()
            estado.last_seed_attempt_result = f"seed:{ancla.source}"
            jid = estado.chat_jid
            fuente = ancla.source
            sesion.flush()

        self.metricas.despertados += 1
        self._avisar_estado(jid, "pending")
        # Esto SI se ve: es el resultado que interesa del Plan E. La etiqueta
        # la pone el formateador, asi que aqui no se repite.
        log.info(
            "chat=%s ancla real detectada (%s): se pide su historial",
            chat_id,
            fuente,
        )

        if self._seed_queue is not None:
            try:
                self._seed_queue.enqueue([jid])
            except Exception:  # noqa: BLE001 - encolar no puede tumbar la ingesta
                log.debug("No se pudo encolar el chat despertado")
        return True

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
        """Que la pantalla se entere de que este chat ya puede recuperarse."""
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
        except Exception:  # noqa: BLE001 - avisar no puede cortar la ingesta
            log.debug("No se pudo publicar el despertar del chat")

    @staticmethod
    def oldest_valid_cursor(sesion: Any, chat_id: int) -> HistorySeed | None:
        """El ancla mas ANTIGUA que se conoce de ese chat.

        Se excava hacia atras, asi que empezar por la mas antigua alcanza lo
        que queda antes de ella. Partir de la mas reciente obligaria a
        recorrer otra vez lo que ya se tiene.
        """
        return (
            sesion.execute(
                select(HistorySeed)
                .where(HistorySeed.chat_id == chat_id, HistorySeed.valid.is_(True))
                .order_by(HistorySeed.timestamp.asc())
                .limit(1)
            )
            .scalars()
            .first()
        )

    # -- Consulta ------------------------------------------------------------

    def resumen(self) -> dict[str, Any]:
        from app.models import ChatHistoryState

        with self._database.transaction() as sesion:
            por_estado = dict(
                sesion.execute(
                    select(ChatHistoryState.history_status, func.count()).group_by(
                        ChatHistoryState.history_status
                    )
                ).all()
            )
            semillas = int(
                sesion.execute(
                    select(func.count()).select_from(HistorySeed)
                ).scalar()
                or 0
            )
            con_semilla = int(
                sesion.execute(
                    select(func.count(func.distinct(HistorySeed.chat_id)))
                ).scalar()
                or 0
            )
        return {
            "waiting_seed": por_estado.get("waiting_seed", 0),
            "exhausted": por_estado.get("exhausted", 0),
            "pending": por_estado.get("pending", 0),
            "timeout": por_estado.get("timeout", 0),
            "seeds_total": semillas,
            "chats_with_seed": con_semilla,
            **self.metricas.to_json(),
        }


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Traduccion desde lo que ya circula por el sistema
# ---------------------------------------------------------------------------


def desde_mensaje_vivo(mensaje: Any, *, source: str = "live") -> SeedCandidate | None:
    """Un mensaje en vivo, ya descifrado y autenticado.

    Solo llegan aqui los que pasaron Signal: un mensaje que no se pudo
    descifrar no es una fuente de nada.
    """
    identificador = getattr(mensaje, "id", None) or getattr(
        mensaje, "whatsapp_message_id", None
    )
    chat = getattr(mensaje, "chat_jid", None)
    if chat is None:
        crudo = getattr(mensaje, "chat", None)
        chat = str(crudo) if crudo is not None else None
    if not chat:
        return None

    return SeedCandidate(
        chat_jid=chat,
        wa_msg_id=identificador,
        timestamp=_segundos(getattr(mensaje, "timestamp", None)),
        from_me=bool(getattr(mensaje, "from_me", False)),
        source=source,
        message_type=getattr(mensaje, "message_type", None),
    )


def desde_web_message_info(crudo: Any, *, source: str) -> SeedCandidate | None:
    """Un ``WebMessageInfo`` de un blob de History Sync."""
    clave = getattr(crudo, "key", None)
    if clave is None:
        return None
    remoto = getattr(clave, "remoteJid", None) or getattr(clave, "remote_jid", None)
    if not remoto:
        return None

    return SeedCandidate(
        chat_jid=str(remoto),
        wa_msg_id=getattr(clave, "id", None) or None,
        timestamp=_segundos(getattr(crudo, "messageTimestamp", None)),
        from_me=bool(getattr(clave, "fromMe", False)),
        source=source,
        participant=getattr(clave, "participant", None) or None,
    )


def _segundos(valor: Any) -> int | None:
    """Marca de tiempo en segundos.

    Las de milisegundos NO se convierten: se rechazan. Dividir por mil es
    adivinar la unidad, y equivocarse produce un cursor que el servidor
    confirma y nunca responde.
    """
    if valor is None:
        return None
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        return None
    return numero if numero > 0 else None


def fuente_de_sync(sync_type: Any) -> str:
    """El nombre de fuente que corresponde a un tipo de History Sync."""
    nombre = str(sync_type or "").upper()
    return {
        "INITIAL_BOOTSTRAP": "initial_bootstrap",
        "RECENT": "recent_history",
        "FULL": "full_history",
        "ON_DEMAND": "on_demand",
    }.get(nombre, "initial_bootstrap")
