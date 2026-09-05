"""Mensajes en tiempo real, sincronizacion manual y ciclo de vida del QR.

Tres bloques:

* el pipeline live: persistir -> clasificar -> evento -> SSE, sin duplicados;
* ``POST /sync/run``: 202, 409 si ya corre, y bloqueado en modo local;
* vinculacion automatica: QR sin pulsar nada, caducidad, renovacion y
  generacion.

Ninguna de estas pruebas abre la sesion de WhatsApp ni toca el protocolo.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("flask")

from app.services import repository as repo  # noqa: E402
from app.services.repository import IncomingMessage  # noqa: E402
from tests.conftest import _DatabaseShim  # noqa: E402

CHAT_JID = "34600333222@s.whatsapp.net"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runtime(settings, database, session, tmp_path):
    """Runtime real, pero con la sesion APUNTANDO A UN TEMPORAL.

    Es imprescindible: al rechazarse un login la aplicacion ARCHIVA la sesion
    (mueve ``device.json`` a ``diagnostics/``). Con los settings de produccion,
    una prueba tocaria la sesion viva del usuario. Se detecto justo asi.
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
    return rt




@pytest.fixture
def chat_vacio(session):
    chat_id = repo.upsert_chat(session, jid=CHAT_JID, chat_type="individual")
    session.flush()
    return chat_id


def _guardar(session, chat_id, wamid: str, texto: str, ts: int = 1_788_000_000) -> int:
    repo.bulk_upsert_messages(
        session,
        {CHAT_JID: chat_id},
        [
            IncomingMessage(
                chat_jid=CHAT_JID,
                timestamp=ts,
                source="live",
                whatsapp_message_id=wamid,
                text=texto,
                message_type="text",
            )
        ],
    )
    repo.refresh_chat_previews(session, [CHAT_JID])
    session.flush()
    from sqlalchemy import select

    from app.models import Message

    return session.execute(
        select(Message.id).where(
            Message.chat_jid == CHAT_JID, Message.whatsapp_message_id == wamid
        )
    ).scalar_one()


# ---------------------------------------------------------------------------
# Pipeline en vivo
# ---------------------------------------------------------------------------


def test_el_mensaje_se_persiste_antes_de_avisar(session, chat_vacio, runtime):
    """El orden importa: la base es la fuente de verdad, el evento es aviso.

    Si el evento saliera primero, el frontend podria pedir un mensaje que
    todavia no existe y recibir un 404.
    """
    from app.api.live_events import translate

    class EventoFalso:
        name = "message_stored"
        extra: dict = {}

        def __init__(self, payload):
            self.payload = payload
            self.extra = {}

    message_id = _guardar(session, chat_vacio, "LIVE0001", "hola en vivo")
    evento = EventoFalso(
        {"chat_id": chat_vacio, "chat_jid": CHAT_JID, "message_id": message_id, "new": True}
    )

    salida = dict(translate(evento, runtime))
    assert "message.created" in salida
    # El mensaje ya estaba en la base cuando se construyo el evento.
    assert salida["message.created"]["message"]["text"] == "hola en vivo"


def test_message_created_lleva_la_burbuja_completa(session, chat_vacio, runtime):
    """Payload suficiente para hacer append sin volver a preguntar."""
    from app.api.live_events import translate

    class EventoFalso:
        name = "message_stored"

        def __init__(self, payload):
            self.payload = payload
            self.extra = {}

    message_id = _guardar(session, chat_vacio, "LIVE0002", "mensaje completo")
    salida = dict(
        translate(
            EventoFalso({"chat_id": chat_vacio, "message_id": message_id, "new": True}),
            runtime,
        )
    )

    burbuja = salida["message.created"]["message"]
    for clave in ("id", "type", "text", "from_me", "timestamp", "sent_at", "preview"):
        assert clave in burbuja
    assert salida["message.created"]["chat_id"] == chat_vacio


def test_un_mensaje_nuevo_tambien_actualiza_el_sidebar(session, chat_vacio, runtime):
    """``chat.updated`` evita recargar los 40 chats por una previa."""
    from app.api.live_events import translate

    class EventoFalso:
        name = "message_stored"

        def __init__(self, payload):
            self.payload = payload
            self.extra = {}

    message_id = _guardar(session, chat_vacio, "LIVE0003", "cambia la previa")
    salida = dict(
        translate(
            EventoFalso({"chat_id": chat_vacio, "message_id": message_id, "new": True}),
            runtime,
        )
    )

    fila = salida["chat.updated"]
    assert fila["chat_id"] == chat_vacio
    assert fila["preview"] == "cambia la previa"
    assert fila["last_message_at"]
    assert fila["message_count"] >= 1


def test_un_duplicado_no_produce_ningun_evento(session, chat_vacio, runtime):
    """La base deduplica por wamid; el SSE no puede saltarse esa verdad.

    Sin esto, un mensaje que llega por History Sync y por el receptor en vivo
    haria aparecer DOS burbujas en una pantalla que no consulta la base.
    """
    from app.api.live_events import translate

    class EventoFalso:
        name = "message_stored"

        def __init__(self, payload):
            self.payload = payload
            self.extra = {}

    message_id = _guardar(session, chat_vacio, "LIVE0004", "solo una vez")
    duplicado = EventoFalso(
        {"chat_id": chat_vacio, "message_id": message_id, "new": False}
    )
    assert translate(duplicado, runtime) == []


def test_la_traduccion_se_memoriza_por_evento(session, chat_vacio, runtime):
    """Cinco pestanas abiertas = una consulta, no cinco."""
    from app.api.live_events import _CACHE_KEY, translate

    class EventoFalso:
        name = "message_stored"

        def __init__(self, payload):
            self.payload = payload
            self.extra = {}

    message_id = _guardar(session, chat_vacio, "LIVE0005", "memorizado")
    evento = EventoFalso(
        {"chat_id": chat_vacio, "message_id": message_id, "new": True}
    )

    primera = translate(evento, runtime)
    assert _CACHE_KEY in evento.extra
    segunda = translate(evento, runtime)
    assert segunda is primera


def test_media_updated_lleva_las_urls(session, chat_vacio, runtime):
    from sqlalchemy import insert

    from app.api.live_events import translate
    from app.models import MediaFile

    class EventoFalso:
        name = "media_ready"

        def __init__(self, payload):
            self.payload = payload
            self.extra = {}

    message_id = _guardar(session, chat_vacio, "LIVE0006", None or "con foto")
    media_id = session.execute(
        insert(MediaFile)
        .values(
            message_id=message_id,
            chat_id=chat_vacio,
            media_type="image",
            mime_type="image/jpeg",
            download_status="downloaded",
            local_path="images/x.jpg",
        )
        .returning(MediaFile.id)
    ).scalar_one()
    session.flush()

    salida = dict(
        translate(
            EventoFalso(
                {
                    "media_id": media_id,
                    "message_id": message_id,
                    "chat_id": chat_vacio,
                    "status": "downloaded",
                }
            ),
            runtime,
        )
    )
    datos = salida["media.updated"]
    assert datos["media_id"] == media_id
    assert datos["message_id"] == message_id
    assert datos["status"] == "downloaded"
    assert datos["file_url"] == f"/api/v1/media/{media_id}/file"
    assert datos["thumbnail_url"] == f"/api/v1/media/{media_id}/thumbnail"
    # Ni rastro de la ruta del disco.
    assert "images/x.jpg" not in str(datos)


def test_el_servicio_live_devuelve_el_id_del_mensaje():
    """Sin el id no se puede servir la burbuja: habria que buscarla dos veces."""
    import inspect

    from app.services.live_service import LiveMessageService

    fuente = inspect.getsource(LiveMessageService._store)
    assert '"message_id": message_id' in fuente
    assert "_resolve_message_id" in fuente


# ---------------------------------------------------------------------------
# Sincronizacion manual
# ---------------------------------------------------------------------------


def test_sync_run_en_modo_local_no_arranca(cliente):
    """Sin sesion de WhatsApp no hay nada que sincronizar."""
    respuesta = cliente.post("/api/v1/sync/run")
    assert respuesta.status_code == 409
    assert respuesta.get_json()["error"]["code"] == "WHATSAPP_DISABLED"


def test_sync_run_sin_conexion_no_arranca_el_backfill(runtime, cliente):
    """Pedir historial sin sesion solo produce timeouts y ensucia el estado."""
    from app.services.sync_job import SyncJob

    runtime._whatsapp = True
    runtime.sync_job = SyncJob(runtime.settings, runtime.database)

    respuesta = cliente.post("/api/v1/sync/run")
    assert respuesta.status_code == 409
    assert respuesta.get_json()["error"]["code"] == "SESSION_NOT_CONNECTED"


def test_sync_run_devuelve_202_y_no_bloquea(runtime, cliente, monkeypatch):
    """202 y vuelve: el ciclo puede durar minutos."""
    import asyncio

    from app.core.session_state import AppState
    from app.services.sync_job import SyncJob

    runtime._whatsapp = True
    runtime.state.set(AppState.CONNECTED, reason="prueba")
    runtime.sync_job = SyncJob(runtime.settings, runtime.database)

    # Un event loop de verdad, en su hilo, como el del cliente real.
    bucle = asyncio.new_event_loop()
    import threading

    hilo = threading.Thread(target=bucle.run_forever, daemon=True)
    hilo.start()

    class ClienteFalso:
        _loop = bucle

    runtime.client = ClienteFalso()
    lanzado: list = []
    monkeypatch.setattr(
        runtime.sync_job, "_ciclo", lambda rt: _corutina_vacia(lanzado)
    )

    comenzo = time.monotonic()
    respuesta = cliente.post("/api/v1/sync/run")
    tardo = time.monotonic() - comenzo

    assert respuesta.status_code == 202
    cuerpo = respuesta.get_json()
    assert cuerpo["started"] is True
    assert cuerpo["state"] == "running"
    assert cuerpo["job_id"]
    assert tardo < 1.0, "la peticion no puede esperar al ciclo"

    bucle.call_soon_threadsafe(bucle.stop)


async def _corutina_vacia(marcador: list) -> None:
    marcador.append(True)


def test_sync_run_rechaza_una_segunda_con_409(runtime, cliente):
    """Dos ciclos a la vez se pisarian los cursores."""
    from app.core.session_state import AppState
    from app.services.sync_job import RUNNING, SyncJob, SyncState

    runtime._whatsapp = True
    runtime.state.set(AppState.CONNECTED, reason="prueba")
    trabajo = SyncJob(runtime.settings, runtime.database)
    trabajo.state = SyncState(state=RUNNING, job_id="enmarcha")
    runtime.sync_job = trabajo

    respuesta = cliente.post("/api/v1/sync/run")
    assert respuesta.status_code == 409
    cuerpo = respuesta.get_json()
    assert cuerpo["error"]["code"] == "SYNC_ALREADY_RUNNING"
    assert cuerpo["sync"]["job_id"] == "enmarcha"


def test_sync_status_informa_del_ciclo(runtime, cliente):
    from app.services.sync_job import RUNNING, SyncJob, SyncState

    trabajo = SyncJob(runtime.settings, runtime.database)
    trabajo.state = SyncState(
        state=RUNNING,
        job_id="abc123",
        phase="backfill",
        started_at="2026-09-02T10:00:00-05:00",
        chats_total=12,
        chats_processed=6,
        messages_new=50,
    )
    runtime.sync_job = trabajo

    cuerpo = cliente.get("/api/v1/sync/status").get_json()
    assert cuerpo["state"] == "running"
    assert cuerpo["job_id"] == "abc123"
    assert cuerpo["phase"] == "backfill"
    assert cuerpo["chats_total"] == 12
    assert cuerpo["chats_processed"] == 6
    assert cuerpo["messages_new"] == 50
    assert cuerpo["connected"] is False
    assert "last_error" in cuerpo


def test_sync_status_informa_de_un_ciclo_terminado(runtime, cliente):
    from app.services.sync_job import COMPLETE, SyncJob, SyncState

    trabajo = SyncJob(runtime.settings, runtime.database)
    trabajo.state = SyncState(
        state=COMPLETE,
        job_id="abc123",
        started_at="2026-09-02T10:00:00-05:00",
        finished_at="2026-09-02T10:04:00-05:00",
        messages_new=57,
    )
    runtime.sync_job = trabajo

    cuerpo = cliente.get("/api/v1/sync/status").get_json()
    assert cuerpo["state"] == "complete"
    assert cuerpo["finished_at"]
    assert cuerpo["messages_new"] == 57
    assert cuerpo["last_error"] is None


def test_la_sincronizacion_no_lanza_ningun_proceso():
    """Ni subprocess, ni os.system, ni scripts. Servicios internos y punto.

    Se mira el AST y no el texto: el modulo MENCIONA ``subprocess`` en la
    documentacion que explica justamente por que no lo usa, y buscarlo como
    cadena daria un falso positivo.
    """
    import ast

    import app.services.sync_job as modulo

    ruta = Path(modulo.__file__)
    arbol = ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))

    prohibidos = {"subprocess", "os", "shutil"}
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            nombres = [a.name for a in nodo.names]
        elif isinstance(nodo, ast.ImportFrom):
            nombres = [nodo.module or ""]
        else:
            continue
        for nombre in nombres:
            raiz = nombre.split(".")[0]
            assert raiz not in prohibidos, (
                f"sync_job importa {raiz}: lanzar un proceso abriria un segundo "
                "proceso que pelearia por el cerrojo de la sesion"
            )

    # Y usa los servicios internos, que es lo que debe hacer.
    fuente = ruta.read_text(encoding="utf-8")
    for servicio in ("MaintenanceService", "backfill", "media"):
        assert servicio in fuente


def test_sync_progress_se_traduce_a_sse():
    from app.api.routes import EVENT_NAMES

    assert EVENT_NAMES["sync_progress"] == "sync.status"
    assert EVENT_NAMES["backfill_progress"] == "backfill.progress"


# ---------------------------------------------------------------------------
# Vinculacion automatica y ciclo de vida del QR
# ---------------------------------------------------------------------------


def test_sin_sesion_el_estado_es_no_session(settings, tmp_path):
    from app.core.runtime import AppRuntime
    from app.core.session_state import AppState, NEEDS_PAIRING

    import dataclasses

    sin_sesion = dataclasses.replace(settings, session_dir=tmp_path)
    rt = AppRuntime(sin_sesion, owner="pytest", configure_logging=False)
    rt._marcar_estado_inicial()
    assert rt.state.state is AppState.NO_SESSION
    assert rt.state.state in NEEDS_PAIRING
    assert rt.needs_pairing() is True


def test_el_qr_esta_disponible_sin_pulsar_nada(runtime, cliente):
    """El primer QR no depende de POST /session/pair."""
    runtime.pairing.note_qr("2@" + "Q" * 100 + ",B,C,D")

    cuerpo = cliente.get("/api/v1/session/qr").get_json()
    assert cuerpo["available"] is True
    assert cuerpo["generation"] == 1
    assert cliente.get("/api/v1/session/qr/image").status_code == 200


def test_la_ventana_del_qr_no_pasa_de_cinco_minutos(runtime, cliente):
    runtime.pairing.note_qr("2@" + "Q" * 100 + ",B,C,D")
    cuerpo = cliente.get("/api/v1/session/qr").get_json()

    assert 0 < cuerpo["expires_in_seconds"] <= 300
    assert cuerpo["ttl_seconds"] == 300
    assert cuerpo["generated_at"] and cuerpo["expires_at"]


def test_un_qr_caducado_da_410(runtime, cliente):
    """410 y no 404: hubo uno y dejo de valer. Son cosas distintas."""
    from datetime import datetime, timedelta, timezone

    runtime.pairing.note_qr("2@" + "Q" * 100 + ",B,C,D")
    runtime.pairing._expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    respuesta = cliente.get("/api/v1/session/qr/image")
    assert respuesta.status_code == 410
    assert respuesta.get_json()["error"]["code"] == "QR_EXPIRED"

    metadatos = cliente.get("/api/v1/session/qr").get_json()
    assert metadatos["available"] is False
    assert metadatos["expired"] is True
    assert metadatos["expires_in_seconds"] == 0


def test_sin_ningun_qr_da_404_no_410(runtime, cliente):
    respuesta = cliente.get("/api/v1/session/qr/image")
    assert respuesta.status_code == 404
    assert respuesta.get_json()["error"]["code"] == "QR_NOT_AVAILABLE"


def test_la_generacion_sube_con_cada_qr(runtime):
    """Cada payload es un codigo DISTINTO: el anterior deja de servir."""
    assert runtime.pairing.note_qr("2@primero,B,C,D") == 1
    assert runtime.pairing.note_qr("2@segundo,B,C,D") == 2
    assert runtime.pairing.note_qr("2@tercero,B,C,D") == 3
    assert runtime.pairing.generation == 3


def test_un_qr_caducado_se_renueva_solo(settings):
    """El vigilante pide otro sin que nadie refresque el backend."""
    from datetime import datetime, timedelta, timezone

    from app.core.pairing import PairingManager

    renovaciones: list = []
    gestor = PairingManager(
        ttl_seconds=30.0, on_renew=lambda: renovaciones.append(True)
    )
    gestor.note_qr("2@caduco,B,C,D")
    gestor._expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    gestor.start_watchdog()
    try:
        # El vigilante mira cada pocos segundos; se le da margen.
        limite = time.monotonic() + 12
        while not renovaciones and time.monotonic() < limite:
            time.sleep(0.2)
    finally:
        gestor.stop()

    assert renovaciones, "el QR caducado tenia que haberse renovado solo"


def test_conectar_invalida_el_qr_y_para_la_renovacion():
    from app.core.pairing import PairingManager

    renovaciones: list = []
    gestor = PairingManager(
        ttl_seconds=30.0, on_renew=lambda: renovaciones.append(True)
    )
    gestor.note_qr("2@vivo,B,C,D")
    gestor.start_watchdog()
    assert gestor.available is True

    gestor.note_linked()

    assert gestor.available is False
    assert gestor.payload() is None
    # Y un QR que llegue tarde ya no reabre la vinculacion.
    generacion = gestor.generation
    gestor.note_qr("2@tardio,B,C,D")
    assert gestor.generation == generacion


def test_no_se_lanzan_dos_vinculaciones_a_la_vez():
    """El cerrojo interno impide dos QR simultaneos."""
    import threading

    from app.core.pairing import PairingManager

    dentro = threading.Event()
    soltar = threading.Event()
    llamadas: list = []

    def renovar_lento() -> None:
        llamadas.append(True)
        dentro.set()
        soltar.wait(timeout=5)

    gestor = PairingManager(ttl_seconds=30.0, on_renew=renovar_lento)

    primero = threading.Thread(target=gestor.renew, daemon=True)
    primero.start()
    assert dentro.wait(timeout=5)

    # Mientras el primero sigue dentro, el segundo se rechaza.
    assert gestor.renew() is False
    assert gestor.renewing is True

    soltar.set()
    primero.join(timeout=5)
    assert len(llamadas) == 1


def test_el_estado_de_sesion_distingue_las_fases(runtime, cliente):
    from app.core.session_state import AppState

    for estado, esperado in (
        (AppState.NO_SESSION, True),
        (AppState.PAIRING, True),
        (AppState.QR_READY, True),
        (AppState.CONNECTED, False),
    ):
        runtime.state.set(estado, reason="prueba")
        cuerpo = cliente.get("/api/v1/session").get_json()
        assert cuerpo["state"] == estado.value
        assert cuerpo["pairing_required"] is esperado


def test_el_estado_de_sesion_solo_expone_datos_seguros(runtime, cliente):
    runtime.pairing.note_qr("2@" + "S" * 100 + ",B,C,D")
    texto = cliente.get("/api/v1/session").get_data(as_text=True)

    for prohibido in ("2@", "SSSS", "identity", "noise", "adv_secret", "password"):
        assert prohibido not in texto, f"{prohibido} no puede salir en /session"


def test_pair_es_idempotente_con_un_qr_vigente(runtime, cliente):
    """Con QR vivo NO se lanza otra vinculacion: se devuelve el que hay."""
    runtime._whatsapp = True
    runtime.pairing.note_qr("2@" + "P" * 100 + ",B,C,D")
    generacion = runtime.pairing.generation

    respuesta = cliente.post("/api/v1/session/pair")
    assert respuesta.status_code == 200
    cuerpo = respuesta.get_json()
    assert cuerpo["status"] == "pairing_in_progress"
    assert cuerpo["restarted"] is False
    assert runtime.pairing.generation == generacion, "no puede generarse otro QR"


def test_pair_rechaza_si_ya_esta_conectado(runtime, cliente):
    from app.core.session_state import AppState

    runtime._whatsapp = True
    runtime.state.set(AppState.CONNECTED, reason="prueba")

    respuesta = cliente.post("/api/v1/session/pair")
    assert respuesta.status_code == 409
    assert respuesta.get_json()["error"]["code"] == "SESSION_ALREADY_CONNECTED"


def test_el_qr_nunca_se_guarda_en_disco():
    """El payload es una credencial: vive en memoria y nada mas."""
    fuente = Path("app/core/pairing.py").read_text(encoding="utf-8")
    for prohibido in ("write_text", "write_bytes", "open(", "json.dump"):
        assert prohibido not in fuente, (
            f"pairing.py no puede persistir nada ({prohibido})"
        )


def test_reconectar_no_invalida_la_sesion(runtime):
    """Perder la conexion no es lo mismo que perder la vinculacion."""
    from app.core.session_state import AppState

    class EventoFalso:
        def __init__(self, name, payload=None):
            self.name = name
            self.payload = payload
            self.extra = {}

    runtime.state.set(AppState.CONNECTED, reason="prueba")
    runtime._observar_evento(EventoFalso("disconnected"))
    assert runtime.state.state is AppState.DISCONNECTED

    # Y al volver el <success>, se reconecta sin pasar por vincular.
    runtime._observar_evento(EventoFalso("session_valid"))
    assert runtime.state.state is AppState.CONNECTED


def test_un_logout_si_invalida_la_sesion(runtime):
    from app.core.session_state import AppState

    class EventoFalso:
        def __init__(self, name, payload=None):
            self.name = name
            self.payload = payload
            self.extra = {}

    runtime.state.set(AppState.CONNECTED, reason="prueba")
    runtime._observar_evento(EventoFalso("logged_out", 401))
    assert runtime.state.state is AppState.SESSION_INVALID


def test_un_qr_entrante_pone_el_estado_en_qr_ready(runtime):
    from app.core.session_state import AppState

    class EventoFalso:
        def __init__(self, name, payload=None):
            self.name = name
            self.payload = payload
            self.extra = {}

    runtime._observar_evento(EventoFalso("qr", "2@" + "R" * 80 + ",B,C,D"))
    assert runtime.state.state is AppState.QR_READY
    assert runtime.pairing.available is True


def test_el_qr_del_sse_nunca_va_por_detras_del_gestor(runtime):
    """La generacion que sale por SSE es la que el gestor acaba de anotar.

    Antes se traducia el ``qr`` crudo del cliente, y ese evento y el del
    gestor llegan a suscriptores distintos: el generador SSE podia
    serializar la generacion ANTERIOR. Se midio contra el servidor real:
    llegaba "generation 2" cuando el gestor ya iba por la 3.
    """
    from app.api.routes import eventos_para

    class EventoFalso:
        def __init__(self, name, payload=None):
            self.name = name
            self.payload = payload
            self.extra = {}

    # El gestor anota primero y publica despues: cuando el SSE traduce, el
    # estado ya esta al dia.
    runtime.pairing.note_qr("2@primero,B,C,D")
    runtime.pairing.note_qr("2@segundo,B,C,D")

    salida = dict(eventos_para(EventoFalso("pairing_qr_ready", {"generation": 2}), runtime))
    assert salida["session.qr"]["generation"] == runtime.pairing.generation == 2


def test_el_qr_crudo_del_cliente_no_se_traduce_solo():
    """Se traduce el aviso del gestor, no el evento del protocolo."""
    from app.api.routes import EVENT_NAMES

    assert "qr" not in EVENT_NAMES
    assert EVENT_NAMES["pairing_qr_ready"] == "session.qr"


# ---------------------------------------------------------------------------
# Un pairing agotado se reintenta; no es un error
# ---------------------------------------------------------------------------


def test_un_pairing_agotado_no_deja_el_estado_en_error(runtime):
    """Medido en una prueba limpia: quedaba en ERROR 4m40s.

    Los intentos de pywhats se agotaban, el estado pasaba a ERROR y ahi se
    quedaba hasta que vencia el TTL del QR. Un frontend mostraria un fallo de
    un sistema que iba a recuperarse solo.
    """
    from app.core.session_state import AppState

    class EventoFalso:
        def __init__(self, name, payload=None):
            self.name = name
            self.payload = payload
            self.extra = {}

    renovaciones: list = []
    runtime.pairing._on_renew = lambda: renovaciones.append(True)
    runtime._whatsapp = True
    runtime.pairing.note_qr("2@" + "X" * 80 + ",B,C,D")
    runtime.state.set(AppState.QR_READY, reason="prueba")

    runtime._observar_evento(
        EventoFalso("client_error", "pairing timed out waiting for server")
    )

    assert runtime.state.state is not AppState.ERROR, (
        "reintentar no es fallar: ERROR es para lo que no se arregla solo"
    )
    assert renovaciones, "tenia que pedirse un QR nuevo en el acto"
    # El estado concreto lo fija ``restart_pairing`` segun haya o no una
    # sesion guardada, y aqui esta sustituido por un doble. Lo que se
    # comprueba es que se PIDE la renovacion y que no se declara un fallo.


def test_al_morir_el_flujo_el_qr_deja_de_ofrecerse(runtime, cliente):
    """El QR muere con su flujo: seguir sirviendolo seria mentir.

    Antes se seguia devolviendo ``available: true`` durante minutos con un
    codigo que ya no funcionaba; quien lo escaneara no conseguia nada.
    """
    from app.core.session_state import AppState

    class EventoFalso:
        def __init__(self, name, payload=None):
            self.name = name
            self.payload = payload
            self.extra = {}

    runtime.pairing._on_renew = lambda: None
    runtime._whatsapp = True
    runtime.pairing.note_qr("2@" + "Y" * 80 + ",B,C,D")
    runtime.state.set(AppState.QR_READY, reason="prueba")
    assert cliente.get("/api/v1/session/qr").get_json()["available"] is True

    runtime._observar_evento(EventoFalso("client_error", "pairing timed out"))

    assert cliente.get("/api/v1/session/qr").get_json()["available"] is False
    assert cliente.get("/api/v1/session/qr/image").status_code in (404, 410)


def test_un_error_con_sesion_establecida_si_es_error(runtime):
    """ERROR sigue existiendo para lo que no es una vinculacion en curso."""
    from app.core.session_state import AppState

    class EventoFalso:
        def __init__(self, name, payload=None):
            self.name = name
            self.payload = payload
            self.extra = {}

    runtime._whatsapp = True
    runtime.state.set(AppState.CONNECTED, reason="prueba")
    runtime._observar_evento(EventoFalso("client_error", "socket roto"))
    assert runtime.state.state is AppState.ERROR


def test_el_vigilante_pide_qr_cuando_no_hay_ninguno():
    """Si la renovacion inmediata falla, el vigilante lo vuelve a intentar."""
    import time as _time

    from app.core.pairing import PairingManager

    intentos: list = []
    gestor = PairingManager(ttl_seconds=30.0, on_renew=lambda: intentos.append(True))
    gestor.note_qr("2@muerto,B,C,D")
    gestor.invalidate()  # el flujo murio: no queda QR

    gestor.start_watchdog()
    try:
        limite = _time.monotonic() + 12
        while not intentos and _time.monotonic() < limite:
            _time.sleep(0.2)
    finally:
        gestor.stop()

    assert intentos, "sin QR y sin vinculo, el vigilante tiene que pedir uno"


# ---------------------------------------------------------------------------
# EL HISTORIAL QUE ENTRA, SIN F5
# ---------------------------------------------------------------------------
#
# Una excavación produce un aviso por cada bloque de cincuenta mensajes. Antes
# ese aviso decía sólo en qué conversaciones había entrado algo, así que la
# pantalla tenía que pedir la lista entera para enterarse del contador nuevo:
# recuperar tres mil mensajes eran sesenta peticiones contra el mismo endpoint.
#
# Ahora la fila va dentro del aviso cuando son pocas conversaciones, y la
# pantalla actualiza en el sitio.


class _EventoFalso:
    def __init__(self, nombre, payload):
        self.name = nombre
        self.payload = payload
        self.extra = {}


def test_el_historial_que_entra_dice_en_que_chats_y_como_quedaron(
    session, chat_vacio, runtime
):
    from app.api.live_events import translate

    _guardar(session, chat_vacio, "HIST0001", "del historial")
    salida = dict(
        translate(
            _EventoFalso(
                "history_ingested",
                {
                    "summary": "1 mensaje",
                    "chat_jids": [CHAT_JID],
                    "messages": 1,
                    "sync_type": "ON_DEMAND",
                },
            ),
            runtime,
        )
    )

    datos = salida["history.progress"]
    assert datos["chat_jids"] == [CHAT_JID]
    assert datos["messages"] == 1
    # Y la fila entera, para poder actualizar sin pedir la lista.
    assert len(datos["chats"]) == 1
    fila = datos["chats"][0]
    assert fila["jid"] == CHAT_JID
    assert fila["message_count"] >= 1


def test_con_demasiadas_conversaciones_no_se_mandan_todas_las_filas(runtime):
    """Un ``INITIAL_BOOTSTRAP`` toca decenas: el aviso no puede pesar tanto."""
    from app.api.live_events import TOPE_DE_FILAS, translate

    muchos = [f"5730000{i:04d}@s.whatsapp.net" for i in range(TOPE_DE_FILAS + 5)]
    salida = dict(
        translate(
            _EventoFalso("history_ingested", {"chat_jids": muchos, "messages": 900}),
            runtime,
        )
    )

    datos = salida["history.progress"]
    assert "chats" not in datos, "con tantas, la pantalla se refresca una vez"
    assert len(datos["chat_jids"]) == len(muchos)


def test_un_aviso_antiguo_de_texto_suelto_no_rompe_nada(runtime):
    """Compatibilidad: antes viajaba una cadena."""
    from app.api.live_events import translate

    salida = dict(
        translate(_EventoFalso("history_ingested", "backfill completado"), runtime)
    )

    assert salida["history.progress"]["summary"] == "backfill completado"


def test_un_adjunto_descargado_NO_es_un_cambio_de_la_lista(session, chat_vacio, runtime):
    """`media.updated` no puede provocar una recarga de conversaciones.

    Se descargan cientos de adjuntos seguidos; refrescar la lista por cada uno
    dejaría el sidebar parpadeando sin que nada haya cambiado en él.
    """
    from app.api.live_events import translate

    salida = dict(
        translate(
            _EventoFalso("media_ready", {"media_id": 1, "chat_id": chat_vacio}), runtime
        )
    )

    assert set(salida) == {"media.updated"}


def test_los_nombres_nuevos_estan_en_el_mapa_de_eventos():
    """Sin esto el aviso se publica y nunca sale por el stream."""
    from app.api.routes import EVENT_NAMES

    assert EVENT_NAMES["web_chat_created"] == "chat.created"
    assert EVENT_NAMES["web_chat_updated"] == "chat.updated"
    assert EVENT_NAMES["web_inventory_done"] == "chat.inventory"


def test_un_cambio_de_estado_lleva_el_identificador_del_chat(session, chat_vacio):
    """El JID basta para buscar, pero la pantalla indexa por id.

    Y una conversación que llegó por LID puede estar en la lista con el JID
    del teléfono: comparar cadenas fallaría justo en ese caso.
    """
    from app.history.seed_collector import RecentSeedCollector

    recolector = RecentSeedCollector(_DatabaseShim(None, session))
    publicados = []
    recolector.publish = lambda nombre, carga=None, **k: publicados.append((nombre, carga))

    recolector._avisar_estado(CHAT_JID, "pending")

    assert publicados[0][0] == "chat_history_status"
    carga = publicados[0][1]
    assert carga["chat_jid"] == CHAT_JID
    assert carga["chat_id"] == chat_vacio
    assert carga["history_status"] == "pending"
