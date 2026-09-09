"""Ponerle nombre a los grupos preguntandoselo a WhatsApp.

EL HUECO QUE CIERRA
-------------------
Un grupo sin nombre aparecia en la lista como "Grupo sin nombre". No es un
fallo de presentacion: es que el nombre nunca llegaba.

El nombre de un grupo puede venir por dos caminos, y los dos fallan a menudo:

* ``Conversation.name`` del blob de History Sync -- WhatsApp lo rellena a
  veces y a veces no, y en los blobs ``ON_DEMAND`` casi nunca;
* el app-state, que trae acciones de contacto pero no asuntos de grupo.

Queda un tercero que no se estaba usando: **preguntarselo al servidor**.
``pywhats`` expone ``Client.get_group_info(jid)`` en su API publica --una iq
``w:g2``-- y devuelve un ``GroupInfo`` con ``subject``, que es exactamente el
nombre. Estaba ahi desde el principio y no lo llamaba nadie.

QUE NO HACE
-----------
No inventa nombres. Si el servidor no contesta o el grupo no tiene asunto, el
chat se queda como estaba: es mejor "Grupo sin nombre" que un nombre fabricado.

Tampoco toca los chats individuales: esos se nombran por contacto, y ese es
otro camino.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select, update

from app.core.logging_setup import get_logger
from app.models import Chat

log = get_logger("SYNC")

#: Cuantos grupos se preguntan de una tanda.
#:
#: Cada uno es una iq contra el servidor. Se van de pocos en pocos y con pausa
#: entre medias: una rafaga de cien peticiones seguidas es justo lo que hace
#: que WhatsApp corte la conexion.
TANDA = 5

#: Pausa entre tandas, en segundos.
PAUSA = 0.4


def grupos_sin_nombre(session: Any, account_id: Any, *, limite: int = 200) -> list[str]:
    """Los JID de grupo de ESA cuenta que siguen sin nombre.

    Se acota por cuenta: preguntar por el grupo de otra persona seria pedirle
    al servidor datos de una conversacion que no es de esta sesion.
    """
    consulta = (
        select(Chat.jid)
        .where(Chat.jid.like("%@g.us"))
        .where((Chat.name.is_(None)) | (Chat.name == ""))
        .limit(limite)
    )
    if account_id is not None:
        consulta = consulta.where(Chat.whatsapp_account_id == account_id)
    try:
        return [j for j in session.execute(consulta).scalars() if j]
    except Exception:  # noqa: BLE001 - sin nombres se sigue funcionando
        log.debug("No se pudieron listar los grupos sin nombre")
        return []


def _guardar(session: Any, jid: str, asunto: str, account_id: Any) -> bool:
    """Escribe el asunto como nombre del chat. ``True`` si cambio algo."""
    consulta = update(Chat).where(Chat.jid == jid)
    if account_id is not None:
        consulta = consulta.where(Chat.whatsapp_account_id == account_id)
    # Solo se rellena el hueco: si alguien ya le puso nombre, ese manda.
    consulta = consulta.where((Chat.name.is_(None)) | (Chat.name == ""))
    return bool(session.execute(consulta.values(name=asunto)).rowcount)


async def resolver_nombres_de_grupo(
    client: Any,
    database: Any,
    *,
    account_id: Any = None,
    publish: Any = None,
) -> int:
    """Pregunta el asunto de cada grupo sin nombre y lo guarda.

    Devuelve cuantos se resolvieron. Nunca lanza: quedarse sin un nombre no
    puede tumbar la sincronizacion.

    :param publish: si se pasa, se avisa por cada grupo resuelto para que la
        pantalla lo actualice en el sitio, sin recargar la lista.
    """
    if client is None or database is None:
        return 0

    with database.transaction() as session:
        pendientes = grupos_sin_nombre(session, account_id)

    if not pendientes:
        return 0

    log.info("%d grupo(s) sin nombre: se le preguntan al servidor", len(pendientes))
    resueltos = 0

    for inicio in range(0, len(pendientes), TANDA):
        tanda = pendientes[inicio : inicio + TANDA]
        for jid in tanda:
            asunto = await _asunto_de(client, jid)
            if not asunto:
                continue
            with database.transaction() as session:
                if not _guardar(session, jid, asunto, account_id):
                    continue
            resueltos += 1
            if publish is not None:
                # Un aviso por grupo, con el HECHO desnudo: quien lo publica no
                # sabe como se pinta una conversacion, y no tiene por que. La
                # fila para la pantalla la construye el traductor de eventos,
                # que es quien conoce ese formato.
                publish("chat_renamed", {"jid": jid, "name": asunto})
        if inicio + TANDA < len(pendientes):
            await asyncio.sleep(PAUSA)

    if resueltos:
        log.info("%d grupo(s) ya tienen nombre", resueltos)
    return resueltos


async def _asunto_de(client: Any, jid: str) -> str | None:
    """El asunto del grupo, o ``None``. No lanza."""
    try:
        from app.wa.tipos import JID

        # `JID` es un dataclass plano: no tiene constructor desde cadena, asi
        # que se parte a mano como en el resto del proyecto.
        usuario, _, servidor = jid.partition("@")
        info = await client.get_group_info(
            JID(user=usuario, server=servidor or "g.us", device=0)
        )
    except Exception as fallo:  # noqa: BLE001 - un grupo que no contesta no para al resto
        log.debug("No se pudo consultar el grupo (%s)", str(fallo)[:80])
        return None
    asunto = (getattr(info, "subject", "") or "").strip()
    return asunto or None
