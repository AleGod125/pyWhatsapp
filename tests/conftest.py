"""Fixtures compartidas.

Los tests de base de datos corren contra el PostgreSQL REAL configurado en
.env, dentro de una transaccion que SIEMPRE se revierte. Asi se prueba el
comportamiento autentico del motor (indices parciales, ON CONFLICT, JSONB,
BYTEA, CHECK) sin dejar residuos ni depender de un SQLite que se comporta
distinto.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings, load_settings  # noqa: E402
from app.core.database import Database, DatabaseError  # noqa: E402


@pytest.fixture(scope="session")
def settings() -> Settings:
    return load_settings()


@pytest.fixture(scope="session")
def database(settings: Settings) -> Iterator[Database]:
    db = Database(settings)
    try:
        db.connect()
    except DatabaseError as exc:
        pytest.skip(f"PostgreSQL no disponible: {exc}")
    yield db
    db.dispose()


@pytest.fixture
def session(database: Database):
    """Sesion aislada: todo lo que escriba el test se revierte al terminar."""
    from sqlalchemy.orm import Session

    connection = database.engine.connect()
    transaction = connection.begin()
    db_session = Session(bind=connection, expire_on_commit=False)
    try:
        yield db_session
    finally:
        db_session.close()
        transaction.rollback()
        connection.close()








# Instancia viva de la ventana compartida, si algun test la ha pedido. Se
# guarda aparte para que ``limpiar_trabajos_tk`` pueda limpiarla sin depender
# de la fixture: depender de ella obligaria a crear Tk en TODOS los tests.
_APP_VIVA = None


