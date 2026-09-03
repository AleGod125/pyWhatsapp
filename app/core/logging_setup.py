"""Logging con etiquetas por subsistema y un filtro anti-secretos.

Formato de los mensajes de la aplicacion:

    [APP] Iniciando...
    [DB]  PostgreSQL disponible

Etiquetas en uso: APP, CONFIG, DB, WA, PAIRING, QR, SIGNAL, SYNC, BACKFILL,
MEDIA, GUI, COMPAT.

El filtro :class:`SecretRedactionFilter` es una red de seguridad de ultimo
recurso, no una excusa para loguear material sensible: la regla sigue siendo
no pasar secretos al logger.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

# Etiquetas conocidas. Se exponen como helpers ``log_app``, ``log_db``, ...
TAGS = (
    "APP",
    "CONFIG",
    "DB",
    "WA",
    "PAIRING",
    "QR",
    "SIGNAL",
    "SYNC",
    "BACKFILL",
    "MEDIA",
    "GUI",
    "API",
    "SSE",
    "COMPAT",
    # Mensajes en vivo. Tiene etiqueta propia para que un problema de
    # multimedia o de excavacion no se confunda con uno de recepcion: cuando
    # todo se mezclaba bajo [APP] costaba ver donde moria un mensaje.
    "LIVE",
)

# Patrones redactados si se cuelan en un mensaje ya formateado.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # password dentro de una URL de conexion
    (re.compile(r"(://[^:/@\s]+):([^@\s]+)@"), r"\1:***@"),
    # asignaciones tipo password=..., secret=..., token=..., api_key=...
    (
        re.compile(
            r"\b(password|passwd|pwd|secret|token|api_key|apikey)\s*[=:]\s*(\S+)",
            re.IGNORECASE,
        ),
        r"\1=***",
    ),
)


class SecretRedactionFilter(logging.Filter):
    """Redacta secretos evidentes del mensaje ya formateado."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - un log roto no debe tumbar la app
            return True

        redacted = message
        for pattern, replacement in _REDACTIONS:
            redacted = pattern.sub(replacement, redacted)

        if redacted != message:
            # Se reemplaza el mensaje ya interpolado y se descartan los args
            # para que el formatter no vuelva a interpolar el original.
            record.msg = redacted
            record.args = ()
        return True


class TaggedFormatter(logging.Formatter):
    """Antepone ``[TAG]`` usando el sufijo del nombre del logger.

    ``app.db`` -> ``[DB]``; los loggers de terceros (``pywhats.pairing``)
    conservan su nombre completo para no disfrazar su origen.
    """

    def __init__(self, *, show_time: bool) -> None:
        fmt = "%(asctime)s %(tagged)s %(message)s" if show_time else "%(tagged)s %(message)s"
        super().__init__(fmt=fmt)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """``2026-09-02 21:39:18.245 -05:00``: fecha, milisegundos y desfase.

        Con la hora a secas no se podian cruzar los tiempos del telefono, de
        WhatsApp Web, del backend y de PostgreSQL. Hacen falta las tres cosas:

        * la FECHA, porque una sesion cruza la medianoche;
        * los MILISEGUNDOS, porque los mensajes de una rafaga caen en el mismo
          segundo y el orden importa;
        * el DESFASE, porque las capturas del telefono son hora local y los
          timestamps de WhatsApp son UTC.

        Ojo con no confundir dos cosas distintas: esta es la hora a la que se
        ESCRIBE la linea, no la hora del mensaje de WhatsApp.
        """
        from datetime import datetime, timezone

        momento = datetime.fromtimestamp(record.created, timezone.utc).astimezone()
        if datefmt:
            return momento.strftime(datefmt)
        desfase = momento.strftime("%z")  # +HHMM
        desfase = f"{desfase[:3]}:{desfase[3:]}" if desfase else ""
        return f"{momento.strftime('%Y-%m-%d %H:%M:%S')}.{momento.microsecond // 1000:03d} {desfase}"

    def format(self, record: logging.LogRecord) -> str:
        name = record.name
        if name.startswith("app."):
            tag = name.split(".", 1)[1].split(".")[0].upper()
            record.tagged = f"[{tag}]"
        elif name == "app":
            record.tagged = "[APP]"
        else:
            record.tagged = f"[{name}]"

        if record.levelno >= logging.WARNING:
            record.tagged = f"{record.tagged} {record.levelname}:"
        return super().format(record)


def setup_logging(
    level: str = "INFO",
    *,
    log_file: Path | None = None,
    show_time: bool = True,
    quiet_libraries: bool = True,
) -> None:
    """Configura el logging raiz. Idempotente: reemplaza los handlers previos.

    :param level: nivel para los loggers de la aplicacion.
    :param log_file: si se indica, ademas escribe a ese archivo (siempre DEBUG).
    :param show_time: incluir hora en la salida de consola. Por defecto SI:
        sin ella no se puede cruzar lo que hizo el backend con lo que se ve en
        el telefono, y eso ha costado varias rondas de diagnostico.
    :param quiet_libraries: subir a WARNING los loggers ruidosos de terceros.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(logging.DEBUG)  # el filtrado real ocurre por handler
    redaction = SecretRedactionFilter()

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(getattr(logging, level, logging.INFO))
    console.setFormatter(TaggedFormatter(show_time=show_time))
    console.addFilter(redaction)
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        file_handler.addFilter(redaction)
        root.addHandler(file_handler)

    if quiet_libraries:
        # SQLAlchemy emite el SQL completo en INFO; websockets es muy verboso.
        for noisy in ("sqlalchemy.engine", "websockets", "websockets.client", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        # pywhats emite un WARNING por cada host de CDN que falla. Con
        # cientos de adjuntos antiguos (404/410) eso son miles de lineas que
        # no aportan nada: el resultado real ya se resume por lotes.
        logging.getLogger("pywhats.media.download").setLevel(logging.ERROR)


def get_logger(tag: str) -> logging.Logger:
    """Devuelve el logger de un subsistema. ``get_logger("DB")`` -> ``[DB]``."""
    normalized = tag.strip().upper()
    if normalized not in TAGS:
        raise ValueError(f"etiqueta de log desconocida: {tag!r}. Conocidas: {TAGS}")
    return logging.getLogger(f"app.{normalized.lower()}")
