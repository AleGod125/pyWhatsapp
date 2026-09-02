"""Resolucion dinamica de la version de WhatsApp Web.

BUG VERIFICADO (pywhats 0.2.0, ``pywhats/version.py:19``)::

    WA_WEB_VERSION: tuple[int, int, int] = (2, 3000, 1035194821)

Esa revision esta congelada desde la publicacion del paquete. El servidor la
rechaza durante el pairing con ``PairingFailed reason=405``. La revision real
publicada el 2026-09-01 era 1046564374.

Segundo detalle, tambien verificado: ``pywhats/pairing.py:83-88`` hace

    from pywhats.version import (..., WA_WEB_VERSION)

a nivel de modulo. Eso copia el valor al espacio de nombres de ``pairing``, asi
que reasignar ``pywhats.version.WA_WEB_VERSION`` NO cambia lo que usan
``_user_agent()`` (lineas 122-124) ni ``_build_hash()`` (linea 160). Hay que
parchear AMBOS modulos, y hacerlo antes de construir el Client.

Politica ante fallo de red: primero en vivo, luego cache. Si no hay ninguno de
los dos, se aborta. NO se continua el pairing con la revision obsoleta.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from app.logging_setup import get_logger

log = get_logger("WA")

SW_JS_URL = "https://web.whatsapp.com/sw.js"

# Cabeceras obligatorias, determinadas empiricamente el 2026-09-01: sin
# Sec-Fetch-Dest/Service-Worker el servidor responde 400 Bad Request. Se imita
# la peticion que hace el navegador al registrar el service worker.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Sec-Fetch-Dest": "serviceworker",
    "Sec-Fetch-Mode": "same-origin",
    "Sec-Fetch-Site": "same-origin",
    "Service-Worker": "script",
    "Referer": "https://web.whatsapp.com/",
}

# El valor viene dentro de un blob JSON incrustado en una cadena JavaScript, de
# modo que las comillas llegan escapadas:
#     ...\"SiteData\":{\"server_revision\":1046564374,\"client_revision\":1046564374,...
# Se eliminan las barras invertidas antes de aplicar el patron, para no tener
# que contemplar cada variante de escapado.
_REVISION_RE = re.compile(r'client_revision"?\s*:\s*(\d+)')

# Componentes fijos del triple de version. Solo la tercera posicion (la
# revision) cambia; asi lo modela tambien pywhats.
VERSION_PRIMARY = 2
VERSION_SECONDARY = 3000

# Cota de cordura: la revision es un entero grande y creciente. Un valor
# absurdamente bajo indica que se ha capturado otra cosa.
_MIN_PLAUSIBLE_REVISION = 1_000_000_000


class WAVersionError(RuntimeError):
    """No se pudo determinar una revision de WhatsApp Web utilizable."""


def fetch_client_revision(*, timeout: float = 10.0) -> int:
    """Descarga ``sw.js`` y extrae ``client_revision``.

    :raises WAVersionError: si la peticion falla o el valor no aparece.
    """
    request = urllib.request.Request(SW_JS_URL, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise WAVersionError(f"sw.js devolvio HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001 - urllib lanza tipos muy variados
        raise WAVersionError(f"no se pudo descargar sw.js: {exc}") from exc

    normalized = body.replace("\\", "")
    match = _REVISION_RE.search(normalized)
    if match is None:
        raise WAVersionError(
            "sw.js se descargo pero no contiene 'client_revision'; "
            "es probable que WhatsApp haya cambiado el formato"
        )

    revision = int(match.group(1))
    if revision < _MIN_PLAUSIBLE_REVISION:
        raise WAVersionError(
            f"la revision extraida ({revision}) no es plausible; "
            f"se esperaba un entero >= {_MIN_PLAUSIBLE_REVISION}"
        )
    return revision


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def read_cache(path: Path) -> tuple[int, int, int] | None:
    """Lee la version cacheada. ``None`` si no existe o esta corrupta."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        version = (int(data["primary"]), int(data["secondary"]), int(data["tertiary"]))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("Cache de version ilegible (%s); se ignora", exc)
        return None

    if version[2] < _MIN_PLAUSIBLE_REVISION:
        log.warning("Cache de version con revision no plausible (%s); se ignora", version[2])
        return None
    return version


def write_cache(path: Path, version: tuple[int, int, int]) -> None:
    """Persiste la version resuelta. Un fallo aqui no es fatal."""
    payload = {
        "primary": version[0],
        "secondary": version[1],
        "tertiary": version[2],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("No se pudo escribir la cache de version: %s", exc)


# ---------------------------------------------------------------------------
# Resolucion + parcheo
# ---------------------------------------------------------------------------


def resolve(
    cache_path: Path, *, timeout: float = 10.0, allow_cache: bool = True
) -> tuple[tuple[int, int, int], str]:
    """Devuelve ``((primary, secondary, tertiary), origen)``.

    ``origen`` es ``"live"`` o ``"cache"``. Si no hay ninguna de las dos se
    lanza :class:`WAVersionError`: continuar con la revision obsoleta de
    pywhats solo produciria un 405 mas confuso.
    """
    log.info("Resolviendo version de WhatsApp Web...")
    try:
        revision = fetch_client_revision(timeout=timeout)
    except WAVersionError as live_error:
        log.warning("No se pudo resolver en vivo: %s", live_error)
        if allow_cache:
            cached = read_cache(cache_path)
            if cached is not None:
                log.info("Usando version cacheada: %s", ".".join(map(str, cached)))
                return cached, "cache"
        raise WAVersionError(
            f"no hay revision en vivo ni cache valida en {cache_path}. "
            f"Causa original: {live_error}. "
            f"No se continua el pairing con la revision obsoleta de pywhats "
            f"porque el servidor la rechaza con 405."
        ) from live_error

    version = (VERSION_PRIMARY, VERSION_SECONDARY, revision)
    log.info("Version resuelta en vivo: %s", ".".join(map(str, version)))
    write_cache(cache_path, version)
    return version, "live"


def apply(version: tuple[int, int, int]) -> None:
    """Inyecta la version en pywhats. Debe llamarse ANTES de crear el Client.

    Se parchean los dos espacios de nombres porque ``pairing`` importo el valor
    por copia (ver docstring del modulo).
    """
    import pywhats.pairing
    import pywhats.version

    previous = pywhats.version.WA_WEB_VERSION

    pywhats.version.WA_WEB_VERSION = version
    pywhats.pairing.WA_WEB_VERSION = version

    log.info(
        "[COMPAT] WA_WEB_VERSION %s -> %s (version + pairing)",
        ".".join(map(str, previous)),
        ".".join(map(str, version)),
    )

    # Verificacion: si pairing siguiera viendo el valor viejo, el parche no
    # habria servido de nada y el 405 volveria sin explicacion.
    if pywhats.pairing.WA_WEB_VERSION != version:  # pragma: no cover - defensivo
        raise WAVersionError("el parche de WA_WEB_VERSION no se aplico a pywhats.pairing")


def resolve_and_apply(
    cache_path: Path, *, timeout: float = 10.0
) -> tuple[tuple[int, int, int], str]:
    """Atajo: resolver y parchear en un paso."""
    version, origin = resolve(cache_path, timeout=timeout)
    apply(version)
    return version, origin
