"""Correr la prueba end-to-end con Baileys y medirla contra pywhats.

QUE HACE
--------
Un solo comando que deja la cuenta piloto extrayendo por Baileys y, al
terminar, escribe el informe de paridad contra la linea base ya guardada.

    py tools/prueba_baileys.py --comprobar          # solo dice si se puede
    py tools/prueba_baileys.py --minutos 20         # corre la prueba

LO QUE NO HACE, Y ES DELIBERADO
-------------------------------
No vincula nada por su cuenta. Si no hay credenciales de Baileys se para y lo
dice: escanear un QR es una decision del usuario.

Aqui se llego a escribir que esos ``Connection Terminated`` eran un limite por
escanear demasiado. Era FALSO. Al aislarlo se vio que el servidor rechazaba el
perfil de escritorio que anunciaba el worker, asi que fallaba siempre y desde
el primer intento, con o sin espera. Ver ``navegadorAAnunciar()`` en
``wa_baileys/worker.js``.

La sesion de pywhats ya no existe: pywhats se retiro entero y Baileys es el unico proveedor.

POR QUE HACE FALTA UN QR
------------------------
Las credenciales de pywhats NO sirven para Baileys. No es un detalle de
formato: son dos registros de dispositivo companion distintos, cada uno con su
propio material de claves. Reutilizar uno en la otra libreria no es
"convertir" nada, es imposible.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_RAIZ = _Path(__file__).resolve().parent.parent
if str(_RAIZ) not in _sys.path:
    _sys.path.insert(0, str(_RAIZ))

import argparse
import json
import os
import time
from datetime import datetime

from app.core.config import load_settings
from app.core.database import Database, DatabaseError
from app.core.logging_setup import get_logger, setup_logging

log = get_logger("WA")

#: Donde vive la linea base de pywhats con la que se compara.
BASE = _Path("diagnostics/base-pywhats-20260909.json")


def carpeta_de_baileys(settings) -> _Path:
    return _Path(settings.session_dir) / "baileys"


def comprobar(settings) -> tuple[bool, list[str]]:
    """Que hace falta para poder correr. Devuelve (se_puede, avisos)."""
    avisos: list[str] = []
    se_puede = True

    worker = _RAIZ / "wa_baileys"
    if not (worker / "node_modules" / "@whiskeysockets" / "baileys").is_dir():
        avisos.append("FALTA  Baileys sin instalar: cd wa_baileys && npm install")
        se_puede = False
    else:
        version = json.loads(
            (worker / "node_modules" / "@whiskeysockets" / "baileys" / "package.json")
            .read_text(encoding="utf-8")
        )["version"]
        avisos.append(f"ok     Baileys {version} instalado")

    creds = carpeta_de_baileys(settings) / "creds.json"
    if creds.is_file():
        avisos.append(f"ok     credenciales de Baileys en {creds.parent}")
    else:
        avisos.append(
            "FALTA  no hay credenciales de Baileys.\n"
            "       Hay que escanear el QR UNA vez. Las de pywhats no sirven:\n"
            "       son dos registros de dispositivo distintos, cada uno con su\n"
            "       propio material de claves.\n"
            "       Si sale Connection Terminated sin llegar a mostrar el\n"
            "       codigo, NO es un limite por escanear de mas: es el perfil\n"
            "       que anuncia el worker. Ver navegadorAAnunciar() en\n"
            "       wa_baileys/worker.js."
        )
        se_puede = False

    if not BASE.is_file():
        avisos.append(
            f"FALTA  no esta la linea base {BASE}.\n"
            "       Es un fichero historico: se genero con la instalacion\n"
            "       anterior y ya no se puede volver a generar."
        )
        se_puede = False
    else:
        avisos.append(f"ok     linea base de pywhats en {BASE}")

    return se_puede, avisos


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--comprobar", action="store_true", help="solo dice si se puede correr"
    )
    parser.add_argument(
        "--minutos", type=float, default=20.0, help="cuanto dejar extraer antes de medir"
    )
    parser.add_argument("--salida", default="", help="donde escribir el informe")
    args = parser.parse_args(argv)

    settings = load_settings()
    setup_logging(settings.log_level)

    se_puede, avisos = comprobar(settings)
    print("\n=== COMPROBACION PREVIA ===")
    for linea in avisos:
        print("  " + linea)

    if args.comprobar:
        print("\n" + ("Se puede correr la prueba." if se_puede else "Todavia no."))
        return 0 if se_puede else 1

    if not se_puede:
        print("\nNo se corre nada: falta algo de lo de arriba.")
        return 1

    print(
        f"\n=== EXTRAYENDO ({args.minutos:g} min) ===\n"
        "  Arranca `service.py` en otra terminal si no esta ya corriendo.\n"
        "  Aqui solo se espera y se mide.\n"
    )
    fin = time.monotonic() + args.minutos * 60
    while time.monotonic() < fin:
        restante = int(fin - time.monotonic())
        print(f"  faltan {restante // 60}m {restante % 60:02d}s", end="\r", flush=True)
        time.sleep(min(15, max(1, restante)))
    print(" " * 40, end="\r")

    from tools.diff_proveedores import comparar, medir

    try:
        database = Database(settings)
        database.connect()
    except DatabaseError as exc:
        print(f"No se pudo conectar: {exc}")
        return 3
    try:
        with database.transaction() as sesion:
            foto = medir(sesion)
    finally:
        database.dispose()

    base = json.loads(BASE.read_text(encoding="utf-8"))
    comparar(base, foto)
    print(informe_de_paridad(base, foto))

    destino = _Path(
        args.salida or f"diagnostics/baileys-{datetime.now().strftime('%Y%m%d-%H%M')}.json"
    )
    destino.write_text(json.dumps(foto, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nMedicion guardada en {destino}")
    return 0


def informe_de_paridad(base: dict, ahora: dict) -> str:
    """La tabla de paridad, rellena con lo medido."""

    def pct(parte: int, total: int) -> str:
        return f"{round(100 * parte / total)}%" if total else "-"

    b3, a3 = base["G3_nombres_y_lid"], ahora["G3_nombres_y_lid"]
    b1, a1 = base["G1_tanda"], ahora["G1_tanda"]
    b2, a2 = base["G2_marcador_de_fin"], ahora["G2_marcador_de_fin"]

    filas = [
        ("Chats con contenido", base["totales"]["chats"], ahora["totales"]["chats"], ""),
        ("Mensajes", base["totales"]["mensajes"], ahora["totales"]["mensajes"], ""),
        (
            "% individuales con nombre",
            pct(b3["sin_nombre_pero_resuelven_por_contacto"], b3["chats_individuales"]),
            pct(a3["sin_nombre_pero_resuelven_por_contacto"], a3["chats_individuales"]),
            "G3",
        ),
        (
            "contactos con LID conocido",
            b3["contactos_con_lid_conocido"],
            a3["contactos_con_lid_conocido"],
            "G3",
        ),
        (
            "mensajes por respuesta (media)",
            b1["media_por_respuesta"],
            a1["media_por_respuesta"],
            "G1",
        ),
        (
            "mensajes por respuesta (max)",
            b1["maximo_en_una_respuesta"],
            a1["maximo_en_una_respuesta"],
            "G1",
        ),
        (
            "conversaciones con veredicto del telefono",
            b2["con_veredicto_del_telefono"],
            a2["con_veredicto_del_telefono"],
            "G2",
        ),
        ("ventana (dias)", b2["ventana_dias"], a2["ventana_dias"], "G2"),
        ("mensaje mas antiguo", b2["mas_antiguo"], a2["mas_antiguo"], "G2"),
    ]

    ancho = max(len(f[0]) for f in filas) + 2
    lineas = [
        "\n=== PARIDAD ===\n",
        f"  {'metrica'.ljust(ancho)}{'pywhats':>16}{'baileys':>16}   compuerta",
        f"  {'-' * (ancho + 34)}",
    ]
    for etiqueta, antes, despues, puerta in filas:
        lineas.append(
            f"  {etiqueta.ljust(ancho)}{str(antes):>16}{str(despues):>16}   {puerta}"
        )
    lineas.append(
        "\n  G4 (descifrado) no se ve en la base: un mensaje que no se descifra\n"
        "  no llega. Se cuenta en el log:\n"
        "      grep -c 'no se pudo descifrar' diagnostics/app.log"
    )
    return "\n".join(lineas)


if __name__ == "__main__":
    raise SystemExit(main())
