"""Cifrado de los tokens de Google guardados en PostgreSQL.

POR QUE
-------
Un ``refresh_token`` de Google da acceso continuado al Drive del usuario hasta
que lo revoque. Si la base se filtra en claro, se filtra ese acceso. Cifrarlos
convierte el problema en "hace falta ADEMAS la clave", que vive en ``.env`` y
nunca en git.

QUE SE USA
----------
``Fernet`` de ``cryptography``: AES-128-CBC con HMAC-SHA256, cifrado
autenticado y con marca de tiempo. Viene ya instalado como dependencia de
pywhats, asi que no anade nada nuevo. No se escribe criptografia propia.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken


class ClaveDeCifradoInvalida(RuntimeError):
    """``APP_ENCRYPTION_KEY`` falta o no sirve."""


def generar_clave() -> str:
    """Una clave nueva, para pegar en ``.env``."""
    return Fernet.generate_key().decode("ascii")


class TokenCipher:
    """Cifra y descifra cadenas cortas. Nada mas."""

    def __init__(self, clave: str | None) -> None:
        if not clave:
            raise ClaveDeCifradoInvalida(
                "Falta APP_ENCRYPTION_KEY. Generala con:\n"
                "    py -c \"from app.auth.crypto import generar_clave; "
                'print(generar_clave())"\n'
                "y ponla en .env. Sin ella no se pueden guardar tokens de Google."
            )
        try:
            self._fernet = Fernet(clave.encode("ascii"))
        except Exception as exc:  # noqa: BLE001
            raise ClaveDeCifradoInvalida(
                "APP_ENCRYPTION_KEY no es una clave Fernet valida (32 bytes en "
                "base64 urlsafe). Genera una nueva con app.auth.crypto.generar_clave()."
            ) from exc

    def encrypt(self, valor: str | None) -> bytes | None:
        if valor is None:
            return None
        return self._fernet.encrypt(valor.encode("utf-8"))

    def decrypt(self, dato: bytes | None) -> str | None:
        """Devuelve ``None`` si no se puede descifrar, en vez de lanzar.

        Un token ilegible (clave rotada, fila corrupta) significa que hay que
        volver a conectar Google. Es un estado recuperable, no un fallo del
        servicio.
        """
        if not dato:
            return None
        try:
            return self._fernet.decrypt(dato).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            return None


# ---------------------------------------------------------------------------
# Tokens de sesion
# ---------------------------------------------------------------------------
#
# En la base NO se guarda el token, sino su hash. Con el hash no se puede
# fabricar la cookie, asi que una filtracion de la tabla no entrega las
# sesiones abiertas.


def nuevo_token_de_sesion() -> str:
    """256 bits de aleatoriedad criptografica, en urlsafe base64."""
    return secrets.token_urlsafe(32)


def hash_de_token(token: str) -> str:
    """SHA-256 del token.

    Aqui SI vale un hash rapido, al reves que con las contrasenas: el token
    tiene 256 bits de entropia real, asi que no hay diccionario que probar. Lo
    que se busca es que el valor guardado no sirva para autenticarse.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def comparar_seguro(a: str, b: str) -> bool:
    """Comparacion en tiempo constante, para no filtrar por cuanto tarda."""
    return hmac.compare_digest(a, b)


def nuevo_estado_oauth() -> str:
    """El ``state`` de OAuth: aleatorio, de un solo uso."""
    return secrets.token_urlsafe(32)


def verificador_pkce() -> tuple[str, str]:
    """``(verifier, challenge)`` para PKCE S256.

    Aunque el cliente es confidencial y guarda su ``client_secret``, PKCE ata
    ademas el codigo a ESTA peticion concreta: un codigo interceptado no sirve
    sin el verificador, que nunca sale del servidor.
    """
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge
