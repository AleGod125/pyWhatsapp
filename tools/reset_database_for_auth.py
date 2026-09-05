"""Dejar la base lista para la fase de cuentas, desde cero.

QUE HACE
--------
Vacia los DATOS operativos (chats, mensajes, multimedia, cursores, contactos,
cuentas, sesiones web, credenciales de Google), conserva el esquema y aplica
las migraciones pendientes.

POR QUE HACE FALTA
------------------
Los chats de antes de multiusuario no pertenecen a nadie: su
``whatsapp_account_id`` es NULL. El sistema NO se los adjudica al primero que
se registre —eso seria entregarle el historial de la cuenta de pruebas—, asi
que se quedarian invisibles para siempre ocupando sitio.

QUE NO TOCA
-----------
El esquema, las migraciones de Alembic, ``diagnostics/`` y el codigo.

COMO SE USA
-----------
    py tools/reset_database_for_auth.py            # solo dice que haria
    py tools/reset_database_for_auth.py --aplicar  # lo hace, tras confirmar

Con ``--sesiones`` borra tambien la sesion de WhatsApp del disco. Va aparte a
proposito: vaciar la base y desvincular el companion son decisiones distintas,
y juntarlas obliga a escanear un QR a quien solo queria limpiar datos.
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

#: Los hijos antes que los padres. ``users`` va al final: todo cuelga de ahi.
TABLAS = (
    "media_files",
    "messages",
    "chat_history_state",
    "history_requests",
    "app_state",
    "chats",
    "contacts",
    "google_credentials",
    "user_sessions",
    "whatsapp_accounts",
    "users",
)

#: Alembic es lo que permite que el esquema siga en pie.
INTOCABLES = ("alembic_version",)


def _fecha() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _entorno_pg(url: str) -> dict[str, str]:
    """Credenciales para ``pg_dump``, por variables de entorno.

    En ``argv`` la URL la veria cualquiera que liste los procesos, y lleva la
    contrasena dentro.
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


def servicio_en_marcha(settings) -> str | None:
    titular = probe(settings.session_dir)
    if titular is None:
        return None
    return f"{getattr(titular, 'owner', 'otro proceso')} (PID {getattr(titular, 'pid', '?')})"


def volcado(settings, destino: Path) -> str:
    destino.mkdir(parents=True, exist_ok=True)
    archivo = destino / "postgres.sql"
    if shutil.which("pg_dump") is None:
        return "SIN volcado (pg_dump no esta en el PATH)"
    try:
        subprocess.run(
            ["pg_dump", "--no-owner", "--file", str(archivo)],
            check=True,
            capture_output=True,
            env=_entorno_pg(settings.database_url),
            timeout=900,
        )
        return f"{archivo.name} ({archivo.stat().st_size // 1024} KB)"
    except Exception as exc:  # noqa: BLE001
        return f"SIN volcado ({type(exc).__name__})"


def vaciar(engine, *, aplicar: bool) -> list[str]:
    from sqlalchemy import inspect, text

    presentes = set(inspect(engine).get_table_names())
    lineas = []
    with engine.begin() as conexion:
        for tabla in TABLAS:
            if tabla not in presentes:
                continue
            total = conexion.execute(text(f"SELECT count(*) FROM {tabla}")).scalar()
            lineas.append(f"  {tabla:<22} {total} fila(s)")
            if aplicar:
                conexion.execute(
                    text(f"TRUNCATE TABLE {tabla} RESTART IDENTITY CASCADE")
                )
    otras = presentes - set(TABLAS) - set(INTOCABLES)
    if otras:
        lineas.append(f"  (no se tocan: {', '.join(sorted(otras))})")
    return lineas


def sesiones_de_whatsapp(settings, *, aplicar: bool) -> list[str]:
    """La sesion principal y las de cada usuario.

    Identidad y Signal Store se van SIEMPRE juntos. Media identidad —un
    ``device.json`` nuevo sobre un store viejo— produce un dispositivo que no
    descifra nada, y es el fallo que mas tiempo costo diagnosticar.
    """
    lineas: list[str] = []
    objetivos: list[Path] = [
        settings.session_file,
        settings.signal_store_file,
        settings.session_dir / "compat_prekey.db",
    ]
    usuarios = settings.session_dir / "users"
    if usuarios.exists():
        lineas.append(f"  {usuarios} (carpeta entera)")

    presentes = [r for r in objetivos if r.exists()]
    lineas += [f"  {r.name} ({r.stat().st_size} bytes)" for r in presentes]
    if not presentes and not usuarios.exists():
        return ["  (no hay ninguna sesion de WhatsApp)"]
    if not aplicar:
        return lineas

    fallos = []
    for ruta in presentes:
        try:
            ruta.unlink()
        except OSError as exc:
            fallos.append(f"{ruta.name}: {exc}")
    if usuarios.exists():
        shutil.rmtree(usuarios, ignore_errors=True)

    restos = [r.name for r in objetivos if r.exists()]
    if fallos or restos:
        lineas.append("  !! NO se pudo borrar todo: " + ", ".join(restos or fallos))
        lineas.append("  !! NO vincules todavia: quedaria una identidad mezclada.")
    else:
        lineas.append("  identidad y Signal Store borrados juntos")
    return lineas


def migrar() -> bool:
    """Aplica las migraciones pendientes."""
    resultado = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
    )
    if resultado.returncode != 0:
        print(resultado.stderr.strip()[-800:])
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepara la base para la fase de cuentas."
    )
    parser.add_argument("--aplicar", action="store_true", help="borra de verdad")
    parser.add_argument(
        "--sesiones",
        action="store_true",
        help="borrar tambien las sesiones de WhatsApp del disco",
    )
    parser.add_argument("--sin-copia", action="store_true", help="no volcar la base")
    parser.add_argument("--si", action="store_true", help="no preguntar")
    args = parser.parse_args()

    settings = load_settings()
    from sqlalchemy import create_engine

    engine = create_engine(settings.database_url)

    print("=" * 70)
    print("RESET PARA LA FASE DE CUENTAS" if args.aplicar else "SIMULACION (no borra nada)")
    print("=" * 70)

    titular = servicio_en_marcha(settings)
    if titular is not None:
        print(f"\n  ATENCION: la sesion la tiene {titular}.")
        if args.aplicar:
            print("\n  ABORTADO. Para el servicio y vuelve a ejecutar esto.")
            return 2

    print("\nPostgreSQL (se vacian los datos, se conserva el esquema):")
    for linea in vaciar(engine, aplicar=False):
        print(linea)

    if args.sesiones:
        print("\nSesiones de WhatsApp en disco:")
        for linea in sesiones_de_whatsapp(settings, aplicar=False):
            print(linea)
    else:
        print("\nSesiones de WhatsApp: NO se tocan (usa --sesiones para borrarlas).")

    print("\nDespues se ejecutara:  alembic upgrade head")
    print("No se tocan: migraciones, esquema, diagnostics/, codigo.")

    if not args.aplicar:
        print("\nEsto ha sido una simulacion. Para hacerlo:")
        print("    py tools/reset_database_for_auth.py --aplicar")
        return 0

    if not args.si:
        print()
        if input("Escribe BORRAR para confirmar: ").strip() != "BORRAR":
            print("Cancelado. No se ha tocado nada.")
            return 1

    if not args.sin_copia:
        destino = settings.diagnostics_dir / f"reset-auth-{_fecha()}"
        print(f"\nCopia de seguridad en {destino}:")
        print(f"  {volcado(settings, destino)}")

    print("\nVaciando PostgreSQL...")
    for linea in vaciar(engine, aplicar=True):
        print(linea)

    if args.sesiones:
        print("\nBorrando sesiones de WhatsApp...")
        for linea in sesiones_de_whatsapp(settings, aplicar=True):
            print(linea)

    print("\nAplicando migraciones...")
    if not migrar():
        print("  !! las migraciones fallaron; revisa el error de arriba")
        return 3
    print("  migraciones al dia")

    print()
    print("=" * 70)
    print("LISTO. Ahora:")
    print()
    print("  1. Configura Google en .env (docs/GOOGLE_OAUTH_SETUP.md).")
    print("  2. py service.py")
    print("  3. ng serve   (en WhatsappBackup)")
    print("  4. Abrir http://localhost:4200 -> crear cuenta -> conectar Google")
    print("  5. Escanear UN codigo QR de WhatsApp.")
    if args.sesiones:
        print()
        print("  Nota: se borro la sesion local. Revisa en el telefono")
        print("  (WhatsApp > Dispositivos vinculados) que no quede colgando.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
