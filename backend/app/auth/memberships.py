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

from sqlalchemy import select, update

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
    # LA ACTIVA MANDA. Con varias cuentas, "la primera membresia" seria una
    # cualquiera: el usuario abriria la aplicacion en el WhatsApp que no
    # esperaba, y al recargar podria cambiarle.
    activa = cuenta_activa_de(sesion, user_id)
    if activa is not None:
        return activa
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

    Mira las DOS vias, igual que ``cuenta_efectiva_de``: hay cuentas creadas
    antes de que existieran las membresias, y su fila sigue diciendo la verdad
    en ``whatsapp_accounts.user_id``. Sin ese segundo camino, quien tenga una
    de esas no podria ni renombrar la suya -- se le contestaria que no es
    suya, que es exactamente lo contrario de la verdad.
    """
    if user_id is None or account_id is None:
        return False
    por_membresia = sesion.execute(
        select(UserWhatsAppMembership.id).where(
            UserWhatsAppMembership.user_id == user_id,
            UserWhatsAppMembership.whatsapp_account_id == account_id,
        )
    ).first()
    if por_membresia is not None:
        return True
    return (
        sesion.execute(
            select(WhatsAppAccount.id).where(
                WhatsAppAccount.id == account_id,
                WhatsAppAccount.user_id == user_id,
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
    sesion: Any,
    *,
    user_id: Any,
    account_id: Any,
    role: str = DUENO,
    activar: bool = False,
) -> UserWhatsAppMembership:
    """Asocia un usuario con una cuenta. EXPLICITO, nunca por deduccion.

    UNA PERSONA PUEDE TENER VARIAS
    ------------------------------
    Antes esto lanzaba ``YaTieneCuenta`` en cuanto habia una: la regla era
    "una cuenta por persona", y la base la sostenia con ``UNIQUE(user_id)``.
    Se retiro a proposito -- el WhatsApp personal y el del trabajo son cosas
    distintas y ninguna razon tecnica obliga a elegir.

    Lo que SI sigue garantizando la base es que solo una este ACTIVA.

    ``activar`` marca la nueva como la que se esta mirando. La primera de un
    usuario lo hace siempre: sin ninguna activa no veria sus propios chats.
    """
    if user_id is None or account_id is None:
        raise ValueError("hacen falta usuario y cuenta para conceder acceso")

    ya = (
        sesion.execute(
            select(UserWhatsAppMembership).where(
                UserWhatsAppMembership.user_id == user_id,
                UserWhatsAppMembership.whatsapp_account_id == account_id,
            )
        )
        .scalars()
        .first()
    )
    if ya is not None:
        # Idempotente: conceder dos veces lo mismo no es un error.
        if activar:
            activar_cuenta(sesion, user_id=user_id, account_id=account_id)
        return ya

    # ¿Es la primera? Entonces se activa quiera o no quien llame: una cuenta
    # sin activar es una cuenta invisible.
    #
    # Se cuenta en CUENTAS y no en membresias: quien tenga una de la via
    # antigua no tiene ninguna membresia, y contar esas haria que la SEGUNDA
    # cuenta pareciera la primera y le robara el sitio a la que estaba
    # mirando.
    primera = (
        sesion.execute(
            select(WhatsAppAccount.id).where(
                WhatsAppAccount.user_id == user_id,
                WhatsAppAccount.id != account_id,
            )
        )
        .scalars()
        .first()
        is None
    )

    membresia = UserWhatsAppMembership(
        user_id=user_id, whatsapp_account_id=account_id, role=role
    )
    sesion.add(membresia)
    sesion.flush()
    if activar or primera:
        activar_cuenta(sesion, user_id=user_id, account_id=account_id)
    log.info(
        "[APP] acceso concedido: usuario=%s cuenta=%s rol=%s",
        str(user_id)[:8],
        str(account_id)[:8],
        role,
    )
    return membresia


def activar_cuenta(
    sesion: Any, *, user_id: Any, account_id: Any, por_el_usuario: bool = False
) -> bool:
    """Marca esa cuenta como la que ese usuario esta mirando.

    ``por_el_usuario`` distingue quien lo pidio, y no es un detalle: una
    eleccion de la PERSONA se respeta aunque la cuenta parezca vacia --acaba
    de vincularse y aun no ha traido nada--, mientras que un valor puesto por
    defecto se puede sustituir sin preguntar. Por omision es ``False``: solo
    el endpoint que atiende al selector dice ``True``.

    Se apagan las demas ANTES de encender esta, y en la misma transaccion: el
    indice unico parcial (``user_id WHERE is_active``) rechazaria la segunda
    activa, asi que hacerlo al reves fallaria siempre.

    Devuelve ``False`` si esa cuenta no es de ese usuario. No lanza y no dice
    por que: un error distinto para "existe pero no es tuya" permitiria ir
    tanteando identificadores ajenos.
    """
    if user_id is None or account_id is None:
        return False

    suya = (
        sesion.execute(
            select(UserWhatsAppMembership).where(
                UserWhatsAppMembership.user_id == user_id,
                UserWhatsAppMembership.whatsapp_account_id == account_id,
            )
        )
        .scalars()
        .first()
    )
    if suya is None:
        # Puede ser una cuenta de la via antigua: existe y es suya, pero
        # nunca se le creo la membresia. Se crea ahora en vez de decir que no
        # es suya -- decir eso la dejaria fuera del selector para siempre.
        propia = sesion.execute(
            select(WhatsAppAccount.id).where(
                WhatsAppAccount.id == account_id,
                WhatsAppAccount.user_id == user_id,
            )
        ).scalar_one_or_none()
        if propia is None:
            return False
        suya = UserWhatsAppMembership(
            user_id=user_id, whatsapp_account_id=account_id, role=DUENO
        )
        sesion.add(suya)
        sesion.flush()

    sesion.execute(
        update(UserWhatsAppMembership)
        .where(
            UserWhatsAppMembership.user_id == user_id,
            UserWhatsAppMembership.whatsapp_account_id != account_id,
            UserWhatsAppMembership.is_active.is_(True),
        )
        .values(is_active=False)
    )
    suya.is_active = True
    if por_el_usuario:
        # Solo se ENCIENDE. Que el sistema vuelva a poner esta misma cuenta por
        # defecto no puede borrar el hecho de que alguien la eligio.
        suya.elegida_por_el_usuario = True
    sesion.flush()
    log.info(
        "[APP] cuenta activa: usuario=%s cuenta=%s (%s)",
        str(user_id)[:8],
        str(account_id)[:8],
        "elegida por el usuario" if por_el_usuario else "por defecto",
    )
    return True


def cuenta_activa_de(sesion: Any, user_id: Any) -> WhatsAppAccount | None:
    """La cuenta que ese usuario esta mirando, o ``None``.

    ``None`` cuando no tiene ninguna marcada -- que puede pasar si la fila de
    membresia nunca se creo, por ejemplo en una vinculacion a medias. Quien
    llame decide el respaldo; aqui no se elige una al azar.
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
            .where(
                UserWhatsAppMembership.user_id == user_id,
                UserWhatsAppMembership.is_active.is_(True),
            )
        )
        .scalars()
        .first()
    )


def asegurar_activa(sesion: Any, user_id: Any) -> WhatsAppAccount | None:
    """La cuenta activa, MARCANDO una si no habia ninguna.

    POR QUE NO BASTA CON "LA PRIMERA"
    ---------------------------------
    Caer en "la primera de la lista" cada vez parece equivalente y no lo es.
    ``created_at`` sale de ``now()``, que en PostgreSQL es la hora de INICIO DE
    LA TRANSACCION: dos cuentas creadas en la misma transaccion comparten
    marca, y entonces el desempate es el UUID -- aleatorio. El usuario abriria
    un WhatsApp u otro sin tocar nada.

    Peor todavia: dos sitios distintos que cayeran en "la primera" podian
    elegir cuentas distintas en la misma peticion. Es lo que hacia que el
    listado de cuentas dijera una y la lista de chats ensenara la otra.

    Marcando una, la respuesta es la misma para todos y se queda quieta. Se
    escribe, si -- una vez, y solo cuando no habia ninguna: sin esto, quien
    tenga una cuenta creada antes de las membresias no tendria ninguna activa
    NUNCA, porque nadie la marca.
    """
    activa = cuenta_activa_de(sesion, user_id)
    if activa is not None:
        return activa

    candidatas = cuentas_de_usuario(sesion, user_id)
    if not candidatas:
        return None

    # La mas antigua de las que ya estan vinculadas; si ninguna lo esta, la
    # mas antigua a secas. Empezar en una cuenta a medio vincular dejaria al
    # usuario mirando una lista vacia teniendo otra con todo su historial.
    elegida = next(
        (c for c in candidatas if c.session_status == "linked"), candidatas[0]
    )
    activar_cuenta(sesion, user_id=user_id, account_id=elegida.id)
    return elegida


def cuentas_de_usuario(sesion: Any, user_id: Any) -> list[WhatsAppAccount]:
    """TODAS las cuentas de ese usuario, en orden estable.

    El orden es por fecha de creacion: la primera vinculada aparece primero,
    que es como el usuario las recuerda. Sin ``ORDER BY``, PostgreSQL puede
    devolverlas en cualquier orden y el selector cambiaria solo.
    """
    if user_id is None:
        return []
    # LAS DOS VIAS, igual que `cuenta_efectiva_de` y por la misma razon: hay
    # cuentas creadas antes de que existieran las membresias, y su fila sigue
    # diciendo la verdad en `whatsapp_accounts.user_id`. Mirar solo las
    # membresias dejaria a esas personas sin ninguna cuenta -- es decir, sin
    # sus propios chats.
    filas = list(
        sesion.execute(
            select(WhatsAppAccount)
            .outerjoin(
                UserWhatsAppMembership,
                UserWhatsAppMembership.whatsapp_account_id == WhatsAppAccount.id,
            )
            .where(
                (UserWhatsAppMembership.user_id == user_id)
                | (WhatsAppAccount.user_id == user_id)
            )
            .order_by(WhatsAppAccount.created_at, WhatsAppAccount.id)
        )
        .scalars()
        .all()
    )
    # El `outerjoin` puede repetir una cuenta compartida por varias personas.
    #
    # Se deduplica con un diccionario y no con `set().add`: hay un guardia de
    # codigo que revisa quien escribe en este modulo buscando llamadas `.add`,
    # y un `set` local le haria saltar por algo que no toca la base.
    unicas = {fila.id: fila for fila in filas}
    return list(unicas.values())


class YaTieneCuenta(RuntimeError):
    """Ya no se lanza: una persona puede tener varias cuentas de WhatsApp.

    Se conserva el tipo porque hay codigo que lo captura, y quitarlo
    convertiria un ``except`` inofensivo en un ``NameError``.
    """
