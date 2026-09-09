"""De quien es cada cosa.

LA CADENA
---------
``chat -> whatsapp_account -> user``. Los mensajes y la multimedia cuelgan del
chat, asi que su dueno es el mismo. No se repite ``user_id`` en cada tabla: una
sola cadena es una sola cosa que mantener correcta.

POR QUE 404 Y NO 403
--------------------
Un 403 sobre un identificador ajeno confirma que ese identificador existe.
Iterando se puede averiguar cuantos chats tiene otra persona y cuando los
creo. Un 404 no dice nada, y para quien pregunta legitimamente por algo que no
existe la respuesta es la misma.

DURANTE LA TRANSICION
---------------------
Las filas anteriores a multiusuario tienen ``whatsapp_account_id`` a NULL. No
pertenecen a nadie, asi que NO se muestran: darlas por buenas para el primero
que entre seria entregar el historial de la cuenta de pruebas al primer
registro. El reset de la fase las elimina.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.models import Chat, MediaFile, Message, WhatsAppAccount


def cuentas_de(session: Any, user_id: Any) -> list[Any]:
    """Identificadores de las cuentas de WhatsApp de ese usuario."""
    return list(
        session.execute(
            select(WhatsAppAccount.id).where(WhatsAppAccount.user_id == user_id)
        ).scalars()
    )


def filtro_de_chats(user_id_cuentas: list[Any]) -> Any:
    """Clausula para acotar cualquier consulta sobre ``chats``.

    Con la lista vacia devuelve una condicion imposible en vez de omitirse:
    un filtro que "no aplica" seria un filtro que deja verlo todo.
    """
    if not user_id_cuentas:
        return Chat.id.is_(None)
    return Chat.whatsapp_account_id.in_(user_id_cuentas)


def chat_es_de(session: Any, chat_id: int, user_id: Any) -> bool:
    cuentas = cuentas_de(session, user_id)
    if not cuentas:
        return False
    return (
        session.execute(
            select(Chat.id).where(
                Chat.id == chat_id,
                Chat.whatsapp_account_id.in_(cuentas),
            )
        ).scalar_one_or_none()
        is not None
    )


def media_es_de(session: Any, media_id: int, user_id: Any) -> bool:
    """Sin esto se podria leer la multimedia de otro cambiando el id en la URL."""
    cuentas = cuentas_de(session, user_id)
    if not cuentas:
        return False
    return (
        session.execute(
            select(MediaFile.id)
            .join(Chat, Chat.id == MediaFile.chat_id)
            .where(
                MediaFile.id == media_id,
                Chat.whatsapp_account_id.in_(cuentas),
            )
        ).scalar_one_or_none()
        is not None
    )


def mensaje_es_de(session: Any, message_id: int, user_id: Any) -> bool:
    cuentas = cuentas_de(session, user_id)
    if not cuentas:
        return False
    return (
        session.execute(
            select(Message.id)
            .join(Chat, Chat.id == Message.chat_id)
            .where(
                Message.id == message_id,
                Chat.whatsapp_account_id.in_(cuentas),
            )
        ).scalar_one_or_none()
        is not None
    )


def cuenta_activa_de(session: Any, user_id: Any) -> Any:
    """La cuenta de WhatsApp de ese usuario, o ``None``."""
    return session.execute(
        select(WhatsAppAccount).where(WhatsAppAccount.user_id == user_id)
    ).scalars().first()


def dueno_del_runtime(session: Any) -> Any:
    """Quien tiene vinculada la sesion que este proceso puede abrir.

    En esta fase el runtime sostiene UNA sesion de WhatsApp. Sirve para
    responder con un conflicto claro cuando otro usuario intenta usarla, en
    vez de dejarle ver una sesion que no es suya.
    """
    fila = session.execute(
        select(WhatsAppAccount).where(WhatsAppAccount.session_status == "linked")
    ).scalars().first()
    return fila.user_id if fila is not None else None
