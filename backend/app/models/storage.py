"""Indice del contenido que vive fuera de PostgreSQL.

EL REPARTO
----------
PostgreSQL guarda el INDICE: quien, cuando, de que chat, en que segmento y en
que linea. Drive guarda el CONTENIDO: el texto de los mensajes y los archivos.

Asi la base se mantiene en un tamano manejable aunque el backup crezca a
cientos de gigabytes, y sigue sirviendo para listar, paginar y buscar sin
tocar la red.

DRIVE TIENE QUE BASTAR PARA RECONSTRUIR
---------------------------------------
Si PostgreSQL se pierde, el manifiesto, las ``appProperties`` de cada archivo
y la cabecera de cada segmento tienen que permitir rehacer este indice. Por
eso el segmento guarda su propio ``chat_jid`` y sus marcas de tiempo: un
indice se puede reconstruir, un contenido perdido no.
"""

from __future__ import annotations

import uuid
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.schema import Base

#: Estados de un segmento.
SEGMENT_STATUSES = ("building", "uploading", "ready", "failed")

#: Estados de un trabajo de subida.
JOB_STATUSES = ("pending", "processing", "complete", "failed", "paused")

#: Que se sube.
JOB_TYPES = ("message_segment", "media")

#: Donde esta el contenido de una fila.
STORAGE_STATUSES = ("local", "pending", "uploading", "ready", "failed")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class UserStorageKey(Base):
    """La clave de contenido de un usuario, ENVUELTA.

    Nunca en claro. Lo que se guarda aqui solo se abre con la KEK del
    servidor, que vive en ``.env`` y jamas viaja a Drive.
    """

    __tablename__ = "user_storage_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=_uuid
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    encrypted_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Para poder rotar la KEK sin quedarse sin poder leer lo anterior.
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class GoogleDriveStorage(Base):
    """Donde vive la copia de un usuario dentro de SU Drive."""

    __tablename__ = "google_drive_storage"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=_uuid
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # Se guarda el identificador, no el nombre: buscar la carpeta por nombre
    # en cada peticion es lento y ademas ambiguo (puede haber dos iguales, o
    # el usuario puede renombrarla).
    root_folder_id: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    bytes_uploaded: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    files_uploaded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_upload_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DriveFolder(Base):
    """Cache de las carpetas ya creadas, por entidad.

    Sin esto habria que preguntarle a Drive por la carpeta de cada chat antes
    de cada subida: una llamada de red para averiguar algo que no cambia.
    """

    __tablename__ = "drive_folders"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=_uuid
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Ruta logica dentro de la copia: "accounts/<uuid>/chats/<uuid>/media".
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    folder_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "path", name="uq_drive_folders_user_path"),
    )


class MessageSegment(Base):
    """Un bloque de mensajes ya subido, o en camino.

    Un mensaje por archivo produciria millones de archivos en Drive, que es
    lento de listar, caro de recorrer y roza los limites de la API. Se agrupan
    en segmentos.

    Un segmento cerrado es INMUTABLE: no se vuelve a descargar ni a reescribir
    cuando llega un mensaje nuevo. Los mensajes nuevos van al siguiente.
    """

    __tablename__ = "message_segments"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=_uuid
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    whatsapp_account_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("whatsapp_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False
    )
    # Se repite aqui a proposito: si hubiera que reconstruir el indice desde
    # Drive, el segmento tiene que saber decir a que conversacion pertenece.
    chat_jid: Mapped[str] = mapped_column(String(128), nullable=False)

    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    drive_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    first_timestamp: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_timestamp: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    uncompressed_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    compressed_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    stored_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # Del contenido en claro, antes de comprimir y cifrar: es lo que permite
    # comprobar que lo que se recupera es lo que se guardo.
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Del archivo tal y como quedo en Drive: comprueba la subida sin
    # descifrar nada.
    ciphertext_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="building")
    format_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Se anota por archivo: si el cifrado se apaga o se enciende, hay que
    # saber como leer CADA uno, no suponerlo por la configuracion de hoy.
    encrypted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "chat_id", "sequence_number", name="uq_message_segments_chat_seq"
        ),
        CheckConstraint(
            "status IN ('building','uploading','ready','failed')",
            name="ck_message_segments_status",
        ),
        Index("ix_message_segments_chat", "chat_id", "sequence_number"),
        Index("ix_message_segments_status", "status"),
        Index("ix_message_segments_user", "user_id"),
    )


class StorageJob(Base):
    """Trabajo pendiente de subida. Vive en la base, no en memoria.

    Es el patron *outbox*: el mensaje y su trabajo se escriben en la MISMA
    transaccion. Si el proceso muere entre una cosa y otra, no puede quedar un
    mensaje sin subir del que nadie se acuerde.
    """

    __tablename__ = "storage_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=_uuid
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    whatsapp_account_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("whatsapp_accounts.id", ondelete="CASCADE"),
        nullable=True,
    )

    job_type: Mapped[str] = mapped_column(String(24), nullable=False)
    # A que apunta: el segmento (UUID) o el adjunto (id numerico como texto).
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Bytes en claro que representa. Sirve para la contrapresion: sin esto no
    # se puede saber cuanto se esta acumulando si Drive lleva dias caido.
    payload_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Un solo trabajo vivo por entidad: es lo que impide que un reintento
        # cree un archivo nuevo en Drive en vez de repetir el mismo.
        UniqueConstraint("job_type", "entity_id", name="uq_storage_jobs_entity"),
        CheckConstraint(
            "status IN ('pending','processing','complete','failed','paused')",
            name="ck_storage_jobs_status",
        ),
        CheckConstraint(
            "job_type IN ('message_segment','media')", name="ck_storage_jobs_type"
        ),
        Index("ix_storage_jobs_listos", "status", "next_retry_at"),
        Index("ix_storage_jobs_user", "user_id", "status"),
    )
