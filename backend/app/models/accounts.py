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
    text,
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

    # COMO LA LLAMA EL USUARIO. Se rellena al vincular con el nombre del
    # perfil que da WhatsApp (`creds.me.name`), pero es suyo: renombrarla no
    # toca nada mas. Nulo significa "todavia no se sabe", y entonces la
    # interfaz cae al numero.
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    # `personal`, `business` o `unknown`. Se queda en `unknown` mientras no se
    # pueda determinar de forma fiable: inventarselo seria peor que callar.
    account_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown"
    )

    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)

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
        # Existe en la base desde `c3d4e5f6a7b8`; se declara aqui para que el
        # autogenerado no proponga borrarla en la siguiente migracion.
        CheckConstraint(
            "account_type IN ('personal','business','unknown')",
            name="ck_whatsapp_accounts_type",
        ),
        UniqueConstraint("session_storage_key", name="uq_whatsapp_accounts_storage"),
        # UN TELEFONO, UNA CUENTA POR USUARIO.
        #
        # Vincular el mismo movil dos veces no da dos copias: da la MISMA
        # copia duplicada, con los mismos identificadores de mensaje en dos
        # filas. Se midio -- 132 conversaciones guardadas dos veces, una de
        # ellas con 654 de 654 mensajes repetidos.
        #
        # Habia una comprobacion en codigo, y esta bien tenerla porque da un
        # mensaje util; pero una comprobacion que solo vive en el codigo se
        # salta por cualquier camino que no pase por ella.
        #
        # SOBRE `phone_number`, NO SOBRE `wa_pn`.
        #
        # `wa_pn` llega de WhatsApp con el identificador de DISPOSITIVO pegado
        # --`573008927374:30@s.whatsapp.net`-- y ese cambia en cada
        # re-vinculacion. Con la unicidad ahi, el mismo telefono se colaba dos
        # veces sin mas que volver a escanear. Se midio, con la restriccion ya
        # puesta:
        #
        #     e2492d66   wa_pn = 573008927374@s.whatsapp.net      linked
        #     4f9774ab   wa_pn = 573008927374:30@s.whatsapp.net   revoked
        #
        # Dos filas, el mismo telefono, y PostgreSQL sin nada que objetar
        # porque las cadenas no son iguales.
        #
        # `phone_number` son solo los digitos, que es lo que identifica a una
        # persona. PUEDE ser NULL, y ahi PostgreSQL trata los NULL como
        # distintos: varias cuentas creadas y aun sin vincular conviven.
        UniqueConstraint(
            "user_id", "phone_number", name="uq_whatsapp_accounts_user_phone"
        ),
        Index("ix_whatsapp_accounts_user", "user_id"),
    )


class UserWhatsAppMembership(Base):
    """Quien puede entrar a que cuenta de WhatsApp. Asociacion EXPLICITA.

    POR QUE HACE FALTA UNA TABLA Y NO BASTA ``whatsapp_accounts.user_id``
    --------------------------------------------------------------------
    La columna ``user_id`` dice quien CREO la vinculacion. Eso no es lo mismo
    que quien puede usarla: una cuenta compartida --socios, una pareja, un
    equipo-- necesita varias personas con acceso a la MISMA sesion de
    WhatsApp, sin duplicar ni la sesion, ni el Signal Store, ni los mensajes.

    Con una sola columna eso obliga a elegir entre dos cosas malas: duplicar
    la cuenta (dos sesiones para un mismo telefono) o mirar el estado global
    del proceso, que es exactamente el fallo que se midio -- un usuario nuevo
    veia "cuenta vinculada" porque habia OTRO WhatsApp conectado en el
    servidor.

    LAS DOS REGLAS, Y POR QUE SON ASIMETRICAS
    -----------------------------------------
    ``UNIQUE(user_id)``: una persona tiene como mucho UNA cuenta de WhatsApp.
    Es la regla de producto de hoy, y la base la hace cumplir en vez de
    confiar en que nadie se salte una comprobacion.

    **No** hay ``UNIQUE(whatsapp_account_id)``, y es a proposito: varias
    personas SI pueden compartir una cuenta. Poner ahi otra unicidad seria
    cerrar la puerta que esta tabla existe para abrir.

    LO QUE NO SE HACE
    -----------------
    Una membresia no se crea nunca por coincidencia. Que alguien escanee un
    telefono que ya esta registrado NO le da acceso: hace falta un acto
    explicito. Compartir por reconocer un numero seria entregar el historial
    de otra persona a quien tenga su movil un minuto.
    """

    __tablename__ = "user_whatsapp_memberships"

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
    #: ``owner`` la creo; ``member`` recibio acceso. Sin permisos finos
    #: todavia: hoy los dos ven lo mismo, y anadir matices antes de que hagan
    #: falta seria inventarse un modelo de permisos sin un caso que lo pida.
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="owner")

    # LA CUENTA QUE ESTA MIRANDO esa persona ahora mismo.
    #
    # Va en la membresia y no en la cuenta a proposito: dos personas pueden
    # compartir un WhatsApp y estar mirando cosas distintas. Y es la base
    # quien impide que queden dos activas a la vez -- un indice unico parcial
    # sobre `user_id WHERE is_active` --, no el codigo: dos peticiones a la
    # vez dejarian dos activas y la siguiente lectura elegiria al azar.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    #: Si esta cuenta la eligio LA PERSONA o la puso el sistema por defecto.
    #:
    #: `is_active` dice cual se esta mirando, pero no de donde salio, y esas
    #: dos cosas se tratan distinto: una eleccion explicita se respeta aunque
    #: la cuenta parezca vacia --acaba de vincularse y aun no ha traido nada--
    #: mientras que un valor por defecto se puede sustituir sin preguntar.
    #:
    #: Sin esta distincion el sistema movia al usuario de cuenta por su cuenta
    #: y en silencio: se vinculaba el segundo telefono, se elegia, y setenta
    #: segundos despues aparecia el primero otra vez.
    elegida_por_el_usuario: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('owner','member')", name="ck_memberships_role"
        ),
        # Aqui vivia `UNIQUE(user_id)` -- "una cuenta de WhatsApp por persona".
        # Era correcta mientras no habia forma de elegir cuenta en la
        # interfaz; ahora la sustituye la regla de la activa, mas abajo.
        #
        # La misma pareja no se puede anotar dos veces.
        UniqueConstraint(
            "user_id", "whatsapp_account_id", name="uq_memberships_user_account"
        ),
        Index("ix_memberships_account", "whatsapp_account_id"),
        # COMO MUCHO UNA ACTIVA por usuario, garantizado por la base.
        Index(
            "uq_memberships_activa",
            "user_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

