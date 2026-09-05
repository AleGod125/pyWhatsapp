"""Fixtures compartidas.

Los tests de base de datos corren contra el PostgreSQL REAL configurado en
.env, dentro de una transaccion que SIEMPRE se revierte. Asi se prueba el
comportamiento autentico del motor (indices parciales, ON CONFLICT, JSONB,
BYTEA, CHECK) sin dejar residuos ni depender de un SQLite que se comporta
distinto.
"""

from __future__ import annotations

import sys
import uuid
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


# ---------------------------------------------------------------------------
# Runtime de pruebas
# ---------------------------------------------------------------------------


class _SessionShim:
    """Sesion del test que la API no puede cerrar."""

    def __init__(self, session) -> None:
        self._session = session

    def __getattr__(self, nombre):
        return getattr(self._session, nombre)

    def close(self) -> None:
        return None


class _DatabaseShim:
    """Base de datos que devuelve SIEMPRE la sesion del test.

    La API abre una sesion por peticion y la cierra al terminar. La del test
    vive dentro de una transaccion que se revierte al final, asi que si la
    cerrara se perderia lo que la prueba acaba de escribir. Este envoltorio
    conserva el contrato y neutraliza el cierre.
    """

    def __init__(self, real, session) -> None:
        self._real = real
        self._session = session

    def session(self):
        return _SessionShim(self._session)

    def health(self):
        return self._real.health()

    def applied_migration(self):
        return self._real.applied_migration()

    def transaction(self):
        """Tambien sobre la sesion del test, NO sobre la base real.

        Delegar esto en ``self._real`` fue un error con consecuencias: los
        servicios que escriben dentro de ``transaction()`` (mantenimiento,
        recuperacion de semillas) hacian COMMIT contra la base de produccion
        del usuario. Se detecto porque una pasada de la suite reclasifico 32
        chats reales. Ahora todo queda dentro de la transaccion que se
        revierte.
        """
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()

    def dispose(self) -> None:
        return None

@pytest.fixture
def runtime(settings, database, session, tmp_path):
    """``AppRuntime`` real, con base del test y sesion en un temporal.

    Se usa el runtime de verdad, no un doble: asi las pruebas ejercitan el
    mismo objeto que construye ``service.py``. Pero la carpeta
    de sesion se aisla: la aplicacion archiva ``device.json`` cuando el
    servidor rechaza un login, y una prueba no puede tocar la sesion viva.
    """
    import dataclasses

    from app.core.runtime import AppRuntime

    aislado = dataclasses.replace(
        settings,
        session_dir=tmp_path / "session",
        diagnostics_dir=tmp_path / "diagnostics",
    )
    (tmp_path / "session").mkdir(parents=True, exist_ok=True)
    (tmp_path / "diagnostics").mkdir(parents=True, exist_ok=True)

    rt = AppRuntime(aislado, owner="pytest", configure_logging=False)
    rt.database = _DatabaseShim(database, session)
    rt._montar_cuentas()
    return rt


# ---------------------------------------------------------------------------
# Cliente HTTP autenticado
# ---------------------------------------------------------------------------
#
# Desde que existen cuentas, la API entera exige sesion. Sin esta fixtura cada
# prueba mediria un 401 en vez de lo que quiere medir.
#
# Autentica de VERDAD —registro, cookie de sesion y cabecera CSRF—, de modo
# que el camino que se ejercita es el mismo que usa el navegador. Las pruebas
# de que un anonimo NO pasa viven en ``test_auth.py`` y usan cliente sin
# cookie a proposito.


@pytest.fixture
def cliente(runtime, session):
    """Cliente YA autenticado, con Google Drive concedido.

    La API entera exige sesion, asi que sin esto cada prueba mediria un 401 en
    vez de lo que quiere medir. Autenticar de verdad —cookie incluida— es
    ademas lo que garantiza que el camino real funciona.

    Las pruebas de que un anonimo NO pasa viven en ``test_auth.py`` y usan un
    cliente sin cookie a proposito.
    """
    from app.api import create_app

    # Cada modulo construye su propio ``runtime``; no todos montan cuentas.
    # Se hace aqui para que la fixtura funcione con cualquiera de ellos.
    runtime._montar_cuentas()

    aplicacion = create_app(runtime)
    aplicacion.config.update(TESTING=True)
    # El token CSRF viaja en una cookie legible y se repite en la cabecera.
    # Un navegador de verdad hace exactamente esto; aqui se replica para que
    # las pruebas ejerciten el mismo camino y no una version sin proteger.
    from app.auth.web import CSRF_COOKIE, CSRF_HEADER

    csrf = "token-de-prueba"
    cli = aplicacion.test_client()
    cli.environ_base["HTTP_" + CSRF_HEADER.upper().replace("-", "_")] = csrf
    cli.set_cookie(CSRF_COOKIE, csrf)

    inicio = runtime.auth.register(
        email=f"prueba-{uuid.uuid4().hex[:8]}@example.com",
        password="una contrasena larga",
        display_name="Prueba",
    )
    cli.set_cookie(runtime.settings.session_cookie_name, inicio.token)
    _conceder_drive(session, inicio.user_id)
    cuenta = _vincular_whatsapp(session, inicio.user_id)

    # Las fixturas crean chats por muchas vias distintas y ninguna conoce la
    # cuenta. En produccion eso no pasa —un chat siempre nace bajo la cuenta
    # que lo recibio—, asi que aqui se adoptan los huerfanos justo antes de
    # cada peticion en vez de reescribir cincuenta fixturas.
    #
    # OJO: solo adopta los que NO tienen dueno. Un chat de otro usuario sigue
    # siendo de otro usuario, que es lo que comprueban las pruebas de
    # aislamiento.
    @aplicacion.before_request
    def _adoptar_huerfanos():
        from sqlalchemy import update as sa_update

        from app.models import Chat

        session.execute(
            sa_update(Chat)
            .where(Chat.whatsapp_account_id.is_(None))
            .values(whatsapp_account_id=cuenta.id)
        )
        session.flush()

    cli.usuario_id = inicio.user_id
    cli.cuenta_id = cuenta.id
    return cli

def _conceder_drive(session, user_id):
    """Credenciales de Google con el scope de Drive, sin hablar con Google."""
    from app.auth.google import SCOPE_DRIVE
    from app.models import GoogleCredential

    session.add(
        GoogleCredential(
            user_id=user_id,
            google_subject=f"sub-{user_id}",
            scope=f"openid email profile {SCOPE_DRIVE}",
            refresh_token_encrypted=b"x",
        )
    )
    session.flush()

def _vincular_whatsapp(session, user_id):
    """Cuenta de WhatsApp del usuario, y los chats existentes pasan a ser suyos.

    Se borran antes las cuentas de otras pruebas: comparten transaccion, y
    ``dueno_actual()`` devuelve la primera vinculada que encuentra. Con dos, la
    comprobacion de propiedad rechazaria al usuario de ESTA prueba por una
    cuenta que dejo otra.
    """
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import update as sa_update

    from app.models import Chat, WhatsAppAccount

    session.execute(sa_delete(WhatsAppAccount).where(WhatsAppAccount.user_id != user_id))
    session.flush()

    cuenta = WhatsAppAccount(
        user_id=user_id,
        session_status="linked",
        session_storage_key=f"users/{user_id}",
    )
    session.add(cuenta)
    session.flush()
    session.execute(sa_update(Chat).values(whatsapp_account_id=cuenta.id))
    session.flush()
    return cuenta

