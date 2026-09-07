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


@pytest.fixture(autouse=True)
def _sin_clientes_de_whatsapp_de_verdad(monkeypatch):
    """Los runtimes por cuenta se crean, pero NO se arrancan en la suite.

    En produccion la fabrica del registro llama a ``start(connect=True)``, y
    tiene que hacerlo: sin arrancar no hay ``client``, y sin ``client`` no se
    genera ningun codigo QR. Pero ``start()`` pasa por ``prepare_pywhats()``,
    que hace dos cosas incompatibles con una suite:

    * resuelve la version de WhatsApp Web por RED, y
    * parchea ``pywhats`` a nivel de PROCESO, sin deshacerlo.

    Lo segundo se midio: al vincular en una prueba quedaba parcheado
    ``pairing._device_props``, y `test_sin_el_parche_no_se_pide_historial_completo`
    --que comprueba justamente la linea base SIN parche-- fallaba despues,
    segun el orden de ejecucion.

    Que la fabrica de verdad arranque lo que crea lo fija
    `test_vinculacion_por_usuario.py::test_la_fabrica_arranca_el_runtime_que_crea`,
    que lee el codigo real en vez de ejecutarlo.
    """
    from app.core import runtime_registry

    def _sin_arrancar(settings, database, account_id):
        from app.core.runtime import AppRuntime

        rt = AppRuntime(
            settings, owner=f"account:{account_id}", configure_logging=False
        )
        rt.database = database
        rt.runtime_owner_account_id = account_id
        rt.runtime_owner_user_id = runtime_registry._usuario_de(database, account_id)
        return rt

    monkeypatch.setattr(runtime_registry, "_crear_runtime", _sin_arrancar)


@pytest.fixture
def session(database: Database):
    """Sesion aislada: todo lo que escriba el test se revierte al terminar."""
    from sqlalchemy.orm import Session

    connection = database.engine.connect()
    transaction = connection.begin()
    db_session = Session(bind=connection, expire_on_commit=False)

    # LA SUITE NO PUEDE DEPENDER DE LO QUE HAYA VINCULADO QUIEN LA EJECUTA.
    #
    # Comparte base con la instalacion real, y `account_scope.cuenta_unica()`
    # --de la que tiran los `upsert_*` que todavia no reciben la cuenta por
    # parametro-- responde `None` en cuanto ve DOS cuentas. Con un WhatsApp
    # vinculado de verdad, la cuenta que crea una prueba era la segunda y todo
    # lo que se apoyara en ella se quedaba sin cuenta.
    #
    # Se midio en los dos sentidos: con la base llena, 66 pruebas pasaban
    # apoyadas en datos ajenos; con la base vacia y una cuenta recien
    # vinculada, esas mismas pruebas fallaban.
    #
    # Se vacian AQUI y no en una fixture posterior: esto corre antes que
    # cualquier otra --todas dependen de `session`--, asi que las cuentas que
    # creen las fixturas siguen en pie. Vaciarlo en `cuenta` se llevaba por
    # delante las que acababa de crear `dos_usuarios`.
    #
    # Vive DENTRO de la transaccion de la prueba, que siempre se deshace: no
    # toca nada de la instalacion.
    from sqlalchemy import delete as _delete

    from app.models import WhatsAppAccount

    db_session.execute(_delete(WhatsAppAccount))
    db_session.flush()

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


@pytest.fixture
def cuenta_del_cliente(session):
    """La cuenta de WhatsApp del usuario que usa la fixture ``cliente``.

    POR QUE NO VALE UNA CUENTA CUALQUIERA
    -------------------------------------
    Las pruebas que van por HTTP se autentican como un usuario concreto, y la
    API le ensena SOLO lo suyo. Un chat creado bajo otra cuenta no aparece en
    su listado -- y eso no es un fallo de la prueba, es el aislamiento
    funcionando. Para comprobar que un chat SE VE hay que crearlo donde el
    usuario pueda verlo.
    """
    from sqlalchemy import select

    from app.models import WhatsAppAccount

    fila = session.execute(select(WhatsAppAccount)).scalars().first()
    assert fila is not None, "la fixture `cliente` deja una cuenta vinculada"
    return fila


@pytest.fixture
def cuenta(session):
    """Una cuenta de WhatsApp REAL a la que atribuir lo que cree la prueba.

    POR QUE TODO CHAT NECESITA UNA
    ------------------------------
    ``chats.whatsapp_account_id`` pasa a ser obligatorio, y no por gusto: la
    unicidad es ``(whatsapp_account_id, jid)`` y PostgreSQL trata los NULL como
    distintos entre si. Un chat sin cuenta no colisiona con nada, asi que dos
    filas del mismo contacto se colarian sin que nadie se entere.

    Se crea una fila de verdad --con su usuario-- en vez de un identificador
    inventado: una clave ajena que no apunta a ninguna parte no prueba nada, y
    ademas la base la rechaza.
    """
    import uuid as _uuid

    from app.models import User, WhatsAppAccount

    usuario = User(
        email=f"fixture-{_uuid.uuid4().hex[:10]}@example.com",
        password_hash="x",
    )
    session.add(usuario)
    session.flush()
    id_cuenta = _uuid.uuid4()
    fila = WhatsAppAccount(
        id=id_cuenta,
        user_id=usuario.id,
        session_status="linked",
        session_storage_key=f"accounts/{id_cuenta}",
    )
    session.add(fila)
    session.flush()
    return fila


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

    import uuid as _uuid_v

    id_cuenta = _uuid_v.uuid4()
    cuenta = WhatsAppAccount(
        id=id_cuenta,
        user_id=user_id,
        session_status="linked",
        session_storage_key=f"accounts/{id_cuenta}",
    )
    session.add(cuenta)
    session.flush()
    session.execute(sa_update(Chat).values(whatsapp_account_id=cuenta.id))
    session.flush()
    return cuenta

