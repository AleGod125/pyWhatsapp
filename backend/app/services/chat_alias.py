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

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging_setup import get_logger
from app.models import Chat, Contact

log = get_logger("WA")


def _usuario(jid: str) -> str:
    """Parte de usuario, sin sufijo de dispositivo (``64940106866902.3@lid``)."""
    return jid.split("@")[0].split(":")[0].split(".")[0]


def canonical_chat_jid(
    session: Session, jid: str, *, account_id: Any = None
) -> str:
    """El JID del chat que YA representa a este contacto, o el mismo si no hay.

    Los grupos y las listas de difusion se devuelven intactos: su
    identificador es unico y no tiene forma alterna.

    POR QUE NO SE EXIGE UNA SOLA FILA
    ---------------------------------
    Todas las busquedas de aqui usaban ``scalar_one_or_none()``, y eso
    reventaba con "Multiple rows were found" en cuanto dos cuentas hablaban
    con el mismo contacto: hay un chat por cuenta con el mismo jid.

    Y exigir una sola fila nunca fue lo correcto aqui: lo que se devuelve es
    una CADENA --el jid canonico--, que es identica mire quien la mire. Dos
    filas no son una ambiguedad, son la misma respuesta dos veces.

    ``account_id`` acota igualmente cuando se sabe, que es mas barato y deja
    claro de que conversacion se habla.
    """
    if not jid or "@" not in jid:
        return jid

    usuario, _, servidor = jid.partition("@")
    if servidor not in ("lid", "s.whatsapp.net"):
        return jid

    # El sufijo de dispositivo no forma parte de la conversacion.
    limpio = f"{_usuario(jid)}@{servidor}"

    existente = _un_chat(session, limpio, account_id)
    if existente:
        return existente

    alterno = _alterno(session, limpio, servidor, account_id)
    if alterno is None:
        return limpio

    existente = _un_chat(session, alterno, account_id)
    if existente:
        log.debug("Destino %s resuelto al chat existente %s", _corto(limpio), _corto(existente))
        return existente
    return limpio


def _un_chat(session: Session, jid: str, account_id: Any) -> str | None:
    """El jid de un chat existente con ese identificador, si lo hay.

    ``.first()`` y no ``scalar_one_or_none()``: con dos cuentas hay un chat
    por cuenta con el mismo jid, y lo que se devuelve --la cadena-- es la
    misma en los dos casos.
    """
    stmt = select(Chat.jid).where(Chat.jid == jid)
    if account_id is not None:
        stmt = stmt.where(Chat.whatsapp_account_id == account_id)
    return session.execute(stmt.limit(1)).scalars().first()


def _alterno(
    session: Session, jid: str, servidor: str, account_id: Any = None
) -> str | None:
    """La otra forma del mismo contacto, segun ``contacts``. Nunca se deduce.

    La correspondencia PN<->LID de un telefono es la misma mire quien la mire,
    asi que varias filas --una por cuenta-- dan la misma respuesta. Se acota
    igualmente cuando se sabe de que cuenta se habla.
    """

    def _uno(columna, condicion):
        stmt = select(columna).where(condicion)
        if account_id is not None:
            stmt = stmt.where(Contact.whatsapp_account_id == account_id)
        return session.execute(stmt.limit(1)).scalars().first()

    usuario = _usuario(jid)
    if servidor == "lid":
        telefono = _uno(Contact.jid, Contact.lid == jid)
        if telefono is None:
            # ``contacts.lid`` puede estar guardado sin servidor.
            telefono = _uno(Contact.jid, Contact.lid == usuario)
        return telefono
    lid = _uno(Contact.lid, Contact.jid == jid)
    if not lid:
        return None
    return lid if "@" in lid else f"{lid}@lid"


def _corto(jid: str) -> str:
    user, _, server = jid.partition("@")
    return f"{user[:6]}***@{server}" if server else f"{user[:6]}***"
