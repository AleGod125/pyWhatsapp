"""Un socket muerto se levanta otra vez. No se borra nada.

EL FALLO, MEDIDO
----------------
En una ejecucion real de ``service.py``::

    app ping failed 1/3
    app ping failed 2/3
    app ping failed 3/3
    NotConnected -> peer presumed dead -> closing connection

Detectar el socket muerto estaba bien. Lo que faltaba era volver a levantarlo:
``_main`` se quedaba esperando ``wait_closed()`` y, al cumplirse, terminaba. El
proceso seguia vivo, Flask seguia respondiendo "Conectado", y no entraba ni un
mensaje mas. La web oficial si los recibia.

LO QUE NO SE HACE AL RECONECTAR
-------------------------------
No se borra ``device.json``, ni el Signal Store, ni las prekeys, ni se pide un
QR. Un socket que se cae no invalida la vinculacion, y tratarlo asi obligaria
a escanear un codigo cada vez que se cae el WiFi.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.session_state import LINKED_STATES, AppState


# ---------------------------------------------------------------------------
# El estado se cuenta como es
# ---------------------------------------------------------------------------


def test_existe_un_estado_para_la_reconexion():
    """``CONNECTED`` con el socket muerto engana al frontend."""
    assert AppState.RECONNECTING.value == "RECONNECTING"
    assert AppState.RECONNECTING is not AppState.CONNECTED


def test_reconectando_sigue_siendo_una_cuenta_vinculada():
    """Se perdio la conexion, no la vinculacion: no hay que escanear nada."""
    assert AppState.RECONNECTING in LINKED_STATES


def test_la_api_no_dice_conectado_mientras_reconecta():
    """Flask puede seguir escuchando con WhatsApp muerto. No vale mentir."""
    from app.api.serializers import state_to_json
    from app.core.session_state import SessionState

    class _RuntimeFalso:
        owner = "service.py"
        session_exists = True
        decrypt_errors = 0

        def __init__(self):
            self.state = SessionState()
            self.pairing = None
            self.settings = None

        def info(self):
            return type("I", (), {"whatsapp_enabled": True})()

    rt = _RuntimeFalso()
    rt.state.set(AppState.RECONNECTING, reason="prueba")

    cuerpo = state_to_json(rt)
    assert cuerpo["state"] == "RECONNECTING"
    assert cuerpo["connected"] is False, (
        "si dice conectado, el frontend pinta 'Conectado' sobre un socket muerto"
    )
    assert cuerpo["linked"] is True, "la vinculacion sigue viva"
    assert cuerpo["pairing_required"] is False, "no hay que escanear ningun QR"


def test_el_runtime_traduce_los_eventos_de_reconexion(settings, tmp_path):
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

    class _Evento:
        def __init__(self, nombre, carga=None):
            self.name = nombre
            self.payload = carga
            self.generation = rt.pairing.connection_generation

    rt._observar_evento(_Evento("reconnecting", {"attempt": 2}))
    assert rt.state.state is AppState.RECONNECTING
    assert rt.sync_state == "RECONNECTING"

    rt._observar_evento(_Evento("reconnected"))
    assert rt.state.state is AppState.CONNECTED
    assert rt.sync_state == "WATCHING"


def test_el_frontend_recibe_los_dos_eventos():
    from app.api.routes import EVENT_NAMES

    assert EVENT_NAMES["reconnecting"] == "session.state"
    assert EVENT_NAMES["reconnected"] == "session.state"


# ---------------------------------------------------------------------------
# El bucle de reconexion
# ---------------------------------------------------------------------------


class _ClienteFalso:
    """``WhatsAppClient`` con lo justo para ejercitar ``_reconectar``."""

    def __init__(self, settings, *, fallos: int = 0):
        from app.whatsapp_client import WhatsAppClient

        self._settings = settings
        self._logged_out = False
        self._reconnects = 0
        self._shutdown_requested = asyncio.Event()
        self._shutdown_timeout = 1.0
        self.post_connect = None
        self.sinks = {}
        self.publicados = []
        self._client = None
        self._fallos_restantes = fallos
        self.conexiones = 0
        # Se toman los metodos REALES: es el codigo que corre en produccion.
        self._reconectar = WhatsAppClient._reconectar.__get__(self)
        # Se toma tambien el calculo de la espera, pero sin esperar de verdad:
        # la prueba mide el comportamiento, no la paciencia.
        self._espera_reconexion = lambda intento: 0.0
        self.RECONEXION_ESPERAS = WhatsAppClient.RECONEXION_ESPERAS
        self.RECONEXION_MAXIMA = WhatsAppClient.RECONEXION_MAXIMA

    def _publish(self, nombre, carga=None, **extra):
        self.publicados.append((nombre, carga))

    def _register_handlers(self, cliente):
        pass

    async def _shutdown(self, timeout):
        pass

    async def _run_post_connect(self):
        pass


def test_al_reconectar_se_reconcilia_lo_reciente():
    """Los mensajes que entraron con el socket muerto no dieron ningun evento."""
    import inspect

    from app.core.orchestrator import Orchestrator

    fuente = inspect.getsource(Orchestrator.post_connect)
    assert "_reconexion" in fuente
    assert "_reconciliar_tras_reconectar" in fuente


def test_la_reconciliacion_no_excava_los_cuarenta_chats():
    """Un backfill completo por cada caida de WiFi seria abusivo."""
    import ast
    import inspect
    import textwrap

    from app.core.orchestrator import Orchestrator

    fuente = textwrap.dedent(
        inspect.getsource(Orchestrator._reconciliar_tras_reconectar)
    )
    arbol = ast.parse(fuente)
    # Se recogen tambien los atributos SIN llamar: ``run_maintenance`` se pasa
    # como funcion a ``to_thread``, asi que no aparece como una llamada.
    nombres = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Attribute):
            nombres.append(nodo.attr)
        elif isinstance(nodo, ast.Name):
            nombres.append(nodo.id)

    assert "_run_backfill" not in nombres
    assert "run" not in nombres, "no se lanza el backfill completo"
    assert "run_maintenance" in nombres, "se reconcilia, que es otra cosa"


# ---------------------------------------------------------------------------
# Peticiones en vuelo cuando se cae la linea
# ---------------------------------------------------------------------------


def test_una_caida_despierta_las_esperas_en_vuelo(settings, session):
    """Esperar 45s por algo imposible no solo pierde tiempo: MIENTE.

    Se midio: dos chats recibieron ACK, no llego respuesta, y acto seguido el
    socket murio (``app ping failed 3/3``). Esas dos esperas se apuntaron como
    "el telefono no contesto", cuando el telefono no tuvo ocasion.
    """
    from app.services.backfill_service import BackfillService, _Pending
    from tests.test_backfill_accounting import _DatabaseFalsa

    backfill = BackfillService(settings, _DatabaseFalsa(session))
    backfill._pending["99911122233@lid"] = _Pending(chat_jid="99911122233@lid")
    backfill._pending["99944455566@lid"] = _Pending(chat_jid="99944455566@lid")

    cortadas = backfill.abort_pending()

    assert cortadas == 2
    for pending in backfill._pending.values():
        assert pending.transport_lost is True
        assert pending.event.is_set()


def test_sin_nada_en_vuelo_no_hay_nada_que_cortar(settings, session):
    from app.services.backfill_service import BackfillService
    from tests.test_backfill_accounting import _DatabaseFalsa

    assert BackfillService(settings, _DatabaseFalsa(session)).abort_pending() == 0


def test_un_corte_de_linea_no_es_un_timeout(settings, session):
    """Son dos conclusiones distintas y no pueden compartir estado.

    ``timeout`` dice algo sobre el TELEFONO: no contesto. Un corte de linea no
    dice nada sobre el, asi que el chat vuelve a 'pending' con su cursor.
    """
    import inspect
    import textwrap

    from app.services.backfill_service import BackfillService

    fuente = textwrap.dedent(
        inspect.getsource(BackfillService._process_chat_locked)
    )
    assert "_last_transport_lost" in fuente
    assert '"pending", "transporte perdido; se reintentara"' in fuente


def test_el_transporte_perdido_no_suma_un_timeout(settings, session):
    """El contador de timeouts tiene que seguir significando lo que dice."""
    import ast
    import inspect
    import textwrap

    from app.services.backfill_service import BackfillService

    arbol = ast.parse(
        textwrap.dedent(inspect.getsource(BackfillService._process_chat_locked))
    )
    # Se busca la rama del transporte: dentro de ella no puede incrementarse
    # ``stats.timeouts``.
    fuente = ast.unparse(arbol)
    rama = fuente.split("_last_transport_lost", 1)[1].split("return", 1)[0]
    assert "timeouts" not in rama


def test_el_runtime_corta_las_esperas_al_perder_el_transporte(settings, tmp_path):
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

    class _BackfillFalso:
        def __init__(self):
            self.cortes = 0

        def abort_pending(self, reason="x"):
            self.cortes += 1
            return 2

    rt.backfill = _BackfillFalso()

    class _Evento:
        name = "transport_lost"
        payload = None
        generation = rt.pairing.connection_generation

    rt._observar_evento(_Evento())

    assert rt.backfill.cortes == 1
    assert rt.counters["transport_aborted_requests"] == 2


# ---------------------------------------------------------------------------
# El socket que muere SIN anunciarlo
# ---------------------------------------------------------------------------
#
# Se creia cubierto y no lo estaba. La reconexion colgaba entera de
# `wait_closed()`, que solo despierta cuando el cliente activa su evento
# `_closed`, y eso pasa en dos sitios: cuando termina el grupo de tareas del
# receptor, o dentro de `Client.disconnect()`. Hay una muerte en la que no
# ocurre ninguno de los dos, y se midio entera::
#
#     12:23:33  el socket deja de contestar
#     12:24:03  app ping failed (1/3)  -> TimeoutError
#     12:24:33  app ping failed (2/3)  -> NotConnected: ya estaba muerto
#     12:25:03  peer presumed dead; closing connection
#               ... y NO aparece "client: session closed"
#               ... y NO aparece "Conexion perdida. Reintento 1"
#
# El manejador del keepalive llama a `sock.disconnect()` sobre un socket que
# llevaba noventa segundos cerrado, y ese camino ya no despierta a nadie. El
# proceso se quedaba vivo, en CONNECTED, y sordo: ni un mensaje entrante mas,
# sin un solo error que lo dijera.


class _SocketDePrueba:
    def __init__(self, *, cerrado=False, ws=object()):
        self._closed = cerrado
        self._ws = ws


class _ClienteConSocket:
    def __init__(self, sock):
        self._sock = sock


def _cliente_de_prueba():
    """Un ``WhatsAppClient`` sin arrancar, para preguntarle por el transporte."""
    import queue

    from app.core.config import load_settings
    from app.whatsapp_client import WhatsAppClient

    return WhatsAppClient(load_settings(), queue.Queue())


