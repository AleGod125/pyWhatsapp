"""¿De verdad quedó limpio? Se comprueba, no se supone. Solo lectura.

POR QUE EXISTE
--------------
Una limpieza a medias es peor que no limpiar: la medición sale, parece válida
y está contaminada con restos de la vinculación anterior. Y eso no se nota
mirando la aplicación, porque una cuenta vieja con dos conversaciones
sobrevivientes se ve igual que una recién creada.

Así que después de limpiar se pregunta, tabla por tabla y archivo por archivo,
qué queda. Lo que quede se enseña; no se borra nada desde aquí.

    py tools/check_clean_state.py
    py tools/check_clean_state.py --json

QUE SE ESPERA (§32)
-------------------
Respecto a WhatsApp: sin vínculo activo, cero conversaciones, cero mensajes,
cero anclas, sin sesión principal y sin LocalAuth del navegador. El usuario,
sus credenciales de Google y las migraciones SI deben seguir ahí: no forman
parte de lo que se mide y volver a crearlos sólo alarga la prueba.

SOLO LECTURA
------------
No borra, no escribe y no toca la red.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, inspect, text  # noqa: E402

from app.core.config import load_settings  # noqa: E402

LINEA = "=" * 68

#: Lo que tiene que estar a cero para que la prueba valga.
TABLAS_QUE_DEBEN_ESTAR_VACIAS = (
    "whatsapp_accounts",
    "chats",
    "messages",
    "history_seeds",
    "history_requests",
    "chat_history_state",
    "media_files",
)

#: Lo que debe SEGUIR estando. Vaciarlo no limpia nada y obliga a repetir el
#: registro y el consentimiento de Google antes de poder medir.
TABLAS_QUE_DEBEN_SEGUIR = ("users", "google_credentials", "alembic_version")


def _archivos(settings) -> list[tuple[str, pathlib.Path, bool]]:
    """Los archivos que delatan una vinculación anterior."""
    sesion = settings.session_dir
    return [
        ("sesion principal (device.json)", settings.session_file, False),
        (
            "Signal Store",
            pathlib.Path(str(settings.session_file) + ".signal.db"),
            False,
        ),
        ("prekeys de compatibilidad", sesion / "compat_prekey.db", False),
        ("LocalAuth del navegador", sesion / "web_companion", False),
        ("sesion del bootstrap web", sesion / "web_bootstrap", False),
        ("blobs de historial", settings.data_dir / "history", False),
        ("candado del runtime", sesion / "runtime.lock", True),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="salida para maquinas")
    args = parser.parse_args()

    settings = load_settings()
    motor = create_engine(settings.database_url)
    informe: dict[str, object] = {}
    problemas: list[str] = []

    # -- Tablas -------------------------------------------------------------
    filas: dict[str, int | None] = {}
    with motor.connect() as conexion:
        existentes = set(inspect(motor).get_table_names())
        for tabla in TABLAS_QUE_DEBEN_ESTAR_VACIAS + TABLAS_QUE_DEBEN_SEGUIR:
            if tabla not in existentes:
                filas[tabla] = None
                continue
            filas[tabla] = int(
                conexion.execute(text(f"SELECT COUNT(*) FROM {tabla}")).scalar() or 0
            )

    for tabla in TABLAS_QUE_DEBEN_ESTAR_VACIAS:
        cuantas = filas.get(tabla)
        if cuantas:
            problemas.append(f"{tabla} tiene {cuantas} filas y deberia estar vacia")
    for tabla in TABLAS_QUE_DEBEN_SEGUIR:
        if filas.get(tabla) in (None, 0):
            problemas.append(f"{tabla} esta vacia o no existe: la prueba pedira registro")

    # -- Archivos -----------------------------------------------------------
    archivos: dict[str, dict[str, object]] = {}
    for nombre, ruta, tolerado in _archivos(settings):
        existe = ruta.exists()
        vacio = True
        if existe and ruta.is_dir():
            vacio = not any(ruta.iterdir())
        elif existe:
            vacio = ruta.stat().st_size == 0
        archivos[nombre] = {"path": str(ruta), "exists": existe, "empty": vacio}
        if existe and not vacio and not tolerado:
            problemas.append(f"{nombre} sigue ahi: {ruta}")

    limpio = not problemas
    informe = {
        "clean": limpio,
        "tables": filas,
        "files": archivos,
        "problems": problemas,
    }

    if args.json:
        print(json.dumps(informe, indent=2, ensure_ascii=False))
        return 0 if limpio else 1

    print(LINEA)
    print("ESTADO DESPUES DE LIMPIAR")
    print(LINEA)
    print("  Debe estar VACIO:")
    for tabla in TABLAS_QUE_DEBEN_ESTAR_VACIAS:
        cuantas = filas.get(tabla)
        marca = "  ok" if not cuantas else " <-- QUEDA"
        texto = "no existe" if cuantas is None else str(cuantas)
        print(f"    {tabla:<24} {texto:>10}{marca}")
    print()
    print("  Debe SEGUIR estando:")
    for tabla in TABLAS_QUE_DEBEN_SEGUIR:
        cuantas = filas.get(tabla)
        marca = "  ok" if cuantas else " <-- FALTA"
        texto = "no existe" if cuantas is None else str(cuantas)
        print(f"    {tabla:<24} {texto:>10}{marca}")
    print()
    print("  Archivos:")
    for nombre, datos in archivos.items():
        estado = "no esta" if not datos["exists"] else ("vacio" if datos["empty"] else "QUEDA")
        print(f"    {nombre:<34} {estado}")

    print()
    print(LINEA)
    if limpio:
        print("LIMPIO. Se puede vincular.")
    else:
        print("NO ESTA LIMPIO. La medicion saldria contaminada:")
        for problema in problemas:
            print(f"  - {problema}")
    print(LINEA)
    print()
    print("Solo lectura: no se ha borrado nada desde aqui.")
    return 0 if limpio else 1


if __name__ == "__main__":
    raise SystemExit(main())
