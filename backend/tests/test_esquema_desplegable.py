"""Una base recien creada queda como el codigo espera.

POR QUE ESTO NO SE VE EN DESARROLLO
-----------------------------------
Aqui la base lleva meses: se creo hace tiempo y ha ido recibiendo migraciones
una a una, con datos dentro. En la nube pasa lo contrario -- una base VACIA y
las trece migraciones seguidas, de golpe. No es el mismo camino, y por eso
puede dar otro resultado.

Dio otro resultado. ``b2c3d4e5f6a7`` aisla los contactos por cuenta, pero
envolvia las restricciones en la misma condicion que el relleno de datos::

    if cuentas:                    # <- filas en whatsapp_accounts
        op.alter_column("contacts", "whatsapp_account_id", nullable=False)
        op.drop_constraint("contacts_jid_key", ...)
        op.create_unique_constraint("uq_contacts_account_jid", ...)

El relleno SI necesita la guarda; las restricciones no. Con la base vacia
--exactamente el despliegue-- ``cuentas`` es 0 y el bloque entero se saltaba::

    contacts.whatsapp_account_id   NULLABLE
    UNIQUE (jid)                   global

Con esa unicidad, dos cuentas no pueden tener el mismo contacto: la segunda
choca con la fila de la primera o la pisa. El nombre que una persona le puso a
un numero acabaria en la agenda de otra -- el mismo fallo que la migracion
venia a cerrar, intacto en toda instalacion nueva.

LO QUE COMPRUEBA
----------------
Que aplicar TODAS las migraciones sobre una base vacia deje el esquema que los
modelos declaran. Se compara con el mismo mecanismo que usa
``alembic revision --autogenerate``: si propusiera algun cambio, es que la base
desplegada y el codigo no dicen lo mismo.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text


BASE_DE_PRUEBA = "whatsapp_esquema_check"


def _url_de(base: str) -> str:
    from app.core.config import load_settings

    return load_settings().database_url.rsplit("/", 1)[0] + "/" + base


@pytest.fixture(scope="module")
def base_recien_creada():
    """Una base vacia con todas las migraciones aplicadas. Se borra al final.

    Es cara --crea una base y corre trece migraciones-- asi que es de modulo:
    una vez por ejecucion, no una por prueba.
    """
    import subprocess
    import sys

    admin = create_engine(_url_de("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as con:
        con.execute(text(f'DROP DATABASE IF EXISTS "{BASE_DE_PRUEBA}"'))
        con.execute(text(f'CREATE DATABASE "{BASE_DE_PRUEBA}"'))

    entorno = {**os.environ, "DATABASE_URL": _url_de(BASE_DE_PRUEBA)}
    resultado = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        env=entorno,
        timeout=180,
    )
    if resultado.returncode != 0:
        pytest.fail(
            "las migraciones no aplican sobre una base vacia:\n"
            + resultado.stderr[-2000:]
        )

    motor = create_engine(_url_de(BASE_DE_PRUEBA))
    yield motor
    motor.dispose()
    with admin.connect() as con:
        con.execute(text(f'DROP DATABASE IF EXISTS "{BASE_DE_PRUEBA}"'))


def test_las_migraciones_dejan_el_esquema_que_el_codigo_espera(base_recien_creada):
    """Sin esto, el despliegue arranca con un esquema que nadie ha comprobado."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.models import Base
    import app.models.accounts  # noqa: F401
    import app.models.schema  # noqa: F401
    import app.models.storage  # noqa: F401

    with base_recien_creada.connect() as con:
        contexto = MigrationContext.configure(
            con, opts={"compare_type": True, "compare_server_default": False}
        )
        diferencias = compare_metadata(contexto, Base.metadata)

    assert not diferencias, (
        "el esquema desplegado no coincide con los modelos:\n"
        + "\n".join(f"  - {str(d)[:160]}" for d in diferencias)
    )


def test_los_contactos_quedan_aislados_por_cuenta(base_recien_creada):
    """La diferencia concreta que se escapaba, y la que mas duele.

    Se comprueba aparte de la comparacion general porque es la que tiene
    consecuencia para una persona: sin ella, la agenda de una cuenta pisa la
    de otra.
    """
    with base_recien_creada.connect() as con:
        restricciones = {
            fila[0]: fila[1]
            for fila in con.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = CAST('contacts' AS regclass)"
                )
            )
        }
        nullable = con.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name='contacts' AND column_name='whatsapp_account_id'"
            )
        ).scalar()

    assert nullable == "NO", "un contacto sin cuenta no se puede aislar de nada"
    assert "uq_contacts_account_jid" in restricciones
    assert "whatsapp_account_id" in restricciones["uq_contacts_account_jid"]
    assert "contacts_jid_key" not in restricciones, (
        "la unicidad global por jid impide que dos cuentas tengan el mismo "
        "contacto"
    )
    assert "fk_contacts_account" in restricciones, (
        "sin clave ajena, borrar una cuenta deja sus contactos sueltos"
    )


def test_los_chats_tambien(base_recien_creada):
    """La misma comprobacion para la tabla que mas importa."""
    with base_recien_creada.connect() as con:
        restricciones = {
            fila[0]: fila[1]
            for fila in con.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = CAST('chats' AS regclass)"
                )
            )
        }

    assert "uq_chats_account_jid" in restricciones
    assert "whatsapp_account_id" in restricciones["uq_chats_account_jid"]
