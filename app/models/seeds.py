"""Anclas de historial: la referencia con la que se puede pedir el pasado.

QUE ES UNA SEMILLA
------------------
``HISTORY_SYNC_ON_DEMAND`` va anclado: hay que decirle DESDE QUE MENSAJE
seguir hacia atras. Una semilla es esa referencia — un identificador de
mensaje real de WhatsApp con su marca de tiempo.

Sin ella, una conversacion se queda en ``waiting_seed``: existe, se ve, y no
se le puede pedir historial.

POR QUE UNA TABLA
-----------------
Las semillas llegan por caminos distintos y en momentos distintos: el
bootstrap inicial, un mensaje en vivo, la descarga de los pendientes al
reconectar, un reenvio tras un fallo de descifrado. Anotarlas permite:

* no perderlas cuando llegan antes de que el chat las necesite;
* saber DE DONDE salio cada una, que es lo unico que dice si una fuente
  aporta algo o no;
* no reprocesar lo mismo una y otra vez.

LO QUE NO GUARDA
----------------
Ni el texto, ni el protobuf, ni nada de Signal. El mensaje ya vive en su sitio
—PostgreSQL como indice, Drive como contenido—; esto es solo el puntero.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.schema import Base

#: De donde salio una semilla. Sirve para medir que fuentes aportan de verdad:
#: sin esto, "hemos anadido fuentes" es una afirmacion que nadie comprueba.
SEED_SOURCES = (
    "initial_bootstrap",
    "recent_history",
    "full_history",
    "on_demand",
    "live",
    "offline",
    "retry_resend",
    "blob_scan",
)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class HistorySeed(Base):
    """Un ancla real para pedir el historial de una conversacion."""

    __tablename__ = "history_seeds"

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
    #: Se repite el JID para poder diagnosticar sin unir tablas.
    chat_jid: Mapped[str] = mapped_column(String(128), nullable=False)

    #: El identificador REAL que dio WhatsApp. Nunca uno fabricado: un ancla
    #: inventada recibe confirmacion y despues silencio, que es el fallo mas
    #: caro de diagnosticar de todo el proyecto.
    wa_msg_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Epoch en SEGUNDOS, como lo espera ON_DEMAND pese al nombre del campo.
    timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    from_me: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    source: Mapped[str] = mapped_column(String(24), nullable=False)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Se conserva aunque deje de valer: saber que se probo y no sirvio es
    #: informacion, y borrarla haria que se volviera a intentar sin fin.
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        # La misma ancla puede llegar por dos caminos —en vivo y en un blob—;
        # es una sola.
        UniqueConstraint(
            "whatsapp_account_id",
            "chat_id",
            "wa_msg_id",
            name="uq_history_seeds_chat_msg",
        ),
        Index("ix_history_seeds_chat", "chat_id", "timestamp"),
        Index("ix_history_seeds_user", "user_id"),
    )


class ScannedBlob(Base):
    """Un archivo de History Sync del que YA se extrajeron semillas.

    Sin esto, cada revision de los chats pendientes volvia a descomprimir e
    interpretar los mismos archivos una vez POR CHAT: con 28 pendientes y 4
    blobs, 112 lecturas para descubrir lo mismo que la primera.
    """

    __tablename__ = "scanned_blobs"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=_uuid
    )
    whatsapp_account_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("whatsapp_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: SHA-256 del archivo. Identifica el contenido, no el nombre: renombrarlo
    #: o volver a archivarlo no lo convierte en un blob nuevo.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sync_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    seeds_found: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "whatsapp_account_id", "sha256", name="uq_scanned_blobs_account_sha"
        ),
    )
