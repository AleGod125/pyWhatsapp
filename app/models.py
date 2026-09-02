"""Esquema PostgreSQL del backup.

Esta es la base de datos de NUESTRA APLICACION. No confundir con el estado
del protocolo, que vive en ``session/`` y pertenece a pywhats:

    session/device.json             <- DeviceStore (credenciales del companion)
    session/device.json.signal.db   <- Signal store (sessions, prekeys, identities)

Decisiones de diseno relevantes:

* ``messages.timestamp`` guarda el epoch en segundos TAL CUAL lo entrega
  WhatsApp. Es el valor fiel al protocolo y el que se usa para ordenar y
  paginar. ``created_at`` / ``updated_at`` son contabilidad de la fila.
* La deduplicacion es un indice UNIQUE PARCIAL sobre
  ``(chat_jid, whatsapp_message_id)`` restringido a los mensajes que tienen
  un ID real. Los mensajes sin ID utilizable no colisionan entre si y su
  identidad es la PK de PostgreSQL.
* ``synthetic_identifier`` existe para correlacion interna y JAMAS debe
  copiarse a ``whatsapp_message_id``: el cursor de ON_DEMAND depende de que
  esa columna contenga solo IDs que el servidor de WhatsApp reconozca.
* ``raw_proto`` conserva el ``WebMessageInfo`` serializado para poder
  reinterpretar el mensaje en el futuro sin volver a pedirlo.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ---------------------------------------------------------------------------
# Vocabularios cerrados. Se aplican como CHECK y se reutilizan en el codigo
# para no escribir literales sueltos por ahi.
# ---------------------------------------------------------------------------

CHAT_TYPES = ("individual", "group", "broadcast", "newsletter", "unknown")

# De donde salio la fila. Un mensaje puede llegar por varias vias; se conserva
# la primera que lo trajo y no se degrada (ver upsert en repository.py).
MESSAGE_SOURCES = ("initial_history", "on_demand", "live", "unknown")

HISTORY_STATUSES = (
    "pending",  # nunca solicitado
    "fetching",  # peticion en vuelo
    "exhausted",  # hay evidencia real de que no queda mas
    "server_limited",  # el servidor corto el historial
    "timeout",  # la peticion no obtuvo respuesta
    "error",
    "no_valid_cursor",  # no hay ningun mensaje con ID real que sirva de ancla
)

MEDIA_TYPES = (
    "image",
    "video",
    "gif",
    "audio",
    "voice_note",
    "sticker",
    "document",
    "unknown",
)

DOWNLOAD_STATUSES = (
    "pending",
    "downloading",
    "downloaded",
    "unavailable",  # el CDN ya no lo tiene
    "expired",
    "failed",
)

REQUEST_STATUSES = ("sent", "acked", "received", "timeout", "error")


def _enum_check(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


class Base(DeclarativeBase):
    """Base declarativa. Alembic autogenera contra ``Base.metadata``."""


# ---------------------------------------------------------------------------
# contacts
# ---------------------------------------------------------------------------


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # JID canonico, p.ej. "34600111222@s.whatsapp.net". Unico cuando existe.
    jid: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)

    # LID ("...@lid"). WhatsApp usa ambos espacios de identificadores; se
    # conservan por separado y no se convierte uno en otro.
    lid: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Solo si puede determinarse de verdad a partir del JID. No se inventa.
    phone_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    push_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    business_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    raw_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_contacts_lid", "lid"),
        Index("ix_contacts_phone_number", "phone_number"),
        # Busqueda por nombre en el sidebar.
        Index("ix_contacts_push_name", "push_name"),
    )

    def __repr__(self) -> str:
        return f"<Contact jid={self.jid!r}>"


# ---------------------------------------------------------------------------
# chats
# ---------------------------------------------------------------------------


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    jid: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)

    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    chat_type: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")

    last_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Epoch en segundos, igual que messages.timestamp.
    last_message_timestamp: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    raw_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    messages: Mapped[list[Message]] = relationship(
        back_populates="chat", cascade="all, delete-orphan", passive_deletes=True
    )
    history_state: Mapped[ChatHistoryState | None] = relationship(
        back_populates="chat", cascade="all, delete-orphan", passive_deletes=True, uselist=False
    )

    __table_args__ = (
        _enum_check("chat_type", CHAT_TYPES, "ck_chats_chat_type"),
        # Orden del sidebar: los chats mas recientes primero.
        Index(
            "ix_chats_last_message_timestamp",
            "last_message_timestamp",
            postgresql_using="btree",
        ),
    )

    def __repr__(self) -> str:
        return f"<Chat jid={self.jid!r} type={self.chat_type}>"


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False
    )
    # Desnormalizado a proposito: el pipeline de ingesta y el cursor de
    # ON_DEMAND trabajan por JID sin tener que resolver el FK primero.
    chat_jid: Mapped[str] = mapped_column(String(128), nullable=False)

    # ID REAL de WhatsApp. NULL si el mensaje no traia uno utilizable.
    # Nunca se rellena con un identificador generado localmente.
    whatsapp_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Identidad interna opcional para filas sin ID oficial. NO es un ID de
    # WhatsApp y no debe usarse como cursor de historial.
    synthetic_identifier: Mapped[str | None] = mapped_column(String(128), nullable=True)

    sender_jid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sender_lid: Mapped[str | None] = mapped_column(String(128), nullable=True)

    message_type: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Epoch en segundos tal como lo entrega WhatsApp.
    timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    from_me: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    source: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")

    raw_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # WebMessageInfo serializado. Permite reinterpretar el mensaje sin
    # volver a pedirlo al servidor.
    raw_proto: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    chat: Mapped[Chat] = relationship(back_populates="messages")
    media: Mapped[list[MediaFile]] = relationship(
        back_populates="message", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        _enum_check("source", MESSAGE_SOURCES, "ck_messages_source"),
        # Un ID de WhatsApp vacio es un error de ingesta, no un "sin ID":
        # para eso esta NULL. Esto protege la integridad del cursor.
        CheckConstraint(
            "whatsapp_message_id IS NULL OR length(whatsapp_message_id) > 0",
            name="ck_messages_wamid_not_empty",
        ),
        # Deduplicacion: UNIQUE parcial. Los mensajes sin ID real quedan
        # fuera del indice y por tanto nunca colisionan entre si.
        Index(
            "uq_messages_chat_wamid",
            "chat_jid",
            "whatsapp_message_id",
            unique=True,
            postgresql_where=sa_text("whatsapp_message_id IS NOT NULL"),
        ),
        # Paginacion de la conversacion en la GUI (ultimos N, luego scroll).
        Index("ix_messages_chat_id_timestamp", "chat_id", "timestamp"),
        # Pipeline de ingesta y calculo del cursor historico.
        Index("ix_messages_chat_jid_timestamp", "chat_jid", "timestamp"),
        Index("ix_messages_source", "source"),
        Index("ix_messages_sender_jid", "sender_jid"),
    )

    def __repr__(self) -> str:
        return (
            f"<Message id={self.id} chat={self.chat_jid!r} "
            f"wamid={self.whatsapp_message_id!r} ts={self.timestamp}>"
        )


# ---------------------------------------------------------------------------
# chat_history_state
# ---------------------------------------------------------------------------


class ChatHistoryState(Base):
    """Progreso de la extraccion historica por chat. Permite reanudar."""

    __tablename__ = "chat_history_state"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    chat_jid: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)

    # Ancla mas antigua UTILIZABLE (ID real de WhatsApp). Puede no coincidir
    # con el mensaje mas antiguo almacenado: ver get_oldest_valid_history_cursor.
    oldest_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    oldest_message_timestamp: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    newest_message_timestamp: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    requests_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    responses_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    history_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    last_response_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Respuestas validas seguidas que no aportaron nada nuevo. Criterio (D)
    # para dar un chat por agotado.
    consecutive_no_progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    chat: Mapped[Chat] = relationship(back_populates="history_state")

    __table_args__ = (
        _enum_check("history_status", HISTORY_STATUSES, "ck_history_state_status"),
        Index("ix_history_state_status", "history_status"),
    )

    def __repr__(self) -> str:
        return f"<ChatHistoryState chat={self.chat_jid!r} status={self.history_status}>"


# ---------------------------------------------------------------------------
# history_requests
# ---------------------------------------------------------------------------


class HistoryRequest(Base):
    """Una peticion HISTORY_SYNC_ON_DEMAND emitida, para correlacionarla."""

    __tablename__ = "history_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # ID de stanza con el que se envio la peticion.
    protocol_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # peerDataRequestSessionId, si el protocolo lo entrega de vuelta.
    peer_data_request_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    chat_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("chats.id", ondelete="SET NULL"), nullable=True
    )
    chat_jid: Mapped[str] = mapped_column(String(128), nullable=False)

    # Ancla enviada: siempre un ID real de WhatsApp.
    cursor_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cursor_timestamp: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    requested_count: Mapped[int] = mapped_column(Integer, nullable=False, default=50)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="sent")

    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    response_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        _enum_check("status", REQUEST_STATUSES, "ck_history_requests_status"),
        Index("ix_history_requests_chat_jid", "chat_jid"),
        Index("ix_history_requests_protocol_id", "protocol_request_id"),
        Index("ix_history_requests_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<HistoryRequest chat={self.chat_jid!r} status={self.status}>"


# ---------------------------------------------------------------------------
# media_files
# ---------------------------------------------------------------------------


class MediaFile(Base):
    """Metadata de un adjunto.

    La fila se crea en cuanto se detecta el adjunto (``pending``). La descarga
    ocurre despues y de forma asincrona. Que el archivo ya no este disponible
    en el CDN no invalida el mensaje: son dos cosas distintas.
    """

    __tablename__ = "media_files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False
    )
    whatsapp_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    media_type: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)

    media_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    direct_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Material necesario para descifrar el adjunto. Se persiste porque sin el
    # un mensaje historico ya no se puede recuperar. NUNCA se loguea.
    media_key: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    file_sha256: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    file_enc_sha256: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    # Ruta relativa a MEDIA_DIR, para que mover la carpeta no rompa la DB.
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    download_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    download_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    message: Mapped[Message] = relationship(back_populates="media")

    __table_args__ = (
        _enum_check("media_type", MEDIA_TYPES, "ck_media_files_media_type"),
        _enum_check("download_status", DOWNLOAD_STATUSES, "ck_media_files_download_status"),
        # Un adjunto por mensaje y tipo: evita duplicar la fila si el mismo
        # mensaje llega por history y por live.
        UniqueConstraint("message_id", "media_type", name="uq_media_files_message_type"),
        Index("ix_media_files_download_status", "download_status"),
        # Deduplicacion del archivo fisico entre mensajes distintos.
        Index("ix_media_files_file_sha256", "file_sha256"),
        Index("ix_media_files_chat_id", "chat_id"),
    )

    def __repr__(self) -> str:
        return f"<MediaFile id={self.id} type={self.media_type} status={self.download_status}>"


# ---------------------------------------------------------------------------
# app_state
# ---------------------------------------------------------------------------


class AppState(Base):
    """Clave/valor de la APLICACION. Nada que ver con el Signal Store.

    Aqui viven flags como ``ondemand_capability_confirmed``.
    """

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<AppState key={self.key!r}>"
