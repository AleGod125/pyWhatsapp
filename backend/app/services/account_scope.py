"""El alcance por cuenta, en UN solo sitio. La transicion vive aqui.

POR QUE ESTE MODULO
-------------------
Los datos privados de WhatsApp --chats, contactos, mensajes, historial-- se
estan pasando de "unicos en todo el sistema" a "unicos dentro de su cuenta".
Ese cambio toca el esquema y el codigo que lo usa, y los dos no pueden cambiar
en el mismo instante: la base sigue hoy en ``f1a2b3c4d5e6``, con las unicidades
globales todavia puestas.

La forma de hacer eso mal es repartir ``if esquema_nuevo:`` por veinte
ficheros. Cuando llegue el dia de aplicar la migracion habria que encontrar los
veinte, y el que se quede sin cambiar es el que duplica datos en silencio.

Asi que la transicion vive aqui. **Cuando la migracion se aplique, se cambia
este fichero y nada mas.**

QUE HAY DENTRO
--------------
* a que restriccion apunta cada ``ON CONFLICT``;
* si una tabla ya tiene columna de cuenta;
* el ayudante de la cuenta unica, que es temporal y esta marcado como tal.

LO QUE NO HAY
-------------
Ningun estado mutable global. Nada que dependa del hilo. La cuenta se pasa
explicita: cuando un dato privado no sabe de quien es, el fallo aparece donde
falta el dato y no tres capas mas abajo.
"""

from __future__ import annotations

from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("APP")


def tiene_columna(modelo: Any, nombre: str) -> bool:
    """Si esa tabla ya tiene esa columna en el esquema cargado.

    Permite que el codigo acepte la cuenta ANTES de que exista la columna
    donde guardarla, sin mentir: mientras no este, no se escribe; en cuanto la
    migracion la anada, se escribe sola.
    """
    try:
        return nombre in modelo.__table__.c
    except Exception:  # noqa: BLE001 - preguntar no puede romper nada
        return False


def destino_de_conflicto(modelo: Any, *columnas: str) -> list[Any]:
    """Las columnas del ``ON CONFLICT``, segun lo que el esquema soporte HOY.

    Devuelve la version por cuenta si la columna ya existe, y la global si
    todavia no. Es la unica pieza que hay que revisar el dia que se aplique la
    migracion, y el dia que se aplique dejara de tener alternativa: devolvera
    siempre la compuesta.
    """
    # Se mira la RESTRICCION, no la columna.
    #
    # `chats` ya tenia `whatsapp_account_id` mucho antes de que existiera la
    # unicidad compuesta, asi que preguntar por la columna decia que si cuando
    # la base seguia teniendo `UNIQUE(jid)` a secas -- y `ON CONFLICT` fallaba
    # con "no hay restriccion unica que coincida". Lo que hace falta saber es
    # si existe el indice que va a resolver el conflicto.
    compuesta = ("whatsapp_account_id",) + columnas
    if _hay_unicidad(modelo, compuesta):
        return [modelo.__table__.c[c] for c in compuesta]
    return [modelo.__table__.c[c] for c in columnas]


def _hay_unicidad(modelo: Any, columnas: tuple[str, ...]) -> bool:
    """Si el modelo declara una unicidad EXACTAMENTE sobre esas columnas."""
    objetivo = set(columnas)
    try:
        from sqlalchemy import UniqueConstraint

        for restriccion in modelo.__table__.constraints:
            if isinstance(restriccion, UniqueConstraint):
                if {c.name for c in restriccion.columns} == objetivo:
                    return True
        for indice in modelo.__table__.indexes:
            if indice.unique and {c.name for c in indice.columns} == objetivo:
                return True
    except Exception:  # noqa: BLE001 - preguntar no puede romper nada
        return False
    return False


def destino_de_dedupe_de_mensaje() -> list[Any]:
    """Por que columnas se deduplica un mensaje: ``(chat_id, wa_msg_id)``.

    POR QUE `chat_id` Y NUNCA `chat_jid`
    ------------------------------------
    ``chat_id`` es unico por ``(cuenta, jid)``, asi que deduplicar por el es
    per-cuenta de forma transitiva: dos cuentas que hablen con la misma persona
    tienen ``chat_id`` distintos y el mismo identificador de mensaje NO
    colisiona.

    ``chat_jid`` es identico entre cuentas. Deduplicar por el significa que el
    mensaje que le llega a la segunda cuenta se descarta como duplicado del de
    la primera, **en silencio**. Y en un grupo es peor: el identificador es el
    mismo para todos los que lo reciben.

    AQUI HABIA UN RESPALDO, Y SE QUITO
    ----------------------------------
    Si el indice no existia, esto devolvia la version por ``chat_jid``. La
    intencion era sobrevivir a una base a medio migrar; el efecto real es que
    una instalacion sin migrar cruza mensajes entre cuentas sin que nada lo
    diga. Un fallo de despliegue tiene que doler en el arranque, no aparecer
    meses despues como conversaciones que faltan.
    """
    from app.models import Message

    if not _hay_unicidad(Message, ("chat_id", "whatsapp_message_id")):
        raise RuntimeError(
            "Falta la unicidad (chat_id, whatsapp_message_id) en `messages`. "
            "Sin ella, los mensajes se deduplicarian por `chat_jid`, que es "
            "el mismo en todas las cuentas: el mensaje de la segunda cuenta se "
            "descartaria como duplicado del de la primera. Ejecuta las "
            "migraciones antes de arrancar."
        )
    return [Message.chat_id, Message.whatsapp_message_id]


def cuenta_unica(sesion: Any) -> Any:
    """La unica cuenta de WhatsApp que hay, o ``None`` si no hay exactamente una.

    AYUDANTE DE TRANSICION. Existe para el codigo que todavia no recibe la
    cuenta por parametro, y **solo contesta cuando la respuesta es inequivoca**:
    con cero cuentas o con dos, devuelve ``None`` en vez de elegir.

    No es una via valida a largo plazo. En cuanto haya dos cuentas de verdad,
    todo lo que dependa de esto empieza a devolver ``None`` -- que es
    exactamente lo que tiene que pasar: mejor quedarse sin dato que atribuirlo
    a quien no es.
    """
    from sqlalchemy import select

    from app.models import WhatsAppAccount

    try:
        filas = sesion.execute(select(WhatsAppAccount.id).limit(2)).scalars().all()
    except Exception:  # noqa: BLE001 - no poder mirarlo no puede tumbar nada
        return None
    return filas[0] if len(filas) == 1 else None


def cuenta_del_chat(sesion: Any, chat_jid: str) -> Any:
    """La cuenta a la que pertenece esa conversacion, si se sabe.

    Se usa cuando quien llama solo tiene el identificador de la conversacion.
    Mientras haya una sola cuenta la respuesta es la misma que
    ``cuenta_unica``; con varias, la da el chat, que es quien lo sabe.
    """
    from sqlalchemy import select

    from app.models import Chat

    if not chat_jid:
        return None
    try:
        # `limit(2)` y no `scalar_one_or_none()`: el mismo JID existe en tantas
        # filas como cuentas hablen con ese contacto, y ahi `scalar_one_or_none`
        # revienta con "Multiple rows were found". Con varias no hay respuesta
        # correcta desde aqui, asi que no se da ninguna.
        filas = (
            sesion.execute(
                select(Chat.whatsapp_account_id)
                .where(Chat.jid == chat_jid)
                .limit(2)
            )
            .scalars()
            .all()
        )
    except Exception:  # noqa: BLE001
        return None
    return filas[0] if len(filas) == 1 else None


def clave_de_cuenta(base: str, account_id: Any) -> str:
    """Una clave de ``app_state`` que pertenece a UNA cuenta de WhatsApp.

    POR QUE HACE FALTA
    ------------------
    ``app_state`` guarda dos cosas distintas bajo el mismo techo: preferencias
    de usuario --que ya llevan el usuario en la clave-- y estado privado de la
    sesion de WhatsApp: si la capacidad ``ON_DEMAND`` esta confirmada, la
    huella de la sesion de extraccion, si llego el historial inicial.

    Ese segundo grupo es POR CUENTA y estaba bajo clave global. Con dos
    cuentas, el motor de la segunda leeria el veredicto de capacidad de la
    primera y actuaria con el: no filtra mensajes, pero si hace que una cuenta
    tome decisiones con la evidencia de otra.

    Se arregla con el prefijo, sin tocar el esquema. Sin cuenta se devuelve la
    clave de siempre: durante la transicion hay codigo que todavia no la sabe,
    y perder el estado guardado costaria una reconfirmacion de capacidad
    --varios minutos de canary-- en cada arranque.
    """
    if account_id is None:
        return base
    return f"{base}:{account_id}"


def estado_de_esta_conversacion(
    chat_jid: str, *, chat_id: Any = None, account_id: Any = None
):
    """El criterio para dar con LA fila de estado de historial. UNO solo.

    POR QUE IMPORTA TANTO
    ---------------------
    ``chat_jid`` deja de ser unico en cuanto dos cuentas tienen el mismo
    contacto. Un ``UPDATE ... WHERE chat_jid`` tocaria entonces las filas de
    LAS DOS: sin error y sin aviso, el progreso de historial de una persona
    modificado por lo que hizo otra.

    Se usa lo mejor que haya, en este orden:

    1. ``chat_id`` -- unico, y apunta a un chat que ya tiene cuenta. Sin
       ambiguedad posible;
    2. la cuenta mas el jid -- se resuelve el chat dentro de esa cuenta;
    3. solo el jid -- lo de siempre, para el codigo que todavia no sabe de
       quien es. Correcto mientras haya una sola cuenta, que es el caso hoy.
    """
    from sqlalchemy import select

    from app.models import Chat, ChatHistoryState

    if chat_id is not None:
        return ChatHistoryState.chat_id == chat_id
    if account_id is not None:
        return ChatHistoryState.chat_id.in_(
            select(Chat.id).where(
                Chat.jid == chat_jid, Chat.whatsapp_account_id == account_id
            )
        )
    return ChatHistoryState.chat_jid == chat_jid



def chat_id_de(sesion: Any, chat_jid: str, *, account_id: Any = None) -> Any:
    """El ``chats.id`` de esa conversacion, o ``None`` si no es inequivoco.

    POR QUE NO VALE `select(Chat.id).where(Chat.jid == jid)`
    -------------------------------------------------------
    Desde que la unicidad es ``(whatsapp_account_id, jid)``, **el mismo JID
    existe en tantas filas como cuentas hablen con ese contacto**, que es
    justamente lo que se buscaba: la conversacion de A con Marta y la de B con
    Marta son dos, y no deben mezclarse.

    Ese ``select`` con ``scalar_one_or_none()`` revienta en cuanto hay dos::

        Multiple rows were found when one or none was required

    Se midio en el mantenimiento real en cuanto existieron dos cuentas. Y la
    "solucion" facil --cambiarlo por ``.first()``-- es peor que el error: la
    base no garantiza ningun orden, asi que le atribuiria a una persona la
    conversacion de otra, en silencio y sin traza.

    Con ``account_id`` la respuesta es exacta. Sin el, solo se contesta cuando
    hay UNA sola candidata; con varias se devuelve ``None``, porque no hay
    respuesta correcta y adivinar es el fallo.
    """
    from sqlalchemy import select

    from app.models import Chat

    if not chat_jid:
        return None
    consulta = select(Chat.id).where(Chat.jid == chat_jid)
    if account_id is not None:
        consulta = consulta.where(Chat.whatsapp_account_id == account_id)
    try:
        filas = sesion.execute(consulta.limit(2)).scalars().all()
    except Exception:  # noqa: BLE001 - no poder mirarlo no puede tumbar nada
        return None
    return filas[0] if len(filas) == 1 else None
