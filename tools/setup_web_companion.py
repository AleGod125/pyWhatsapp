"""Instala las dependencias de Node del Web Companion. A mano, a proposito.

POR QUE NO LO HACE service.py SOLO
----------------------------------
``npm install`` baja codigo de internet y, con whatsapp-web.js, arrastra un
Chromium entero (unos cientos de megas). Eso no puede pasar en silencio dentro
de un arranque: el usuario tiene que poder decidir cuando y saber que se
descargo.

El arranque normal sigue siendo ``py service.py``. Si falta algo, lo dice y
remite aqui.

    py tools/setup_web_companion.py
    py tools/setup_web_companion.py --comprobar   # solo mira, no instala
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent / "web_companion"


def _comprobar() -> list[str]:
    """Lo que falta para poder arrancar el worker."""
    problemas: list[str] = []
    if shutil.which("node") is None:
        problemas.append("Node no esta instalado o no esta en el PATH")
    if shutil.which("npm") is None:
        problemas.append("npm no esta instalado o no esta en el PATH")
    if not (RAIZ / "package.json").exists():
        problemas.append(f"falta {RAIZ / 'package.json'}")
    if not (RAIZ / "node_modules").is_dir():
        problemas.append("faltan las dependencias de Node (node_modules)")
    return problemas


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--comprobar",
        action="store_true",
        help="solo dice que falta; no descarga nada",
    )
    args = parser.parse_args()

    print("=" * 66)
    print("WEB COMPANION - dependencias de Node")
    print("=" * 66)
    print(f"\nCarpeta: {RAIZ}")

    problemas = _comprobar()
    if not problemas:
        print("\nTodo listo. Activalo con WEB_COMPANION_ENABLED=true en tu .env")
        return 0

    print("\nFalta:")
    for problema in problemas:
        print(f"   - {problema}")

    if args.comprobar:
        return 1

    if shutil.which("npm") is None:
        print("\nInstala Node.js (incluye npm) y vuelve a ejecutar esto.")
        return 2

    print(
        "\nSe va a ejecutar 'npm install'. Descarga whatsapp-web.js y un\n"
        "Chromium propio: varios cientos de megas la primera vez.\n"
    )
    respuesta = input("Continuar? [s/N]: ").strip().lower()
    if respuesta not in ("s", "si", "sí", "y", "yes"):
        print("Cancelado. No se ha descargado nada.")
        return 1

    try:
        resultado = subprocess.run(  # noqa: S603 - orden fija, sin entrada del usuario
            ["npm", "install"],
            cwd=str(RAIZ),
            check=False,
            shell=sys.platform == "win32",
        )
    except OSError as exc:
        print(f"\nNo se pudo ejecutar npm: {exc}")
        return 2

    if resultado.returncode != 0:
        print(f"\nnpm install fallo (codigo {resultado.returncode}).")
        return resultado.returncode

    restantes = _comprobar()
    if restantes:
        print("\nSigue faltando:")
        for problema in restantes:
            print(f"   - {problema}")
        return 1

    print("\nListo.")
    print("\nSiguiente paso: pon esto en tu .env")
    print("   WEB_COMPANION_ENABLED=true")
    print("\nY arranca como siempre:  py service.py")
    print("La primera vez pedira SU PROPIO codigo QR: es otro dispositivo")
    print("vinculado, distinto del emparejamiento principal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
