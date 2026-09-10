"""Operaciones de persistencia sobre PostgreSQL.

Todo lo que escribe en la base pasa por aqui. Dos reglas de rendimiento que se
aplican en todo el modulo:

* Nunca un COMMIT por mensaje. Los lotes se insertan con un unico INSERT ...
  ON CONFLICT y el llamante controla la transaccion.
* Las consultas de la GUI no traen ``raw_proto``: es la columna mas pesada y
  no se necesita para pintar una conversacion.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from sqlalchemy import bindparam, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import AppState, Chat, ChatHistoryState, Contact, MediaFile, Message

# Prefijos de identificadores generados localmente por implementaciones
# previas. Nunca deben usarse como ancla de historial: el servidor de WhatsApp
# no los conoce, responde con un ACK y luego no llega ninguna History Sync.
SYNTHETIC_PREFIXES = ("opaque-", "synthetic-", "local-")


# ---------------------------------------------------------------------------
# Cursor historico
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryCursor:
    """Ancla utilizable para una peticion HISTORY_SYNC_ON_DEMAND."""

    message_id: str
    timestamp: int


def is_valid_history_cursor_id(message_id: str | None) -> bool:
    """``True`` si el identificador sirve como ancla para el servidor.

    Rechaza NULL, cadena vacia y cualquier identificador generado localmente.
    """
    if not message_id:
        return False
    lowered = message_id.lower()
    return not any(lowered.startswith(prefix) for prefix in SYNTHETIC_PREFIXES)


def get_oldest_valid_history_cursor(
    session: Session, chat_jid: str, *, chat_id: int | None = None
) -> HistoryCursor | None:
    """Mensaje mas antiguo del chat QUE TENGA UN ID REAL DE WHATSAPP.

    Ojo con la distincion, que es la causa de un bug historico:

    * "mensaje mas antiguo almacenado" -> es lo que se muestra en las
      estadisticas (ver :func:`get_oldest_stored_timestamp`).
    * "cursor tecnico mas antiguo utilizable" -> es ESTO, y puede ser un
      mensaje POSTERIOR.

    Ejemplo: si el mensaje del 13 de agosto no tiene ID real y el del 14 si,
    las estadisticas siguen diciendo "historial desde el 13" pero ON_DEMAND
    debe anclarse en el del 14.

    Devuelve ``None`` si el chat no tiene ningun mensaje con ID utilizable;
    ese chat es ``no_valid_cursor``, no un error.

    ``chat_id`` acota a UNA conversacion. Sin el se buscan los mensajes por
    ``chat_jid``, que se repite en cuanto dos cuentas hablan con el mismo
    contacto: el ancla saldria de la conversacion de otra persona, y ON_DEMAND
    pediria historial anclado en un mensaje que este telefono no conoce.
    """
    stmt = (
        select(Message.whatsapp_message_id, Message.timestamp)
        .where(
            Message.chat_id == chat_id
            if chat_id is not None
            else Message.chat_jid == chat_jid,
            Message.whatsapp_message_id.is_not(None),
            Message.whatsapp_message_id != "",
        )
        .order_by(Message.timestamp.asc(), Message.id.asc())
    )
    for message_id, timestamp in session.execute(stmt):
        # Segundo filtro en Python: la lista de prefijos sinteticos es
        # politica de la aplicacion y puede crecer sin tocar el esquema.
        if is_valid_history_cursor_id(message_id):
            return HistoryCursor(message_id=message_id, timestamp=timestamp)
    return None


def get_oldest_stored_timestamp(session: Session, chat_jid: str) -> int | None:
    """Timestamp del mensaje mas antiguo almacenado, tenga ID real o no."""
    return session.execute(
        select(func.min(Message.timestamp)).where(Message.chat_jid == chat_jid)
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Chats y contactos
# ---------------------------------------------------------------------------


def upsert_chat(
    session: Session,
    *,
    jid: str,
    name: str | None = None,
    chat_type: str | None = None,
    last_message: str | None = None,
    last_message_timestamp: int | None = None,
    raw_metadata: dict[str, Any] | None = None,
    whatsapp_account_id: Any = None,
    archived: bool | None = None,
    locked: bool | None = None,
    pinned_at: int | None = None,
    mute_until: int | None = None,
) -> int:
    """Inserta o actualiza un chat y devuelve su ``id``.

    Los campos que llegan como ``None`` NO pisan lo que ya hubiera: History
    Sync y los mensajes live traen informacion parcial distinta y un update
    ciego borraria el nombre que ya se habia resuelto.

    EL ESTADO DE LA CONVERSACION VIAJA JUNTO
    ----------------------------------------
    ``archived``, ``locked``, ``pinned_at`` y ``mute_until`` son una excepcion
    a la regla de arriba, y tienen que serlo: aqui ``False`` y ``NULL`` son
    datos, no ausencias. Desarchivar un chat es exactamente mandar
    ``archived=False``, y dejar de fijarlo es mandar ``pinned_at=None``; si se
    ignorara lo vacio, un chat archivado no podria volver nunca a la lista
    normal.

    Por eso los cuatro llegan juntos o no llega ninguno. Quien los sabe --el
    historial, que los recibe en cada ``proto.Conversation``-- los manda todos;
    quien no --un mensaje en vivo, que solo trae texto y hora-- no manda
    ninguno y el estado guardado se queda como estaba. ``archived`` es el que
    marca la diferencia entre los dos casos.

    ``whatsapp_account_id`` dice DE QUIEN es el chat. Sin el, el chat queda
    sin dueno y el filtro de propiedad lo excluye de la lista: existe en la
    base y no lo ve nadie. Es lo que dejaba el panel vacio con 40 chats
    dentro.
    """
    # LA CUENTA ES OBLIGATORIA.
    #
    # Un chat sin dueno no colisiona con nada --PostgreSQL trata los NULL como
    # distintos-- asi que se duplicaria en silencio, y ademas el filtro de
    # propiedad lo excluiria del listado: existiria en la base sin que lo viera
    # nadie. Es lo que dejaba el panel vacio con cuarenta chats dentro.
    #
    # Si quien llama no la sabe, se usa la unica que haya. Ese respaldo es de
    # transicion y se cae solo: con dos cuentas devuelve `None` y entonces hay
    # que pasarla, que es exactamente lo que debe ocurrir.
    from app.services.account_scope import cuenta_unica, destino_de_conflicto

    if whatsapp_account_id is None:
        whatsapp_account_id = cuenta_unica(session)
    if whatsapp_account_id is None:
        raise ValueError(
            "upsert_chat necesita whatsapp_account_id: un chat sin cuenta no "
            "lo ve nadie y se duplica en silencio"
        )

    values: dict[str, Any] = {
        "jid": jid,
        "chat_type": chat_type or "unknown",
        "whatsapp_account_id": whatsapp_account_id,
    }
    for column, value in (
        ("name", name),
        ("last_message", last_message),
        ("last_message_timestamp", last_message_timestamp),
        ("raw_metadata", raw_metadata),
    ):
        if value is not None:
            values[column] = value

    # El estado de la conversacion, TAL CUAL, vacios incluidos. Tiene que ir
    # antes de construir el INSERT: `values` ya no se relee despues.
    estado = (
        {
            "archived": bool(archived),
            "locked": bool(locked),
            "pinned_at": pinned_at,
            "mute_until": mute_until,
        }
        if archived is not None
        else {}
    )
    values.update(estado)

    stmt = insert(Chat).values(**values)
    updates = {
        column: func.coalesce(stmt.excluded[column], Chat.__table__.c[column])
        for column in ("name", "last_message", "raw_metadata")
        if column in values
    }
    if "whatsapp_account_id" in values:
        # Se rellena si falta, pero NO se reasigna: cambiar el dueno de un
        # chat que ya lo tiene seria entregarle a alguien la conversacion de
        # otro. Un chat huerfano si puede adoptarse por su cuenta legitima.
        updates["whatsapp_account_id"] = func.coalesce(
            Chat.__table__.c.whatsapp_account_id,
            stmt.excluded.whatsapp_account_id,
        )
    # El chat_type solo mejora: 'unknown' nunca pisa un tipo ya conocido.
    if chat_type and chat_type != "unknown":
        updates["chat_type"] = stmt.excluded.chat_type
    # El timestamp del ultimo mensaje solo avanza hacia delante.
    if last_message_timestamp is not None:
        updates["last_message_timestamp"] = func.greatest(
            func.coalesce(Chat.__table__.c.last_message_timestamp, 0),
            stmt.excluded.last_message_timestamp,
        )

    # `coalesce` no vale aqui: dejaria de fijar un chat imposible, porque
    # NULL significa "ya no esta fijado", no "no lo se".
    updates.update({columna: stmt.excluded[columna] for columna in estado})

    # El destino del conflicto sale de un solo sitio.
    stmt = stmt.on_conflict_do_update(
        index_elements=destino_de_conflicto(Chat, "jid"), set_=updates
    ).returning(Chat.id)
    return session.execute(stmt).scalar_one()


def marcar_estado_de_chat(
    database: Any,
    jid: str,
    *,
    whatsapp_account_id: Any,
    archived: bool | None = None,
    pinned: Any = None,
    muted: Any = None,
) -> bool:
    """Archiva, fija o silencia un chat que YA existe. Devuelve si cambio algo.

    POR QUE NO PASA POR ``upsert_chat``
    -----------------------------------
    Esto llega de ``chats.update``, que avisa de un cambio sobre una
    conversacion que el telefono ya conoce. Si el chat no esta en la base, no
    hay que crearlo: crearlo aqui produciria una fila sin nombre, sin mensajes
    y sin tipo, solo para anotar que esta archivada. Se ignora y ya.

    LO QUE LLEGA Y LO QUE SE GUARDA NO ES LO MISMO
    ----------------------------------------------
    El evento dice "fijado si/no"; la columna guarda CUANDO se fijo, porque
    entre varios fijados el orden es ese. Cuando el evento trae la marca de
    tiempo se usa; cuando solo trae el si/no --versiones viejas del worker--
    se usa la hora actual, que es una aproximacion honesta: el chat se acaba
    de fijar.

    Con el silencio pasa lo mismo, y ahi hay una trampa: WhatsApp usa 0 para
    "silenciado para siempre". Colapsar 0 con "sin silenciar" haria que un
    chat silenciado indefinidamente apareciera como normal.

    ``locked`` NO esta aqui: Baileys no procesa ``lockChatAction``, asi que no
    existe el evento en vivo. Los chats restringidos solo los sabe el
    historial.
    """
    valores: dict[str, Any] = {}
    if archived is not None:
        valores["archived"] = bool(archived)
    if pinned is not None:
        valores["pinned_at"] = _marca_de_fijado(pinned)
    if muted is not None:
        valores["mute_until"] = _marca_de_silencio(muted)
    if not valores:
        return False

    with database.transaction() as session:
        filas = session.execute(
            update(Chat)
            .where(Chat.jid == jid, Chat.whatsapp_account_id == whatsapp_account_id)
            .values(**valores)
        ).rowcount
    return bool(filas)


def _marca_de_fijado(pinned: Any) -> int | None:
    """La marca de tiempo en que se fijo, o ``None`` si dejo de estarlo."""
    if pinned is False or pinned is None:
        return None
    if pinned is True:
        return int(time.time())
    try:
        return int(pinned) or int(time.time())
    except (TypeError, ValueError):
        return int(time.time())


def _marca_de_silencio(muted: Any) -> int | None:
    """Hasta cuando esta silenciado. ``None`` es "no lo esta"; 0 es "siempre"."""
    if muted is False or muted is None:
        return None
    if muted is True:
        # Sin fecha de fin, "silenciado" solo puede significar indefinido.
        return 0
    try:
        return int(muted)
    except (TypeError, ValueError):
        return 0


def upsert_contact(
    session: Session,
    *,
    whatsapp_account_id: Any = None,
    jid: str,
    lid: str | None = None,
    phone_number: str | None = None,
    display_name: str | None = None,
    push_name: str | None = None,
    business_name: str | None = None,
    raw_metadata: dict[str, Any] | None = None,
) -> int:
    """Inserta o actualiza un contacto sin degradar datos ya conocidos."""
    values: dict[str, Any] = {"jid": jid}
    for column, value in (
        ("lid", lid),
        ("phone_number", phone_number),
        ("display_name", display_name),
        ("push_name", push_name),
        ("business_name", business_name),
        ("raw_metadata", raw_metadata),
    ):
        if value is not None:
            values[column] = value

    # DE QUIEN es este contacto. La agenda es privada: el nombre que una
    # persona le pone a un numero no puede aparecer en la de otra.
    #
    # Se acepta ya, aunque la columna todavia no exista en el esquema: asi los
    # llamantes se adaptan ahora y el dia que la migracion la anada empieza a
    # guardarse sola, sin volver a tocar ni una llamada.
    from app.services.account_scope import (
        cuenta_unica,
        destino_de_conflicto,
        tiene_columna,
    )

    if whatsapp_account_id is None:
        whatsapp_account_id = cuenta_unica(session)
    if whatsapp_account_id is None and tiene_columna(Contact, "whatsapp_account_id"):
        raise ValueError(
            "upsert_contact necesita whatsapp_account_id: la agenda es privada "
            "de cada cuenta"
        )

    if whatsapp_account_id is not None and tiene_columna(
        Contact, "whatsapp_account_id"
    ):
        values["whatsapp_account_id"] = whatsapp_account_id

    conflicto = destino_de_conflicto(Contact, "jid")
    alcance = [Contact.jid == jid]
    if whatsapp_account_id is not None and tiene_columna(
        Contact, "whatsapp_account_id"
    ):
        alcance.append(Contact.whatsapp_account_id == whatsapp_account_id)

    stmt = insert(Contact).values(**values)
    updates = {
        column: func.coalesce(stmt.excluded[column], Contact.__table__.c[column])
        for column in values
        if column not in ("jid", "whatsapp_account_id")
    }
    if not updates:
        stmt = stmt.on_conflict_do_nothing(index_elements=conflicto)
        session.execute(stmt)
        return session.execute(select(Contact.id).where(*alcance)).scalar_one()

    stmt = stmt.on_conflict_do_update(
        index_elements=conflicto, set_=updates
    ).returning(Contact.id)
    return session.execute(stmt).scalar_one()


# ---------------------------------------------------------------------------
# Mensajes
# ---------------------------------------------------------------------------


@dataclass
class IncomingMessage:
    """Mensaje normalizado, listo para persistir.

    ``whatsapp_message_id`` debe ser el ID REAL o ``None``. Nunca un
    identificador fabricado: para eso esta ``synthetic_identifier``.
    """

    chat_jid: str
    timestamp: int
    source: str
    whatsapp_message_id: str | None = None
    synthetic_identifier: str | None = None
    sender_jid: str | None = None
    sender_lid: str | None = None
    message_type: str = "unknown"
    text: str | None = None
    from_me: bool = False
    raw_metadata: dict[str, Any] | None = None
    raw_proto: bytes | None = None


# Cuantas filas por INSERT. `Message` tiene 15 columnas por fila; PostgreSQL
# limita una sentencia a 65535 parametros en total (65535 // 15 = 4369). Se
# midio en produccion: un History Sync de un enlace fresco trajo un lote que
# lo supero, el INSERT entero fallo --
# "number of parameters must be between 0 and 65535"-- y el bootstrap COMPLETO
# se perdio sin persistir un solo mensaje, silenciosamente hasta el log. El
# tope se deja con margen de sobra, no pegado al limite.
MENSAJES_POR_TANDA = 1000


def _en_tandas(items: Sequence[Any], tamano: int) -> Iterable[Sequence[Any]]:
    """Trocea una secuencia en listas de como mucho `tamano` elementos."""
    for inicio in range(0, len(items), tamano):
        yield items[inicio : inicio + tamano]


def bulk_upsert_messages(
    session: Session, chat_ids: dict[str, int], messages: Sequence[IncomingMessage]
) -> int:
    """Inserta un lote deduplicando por ``(chat_id, whatsapp_message_id)``.

    Por ``chat_id`` y no por ``chat_jid``: un mensaje de grupo lleva el MISMO
    identificador para todos los que lo reciben, asi que deduplicando por el
    jid el que le llega a la segunda cuenta se descartaria como duplicado del
    de la primera -- en silencio, sin error, y sin forma de notarlo hasta que
    alguien echa de menos una conversacion entera. `chat_id` ya es de una sola
    cuenta, asi que la deduplicacion queda acotada sola.

    Devuelve cuantas filas se insertaron de verdad (las que ya existian no
    cuentan). Un unico INSERT para todo el lote: sin commit por mensaje.

    Los mensajes SIN ID real no pueden deduplicarse por indice, asi que se
    insertan aparte y siempre crean fila. Es deliberado: inventar una clave
    para ellos falsearia la identidad del mensaje.
    """
    if not messages:
        return 0

    with_id = [m for m in messages if m.whatsapp_message_id]
    without_id = [m for m in messages if not m.whatsapp_message_id]
    inserted = 0

    from app.services.account_scope import destino_de_dedupe_de_mensaje

    index_elements = destino_de_dedupe_de_mensaje()

    if with_id:
        for chunk in _en_tandas(with_id, MENSAJES_POR_TANDA):
            rows = [_row(m, chat_ids[m.chat_jid]) for m in chunk]
            stmt = insert(Message).values(rows)
            # El primer origen que trajo el mensaje se conserva; solo se
            # rellenan los huecos que aquella vez quedaron vacios.
            # Se deduplica por CONVERSACION, no por identificador de WhatsApp
            # global. Un mensaje de grupo lleva el mismo id para todos los que
            # lo reciben, asi que por `chat_jid` el que le llega a la segunda
            # persona se descartaria como duplicado del de la primera -- sin
            # error y sin aviso. `chat_id` apunta a un chat que ya tiene
            # cuenta.
            stmt = stmt.on_conflict_do_update(
                index_elements=index_elements,
                index_where=Message.whatsapp_message_id.is_not(None),
                set_={
                    "text": func.coalesce(Message.__table__.c.text, stmt.excluded.text),
                    "raw_proto": func.coalesce(
                        Message.__table__.c.raw_proto, stmt.excluded.raw_proto
                    ),
                    "raw_metadata": func.coalesce(
                        Message.__table__.c.raw_metadata, stmt.excluded.raw_metadata
                    ),
                    "sender_lid": func.coalesce(
                        Message.__table__.c.sender_lid, stmt.excluded.sender_lid
                    ),
                },
            ).returning(literal_column("(xmax = 0)").label("was_inserted"))
            # xmax = 0 distingue en PostgreSQL una fila realmente INSERTADA de
            # una que entro por la rama DO UPDATE del conflicto. Es el idiom
            # estandar: comparar created_at con updated_at no sirve porque el
            # onupdate de SQLAlchemy no se aplica dentro de ON CONFLICT DO
            # UPDATE.
            inserted += sum(
                1 for (was_inserted,) in session.execute(stmt) if was_inserted
            )

    if without_id:
        for chunk in _en_tandas(without_id, MENSAJES_POR_TANDA):
            rows = [_row(m, chat_ids[m.chat_jid]) for m in chunk]
            session.execute(insert(Message).values(rows))
            inserted += len(rows)

    return inserted


def _row(message: IncomingMessage, chat_id: int) -> dict[str, Any]:
    return {
        "chat_id": chat_id,
        "chat_jid": message.chat_jid,
        "whatsapp_message_id": message.whatsapp_message_id,
        "synthetic_identifier": message.synthetic_identifier,
        "sender_jid": message.sender_jid,
        "sender_lid": message.sender_lid,
        "message_type": message.message_type,
        "text": message.text,
        "timestamp": message.timestamp,
        "from_me": message.from_me,
        "source": message.source,
        "raw_metadata": message.raw_metadata,
        "raw_proto": message.raw_proto,
    }


def count_messages(
    session: Session, chat_jid: str | None = None, *, chat_id: int | None = None
) -> int:
    """Cuantos mensajes hay. Con ``chat_id`` se cuenta UNA conversacion.

    ``chat_jid`` sigue admitiendose porque hay llamadas que solo lo tienen,
    pero cuenta los de TODAS las cuentas que hablen con ese contacto: para un
    recuento por conversacion hay que pasar ``chat_id``.
    """
    stmt = select(func.count()).select_from(Message)
    if chat_id is not None:
        stmt = stmt.where(Message.chat_id == chat_id)
    elif chat_jid is not None:
        stmt = stmt.where(Message.chat_jid == chat_jid)
    return session.execute(stmt).scalar_one()


# ---------------------------------------------------------------------------
# Paginacion para la GUI
# ---------------------------------------------------------------------------

# Columnas que la GUI necesita.
#
# ``raw_proto`` queda fuera A PROPOSITO: es la columna mas pesada (kilobytes
# por fila) y traerla para 200 burbujas multiplicaria por diez el coste de
# abrir un chat. ``raw_metadata`` si viene, porque es JSONB pequeno y lleva el
# ``stub_type`` con el que se rotula un evento de sistema; el clasificador
# sabe trabajar solo con eso.
#: Lo que se lee para pintar una pagina de conversacion.
#:
#: ``text`` sigue aqui, pero como PREVIA y respaldo: el contenido completo
#: vive en el segmento. Las tres ultimas columnas son las que permiten ir a
#: buscarlo sin recorrer el archivo entero.
_PAGE_COLUMNS = (
    Message.id,
    Message.chat_id,
    Message.whatsapp_message_id,
    Message.sender_jid,
    Message.sender_lid,
    Message.message_type,
    Message.text,
    Message.timestamp,
    Message.from_me,
    Message.raw_metadata,
    Message.segment_id,
    Message.segment_index,
    Message.storage_status,
)


def get_recent_messages(session: Session, chat_id: int, limit: int = 200) -> list[Any]:
    """Ultimos ``limit`` mensajes del chat, en orden cronologico ascendente."""
    stmt = (
        select(*_PAGE_COLUMNS)
        .where(Message.chat_id == chat_id)
        .order_by(Message.timestamp.desc(), Message.id.desc())
        .limit(limit)
    )
    return list(reversed(session.execute(stmt).all()))


def get_messages_before(
    session: Session, chat_id: int, before_timestamp: int, before_id: int, limit: int = 200
) -> list[Any]:
    """Pagina anterior, para el scroll hacia arriba.

    Se pagina por ``(timestamp, id)`` y no por OFFSET: con cientos de miles de
    filas el OFFSET degrada, y ademas la clave compuesta desempata los
    mensajes que comparten timestamp (habitual en History Sync).

    IMPORTANTE: esto es paginacion VISUAL sobre lo que YA esta en PostgreSQL.
    No dispara ninguna peticion a WhatsApp.
    """
    stmt = (
        select(*_PAGE_COLUMNS)
        .where(
            Message.chat_id == chat_id,
            (Message.timestamp < before_timestamp)
            | ((Message.timestamp == before_timestamp) & (Message.id < before_id)),
        )
        .order_by(Message.timestamp.desc(), Message.id.desc())
        .limit(limit)
    )
    return list(reversed(session.execute(stmt).all()))


def get_messages_after(
    session: Session, chat_id: int, after_timestamp: int, after_id: int, limit: int = 200
) -> list[Any]:
    """Lo que ha entrado DESPUES de un punto conocido. Para reconciliar.

    El transporte en tiempo real (SSE) puede perder eventos: una reconexion,
    un proxy que corta, el portatil que se suspende. PostgreSQL es la fuente
    de verdad, no el flujo de eventos, asi que el frontend tiene que poder
    preguntar "que me he perdido desde este mensaje" y ponerse al dia sin
    recargar la conversacion entera.

    Misma clave compuesta que :func:`get_messages_before`, en el otro sentido,
    y por el mismo motivo: el OFFSET degrada y el ``timestamp`` solo no
    desempata.
    """
    stmt = (
        select(*_PAGE_COLUMNS)
        .where(
            Message.chat_id == chat_id,
            (Message.timestamp > after_timestamp)
            | ((Message.timestamp == after_timestamp) & (Message.id > after_id)),
        )
        .order_by(Message.timestamp.asc(), Message.id.asc())
        .limit(limit)
    )
    return list(session.execute(stmt).all())




@dataclass
class ChatSummary:
    """Fila del sidebar, con el nombre ya resuelto."""

    id: int
    jid: str
    display_name: str
    chat_type: str
    last_message: str | None
    last_message_timestamp: int | None
    message_count: int
    # Los que NO son avisos del sistema. Es lo que decide si esta
    # conversacion se lista: WhatsApp reparte "este chat pasa a estar cifrado"
    # a contactos con los que nunca se ha hablado, y eso no es un chat.
    real_message_count: int = 0
    # El estado del historial viaja CON la fila del sidebar. Sin el, el
    # frontend no puede distinguir "no se pudo interpretar el mensaje" de
    # "todavia no hay una referencia para pedir el historial", y acaba
    # pintando "Mensaje no compatible" sobre chats que solo esperan semilla.
    history_status: str | None = None
    # El estado de la conversacion. Viaja con la fila porque el sidebar lo
    # pinta --el altavoz tachado, la chincheta-- y porque el orden de la lista
    # normal depende de `pinned_at`.
    archived: bool = False
    locked: bool = False
    pinned_at: int | None = None
    mute_until: int | None = None


def display_name_for(
    jid: str, *candidates: str | None, phone_jid: str | None = None
) -> str:
    """Primer nombre util. Nunca un LID crudo si hay algo mejor.

    Prioridad: nombre de la conversacion, metadata del contacto, pushname y
    -- solo para los chats identificados por LID -- el telefono que el mapa de
    contactos ya asocia a ese LID. Nunca se inventa un nombre ni se convierte
    un LID en telefono: ``phone_jid`` viene del contacto, no de un calculo.

    Cuando no hay NADA se dice que no hay nombre, en vez de ensenar el
    identificador interno. ``21935119425699 (LID)`` no le dice nada a nadie:
    no es un telefono, no es un nombre, y ocupa el sitio donde deberia estar
    la persona.
    """
    for candidate in candidates:
        if candidate and candidate.strip():
            return candidate.strip()

    user = jid.split("@")[0]
    if jid.endswith("@lid"):
        # El telefono SI es legible, y es la misma persona. Solo se usa si el
        # contacto lo tiene guardado; no se deduce del LID.
        if phone_jid and phone_jid.endswith("@s.whatsapp.net"):
            numero = phone_jid.split("@")[0].split(":")[0]
            if numero.isdigit():
                return f"+{numero}"
        # SIN MAPEAR, que no es lo mismo que sin nombre.
        #
        # "Contacto sin nombre" decia que la persona no tiene nombre guardado.
        # Aqui el problema es otro: no sabemos ni su numero, porque el par
        # LID<->PN todavia no ha llegado. Decirlo permite distinguir "esta
        # persona no esta en tu agenda" de "falta un dato que si va a llegar".
        return "Desconocido (lid)"
    if jid.endswith("@g.us"):
        return "Grupo sin nombre"
    if user.isdigit():
        return f"+{user}"
    return user or "Contacto sin nombre"


def list_chat_summaries(
    session: Session,
    *,
    search: str | None = None,
    limit: int = 500,
    accounts: list | None = None,
    solo_con_mensajes: bool = False,
    vista: str = "normal",
) -> list[ChatSummary]:
    """Chats para el sidebar con nombre resuelto y numero de mensajes.

    ``vista`` elige QUE seccion se pide, como en WhatsApp Web:

    ``normal``
        Lo que se ve al abrir. Deja fuera los archivados y los restringidos:
        el sentido de las dos cosas es justamente no aparecer aqui.
    ``archivados``
        Los archivados que no esten ademas restringidos. Un chat restringido
        y archivado pertenece a los restringidos, que es la seccion mas
        cerrada de las dos.
    ``restringidos``
        Los "chats bloqueados". Se listan esten archivados o no.

    Un valor que no sea ninguno de los tres se trata como ``normal``: es lo
    que llega de la URL, y un parametro mal escrito no puede acabar
    ensenando lo que estaba escondido.

    ``solo_con_mensajes`` deja fuera las conversaciones donde no se ha escrito
    nada: las que solo tienen avisos del sistema y las que aun no se han
    excavado. NO borra nada -- siguen en la base y en Drive-- y en cuanto una
    de ellas reciba un mensaje real aparece sola.

    Se resuelve en una sola consulta con LEFT JOIN a contactos y un conteo
    agregado: hacerlo por chat seria N+1 y con cientos de chats se notaria.

    ``accounts`` acota a las cuentas de WhatsApp de un usuario. Es obligatorio
    en la API: sin el, un usuario veria los chats de todos. Se deja opcional
    para el mantenimiento interno, que si trabaja sobre la base entera.
    """
    # Dos conteos, y la diferencia importa.
    #
    # `total` es lo que se ha guardado, y es lo honesto de ensenar: el
    # producto es una copia de seguridad. `reales` deja fuera los mensajes de
    # tipo `system` --"este chat pasa a estar cifrado", cambios de codigo de
    # seguridad-- que WhatsApp reparte por conversaciones en las que nunca se
    # ha escrito nada.
    #
    # Medido sobre la base real: de 205 conversaciones, 164 no tenian NI UN
    # mensaje real y 141 tenian exactamente un aviso de cifrado. Las 41
    # restantes son las que el usuario reconoce como sus chats.
    message_counts = (
        select(
            Message.chat_id,
            func.count().label("total"),
            func.count()
            .filter(Message.message_type != "system")
            .label("reales"),
        )
        .group_by(Message.chat_id)
        .subquery()
    )
    # Dos vias de resolucion: por JID directo y, para los chats identificados
    # con @lid, por el mapeo que pywhats va aprendiendo (contacts.lid).
    stmt = (
        select(
            Chat.id,
            Chat.jid,
            Chat.name,
            Chat.chat_type,
            Chat.last_message,
            Chat.last_message_timestamp,
            Contact.display_name,
            Contact.push_name,
            # El JID de telefono del contacto. Para un chat @lid es la unica
            # forma legible de nombrarlo cuando no hay nombre guardado.
            Contact.jid,
            func.coalesce(message_counts.c.total, 0),
            ChatHistoryState.history_status,
            func.coalesce(message_counts.c.reales, 0),
            Chat.archived,
            Chat.locked,
            Chat.pinned_at,
            Chat.mute_until,
        )
        .outerjoin(Contact, (
                (Contact.jid == Chat.jid) | (Contact.lid == Chat.jid)
            )
            & (Contact.whatsapp_account_id == Chat.whatsapp_account_id))
        .outerjoin(message_counts, message_counts.c.chat_id == Chat.id)
        .outerjoin(ChatHistoryState, ChatHistoryState.chat_id == Chat.id)
        .order_by(
            # Los fijados van arriba, y entre ellos manda CUANDO se fijaron.
            # `nulls_last` es lo que deja abajo a los que no lo estan.
            Chat.pinned_at.desc().nulls_last(),
            Chat.last_message_timestamp.desc().nulls_last(),
            Chat.id.desc(),
        )
        .limit(limit)
    )

    if accounts is not None:
        # Lista vacia -> condicion imposible, no ausencia de filtro. Un filtro
        # que "no aplica" seria un filtro que deja verlo todo.
        stmt = stmt.where(
            Chat.whatsapp_account_id.in_(accounts) if accounts else Chat.id.is_(None)
        )

    # LO QUE NO ES UNA CONVERSACION, nunca se lista.
    #
    #   `0@s.whatsapp.net`  la cuenta de servicio de WhatsApp. No es una
    #                       persona: por ahi entran los avisos del sistema, y
    #                       en el panel salia como un contacto llamado "+0".
    #   `status@broadcast`  los estados. No son un chat.
    #   parte de usuario 0  o vacia: un identificador que no apunta a nadie.
    stmt = stmt.where(
        Chat.jid.notin_(["0@s.whatsapp.net", "status@broadcast"]),
        func.split_part(Chat.jid, "@", 1).notin_(["", "0"]),
    )

    # LA SECCION. Por defecto, la lista normal.
    if vista == "archivados":
        stmt = stmt.where(Chat.archived.is_(True), Chat.locked.is_(False))
    elif vista == "restringidos":
        stmt = stmt.where(Chat.locked.is_(True))
    else:
        stmt = stmt.where(Chat.archived.is_(False), Chat.locked.is_(False))

    if solo_con_mensajes:
        stmt = stmt.where(func.coalesce(message_counts.c.reales, 0) > 0)

    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(
            Chat.name.ilike(pattern)
            | Chat.jid.ilike(pattern)
            | Contact.display_name.ilike(pattern)
            | Contact.push_name.ilike(pattern)
            | Contact.lid.ilike(pattern)
        )

    summaries = []
    for row in session.execute(stmt):
        (
            chat_id,
            jid,
            name,
            chat_type,
            last_message,
            last_timestamp,
            contact_name,
            push_name,
            contact_jid,
            total,
            history_status,
            reales,
            archivado,
            restringido,
            fijado_en,
            silenciado_hasta,
        ) = row
        summaries.append(
            ChatSummary(
                id=chat_id,
                jid=jid,
                display_name=display_name_for(
                    jid, name, contact_name, push_name, phone_jid=contact_jid
                ),
                chat_type=chat_type,
                last_message=last_message,
                last_message_timestamp=last_timestamp,
                message_count=total,
                real_message_count=reales,
                history_status=history_status,
                archived=bool(archivado),
                locked=bool(restringido),
                pinned_at=fijado_en,
                mute_until=silenciado_hasta,
            )
        )
    return summaries


#: Identificadores que NUNCA son una conversacion.
#:
#: ``0@s.whatsapp.net`` es la cuenta de servicio de WhatsApp --por ahi entran
#: los avisos del propio sistema-- y en el panel salia como un contacto
#: llamado "+0". ``status@broadcast`` son los estados.
JIDS_QUE_NO_SON_CHAT = frozenset({"0@s.whatsapp.net", "status@broadcast"})


def se_lista(resumen: "ChatSummary") -> bool:
    """Si esta conversacion tiene sitio en el panel.

    LA MISMA REGLA PARA LOS DOS CAMINOS
    -----------------------------------
    Una fila del sidebar llega por dos sitios: el listado (``/chats``) y los
    avisos en vivo, que la mandan ya montada para no obligar a recargar.

    El listado filtraba y los avisos no, asi que durante una excavacion se
    colaban por la puerta de atras las conversaciones que el listado dejaba
    fuera -- y el usuario veia aparecer "+0" y chats con un solo aviso de
    cifrado, justo lo que el filtro existe para evitar. Con la regla aqui, los
    dos caminos deciden igual.
    """
    if resumen.jid in JIDS_QUE_NO_SON_CHAT:
        return False
    usuario = resumen.jid.split("@")[0]
    if usuario in ("", "0"):
        return False
    return resumen.real_message_count > 0


def chat_summary(session: Session, chat_id: int) -> ChatSummary | None:
    """Resumen de UN chat, para refrescar su fila del sidebar.

    Cuando llega un mensaje no hace falta reconstruir la lista entera: basta
    con volver a leer el chat afectado. Con miles de conversaciones, destruir
    y recrear todos los widgets por cada mensaje es lo que hace que una
    interfaz se sienta lenta.
    """
    fila = session.execute(
        select(
            Chat.id,
            Chat.jid,
            Chat.name,
            Chat.chat_type,
            Chat.last_message,
            Chat.last_message_timestamp,
            Contact.display_name,
            Contact.push_name,
            Contact.jid,
            # EL ESTADO VIAJA TAMBIEN AQUI, y no es opcional.
            #
            # Esta fila es la que manda el canal en vivo en `chat.updated`. Sin
            # estas columnas llegaria con `archived=False` y `locked=False` por
            # omision, y el panel colocaria en la lista normal un chat que el
            # usuario habia archivado --o, peor, uno RESTRINGIDO-- en cuanto
            # recibiera un mensaje. Destapar un chat restringido con solo
            # escribirle es exactamente lo que la funcion existe para impedir.
            Chat.archived,
            Chat.locked,
            Chat.pinned_at,
            Chat.mute_until,
        )
        .outerjoin(Contact, (
                (Contact.jid == Chat.jid) | (Contact.lid == Chat.jid)
            )
            & (Contact.whatsapp_account_id == Chat.whatsapp_account_id))
        .where(Chat.id == chat_id)
        .limit(1)
    ).first()
    if fila is None:
        return None

    total, reales = session.execute(
        select(
            func.count(),
            func.count().filter(Message.message_type != "system"),
        )
        .select_from(Message)
        .where(Message.chat_id == chat_id)
    ).one()
    (
        row_id, jid, name, chat_type, last_message, last_timestamp,
        contact_name, push_name, contact_jid,
        archivado, restringido, fijado_en, silenciado_hasta,
    ) = fila
    return ChatSummary(
        id=row_id,
        jid=jid,
        display_name=display_name_for(
            jid, name, contact_name, push_name, phone_jid=contact_jid
        ),
        chat_type=chat_type,
        last_message=last_message,
        last_message_timestamp=last_timestamp,
        message_count=total,
        real_message_count=reales,
        archived=bool(archivado),
        locked=bool(restringido),
        pinned_at=fijado_en,
        mute_until=silenciado_hasta,
    )




# ---------------------------------------------------------------------------
# Estado de historial
# ---------------------------------------------------------------------------


def get_or_create_history_state(
    session: Session, *, chat_id: int, chat_jid: str
) -> ChatHistoryState:
    """El estado de historial de ESA conversacion, creandolo si no existe.

    POR ``chat_id``, NUNCA por ``chat_jid``.
    ---------------------------------------
    El jid deja de ser unico en cuanto dos cuentas hablan con el mismo
    contacto: hay una conversacion por cuenta, cada una con su estado. Buscar
    por jid devolvia DOS filas y `scalar_one_or_none()` reventaba con
    "Multiple rows were found", tumbando la ingesta entera del blob.

    Se midio en cuanto existieron dos cuentas: todos los History Sync
    fallaban, y con ellos la excavacion y los mensajes en vivo.

    El ``chat_id`` ya viene, y es unico: apunta a un chat que tiene cuenta.
    """
    state = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_id == chat_id)
    ).scalar_one_or_none()
    if state is None:
        state = ChatHistoryState(chat_id=chat_id, chat_jid=chat_jid, history_status="pending")
        session.add(state)
        session.flush()
    return state


def refresh_history_state(
    session: Session, chat_jid: str, *, chat_id: int | None = None
) -> None:
    """Recalcula contadores y cursor del chat a partir de sus mensajes.

    ``chat_id`` no es un adorno: sin el, tanto la fila de estado como los
    contadores se buscan por ``chat_jid``, que se repite en cuanto dos cuentas
    hablan con el mismo contacto. El resultado eran dos cosas a la vez:
    `scalar_one_or_none()` reventando, y --cuando no reventaba-- un recuento
    que sumaba los mensajes de LAS DOS conversaciones.
    """
    if chat_id is None:
        # Ultimo recurso para las llamadas que aun no lo saben. Si el jid da
        # mas de un chat no se elige: se deja como estaba, que es mejor que
        # mezclar dos conversaciones en un contador.
        from app.services.account_scope import chat_id_de

        chat_id = chat_id_de(session, chat_jid)
        if chat_id is None:
            return

    state = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_id == chat_id)
    ).scalar_one_or_none()
    if state is None:
        return

    cursor = get_oldest_valid_history_cursor(session, chat_jid, chat_id=chat_id)
    newest = session.execute(
        select(func.max(Message.timestamp)).where(Message.chat_id == chat_id)
    ).scalar_one_or_none()

    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.id == state.id)
        .values(
            message_count=count_messages(session, chat_id=chat_id),
            oldest_message_id=cursor.message_id if cursor else None,
            oldest_message_timestamp=cursor.timestamp if cursor else None,
            newest_message_timestamp=newest,
        )
    )


# ---------------------------------------------------------------------------
# app_state
# ---------------------------------------------------------------------------


def refresh_chat_previews(session: Session, chat_jids: Iterable[str]) -> int:
    """Rellena ``last_message`` y ``last_message_timestamp`` de cada chat.

    Los blobs de History Sync traen ``Conversation.last_msg_timestamp`` vacio,
    asi que sin esto el sidebar no tendria ni vista previa ni criterio de
    orden: los chats saldrian en un orden arbitrario y todos con el mismo
    aspecto. El dato se deriva del mensaje mas reciente ya guardado.

    La previa se construye con :func:`app.core.previews.preview_for`, que sabe
    traducir un adjunto a "📷 Imagen" y un evento de sistema a "Llamada
    perdida". La version anterior concatenaba ``'[' || message_type || ']'``
    en SQL y por eso el chat propio mostraba ``[unknown]`` teniendo cuatro
    imagenes dentro.

    Escalabilidad: una sola consulta ``DISTINCT ON`` devuelve UN mensaje por
    chat, no la tabla entera. El coste crece con el numero de chats, no con
    el de mensajes.
    """
    wanted = list(chat_jids)
    if not wanted:
        return 0

    from app.core.previews import preview_for

    # Se trae tambien lo que el chat tiene guardado ahora mismo para poder
    # escribir SOLO lo que cambia: asi la operacion es idempotente y el
    # contador que devuelve significa "filas realmente modificadas".
    newest = session.execute(
        select(
            Message.chat_jid,
            Message.text,
            Message.message_type,
            Message.timestamp,
            Message.raw_proto,
            Message.raw_metadata,
            Chat.last_message,
            Chat.last_message_timestamp,
        )
        .join(Chat, Chat.jid == Message.chat_jid)
        .where(Message.chat_jid.in_(wanted))
        .distinct(Message.chat_jid)
        .order_by(Message.chat_jid, Message.timestamp.desc(), Message.id.desc())
    ).all()
    if not newest:
        return 0

    updates = []
    for (
        chat_jid, text, message_type, timestamp, raw_proto, metadata,
        previa_actual, ts_actual,
    ) in newest:
        previa = preview_for(message_type, text, raw_proto=raw_proto, metadata=metadata)
        if previa == previa_actual and timestamp == ts_actual:
            continue
        updates.append({"jid_": chat_jid, "preview": previa, "ts": timestamp})
    if not updates:
        return 0

    # Se actualiza la TABLA, no la entidad ORM: con una lista de parametros el
    # ORM intenta un "bulk update by primary key" y exige el id de cada fila,
    # que aqui no se tiene (se identifica por JID). A nivel de tabla es un
    # unico UPDATE parametrizado, que ademas es lo que interesa por coste.
    session.execute(
        update(Chat.__table__)
        .where(Chat.__table__.c.jid == bindparam("jid_"))
        .values(last_message=bindparam("preview"), last_message_timestamp=bindparam("ts")),
        updates,
    )
    return len(updates)


@dataclass(frozen=True)
class ChatStats:
    """Cifras REALES del chat, consultadas a PostgreSQL. Sin estimaciones."""

    total: int
    oldest_timestamp: int | None
    newest_timestamp: int | None


def get_chat_stats(session: Session, chat_id: int) -> ChatStats:
    """COUNT(*), mas antiguo y mas reciente en una sola pasada.

    Es la fuente para la cabecera: "N mensajes almacenados" tiene que salir de
    la base, no de cuantos widgets haya pintados.
    """
    row = session.execute(
        select(
            func.count(),
            func.min(Message.timestamp),
            func.max(Message.timestamp),
        ).where(Message.chat_id == chat_id)
    ).one()
    return ChatStats(total=row[0], oldest_timestamp=row[1], newest_timestamp=row[2])


def get_chat_message_count(session: Session, chat_id: int) -> int:
    return session.execute(
        select(func.count()).where(Message.chat_id == chat_id)
    ).scalar_one()


def media_for_messages(session: Session, message_ids: Iterable[int]) -> dict[int, Any]:
    """Adjuntos indexados por ``message_id``, para pintar una pagina.

    Se consulta en bloque para la pagina entera: hacerlo por burbuja seria
    N+1 y con 200 mensajes se notaria en cada scroll.
    """
    wanted = list(message_ids)
    if not wanted:
        return {}
    rows = session.execute(
        select(
            # El id hace falta para construir las URLs de la API
            # (/api/v1/media/<id>/file). La ventana Tkinter no lo usaba
            # porque abre el archivo por ruta local, pero el navegador no
            # puede hacer eso: solo conoce URLs.
            MediaFile.id,
            MediaFile.message_id,
            MediaFile.media_type,
            MediaFile.mime_type,
            MediaFile.file_name,
            MediaFile.file_size,
            MediaFile.duration_seconds,
            MediaFile.width,
            MediaFile.height,
            MediaFile.local_path,
            MediaFile.download_status,
        ).where(MediaFile.message_id.in_(wanted))
    ).all()
    return {row.message_id: row for row in rows}


def media_stats(session: Session, *, accounts: list | None = None) -> dict[str, int]:
    """Contadores reales de multimedia, consultados a PostgreSQL.

    ``accounts`` acota a las cuentas de WhatsApp de un usuario, y no es
    opcional en la practica: sin el, esto contaba los adjuntos de TODAS las
    cuentas de la maquina y el panel de una persona ensenaba cifras que
    incluian los de otra. Se llega por `chat_id`, que es de una sola cuenta.

    Sin ``accounts`` se devuelve vacio en vez de contarlo todo: quedarse sin
    cifras se ve y se arregla; ensenar las de otro, no.
    """
    if accounts is None:
        return {}
    if not accounts:
        return {}
    rows = session.execute(
        select(MediaFile.download_status, func.count())
        .join(Chat, Chat.id == MediaFile.chat_id)
        .where(Chat.whatsapp_account_id.in_(accounts))
        .group_by(MediaFile.download_status)
    ).all()
    return {status: total for status, total in rows}


def get_app_state(session: Session, key: str) -> Any | None:
    return session.execute(
        select(AppState.value).where(AppState.key == key)
    ).scalar_one_or_none()


def set_app_state(session: Session, key: str, value: Any) -> None:
    stmt = insert(AppState).values(key=key, value=value)
    session.execute(
        stmt.on_conflict_do_update(index_elements=[AppState.key], set_={"value": stmt.excluded.value})
    )




def history_state_for(session: Session, chat_jid: str) -> Any | None:
    """Estado historico de un chat. ``None`` si no tiene fila todavia."""
    return session.execute(
        select(
            ChatHistoryState.history_status,
            ChatHistoryState.oldest_message_id,
            ChatHistoryState.oldest_message_timestamp,
            ChatHistoryState.newest_message_timestamp,
            ChatHistoryState.message_count,
            ChatHistoryState.last_error,
            # Para que el panel pueda decir "reintento pendiente" con la hora
            # de verdad, en vez de suponerla.
            ChatHistoryState.attempt_count,
            ChatHistoryState.next_retry_at,
        ).where(ChatHistoryState.chat_jid == chat_jid)
    ).first()


def history_counters(session: Session, *, accounts: list | None = None) -> dict[str, int]:
    """Cuantos chats hay en cada situacion historica.

    Se agrupan por lo que SIGNIFICAN, no por el nombre interno del estado:
    el frontend necesita distinguir "terminado" de "no se pudo ni empezar",
    y agruparlo todo como "sincronizado" era justamente el problema.
    """
    from app.models import COMPLETE_STATUSES, SEEDLESS_STATUSES

    # ACOTADO A LAS CUENTAS DE QUIEN PREGUNTA.
    #
    # Sin esto se contaban los chats de TODAS las cuentas de la maquina, y el
    # panel de una persona ensenaba un total que incluia las conversaciones de
    # otra. Se llega por `chat_id`, que pertenece a una sola cuenta.
    if not accounts:
        return {
            "chats_total": 0,
            "chats_complete": 0,
            "chats_fetching": 0,
            "chats_pending": 0,
            "chats_waiting_seed": 0,
            "chats_no_cursor": 0,
            "chats_empty_confirmed": 0,
            "chats_seedless": 0,
        }
    crudos = dict(
        session.execute(
            select(ChatHistoryState.history_status, func.count())
            .join(Chat, Chat.id == ChatHistoryState.chat_id)
            .where(Chat.whatsapp_account_id.in_(accounts))
            .group_by(ChatHistoryState.history_status)
        ).all()
    )
    return {
        "chats_total": sum(crudos.values()),
        "chats_complete": sum(crudos.get(e, 0) for e in COMPLETE_STATUSES),
        "chats_fetching": crudos.get("fetching", 0),
        "chats_pending": crudos.get("pending", 0),
        "chats_waiting_seed": crudos.get("waiting_seed", 0),
        "chats_no_cursor": crudos.get("no_valid_cursor", 0),
        "chats_empty_confirmed": crudos.get("empty_confirmed", 0),
        "chats_seedless": sum(crudos.get(e, 0) for e in SEEDLESS_STATUSES),
        "por_estado": crudos,
    }
