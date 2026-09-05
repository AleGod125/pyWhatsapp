"""OAuth 2.0 con Google, del lado del servidor.

POR QUE SERVER-SIDE
-------------------
El ``client_secret`` vive solo aqui. Si el intercambio del codigo ocurriera en
Angular, el secreto estaria en un bundle que cualquiera descarga.

Angular nunca ve el ``access_token`` ni el ``refresh_token``: solo sabe si
Drive esta autorizado o no.

SOBRE LA FIRMA DEL ID TOKEN
---------------------------
No se valida la firma RS256 contra el JWKS de Google, y es deliberado: el
token llega en la RESPUESTA de un POST que este servidor hace directamente al
endpoint de Google, sobre TLS y autenticandose con el ``client_secret``. El
canal ya garantiza el origen. Es lo que la propia documentacion de Google
permite para clientes confidenciales en flujo server-side, y evita traer un
verificador de JWT con su cache de claves.

Si que se comprueban las AFIRMACIONES (``iss``, ``aud``, ``exp``, ``nonce``),
que es lo que protege de reutilizar un token de otra aplicacion o caducado.

MINIMO PRIVILEGIO
-----------------
Se pide ``drive.file``, no ``drive``. Ver docs/GOOGLE_OAUTH_SETUP.md.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("AUTH")

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
DRIVE_ABOUT_ENDPOINT = "https://www.googleapis.com/drive/v3/about"

EMISORES_VALIDOS = ("https://accounts.google.com", "accounts.google.com")

#: Identidad basica. ``openid`` es lo que hace que Google devuelva ID token.
SCOPES_IDENTIDAD = ("openid", "email", "profile")

#: Solo los archivos que crea esta aplicacion. NO ``.../auth/drive``, que da
#: acceso al Drive entero del usuario y no hace ninguna falta.
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive.file"

SCOPES = (*SCOPES_IDENTIDAD, SCOPE_DRIVE)

#: Margen antes de dar por vivo un access token. Uno que caduca en 20 segundos
#: puede morir a mitad de la peticion siguiente.
MARGEN_CADUCIDAD_SEGUNDOS = 120

TIEMPO_LIMITE = 20


class GoogleError(Exception):
    """Algo fallo hablando con Google."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class GoogleNoConfigurado(GoogleError):
    def __init__(self) -> None:
        super().__init__(
            "GOOGLE_NOT_CONFIGURED",
            "Faltan GOOGLE_CLIENT_ID y GOOGLE_CLIENT_SECRET en .env. "
            "Ver docs/GOOGLE_OAUTH_SETUP.md.",
        )


@dataclass
class TokensDeGoogle:
    """Lo que devuelve el intercambio del codigo."""

    access_token: str
    expires_in: int
    scope: str
    refresh_token: str | None = None
    id_token_claims: dict[str, Any] = field(default_factory=dict)

    @property
    def google_subject(self) -> str | None:
        return self.id_token_claims.get("sub")

    @property
    def email(self) -> str | None:
        return self.id_token_claims.get("email")

    @property
    def scopes(self) -> list[str]:
        return [s for s in (self.scope or "").split(" ") if s]

    @property
    def drive_autorizado(self) -> bool:
        """Google puede conceder identidad y NEGAR Drive. Son cosas distintas."""
        return SCOPE_DRIVE in self.scopes


class GoogleOAuthClient:
    """Las cuatro llamadas que hacen falta. Sin dependencias extra."""

    def __init__(self, settings: Any) -> None:
        self._client_id = getattr(settings, "google_client_id", None)
        self._client_secret = getattr(settings, "google_client_secret", None)
        self._redirect_uri = getattr(settings, "google_redirect_uri", "")

    @property
    def configurado(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _exigir_configuracion(self) -> None:
        if not self.configurado:
            raise GoogleNoConfigurado()

    # -- 1. Enviar al usuario a Google --------------------------------------

    def url_de_autorizacion(
        self, *, state: str, code_challenge: str, nonce: str, forzar_consentimiento: bool
    ) -> str:
        self._exigir_configuracion()
        parametros = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # Sin ``offline`` no hay refresh token, y sin refresh token el
            # acceso a Drive muere en una hora.
            "access_type": "offline",
            "include_granted_scopes": "true",
        }
        if forzar_consentimiento:
            # Google solo entrega refresh_token la PRIMERA vez que el usuario
            # consiente. Si no lo tenemos guardado, hay que volver a pedirlo
            # explicitamente o nos quedamos sin acceso duradero.
            parametros["prompt"] = "consent"
        return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(parametros)}"

    # -- 2. Canjear el codigo ------------------------------------------------

    def canjear_codigo(
        self, *, code: str, code_verifier: str, nonce: str
    ) -> TokensDeGoogle:
        self._exigir_configuracion()
        cuerpo = {
            "code": code,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "redirect_uri": self._redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }
        datos = self._post_form(TOKEN_ENDPOINT, cuerpo)

        claims: dict[str, Any] = {}
        if datos.get("id_token"):
            claims = self._afirmaciones_del_id_token(datos["id_token"], nonce=nonce)

        return TokensDeGoogle(
            access_token=datos.get("access_token", ""),
            refresh_token=datos.get("refresh_token"),
            expires_in=int(datos.get("expires_in") or 0),
            scope=datos.get("scope", ""),
            id_token_claims=claims,
        )

    # -- 3. Renovar ----------------------------------------------------------

    def refrescar(self, refresh_token: str) -> TokensDeGoogle:
        self._exigir_configuracion()
        datos = self._post_form(
            TOKEN_ENDPOINT,
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        return TokensDeGoogle(
            access_token=datos.get("access_token", ""),
            # Al refrescar, Google NO reenvia el refresh token. Devolver None
            # aqui es correcto; lo que no puede hacer quien llama es escribir
            # ese None encima del que ya tiene guardado.
            refresh_token=datos.get("refresh_token"),
            expires_in=int(datos.get("expires_in") or 0),
            scope=datos.get("scope", ""),
        )

    # -- 4. Comprobar y revocar ---------------------------------------------

    def userinfo(self, access_token: str) -> dict[str, Any]:
        return self._get(USERINFO_ENDPOINT, access_token)

    def probar_drive(self, access_token: str) -> dict[str, Any]:
        """Llamada minima que demuestra que el token sirve para Drive.

        ``about`` con ``fields`` acotado no crea nada, no lista archivos y
        entra dentro de ``drive.file``. Es la comprobacion mas barata posible
        de que la autorizacion existe de verdad, en vez de suponerlo porque el
        login funciono.
        """
        url = f"{DRIVE_ABOUT_ENDPOINT}?fields=user(displayName,emailAddress),storageQuota(limit,usage)"
        return self._get(url, access_token)

    def revocar(self, token: str) -> bool:
        """Le pide a Google que invalide el token. Nunca lanza.

        Si falla, las credenciales locales se borran igual: dejarlas porque
        Google no contesto seria peor.
        """
        try:
            self._post_form(REVOKE_ENDPOINT, {"token": token})
            return True
        except GoogleError as exc:
            log.info("No se pudo revocar el token en Google: %s", exc.code)
            return False

    # -- HTTP ----------------------------------------------------------------

    def _post_form(self, url: str, cuerpo: dict[str, str]) -> dict[str, Any]:
        datos = urllib.parse.urlencode(cuerpo).encode("ascii")
        peticion = urllib.request.Request(
            url,
            data=datos,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        return self._ejecutar(peticion)

    def _get(self, url: str, access_token: str) -> dict[str, Any]:
        peticion = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {access_token}"}, method="GET"
        )
        return self._ejecutar(peticion)

    def _ejecutar(self, peticion: Any) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(peticion, timeout=TIEMPO_LIMITE) as respuesta:
                crudo = respuesta.read().decode("utf-8")
            return json.loads(crudo) if crudo else {}
        except urllib.error.HTTPError as exc:
            detalle = ""
            try:
                detalle = json.loads(exc.read().decode("utf-8")).get("error", "")
            except Exception:  # noqa: BLE001 - el detalle es un extra
                pass
            # El cuerpo puede llevar el codigo, nunca los tokens: no se
            # registra entero.
            log.warning("Google respondio %s (%s)", exc.code, detalle or "sin detalle")
            if exc.code in (400, 401):
                raise GoogleError(
                    "GOOGLE_TOKEN_REJECTED",
                    "Google rechazo la peticion. Vuelve a conectar tu cuenta.",
                ) from exc
            raise GoogleError(
                "GOOGLE_UNAVAILABLE", "Google no respondio correctamente."
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GoogleError(
                "GOOGLE_UNREACHABLE", "No se pudo contactar con Google."
            ) from exc
        except json.JSONDecodeError as exc:
            raise GoogleError(
                "GOOGLE_BAD_RESPONSE", "Google devolvio una respuesta ilegible."
            ) from exc

    # -- ID token ------------------------------------------------------------

    def _afirmaciones_del_id_token(
        self, id_token: str, *, nonce: str
    ) -> dict[str, Any]:
        """Lee y comprueba las afirmaciones. La firma la avala el canal TLS."""
        try:
            _, carga, _ = id_token.split(".")
            relleno = "=" * (-len(carga) % 4)
            claims = json.loads(base64.urlsafe_b64decode(carga + relleno))
        except Exception as exc:  # noqa: BLE001
            raise GoogleError(
                "GOOGLE_BAD_ID_TOKEN", "El identificador de Google no es legible."
            ) from exc

        if claims.get("iss") not in EMISORES_VALIDOS:
            raise GoogleError("GOOGLE_BAD_ISSUER", "El emisor del token no es Google.")
        if claims.get("aud") != self._client_id:
            # Un token emitido para OTRA aplicacion. Aceptarlo permitiria
            # entrar aqui con credenciales de un servicio distinto.
            raise GoogleError(
                "GOOGLE_BAD_AUDIENCE", "Ese token no fue emitido para esta aplicacion."
            )
        if int(claims.get("exp") or 0) <= int(time.time()):
            raise GoogleError("GOOGLE_TOKEN_EXPIRED", "El token de Google ha caducado.")
        if nonce and claims.get("nonce") != nonce:
            # Ata la respuesta a ESTA peticion: un token capturado antes no
            # sirve para completar un inicio de sesion nuevo.
            raise GoogleError(
                "GOOGLE_BAD_NONCE", "La respuesta de Google no corresponde a esta peticion."
            )
        if not claims.get("sub"):
            raise GoogleError(
                "GOOGLE_NO_SUBJECT", "Google no devolvio un identificador de usuario."
            )
        return claims


def token_vivo(expira_en: Any) -> bool:
    """``True`` si el access token aun sirve, con margen."""
    if expira_en is None:
        return False
    from datetime import datetime, timezone

    momento = expira_en
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    restante = (momento - datetime.now(timezone.utc)).total_seconds()
    return restante > MARGEN_CADUCIDAD_SEGUNDOS
