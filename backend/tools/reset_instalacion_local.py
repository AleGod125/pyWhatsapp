"""Deja la instalacion local virgen: sin usuarios, sin cuentas, sin sesiones.

QUE BORRA
---------
Los DATOS de la aplicacion: usuarios, sus sesiones web, sus credenciales de
Google, sus cuentas de WhatsApp, membresias, chats, contactos, mensajes,
multimedia, historial y todo lo que cuelgue de ellos. Y las identidades de
WhatsApp del disco, incluidas las de ``session/accounts/``.

QUE NO BORRA, NUNCA
-------------------
La ESTRUCTURA. Ni tablas, ni columnas, ni indices, ni restricciones, ni
funciones, ni extensiones, ni ``alembic_version`` -- tocarla obligaria a
recrear la base a mano y perderia el punto de migracion. Tampoco el codigo ni
la configuracion.

COMO BORRA
----------
Un solo ``DELETE FROM users`` en cascada. Las claves ajenas ya estan puestas
con ``ON DELETE CASCADE``, asi que arrastran todo lo que pertenece a esas
personas por el camino que define el propio esquema. Borrar tabla por tabla en
un orden inventado es como se acaba dejando filas huerfanas que luego nadie
sabe de quien eran.

Lo que no cuelga de un usuario --``app_state``, ``scanned_blobs``-- se limpia
aparte: es estado de sesiones que ya no existen.

LAS GUARDAS
-----------
* Se niega si el servicio esta corriendo: borrar la sesion bajo sus pies
  corrompe el Signal Store que tenga abierto.
* Por defecto solo SIMULA y ensena el recuento de lo que se llevaria.
* Para borrar de verdad hay que escribirlo entero: ``--confirmar BORRAR_TODO``.

USO
---
::

    py tools/reset_instalacion_local.py                        # simula
    py tools/reset_instalacion_local.py --confirmar BORRAR_TODO
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import load_settings  # noqa: E402
from app.core.database import Database  # noqa: E402

#: Lo que se cuenta antes y despues. El orden es el de lectura, no el de
#: borrado: el borrado lo decide la cascada del esquema.
TABLAS = (
    "users",
    "user_sessions",
    "user_storage_keys",
    "google_credentials",
    "google_drive_storage",
    "drive_folders",
    "whatsapp_accounts",
    "user_whatsapp_memberships",
    "chats",
    "contacts",
    "messages",
    "media_files",
    "chat_history_state",
    "history_seeds",
    "history_requests",
    "message_segments",
    "storage_jobs",
    "scanned_blobs",
    "app_state",
)

#: Las piezas de sesion que pueden quedar sueltas en `session/`.
#:
#: Identidad y registro de establecimientos van JUNTOS o no va ninguno: un
#: `device.json` nuevo sobre un `compat_prekey.db` viejo produce un dispositivo
#: que no descifra nada.
SUELTOS = ("device.json", "device.json.signal.db", "compat_prekey.db")
WAL_SHM = ("-wal", "-shm", "-journal")

#: Estructura. Se nombra explicitamente para que quede dicho que NO se toca.
INTOCABLES = ("alembic_version",)


def _servicio_corriendo(base: Path) -> dict | None:
    cerrojo = base / "runtime.lock"
    if not cerrojo.is_file():
        return None
    try:
        datos = json.loads(cerrojo.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"pid": "?"}
    pid = datos.get("pid")
    if not isinstance(pid, int):
        return datos
    try:
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return datos
        return None
    except Exception:  # noqa: BLE001 - fuera de Windows, se asume vivo
        return datos


def _recuento(db) -> dict[str, int]:
    from sqlalchemy import text

    salida: dict[str, int] = {}
    with db.transaction() as sesion:
        for tabla in TABLAS:
            try:
                salida[tabla] = sesion.execute(
                    text(f"SELECT count(*) FROM {tabla}")
                ).scalar()
            except Exception:  # noqa: BLE001 - una tabla que no exista no importa
                salida[tabla] = -1
    return salida


def _identidades(base: Path) -> list[Path]:
    """Las identidades de WhatsApp que hay en disco.

    La carpeta plana cuenta si tiene CUALQUIER pieza de sesion, no solo
    ``device.json``.

    Se midio: con la sesion ya migrada a ``accounts/``, la carpeta plana se
    quedaba sin ``device.json`` pero conservaba su ``compat_prekey.db``, y este
    reset lo daba por inexistente. Sobrevivia al borrado un registro de
    establecimientos huerfano --que dice que prekeys se han consumido-- y la
    vinculacion siguiente nacia con identidad nueva sobre ese registro viejo.
    Es exactamente la mezcla que produce "unknown one-time pre-key".
    """
    encontradas = []
    if any((base / f"{n}{s}").is_file() for n in SUELTOS for s in ("",) + WAL_SHM):
        encontradas.append(base)
    cuentas = base / "accounts"
    if cuentas.is_dir():
        for carpeta in sorted(cuentas.iterdir()):
            if carpeta.is_dir():
                encontradas.append(carpeta)
    return encontradas


def main() -> int:
    argumentos = argparse.ArgumentParser(description=__doc__)
    argumentos.add_argument("--confirmar", default="", help="escribe BORRAR_TODO")
    opciones = argumentos.parse_args()

    ajustes = load_settings()
    base = Path(ajustes.session_dir)

    vivo = _servicio_corriendo(base)
    if vivo is not None:
        print(f"ABORTADO: el servicio esta corriendo (pid={vivo.get('pid')}).")
        print("Borrar la sesion bajo sus pies corrompe el Signal Store abierto.")
        return 2

    db = Database(ajustes)
    db.connect()
    antes = _recuento(db)

    print("=== FILAS QUE SE VAN A ELIMINAR ===")
    for tabla, cuantas in antes.items():
        if cuantas > 0:
            print(f"  {tabla:30} {cuantas}")
    total = sum(c for c in antes.values() if c > 0)
    print(f"  {'TOTAL':30} {total}")

    print("\n=== IDENTIDADES DE WHATSAPP EN DISCO ===")
    identidades = _identidades(base)
    for carpeta in identidades:
        ficheros = sorted(f.name for f in carpeta.glob("*") if f.is_file())
        print(f"  {carpeta}")
        for nombre in ficheros:
            print(f"      {nombre}")
    if not identidades:
        print("  (ninguna)")

    print("\n=== LO QUE NO SE TOCA ===")
    print(f"  estructura: tablas, indices, restricciones, funciones, extensiones")
    print(f"  {', '.join(INTOCABLES)} (el punto de migracion se conserva)")

    if opciones.confirmar != "BORRAR_TODO":
        print("\nSIMULACION. No se ha borrado nada.")
        print("Para borrar de verdad:")
        print("  py tools/reset_instalacion_local.py --confirmar BORRAR_TODO")
        return 0

    # -- Datos --------------------------------------------------------------
    from sqlalchemy import text

    with db.transaction() as sesion:
        # Una sola cascada. El esquema ya sabe que cuelga de quien; recorrer
        # tablas en un orden inventado deja huerfanos.
        sesion.execute(text("DELETE FROM users"))
        # Y lo que no cuelga de un usuario.
        #
        # `history_requests` no se arrastra con la cascada: su clave ajena a
        # `chats` es ON DELETE SET NULL, asi que al borrar los chats las filas
        # sobreviven con `chat_id` a nulo. Quedan huerfanas y ya no se pueden
        # atribuir a nadie, asi que se van con su cuenta.
        for tabla in ("history_requests", "app_state", "scanned_blobs"):
            try:
                sesion.execute(text(f"DELETE FROM {tabla}"))
            except Exception:  # noqa: BLE001
                pass

    # -- Identidades de WhatsApp --------------------------------------------
    borradas = []
    for carpeta in identidades:
        if carpeta == base:
            # Todas las piezas, con sus -wal/-shm. Antes se nombraban dos a
            # mano y se dejaba fuera `device.json.signal.db` cuando no habia
            # `device.json` que lo arrastrara.
            for nombre in SUELTOS:
                for fichero in base.glob(f"{nombre}*"):
                    if fichero.is_file():
                        fichero.unlink()
                        borradas.append(str(fichero))
        else:
            shutil.rmtree(carpeta, ignore_errors=True)
            borradas.append(str(carpeta))
    for suelto in ("runtime.lock",):
        fichero = base / suelto
        if fichero.exists():
            fichero.unlink()
            borradas.append(str(fichero))

    despues = _recuento(db)
    print("\n=== DESPUES ===")
    for tabla, cuantas in despues.items():
        if antes.get(tabla, 0) > 0 or cuantas > 0:
            print(f"  {tabla:30} {antes.get(tabla, 0):>6}  ->  {cuantas}")
    print(f"\n  identidades eliminadas: {len(borradas)}")
    print(f"  quedan en disco: {len(_identidades(base))}")

    with db.transaction() as sesion:
        revision = sesion.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar()
    print(f"  revision de migracion (intacta): {revision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
