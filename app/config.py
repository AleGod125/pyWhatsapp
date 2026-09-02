"""Carga y validacion de configuracion desde .env.

Reglas de este modulo:

* Ninguna ruta absoluta de una maquina concreta vive en el codigo. Todo sale
  de ``.env`` y se resuelve contra la raiz del proyecto.
* La password de PostgreSQL nunca se imprime ni se incluye en ``repr``.
  Para logs se usa :meth:`Settings.safe_database_url`.
* La validacion ocurre al arrancar y dice exactamente que variable falta.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

# Raiz del proyecto = carpeta que contiene este paquete, un nivel arriba.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "off"}


class ConfigError(RuntimeError):
    """Configuracion ausente o invalida. El mensaje nombra la variable."""


# ---------------------------------------------------------------------------
# Lectores tipados
# ---------------------------------------------------------------------------


def _str(name: str, default: str | None = None, *, required: bool = False) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        if required:
            raise ConfigError(
                f"falta la variable obligatoria {name}. "
                f"Copia .env.example a .env y completala."
            )
        return default if default is not None else ""
    return raw.strip()


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ConfigError(
        f"{name}={raw!r} no es un booleano valido. "
        f"Usa uno de: {sorted(_TRUE_VALUES | _FALSE_VALUES)}"
    )


def _int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} no es un entero valido") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name}={value} debe ser >= {minimum}")
    return value


def _float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} no es un numero valido") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name}={value} debe ser >= {minimum}")
    return value


# Tope de mensajes por peticion ON_DEMAND. El servidor acota la respuesta por
# su cuenta, asi que el valor pedido es una cota superior, no una promesa: el
# backfill encadena peticiones hasta agotar cada chat de todos modos.
# El limite real se descubre observando lo que devuelve (se registra en
# history_requests.response_count), no suponiendolo.
MAX_ON_DEMAND_COUNT = 500
DEFAULT_ON_DEMAND_COUNT = 50


def _history_count() -> int:
    value = _int("HISTORY_ON_DEMAND_COUNT", DEFAULT_ON_DEMAND_COUNT, minimum=1)
    if value > MAX_ON_DEMAND_COUNT:
        import logging

        logging.getLogger("app.config").warning(
            "HISTORY_ON_DEMAND_COUNT=%d es mayor que el maximo razonable (%d). "
            "El servidor acota la respuesta igualmente, asi que un valor alto no "
            "acelera la extraccion y puede hacer que rechace la peticion. "
            "Se usara %d; la extraccion sigue siendo completa porque encadena "
            "peticiones hasta agotar el historial.",
            value,
            MAX_ON_DEMAND_COUNT,
            MAX_ON_DEMAND_COUNT,
        )
        return MAX_ON_DEMAND_COUNT
    return value


def _path(name: str, default: str) -> Path:
    """Resuelve una ruta del .env contra la raiz del proyecto.

    Una ruta relativa como ``./session`` se ancla en PROJECT_ROOT para que el
    comportamiento no dependa del directorio de trabajo. Una ruta absoluta se
    respeta tal cual.
    """
    raw = _str(name, default)
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate
    return (PROJECT_ROOT / candidate).resolve()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Configuracion efectiva de la aplicacion."""

    # --- Aplicacion ---
    app_env: str
    app_debug: bool
    log_level: str

    # --- PostgreSQL ---
    database_url: str = field(repr=False)  # contiene la password: fuera del repr
    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_user: str

    # --- Rutas ---
    session_dir: Path
    data_dir: Path
    media_dir: Path
    diagnostics_dir: Path
    wa_version_cache: Path

    # --- Companion ---
    pairing_name: str
    pairing_max_attempts: int
    backfill_all_after_canary: bool

    # --- History on-demand ---
    history_on_demand_count: int
    history_request_timeout: float
    history_settle_seconds: float
    max_on_demand_concurrency: int

    # --- Multimedia ---
    media_download_concurrency: int
    # Cada cuanto revisa el worker permanente si hay adjuntos nuevos.
    media_worker_interval: float

    # --- Mantenimiento automatico ---
    # Periodo de la reconciliacion en segundo plano. 0 la desactiva.
    maintenance_interval_seconds: float

    # --- Interfaz ---
    gui_enabled: bool
    terminal_progress_enabled: bool

    # --- Compatibilidades pywhats 0.2.0 ---
    compat_wa_version: bool
    wa_version_fetch_timeout: float
    compat_windows_store: bool
    compat_pairing_515: bool
    pairing_515_timeout: float
    compat_prekey_replay: bool
    compat_history_messages: bool

    # -- Derivados -----------------------------------------------------------

    @property
    def session_file(self) -> Path:
        """Ruta del DeviceStore que pywhats persiste.

        pywhats deriva el Signal store como ``f"{session_path}.signal.db"``
        (client.py:379), asi que este archivo determina ambos:
            session/device.json
            session/device.json.signal.db
        """
        return self.session_dir / "device.json"

    @property
    def signal_store_file(self) -> Path:
        """Signal store SQLite de pywhats. Propiedad del protocolo, NO del backup."""
        return Path(str(self.session_file) + ".signal.db")

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    def safe_database_url(self) -> str:
        """URL de PostgreSQL apta para logs, con la password sustituida.

        Se opera sobre la cadena sin usar SQLAlchemy para que sea utilizable
        aunque la URL este malformada.
        """
        return _redact_url(self.database_url)

    def ensure_directories(self) -> None:
        """Crea las carpetas de trabajo. Idempotente."""
        for directory in (
            self.session_dir,
            self.data_dir,
            self.media_dir,
            self.diagnostics_dir,
            self.cache_dir,
            self.wa_version_cache.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)


def _redact_url(url: str) -> str:
    """Sustituye la password de una URL por ``***``.

    ``postgresql+psycopg://user:secreto@host:5432/db``
        -> ``postgresql+psycopg://user:***@host:5432/db``
    """
    if "@" not in url or "//" not in url:
        return url
    scheme, _, rest = url.partition("//")
    credentials, _, location = rest.rpartition("@")
    if not credentials:
        return url
    user, sep, password = credentials.partition(":")
    if not sep or not password:
        return url
    return f"{scheme}//{user}:***@{location}"


def build_database_url(
    *, host: str, port: int, database: str, user: str, password: str
) -> str:
    """Construye la URL de PostgreSQL a partir de las piezas POSTGRES_*.

    Usuario y password se codifican para tolerar caracteres reservados
    (``@``, ``:``, ``/``) sin romper el parseo de la URL.
    """
    user_part = quote(user, safe="")
    if password:
        user_part = f"{user_part}:{quote(password, safe='')}"
    return f"postgresql+psycopg://{user_part}@{host}:{port}/{database}"


def load_settings(*, env_file: Path | None = None, override: bool = False) -> Settings:
    """Carga ``.env`` y devuelve la configuracion validada.

    :param env_file: ruta al .env. Por defecto ``<raiz>/.env``.
    :param override: si el .env debe pisar variables ya presentes en el entorno.
    :raises ConfigError: si falta una variable obligatoria o hay un valor invalido.
    """
    target = env_file if env_file is not None else PROJECT_ROOT / ".env"
    if target.exists():
        load_dotenv(target, override=override)

    # --- PostgreSQL: DATABASE_URL manda; si no, se construye de POSTGRES_* ---
    host = _str("POSTGRES_HOST", "localhost")
    port = _int("POSTGRES_PORT", 5432, minimum=1)
    database = _str("POSTGRES_DB", "whatsapp_backup")
    user = _str("POSTGRES_USER", "postgres")
    password = os.environ.get("POSTGRES_PASSWORD", "")

    database_url = _str("DATABASE_URL", "")
    if not database_url:
        if not user:
            raise ConfigError(
                "no hay DATABASE_URL y POSTGRES_USER esta vacio: "
                "no es posible construir la conexion a PostgreSQL"
            )
        database_url = build_database_url(
            host=host, port=port, database=database, user=user, password=password
        )

    settings = Settings(
        app_env=_str("APP_ENV", "development"),
        app_debug=_bool("APP_DEBUG", True),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        database_url=database_url,
        postgres_host=host,
        postgres_port=port,
        postgres_db=database,
        postgres_user=user,
        session_dir=_path("SESSION_DIR", "./session"),
        data_dir=_path("DATA_DIR", "./data"),
        media_dir=_path("MEDIA_DIR", "./data/media"),
        diagnostics_dir=_path("DIAGNOSTICS_DIR", "./diagnostics"),
        wa_version_cache=_path("WA_VERSION_CACHE", "./data/cache/wa_web_version.json"),
        pairing_name=_str("PAIRING_NAME", "WhatsApp Backup"),
        pairing_max_attempts=_int("PAIRING_MAX_ATTEMPTS", 3, minimum=1),
        backfill_all_after_canary=_bool("BACKFILL_ALL_AFTER_CANARY", True),
        history_on_demand_count=_history_count(),
        history_request_timeout=_float("HISTORY_REQUEST_TIMEOUT", 45.0, minimum=1.0),
        history_settle_seconds=_float("HISTORY_SETTLE_SECONDS", 8.0, minimum=1.0),
        max_on_demand_concurrency=_int("MAX_ON_DEMAND_CONCURRENCY", 1, minimum=1),
        media_download_concurrency=_int("MEDIA_DOWNLOAD_CONCURRENCY", 2, minimum=1),
        media_worker_interval=_float("MEDIA_WORKER_INTERVAL", 20.0, minimum=5.0),
        maintenance_interval_seconds=_float(
            "MAINTENANCE_INTERVAL_SECONDS", 900.0, minimum=0.0
        ),
        gui_enabled=_bool("GUI_ENABLED", True),
        terminal_progress_enabled=_bool("TERMINAL_PROGRESS_ENABLED", True),
        compat_wa_version=_bool("COMPAT_WA_VERSION", True),
        wa_version_fetch_timeout=_float("WA_VERSION_FETCH_TIMEOUT", 10.0, minimum=1.0),
        compat_windows_store=_bool("COMPAT_WINDOWS_STORE", True),
        compat_pairing_515=_bool("COMPAT_PAIRING_515", True),
        pairing_515_timeout=_float("PAIRING_515_TIMEOUT", 20.0, minimum=1.0),
        compat_prekey_replay=_bool("COMPAT_PREKEY_REPLAY", True),
        compat_history_messages=_bool("COMPAT_HISTORY_MESSAGES", True),
    )

    _validate(settings)
    return settings


def _validate(settings: Settings) -> None:
    """Comprobaciones que no dependen de servicios externos."""
    valid_levels = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
    if settings.log_level not in valid_levels:
        raise ConfigError(
            f"LOG_LEVEL={settings.log_level!r} invalido. Usa uno de: {sorted(valid_levels)}"
        )

    if not settings.database_url.startswith("postgresql"):
        raise ConfigError(
            "DATABASE_URL debe apuntar a PostgreSQL "
            "(se esperaba un esquema 'postgresql+psycopg://...'). "
            "Este proyecto no admite SQLite como base de datos del backup."
        )
