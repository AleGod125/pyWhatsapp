"""Usuarios, sesiones web, credenciales de Google y cuentas de WhatsApp.

EL MODELO DE PROPIEDAD
----------------------
Todo cuelga del usuario, y los datos del backup cuelgan de la cuenta de
WhatsApp::

    users
      ├── user_sessions          sesion web (cookie)
      ├── google_credentials     tokens OAuth cifrados
      └── whatsapp_accounts      la vinculacion del companion
            └── chats  ──► messages, media_files, chat_history_state, ...

Asi la propiedad de un mensaje se resuelve siguiendo una sola cadena y no hay
que repetir ``user_id`` en cada tabla, que es donde acaban apareciendo filas
con dueno equivocado.

LO QUE NUNCA SE GUARDA EN CLARO
-------------------------------
* contrasenas -> Argon2id (:mod:`app.auth.passwords`)
* tokens de Google -> Fernet (:mod:`app.auth.crypto`)
* token de sesion -> solo su SHA-256; con el hash no se puede fabricar la cookie
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.schema import Base

#: Como se autentica una cuenta. ``both`` es quien empezo con contrasena y
#: despues vinculo Google, o al reves.
AUTH_PROVIDERS = ("local", "google", "both")

#: Estado de la vinculacion del companion para esa cuenta.
WHATSAPP_SESSION_STATUSES = (
    "never_linked",
    "linked",
    "disconnected",
    "revoked",
    "error",
)

#: Los que significan "este usuario YA vinculo una cuenta".
#:
#: ``disconnected`` cuenta: el socket se cae constantemente —red, suspension,
#: reinicio del servicio— y eso NO desvincula nada. Si contara como no
#: vinculado, cada corte devolveria al usuario a la pantalla del codigo QR
#: con su cuenta perfectamente vinculada.
#:
#: ``revoked`` NO cuenta: ahi el servidor ha dicho que esa vinculacion ya no
#: existe, y hace falta uno nuevo de verdad.
LINKED_STATUSES = frozenset({"linked", "disconnected"})


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class User(Base):
    """Una persona que usa la aplicacion."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=_uuid
    )
    # CITEXT: dos personas no pueden registrarse con Ana@x.com y ana@x.com.
    # La unicidad la garantiza el motor, no una comprobacion nuestra que se
    # puede colar entre dos peticiones simultaneas.
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)

    # Nulo a proposito: una cuenta creada con Google puede no tener nunca
    # contrasena local. No es un dato que falte, es que no existe.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)

    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    auth_provider: Mapped[str] = mapped_column(
        String(16), nullable=False, default="local"
    )
    # El ``sub`` de Google: estable aunque la persona cambie su correo. El
    # email NO sirve como identidad: se puede cambiar y se puede reutilizar.
    google_subject: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )

    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    google: Mapped["GoogleCredential | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    whatsapp_accounts: Mapped[list["WhatsAppAccount"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "auth_provider IN ('local','google','both')",
            name="ck_users_auth_provider",
        ),
        # Una cuenta tiene que poder autenticarse de alguna forma. Sin esto,
        # una fila con password_hash y google_subject a NULL seria una cuenta
        # a la que nadie puede entrar y que nadie puede recuperar.
        CheckConstraint(
            "password_hash IS NOT NULL OR google_subject IS NOT NULL",
            name="ck_users_tiene_alguna_credencial",
        ),
    )


class UserSession(Base):
    """Una sesion web abierta. La cookie lleva el token; aqui vive su hash."""

    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=_uuid
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # SHA-256 del token. Nunca el token: con el hash no se puede fabricar la
    # cookie, asi que una filtracion de esta tabla no entrega sesiones vivas.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")

    __table_args__ = (
        Index("ix_user_sessions_user", "user_id"),
        Index("ix_user_sessions_expires", "expires_at"),
    )


class GoogleCredential(Base):
    """Los tokens OAuth de un usuario. Cifrados."""

    __tablename__ = "google_credentials"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=_uuid
    )
    # Una sola conexion de Google por usuario en esta fase.
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    google_subject: Mapped[str] = mapped_column(String(255), nullable=False)

    # Los scopes REALES que Google concedio, no los que se pidieron. Puede
    # conceder identidad y negar Drive, y son estados distintos.
    scope: Mapped[str] = mapped_column(Text, nullable=False, default="")

    access_token_encrypted: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )
    refresh_token_encrypted: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )
    access_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="google")


class WhatsAppAccount(Base):
    """La vinculacion del companion, con dueno explicito.

    Sustituye a la sesion "global" de antes: cada vinculacion pertenece a un
    usuario y su estado en disco vive bajo ``session/users/<user_id>/``.
    """

    __tablename__ = "whatsapp_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=_uuid
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    phone_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    wa_pn: Mapped[str | None] = mapped_column(String(64), nullable=True)
    wa_lid: Mapped[str | None] = mapped_column(String(64), nullable=True)

    session_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="never_linked"
    )
    # Carpeta bajo session/users/. Se guarda para no tener que deducirla en
    # dos sitios distintos y que un dia dejen de coincidir.
    session_storage_key: Mapped[str] = mapped_column(String(128), nullable=False)

    linked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="whatsapp_accounts")

    __table_args__ = (
        CheckConstraint(
            "session_status IN "
            "('never_linked','linked','disconnected','revoked','error')",
            name="ck_whatsapp_accounts_status",
        ),
        UniqueConstraint("session_storage_key", name="uq_whatsapp_accounts_storage"),
        Index("ix_whatsapp_accounts_user", "user_id"),
    )
