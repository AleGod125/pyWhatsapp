"""Regenera ``database/schema.sql`` desde la base de datos real.

POR QUE EXISTE
--------------
``database/schema.sql`` es un atajo para levantar una base desde cero sin
ejecutar las 11 migraciones. Un atajo asi empieza a mentir en cuanto alguien
anade una migracion y se olvida de regenerarlo, y un esquema que miente es
peor que no tenerlo: instala algo que no es lo que el codigo espera y el fallo
aparece mucho despues.

Regenerarlo tiene que costar un comando. De ahi esto.

QUE HACE
--------
Llama a ``pg_dump --schema-only`` sobre la base configurada en el ``.env``,
le pone la cabecera explicativa y la fila de ``alembic_version``, y lo escribe.
NO toca la base: ``pg_dump`` solo lee.

USO
---
::

    py backend/tools/exportar_esquema.py            # regenera
    py backend/tools/exportar_esquema.py --comprobar # ¿esta al dia? (no escribe)

``--comprobar`` devuelve 1 si el archivo no coincide con la base. Sirve para
enterarse en CI en vez de descubrirlo instalando.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import PROJECT_ROOT, load_settings  # noqa: E402

#: Donde vive el esquema: en la raiz del repositorio, junto al `.env`, porque
#: es infraestructura compartida y no algo interno del backend.
DESTINO = PROJECT_ROOT.parent / "database" / "schema.sql"

CABECERA = """\
-- ===========================================================================
-- whatsapp_backup - ESQUEMA COMPLETO DE LA BASE DE DATOS
-- ===========================================================================
--
-- Motor      : PostgreSQL {version}  (requiere 13+ por gen_random_uuid())
-- Revision   : {revision}   <- la cabeza de Alembic
-- Generado   : {fecha}, con `pg_dump --schema-only` sobre la base real.
--
-- QUE ES ESTO
-- -----------
-- El esquema entero en un solo archivo, autocontenido y en el orden correcto:
-- extension -> secuencias -> tablas -> restricciones -> indices -> semilla.
-- Sirve para levantar una base desde cero sin ejecutar las migraciones.
--
-- NO LLEVA NI UNA FILA DE DATOS. Solo estructura, mas la unica fila que hace
-- falta: la revision de Alembic (al final).
--
-- QUIEN MANDA
-- -----------
-- La fuente de verdad siguen siendo las migraciones de `backend/migrations/`.
-- Este archivo es un ATAJO para instalar de cero, no un sustituto: si tocas el
-- esquema, hazlo con una migracion de Alembic y despues regenera esto con
--
--     py backend/tools/exportar_esquema.py
--
-- COMO USARLO
-- -----------
--     createdb whatsapp_backup
--     psql -d whatsapp_backup -f database/schema.sql
--
-- AVISO
-- -----
-- Solo tiene `CREATE`: sobre una base que ya tenga estas tablas fallara con
-- "already exists". Es a proposito -- que falle es mejor que que pise datos.
-- ===========================================================================

"""

PIE = """
-- ===========================================================================
-- SEMILLA MINIMA
-- ===========================================================================
--
-- La unica fila obligatoria de toda la base. Sin ella, Alembic cree que la
-- base esta vacia e intentaria aplicar las migraciones sobre un esquema que ya
-- existe, fallando en la primera.
--
-- No hay ninguna otra: ni planes, ni roles, ni catalogos. Los usuarios se dan
-- de alta con Google desde la aplicacion.
INSERT INTO public.alembic_version (version_num)
VALUES ('{revision}')
ON CONFLICT DO NOTHING;
"""


def _partes_de_la_url(url: str) -> dict[str, str]:
    """Usuario, contrasena, host, puerto y base de la URL de SQLAlchemy."""
    m = re.match(
        r"^[^:]+://(?P<user>[^:@/]+)(?::(?P<password>[^@]*))?@"
        r"(?P<host>[^:/]+)(?::(?P<port>\d+))?/(?P<db>.+)$",
        url,
    )
    if m is None:
        raise SystemExit(f"No se pudo interpretar DATABASE_URL: {url[:40]}...")
    d = m.groupdict()
    return {
        "user": d["user"],
        "password": d["password"] or "",
        "host": d["host"],
        "port": d["port"] or "5432",
        "db": d["db"],
    }


def _pg_dump() -> str:
    """El ejecutable de pg_dump, del PATH o de la instalacion de Windows."""
    hallado = shutil.which("pg_dump")
    if hallado:
        return hallado
    for version in ("18", "17", "16", "15", "14"):
        candidato = Path(
            rf"C:\Program Files\PostgreSQL\{version}\bin\pg_dump.exe"
        )
        if candidato.is_file():
            return str(candidato)
    raise SystemExit("No se encontro pg_dump. Anadelo al PATH.")


def _revision(ajustes) -> str:
    from sqlalchemy import text

    from app.core.database import Database

    db = Database(ajustes)
    db.connect()
    with db.transaction() as sesion:
        sesion.execute(text("SET TRANSACTION READ ONLY"))
        return sesion.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _version_del_servidor(ajustes) -> str:
    from sqlalchemy import text

    from app.core.database import Database

    db = Database(ajustes)
    db.connect()
    with db.transaction() as sesion:
        sesion.execute(text("SET TRANSACTION READ ONLY"))
        return sesion.execute(text("SHOW server_version")).scalar()


def construir() -> str:
    ajustes = load_settings()
    partes = _partes_de_la_url(str(ajustes.database_url))
    revision = _revision(ajustes)

    entorno = dict(os.environ)
    if partes["password"]:
        entorno["PGPASSWORD"] = partes["password"]

    volcado = subprocess.run(
        [
            _pg_dump(), "--schema-only", "--no-owner", "--no-privileges",
            "--no-comments",
            "-h", partes["host"], "-p", partes["port"],
            "-U", partes["user"], "-d", partes["db"],
        ],
        capture_output=True, text=True, env=entorno, check=False,
    )
    if volcado.returncode != 0:
        raise SystemExit(f"pg_dump fallo:\n{volcado.stderr[:600]}")

    cabecera = CABECERA.format(
        version=_version_del_servidor(ajustes),
        revision=revision,
        fecha=date.today().isoformat(),
    )
    return cabecera + _limpiar(volcado.stdout) + PIE.format(revision=revision)


def _limpiar(volcado: str) -> str:
    r"""Quita los `\restrict` / `\unrestrict` que anade pg_dump 18.

    Son dos cosas a la vez, y las dos estorban:

    * llevan un token ALEATORIO en cada ejecucion, asi que dos volcados
      seguidos de la misma base nunca coinciden y `--comprobar` diria siempre
      "desactualizado";
    * son metacomandos de `psql`, no SQL. Con ellos dentro, el archivo solo se
      puede aplicar con `psql`: cualquier otro cliente --DBeaver, pgAdmin,
      psycopg-- se atraganta en la primera linea.

    Existen para replicar volcados de bases ajenas sin que un nombre de objeto
    malicioso inyecte ordenes. Aqui el esquema es el nuestro y el archivo se
    lee entero, asi que no aportan nada.
    """
    marcas = ("\\restrict ", "\\unrestrict ")
    utiles = [l for l in volcado.splitlines() if not l.startswith(marcas)]
    return "\n".join(utiles) + "\n"


def _sin_fecha(texto: str) -> str:
    """El contenido ignorando la linea de fecha, que cambia cada dia."""
    return "\n".join(
        l for l in texto.splitlines() if not l.startswith("-- Generado   :")
    )


def main() -> int:
    argumentos = argparse.ArgumentParser(description=__doc__)
    argumentos.add_argument(
        "--comprobar",
        action="store_true",
        help="no escribe; devuelve 1 si el archivo no esta al dia",
    )
    opciones = argumentos.parse_args()

    nuevo = construir()

    if opciones.comprobar:
        if not DESTINO.exists():
            print(f"FALTA: {DESTINO}")
            return 1
        actual = DESTINO.read_text(encoding="utf-8")
        if _sin_fecha(actual) != _sin_fecha(nuevo):
            print(f"DESACTUALIZADO: {DESTINO}")
            print("Regenera con:  py backend/tools/exportar_esquema.py")
            return 1
        print(f"AL DIA: {DESTINO}")
        return 0

    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    DESTINO.write_text(nuevo, encoding="utf-8")
    print(f"Escrito {DESTINO} ({len(nuevo.splitlines())} lineas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
