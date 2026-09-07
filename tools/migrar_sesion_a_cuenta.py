"""Mueve la sesion suelta de ``session/`` a la carpeta de su cuenta.

QUE HACE, EXACTAMENTE
---------------------
::

    session/device.json            ->  session/accounts/<id>/device.json
    session/device.json.signal.db  ->  session/accounts/<id>/device.json.signal.db
    session/compat_prekey.db       ->  session/accounts/<id>/compat_prekey.db
    (y sus -wal / -shm, que viajan con su base)

Y actualiza ``whatsapp_accounts.session_storage_key`` para que la base sepa
donde ha quedado.

QUE NO HACE, NUNCA
------------------
No copia: **mueve**. No regenera. No borra. No vuelve a emparejar. No genera un
codigo QR. La identidad de WhatsApp y su Signal Store son los mismos ficheros
antes y despues: perderlos significaria perder todo el historial que solo se
puede pedir con esa identidad.

LAS GUARDAS
-----------
Se niega a hacer nada si:

* el servicio esta corriendo -- mover un SQLite abierto lo corrompe;
* la cuenta no se puede determinar sin ambiguedad;
* el destino ya contiene otra identidad.

Por defecto solo SIMULA. Para mover de verdad hay que pedirlo con
``--ejecutar``.

USO
---
::

    py tools/migrar_sesion_a_cuenta.py              # simula y explica
    py tools/migrar_sesion_a_cuenta.py --ejecutar   # mueve
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import load_settings  # noqa: E402
from app.core.database import Database  # noqa: E402
from app.core.session_paths import (  # noqa: E402
    FICHEROS_DE_SESION,
    SUFIJOS_DE_SQLITE,
    MigracionAmbigua,
    carpeta_de_cuenta,
    hay_sesion_en,
    migrar_sesion_plana,
)


def _servicio_corriendo(base: Path) -> dict | None:
    """El cerrojo del servicio, si lo tiene alguien vivo."""
    cerrojo = base / "runtime.lock"
    if not cerrojo.is_file():
        return None
    try:
        datos = json.loads(cerrojo.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"pid": "?", "owner": "?"}

    pid = datos.get("pid")
    if not isinstance(pid, int):
        return datos
    # ¿Sigue vivo ese proceso? Un cerrojo huerfano no puede bloquear para
    # siempre, pero uno vivo tiene los SQLite abiertos.
    try:
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return datos
        return None
    except Exception:  # noqa: BLE001 - fuera de Windows, se asume vivo
        return datos


def _inventario(carpeta: Path) -> list[tuple[str, int]]:
    """Que ficheros de sesion hay y cuanto ocupan. Para comparar despues."""
    salida = []
    for nombre in FICHEROS_DE_SESION:
        for sufijo in ("",) + SUFIJOS_DE_SQLITE:
            fichero = carpeta / f"{nombre}{sufijo}"
            if fichero.exists():
                salida.append((fichero.name, fichero.stat().st_size))
    return sorted(salida)


def _identidad_en_disco(base: Path) -> tuple[str | None, str | None]:
    """El PN y el LID que declara ``device.json``. Ningun secreto sale de aqui."""
    try:
        datos = json.loads((base / "device.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None, None
    jid = datos.get("jid") or {}
    usuario = jid.get("user")
    pn = f"{usuario}@{jid.get('server', 's.whatsapp.net')}" if usuario else None
    return pn, (datos.get("lid") or None)


def _de_quien_es(base: Path, cuentas: list) -> tuple:
    """A que cuenta pertenece la sesion suelta. Por EVIDENCIA, no por descarte.

    Se pregunta primero a la propia identidad: ``device.json`` dice su PN y su
    LID, y la base guarda los de cada cuenta. Si coincide con UNA, es esa, y da
    igual cuantas cuentas haya.

    Solo si la identidad no decide se recurre a "hay una sola cuenta, asi que
    no puede ser de otra". Con dos o mas y sin coincidencia se devuelve
    ``None``: mejor no migrar que migrar a la cuenta equivocada, porque eso le
    entrega a alguien la conversacion de otro.
    """
    pn, lid = _identidad_en_disco(base)
    if pn or lid:
        coinciden = [
            fila
            for fila in cuentas
            if (pn and fila[2] == pn) or (lid and fila[3] == lid)
        ]
        if len(coinciden) == 1:
            fila = coinciden[0]
            return fila[0], fila[1], "su identidad coincide con la de device.json"

    if len(cuentas) == 1:
        fila = cuentas[0]
        return fila[0], fila[1], "es la unica cuenta que existe"

    return None, None, ""


def main() -> int:
    argumentos = argparse.ArgumentParser(description=__doc__)
    argumentos.add_argument(
        "--ejecutar",
        action="store_true",
        help="mover de verdad (por defecto solo simula)",
    )
    opciones = argumentos.parse_args()

    ajustes = load_settings()
    base = Path(ajustes.session_dir)

    print(f"Carpeta de sesion: {base}")

    # -- Guarda 1: el servicio no puede estar corriendo ---------------------
    vivo = _servicio_corriendo(base)
    if vivo is not None:
        print(
            f"\nABORTADO: el servicio esta corriendo (pid={vivo.get('pid')}, "
            f"desde {vivo.get('acquired_at', '?')})."
        )
        print(
            "Mover un SQLite que esta abierto lo corrompe, y el Signal Store "
            "no se puede reconstruir. Para el servicio y vuelve a intentarlo."
        )
        return 2

    if not hay_sesion_en(base):
        print("\nNo hay sesion suelta que mover: o ya se migro, o nunca la hubo.")
        return 0

    antes = _inventario(base)
    print(f"\nFicheros a mover ({len(antes)}):")
    for nombre, tamano in antes:
        print(f"  {nombre:34} {tamano:>10} bytes")

    # -- Guarda 2: de quien es esta sesion ----------------------------------
    db = Database(ajustes)
    db.connect()
    from sqlalchemy import select, update

    from app.models import WhatsAppAccount

    with db.transaction() as sesion:
        cuentas = sesion.execute(
            select(
                WhatsAppAccount.id,
                WhatsAppAccount.session_storage_key,
                WhatsAppAccount.wa_pn,
                WhatsAppAccount.wa_lid,
            )
        ).all()

    cuenta_id, clave_actual, como = _de_quien_es(base, cuentas)
    if cuenta_id is None:
        print(
            f"\nABORTADO: hay {len(cuentas)} cuenta(s) y la identidad de la "
            "sesion no coincide con ninguna sola. Atribuirla a la equivocada "
            "le entrega a alguien la conversacion de otro."
        )
        return 2

    print(f"\nDe quien es esta sesion: {como}")
    destino = carpeta_de_cuenta(ajustes, cuenta_id)
    print(f"\nCuenta:  {cuenta_id}")
    print(f"Destino: {destino}")
    print(f"session_storage_key: {clave_actual}  ->  accounts/{cuenta_id}")

    if not opciones.ejecutar:
        print("\nSIMULACION. No se ha movido nada.")
        print("Para hacerlo de verdad: py tools/migrar_sesion_a_cuenta.py --ejecutar")
        return 0

    # -- Mover --------------------------------------------------------------
    try:
        movida = migrar_sesion_plana(ajustes, [cuenta_id])
    except MigracionAmbigua as fallo:
        print(f"\nABORTADO: {fallo}")
        return 2

    despues = _inventario(destino)
    print(f"\nMovido a {movida}")
    for nombre, tamano in despues:
        print(f"  {nombre:34} {tamano:>10} bytes")

    if antes != despues:
        print(
            "\nATENCION: el inventario no coincide. NO se ha borrado nada; "
            "revisa las dos carpetas antes de arrancar el servicio."
        )
        return 3

    # -- Y la base apunta al sitio nuevo ------------------------------------
    with db.transaction() as sesion:
        sesion.execute(
            update(WhatsAppAccount)
            .where(WhatsAppAccount.id == cuenta_id)
            .values(session_storage_key=f"accounts/{cuenta_id}")
        )

    print("\nListo. Los mismos ficheros, mismo tamano, en su carpeta de cuenta.")
    print("Arranca el servicio: la sesion se recupera sin escanear nada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
