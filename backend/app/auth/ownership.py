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
    """TODAS las cuentas de WhatsApp de ese usuario.

    Es la lista de AUTORIZACION: lo que esa persona tiene derecho a tocar.
    Para decidir que se le ENSEÑA ahora mismo no sirve --le mezclaria los
    chats de dos WhatsApp distintos en una sola lista--; eso lo resuelve
    :func:`cuenta_visible_de`.

    El orden es estable a proposito. Sin ``ORDER BY``, PostgreSQL puede
    devolver las filas en cualquier orden, y "la primera" dejaria de ser
    siempre la misma cuenta entre dos peticiones.
    """
    return list(
        session.execute(
            select(WhatsAppAccount.id)
            .where(WhatsAppAccount.user_id == user_id)
            .order_by(WhatsAppAccount.created_at, WhatsAppAccount.id)
        ).scalars()
    )


def cuenta_visible_de(session: Any, user_id: Any, pedida: Any = None) -> Any:
    """LA cuenta cuyo contenido se enseña en esta peticion. Nunca varias.

    POR QUE UNA Y NO LA LISTA
    -------------------------
    Un usuario puede tener varios WhatsApp vinculados --el personal y el del
    trabajo, por ejemplo-- y son cosas separadas: sus chats, su historial y su
    copia de seguridad no tienen nada que ver. Acotar los listados por USUARIO
    los junta en una sola lista sin que nada lo delate, y el usuario no tiene
    forma de saber de cual es cada conversacion.

    ``pedida`` es lo que dice el navegador, y NO se cree por si solo: si no es
    una cuenta de este usuario se ignora y se cae en la suya. Confiar en ese
    identificador seria dejar que cualquiera lea la copia de otro cambiando un
    parametro de la URL.

    Devuelve ``None`` cuando esa persona no tiene ninguna cuenta, que es
    distinto de "no se pudo comprobar": significa que hay que vincular.
    """
    mias = cuentas_de(session, user_id)
    if not mias:
        return None
    if pedida is not None:
        pedida = str(pedida)
        for cuenta in mias:
            if str(cuenta) == pedida:
                return cuenta
        # Pedida pero ajena: se ignora en silencio y se sigue con la suya. No
        # se responde 403 porque eso confirmaria que esa cuenta existe.

    # La que ese usuario dejo seleccionada. Se guarda en el servidor para que
    # sobreviva a recargas y para que una ruta que se olvide de mandar el
    # parametro no caiga en una cuenta cualquiera.
    from app.auth.memberships import asegurar_activa

    # `asegurar_activa` y no "la primera de la lista": las dos parecen
    # equivalentes y no lo son. Ver su docstring -- dos cuentas creadas en la
    # misma transaccion comparten `created_at` y el desempate acaba siendo el
    # UUID, asi que "la primera" podia cambiar entre dos consultas.
    activa = asegurar_activa(session, user_id)
    if activa is not None:
        return activa.id
    return mias[0]


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
