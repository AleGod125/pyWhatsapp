"""Apartar la sesion de WhatsApp cuando deja de servir.

QUE HACE
--------
Mueve la identidad a ``diagnostics/`` en vez de borrarla. Se archiva, no se
tira: una sesion que dejo de funcionar es la mejor prueba de por que dejo de
funcionar, y recuperarla cuesta un codigo QR.

NUNCA LANZA, Y ESO SE MIDIO
---------------------------
Al archivar tras un 401, Windows devolvia ``WinError 32`` sobre un fichero que
nuestro propio proceso tenia abierto. La excepcion abortaba el resto del
manejo del error y el sistema entraba en un bucle de reintentos.

Si un archivo esta bloqueado se salta y se deja constancia: con que salga de
en medio lo que identifica al dispositivo ya basta para que el siguiente
arranque vincule limpio.

LA CARPETA ES UNA UNIDAD
------------------------
Credenciales y almacen de Signal viven juntos y se mueven juntos. Llevarse
medio deja un dispositivo NUEVO usando ratchets VIEJOS, y el sintoma es
``unknown one-time pre-key id`` con peticiones de historial que reciben ACK y
despues nada. Es el fallo que mas costo diagnosticar, asi que aqui se recorre
la carpeta ENTERA -- ficheros y subcarpetas.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("WA")


def _carpeta(settings: Any) -> Path:
    """La carpeta de la vinculacion, y SOLO esa.

    Se apunta a ``session_dir_baileys`` y no a ``session_dir`` a proposito:
    el segundo es la carpeta de la instalacion y ahi dentro cuelga tambien
    ``accounts/``. Archivar el padre se llevaria por delante las sesiones de
    LOS DEMAS usuarios, que no tienen nada que ver con este 401.
    """
    directa = getattr(settings, "session_dir_baileys", None)
    if directa is not None:
        return Path(directa)
    return Path(settings.session_dir) / "baileys"


def hay_sesion(settings: Any) -> bool:
    """Si hay algo que archivar."""
    carpeta = _carpeta(settings)
    if not carpeta.is_dir():
        return False
    return any(carpeta.iterdir())


#: Lo que un nombre de carpeta puede llevar. Todo lo demas se sustituye.
_PERMITIDO = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
)


def _para_nombre(motivo: Any) -> str:
    r"""El motivo, convertido en algo que Windows admita como carpeta.

    EL FALLO, Y COMO SE VEIA
    ------------------------
    El motivo se metia en el nombre TAL CUAL. Con pywhats era un numero
    --``401``-- y funcionaba. Baileys manda un diccionario::

        {'reason': 'loggedOut'}

    y ``{``, ``'``, ``:`` y ``}`` son ilegales en un nombre de fichero de
    Windows. El resultado::

        NotADirectoryError: [WinError 267] El nombre del directorio no es
        valido: "...\session-20260909-210626-revoked-{'reason': 'loggedOut'}"

    Y lo que provocaba era mucho peor que un error feo: archivar fallaba, las
    credenciales revocadas se quedaban en disco, y en cada arranque la cuenta
    volvia a intentar entrar con una sesion que el servidor ya habia
    rechazado. Un bucle del que no se sale solo.

    Se recorta ademas a 40 caracteres: el motivo es una pista, no un informe,
    y una ruta demasiado larga vuelve a fallar en Windows por otro sitio.
    """
    texto = str(motivo)
    limpio = "".join(c if c in _PERMITIDO else "-" for c in texto).strip("-")
    # Varios seguidos quedan feos y no aportan nada.
    while "--" in limpio:
        limpio = limpio.replace("--", "-")
    return limpio[:40] or "desconocido"


def archive_session(settings: Any, reason: str) -> Path | None:
    """Mueve la sesion actual a ``diagnostics/``. Devuelve donde, o ``None``.

    ``None`` significa que no habia nada que archivar, y en ese caso no deja
    carpeta vacia detras: se llegaron a crear 99 en un bucle de reintentos.
    """
    if not hay_sesion(settings):
        return None

    sello = datetime.now().strftime("%Y%m%d-%H%M%S")
    destino = Path(settings.diagnostics_dir) / f"session-{sello}-{_para_nombre(reason)}"
    destino.mkdir(parents=True, exist_ok=True)

    movidos: list[str] = []
    bloqueados: list[str] = []
    for ruta in sorted(_carpeta(settings).iterdir()):
        try:
            if ruta.is_dir():
                # La carpeta de credenciales va entera: media carpeta es peor
                # que ninguna.
                shutil.move(str(ruta), str(destino / ruta.name))
            else:
                ruta.replace(destino / ruta.name)
            movidos.append(ruta.name)
        except OSError as fallo:
            # Bloqueado por otro proceso (o por nosotros). No es motivo para
            # abortar.
            bloqueados.append(ruta.name)
            log.debug("No se pudo archivar %s: %s", ruta.name, fallo)

    if not movidos:
        try:
            destino.rmdir()
        except OSError:  # pragma: no cover
            pass
        log.warning(
            "No se pudo archivar la sesion (%d elementos bloqueados)", len(bloqueados)
        )
        return None

    log.info(
        "Sesion archivada en %s (%d elementos: %s%s)",
        destino,
        len(movidos),
        ", ".join(movidos),
        f"; bloqueados: {', '.join(bloqueados)}" if bloqueados else "",
    )
    return destino
