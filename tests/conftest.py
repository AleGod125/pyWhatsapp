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


def pytest_addoption(parser):
    parser.addoption(
        "--sin-ventana",
        action="store_true",
        default=False,
        help=(
            "Salta las pruebas que abren la ventana Tkinter. La suite crea un "
            "root de Tk de verdad, y eso hace aparecer una ventana de Python "
            "por encima de lo que estes mirando. Con service.py delante en la "
            "terminal, esa ventana solo estorba."
        ),
    )


@pytest.fixture(scope="session")
def tk_app(database, request):
    """UNA sola ventana Tk para toda la suite.

    Cada modulo creaba y destruia su propio ``Tk()``. Crear varios interpretes
    Tcl seguidos en un mismo proceso es fragil en Windows: el segundo o el
    tercero fallan con "Can't find a usable init.tcl" y las pruebas de
    interfaz se saltaban de forma intermitente. Con una unica ventana
    compartida eso desaparece y ademas se parece mas a la aplicacion real, que
    tiene un solo ``root`` y un solo ``mainloop``.

    Con ``--sin-ventana`` no se crea nada y las pruebas que dependan de ella
    se saltan: sirve para validar el backend sin que aparezca una ventana
    encima de la terminal.

    Se entrega TAL CUAL la crea la aplicacion, sin forzar vista ni geometria:
    cada modulo prueba una pantalla distinta y dejarle una impuesta cambiaria
    lo que mide. Quien necesite el visor mapeado usa ``viewer_app``.
    """
    if request.config.getoption("--sin-ventana"):
        pytest.skip("--sin-ventana: no se abre la ventana Tkinter")

    import queue as _queue
    import tkinter as _tk

    pytest.importorskip("tkinter")
    from app.gui import App

    try:
        instance = App(_queue.Queue())
    except _tk.TclError as exc:  # pragma: no cover - sin entorno grafico
        pytest.skip(f"sin display: {exc}")

    instance.attach_viewer(database.session)

    global _APP_VIVA
    _APP_VIVA = instance
    yield instance
    _APP_VIVA = None

    try:
        instance.root.destroy()
    except _tk.TclError:
        pass


@pytest.fixture
def viewer_app(tk_app):
    """La ventana con el visor REALMENTE mapeado y de tamano conocido.

    Hace falta para medir scroll: Tk no calcula la geometria de un widget que
    no se muestra, asi que ``winfo_y()`` y ``yview()`` devolverian valores sin
    sentido. Y la geometria se fija DESPUES de mapear, porque antes Tk la
    descarta y dimensiona la ventana al tamano solicitado por el contenido:
    con 452 mensajes el "viewport" acababa midiendo diez mil pixeles.

    Al terminar devuelve la ventana como estaba, para no condicionar a los
    modulos que prueban otras pantallas.
    """
    import tkinter as _tk

    vista_previa = tk_app._current
    geometria_previa = tk_app.root.winfo_geometry()
    try:
        tk_app._swap(tk_app.viewer)
        tk_app.root.update()
        tk_app.root.geometry("1100x720")
        tk_app.root.update()
    except _tk.TclError as exc:  # pragma: no cover
        pytest.skip(f"no se pudo mapear la ventana: {exc}")

    yield tk_app

    try:
        if vista_previa is not None:
            tk_app._swap(vista_previa)
        tk_app.root.geometry(geometria_previa)
        tk_app.root.update()
    except _tk.TclError:  # pragma: no cover
        pass


# Instancia viva de la ventana compartida, si algun test la ha pedido. Se
# guarda aparte para que ``limpiar_trabajos_tk`` pueda limpiarla sin depender
# de la fixture: depender de ella obligaria a crear Tk en TODOS los tests.
_APP_VIVA = None


@pytest.fixture(autouse=True)
def limpiar_trabajos_tk():
    """Cancela el trabajo diferido de Tk al terminar cada prueba.

    La ventana se comparte, y varias partes de la aplicacion programan trabajo
    con ``after``: el refresco agrupado del sidebar y el pintor perezoso de
    miniaturas. Si una prueba termina con uno pendiente, se dispara dentro de
    la SIGUIENTE, en mitad de su ``update()``, y repinta el sidebar o cambia de
    vista por debajo. Eso producia fallos que aparecian y desaparecian segun el
    orden en que pytest ejecutara los modulos.

    Tambien se devuelve el visor a su fabrica de sesiones original: varias
    pruebas le enchufan la suya, que al revertirse dejaria al resto de la suite
    consultando sobre una conexion cerrada.
    """
    import tkinter as _tk

    fabrica = None
    if _APP_VIVA is not None and getattr(_APP_VIVA, "viewer", None) is not None:
        fabrica = _APP_VIVA.viewer._session_factory

    yield

    app = _APP_VIVA
    if app is None:
        return
    try:
        if getattr(app, "_sidebar_job", None) is not None:
            app.root.after_cancel(app._sidebar_job)
            app._sidebar_job = None
        visor = getattr(app, "viewer", None)
        if visor is not None:
            if fabrica is not None:
                visor._session_factory = fabrica
            if getattr(visor, "_search_job", None) is not None:
                visor.after_cancel(visor._search_job)
                visor._search_job = None
            panel = getattr(visor, "conversation", None)
            if panel is not None and getattr(panel, "_thumb_job", None) is not None:
                panel.after_cancel(panel._thumb_job)
                panel._thumb_job = None
    except (_tk.TclError, ValueError, AttributeError):  # pragma: no cover
        pass
