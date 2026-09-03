"""Dejar el sistema como recien instalado, para una prueba de producto limpia.

QUE BORRA
---------
Los DATOS operativos, no la estructura:

* filas de PostgreSQL (chats, mensajes, multimedia, cursores, contactos...);
* la sesion principal COMO UNIDAD: ``device.json`` **y**
  ``device.json.signal.db`` juntos;
* la sesion auxiliar de Web Bootstrap, si existe.

QUE **NO** TOCA
---------------
El esquema, las migraciones de Alembic, ``diagnostics/`` y el codigo.

POR QUE LA IDENTIDAD Y EL SIGNAL STORE VAN JUNTOS
-------------------------------------------------
No es una preferencia: se midio. Borrar ``device.json`` dejando el
``.signal.db`` produce un dispositivo NUEVO usando ratchets de uno VIEJO. El
sintoma fue ``unknown one-time pre-key id 66`` y ON_DEMAND devolviendo ACK y
despues nada — el fallo que mas tiempo costo diagnosticar. Este script se
niega a borrar uno sin el otro.

COMO SE USA
-----------
No hace nada sin que se lo pidan dos veces::

    py tools/reset_product_test.py            # solo dice que haria
    py tools/reset_product_test.py --aplicar  # lo hace, tras confirmar

Antes de borrar guarda una copia en ``session/backups/`` y, si hay
``pg_dump``, un volcado de la base. Con ``--sin-copia`` se salta ese paso.

DESPUES
-------
Hay que desvincular el dispositivo desde el telefono (WhatsApp > Dispositivos
vinculados). Este script no puede hacerlo y no lo finge: borrar la sesion
local deja el dispositivo colgando en la lista del telefono.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import load_settings  # noqa: E402
from app.core.lock import probe  # noqa: E402

#: Se vacian en este orden: los hijos antes que los padres.
#:
#: ``app_state`` y ``history_requests`` van dentro a proposito: son estado de
#: extraccion atado a la IDENTIDAD (versiones de app-state, peticiones
#: ON_DEMAND ya emitidas). Con un dispositivo nuevo describen a otro, y
#: dejarlos haria que el sistema creyera haber pedido cosas que nunca pidio.
TABLAS = (
    "media_files",
    "messages",
    "chat_history_state",
    "history_requests",
    "app_state",
    "chats",
    "contacts",
)

#: Nunca se tocan. Alembic es lo que permite que el esquema siga en pie.
INTOCABLES = ("alembic_version",)


def _fecha() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
# Comprobaciones previas
# ---------------------------------------------------------------------------


def servicio_en_marcha(settings) -> str | None:
    """Quien tiene la sesion abierta ahora, o ``None`` si esta libre.

    Borrar la sesion con el servicio vivo deja el Signal Store abierto en
    SQLite y el borrado se salta el archivo en silencio: exactamente como se
    fabrico la identidad mezclada la primera vez.
    """
    titular = probe(settings.session_dir)
    if titular is None:
        return None
    return f"{getattr(titular, 'owner', 'otro proceso')} (PID {getattr(titular, 'pid', '?')})"


def _tablas_reales(engine) -> set[str]:
    from sqlalchemy import inspect

    return set(inspect(engine).get_table_names())


# ---------------------------------------------------------------------------
# Copia de seguridad
# ---------------------------------------------------------------------------


def _entorno_pg(url: str) -> dict[str, str]:
    """Las credenciales para ``pg_dump``, por variables de entorno.

    En ``argv`` la URL la veria cualquiera que liste los procesos de la
    maquina, y lleva la contrasena dentro. Las variables de un proceso hijo no
    son legibles por otros usuarios en Windows.
    """
    import os
    from urllib.parse import unquote, urlparse

    partes = urlparse(url)
    entorno = dict(os.environ)
    for clave, valor in (
        ("PGHOST", partes.hostname),
        ("PGPORT", str(partes.port) if partes.port else None),
        ("PGUSER", unquote(partes.username) if partes.username else None),
        ("PGPASSWORD", unquote(partes.password) if partes.password else None),
        ("PGDATABASE", partes.path.lstrip("/") or None),
    ):
        if valor:
            entorno[clave] = valor
    return entorno


def copia_de_seguridad(settings, destino: Path) -> list[str]:
    """Guarda sesion y (si se puede) un volcado de la base. Devuelve que hizo."""
    hecho: list[str] = []
    destino.mkdir(parents=True, exist_ok=True)

    for ruta in (settings.session_file, settings.signal_store_file):
        if ruta.exists():
            shutil.copy2(ruta, destino / ruta.name)
            hecho.append(f"sesion: {ruta.name}")

    volcado = destino / "postgres.sql"
    if shutil.which("pg_dump") is None:
        hecho.append("base: SIN volcado (pg_dump no esta en el PATH)")
        return hecho
    try:
        subprocess.run(
            ["pg_dump", "--no-owner", "--file", str(volcado)],
            check=True,
            capture_output=True,
            env=_entorno_pg(settings.database_url),
            timeout=600,
        )
        hecho.append(f"base: {volcado.name} ({volcado.stat().st_size // 1024} KB)")
    except Exception as exc:  # noqa: BLE001
        # Se dice; no se oculta tras un "listo".
        hecho.append(f"base: SIN volcado ({type(exc).__name__})")
    return hecho


# ---------------------------------------------------------------------------
# Borrado
# ---------------------------------------------------------------------------


def vaciar_tablas(engine, *, aplicar: bool) -> list[str]:
    """Vacia los datos respetando las claves ajenas. Conserva el esquema."""
    from sqlalchemy import text

    presentes = _tablas_reales(engine)
    objetivo = [t for t in TABLAS if t in presentes]
    lineas = []

    with engine.begin() as conexion:
        for tabla in objetivo:
            total = conexion.execute(text(f"SELECT count(*) FROM {tabla}")).scalar()
            lineas.append(f"  {tabla:<22} {total} fila(s)")
            if aplicar:
                # RESTART IDENTITY deja los contadores como recien creados;
                # CASCADE se ocupa de lo que cuelgue y no este en la lista.
                conexion.execute(
                    text(f"TRUNCATE TABLE {tabla} RESTART IDENTITY CASCADE")
                )

    huerfanas = presentes - set(TABLAS) - set(INTOCABLES)
    if huerfanas:
        lineas.append(f"  (no se tocan: {', '.join(sorted(huerfanas))})")
    return lineas


def borrar_sesion(settings, *, aplicar: bool) -> list[str]:
    """Quita identidad y Signal Store COMO UNIDAD, o no quita nada."""
    pareja = [settings.session_file, settings.signal_store_file]
    presentes = [r for r in pareja if r.exists()]
    if not presentes:
        return ["  (no hay sesion principal)"]

    lineas = [f"  {r.name} ({r.stat().st_size} bytes)" for r in presentes]
    if not aplicar:
        return lineas

    fallos = []
    for ruta in presentes:
        try:
            ruta.unlink()
        except OSError as exc:
            fallos.append(f"{ruta.name}: {exc}")

    if fallos:
        # Media identidad borrada es peor que ninguna. Se dice fuerte.
        lineas.append("  !! NO se pudo borrar la pareja entera: " + "; ".join(fallos))
        lineas.append("  !! NO vincules todavia: quedaria una identidad mezclada.")
        lineas.append("  !! Para el servicio y vuelve a ejecutar esto.")
    else:
        # Y se comprueba, en vez de confiar en que unlink dijo la verdad.
        restos = [r.name for r in pareja if r.exists()]
        lineas.append(
            "  !! SIGUEN AHI: " + ", ".join(restos)
            if restos
            else "  identidad y Signal Store borrados juntos"
        )
    return lineas


def borrar_auxiliar(settings, *, aplicar: bool) -> list[str]:
    """La sesion de Web Bootstrap. Independiente de la principal."""
    ruta = settings.session_dir / "web_bootstrap"
    if not ruta.exists():
        return ["  (no hay sesion auxiliar)"]
    lineas = [f"  {ruta}"]
    if aplicar:
        shutil.rmtree(ruta, ignore_errors=True)
        lineas.append("  borrada" if not ruta.exists() else "  !! sigue ahi")
    return lineas


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deja el sistema listo para una prueba de producto desde cero.",
    )
    parser.add_argument(
        "--aplicar",
        action="store_true",
        help="borra de verdad. Sin esto solo se muestra que se haria.",
    )
    parser.add_argument(
        "--sin-copia",
        action="store_true",
        help="no guardar copia de la sesion ni volcado de la base.",
    )
    parser.add_argument(
        "--si",
        action="store_true",
        help="no preguntar (para automatizar). Requiere --aplicar.",
    )
    args = parser.parse_args()

    settings = load_settings()
    from sqlalchemy import create_engine

    engine = create_engine(settings.database_url)

    print("=" * 70)
    print("RESET PARA PRUEBA DE PRODUCTO" if args.aplicar else "SIMULACION (no borra nada)")
    print("=" * 70)

    # La simulacion se muestra igual: poder ver el plan con el servicio vivo
    # es justo lo que hace falta para decidir cuando pararlo.
    titular = servicio_en_marcha(settings)
    if titular is not None:
        print()
        print(f"  ATENCION: la sesion la tiene ahora {titular}.")
        print("  Borrar con el servicio vivo deja el Signal Store abierto y el")
        print("  borrado se salta el archivo en silencio: asi se fabrico la")
        print("  identidad mezclada la vez anterior.")
        if args.aplicar:
            print()
            print("  ABORTADO. Para el servicio y vuelve a ejecutar esto.")
            return 2

    print("\nPostgreSQL (se vacian los datos, se conserva el esquema):")
    for linea in vaciar_tablas(engine, aplicar=False):
        print(linea)
    print("\nSesion principal (identidad + Signal Store, como unidad):")
    for linea in borrar_sesion(settings, aplicar=False):
        print(linea)
    print("\nSesion auxiliar Web Bootstrap:")
    for linea in borrar_auxiliar(settings, aplicar=False):
        print(linea)
    print("\nNo se tocan: migraciones de Alembic, esquema, diagnostics/, codigo.")

    if not args.aplicar:
        print("\nEsto ha sido una simulacion. Para hacerlo:")
        print("    py tools/reset_product_test.py --aplicar")
        return 0

    if not args.si:
        print()
        respuesta = input("Escribe BORRAR para confirmar: ").strip()
        if respuesta != "BORRAR":
            print("Cancelado. No se ha tocado nada.")
            return 1

    if not args.sin_copia:
        destino = settings.session_dir / "backups" / f"reset-{_fecha()}"
        print(f"\nCopia de seguridad en {destino}:")
        for linea in copia_de_seguridad(settings, destino):
            print(f"  {linea}")

    print("\nVaciando PostgreSQL...")
    for linea in vaciar_tablas(engine, aplicar=True):
        print(linea)
    print("\nBorrando la sesion principal...")
    for linea in borrar_sesion(settings, aplicar=True):
        print(linea)
    print("\nBorrando la sesion auxiliar...")
    for linea in borrar_auxiliar(settings, aplicar=True):
        print(linea)

    print()
    print("=" * 70)
    print("LISTO. Ahora, en este orden:")
    print()
    print("  1. En el telefono: WhatsApp > Dispositivos vinculados > cerrar")
    print("     las sesiones antiguas. Este script no puede hacerlo y borrar")
    print("     la sesion local las deja colgando en esa lista.")
    print("  2. py service.py")
    print("  3. ng serve   (en WhatsappBackup)")
    print("  4. Abrir http://localhost:4200 y escanear UN codigo QR.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
