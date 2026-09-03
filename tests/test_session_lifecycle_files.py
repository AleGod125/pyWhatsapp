"""Una sesion son DOS archivos, y se van juntos o no se va ninguno.

LA CONTAMINACION, MEDIDA
------------------------
Tras un 401 se archivo la sesion revocada y el log dijo::

    Sesion archivada en ... (3 archivos: compat_prekey.db, device.json,
    runtime.lock; bloqueados: device.json.signal.db)

El Signal Store se quedo. El pairing siguiente creo un ``device.json`` nuevo
encima, y el resultado fue una identidad que no es de nadie::

    device.json          device_id=86  registration_id=572666329  (nuevo)
    device.json.signal.db  14 sesiones, 14 identidades,
                            8 sender keys, 9 claves de app-state  (del 84)

Una vinculacion de minutos no puede tener ese estado heredado. Sintoma visible
en cada mensaje entrante::

    decrypt failed type=pkmsg: unknown one-time pre-key id 66

POR QUE QUEDABA BLOQUEADO
-------------------------
Quien cierra el Signal Store es ``Client.disconnect()``. En la rama del logout,
``_main`` salia sin cerrar nada, asi que el handle de SQLite seguia vivo y el
archivado no podia mover el fichero. De paso quedaban tareas colgadas: los
"Task was destroyed but it is pending! post-connect".

LO QUE FIJAN ESTAS PRUEBAS
--------------------------
Que se cierre al salir por logout, y que NO se vincule de nuevo si algun
archivo criptografico de la sesion anterior sigue en su sitio.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.core.session_state import AppState

# Los archivos que forman una sesion de pywhats. Los dos primeros son
# criptograficos y no pueden separarse.
CRIPTOGRAFICOS = ("device.json", "device.json.signal.db")


@pytest.fixture
def sesion_completa(settings, tmp_path):
    """Un runtime con los DOS archivos de una sesion, como en la realidad."""
    import dataclasses

    from app.core.runtime import AppRuntime

    aislado = dataclasses.replace(
        settings,
        session_dir=tmp_path / "session",
        diagnostics_dir=tmp_path / "diagnostics",
    )
    (tmp_path / "session").mkdir(parents=True, exist_ok=True)
    (tmp_path / "diagnostics").mkdir(parents=True, exist_ok=True)

    aislado.session_file.write_text(
        json.dumps(
            {
                "jid": {"user": "573002389304"},
                "device_id": 84,
                "registration_id": 1403204623,
            }
        ),
        encoding="utf-8",
    )
    # Un Signal Store con estado, como el que se hereda.
    con = sqlite3.connect(str(aislado.signal_store_file))
    con.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, state BLOB)")
    con.execute("INSERT INTO sessions VALUES ('573243116421:0@s.whatsapp.net', x'00')")
    con.commit()
    con.close()

    rt = AppRuntime(aislado, owner="pytest", configure_logging=False)
    rt.pairing._on_renew = lambda: None
    return rt


def _rechazar(runtime, veces: int) -> None:
    for _ in range(veces):
        runtime._sesion_rechazada(401)


# ---------------------------------------------------------------------------
# Los dos archivos se van juntos
# ---------------------------------------------------------------------------


def test_se_archivan_los_dos_archivos_criptograficos(sesion_completa):
    _rechazar(sesion_completa, 3)

    assert sesion_completa.session_exists is False
    assert sesion_completa.settings.signal_store_file.exists() is False, (
        "el Signal Store lleva el estado construido BAJO la identidad del "
        "device.json: separarlos produce una identidad mezclada"
    )

    archivadas = list(sesion_completa.settings.diagnostics_dir.glob("session-*"))
    assert archivadas
    guardados = {p.name for p in archivadas[0].iterdir()}
    for nombre in CRIPTOGRAFICOS:
        assert nombre in guardados, f"{nombre} tiene que viajar con la sesion"


def test_si_el_store_queda_bloqueado_no_se_vincula(sesion_completa, monkeypatch):
    """El caso EXACTO que se midio: device.json se va, el store se queda.

    Vincular ahi produce un device.json nuevo sobre un store viejo. Antes que
    eso, se para con un error que dice que hacer.
    """
    import app.whatsapp_client as cliente_module

    real = cliente_module.archive_session

    def archivar_solo_el_device(settings, reason):
        """Simula el bloqueo: se lleva el device.json y no el store."""
        destino = settings.diagnostics_dir / f"session-parcial-{reason}"
        destino.mkdir(parents=True, exist_ok=True)
        settings.session_file.replace(destino / "device.json")
        return destino

    monkeypatch.setattr(cliente_module, "archive_session", archivar_solo_el_device)

    _rechazar(sesion_completa, 3)

    assert sesion_completa.state.state is AppState.ERROR, (
        "con el store viejo en su sitio, vincular crearia una identidad mezclada"
    )
    assert sesion_completa.settings.signal_store_file.exists() is True


def test_el_error_dice_que_archivo_falta(sesion_completa, monkeypatch, caplog):
    import logging

    import app.whatsapp_client as cliente_module

    monkeypatch.setattr(cliente_module, "archive_session", lambda *a, **k: None)

    with caplog.at_level(logging.ERROR):
        _rechazar(sesion_completa, 3)

    texto = "\n".join(r.getMessage() for r in caplog.records)
    assert "device.json" in texto
    assert "--fresh" in texto


def test_no_se_toca_postgresql_al_descartar():
    import ast
    import inspect
    import textwrap

    from app.core.runtime import AppRuntime

    arbol = ast.parse(
        textwrap.dedent(inspect.getsource(AppRuntime._descartar_sesion_revocada))
    )
    nombres = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Attribute):
            nombres.append(nodo.attr)
        elif isinstance(nodo, ast.Name):
            nombres.append(nodo.id)

    for prohibido in ("database", "execute", "Message", "Chat", "MediaFile"):
        assert prohibido not in nombres


# ---------------------------------------------------------------------------
# El cierre que liberaba el handle
# ---------------------------------------------------------------------------


def test_al_hacer_logout_se_cierra_la_sesion():
    """Quien cierra el Signal Store es ``disconnect()``, dentro de _shutdown.

    Salir de ``_main`` sin cerrar dejaba el handle de SQLite vivo, y por eso
    el archivado no podia mover el fichero.
    """
    import inspect

    from app.whatsapp_client import WhatsAppClient

    fuente = inspect.getsource(WhatsAppClient._main)
    desde_logout = fuente[fuente.index("self._logged_out") :]
    corte = desde_logout.index("return")
    assert "_shutdown" in desde_logout[:corte], (
        "la rama del logout tiene que cerrar antes de salir"
    )


def test_el_cierre_cancela_las_tareas_en_vuelo():
    """Era el origen de los "Task was destroyed but it is pending!"."""
    import inspect

    from app.whatsapp_client import WhatsAppClient

    fuente = inspect.getsource(WhatsAppClient._shutdown)
    assert "cancel()" in fuente
    assert "gather" in fuente


def test_pywhats_cierra_el_store_en_disconnect():
    """La suposicion en la que se apoya el arreglo, comprobada en el paquete."""
    import inspect

    from pywhats.client import Client

    fuente = inspect.getsource(Client)
    assert "store.close()" in fuente, (
        "si pywhats dejara de cerrarlo ahi, habria que cerrarlo nosotros"
    )
