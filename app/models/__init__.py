"""Esquema de PostgreSQL y descriptores protobuf.

El esquema vive en :mod:`app.models.schema` y se reexporta aqui, de modo que
``from app.models import Chat, Message`` sigue funcionando igual que cuando
``app/models.py`` era un unico modulo. Alembic tambien depende de esa forma
(``from app.models import Base``), asi que el reexporte no es azucar: es parte
del contrato.

Los descriptores protobuf propios estan en :mod:`app.models.proto`.
"""

from __future__ import annotations

from app.models.accounts import (  # noqa: F401
    AUTH_PROVIDERS,
    WHATSAPP_SESSION_STATUSES,
    GoogleCredential,
    User,
    UserSession,
    WhatsAppAccount,
)
from app.models.seeds import (  # noqa: F401
    SEED_SOURCES,
    HistorySeed,
    ScannedBlob,
)
from app.models.storage import (  # noqa: F401
    JOB_STATUSES,
    JOB_TYPES,
    SEGMENT_STATUSES,
    STORAGE_STATUSES,
    DriveFolder,
    GoogleDriveStorage,
    MessageSegment,
    StorageJob,
    UserStorageKey,
)
from app.models.schema import (
    AppState,
    Base,
    Chat,
    ChatHistoryState,
    Contact,
    HistoryRequest,
    MediaFile,
    Message,
    CHAT_TYPES,
    COMPLETE_STATUSES,
    DOWNLOAD_STATUSES,
    HISTORY_STATUSES,
    SEEDLESS_STATUSES,
    MEDIA_TYPES,
    MESSAGE_SOURCES,
)

__all__ = [
    "AppState",
    "Base",
    "Chat",
    "ChatHistoryState",
    "Contact",
    "HistoryRequest",
    "MediaFile",
    "Message",
    "CHAT_TYPES",
    "COMPLETE_STATUSES",
    "DOWNLOAD_STATUSES",
    "HISTORY_STATUSES",
    "SEEDLESS_STATUSES",
    "MEDIA_TYPES",
    "MESSAGE_SOURCES",
]
