"""Una sesion es UNA CARPETA, y se va entera o no se va ninguna.

LA CONTAMINACION, MEDIDA
------------------------
Tras un 401 se archivo la sesion revocada y el log dijo::

    Sesion archivada en ... (3 archivos: compat_prekey.db, device.json,
    runtime.lock; bloqueados: device.json.signal.db)

El almacen de Signal se quedo. La vinculacion siguiente creo credenciales
nuevas encima, y el resultado fue una identidad que no es de nadie: claves
nuevas usando ratchets viejos. Sintoma visible en cada mensaje entrante::

    decrypt failed type=pkmsg: unknown one-time pre-key id 66

CON BAILEYS ES LA MISMA LEY, CON OTRA FORMA
-------------------------------------------
``useMultiFileAuthState`` guarda las credenciales y el almacen de Signal en la
MISMA carpeta: ``creds.json`` mas un fichero por sesion, por pre-key y por
sender key. Llevarse media carpeta reproduce exactamente el mismo desastre,
asi que la unidad que se archiva es la carpeta entera.

LO QUE FIJAN ESTAS PRUEBAS
--------------------------
Que se archive la carpeta completa, y que NO se vincule de nuevo si queda
algo de la sesion anterior en su sitio.
"""

from __future__ import annotations

import json

import pytest

from app.core.session_state import AppState


@pytest.fixture
def sesion_completa(settings, tmp_path):
    """Un runtime con una carpeta de sesion como la de la realidad."""
    import dataclasses

    from app.core.runtime import AppRuntime

    aislado = dataclasses.replace(
        settings,
        session_dir=tmp_path / "session",
        diagnostics_dir=tmp_path / "diagnostics",
    )
    (tmp_path / "diagnostics").mkdir(parents=True, exist_ok=True)

    carpeta = aislado.session_dir_baileys
    carpeta.mkdir(parents=True, exist_ok=True)
    aislado.session_file.write_text(
        json.dumps(
            {
                "me": {"id": "573002389304:84@s.whatsapp.net", "name": "Prueba"},
                "registrationId": 1403204623,
            }
        ),
        encoding="utf-8",
    )
    # El estado de Signal que se hereda si no se va todo junto.
    (carpeta / "session-573243116421.0.json").write_text("{}", encoding="utf-8")
    (carpeta / "pre-key-66.json").write_text("{}", encoding="utf-8")
    (carpeta / "sender-key-12345.json").write_text("{}", encoding="utf-8")

    rt = AppRuntime(aislado, owner="pytest", configure_logging=False)
    rt.pairing._on_renew = lambda: None
    return rt


def _rechazar(runtime, veces: int) -> None:
    for _ in range(veces):
        runtime._sesion_rechazada(401)


# ---------------------------------------------------------------------------
# La carpeta se va entera
# ---------------------------------------------------------------------------


def test_se_archiva_la_carpeta_entera(sesion_completa):
    _rechazar(sesion_completa, 3)

    assert sesion_completa.session_exists is False
    carpeta = sesion_completa.settings.session_dir_baileys
    assert not carpeta.is_dir() or not any(carpeta.iterdir()), (
        "el almacen de Signal lleva el estado construido BAJO esas "
        "credenciales: separarlos produce una identidad mezclada"
    )

    archivadas = list(sesion_completa.settings.diagnostics_dir.glob("session-*"))
    assert archivadas
    guardados = {p.name for p in archivadas[0].iterdir()}
    for nombre in (
        "creds.json",
        "session-573243116421.0.json",
        "pre-key-66.json",
        "sender-key-12345.json",
    ):
        assert nombre in guardados, f"{nombre} tiene que viajar con la sesion"


def test_si_queda_estado_de_signal_no_se_vincula(sesion_completa, monkeypatch):
    """El caso EXACTO que se midio: las credenciales se van, el estado se queda.

    Vincular ahi produce claves nuevas sobre ratchets viejos. Antes que eso,
    se para con un error que dice que hacer.
    """
    import app.wa.sesion as sesion_module

    def archivar_solo_las_credenciales(settings, reason):
        """Simula el bloqueo: se lleva creds.json y no el resto."""
        destino = settings.diagnostics_dir / f"session-parcial-{reason}"
        destino.mkdir(parents=True, exist_ok=True)
        settings.session_file.replace(destino / "creds.json")
        return destino

    monkeypatch.setattr(sesion_module, "archive_session", archivar_solo_las_credenciales)

    _rechazar(sesion_completa, 3)

    assert sesion_completa.state.state is AppState.ERROR, (
        "con el estado viejo en su sitio, vincular crearia una identidad mezclada"
    )
    assert (
        sesion_completa.settings.session_dir_baileys / "pre-key-66.json"
    ).exists() is True


def test_el_error_dice_que_queda_ahi(sesion_completa, monkeypatch, caplog):
    import logging

    import app.wa.sesion as sesion_module

    monkeypatch.setattr(sesion_module, "archive_session", lambda *a, **k: None)

    with caplog.at_level(logging.ERROR):
        _rechazar(sesion_completa, 3)

    texto = "\n".join(r.getMessage() for r in caplog.records)
    assert "creds.json" in texto
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
# El cierre, que antes dejaba el fichero bloqueado
# ---------------------------------------------------------------------------


def test_el_worker_se_para_al_cerrar():
    """Quien tenia los ficheros abiertos era el proceso de Node.

    Con pywhats el handle de SQLite se quedaba vivo al salir por logout y el
    archivado no podia mover el Signal Store. Aqui el equivalente es que
    ``stop()`` termine el proceso hijo: mientras viva, Windows mantiene sus
    ficheros bloqueados y el archivado se queda a medias.
    """
    import inspect

    from app.wa.baileys_client import BaileysClient

    fuente = inspect.getsource(BaileysClient.stop)
    assert "terminate" in fuente or "kill" in fuente, (
        "si el worker sigue vivo, sus ficheros siguen bloqueados"
    )


def test_archivar_nunca_lanza_aunque_haya_bloqueos():
    """La excepcion abortaba el manejo del 401 y disparaba el bucle."""
    import inspect

    from app.wa.sesion import archive_session

    fuente = inspect.getsource(archive_session)
    assert "except OSError" in fuente, (
        "un fichero bloqueado se salta y se anota; no puede abortar el resto"
    )
