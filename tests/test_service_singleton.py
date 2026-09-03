"""Un solo ``service.py`` manda sobre la sesion, y se sabe cual.

EL PROBLEMA, MEDIDO
-------------------
Habia dos procesos a la vez::

    PID 19436   .venv\\Scripts\\python.exe service.py
    PID 18516   Python311\\python.exe service.py

El cerrojo protegio la sesion, si. Pero el segundo proceso se quedo VIVO
sirviendo la API en modo lectura sin decirlo, porque la comprobacion del
cerrojo ocurria en un hilo de fondo DESPUES de que Flask ya estuviera
escuchando. Resultado: dos APIs, logs mezclados, y ninguna forma de saber
contra cual se estaba probando.

Lo que se arregla aqui:

* la comprobacion pasa a ser lo PRIMERO del arranque;
* una segunda instancia sale con codigo 5 en vez de degradar en silencio;
* solo ``--local`` abre la API sin sesion, y siempre a proposito;
* el cerrojo guarda interprete y linea de ordenes, para distinguir los dos;
* nunca se mata al otro proceso: solo se informa.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

import service
from app.core.lock import LockInfo, SessionLock, probe


@pytest.fixture
def cliente(settings, database, session, tmp_path):
    """API real sobre la base del test, con la sesion en un temporal."""
    import dataclasses

    from app.api import create_app
    from app.core.runtime import AppRuntime
    from tests.test_api import _DatabaseShim

    aislado = dataclasses.replace(
        settings,
        session_dir=tmp_path / "api-session",
        diagnostics_dir=tmp_path / "api-diagnostics",
    )
    (tmp_path / "api-session").mkdir(parents=True, exist_ok=True)
    (tmp_path / "api-diagnostics").mkdir(parents=True, exist_ok=True)

    rt = AppRuntime(aislado, owner="pytest", configure_logging=False)
    rt.database = _DatabaseShim(database, session)
    aplicacion = create_app(rt)
    aplicacion.config.update(TESTING=True)
    return aplicacion.test_client()


@pytest.fixture
def sesion_dir(tmp_path):
    destino = tmp_path / "session"
    destino.mkdir()
    return destino


# ---------------------------------------------------------------------------
# Identidad del proceso en el cerrojo
# ---------------------------------------------------------------------------


def test_el_cerrojo_guarda_la_identidad_del_proceso(sesion_dir):
    """El PID solo no distingue el service.py del venv del global."""
    cerrojo = SessionLock(sesion_dir, owner="service.py")
    cerrojo.acquire()
    try:
        datos = json.loads((sesion_dir / "runtime.lock").read_text(encoding="utf-8"))
        assert datos["pid"] == os.getpid()
        assert datos["owner"] == "service.py"
        assert datos["executable"], "hace falta saber con que interprete se arranco"
        assert "command_line" in datos
    finally:
        cerrojo.release()


def test_un_cerrojo_antiguo_sin_los_campos_nuevos_se_lee_igual(sesion_dir):
    """No se puede romper por encontrarse un cerrojo escrito antes."""
    (sesion_dir / "runtime.lock").write_text(
        json.dumps({"pid": 123, "owner": "service.py", "acquired_at": "ayer"}),
        encoding="utf-8",
    )
    titular = SessionLock(sesion_dir, owner="otro").read()
    assert titular is not None
    assert titular.pid == 123
    assert titular.executable == ""


def test_describe_identifica_el_proceso():
    texto = LockInfo(
        pid=18516,
        owner="service.py",
        acquired_at="2026-09-02 18:35:37",
        executable=r"C:\Python311\python.exe",
        command_line="service.py",
    ).describe()
    assert "18516" in texto
    assert "Python311" in texto


# ---------------------------------------------------------------------------
# probe: quien manda, sin tomar el cerrojo
# ---------------------------------------------------------------------------


def test_sin_cerrojo_la_sesion_esta_libre(sesion_dir):
    assert probe(sesion_dir) is None


def test_con_cerrojo_vivo_se_sabe_quien_lo_tiene(sesion_dir):
    cerrojo = SessionLock(sesion_dir, owner="service.py")
    cerrojo.acquire()
    try:
        titular = probe(sesion_dir)
        assert titular is not None
        assert titular.pid == os.getpid()
    finally:
        cerrojo.release()


def test_un_cerrojo_de_un_proceso_muerto_no_bloquea(sesion_dir):
    """Windows reutiliza los PID: el latido es lo que decide, no el numero."""
    (sesion_dir / "runtime.lock").write_text(
        json.dumps({"pid": 999999, "owner": "service.py", "acquired_at": "ayer"}),
        encoding="utf-8",
    )
    assert probe(sesion_dir) is None


def test_probe_no_toma_el_cerrojo(sesion_dir):
    """Sondear no puede tener efectos: solo mira."""
    probe(sesion_dir)
    assert not (sesion_dir / "runtime.lock").exists()


# ---------------------------------------------------------------------------
# El arranque
# ---------------------------------------------------------------------------


def test_una_segunda_instancia_sale_con_codigo_claro(settings, sesion_dir, monkeypatch):
    """Antes seguia viva sirviendo la API sin decirlo."""
    import dataclasses

    cerrojo = SessionLock(sesion_dir, owner="service.py")
    cerrojo.acquire()
    try:
        aislado = dataclasses.replace(settings, session_dir=sesion_dir)
        monkeypatch.setattr(service, "load_settings", lambda: aislado)
        assert service.main([]) == service.SALIDA_YA_HAY_SERVICIO
    finally:
        cerrojo.release()


def test_la_segunda_instancia_no_llega_a_construir_el_runtime(
    settings, sesion_dir, monkeypatch
):
    """Sale ANTES de tocar nada: ni base de datos, ni Flask, ni sesion."""
    import dataclasses

    construido = []

    cerrojo = SessionLock(sesion_dir, owner="service.py")
    cerrojo.acquire()
    try:
        aislado = dataclasses.replace(settings, session_dir=sesion_dir)
        monkeypatch.setattr(service, "load_settings", lambda: aislado)
        import app.core.runtime as runtime_module

        monkeypatch.setattr(
            runtime_module,
            "build_runtime",
            lambda **k: construido.append(k) or pytest.fail("no debio construirse"),
        )
        assert service.main([]) == service.SALIDA_YA_HAY_SERVICIO
        assert construido == []
    finally:
        cerrojo.release()


def test_local_si_puede_arrancar_con_la_sesion_ocupada(settings, sesion_dir, monkeypatch):
    """El modo lectura sigue existiendo, pero solo si se pide."""
    import dataclasses

    cerrojo = SessionLock(sesion_dir, owner="service.py")
    cerrojo.acquire()
    try:
        aislado = dataclasses.replace(settings, session_dir=sesion_dir)
        monkeypatch.setattr(service, "load_settings", lambda: aislado)
        # --check corta justo despues de comprobar la base: basta para saber
        # que NO se salio por el cerrojo.
        assert service.main(["--local", "--check"]) == 0
    finally:
        cerrojo.release()


def test_el_arranque_no_mata_procesos():
    """Informar, nunca matar. Se mira el arbol, no el texto del docstring."""
    arbol = ast.parse(Path("service.py").read_text(encoding="utf-8"))
    nombres = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Attribute):
            nombres.append(nodo.attr)
        elif isinstance(nodo, ast.Name):
            nombres.append(nodo.id)
    for prohibido in ("kill", "terminate", "taskkill", "TerminateProcess"):
        assert prohibido not in nombres


def test_un_service_normal_nunca_degrada_a_local_en_silencio():
    """La degradacion silenciosa era justo el fallo. Queda escrito."""
    fuente = Path("service.py").read_text(encoding="utf-8")
    assert "No se iniciara una segunda instancia" in fuente
    assert "SALIDA_YA_HAY_SERVICIO" in fuente


# ---------------------------------------------------------------------------
# Puerto
# ---------------------------------------------------------------------------


def test_un_puerto_libre_no_se_declara_ocupado():
    import socket

    with socket.socket() as libre:
        libre.bind(("127.0.0.1", 0))
        puerto = libre.getsockname()[1]
    # El socket ya esta cerrado: ese puerto esta libre.
    assert service._puerto_ocupado("127.0.0.1", puerto) is False


def test_un_puerto_ocupado_se_detecta():
    import socket

    with socket.socket() as ocupado:
        ocupado.bind(("127.0.0.1", 0))
        ocupado.listen(1)
        puerto = ocupado.getsockname()[1]
        assert service._puerto_ocupado("127.0.0.1", puerto) is True


def test_el_arranque_se_para_si_el_puerto_esta_ocupado(settings, sesion_dir, monkeypatch):
    import dataclasses
    import socket

    with socket.socket() as ocupado:
        ocupado.bind(("127.0.0.1", 0))
        ocupado.listen(1)
        puerto = ocupado.getsockname()[1]

        aislado = dataclasses.replace(settings, session_dir=sesion_dir)
        monkeypatch.setattr(service, "load_settings", lambda: aislado)
        assert (
            service.main(["--local", "--port", str(puerto)])
            == service.SALIDA_PUERTO_OCUPADO
        )


# ---------------------------------------------------------------------------
# Visibilidad por la API
# ---------------------------------------------------------------------------


def test_la_api_dice_quien_controla_la_sesion(cliente):
    cuerpo = cliente.get("/api/v1/session").get_json()
    assert "session_owner_pid" in cuerpo
    assert "session_owner_name" in cuerpo
    assert "this_process_owns_session" in cuerpo


def test_health_tambien_lo_dice(cliente):
    """Es lo primero que se mira cuando algo no cuadra."""
    cuerpo = cliente.get("/api/v1/health").get_json()
    assert "session_owner_pid" in cuerpo
    assert "this_process_owns_session" in cuerpo


def test_no_se_expone_la_ruta_del_interprete(cliente):
    """Las rutas locales no salen por la API, ni siquiera esta."""
    cuerpo = cliente.get("/api/v1/session").get_json()
    assert "executable" not in cuerpo
    assert "command_line" not in cuerpo


def test_check_funciona_aunque_haya_un_servicio_vivo(settings, sesion_dir, monkeypatch):
    """``--check`` no abre sesion ni escucha: no puede estorbar a nadie.

    Rechazarlo por haber otro servicio en marcha impediria justo lo que se
    quiere poder hacer, que es comprobar la configuracion sin parar nada.
    """
    import dataclasses

    cerrojo = SessionLock(sesion_dir, owner="service.py")
    cerrojo.acquire()
    try:
        aislado = dataclasses.replace(settings, session_dir=sesion_dir)
        monkeypatch.setattr(service, "load_settings", lambda: aislado)
        assert service.main(["--check"]) == 0
    finally:
        cerrojo.release()
