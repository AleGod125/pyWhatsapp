"""El cursor de historial de un chat. UNA sola definicion, para todos.

EL BUG QUE ORIGINA ESTE MODULO
------------------------------
En la misma ejecucion aparecian las dos cosas::

    CANARY: no hay ningun chat con cursor valido
    ... y a continuacion el backfill encontraba uno, lo mandaba y hubo ACK.

No era un fallo de datos: eran DOS definiciones distintas de "tiene cursor".
``pick_canary`` exigia ademas chat individual y >=2 mensajes, y cuando no
encontraba ninguno lo contaba como "no hay cursor valido", que es otra cosa.
Se midio sobre la base real: tres chats con cursor de verdad, uno solo
elegible para el canary.

Y habia una tercera definicion. El motor de excavacion lee el cursor de
``messages``; el Plan E anota anclas en ``history_seeds``. Un ancla que
existiera solo en ``history_seeds`` era invisible para quien pide.

QUIEN MANDA
-----------
``history_seeds``       catalogo de todas las anclas conocidas
``messages``            los mensajes reales guardados
``chat_history_state``  el CURSOR ACTIVO, que es lo que se persiste y lo que
                        sobrevive a un reinicio

Esta funcion mira las tres y devuelve la mas ANTIGUA que sea real. Se excava
hacia atras: partir de la mas reciente obligaria a recorrer otra vez lo que ya
se tiene.

LA REGLA QUE NO SE ROMPE
------------------------
No se fabrica nada. Si no hay ancla real, se devuelve ``None`` y el chat se
queda esperando. Un cursor inventado recibe ACK del servidor y despues
silencio, que es el fallo mas caro de diagnosticar de este proyecto.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update

from app.core.logging_setup import get_logger
from app.models import Chat, ChatHistoryState, Contact, HistorySeed, Message
from app.services.repository import is_valid_history_cursor_id

log = get_logger("BACKFILL")

#: Espera entre reintentos de un chat cuya peticion vencio. Creciente: si el
#: telefono no contesta, insistir cada minuto no lo hace contestar y si
#: consume la unica ranura de peticiones. El ultimo valor se repite.
RETRY_BACKOFF_SECONDS = (60, 300, 900, 3600)


@dataclass(frozen=True)
class CursorInfo:
    """Un ancla utilizable para ``HISTORY_SYNC_ON_DEMAND``."""

    wa_msg_id: str
    timestamp: int
    from_me: bool = False
    #: De donde salio: ``message``, ``seed`` o ``state``. Solo diagnostico.
    source: str = "message"
    valid: bool = True

    @property
    def message_id(self) -> str:
        """Nombre que usaba el motor antes de existir este modulo.

        Se conserva para no tener que reescribir cada llamada del camino que
        construye la peticion, que es justo lo que no se puede tocar.
        """
        return self.wa_msg_id


def aliases_de(session: Any, chat_jid: str) -> list[str]:
    """Todos los identificadores conocidos del MISMO contacto.

    Un contacto aparece por telefono y por LID: son la misma conversacion, y
    el ancla puede estar guardada bajo el otro identificador. No se deduce
    ninguno —un telefono y un LID no son convertibles el uno en el otro—: se
    leen de ``contacts``, que ``lid_bridge`` rellena desde el ``lid_map``.
    """
    encontrados = {chat_jid}
    usuario = chat_jid.split("@")[0].split(":")[0].split(".")[0]
    servidor = chat_jid.partition("@")[2]

    if servidor == "lid":
        for jid in session.execute(
            select(Contact.jid).where(Contact.lid.in_([chat_jid, usuario]))
        ).scalars():
            if jid:
                encontrados.add(jid)
    elif servidor == "s.whatsapp.net":
        for lid in session.execute(
            select(Contact.lid).where(Contact.jid == chat_jid)
        ).scalars():
            if lid:
                encontrados.add(lid if "@" in lid else f"{lid}@lid")

    return sorted(encontrados)


def _de_mensajes(session: Any, jids: list[str]) -> CursorInfo | None:
    """El mensaje mas antiguo con ID REAL de WhatsApp, entre todos los alias.

    Ojo con la distincion, que causo un bug historico: "mensaje mas antiguo
    almacenado" y "ancla mas antigua utilizable" no son lo mismo. Si el del 13
    de agosto no trae ID real y el del 14 si, el ancla es la del 14.
    """
    filas = session.execute(
        select(Message.whatsapp_message_id, Message.timestamp, Message.from_me)
        .where(
            Message.chat_jid.in_(jids),
            Message.whatsapp_message_id.is_not(None),
            Message.whatsapp_message_id != "",
        )
        .order_by(Message.timestamp.asc(), Message.id.asc())
    )
    for wamid, ts, from_me in filas:
        # El segundo filtro va en Python: la lista de prefijos fabricados es
        # politica de la aplicacion y puede crecer sin tocar el esquema.
        if is_valid_history_cursor_id(wamid) and ts:
            return CursorInfo(wamid, int(ts), bool(from_me), source="message")
    return None


def _de_semillas(session: Any, chat_id: int | None) -> CursorInfo | None:
    """El ancla mas antigua del catalogo del Plan E."""
    if chat_id is None:
        return None
    fila = (
        session.execute(
            select(HistorySeed)
            .where(HistorySeed.chat_id == chat_id, HistorySeed.valid.is_(True))
            .order_by(HistorySeed.timestamp.asc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if fila is None or not is_valid_history_cursor_id(fila.wa_msg_id):
        return None
    return CursorInfo(
        fila.wa_msg_id, int(fila.timestamp), bool(fila.from_me), source="seed"
    )


def _del_estado(estado: Any) -> CursorInfo | None:
    """El cursor ACTIVO ya persistido.

    Es el que sobrevive a un reinicio, y por eso entra en la comparacion
    aunque casi siempre coincida con uno de los otros dos: si un dia el
    mensaje que lo origino desapareciera, el cursor seguiria siendo real.
    """
    if estado is None:
        return None
    wamid = getattr(estado, "oldest_message_id", None)
    ts = getattr(estado, "oldest_message_timestamp", None)
    if not wamid or not ts or not is_valid_history_cursor_id(wamid):
        return None
    return CursorInfo(
        wamid, int(ts), bool(getattr(estado, "oldest_from_me", False)), source="state"
    )


def get_valid_history_cursor(
    session: Any, *, chat_id: int | None = None, chat_jid: str | None = None
) -> CursorInfo | None:
    """El ancla utilizable de un chat, o ``None`` si no la hay.

    LA definicion. Canary, backfill, cola de semillas, revision y
    reconciliacion usan esta y solo esta: dos definiciones de "tiene cursor"
    es exactamente el bug que se estaba arreglando.
    """
    if chat_jid is None and chat_id is None:
        return None

    if chat_jid is None:
        chat_jid = session.execute(
            select(Chat.jid).where(Chat.id == chat_id)
        ).scalar_one_or_none()
        if chat_jid is None:
            return None
    if chat_id is None:
        chat_id = session.execute(
            select(Chat.id).where(Chat.jid == chat_jid)
        ).scalar_one_or_none()

    estado = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == chat_jid)
    ).scalar_one_or_none()

    candidatos = [
        c
        for c in (
            _de_mensajes(session, aliases_de(session, chat_jid)),
            _de_semillas(session, chat_id),
            _del_estado(estado),
        )
        if c is not None
    ]
    if not candidatos:
        return None
    # La MAS ANTIGUA. Si el chat tiene anclas de las 17:00, 18:20 y 18:28, la
    # que sirve es la de las 17:00: lo que queda por recuperar esta antes.
    return min(candidatos, key=lambda c: c.timestamp)


def persist_cursor(session: Any, chat_jid: str, cursor: CursorInfo) -> bool:
    """Escribe el cursor activo en ``chat_history_state``.

    Se persiste ANTES de cambiar de estado. Si el proceso muere entre las dos
    cosas es preferible un chat que sigue esperando con su ancla guardada, a
    uno marcado como listo para excavar sin nada con que hacerlo.
    """
    return bool(
        session.execute(
            update(ChatHistoryState)
            .where(ChatHistoryState.chat_jid == chat_jid)
            .values(
                oldest_message_id=cursor.wa_msg_id,
                oldest_message_timestamp=cursor.timestamp,
                oldest_from_me=cursor.from_me,
                cursor_source=cursor.source,
            )
        ).rowcount
    )


# ---------------------------------------------------------------------------
# Reintentos
# ---------------------------------------------------------------------------


def proxima_espera(intento: int) -> int:
    """Segundos hasta el siguiente intento, para el intento numero ``intento``."""
    if intento <= 0:
        return RETRY_BACKOFF_SECONDS[0]
    indice = min(intento - 1, len(RETRY_BACKOFF_SECONDS) - 1)
    return RETRY_BACKOFF_SECONDS[indice]


def anotar_intento_fallido(session: Any, chat_jid: str) -> tuple[int, Any]:
    """Suma un intento y calcula cuando se puede volver a probar.

    NO toca el cursor. Un timeout no dice nada malo del ancla: dice que el
    telefono no contesto. Borrarla convertiria un chat recuperable en uno que
    vuelve a esperar una semilla que ya tiene.
    """
    estado = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == chat_jid)
    ).scalar_one_or_none()
    ahora = datetime.now(timezone.utc)
    if estado is None:
        return 0, ahora

    intento = int(getattr(estado, "attempt_count", 0) or 0) + 1
    proximo = ahora + timedelta(seconds=proxima_espera(intento))
    estado.attempt_count = intento
    estado.last_attempt_at = ahora
    estado.next_retry_at = proximo
    return intento, proximo


def limpiar_reintentos(session: Any, chat_jid: str) -> None:
    """La peticion funciono: el contador de intentos vuelve a cero."""
    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.chat_jid == chat_jid)
        .values(attempt_count=0, next_retry_at=None)
    )


def espera_cumplida(proximo: Any, ahora: Any = None) -> bool:
    """``True`` si ya paso el instante ``proximo`` (o si no habia ninguno)."""
    if proximo is None:
        return True
    if getattr(proximo, "tzinfo", None) is None:
        proximo = proximo.replace(tzinfo=timezone.utc)
    return (ahora or datetime.now(timezone.utc)) >= proximo


def toca_reintentar(estado: Any, ahora: Any = None) -> bool:
    """``True`` si ya paso la espera de reintento de este chat."""
    return espera_cumplida(getattr(estado, "next_retry_at", None), ahora)
