"""Registro, login y sesiones web.

QUIEN DECIDE QUIEN ERES
-----------------------
El servidor, siempre, a partir de la cookie de sesion. El navegador nunca
manda un ``user_id``: si lo hiciera, cambiarlo seria todo lo que hace falta
para leer los chats de otra persona.

QUE HAY EN LA COOKIE
--------------------
Un token aleatorio de 256 bits. Nada mas: ni identificador, ni correo, ni
tokens de Google. En la base se guarda solo su SHA-256, asi que ni siquiera
una filtracion de ``user_sessions`` entrega sesiones vivas.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update

from app.auth.crypto import hash_de_token, nuevo_token_de_sesion
from app.auth.passwords import PasswordHasherService, validar_password
from app.core.logging_setup import get_logger
from app.models import User, UserSession

log = get_logger("AUTH")


class AuthError(Exception):
    """Fallo de autenticacion con un codigo que el frontend entiende."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class SesionIniciada:
    """El resultado de autenticarse: a quien, y con que token de cookie."""

    user_id: Any
    token: str
    expires_at: datetime


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def normalizar_email(email: str | None) -> str:
    if not isinstance(email, str):
        return ""
    return email.strip()


def email_valido(email: str) -> bool:
    """Comprobacion deliberadamente laxa.

    Validar correos con una expresion regular estricta rechaza direcciones
    legitimas y no impide ninguna falsa. Lo unico que importa aqui es que
    tenga forma de correo; que exista lo dira la verificacion, no un regex.
    """
    if not email or len(email) > 254 or " " in email:
        return False
    local, arroba, dominio = email.partition("@")
    return bool(arroba and local and "." in dominio and not dominio.startswith("."))


class AuthService:
    """Cuentas y sesiones. No sabe nada de HTTP."""

    def __init__(self, database: Any, settings: Any) -> None:
        self._database = database
        self._settings = settings
        self._hasher = PasswordHasherService()

    # -- Registro ------------------------------------------------------------

    def register(
        self, email: str, password: str, display_name: str | None = None
    ) -> SesionIniciada:
        email = normalizar_email(email)
        if not email_valido(email):
            raise AuthError("INVALID_EMAIL", "Ese correo no parece valido.")

        problema = validar_password(password)
        if problema is not None:
            raise AuthError("WEAK_PASSWORD", problema)

        with self._database.transaction() as session:
            # La comprobacion previa es solo para dar un mensaje claro; quien
            # garantiza la unicidad es el indice, que no se puede colar entre
            # dos peticiones simultaneas.
            existente = session.execute(
                select(User.id).where(User.email == email)
            ).scalar_one_or_none()
            if existente is not None:
                raise AuthError(
                    "EMAIL_TAKEN", "Ya hay una cuenta con ese correo.", status=409
                )

            usuario = User(
                email=email,
                password_hash=self._hasher.hash(password),
                display_name=(display_name or "").strip() or None,
                auth_provider="local",
                email_verified=False,
            )
            session.add(usuario)
            session.flush()
            log.info("Cuenta creada (%s)", _correo_corto(email))
            return self._abrir_sesion(session, usuario)

    # -- Login ---------------------------------------------------------------

    def login(self, email: str, password: str) -> SesionIniciada:
        """Autentica con contrasena.

        Todos los fallos devuelven el MISMO error. Distinguir "ese correo no
        existe" de "la contrasena no coincide" convierte el formulario en un
        buscador de cuentas registradas.
        """
        email = normalizar_email(email)
        generico = AuthError(
            "INVALID_CREDENTIALS", "Correo o contrasena incorrectos.", status=401
        )

        with self._database.transaction() as session:
            usuario = session.execute(
                select(User).where(User.email == email)
            ).scalar_one_or_none()

            # Se verifica aunque el usuario no exista: si se saliera antes, el
            # tiempo de respuesta diria cuales estan registrados.
            resultado = self._hasher.verify(
                usuario.password_hash if usuario else None, password
            )
            if usuario is None or not resultado.valida:
                raise generico
            if not usuario.is_active:
                raise AuthError(
                    "ACCOUNT_DISABLED", "Esta cuenta esta desactivada.", status=403
                )

            if resultado.necesita_rehash:
                # El coste de Argon2 subio desde que se guardo: se refuerza
                # ahora, que es cuando tenemos la contrasena en claro.
                usuario.password_hash = self._hasher.hash(password)

            return self._abrir_sesion(session, usuario)

    # -- Sesiones ------------------------------------------------------------

    def _abrir_sesion(self, session: Any, usuario: User) -> SesionIniciada:
        token = nuevo_token_de_sesion()
        caduca = _ahora() + timedelta(days=self._settings.session_lifetime_days)
        session.add(
            UserSession(
                user_id=usuario.id,
                token_hash=hash_de_token(token),
                expires_at=caduca,
                last_seen_at=_ahora(),
            )
        )
        usuario.last_login_at = _ahora()
        session.flush()
        return SesionIniciada(user_id=usuario.id, token=token, expires_at=caduca)

    def abrir_sesion_para(self, user_id: Any) -> SesionIniciada:
        """Abre sesion sin comprobar contrasena. Solo para OAuth.

        Quien llama YA ha demostrado la identidad contra Google. No puede
        alcanzarse desde ningun endpoint que reciba datos del navegador.
        """
        with self._database.transaction() as session:
            usuario = session.get(User, user_id)
            if usuario is None:
                raise AuthError("USER_NOT_FOUND", "La cuenta no existe.", status=404)
            if not usuario.is_active:
                raise AuthError(
                    "ACCOUNT_DISABLED", "Esta cuenta esta desactivada.", status=403
                )
            return self._abrir_sesion(session, usuario)

    def resolver(self, token: str | None) -> User | None:
        """El usuario de esa cookie, o ``None``.

        Devuelve ``None`` sin distinguir causas —no existe, caducada,
        revocada, cuenta desactivada—: para quien pregunta todas significan lo
        mismo, y separarlas solo daria informacion.
        """
        if not token:
            return None

        with self._database.transaction() as session:
            sesion = session.execute(
                select(UserSession).where(
                    UserSession.token_hash == hash_de_token(token)
                )
            ).scalar_one_or_none()
            if sesion is None or sesion.revoked_at is not None:
                return None
            if _con_zona(sesion.expires_at) <= _ahora():
                return None

            usuario = session.get(User, sesion.user_id)
            if usuario is None or not usuario.is_active:
                return None

            sesion.last_seen_at = _ahora()
            session.flush()
            # Se separa de la sesion de SQLAlchemy para que siga siendo
            # utilizable despues de cerrar la transaccion.
            session.expunge(usuario)
            return usuario

    def revocar(self, token: str | None) -> bool:
        if not token:
            return False
        with self._database.transaction() as session:
            filas = session.execute(
                update(UserSession)
                .where(
                    UserSession.token_hash == hash_de_token(token),
                    UserSession.revoked_at.is_(None),
                )
                .values(revoked_at=_ahora())
            ).rowcount
        return bool(filas)

    def revocar_todas(self, user_id: Any) -> int:
        """Cierra todas las sesiones de un usuario. Para cambios de credencial."""
        with self._database.transaction() as session:
            return (
                session.execute(
                    update(UserSession)
                    .where(
                        UserSession.user_id == user_id,
                        UserSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=_ahora())
                ).rowcount
                or 0
            )

    def limpiar_caducadas(self) -> int:
        """Borra sesiones muertas. Mantenimiento, no seguridad."""
        from sqlalchemy import delete

        with self._database.transaction() as session:
            return (
                session.execute(
                    delete(UserSession).where(UserSession.expires_at < _ahora())
                ).rowcount
                or 0
            )


def _con_zona(momento: datetime) -> datetime:
    """PostgreSQL puede devolver el valor sin zona segun el driver."""
    if momento.tzinfo is None:
        return momento.replace(tzinfo=timezone.utc)
    return momento


def _correo_corto(email: str) -> str:
    """Para los logs: identifica sin escribir la direccion entera."""
    local, _, dominio = email.partition("@")
    return f"{local[:2]}***@{dominio}"
