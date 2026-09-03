"""Una sesion revocada acaba en un QR nuevo, no en un estado eterno.

EL FALLO, MEDIDO
----------------
Tras rechazar el servidor la sesion con 401, la API se quedaba asi::

    GET /session      state=PAIRING  pairing_in_progress=true
                      qr_available=false  session_file_present=true
    GET /session/qr   available=false  generation=0

Para siempre. El frontend giraba en "Generando codigo seguro..." y no era
culpa suya: no habia ningun QR que mostrar.

LA CAUSA
--------
Al dejar de archivar la sesion en cada 401 -- lo correcto, porque un 401
suelto puede ser un corte de red -- ``restart_pairing`` creaba un cliente
nuevo que encontraba el ``device.json`` muerto. Y ``WhatsAppClient._main``
decide por ahi::

    existed = self.session_exists
    if existed:
        await self._client.connect()                # login -> otro 401
    else:
        await self._connect_with_pairing_retries()  # <- la que emite el QR

Nunca se llegaba a la segunda rama.

EL ARREGLO
----------
Los dos primeros rechazos solo se cuentan. Al TERCERO de la misma sesion ya no
hay ambiguedad -- el servidor lo ha dicho tres veces -- y entonces si se
archiva y se pide un codigo nuevo. Archivar no es borrar, y PostgreSQL no se
toca en ningun caso.
"""

from __future__ import annotations

import json

import pytest

from app.core.session_state import AppState


@pytest.fixture
def runtime(settings, tmp_path):
    """Runtime con la sesion en un temporal: nunca la real."""
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
    rt.pairing._on_renew = lambda: None
    # Una sesion guardada, como la que el servidor acaba de rechazar.
    aislado.session_file.write_text(
        json.dumps({"jid": {"user": "573002389304"}, "device_id": 84}),
        encoding="utf-8",
    )
    return rt


def _rechazar(runtime, veces: int) -> None:
    for _ in range(veces):
        runtime._sesion_rechazada(401)


# ---------------------------------------------------------------------------
# Los primeros rechazos no destruyen nada
# ---------------------------------------------------------------------------


def test_un_rechazo_suelto_no_toca_la_sesion(runtime):
    """Puede ser un corte de red: tirar la vinculacion seria desproporcionado."""
    _rechazar(runtime, 1)

    assert runtime.session_exists is True
    assert runtime.state.state is AppState.SESSION_INVALID


def test_dos_rechazos_tampoco(runtime):
    _rechazar(runtime, 2)
    assert runtime.session_exists is True


# ---------------------------------------------------------------------------
# El tercero: el servidor ya lo ha dicho tres veces
# ---------------------------------------------------------------------------


def test_al_tercer_rechazo_la_sesion_se_aparta(runtime):
    _rechazar(runtime, 3)

    assert runtime.session_exists is False, (
        "sin apartar el device.json, el cliente nuevo vuelve a hacer login y "
        "no se pide ningun codigo"
    )


def test_la_sesion_se_archiva_no_se_borra(runtime):
    _rechazar(runtime, 3)

    archivadas = list(runtime.settings.diagnostics_dir.glob("session-*"))
    assert archivadas, "archivar no es borrar: tiene que quedar copia"
    assert any((d / "device.json").exists() for d in archivadas)


def test_se_pide_una_vinculacion_nueva(runtime):
    _rechazar(runtime, 3)

    assert runtime.state.state is not AppState.ERROR
    assert runtime.state.state in (
        AppState.PAIRING_REQUIRED,
        AppState.PAIRING,
        AppState.CONNECTING,
    )


def test_la_cuenta_se_reinicia_para_la_sesion_nueva(runtime):
    """Empezar sin margen haria que el pairing nuevo muriera al primer fallo."""
    _rechazar(runtime, 3)

    assert runtime._rechazos_seguidos == 0
    assert runtime._huella_rechazada is None
    assert runtime._reintentos_seguidos == 0


def test_no_se_toca_postgresql():
    """Una sesion revocada no tiene nada que ver con la copia guardada."""
    import ast
    import inspect
    import textwrap

    from app.core.runtime import AppRuntime

    fuente = textwrap.dedent(
        inspect.getsource(AppRuntime._descartar_sesion_revocada)
    )
    arbol = ast.parse(fuente)
    nombres = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Attribute):
            nombres.append(nodo.attr)
        elif isinstance(nodo, ast.Name):
            nombres.append(nodo.id)

    for prohibido in ("database", "execute", "Message", "Chat"):
        assert prohibido not in nombres, f"no puede tocar {prohibido}"


# ---------------------------------------------------------------------------
# Nunca un estado zombi
# ---------------------------------------------------------------------------


def test_si_no_se_puede_apartar_la_sesion_se_dice(runtime, monkeypatch):
    """Un error claro es mejor que una vinculacion que no va a ocurrir."""
    import app.whatsapp_client as cliente_module

    # El archivado no se lleva nada: el device.json sigue ahi.
    monkeypatch.setattr(cliente_module, "archive_session", lambda *a, **k: None)

    _rechazar(runtime, 3)

    assert runtime.session_exists is True
    assert runtime.state.state is AppState.ERROR


def test_no_se_anuncia_pairing_si_no_va_a_haber_qr():
    """``_main`` decide por el device.json: con sesion guardada NO emite QR.

    Anunciar PAIRING en ese caso es lo que dejaba al frontend esperando un
    codigo que nunca llegaba.
    """
    import inspect

    from app.core.runtime import AppRuntime

    fuente = inspect.getsource(AppRuntime.restart_pairing)
    assert "session_exists" in fuente
    assert "AppState.CONNECTING" in fuente


def test_la_combinacion_zombi_es_imposible(runtime):
    """state=PAIRING + qr_available=false + session_file_present=true."""
    from app.api.serializers import state_to_json

    _rechazar(runtime, 3)
    cuerpo = state_to_json(runtime)

    zombi = (
        cuerpo["state"] == "PAIRING"
        and cuerpo["qr_available"] is False
        and cuerpo["session_file_present"] is True
    )
    assert not zombi, "ese es exactamente el estado que dejaba al frontend girando"


# ---------------------------------------------------------------------------
# La secuencia REAL, con el cliente muriendo entre rechazos
# ---------------------------------------------------------------------------
#
# Esto es lo que ninguna prueba cubria, y por eso el arreglo anterior parecia
# bueno y en ejecucion no servia: los tests llamaban a ``_sesion_rechazada``
# tres veces seguidas, pero en la realidad el cliente MUERE tras cada 401 y
# entre uno y otro pasa ``_fin_del_cliente``. Ahi estaba el bloqueo.


class _EventoFalso:
    def __init__(self, nombre, carga=None):
        self.name = nombre
        self.payload = carga


@pytest.fixture
def con_cliente_falso(runtime, monkeypatch):
    """Sustituye el cliente por uno que no habla con nadie."""

    class _ClienteFalso:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

        def stop(self, timeout=None):
            pass

    import app.whatsapp_client as cliente_module

    monkeypatch.setattr(cliente_module, "WhatsAppClient", _ClienteFalso)
    monkeypatch.setattr(runtime, "_wire_services", lambda: None)
    # El fixture base anula ``_on_renew`` para que las pruebas de conteo no
    # arranquen nada. Aqui hace falta el REAL: la cadena que se esta
    # comprobando es justo renew() -> restart_pairing().
    runtime.pairing._on_renew = runtime.restart_pairing
    runtime._whatsapp = True
    return runtime


def _ciclo(runtime, veces: int) -> None:
    """Un 401 y la muerte del cliente, tantas veces como se pida."""
    for _ in range(veces):
        runtime._ultimo_reintento = 0.0  # el backoff ya vencio
        runtime._observar_evento(_EventoFalso("logged_out", 401))
        runtime._ultimo_reintento = 0.0
        runtime._observar_evento(_EventoFalso("client_stopped", None))


def test_tras_el_primer_401_se_vuelve_a_intentar(con_cliente_falso):
    """El fallo medido: despues del primero no habia segundo intento.

    ``_fin_del_cliente`` consumia el freno y ademas anunciaba PAIRING. Luego
    ``restart_pairing`` volvia a comprobar el freno, lo encontraba recien
    gastado y se iba sin arrancar nada. No habia segundo 401, ni tercero, ni
    archivado, ni QR.
    """
    from app.core.session_state import AppState

    _ciclo(con_cliente_falso, 1)

    assert con_cliente_falso.state.state is AppState.CONNECTING, (
        "con sesion guardada lo que toca es reintentar el login, no anunciar "
        "un codigo que no va a generarse"
    )
    assert con_cliente_falso.session_exists is True


def test_el_estado_no_se_queda_en_pairing_sin_qr(con_cliente_falso):
    """La combinacion exacta que dejaba al frontend girando."""
    from app.api.serializers import state_to_json

    _ciclo(con_cliente_falso, 1)
    cuerpo = state_to_json(con_cliente_falso)

    assert not (
        cuerpo["state"] == "PAIRING"
        and cuerpo["qr_available"] is False
        and cuerpo["session_file_present"] is True
    )


def test_la_cuenta_avanza_entre_ciclos(con_cliente_falso):
    """Sin reintento no habia forma de llegar al segundo rechazo."""
    _ciclo(con_cliente_falso, 1)
    assert con_cliente_falso._rechazos_seguidos == 1

    _ciclo(con_cliente_falso, 1)
    assert con_cliente_falso._rechazos_seguidos == 2


def test_al_tercer_ciclo_se_archiva_y_se_pide_codigo(con_cliente_falso):
    from app.core.session_state import AppState

    _ciclo(con_cliente_falso, 3)

    assert con_cliente_falso.session_exists is False
    assert con_cliente_falso.state.state is AppState.PAIRING
    assert list(con_cliente_falso.settings.diagnostics_dir.glob("session-*"))


def test_el_freno_se_consume_una_sola_vez():
    """Dos consumos en la misma vuelta dejaban el sistema sin arrancar nada."""
    import inspect

    from app.core.runtime import AppRuntime

    fuente = inspect.getsource(AppRuntime._fin_del_cliente)
    assert "_puede_reintentar" not in fuente, (
        "el freno lo decide restart_pairing, y solo el"
    )
