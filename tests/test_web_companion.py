"""El Web Companion: mide, y NO toca nada.

LA FRONTERA QUE FIJAN ESTAS PRUEBAS
-----------------------------------
Esta fase responde a una pregunta y no hace nada mas. Las pruebas que
importan de verdad son las que cuentan filas antes y despues: son lo que
impide que esto se convierta con el tiempo en un segundo extractor
funcionando en paralelo sin que nadie lo decidiera.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import Chat, ChatHistoryState, HistorySeed, Message
from app.web_companion.probe import WebCompanionProbe
from app.web_companion.supervisor import (
    ESTADOS,
    WebCompanionNoDisponible,
    WebCompanionSupervisor,
)

ANCLA = "3A1F8BDD4678EB6DE395"


class _DatabaseDeSesion:
    def __init__(self, session):
        self._session = session

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


class _SupervisorFalso:
    """Contesta lo que se le diga. No lanza ningun proceso."""

    def __init__(self, respuesta):
        self.respuesta = respuesta
        self.enviados = []

    def enviar(self, comando, *, timeout=None):
        self.enviados.append(comando)
        return self.respuesta


@pytest.fixture
def esperando(session):
    """Una conversacion sin ancla, que es el caso que motiva todo esto."""
    jid = f"5730{uuid.uuid4().hex[:8]}@s.whatsapp.net"
    chat = Chat(jid=jid, chat_type="individual")
    session.add(chat)
    session.flush()
    session.add(
        ChatHistoryState(chat_id=chat.id, chat_jid=jid, history_status="waiting_seed")
    )
    session.flush()
    return chat


def _respuesta_con_candidato(jid, **cambios):
    candidato = {
        "chat_jid": jid,
        "wa_msg_id": ANCLA,
        "timestamp": 1_760_000_000,
        "from_me": False,
        "source": "web_store",
    }
    candidato.update(cambios)
    return {
        "event": "seed_probe_result",
        "summary": {"waiting": 1, "visible_store": 1, "with_messages": 1},
        "chats": [{"chat_jid": jid, "visible": True, "candidate": candidato}],
    }


# ---------------------------------------------------------------------------
# Solo mide: nada cambia
# ---------------------------------------------------------------------------


def test_el_sondeo_NO_escribe_ni_una_fila(session, esperando):
    """La prueba que sostiene toda esta fase.

    Se cuentan las filas de las tres tablas que podrian cambiar, se sondea con
    un candidato PERFECTAMENTE valido, y tienen que seguir igual.
    """
    db = _DatabaseDeSesion(session)

    def contar():
        return {
            "anclas": session.execute(select(func.count()).select_from(HistorySeed)).scalar(),
            "mensajes": session.execute(select(func.count()).select_from(Message)).scalar(),
            "estados": dict(
                session.execute(
                    select(ChatHistoryState.history_status, func.count()).group_by(
                        ChatHistoryState.history_status
                    )
                ).all()
            ),
        }

    antes = contar()
    resultado = WebCompanionProbe(
        db, _SupervisorFalso(_respuesta_con_candidato(esperando.jid))
    ).sondear()
    session.flush()

    assert resultado["seed_usable"] == 1, "el candidato era valido"
    assert contar() == antes, "y aun asi NADA cambio"


def test_el_sondeo_lo_dice_en_su_propia_respuesta(session, esperando):
    """Para que nadie tenga que fiarse de la documentacion."""
    resultado = WebCompanionProbe(
        _DatabaseDeSesion(session), _SupervisorFalso(_respuesta_con_candidato(esperando.jid))
    ).sondear()

    assert resultado["read_only"] is True
    assert resultado["mutations"] == 0
    assert resultado["on_demand_requests"] == 0


def test_un_chat_despertable_se_cuenta_pero_NO_se_despierta(session, esperando):
    """Se dice cuantos se PODRIAN despertar. Ninguno se despierta."""
    resultado = WebCompanionProbe(
        _DatabaseDeSesion(session), _SupervisorFalso(_respuesta_con_candidato(esperando.jid))
    ).sondear()
    session.flush()

    assert resultado["wakeable_chats"] == 1
    estado = session.execute(
        select(ChatHistoryState.history_status).where(
            ChatHistoryState.chat_jid == esperando.jid
        )
    ).scalar_one()
    assert estado == "waiting_seed", "sigue esperando: esta fase solo mide"


def test_el_sondeo_no_puede_pedir_historial():
    """Ni por descuido: no importa nada capaz de hacerlo."""
    import inspect

    from app.web_companion import probe

    fuente = inspect.getsource(probe)
    for prohibido in ("BackfillService", "build_on_demand_message", "run_canary", "_process_chat"):
        assert prohibido not in fuente, f"el sondeo alcanza {prohibido}"


def test_el_sondeo_no_importa_el_colector_de_anclas():
    """Valida con la MISMA funcion, pero no puede anotar nada."""
    import inspect

    from app.web_companion import probe

    fuente = inspect.getsource(probe)
    assert "validar" in fuente, "usa el mismo filtro que el motor"
    assert "RecentSeedCollector" not in fuente, "pero no puede escribir anclas"


# ---------------------------------------------------------------------------
# Python valida; Node solo propone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cambio,esperado",
    [
        ({"wa_msg_id": None}, "sin identificador"),
        ({"wa_msg_id": "inventado"}, "forma inesperada"),
        ({"timestamp": 0}, "sin marca"),
        ({"timestamp": 1_760_000_000_000}, "milisegundos"),
    ],
)
def test_un_candidato_malo_de_Node_lo_rechaza_Python(session, esperando, cambio, esperado):
    """Node no tiene la ultima palabra, y se comprueba caso por caso."""
    resultado = WebCompanionProbe(
        _DatabaseDeSesion(session),
        _SupervisorFalso(_respuesta_con_candidato(esperando.jid, **cambio)),
    ).sondear()

    assert resultado["seed_usable"] == 0
    assert any(esperado in motivo for motivo in resultado["rejections"])


def test_un_candidato_de_un_chat_que_no_existe_se_rechaza(session, esperando):
    """WhatsApp Web ve mas conversaciones que este backend. No se inventan."""
    resultado = WebCompanionProbe(
        _DatabaseDeSesion(session),
        _SupervisorFalso(_respuesta_con_candidato("00000000000@s.whatsapp.net")),
    ).sondear()

    assert resultado["seed_usable"] == 0
    # Prefijado con quien rechazo: Node y Python filtran cosas distintas.
    assert "python:la conversacion no existe en esta cuenta" in resultado["rejections"]


def test_un_candidato_por_LID_encuentra_su_conversacion(session, esperando):
    """Telefono y LID son el mismo contacto: se usa el resolutor de siempre."""
    from app.models import Contact

    lid = f"9998{uuid.uuid4().hex[:8]}@lid"
    session.add(Contact(jid=esperando.jid, lid=lid))
    session.flush()

    resultado = WebCompanionProbe(
        _DatabaseDeSesion(session), _SupervisorFalso(_respuesta_con_candidato(lid))
    ).sondear()

    assert resultado["seed_usable"] == 1


def test_sin_conversaciones_esperando_no_se_pregunta_nada(session):
    """No se molesta al worker para nada."""
    from sqlalchemy import update

    session.execute(update(ChatHistoryState).values(history_status="exhausted"))
    session.flush()

    supervisor = _SupervisorFalso({})
    resultado = WebCompanionProbe(_DatabaseDeSesion(session), supervisor).sondear()

    assert resultado["waiting"] == 0
    assert supervisor.enviados == []


def test_el_origen_de_cada_ancla_se_cuenta(session, esperando):
    """Sin esto no se puede saber que via sirve y cual sobra."""
    resultado = WebCompanionProbe(
        _DatabaseDeSesion(session),
        _SupervisorFalso(_respuesta_con_candidato(esperando.jid, source="web_load_earlier")),
    ).sondear()

    assert resultado["by_source"] == {"web_load_earlier": 1}


# ---------------------------------------------------------------------------
# Inventario
# ---------------------------------------------------------------------------


def test_el_inventario_manda_los_chats_que_ya_conoce_Python(session, esperando):
    """Para que Node pueda decir cuales son NUEVOS, no repetir los de siempre."""
    supervisor = _SupervisorFalso(
        {"event": "inventory_result", "metrics": {"union_chats": 1}, "unknown_to_python": []}
    )
    WebCompanionProbe(_DatabaseDeSesion(session), supervisor).inventario()

    assert supervisor.enviados[0]["cmd"] == "inventory"
    assert esperando.jid in supervisor.enviados[0]["python_chat_jids"]


def test_el_inventario_no_devuelve_identificadores_completos(session, esperando):
    """Un JID completo es un numero de telefono."""
    supervisor = _SupervisorFalso(
        {
            "event": "inventory_result",
            "metrics": {},
            "unknown_to_python": [
                {"chat_jid": "573001112233@c.us", "is_group": False, "msgs_in_memory": 3}
            ],
        }
    )
    resultado = WebCompanionProbe(_DatabaseDeSesion(session), supervisor).inventario()

    assert resultado["unknown_to_python"] == [{"is_group": False, "msgs_in_memory": 3}]
    assert "573001112233" not in json.dumps(resultado)


# ---------------------------------------------------------------------------
# El supervisor del proceso
# ---------------------------------------------------------------------------


@pytest.fixture
def apagado(settings):
    """Con el companion APAGADO, pase lo que pase en el .env del equipo.

    Sin esto, estas pruebas pasaban o fallaban segun como tuviera el usuario
    su configuracion, que es justo lo que una prueba no puede permitirse.
    """
    previo = getattr(settings, "web_companion_enabled", False)
    object.__setattr__(settings, "web_companion_enabled", False)
    yield settings
    object.__setattr__(settings, "web_companion_enabled", previo)


def test_apagado_no_arranca_nada(apagado):
    supervisor = WebCompanionSupervisor(apagado)
    assert supervisor.habilitado is False
    assert supervisor.start() is False
    assert supervisor.snapshot()["state"] == "disabled"


def test_apagado_no_deja_hablar_con_el(apagado):
    with pytest.raises(WebCompanionNoDisponible) as exc:
        WebCompanionSupervisor(apagado).enviar({"cmd": "status"})
    assert exc.value.code == "WEB_COMPANION_DISABLED"


def test_sin_dependencias_dice_QUE_falta(settings, tmp_path):
    """Un ENOENT suelto en el log no le sirve a nadie."""
    supervisor = WebCompanionSupervisor(settings, raiz=tmp_path)
    object.__setattr__(settings, "web_companion_enabled", True)
    try:
        disponible, motivo = supervisor.comprobar_entorno()
        assert disponible is False
        assert "worker.js" in motivo
    finally:
        object.__setattr__(settings, "web_companion_enabled", False)


def test_encendido_pero_sin_node_modules_lo_dice(settings, tmp_path):
    (tmp_path / "worker.js").write_text("", encoding="utf-8")
    supervisor = WebCompanionSupervisor(settings, raiz=tmp_path)
    object.__setattr__(settings, "web_companion_enabled", True)
    try:
        disponible, motivo = supervisor.comprobar_entorno()
        assert disponible is False
        assert "setup_web_companion" in motivo
    finally:
        object.__setattr__(settings, "web_companion_enabled", False)


def test_si_no_esta_en_marcha_no_se_finge_una_respuesta(settings):
    object.__setattr__(settings, "web_companion_enabled", True)
    try:
        with pytest.raises(WebCompanionNoDisponible) as exc:
            WebCompanionSupervisor(settings).enviar({"cmd": "status"})
        assert exc.value.code == "WEB_COMPANION_NOT_RUNNING"
    finally:
        object.__setattr__(settings, "web_companion_enabled", False)


def test_los_estados_son_los_que_espera_el_panel():
    for estado in ("disabled", "starting", "qr_required", "connected", "ready", "error", "stopped"):
        assert estado in ESTADOS


def test_client_ready_no_se_confunde_con_store_ready(settings):
    supervisor = WebCompanionSupervisor(settings)
    supervisor._procesar(
        {
            "event": "state",
            "state": "connected",
            "web_client_ready": True,
            "store_ready": False,
        }
    )
    estado = supervisor.snapshot()
    assert estado["web_client_ready"] is True
    assert estado["store_ready"] is False

    supervisor._procesar(
        {
            "event": "state",
            "state": "ready",
            "web_client_ready": True,
            "store_ready": True,
            "capabilities": {"store_chat_models": True},
            "diagnostics": {"strategy": "window.require(WAWebCollections)"},
        }
    )
    estado = supervisor.snapshot()
    assert estado["store_ready"] is True
    assert estado["capabilities"]["store_chat_models"] is True
    assert estado["diagnostics"]["strategy"] == "window.require(WAWebCollections)"


def test_un_arranque_nuevo_no_reutiliza_ready_anterior(settings, monkeypatch):
    supervisor = WebCompanionSupervisor(settings)
    supervisor._estado.web_client_ready = True
    supervisor._estado.store_ready = True
    supervisor._estado.authenticated = True
    supervisor._estado.capabilities = {"viejo": True}

    class _Process:
        pid = 123
        stdin = stdout = stderr = None

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr("subprocess.Popen", lambda *_a, **_k: _Process())
    monkeypatch.setattr("threading.Thread.start", lambda _self: None)
    monkeypatch.setattr(supervisor, "comprobar_entorno", lambda: (True, "ok"))
    object.__setattr__(supervisor._settings, "web_companion_enabled", True)
    assert supervisor.start() is True
    estado = supervisor.snapshot()
    assert estado["web_client_ready"] is False
    assert estado["store_ready"] is False
    assert estado["authenticated"] is False
    assert estado["capabilities"] is None


def test_timeout_de_startup_mantiene_conectando(settings):
    supervisor = WebCompanionSupervisor(settings)
    supervisor._procesar(
        {
            "event": "state",
            "state": "connected",
            "authenticated": True,
            "web_client_ready": False,
            "store_ready": False,
            "startup_timeout": True,
        }
    )
    estado = supervisor.snapshot()
    assert estado["state"] == "connected"
    assert estado["startup_timeout"] is True
    assert estado["web_client_ready"] is False


def test_status_api_expone_los_estados_independientes(cliente, runtime):
    runtime.web_companion._estado.authenticated = True
    runtime.web_companion._estado.web_client_ready = True
    runtime.web_companion._estado.store_ready = False
    runtime.web_companion._estado.probe_running = False
    cuerpo = cliente.get("/api/v1/web-companion/status").get_json()
    for campo in (
        "process_running", "authenticated", "web_client_ready",
        "store_ready", "probe_running", "capabilities",
    ):
        assert campo in cuerpo
    assert cuerpo["web_client_ready"] is True
    assert cuerpo["store_ready"] is False


def test_la_sesion_del_companion_es_OTRA(settings):
    """Nunca la de pywhats. Mezclarlas corrompe las dos."""
    entorno = WebCompanionSupervisor(settings)._entorno()
    carpeta = entorno["WEB_COMPANION_SESSION_DIR"]

    assert carpeta.endswith("web_companion")
    assert "device.json" not in carpeta
    assert carpeta != str(settings.session_dir)


def test_los_niveles_de_experimento_llegan_al_worker(settings):
    """Los interruptores viven en el .env, no repartidos por el codigo."""
    entorno = WebCompanionSupervisor(settings)._entorno()
    assert entorno["WEB_STORE_LOAD_EARLIER"] == "false"
    assert entorno["WEB_STORE_DISCOVERY_SCROLL"] == "false"


def test_el_backoff_crece_y_tiene_tope():
    """Un Chromium que no arranca no arranca mejor por insistir cien veces."""
    from app.web_companion.supervisor import BACKOFF

    assert list(BACKOFF) == sorted(BACKOFF)
    assert len(BACKOFF) <= 5


# ---------------------------------------------------------------------------
# La API
# ---------------------------------------------------------------------------


def test_el_estado_se_puede_consultar_siempre(cliente):
    """Apagado responde "disabled", no 404: son cosas distintas."""
    respuesta = cliente.get("/api/v1/web-companion/status")
    assert respuesta.status_code == 200
    cuerpo = respuesta.get_json()
    assert cuerpo["state"] in ESTADOS
    assert cuerpo["experimental"] is True, "su QR no es el del emparejamiento principal"


def test_apagado_el_sondeo_contesta_que_esta_apagado(cliente, principal_lista):
    """Con la principal lista: si no, lo que falta es OTRA cosa y lo dice.

    La conexion principal se pregunta antes que el estado del segundo
    dispositivo, asi que sin ella la respuesta seria PRIMARY_NOT_READY. Aqui
    se quiere medir el companion apagado, no la puerta.
    """
    respuesta = cliente.post("/api/v1/web-companion/probe")
    assert respuesta.status_code == 409
    assert respuesta.get_json()["error"]["code"].startswith("WEB_COMPANION")


def test_el_producto_funciona_igual_con_el_apagado(cliente):
    """Es opcional de verdad: nada del camino normal depende de el."""
    for ruta in ("/api/v1/sync/status", "/api/v1/chats"):
        assert cliente.get(ruta).status_code in (200, 401), ruta


# ---------------------------------------------------------------------------
# El canal, de verdad: Python arranca Node y le habla
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    __import__("shutil").which("node") is None, reason="Node no esta instalado"
)
def test_python_habla_con_el_worker_de_verdad(settings, monkeypatch):
    """Se lanza el proceso REAL y se le mandan comandos por sus tuberias.

    Sin Chromium y sin whatsapp-web.js: lo que se comprueba es el canal, no el
    navegador. Si esto se hiciera con un doble, no probaria nada de lo que de
    verdad puede romperse — el troceado de lineas, la correlacion por ``id`` y
    el cierre.
    """
    monkeypatch.setenv("WEB_COMPANION_NO_CLIENT", "true")
    object.__setattr__(settings, "web_companion_enabled", True)

    supervisor = WebCompanionSupervisor(settings)
    # El worker atiende el canal sin dependencias de Node: es justo lo que se
    # esta probando, asi que la comprobacion de node_modules no aplica aqui.
    supervisor.comprobar_entorno = lambda: (True, "prueba del canal")
    try:
        assert supervisor.start() is True

        estado = supervisor.enviar({"cmd": "status"}, timeout=15)
        assert estado["event"] == "status"
        assert estado["id"] == 1, "la respuesta se casa con su comando"

        desconocido = supervisor.enviar({"cmd": "no_existe"}, timeout=15)
        assert desconocido["error"] == "comando_desconocido"
        assert desconocido["id"] == 2, "y sigue casando despues de un error"

        # Sin cliente no se finge un inventario vacio: se dice que no puede.
        assert supervisor.enviar({"cmd": "inventory"}, timeout=15)["error"] == "no_listo"
    finally:
        supervisor.stop()
        object.__setattr__(settings, "web_companion_enabled", False)

    assert supervisor.snapshot()["state"] == "stopped"
    assert supervisor.vivo is False, "el proceso no queda huerfano"


# ---------------------------------------------------------------------------
# El QR: una imagen escaneable, no una cadena
# ---------------------------------------------------------------------------


def test_el_payload_del_QR_no_sale_por_la_API(settings):
    """Es material de vinculacion. La imagen se pinta en el backend.

    Mandar la cadena obligaba al navegador a dibujarla —y el panel acababa
    imprimiendo el texto crudo— y ademas dejaba una credencial a la vista de
    cualquiera que mirase la respuesta.
    """
    supervisor = WebCompanionSupervisor(settings)
    supervisor._procesar({"event": "state", "state": "qr_required", "qr": "2@ABCdef,payload"})

    cuerpo = supervisor.snapshot()
    assert cuerpo["qr_available"] is True
    assert "qr" not in cuerpo, "el payload NO viaja"
    assert "2@ABCdef" not in json.dumps(cuerpo)


def test_un_QR_nuevo_reemplaza_al_anterior(settings):
    """Y sube la generacion, que es lo que fuerza a repintar la imagen."""
    supervisor = WebCompanionSupervisor(settings)

    supervisor._procesar({"event": "state", "state": "qr_required", "qr": "primero"})
    primera = supervisor.snapshot()["qr_generation"]

    supervisor._procesar({"event": "state", "state": "qr_required", "qr": "segundo"})
    segunda = supervisor.snapshot()["qr_generation"]

    assert segunda > primera, "sin esto el navegador reusaria la imagen caducada"
    assert supervisor.qr_payload()[0] == "segundo", "el viejo no se conserva"


def test_el_mismo_QR_repetido_no_sube_la_generacion(settings):
    """Repintar sin motivo hace parpadear el codigo mientras se escanea."""
    supervisor = WebCompanionSupervisor(settings)
    supervisor._procesar({"event": "state", "state": "qr_required", "qr": "igual"})
    antes = supervisor.snapshot()["qr_generation"]
    supervisor._procesar({"event": "state", "state": "qr_required", "qr": "igual"})

    assert supervisor.snapshot()["qr_generation"] == antes


def test_al_vincularse_el_QR_desaparece(settings):
    """Dejarlo ahi haria que el panel siguiera ensenando un codigo muerto."""
    supervisor = WebCompanionSupervisor(settings)
    supervisor._procesar({"event": "state", "state": "qr_required", "qr": "algo"})
    assert supervisor.snapshot()["qr_available"] is True

    supervisor._procesar({"event": "state", "state": "ready"})

    assert supervisor.snapshot()["qr_available"] is False
    assert supervisor.qr_payload()[0] is None


@pytest.fixture
def principal_lista(runtime):
    """Deja la conexion principal LISTA de verdad, no a medias.

    Hace falta desde que el segundo dispositivo espera a la principal: sin
    esto, todas las rutas del companion contestan ``PRIMARY_NOT_READY`` y las
    pruebas medirian la puerta en vez de lo que quieren medir.

    Se cumplen las cuatro condiciones, que es justo lo que exige la puerta:
    estado conectado, identidad propia, Signal Store y cuenta.
    """
    from app.core.session_state import AppState

    runtime.state.set(AppState.CONNECTED)
    runtime.settings.signal_store_file.parent.mkdir(parents=True, exist_ok=True)
    runtime.settings.signal_store_file.write_text("{}", encoding="utf-8")
    runtime.client = SimpleNamespace(
        _client=SimpleNamespace(
            device=SimpleNamespace(jid=SimpleNamespace(user="34600111222"))
        )
    )
    if getattr(runtime, "runtime_owner_account_id", None) is None:
        runtime.runtime_owner_account_id = 1
    return runtime


@pytest.fixture
def companion_encendido(runtime):
    """El companion del runtime que sirve la API, encendido.

    Se enciende sobre SU objeto de configuracion, no sobre otro parecido: la
    ruta pregunta al supervisor del runtime, y encender una copia no lo
    enciende a el.
    """
    supervisor = runtime.web_companion
    ajustes = supervisor._settings
    previo = getattr(ajustes, "web_companion_enabled", False)
    object.__setattr__(ajustes, "web_companion_enabled", True)
    yield supervisor
    object.__setattr__(ajustes, "web_companion_enabled", previo)


def test_sin_QR_la_imagen_contesta_404_y_no_una_imagen_vacia(
    cliente, companion_encendido, principal_lista
):
    """"Todavia no hay" es una respuesta, y hay que poder distinguirla."""
    respuesta = cliente.get("/api/v1/web-companion/qr/image")

    assert respuesta.status_code == 404
    assert respuesta.get_json()["error"]["code"] == "WEB_COMPANION_QR_NOT_AVAILABLE"


def test_con_QR_la_imagen_es_un_PNG_escaneable(
    cliente, companion_encendido, principal_lista
):
    """Un PNG de verdad, con la generacion en la cabecera."""
    companion_encendido._procesar(
        {"event": "state", "state": "qr_required", "qr": "2@abcdefghijklmnop,payload,test"}
    )

    respuesta = cliente.get("/api/v1/web-companion/qr/image?size=320")

    assert respuesta.status_code == 200
    assert respuesta.mimetype == "image/png"
    assert respuesta.data[:4] == bytes([0x89, 0x50, 0x4E, 0x47]), "es un PNG de verdad"
    assert respuesta.headers["X-QR-Generation"] == "1"
    # Un QR cacheado es un codigo muerto que el usuario intenta escanear.
    assert "no-store" in respuesta.headers["Cache-Control"]


def test_la_imagen_se_puede_volver_a_leer_como_QR(settings):
    """No basta con que sea un PNG: tiene que contener el payload EXACTO."""
    from io import BytesIO

    from app.core.qr_render import render_qr

    payload = "2@abcdefghijklmnopqrstuvwxyz,0123456789,test"
    imagen = render_qr(payload, max_pixels=456)
    buffer = BytesIO()
    imagen.save(buffer, format="PNG")

    assert buffer.tell() > 0
    assert imagen.width >= 120 and imagen.height >= 120


def test_el_QR_no_se_registra_entero():
    """Es una credencial: en los registros solo puede salir su longitud."""
    import inspect

    from app.core import qr_render
    from app.web_companion import supervisor as sup

    for modulo in (qr_render, sup):
        fuente = inspect.getsource(modulo)
        assert 'log.info("Payload' not in fuente
        assert "log.info(\n            \"QR renovado" not in fuente


# ---------------------------------------------------------------------------
# Candidatos con la forma REAL de WhatsApp Web
# ---------------------------------------------------------------------------
#
# Medida en la sesion vinculada, no inventada:
#   msg.id.id -> hexadecimal de 20 o 32 caracteres
#   msg.t     -> segundos (10 digitos)
# Y el sondeo real dio 22 de 22 conversaciones con mensajes -> 22 validas.


WAMID_32 = "AC7B0102030405060708090A0B0C24EB"
WAMID_20 = "3A4901020304050607FA"


def _candidato_web(jid, **cambios):
    base = {
        "chat_jid": jid,
        "wa_msg_id": WAMID_32,
        "timestamp": 1_760_000_000,
        "from_me": False,
        "source": "web_store",
        "message_type": "chat",
    }
    base.update(cambios)
    return base


def _respuesta_web(jid, **cambios):
    return {
        "event": "seed_probe_result",
        "summary": {"waiting": 1, "visible_store": 1, "with_messages": 1, "rejections": {}},
        "chats": [
            {"chat_jid": jid, "visible": True, "candidate": _candidato_web(jid, **cambios)}
        ],
    }


@pytest.mark.parametrize("wamid", [WAMID_20, WAMID_32])
def test_los_WAMID_reales_de_Web_pasan_la_validacion(session, esperando, wamid):
    """20 y 32 caracteres hexadecimales: las dos formas medidas en vivo."""
    resultado = WebCompanionProbe(
        _DatabaseDeSesion(session),
        _SupervisorFalso(_respuesta_web(esperando.jid, wa_msg_id=wamid)),
    ).sondear()

    assert resultado["seed_usable"] == 1, f"{wamid} deberia valer"
    assert resultado["rejections"] == {}


def test_un_mensaje_propio_conserva_su_direccion(session, esperando):
    """``oldestMsgFromMe`` viaja en la peticion: no se puede suponer."""
    sondeo = WebCompanionProbe(
        _DatabaseDeSesion(session),
        _SupervisorFalso(_respuesta_web(esperando.jid, from_me=True)),
    )
    assert sondeo.sondear()["seed_usable"] == 1


def test_un_sticker_de_Web_sirve_de_ancla(session, esperando):
    """Lo que descarta un ancla es ser senalizacion, no llevar contenido."""
    resultado = WebCompanionProbe(
        _DatabaseDeSesion(session),
        _SupervisorFalso(_respuesta_web(esperando.jid, message_type="sticker")),
    ).sondear()
    assert resultado["seed_usable"] == 1


def test_un_mensaje_de_protocolo_lo_rechaza_PYTHON(session, esperando):
    """Node manda el tipo; la regla de protocolo es nuestra, no suya."""
    resultado = WebCompanionProbe(
        _DatabaseDeSesion(session),
        _SupervisorFalso(_respuesta_web(esperando.jid, message_type="protocol")),
    ).sondear()

    assert resultado["seed_usable"] == 0
    assert any("protocol" in motivo for motivo in resultado["rejections"])


def test_los_rechazos_dicen_QUIEN_rechazo(session, esperando):
    """Node y Python filtran cosas distintas; mezclarlos esconde cual fallo."""
    respuesta = _respuesta_web(esperando.jid)
    respuesta["summary"]["rejections"] = {"sin_id": 4, "timestamp_unidad_invalida": 1}

    resultado = WebCompanionProbe(
        _DatabaseDeSesion(session), _SupervisorFalso(respuesta)
    ).sondear()

    assert resultado["rejections"]["node:sin_id"] == 4
    assert resultado["rejections"]["node:timestamp_unidad_invalida"] == 1


def test_una_marca_en_milisegundos_de_Web_se_rechaza(session, esperando):
    resultado = WebCompanionProbe(
        _DatabaseDeSesion(session),
        _SupervisorFalso(_respuesta_web(esperando.jid, timestamp=1_760_000_000_000)),
    ).sondear()

    assert resultado["seed_usable"] == 0
    assert any("milisegundos" in motivo for motivo in resultado["rejections"])


def test_veintidos_candidatos_dan_veintidos_anclas_y_CERO_escrituras(session, runtime):
    """El caso real: 25 esperando, 22 con mensajes, 22 validas.

    Es la prueba que representa el resultado medido. Y sigue siendo solo
    lectura: 22 anclas validas y ni una fila escrita.
    """
    from sqlalchemy import func, update

    from app.models import WhatsAppAccount

    inicio = runtime.auth.register(
        email=f"wc-{uuid.uuid4().hex[:10]}@example.com", password="una contrasena larga"
    )
    cuenta = WhatsAppAccount(
        user_id=inicio.user_id,
        session_status="linked",
        session_storage_key=f"users/{inicio.user_id}",
    )
    session.add(cuenta)
    session.flush()

    # Las de la base real quedan fuera: este test decide su escenario.
    session.execute(update(ChatHistoryState).values(history_status="exhausted"))
    session.flush()

    filas = []
    for i in range(22):
        jid = f"5730{uuid.uuid4().hex[:8]}@s.whatsapp.net"
        chat = Chat(jid=jid, chat_type="individual", whatsapp_account_id=cuenta.id)
        session.add(chat)
        session.flush()
        session.add(
            ChatHistoryState(chat_id=chat.id, chat_jid=jid, history_status="waiting_seed")
        )
        filas.append(
            {
                "chat_jid": jid,
                "visible": True,
                "candidate": _candidato_web(jid, wa_msg_id=f"AC7B{i:028X}"),
            }
        )
    session.flush()

    def contar():
        return (
            session.execute(select(func.count()).select_from(HistorySeed)).scalar(),
            dict(
                session.execute(
                    select(ChatHistoryState.history_status, func.count()).group_by(
                        ChatHistoryState.history_status
                    )
                ).all()
            ),
        )

    antes = contar()
    resultado = WebCompanionProbe(
        _DatabaseDeSesion(session),
        _SupervisorFalso(
            {
                "event": "seed_probe_result",
                "summary": {"waiting": 25, "visible_store": 25, "with_messages": 22},
                "chats": filas,
            }
        ),
    ).sondear(cuenta.id)
    session.flush()

    assert resultado["seed_usable"] == 22
    assert resultado["wakeable_chats"] == 22
    assert contar() == antes, "22 anclas validas y NI UNA fila escrita"
    assert resultado["mutations"] == 0
    assert resultado["on_demand_requests"] == 0


def test_el_inventario_clasifica_las_extra_sin_nombrarlas(session, esperando):
    """Importa si son diez grupos o diez difusiones, no cuales."""
    supervisor = _SupervisorFalso(
        {
            "event": "inventory_result",
            "metrics": {"extra_vs_python": 10, "missing_vs_python": 1},
            "extra_por_clase": {"individual": 9, "group": 1},
            "faltan_por_clase": {"individual": 1},
            "unknown_to_python": [],
        }
    )
    resultado = WebCompanionProbe(_DatabaseDeSesion(session), supervisor).inventario()

    assert resultado["extra_por_clase"] == {"individual": 9, "group": 1}
    assert resultado["faltan_por_clase"] == {"individual": 1}
    assert "@" not in json.dumps(resultado), "ni un identificador completo"
