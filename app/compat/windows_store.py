"""Persistencia del DeviceStore en Windows.

BUG VERIFICADO (pywhats 0.2.0, ``pywhats/store.py:359``)::

    fd, tmp_path = tempfile.mkstemp(...)
    os.fchmod(fd, 0o600)          # <-- no existe en Windows

Comprobacion empirica en Windows 11 + Python 3.11.9::

    >>> hasattr(os, "fchmod")
    False
    >>> save_device_store(make_fresh_device(), "device.json")
    AttributeError: module 'os' has no attribute 'fchmod'

Consecuencia real: el pairing llega a ``pair-success verified`` y revienta al
guardar la sesion, de modo que el dispositivo queda vinculado en el telefono
pero la PC no conserva credenciales y vuelve a pedir QR.

Este parche reimplementa ``save_device_store`` conservando exactamente la misma
semantica de escritura atomica (mkstemp en el mismo directorio -> permisos ->
write -> flush -> fsync -> os.replace -> limpieza -> fsync del directorio) y
solo omite la llamada POSIX en plataformas que no la tienen. En POSIX se sigue
aplicando 0600, porque ahi los permisos si se respetan.

El camino de LECTURA no necesita parche: ``_check_permissions`` (store.py:322)
ya hace ``if os.name != "posix": return``.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger

log = get_logger("COMPAT")

_MARKER = "_whatsapp_backup_windows_patch"


def _safe_save_device_store(store: Any, path: str | os.PathLike[str]) -> None:
    """Escritura atomica del DeviceStore, valida en Windows y en POSIX."""
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(store.to_json(), indent=2, sort_keys=True).encode("utf-8")

    # Temporal en el MISMO directorio: os.replace solo es atomico dentro del
    # mismo sistema de archivos.
    fd, tmp_path = tempfile.mkstemp(
        prefix=".devstore-", suffix=".tmp", dir=str(destination.parent)
    )
    fd_owned = True  # nadie ha envuelto todavia el descriptor
    try:
        # Unica diferencia con el original: os.fchmod solo donde existe.
        if hasattr(os, "fchmod"):
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)  # 0600

        fd_owned = False  # a partir de aqui lo cierra el context manager
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, destination)
    except Exception:
        # En Windows no se puede borrar un archivo con un descriptor abierto,
        # asi que hay que cerrarlo antes de intentar la limpieza. Sin esto, un
        # fallo temprano dejaria un .devstore-*.tmp huerfano (que es justo lo
        # que hace el save_device_store original al reventar en os.fchmod).
        if fd_owned:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                pass
        raise

    # fsync del directorio para que el rename sea duradero. En Windows no se
    # puede abrir un directorio con os.open: el OSError se ignora, igual que
    # hace el original con los sistemas de archivos que no lo soportan.
    try:
        dir_fd = os.open(str(destination.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def is_needed() -> bool:
    """``True`` si la plataforma carece de ``os.fchmod``."""
    return not hasattr(os, "fchmod")


def apply() -> bool:
    """Instala el parche. Devuelve ``True`` si se aplico.

    Es idempotente: una segunda llamada no vuelve a envolver nada.
    """
    import pywhats.store

    current = pywhats.store.save_device_store
    if getattr(current, _MARKER, False):
        return True

    if not is_needed():
        log.debug("os.fchmod disponible; no hace falta el parche de persistencia")
        return False

    setattr(_safe_save_device_store, _MARKER, True)
    # Un unico punto de parcheo: DeviceStore.save() (store.py:308) resuelve
    # save_device_store como global del modulo en tiempo de llamada, asi que
    # ambos caminos quedan cubiertos.
    pywhats.store.save_device_store = _safe_save_device_store

    log.info("Adaptacion de persistencia Windows aplicada")
    return True
