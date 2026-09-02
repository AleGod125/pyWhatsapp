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

from app.config import Settings, load_settings  # noqa: E402
from app.database import Database, DatabaseError  # noqa: E402


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


@pytest.fixture(scope="session")
def tk_app(database):
    """UNA sola ventana Tk para toda la suite.

    Cada modulo creaba y destruia su propio ``Tk()``. Crear varios interpretes
    Tcl seguidos en un mismo proceso es fragil en Windows: el segundo o el
    tercero fallan con "Can't find a usable init.tcl" y las pruebas de
    interfaz se saltaban de forma intermitente. Con una unica ventana
    compartida eso desaparece y ademas se parece mas a la aplicacion real, que
    tiene un solo ``root`` y un solo ``mainloop``.

    Se entrega TAL CUAL la crea la aplicacion, sin forzar vista ni geometria:
    cada modulo prueba una pantalla distinta y dejarle una impuesta cambiaria
    lo que mide. Quien necesite el visor mapeado usa ``viewer_app``.
    """
    import queue as _queue
    import tkinter as _tk

    pytest.importorskip("tkinter")
    from app.gui import App

    try:
        instance = App(_queue.Queue())
    except _tk.TclError as exc:  # pragma: no cover - sin entorno grafico
        pytest.skip(f"sin display: {exc}")

    instance.attach_viewer(database.session)
    yield instance

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
