"""Lo que NO puede volver a pasar: sesiones destruidas solas y datos expuestos.

DE DONDE SALE CADA PRUEBA
-------------------------
Todas vienen de un incidente medido, no de una lista de buenas practicas:

* el archivado automatico de la sesion al recibir un 401 produjo 74 logins y
  61 QR en segundos, 99 carpetas vacias y una sesion corrupta;
* la API sirve la copia entera de las conversaciones sin pedir contrasena, asi
  que escuchar fuera de ``localhost`` tiene que ser una decision explicita;
* las rutas de multimedia construyen ficheros a partir de datos de la base, y
  una ruta que se escape del directorio serviria cualquier fichero del disco.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# La sesion no se destruye sola
# ---------------------------------------------------------------------------


def _funcion(ruta: str, nombre: str) -> ast.FunctionDef:
    arbol = ast.parse(Path(ruta).read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.FunctionDef) and nodo.name == nombre:
            return nodo
    raise AssertionError(f"{nombre} no existe en {ruta}")


def test_el_manejo_de_un_401_no_archiva_la_sesion():
    """Archivar es destruir la vinculacion; un 401 puede ser algo pasajero.

    Se mira el ARBOL, no el texto: el modulo menciona ``archive_session`` en
    la explicacion de por que ya no la llama, y buscarlo como cadena daria un
    falso positivo.
    """
    funcion = _funcion("app/core/runtime.py", "_sesion_rechazada")
    llamadas = [
        nodo.func.id
        for nodo in ast.walk(funcion)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name)
    ]
    assert "archive_session" not in llamadas


def test_archivar_sigue_disponible_para_cuando_el_usuario_lo_pide():
    """No se ha quitado la capacidad: se ha quitado el automatismo."""
    from app.whatsapp_client import archive_session

    assert callable(archive_session)
    fuente = Path("main.py").read_text(encoding="utf-8")
    assert 'reason="fresh"' in fuente, "--fresh es la via explicita para archivar"


def test_tres_rechazos_de_la_misma_sesion_detienen_el_intento():
    from app.core.runtime import AppRuntime

    assert AppRuntime.MAX_RECHAZOS_MISMA_SESION == 3


class _RuntimeFalso:
    """Lo minimo para ejercitar ``_sesion_rechazada`` sin abrir nada."""

    MAX_RECHAZOS_MISMA_SESION = 3

    def __init__(self, huella: str | None):
        from app.core.session_state import SessionState

        self._huella = huella
        self._huella_rechazada = None
        self._rechazos_seguidos = 0
        self.state = SessionState()
        self.pairing = _PairingFalso()
        # El manejo del 401 registra si queda una sesion guardada: es el dato
        # que decide si el siguiente intento sera un login o un codigo nuevo.
        self.session_exists = True

    fingerprint = property(lambda self: self._huella)

    _sesion_rechazada = None  # se asigna abajo


class _PairingFalso:
    def __init__(self):
        self.desvinculado = 0
        self.generaciones = 0

    def note_unlinked(self):
        self.desvinculado += 1

    def next_generation(self):
        self.generaciones += 1


@pytest.fixture
def rechazable():
    from app.core.runtime import AppRuntime

    _RuntimeFalso._sesion_rechazada = AppRuntime._sesion_rechazada
    return _RuntimeFalso


def test_los_dos_primeros_rechazos_reintentan(rechazable):
    from app.core.session_state import AppState

    rt = rechazable("huella-1")
    rt._sesion_rechazada("401")
    assert rt.state.state is AppState.SESSION_INVALID
    assert rt.pairing.generaciones == 1

    rt._sesion_rechazada("401")
    assert rt.state.state is AppState.SESSION_INVALID
    assert rt.pairing.generaciones == 2


def test_el_tercer_rechazo_descarta_la_sesion_revocada(rechazable):
    """Tres veces la misma respuesta del servidor no es ambiguedad.

    Antes se paraba con ERROR sin tocar nada. Sonaba prudente y dejaba al
    usuario sin salida: la sesion muerta seguia ahi, el cliente nuevo hacia
    login con ella, y no se generaba ningun QR. Ahora se archiva y se pide uno.
    """
    rt = rechazable("huella-1")
    llamadas = []
    rt._descartar_sesion_revocada = lambda motivo: llamadas.append(motivo)

    for _ in range(3):
        rt._sesion_rechazada("401")

    assert llamadas == ["401"], "el tercero descarta la sesion revocada"
    assert rt.pairing.generaciones == 2, (
        "los dos primeros si piden reintento; el tercero cambia de estrategia"
    )


def test_una_sesion_distinta_reinicia_la_cuenta(rechazable):
    """Un 401 de la sesion vieja no puede penalizar a la nueva."""
    from app.core.session_state import AppState

    rt = rechazable("huella-1")
    rt._sesion_rechazada("401")
    rt._sesion_rechazada("401")

    rt._huella = "huella-2"
    rt._sesion_rechazada("401")

    assert rt._rechazos_seguidos == 1
    assert rt.state.state is AppState.SESSION_INVALID


# ---------------------------------------------------------------------------
# La API no se expone por descuido
# ---------------------------------------------------------------------------


def test_por_defecto_escucha_solo_en_local(settings):
    assert settings.api_host == "127.0.0.1"
    assert settings.allow_remote_api is False


@pytest.mark.parametrize(
    "host,local",
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("0.0.0.0", False),
        ("192.168.1.50", False),
        ("", False),
    ],
)
def test_que_cuenta_como_local(host, local):
    """``0.0.0.0`` es TODAS las interfaces, no ninguna: es el error clasico."""
    import service

    assert service._es_local(host) is local


def test_el_env_de_ejemplo_documenta_el_riesgo():
    ejemplo = Path(".env.example").read_text(encoding="utf-8")
    assert "ALLOW_REMOTE_API=false" in ejemplo
    assert "contrasena" in ejemplo.lower() or "autenticacion" in ejemplo.lower()


# ---------------------------------------------------------------------------
# Multimedia: nada fuera de su directorio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ruta",
    [
        "../../../etc/passwd",
        "..\\..\\..\\Windows\\System32\\config\\SAM",
        "/etc/shadow",
        "C:\\Windows\\win.ini",
    ],
)
def test_una_ruta_que_se_escapa_del_directorio_no_se_sirve(
    settings, ruta, monkeypatch
):
    """``local_path`` viene de la base, y la base la escriben datos de fuera."""
    from app.api import routes

    monkeypatch.setattr(
        routes, "runtime", lambda: type("R", (), {"settings": settings})()
    )
    assert routes._archivo_local({"id": 1, "local_path": ruta}) is None


def test_un_fichero_dentro_del_directorio_si_se_sirve(settings, tmp_path, monkeypatch):
    from app.api import routes

    dentro = settings.media_dir / "prueba_seguridad.bin"
    dentro.parent.mkdir(parents=True, exist_ok=True)
    dentro.write_bytes(b"contenido")
    try:
        monkeypatch.setattr(
            routes, "runtime", lambda: type("R", (), {"settings": settings})()
        )
        assert routes._archivo_local({"id": 1, "local_path": str(dentro)}) == dentro
    finally:
        dentro.unlink(missing_ok=True)
