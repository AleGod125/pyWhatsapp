"""Ingesta de History Sync a PostgreSQL.

Convierte un :class:`FullHistorySync` (conversaciones + WebMessageInfo en
crudo) en filas, en LOTES y dentro de una sola transaccion. Nunca un commit
por mensaje.

Orden de las operaciones, que importa por las claves ajenas:

    1. upsert de los chats  -> se obtienen sus id
    2. upsert de mensajes en lote (ON CONFLICT deduplica)
    3. filas de media pendientes
    4. recalculo del estado de historial por chat
"""

from __future__ import annotations

from typing import Any

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.services import repository as repo
from app.wa.historial import FullHistorySync
from app.core.logging_setup import get_logger
from app.core.message_classifier import classify_parsed, is_internal
from app.core.message_parser import ParsedMessage, classify_chat, parse_web_message_info
from app.models import MediaFile, Message
from app.services.repository import IncomingMessage

log = get_logger("SYNC")


@dataclass
class IngestResult:
    """Que ha entrado de verdad. Los contadores son reales, no estimados."""

    conversations: int = 0
    #: Los JID que NO existian antes de esta ingesta.
    #:
    #: La pantalla los usa para ir soltando las conversaciones una a una segun
    #: se extraen, en vez de quedarse quieta hasta que el usuario recarga.
    #: Se calculan ANTES de tocar nada: despues del upsert ya no hay forma de
    #: distinguir "recien creado" de "ya estaba".
    new_chat_jids: list[str] = field(default_factory=list)
    messages_seen: int = 0
    messages_inserted: int = 0
    messages_unparsable: int = 0
    media_detected: int = 0
    pushnames: int = 0
    protocol_filtered: int = 0

    def __str__(self) -> str:
        return (
            f"conversaciones={self.conversations} vistos={self.messages_seen} "
            f"nuevos={self.messages_inserted} media={self.media_detected} "
            f"protocolo={self.protocol_filtered} ilegibles={self.messages_unparsable}"
        )


def _jids_existentes(
    session: Session, jids: list[str], whatsapp_account_id: Any
) -> set[str]:
    """Cuales de esos JID ya tienen chat en ESTA cuenta.

    En una sola consulta: preguntarlo chat a chat serian decenas de viajes en
    cada bloque de historial.
    """
    from app.models import Chat

    if not jids:
        return set()
    consulta = select(Chat.jid).where(Chat.jid.in_(jids))
    if whatsapp_account_id is not None:
        consulta = consulta.where(Chat.whatsapp_account_id == whatsapp_account_id)
    try:
        return {j for j in session.execute(consulta).scalars() if j}
    except Exception:  # noqa: BLE001 - no saberlo solo cuesta un aviso de mas
        return set()


def ingest_history_sync(
    session: Session,
    sync: FullHistorySync,
    *,
    own_jid: str | None = None,
    whatsapp_account_id: "Any" = None,
) -> IngestResult:
    """Persiste un blob de History Sync completo.

    :param own_jid: JID propio, para rellenar el emisor de los mensajes con
        ``fromMe``. Si no se conoce se deja a NULL en vez de inventarlo.
    :param whatsapp_account_id: de quien son estos chats. Sin el quedan sin
        dueno y el filtro de propiedad los excluye: existen en la base y no
        los ve nadie.
    """
    result = IngestResult()

    # -- 0. Que habia ANTES -------------------------------------------------
    #
    # `upsert_chat` no distingue insercion de actualizacion, y despues de
    # llamarlo ya es tarde para saberlo. Se pregunta antes, en UNA consulta.
    conocidos = _jids_existentes(
        session,
        [c.jid for c in sync.conversations if c.jid],
        whatsapp_account_id,
    )

    # -- 1. Chats -----------------------------------------------------------
    chat_ids: dict[str, int] = {}
    for conversation in sync.conversations:
        if not conversation.jid:
            continue
        chat_ids[conversation.jid] = repo.upsert_chat(
            session,
            jid=conversation.jid,
            name=conversation.name,
            chat_type=classify_chat(conversation.jid),
            last_message_timestamp=conversation.last_message_timestamp,
            whatsapp_account_id=whatsapp_account_id,
            # El estado llega en CADA `proto.Conversation` y hasta ahora se
            # tiraba. Es lo unico que permite separar los archivados y saber
            # cuales son los "chats restringidos": `lockChatAction` es la
            # unica accion de app-state que Baileys no procesa, asi que el
            # historial es la unica fuente que hay para `locked`.
            archived=conversation.archived,
            locked=conversation.locked,
            pinned_at=conversation.pinned_at,
            mute_until=conversation.mute_until,
        )
        result.conversations += 1
        if conversation.jid not in conocidos:
            result.new_chat_jids.append(conversation.jid)

    # -- 2. Mensajes --------------------------------------------------------
    parsed: list[ParsedMessage] = []
    for conversation in sync.conversations:
        for raw, _order in conversation.messages:
            result.messages_seen += 1
            message = parse_web_message_info(raw)
            if message is None:
                result.messages_unparsable += 1
                continue
            # El chat del mensaje manda sobre el de la conversacion: en los
            # blobs pueden no coincidir.
            if message.chat_jid not in chat_ids:
                # Un chat que solo aparece por un mensaje, sin fila de
                # conversacion. No trae nombre: se deja a `None` para que el
                # upsert no pise el que pudiera resolverse mas adelante.
                if message.chat_jid not in conocidos:
                    result.new_chat_jids.append(message.chat_jid)
                chat_ids[message.chat_jid] = repo.upsert_chat(
                    session,
                    jid=message.chat_jid,
                    chat_type=classify_chat(message.chat_jid),
                    whatsapp_account_id=whatsapp_account_id,
                )
            # Clasificacion central: lo interno se procesa pero no se guarda
            # como mensaje de conversacion.
            clase = classify_parsed(message)
            if is_internal(clase):
                result.protocol_filtered += 1
                continue
            parsed.append(message)

    source = source_for(sync.sync_type)
    incoming = [_to_incoming(message, own_jid, source) for message in parsed]
    result.messages_inserted = repo.bulk_upsert_messages(session, chat_ids, incoming)

    # -- 3. Media -----------------------------------------------------------
    result.media_detected = _register_media(
        session, parsed, chat_ids, whatsapp_account_id
    )

    # -- 4. Contactos y pushnames -------------------------------------------
    for jid, push_name in sync.pushnames:
        if jid and push_name:
            repo.upsert_contact(
                session,
                whatsapp_account_id=whatsapp_account_id,
                jid=jid,
                push_name=push_name,
            )
            result.pushnames += 1
    for message in parsed:
        sender = message.sender_jid or message.sender_lid
        if sender and message.push_name:
            repo.upsert_contact(
                session,
                whatsapp_account_id=whatsapp_account_id,
                jid=sender,
                push_name=message.push_name,
                lid=message.sender_lid,
            )

    # -- 4b. El mapeo LID -> telefono ya no se copia de ningun sitio --------
    #
    # Lo mantenia `lid_bridge`, leyendo la tabla `lid_map` del Signal Store de
    # pywhats. Con Baileys ese fichero no existe, y el dato llega por dos
    # caminos mejores que ya estan puestos: el par que viaja en la clave de
    # cada mensaje (`remoteJidAlt`/`participantAlt`, sink `lid_pair`) y el
    # usync. Los dos escriben en `contacts.lid` igual que antes, asi que
    # aguas abajo no cambia nada.

    # -- 5. Vista previa del sidebar ----------------------------------------
    repo.refresh_chat_previews(session, chat_ids.keys())

    # -- 6. Estado de historial ---------------------------------------------
    for chat_jid, chat_id in chat_ids.items():
        repo.get_or_create_history_state(session, chat_id=chat_id, chat_jid=chat_jid)
        # CON el chat_id: sin el, el recuento sumaba los mensajes de las dos
        # conversaciones cuando dos cuentas hablan con el mismo contacto.
        repo.refresh_history_state(session, chat_jid, chat_id=chat_id)

    log.info("HistorySync %s ingerido: %s", sync.sync_type, result)
    return result


# De donde vino el mensaje, segun el tipo de History Sync. Distinguirlo
# importa: sin esto no se puede medir si ON_DEMAND esta aportando algo
#, porque los mensajes live tapan la diferencia.
_SOURCE_BY_SYNC_TYPE = {
    "ON_DEMAND": "on_demand",
    "INITIAL_BOOTSTRAP": "initial_history",
    "INITIAL_STATUS_V3": "initial_history",
    "RECENT": "initial_history",
    "FULL": "initial_history",
    "PUSH_NAME": "initial_history",
    "NON_BLOCKING_DATA": "initial_history",
}


def source_for(sync_type: str) -> str:
    return _SOURCE_BY_SYNC_TYPE.get(sync_type, "initial_history")


def _to_incoming(
    message: ParsedMessage, own_jid: str | None, source: str = "initial_history"
) -> IncomingMessage:
    sender_jid = message.sender_jid
    if message.from_me and own_jid:
        sender_jid = own_jid
    return IncomingMessage(
        chat_jid=message.chat_jid,
        timestamp=message.timestamp,
        source=source,
        whatsapp_message_id=message.whatsapp_message_id,
        sender_jid=sender_jid,
        sender_lid=message.sender_lid,
        message_type=message.message_type,
        text=message.text,
        from_me=message.from_me,
        raw_metadata=message.metadata or None,
        raw_proto=message.raw_proto,
    )


def _register_media(
    session: Session,
    messages: Iterable[ParsedMessage],
    chat_ids: dict[str, int],
    whatsapp_account_id: Any = None,
) -> int:
    """Crea las filas de ``media_files`` en estado ``pending``.

    Hay que resolver antes el id del mensaje ya insertado, asi que se hace en
    una segunda pasada consultando por ``(chat_jid, whatsapp_message_id)``.
    Los mensajes sin ID real no se pueden correlacionar de forma fiable y su
    adjunto se registra en la siguiente pasada, cuando lo tengan.
    """
    with_media = [
        message
        for message in messages
        if message.media is not None and message.whatsapp_message_id
    ]
    if not with_media:
        return 0

    wanted = {(m.chat_jid, m.whatsapp_message_id) for m in with_media}
    rows = session.execute(
        select(Message.id, Message.chat_jid, Message.whatsapp_message_id).where(
            Message.whatsapp_message_id.in_([w[1] for w in wanted])
        )
    ).all()
    message_ids = {(chat_jid, wamid): row_id for row_id, chat_jid, wamid in rows}

    payload = []
    for message in with_media:
        key = (message.chat_jid, message.whatsapp_message_id)
        message_id = message_ids.get(key)
        if message_id is None:
            continue
        media = message.media
        assert media is not None
        payload.append(
            {
                "message_id": message_id,
                "chat_id": chat_ids[message.chat_jid],
                # Escrita a mano ademas de deducible por `chat_id`: asi una
                # consulta de multimedia no puede olvidarse de acotar.
                "whatsapp_account_id": whatsapp_account_id,
                "whatsapp_message_id": message.whatsapp_message_id,
                "media_type": media.media_type,
                "mime_type": media.mime_type,
                "file_name": media.file_name,
                "file_size": media.file_size,
                "duration_seconds": media.duration_seconds,
                "width": media.width,
                "height": media.height,
                "direct_path": media.direct_path,
                "media_key": media.media_key,
                "file_sha256": media.file_sha256,
                "file_enc_sha256": media.file_enc_sha256,
                "download_status": "pending",
            }
        )

    if not payload:
        return 0

    statement = insert(MediaFile).values(payload)
    # Un mensaje puede llegar por history y por live: la fila de media no debe
    # duplicarse ni perder lo que ya se hubiera descargado.
    statement = statement.on_conflict_do_nothing(
        constraint="uq_media_files_message_type"
    )
    session.execute(statement)
    return len(payload)
