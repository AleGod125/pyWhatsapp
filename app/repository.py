"""Operaciones de persistencia sobre PostgreSQL.

Todo lo que escribe en la base pasa por aqui. Dos reglas de rendimiento que se
aplican en todo el modulo:

* Nunca un COMMIT por mensaje. Los lotes se insertan con un unico INSERT ...
  ON CONFLICT y el llamante controla la transaccion.
* Las consultas de la GUI no traen ``raw_proto``: es la columna mas pesada y
  no se necesita para pintar una conversacion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from sqlalchemy import func, literal_column, select, update
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


def get_oldest_valid_history_cursor(session: Session, chat_jid: str) -> HistoryCursor | None:
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
    """
    stmt = (
        select(Message.whatsapp_message_id, Message.timestamp)
        .where(
            Message.chat_jid == chat_jid,
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
) -> int:
    """Inserta o actualiza un chat y devuelve su ``id``.

    Los campos que llegan como ``None`` NO pisan lo que ya hubiera: History
    Sync y los mensajes live traen informacion parcial distinta y un update
    ciego borraria el nombre que ya se habia resuelto.
    """
    values: dict[str, Any] = {"jid": jid, "chat_type": chat_type or "unknown"}
    for column, value in (
        ("name", name),
        ("last_message", last_message),
        ("last_message_timestamp", last_message_timestamp),
        ("raw_metadata", raw_metadata),
    ):
        if value is not None:
            values[column] = value

    stmt = insert(Chat).values(**values)
    updates = {
        column: func.coalesce(stmt.excluded[column], Chat.__table__.c[column])
        for column in ("name", "last_message", "raw_metadata")
        if column in values
    }
    # El chat_type solo mejora: 'unknown' nunca pisa un tipo ya conocido.
    if chat_type and chat_type != "unknown":
        updates["chat_type"] = stmt.excluded.chat_type
    # El timestamp del ultimo mensaje solo avanza hacia delante.
    if last_message_timestamp is not None:
        updates["last_message_timestamp"] = func.greatest(
            func.coalesce(Chat.__table__.c.last_message_timestamp, 0),
            stmt.excluded.last_message_timestamp,
        )

    stmt = stmt.on_conflict_do_update(index_elements=[Chat.jid], set_=updates).returning(
        Chat.id
    )
    return session.execute(stmt).scalar_one()


def upsert_contact(
    session: Session,
    *,
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

    stmt = insert(Contact).values(**values)
    updates = {
        column: func.coalesce(stmt.excluded[column], Contact.__table__.c[column])
        for column in values
        if column != "jid"
    }
    if not updates:
        stmt = stmt.on_conflict_do_nothing(index_elements=[Contact.jid])
        session.execute(stmt)
        return session.execute(select(Contact.id).where(Contact.jid == jid)).scalar_one()

    stmt = stmt.on_conflict_do_update(index_elements=[Contact.jid], set_=updates).returning(
        Contact.id
    )
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


def bulk_upsert_messages(
    session: Session, chat_ids: dict[str, int], messages: Sequence[IncomingMessage]
) -> int:
    """Inserta un lote deduplicando por ``(chat_jid, whatsapp_message_id)``.

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

    if with_id:
        rows = [_row(m, chat_ids[m.chat_jid]) for m in with_id]
        stmt = insert(Message).values(rows)
        # El primer origen que trajo el mensaje se conserva; solo se rellenan
        # los huecos que aquella vez quedaron vacios.
        stmt = stmt.on_conflict_do_update(
            index_elements=[Message.chat_jid, Message.whatsapp_message_id],
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
        # xmax = 0 distingue en PostgreSQL una fila realmente INSERTADA de una
        # que entro por la rama DO UPDATE del conflicto. Es el idiom estandar:
        # comparar created_at con updated_at no sirve porque el onupdate de
        # SQLAlchemy no se aplica dentro de ON CONFLICT DO UPDATE.
        inserted += sum(1 for (was_inserted,) in session.execute(stmt) if was_inserted)

    if without_id:
        rows = [_row(m, chat_ids[m.chat_jid]) for m in without_id]
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


def count_messages(session: Session, chat_jid: str | None = None) -> int:
    stmt = select(func.count()).select_from(Message)
    if chat_jid is not None:
        stmt = stmt.where(Message.chat_jid == chat_jid)
    return session.execute(stmt).scalar_one()


# ---------------------------------------------------------------------------
# Paginacion para la GUI
# ---------------------------------------------------------------------------

# Columnas que la GUI necesita. raw_proto queda fuera a proposito.
_PAGE_COLUMNS = (
    Message.id,
    Message.whatsapp_message_id,
    Message.sender_jid,
    Message.message_type,
    Message.text,
    Message.timestamp,
    Message.from_me,
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


def list_chats(session: Session, limit: int = 500) -> list[Any]:
    """Chats para el sidebar, los mas recientes primero."""
    stmt = (
        select(
            Chat.id,
            Chat.jid,
            Chat.name,
            Chat.chat_type,
            Chat.last_message,
            Chat.last_message_timestamp,
        )
        .order_by(Chat.last_message_timestamp.desc().nulls_last())
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


def display_name_for(jid: str, *candidates: str | None) -> str:
    """Primer nombre util, con el JID como ultimo recurso.

    Prioridad (seccion 36): nombre de la conversacion, metadata del contacto,
    pushname, asunto del grupo y, si no hay nada, el identificador. Nunca se
    inventa un nombre ni se convierte un LID en telefono.
    """
    for candidate in candidates:
        if candidate and candidate.strip():
            return candidate.strip()
    user = jid.split("@")[0]
    if jid.endswith("@lid"):
        # Un LID no es un telefono: se marca para no dar a entender lo que no es.
        return f"{user} (LID)"
    if jid.endswith("@g.us"):
        return f"Grupo {user.split('-')[0]}"
    return user


def list_chat_summaries(
    session: Session, *, search: str | None = None, limit: int = 500
) -> list[ChatSummary]:
    """Chats para el sidebar con nombre resuelto y numero de mensajes.

    Se resuelve en una sola consulta con LEFT JOIN a contactos y un conteo
    agregado: hacerlo por chat seria N+1 y con cientos de chats se notaria.
    """
    message_counts = (
        select(Message.chat_id, func.count().label("total"))
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
            func.coalesce(message_counts.c.total, 0),
        )
        .outerjoin(Contact, (Contact.jid == Chat.jid) | (Contact.lid == Chat.jid))
        .outerjoin(message_counts, message_counts.c.chat_id == Chat.id)
        .order_by(Chat.last_message_timestamp.desc().nulls_last(), Chat.id.desc())
        .limit(limit)
    )

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
            total,
        ) = row
        summaries.append(
            ChatSummary(
                id=chat_id,
                jid=jid,
                display_name=display_name_for(jid, name, contact_name, push_name),
                chat_type=chat_type,
                last_message=last_message,
                last_message_timestamp=last_timestamp,
                message_count=total,
            )
        )
    return summaries


def sender_names(session: Session, jids: Iterable[str]) -> dict[str, str]:
    """Nombre a mostrar de cada emisor, para las burbujas de un grupo."""
    wanted = [jid for jid in set(jids) if jid]
    if not wanted:
        return {}
    rows = session.execute(
        select(Contact.jid, Contact.display_name, Contact.push_name).where(
            Contact.jid.in_(wanted)
        )
    ).all()
    resolved = {
        jid: display_name_for(jid, display_name, push_name)
        for jid, display_name, push_name in rows
    }
    for jid in wanted:
        resolved.setdefault(jid, display_name_for(jid))
    return resolved


# ---------------------------------------------------------------------------
# Estado de historial
# ---------------------------------------------------------------------------


def get_or_create_history_state(
    session: Session, *, chat_id: int, chat_jid: str
) -> ChatHistoryState:
    state = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == chat_jid)
    ).scalar_one_or_none()
    if state is None:
        state = ChatHistoryState(chat_id=chat_id, chat_jid=chat_jid, history_status="pending")
        session.add(state)
        session.flush()
    return state


def refresh_history_state(session: Session, chat_jid: str) -> None:
    """Recalcula contadores y cursor del chat a partir de los mensajes."""
    state = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == chat_jid)
    ).scalar_one_or_none()
    if state is None:
        return

    cursor = get_oldest_valid_history_cursor(session, chat_jid)
    newest = session.execute(
        select(func.max(Message.timestamp)).where(Message.chat_jid == chat_jid)
    ).scalar_one_or_none()

    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.id == state.id)
        .values(
            message_count=count_messages(session, chat_jid),
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
    """
    wanted = list(chat_jids)
    if not wanted:
        return 0

    # Un solo UPDATE ... FROM con DISTINCT ON: una pasada por chat en lugar de
    # una consulta por chat.
    newest = (
        select(
            Message.chat_jid,
            Message.text,
            Message.message_type,
            Message.timestamp,
        )
        .where(Message.chat_jid.in_(wanted))
        .distinct(Message.chat_jid)
        .order_by(Message.chat_jid, Message.timestamp.desc(), Message.id.desc())
        .subquery()
    )

    updated = session.execute(
        update(Chat)
        .where(Chat.jid == newest.c.chat_jid)
        .values(
            # Si el mensaje no tiene texto (imagen, audio...), se guarda una
            # etiqueta con su tipo para que la vista previa diga algo util.
            last_message=func.coalesce(newest.c.text, "[" + newest.c.message_type + "]"),
            last_message_timestamp=newest.c.timestamp,
        )
    ).rowcount
    return updated or 0


@dataclass(frozen=True)
class ChatStats:
    """Cifras REALES del chat, consultadas a PostgreSQL. Sin estimaciones."""

    total: int
    oldest_timestamp: int | None
    newest_timestamp: int | None


def get_chat_stats(session: Session, chat_id: int) -> ChatStats:
    """COUNT(*), mas antiguo y mas reciente en una sola pasada.

    Es la fuente para la cabecera: "N mensajes almacenados" tiene que salir de
    la base, no de cuantos widgets haya pintados (seccion 17).
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


def media_stats(session: Session) -> dict[str, int]:
    """Contadores reales de multimedia, consultados a PostgreSQL."""
    rows = session.execute(
        select(MediaFile.download_status, func.count()).group_by(MediaFile.download_status)
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


def chat_ids_by_jid(session: Session, jids: Iterable[str]) -> dict[str, int]:
    """Mapa jid -> id para resolver los FK de un lote de una sola consulta."""
    wanted = list(jids)
    if not wanted:
        return {}
    rows = session.execute(select(Chat.jid, Chat.id).where(Chat.jid.in_(wanted))).all()
    return {jid: chat_id for jid, chat_id in rows}
