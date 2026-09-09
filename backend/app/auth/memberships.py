"""Quien puede entrar a que cuenta de WhatsApp. Un solo sitio donde se decide.

POR QUE EXISTE ESTE MODULO
--------------------------
La pregunta "¿este usuario tiene WhatsApp?" se hace en el onboarding, en el
emparejamiento, en cada ruta privada y en el arranque. Si cada sitio la
contesta a su manera, tarde o temprano dos sitios contestan distinto -- y el
que conteste de mas es por donde se escapan los datos de otra persona.

Se midio ese fallo en su version mas simple: el backend miraba el estado
GLOBAL del proceso, asi que un usuario recien registrado veia "cuenta
vinculada" porque habia OTRA cuenta conectada en el servidor.

LA REGLA
--------
El acceso NO es una propiedad del proceso ni del runtime que este corriendo.
Es una fila en ``user_whatsapp_memberships``. O esta, o no esta.

LO QUE NO SE HACE AQUI
----------------------
No se crea ninguna membresia por coincidencia. Que alguien escanee un telefono
que ya esta registrado no le da acceso: hace falta un acto explicito. Dar
acceso por reconocer un numero seria entregar el historial de otra persona a
quien tenga su movil un minuto.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.core.logging_setup import get_logger
from app.models import UserWhatsAppMembership, WhatsAppAccount

log = get_logger("APP")

#: Quien creo la vinculacion. Hoy ve lo mismo que un miembro; el rol existe
#: para poder distinguirlos el dia que haga falta, no antes.
DUENO = "owner"
#: Quien recibio acceso a una cuenta que no creo.
MIEMBRO = "member"


def cuenta_de(sesion: Any, user_id: Any) -> WhatsAppAccount | None:
    """La cuenta de WhatsApp de ese usuario, o ``None`` si no tiene ninguna.

    ``None`` significa exactamente eso: **este usuario no tiene WhatsApp**. No
    significa "no se pudo comprobar" ni "hay otro conectado". Quien llame debe
    tratarlo como "hay que vincular".
    """
    if user_id is None:
        return None
    return (
        sesion.execute(
            select(WhatsAppAccount)
            .join(
                UserWhatsAppMembership,
                UserWhatsAppMembership.whatsapp_account_id == WhatsAppAccount.id,
            )
            .where(UserWhatsAppMembership.user_id == user_id)
        )
        .scalars()
        .first()
    )


def id_de_cuenta_de(sesion: Any, user_id: Any) -> Any:
    """Solo el identificador, para quien no necesita la fila entera."""
    if user_id is None:
        return None
    return sesion.execute(
        select(UserWhatsAppMembership.whatsapp_account_id).where(
            UserWhatsAppMembership.user_id == user_id
        )
    ).scalar_one_or_none()


def cuenta_efectiva_de(sesion: Any, user_id: Any) -> WhatsAppAccount | None:
    """La cuenta de ese usuario, contando tambien la via antigua.

    POR QUE HAY DOS VIAS Y NO UNA
    -----------------------------
    La membresia es la buena y manda. Pero ``whatsapp_accounts.user_id`` --la
    columna que dice quien CREO la vinculacion-- sigue siendo verdad durante
    dos ratos en los que perder el acceso seria un fallo grave:

    * mientras una vinculacion se esta completando, puede existir ya la fila
      de cuenta y todavia no la de membresia. Mandar a esa persona a escanear
      otra vez lo que acaba de escanear es exactamente lo que no puede pasar;
    * en una base que aun no ha pasado la migracion.

    Asi que "no tiene membresia" NO significa "no tiene cuenta". Confundir esas
    dos cosas fue lo que rompio dos pruebas: un usuario con su cuenta creada
    por la via antigua aparecia como si la sesion fuera de otro.

    Devolver ``None`` aqui si significa, ahora si, que esta persona no tiene
    ninguna cuenta de WhatsApp.
    """
    if user_id is None:
        return None
    por_membresia = cuenta_de(sesion, user_id)
    if por_membresia is not None:
        return por_membresia
    return (
        sesion.execute(
            select(WhatsAppAccount).where(WhatsAppAccount.user_id == user_id)
        )
        .scalars()
        .first()
    )


def tiene_acceso(sesion: Any, user_id: Any, account_id: Any) -> bool:
    """Si ese usuario puede entrar a ESA cuenta.

    Es la comprobacion que va en toda ruta que reciba un identificador de
    cuenta desde el navegador. Un parametro del cliente no es una autoridad:
    dice a que se quiere entrar, no a que se puede.
    """
    if user_id is None or account_id is None:
        return False
    return (
        sesion.execute(
            select(UserWhatsAppMembership.id).where(
                UserWhatsAppMembership.user_id == user_id,
                UserWhatsAppMembership.whatsapp_account_id == account_id,
            )
        ).first()
        is not None
    )


def miembros_de(sesion: Any, account_id: Any) -> list[Any]:
    """Los usuarios con acceso a esa cuenta. Puede haber mas de uno."""
    if account_id is None:
        return []
    return list(
        sesion.execute(
            select(UserWhatsAppMembership.user_id).where(
                UserWhatsAppMembership.whatsapp_account_id == account_id
            )
        )
        .scalars()
        .all()
    )


def conceder(
    sesion: Any, *, user_id: Any, account_id: Any, role: str = DUENO
) -> UserWhatsAppMembership:
    """Asocia un usuario con una cuenta. EXPLICITO, nunca por deduccion.

    :raises YaTieneCuenta: si ese usuario ya tiene otra cuenta. Una persona
        tiene como mucho una, y la base tambien lo impide -- esto solo permite
        dar un error entendible en vez de un choque de restriccion.
    """
    if user_id is None or account_id is None:
        raise ValueError("hacen falta usuario y cuenta para conceder acceso")

    ya = (
        sesion.execute(
            select(UserWhatsAppMembership).where(
                UserWhatsAppMembership.user_id == user_id
            )
        )
        .scalars()
        .first()
    )
    if ya is not None:
        if ya.whatsapp_account_id == account_id:
            return ya  # idempotente: conceder dos veces lo mismo no es un error
        raise YaTieneCuenta(
            "Este usuario ya tiene una cuenta de WhatsApp asociada."
        )

    membresia = UserWhatsAppMembership(
        user_id=user_id, whatsapp_account_id=account_id, role=role
    )
    sesion.add(membresia)
    sesion.flush()
    log.info(
        "[APP] acceso concedido: usuario=%s cuenta=%s rol=%s",
        str(user_id)[:8],
        str(account_id)[:8],
        role,
    )
    return membresia


class YaTieneCuenta(RuntimeError):
    """Una persona, una cuenta de WhatsApp. Es la regla de producto de hoy."""
