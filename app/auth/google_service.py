"""Guardar, renovar y consultar la conexion de Google de un usuario.

DOS ESTADOS, NO UNO
-------------------
``google_connected`` y ``drive_authorized`` son cosas distintas. Google puede
dar identidad y negar Drive —el usuario desmarca la casilla en la pantalla de
consentimiento—, y dar el login por bueno seria prometer un almacenamiento que
no existe.

EL REFRESH TOKEN SOLO LLEGA UNA VEZ
-----------------------------------
Google lo entrega en el primer consentimiento. En los inicios de sesion
siguientes NO lo reenvia. Escribir ese ``None`` encima del que ya hay deja al
usuario sin acceso duradero en cuanto caduque el access token, y sin ninguna
senal de por que. Aqui solo se sobrescribe cuando llega uno nuevo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.auth.crypto import TokenCipher
from app.auth.google import (
    SCOPE_DRIVE,
    GoogleError,
    GoogleOAuthClient,
    TokensDeGoogle,
    token_vivo,
)
from app.core.logging_setup import get_logger
from app.models import GoogleCredential, User

log = get_logger("AUTH")


@dataclass(frozen=True)
class EstadoGoogle:
    """Lo que el frontend necesita saber. Nunca un token."""

    google_connected: bool
    drive_authorized: bool
    token_valid: bool
    scopes: tuple[str, ...] = ()
    email: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "google_connected": self.google_connected,
            "drive_authorized": self.drive_authorized,
            "token_valid": self.token_valid,
            "scopes": list(self.scopes),
            "email": self.email,
        }


DESCONECTADO = EstadoGoogle(
    google_connected=False, drive_authorized=False, token_valid=False
)


class GoogleService:
    """Persistencia y ciclo de vida de los tokens."""

    def __init__(self, database: Any, settings: Any) -> None:
        self._database = database
        self._settings = settings
        self._client = GoogleOAuthClient(settings)
        self._cipher: TokenCipher | None = None

    @property
    def client(self) -> GoogleOAuthClient:
        return self._client

    @property
    def cipher(self) -> TokenCipher:
        """Se crea al primer uso: sin conexion de Google no hace falta clave."""
        if self._cipher is None:
            self._cipher = TokenCipher(getattr(self._settings, "app_encryption_key", None))
        return self._cipher

    # -- Guardar -------------------------------------------------------------

    def guardar(self, user_id: Any, tokens: TokensDeGoogle) -> None:
        cipher = self.cipher
        caduca = datetime.now(timezone.utc) + timedelta(
            seconds=max(0, tokens.expires_in)
        )

        with self._database.transaction() as session:
            fila = session.execute(
                select(GoogleCredential).where(GoogleCredential.user_id == user_id)
            ).scalar_one_or_none()

            if fila is None:
                fila = GoogleCredential(
                    user_id=user_id,
                    google_subject=tokens.google_subject or "",
                )
                session.add(fila)

            fila.google_subject = tokens.google_subject or fila.google_subject
            fila.scope = tokens.scope or fila.scope
            fila.access_token_encrypted = cipher.encrypt(tokens.access_token)
            fila.access_token_expires_at = caduca
            fila.updated_at = datetime.now(timezone.utc)

            if tokens.refresh_token:
                fila.refresh_token_encrypted = cipher.encrypt(tokens.refresh_token)
            # Si no llego uno nuevo se conserva el guardado. Ver cabecera.

            session.flush()

        log.info(
            "Credenciales de Google guardadas (drive=%s)",
            "si" if tokens.drive_autorizado else "NO",
        )

    # -- Consultar -----------------------------------------------------------

    def estado(self, user_id: Any) -> EstadoGoogle:
        """Estado SIN llamar a Google. Es lo que consulta el frontend."""
        with self._database.transaction() as session:
            fila = session.execute(
                select(GoogleCredential).where(GoogleCredential.user_id == user_id)
            ).scalar_one_or_none()
            if fila is None:
                return DESCONECTADO

            scopes = tuple(s for s in (fila.scope or "").split(" ") if s)
            tiene_refresh = fila.refresh_token_encrypted is not None
            usuario = session.get(User, user_id)
            return EstadoGoogle(
                google_connected=True,
                drive_authorized=SCOPE_DRIVE in scopes,
                # Con refresh token se considera valido aunque el access haya
                # caducado: se renueva solo cuando haga falta.
                token_valid=token_vivo(fila.access_token_expires_at) or tiene_refresh,
                scopes=scopes,
                email=usuario.email if usuario else None,
            )

    # -- Usar ----------------------------------------------------------------

    def access_token(self, user_id: Any) -> str | None:
        """Un access token utilizable, renovandolo si hace falta.

        ``None`` significa que hay que volver a conectar Google. No se lanza:
        que Drive deje de estar autorizado es un estado del producto, no un
        fallo del servicio.
        """
        with self._database.transaction() as session:
            fila = session.execute(
                select(GoogleCredential).where(GoogleCredential.user_id == user_id)
            ).scalar_one_or_none()
            if fila is None:
                return None
            vivo = token_vivo(fila.access_token_expires_at)
            actual = self.cipher.decrypt(fila.access_token_encrypted)
            refresh = self.cipher.decrypt(fila.refresh_token_encrypted)

        if vivo and actual:
            return actual
        if not refresh:
            log.info("Sin refresh token: hay que volver a conectar Google")
            return None

        try:
            nuevos = self._client.refrescar(refresh)
        except GoogleError as exc:
            # Revocado desde la cuenta de Google, o credenciales cambiadas.
            log.info("No se pudo renovar el acceso a Google: %s", exc.code)
            self.marcar_invalido(user_id)
            return None

        # El refresh token se conserva: ``refrescar`` no lo reenvia.
        self.guardar(user_id, nuevos)
        return nuevos.access_token or None

    def comprobar_drive(self, user_id: Any) -> tuple[bool, str | None]:
        """Llamada real a Drive. ``(funciona, motivo_si_no)``.

        Es lo unico que demuestra que la autorizacion sirve. Que el login de
        Google funcionara no dice nada sobre Drive.
        """
        estado = self.estado(user_id)
        if not estado.google_connected:
            return False, "Google no esta conectado."
        if not estado.drive_authorized:
            return False, "No concediste acceso a Google Drive."

        token = self.access_token(user_id)
        if not token:
            return False, "El acceso a Google caduco. Vuelve a conectar tu cuenta."

        try:
            self._client.probar_drive(token)
        except GoogleError as exc:
            return False, exc.message
        return True, None

    # -- Quitar --------------------------------------------------------------

    def marcar_invalido(self, user_id: Any) -> None:
        """El acceso ya no sirve: se olvida el token muerto, no la conexion."""
        with self._database.transaction() as session:
            fila = session.execute(
                select(GoogleCredential).where(GoogleCredential.user_id == user_id)
            ).scalar_one_or_none()
            if fila is None:
                return
            fila.access_token_encrypted = None
            fila.access_token_expires_at = None
            fila.refresh_token_encrypted = None
            session.flush()

    def desconectar(self, user_id: Any) -> bool:
        """Revoca en Google y borra las credenciales. La cuenta se conserva."""
        with self._database.transaction() as session:
            fila = session.execute(
                select(GoogleCredential).where(GoogleCredential.user_id == user_id)
            ).scalar_one_or_none()
            if fila is None:
                return False
            try:
                refresh = self.cipher.decrypt(fila.refresh_token_encrypted)
                acceso = self.cipher.decrypt(fila.access_token_encrypted)
            except Exception:  # noqa: BLE001 - no poder descifrarlo no impide borrar
                refresh = acceso = None
            session.delete(fila)
            session.flush()

        # Se avisa a Google DESPUES de borrar: si la revocacion falla, aqui ya
        # no queda nada, que es lo que el usuario pidio.
        for token in (refresh, acceso):
            if token:
                self._client.revocar(token)
                break

        log.info("Google desconectado; la cuenta de la aplicacion se conserva")
        return True
