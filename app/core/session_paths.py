"""Donde vive la sesion de CADA cuenta de WhatsApp.

EL PROBLEMA
-----------
Hoy hay una sola carpeta::

    session/
        device.json
        device.json.signal.db
        compat_prekey.db

Con una cuenta funciona. Con dos, la segunda escribiria encima de la identidad
de la primera: mismo ``device.json``, mismo Signal Store, mismas claves. No es
que se mezclen datos -- es que una cuenta dejaria de existir.

LA FORMA NUEVA
--------------
::

    session/
        accounts/
            <whatsapp_account_id>/
                device.json
                device.json.signal.db
                compat_prekey.db

El nombre de la carpeta es el identificador de la cuenta, que es estable y no
dice nada de nadie. **Nunca el correo ni el telefono**: el disco no es sitio
para un dato personal, y ademas los dos pueden cambiar.

LA MIGRACION DE LA SESION QUE YA EXISTE
---------------------------------------
Hay una sesion viva que costo tres sesiones de trabajo dejar en pie. Se mueve,
no se regenera: volver a escanear un codigo QR seria perder el Signal Store y
con el todo el historial que solo se puede pedir con esa identidad.

Y se mueve **solo cuando no hay duda**: una sola sesion plana, una sola cuenta
vinculada, y el destino vacio. Con cualquier otra combinacion se aborta con el
diagnostico delante. Adivinar de quien es una identidad de WhatsApp es
exactamente lo que no se puede hacer.
"""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("APP")

#: Los ficheros que forman UNA identidad de WhatsApp.
#:
#: Se mueven juntos o no se mueve ninguno. El ``device.json`` sin su Signal
#: Store es una identidad sin claves: el servidor la acepta y despues no se
#: puede descifrar nada.
FICHEROS_DE_SESION = (
    "device.json",
    "device.json.signal.db",
    "compat_prekey.db",
)

#: SQLite deja estos al lado mientras la base esta abierta. Copiar el ``.db``
#: sin ellos puede dejar fuera lo ultimo escrito.
SUFIJOS_DE_SQLITE = ("-wal", "-shm", "-journal")

CARPETA_DE_CUENTAS = "accounts"


def carpeta_de_cuenta(settings: Any, account_id: Any) -> Path:
    """La carpeta de sesion de esa cuenta. No la crea."""
    return Path(settings.session_dir) / CARPETA_DE_CUENTAS / str(account_id)


def ajustes_de_cuenta(settings: Any, account_id: Any) -> Any:
    """Unos ``Settings`` que apuntan a la sesion de ESA cuenta.

    Es lo que permite que cada runtime sea el de siempre sin cambiarle nada:
    ``AppRuntime`` ya toma su carpeta de ``settings.session_dir``, asi que
    darle unos ajustes con otra carpeta basta para aislarlo entero --
    identidad, Signal Store y registro de establecimientos incluidos.
    """
    destino = carpeta_de_cuenta(settings, account_id)
    destino.mkdir(parents=True, exist_ok=True)
    return dataclasses.replace(settings, session_dir=destino)


def hay_sesion_en(carpeta: Path) -> bool:
    """Si esa carpeta contiene una identidad de WhatsApp utilizable."""
    return (carpeta / "device.json").is_file()


class MigracionAmbigua(RuntimeError):
    """No se puede saber de quien es la sesion plana. No se adivina."""


def identidad_de(carpeta: Path) -> tuple[str | None, str | None]:
    """El PN y el LID que declara el ``device.json`` de esa carpeta.

    Sirve para saber DE QUIEN es una sesion sin adivinarlo: la base guarda el
    PN y el LID de cada cuenta, asi que basta comparar. Ninguna clave privada
    sale de aqui -- solo los dos identificadores publicos.

    ``(None, None)`` si no hay identidad legible.
    """
    import json

    try:
        datos = json.loads((carpeta / "device.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - sin identidad legible no se concluye nada
        return None, None
    jid = datos.get("jid") or {}
    usuario = jid.get("user")
    pn = f"{usuario}@{jid.get('server', 's.whatsapp.net')}" if usuario else None
    return pn, (datos.get("lid") or None)


def migrar_sesion_plana(settings: Any, cuentas: list[Any]) -> Path | None:
    """Mueve la sesion suelta de ``session/`` a la carpeta de su cuenta.

    Devuelve la carpeta destino si movio algo, ``None`` si no habia nada que
    hacer. **Idempotente**: la segunda vez no encuentra sesion plana y sale.

    :raises MigracionAmbigua: si hay sesion plana y el destino no es
        inequivoco -- ninguna cuenta, varias cuentas, o un destino que ya
        tiene otra identidad dentro. Sobrescribir ahi seria destruir la sesion
        de alguien.
    """
    base = Path(settings.session_dir)
    if not hay_sesion_en(base):
        return None  # ya migrada, o nunca hubo

    if len(cuentas) != 1:
        raise MigracionAmbigua(
            f"hay una sesion suelta en {base} y {len(cuentas)} cuenta(s) "
            "vinculadas. No se mueve: atribuir una identidad de WhatsApp a la "
            "cuenta equivocada le entrega a alguien la conversacion de otro."
        )

    destino = carpeta_de_cuenta(settings, cuentas[0])
    if hay_sesion_en(destino):
        raise MigracionAmbigua(
            f"{destino} ya contiene una identidad. No se sobrescribe: seria "
            "destruir una sesion que puede ser la buena."
        )

    destino.mkdir(parents=True, exist_ok=True)
    movidos: list[str] = []
    for nombre in FICHEROS_DE_SESION:
        origen = base / nombre
        if not origen.exists():
            continue
        # El fichero y sus acompanantes de SQLite, juntos. Mover el `.db` solo
        # puede dejar fuera lo ultimo escrito, que es justo lo que hace falta.
        for sufijo in ("",) + SUFIJOS_DE_SQLITE:
            pieza = base / f"{nombre}{sufijo}"
            if pieza.exists():
                shutil.move(str(pieza), str(destino / pieza.name))
                movidos.append(pieza.name)

    log.info(
        "[APP] sesion movida a %s (%d fichero(s)): la identidad se conserva, "
        "no hay que volver a escanear nada",
        destino,
        len(movidos),
    )
    return destino
