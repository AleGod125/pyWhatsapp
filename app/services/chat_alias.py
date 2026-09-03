"""Un contacto, un chat: resolver PN y LID a la conversacion que ya existe.

POR QUE HACE FALTA
------------------
El ``destination_jid`` de un ``DeviceSentMessage`` puede venir por telefono
(``@s.whatsapp.net``) o por LID (``@lid``), y no siempre en la misma forma en
que se creo el chat. Medido en esta base: 33 chats por LID, 1 por telefono, 6
grupos. Si un mensaje saliente llega con la forma que NO se uso al crear el
chat, se crearia una conversacion duplicada para la misma persona.

COMO SE RESUELVE
----------------
La correspondencia no se inventa ni se deduce del numero: PN y LID no son
convertibles el uno en el otro. Se lee de lo que ya sabemos:

  1. si ya existe un chat con ese identificador exacto, ese es;
  2. si no, se traduce con ``contacts.lid`` <-> ``contacts.jid``, que
     ``lid_bridge`` ya rellena desde el ``lid_map`` de pywhats, y se mira si
     el chat existe en la otra forma;
  3. si tampoco, se devuelve el identificador tal cual y se creara el chat.

El paso 3 es deliberado: sin traduccion conocida, inventarse a que chat
pertenece seria peor que abrir uno nuevo con el identificador que WhatsApp
declaro.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging_setup import get_logger
from app.models import Chat, Contact

log = get_logger("WA")


def _usuario(jid: str) -> str:
    """Parte de usuario, sin sufijo de dispositivo (``64940106866902.3@lid``)."""
    return jid.split("@")[0].split(":")[0].split(".")[0]


def canonical_chat_jid(session: Session, jid: str) -> str:
    """El JID del chat que YA representa a este contacto, o el mismo si no hay.

    Los grupos y las listas de difusion se devuelven intactos: su
    identificador es unico y no tiene forma alterna.
    """
    if not jid or "@" not in jid:
        return jid

    usuario, _, servidor = jid.partition("@")
    if servidor not in ("lid", "s.whatsapp.net"):
        return jid

    # El sufijo de dispositivo no forma parte de la conversacion.
    limpio = f"{_usuario(jid)}@{servidor}"

    existente = session.execute(
        select(Chat.jid).where(Chat.jid == limpio)
    ).scalar_one_or_none()
    if existente:
        return existente

    alterno = _alterno(session, limpio, servidor)
    if alterno is None:
        return limpio

    existente = session.execute(
        select(Chat.jid).where(Chat.jid == alterno)
    ).scalar_one_or_none()
    if existente:
        log.debug("Destino %s resuelto al chat existente %s", _corto(limpio), _corto(existente))
        return existente
    return limpio


def _alterno(session: Session, jid: str, servidor: str) -> str | None:
    """La otra forma del mismo contacto, segun ``contacts``. Nunca se deduce."""
    usuario = _usuario(jid)
    if servidor == "lid":
        telefono = session.execute(
            select(Contact.jid).where(Contact.lid == jid)
        ).scalar_one_or_none()
        if telefono is None:
            # ``contacts.lid`` puede estar guardado sin servidor.
            telefono = session.execute(
                select(Contact.jid).where(Contact.lid == usuario)
            ).scalar_one_or_none()
        return telefono
    lid = session.execute(
        select(Contact.lid).where(Contact.jid == jid)
    ).scalar_one_or_none()
    if not lid:
        return None
    return lid if "@" in lid else f"{lid}@lid"


def _corto(jid: str) -> str:
    user, _, server = jid.partition("@")
    return f"{user[:6]}***@{server}" if server else f"{user[:6]}***"
