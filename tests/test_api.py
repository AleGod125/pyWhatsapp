"""Pruebas de la API Flask y del cerrojo de sesion.

Cubren cuatro cosas:

* que los endpoints devuelven lo que el frontend necesita;
* que NUNCA sale una ruta del sistema de archivos ni material sensible;
* que la paginacion es por keyset y no por OFFSET;
* que ``app/api`` no depende de Tkinter, y que los dos entrypoints no pueden
  abrir la sesion de WhatsApp a la vez.

Corren contra el PostgreSQL real dentro de una transaccion que se revierte,
igual que el resto de la suite. NO abren la sesion de WhatsApp.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytest.importorskip("flask")

from app.services import repository as repo  # noqa: E402
from app.services.repository import IncomingMessage  # noqa: E402

CHAT_JID = "34600222111@s.whatsapp.net"
TOTAL = 250


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _SessionShim:
    """Sesion del test que la API no puede cerrar."""

    def __init__(self, session) -> None:
        self._session = session

    def __getattr__(self, nombre):
        return getattr(self._session, nombre)

    def close(self) -> None:
        return None


class _DatabaseShim:
    """Base de datos que devuelve SIEMPRE la sesion del test.

    La API abre una sesion por peticion y la cierra al terminar. La del test
    vive dentro de una transaccion que se revierte al final, asi que si la
    cerrara se perderia lo que la prueba acaba de escribir. Este envoltorio
    conserva el contrato y neutraliza el cierre.
    """

    def __init__(self, real, session) -> None:
        self._real = real
        self._session = session

    def session(self):
        return _SessionShim(self._session)

    def health(self):
        return self._real.health()

    def applied_migration(self):
        return self._real.applied_migration()

    def transaction(self):
        """Tambien sobre la sesion del test, NO sobre la base real.

        Delegar esto en ``self._real`` fue un error con consecuencias: los
        servicios que escriben dentro de ``transaction()`` (mantenimiento,
        recuperacion de semillas) hacian COMMIT contra la base de produccion
        del usuario. Se detecto porque una pasada de la suite reclasifico 32
        chats reales. Ahora todo queda dentro de la transaccion que se
        revierte.
        """
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()

    def dispose(self) -> None:
        return None


@pytest.fixture
def runtime(settings, database, session, tmp_path):
    """``AppRuntime`` real, con base del test y sesion en un temporal.

    Se usa el runtime de verdad, no un doble: asi las pruebas ejercitan el
    mismo objeto que construye ``service.py``. Pero la carpeta
    de sesion se aisla: la aplicacion archiva ``device.json`` cuando el
    servidor rechaza un login, y una prueba no puede tocar la sesion viva.
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
def cliente(runtime):
    from app.api import create_app

    aplicacion = create_app(runtime)
    aplicacion.config.update(TESTING=True)
    return aplicacion.test_client()


@pytest.fixture
def chat_con_mensajes(session):
    chat_id = repo.upsert_chat(session, jid=CHAT_JID, chat_type="individual")
    repo.bulk_upsert_messages(
        session,
        {CHAT_JID: chat_id},
        [
            IncomingMessage(
                chat_jid=CHAT_JID,
                timestamp=1_754_000_000 + i * 60,
                source="on_demand",
                whatsapp_message_id=f"API{i:04d}",
                text=f"mensaje {i}",
                message_type="text",
            )
            for i in range(TOTAL)
        ],
    )
    repo.refresh_chat_previews(session, [CHAT_JID])
    session.flush()
    return chat_id


# ---------------------------------------------------------------------------
# Salud y sesion
# ---------------------------------------------------------------------------


def test_health_responde_sin_recorrer_los_mensajes(cliente):
    respuesta = cliente.get("/api/v1/health")
    assert respuesta.status_code == 200
    cuerpo = respuesta.get_json()
    assert cuerpo["status"] == "ok"
    assert cuerpo["database"] is True
    assert cuerpo["api_version"] == "v1"
    # No hay contadores de mensajes: el health check es LIGERO (seccion 36).
    assert "message_count" not in cuerpo


def test_session_no_confunde_device_json_con_estar_conectado(cliente, runtime):
    cuerpo = cliente.get("/api/v1/session").get_json()
    assert cuerpo["connected"] is False
    # El archivo puede existir y la sesion estar revocada: se informa del
    # hecho, no de la conclusion.
    assert "session_file_present" in cuerpo
    assert cuerpo["state"] == runtime.state.state.value


def test_el_estado_refleja_la_maquina_de_estados(cliente, runtime):
    from app.core.session_state import AppState

    runtime.state.set(AppState.CONNECTING, reason="prueba")
    assert cliente.get("/api/v1/session").get_json()["state"] == "CONNECTING"

    runtime.state.set(AppState.CONNECTED, reason="prueba")
    cuerpo = cliente.get("/api/v1/session").get_json()
    assert cuerpo["state"] == "CONNECTED"
    assert cuerpo["connected"] is True


def test_pair_avisa_cuando_la_instancia_es_solo_lectura(cliente):
    respuesta = cliente.post("/api/v1/session/pair")
    assert respuesta.status_code == 409
    assert respuesta.get_json()["error"]["code"] == "WHATSAPP_DISABLED"


def test_el_qr_no_expone_el_payload(cliente, runtime):
    """El payload del QR es una credencial: se sirve como imagen, no como texto."""
    sin_qr = cliente.get("/api/v1/session/qr").get_json()
    assert sin_qr["available"] is False

    runtime.pairing.note_qr("2@" + "A" * 120 + ",B,C,D")
    con_qr = cliente.get("/api/v1/session/qr").get_json()
    assert con_qr["available"] is True
    assert con_qr["image_url"].startswith("/api/v1/session/qr/image")
    # Ni el payload ni un fragmento suyo pueden aparecer en el JSON.
    assert "2@" not in str(con_qr)
    assert "AAAA" not in str(con_qr)


def test_el_qr_se_sirve_como_imagen(cliente, runtime):
    runtime.pairing.note_qr("2@" + "A" * 120 + ",B,C,D")
    respuesta = cliente.get("/api/v1/session/qr/image")
    assert respuesta.status_code == 200
    assert respuesta.mimetype == "image/png"
    assert respuesta.data[:8] == b"\x89PNG\r\n\x1a\n"
    # Un QR caducado no puede servirse desde cache.
    assert "no-store" in respuesta.headers.get("Cache-Control", "")


# ---------------------------------------------------------------------------
# Chats y mensajes
# ---------------------------------------------------------------------------


def test_chats_ordenados_por_ultimo_mensaje(cliente, chat_con_mensajes):
    cuerpo = cliente.get("/api/v1/chats").get_json()
    assert cuerpo["count"] >= 1

    marcas = [
        c["last_message_timestamp"]
        for c in cuerpo["chats"]
        if c["last_message_timestamp"] is not None
    ]
    assert marcas == sorted(marcas, reverse=True), (
        "el sidebar se ordena por last_message_timestamp descendente"
    )


def test_un_chat_trae_nombre_previa_y_avatar(cliente, chat_con_mensajes):
    cuerpo = cliente.get(f"/api/v1/chats/{chat_con_mensajes}").get_json()
    assert cuerpo["id"] == chat_con_mensajes
    assert cuerpo["jid"] == CHAT_JID
    assert cuerpo["display_name"]
    assert cuerpo["preview"]
    assert cuerpo["avatar"]["initials"]
    assert cuerpo["avatar"]["color"].startswith("#")
    assert cuerpo["stats"]["total"] == TOTAL
    assert cuerpo["stats"]["oldest_at"] and cuerpo["stats"]["newest_at"]


def test_chat_inexistente_da_404(cliente):
    assert cliente.get("/api/v1/chats/999999999").status_code == 404


def test_mensajes_devuelve_los_ultimos_y_un_cursor(cliente, chat_con_mensajes):
    cuerpo = cliente.get(
        f"/api/v1/chats/{chat_con_mensajes}/messages?limit=100"
    ).get_json()
    assert cuerpo["count"] == 100
    assert cuerpo["stored_total"] == TOTAL
    assert cuerpo["has_more"] is True
    assert cuerpo["next_cursor"]["before_id"] == cuerpo["messages"][0]["id"]

    marcas = [m["timestamp"] for m in cuerpo["messages"]]
    assert marcas == sorted(marcas), "los mensajes llegan en orden cronologico"


def test_la_paginacion_es_por_keyset(cliente, chat_con_mensajes):
    """Se recorre el chat entero con el cursor, sin OFFSET y sin repetir."""
    vistos: list[int] = []
    cursor = None
    for _ in range(10):
        url = f"/api/v1/chats/{chat_con_mensajes}/messages?limit=100"
        if cursor:
            url += (
                f"&before_timestamp={cursor['before_timestamp']}"
                f"&before_id={cursor['before_id']}"
            )
        cuerpo = cliente.get(url).get_json()
        ids = [m["id"] for m in cuerpo["messages"]]
        assert not (set(ids) & set(vistos)), "una pagina no puede repetir mensajes"
        vistos.extend(ids)
        cursor = cuerpo["next_cursor"]
        if not cuerpo["messages"]:
            break

    assert len(vistos) == TOTAL, f"se recorrieron {len(vistos)} de {TOTAL}"


def test_el_cursor_incompleto_se_rechaza(cliente, chat_con_mensajes):
    """``before_timestamp`` sin ``before_id`` no desempata: es un error."""
    respuesta = cliente.get(
        f"/api/v1/chats/{chat_con_mensajes}/messages?before_timestamp=1754000000"
    )
    assert respuesta.status_code == 400
    assert "before_id" in respuesta.get_json()["error"]


def test_el_limite_esta_acotado(cliente, chat_con_mensajes):
    """Nadie puede pedirse la conversacion entera de un tiron."""
    from app.api.routes import MAX_LIMIT

    cuerpo = cliente.get(
        f"/api/v1/chats/{chat_con_mensajes}/messages?limit=100000"
    ).get_json()
    assert cuerpo["count"] <= MAX_LIMIT


# ---------------------------------------------------------------------------
# Nada de rutas locales ni de datos sensibles
# ---------------------------------------------------------------------------


# Una ruta de Windows es UNA letra de unidad seguida de barra. El
# ``(?<![A-Za-z])`` es imprescindible: sin el, la "s:/" de un "https://" que
# venga dentro del texto de un mensaje se toma por una unidad de disco, y la
# prueba fallaria por contenido legitimo del usuario.
RUTA_LOCAL = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]|\\\\Users|[\\/]AppData[\\/]")


@pytest.mark.parametrize(
    "ruta",
    ["/api/v1/health", "/api/v1/session", "/api/v1/chats", "/api/v1/sync/status"],
)
def test_ninguna_respuesta_lleva_una_ruta_del_disco(cliente, chat_con_mensajes, ruta):
    texto = cliente.get(ruta).get_data(as_text=True)
    assert not RUTA_LOCAL.search(texto), (
        f"{ruta} filtro una ruta del sistema de archivos"
    )


def test_los_mensajes_no_llevan_rutas_ni_protobuf(cliente, chat_con_mensajes):
    texto = cliente.get(
        f"/api/v1/chats/{chat_con_mensajes}/messages?limit=50"
    ).get_data(as_text=True)
    assert not RUTA_LOCAL.search(texto)
    for prohibido in ("raw_proto", "media_key", "local_path", "file_enc_sha256"):
        assert prohibido not in texto, f"{prohibido} no puede salir en la API"


def test_el_adjunto_solo_expone_urls():
    """El serializador de multimedia devuelve URLs, nunca ``local_path``."""
    from app.api.serializers import media_to_json

    class MediaFalsa:
        id = 7
        media_type = "image"
        mime_type = "image/jpeg"
        file_name = "foto.jpg"
        file_size = 1024
        duration_seconds = None
        width = 800
        height = 600
        download_status = "downloaded"
        local_path = "images/7_1.jpg"

    cuerpo = media_to_json(MediaFalsa())
    assert cuerpo["file_url"] == "/api/v1/media/7/file"
    assert cuerpo["thumbnail_url"] == "/api/v1/media/7/thumbnail"
    assert "local_path" not in cuerpo
    assert "images" not in str(cuerpo)


def test_un_adjunto_no_disponible_no_ofrece_url():
    from app.api.serializers import media_to_json

    class MediaFalsa:
        id = 9
        media_type = "video"
        mime_type = "video/mp4"
        file_name = None
        file_size = 2048
        duration_seconds = 30
        width = None
        height = None
        download_status = "unavailable"
        local_path = None

    cuerpo = media_to_json(MediaFalsa())
    assert cuerpo["available"] is False
    assert cuerpo["permanently_unavailable"] is True
    assert cuerpo["file_url"] is None
    assert cuerpo["thumbnail_url"] is None


def test_adjunto_inexistente_da_404(cliente):
    assert cliente.get("/api/v1/media/999999999").status_code == 404
    assert cliente.get("/api/v1/media/999999999/file").status_code == 404


# ---------------------------------------------------------------------------
# Estado de sincronizacion y SSE
# ---------------------------------------------------------------------------


def test_sync_status_no_bloquea_y_trae_las_cifras(cliente):
    cuerpo = cliente.get("/api/v1/sync/status").get_json()
    for clave in ("connection", "history", "media_pending", "backfill", "session"):
        assert clave in cuerpo


def test_el_stream_abre_con_el_estado_actual(cliente):
    """Un cliente que llega a mitad tiene que saber donde esta, no esperar."""
    respuesta = cliente.get("/api/v1/events/stream")
    assert respuesta.status_code == 200
    assert respuesta.mimetype == "text/event-stream"

    primeros = []
    for trozo in respuesta.response:
        primeros.append(trozo.decode("utf-8"))
        if len(primeros) >= 2:
            break
    respuesta.close()

    texto = "".join(primeros)
    assert "event: session.state" in texto
    assert "event: sync.status" in texto


def test_el_bus_reparte_una_copia_a_cada_consumidor():
    """La ventana y cada cliente SSE reciben TODOS los eventos, no se los rifan."""
    from app.events import EventBus

    bus = EventBus()
    ventana = bus.subscribe()
    web_a = bus.subscribe()
    web_b = bus.subscribe()

    bus.publish("message_stored", {"chat_id": 1})

    for consumidor in (ventana, web_a, web_b):
        evento = consumidor.get_nowait()
        assert evento is not None and evento.name == "message_stored"


def test_un_consumidor_atascado_no_frena_al_productor():
    """Se descarta lo viejo de ESE consumidor, nunca se bloquea la recepcion."""
    from app.events.bus import EventBus, Subscription

    bus = EventBus()
    lento = Subscription(bus, maxsize=2)
    bus._subscribers.append(lento)

    for i in range(20):
        bus.publish("ruido", i)

    assert lento.queue.qsize() == 2
    assert lento.dropped >= 18
    # Y lo que queda es lo MAS RECIENTE, que es lo que interesa.
    assert lento.get_nowait().payload == 18


def test_los_nombres_de_evento_se_traducen_al_vocabulario_del_frontend():
    from app.api.routes import EVENT_NAMES

    # El QR se anuncia con ``pairing_qr_ready``, que publica el gestor DESPUES
    # de anotarlo. Traducir el ``qr`` crudo del cliente hacia que el SSE
    # pudiera serializar la generacion anterior: se midio.
    assert EVENT_NAMES["pairing_qr_ready"] == "session.qr"
    assert "qr" not in EVENT_NAMES
    assert EVENT_NAMES["media_downloaded"] == "media.updated"
    assert EVENT_NAMES["backfill_done"] == "backfill.progress"
    assert EVENT_NAMES["status"] == "sync.status"
    # ``message_stored`` y ``media_ready`` NO estan en la tabla: producen
    # VARIOS eventos y los traduce app.api.live_events.
    assert "message_stored" not in EVENT_NAMES
    assert "media_ready" not in EVENT_NAMES


# ---------------------------------------------------------------------------
# Independencia entre adaptadores
# ---------------------------------------------------------------------------


def _modulos_de(paquete: str) -> list[Path]:
    import importlib

    modulo = importlib.import_module(paquete)
    return sorted(Path(modulo.__file__).parent.rglob("*.py"))


def _imports_de(ruta: Path) -> list[str]:
    """Modulos importados, leidos del AST.

    Se mira el arbol y no el texto porque estos archivos MENCIONAN "tkinter"
    en los comentarios que explican justamente esta regla; buscarlo como
    cadena daria un falso positivo.
    """
    arbol = ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))
    nombres: list[str] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            nombres.extend(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom):
            if nodo.module:
                nombres.append(nodo.module)
    return nombres


def test_ningun_modulo_vuelve_a_importar_tkinter():
    """El producto es la API. La ventana de escritorio se retiro entera.

    Se mira el arbol y no el texto: los comentarios que explican esta regla
    mencionan ``tkinter``, y buscarlo como cadena daria un falso positivo.
    """
    for paquete in ("app.api", "app.core", "app.services", "app.compat"):
        for ruta in _modulos_de(paquete):
            for nombre in _imports_de(ruta):
                raiz = nombre.split(".")[0]
                assert raiz != "tkinter", f"{ruta.name} importa tkinter"
                assert not nombre.startswith("app.gui"), (
                    f"{ruta.name} importa {nombre}, que ya no existe"
                )


def test_el_nucleo_tampoco_arrastra_tkinter():
    """``AppRuntime`` tiene que poder vivir en un proceso sin ventana."""
    for ruta in _modulos_de("app.core"):
        for nombre in _imports_de(ruta):
            assert nombre.split(".")[0] != "tkinter", (
                f"{ruta.name} importa tkinter: el nucleo debe servir sin GUI"
            )


def test_los_servicios_no_dependen_de_ninguna_interfaz():
    for ruta in _modulos_de("app.services"):
        for nombre in _imports_de(ruta):
            raiz = nombre.split(".")[0]
            assert raiz not in ("tkinter", "flask"), f"{ruta.name} importa {raiz}"
            assert not nombre.startswith(("app.gui", "app.api")), (
                f"{ruta.name} importa {nombre}: un servicio no conoce adaptadores"
            )




# ---------------------------------------------------------------------------
# Cerrojo de sesion
# ---------------------------------------------------------------------------


def test_dos_procesos_no_pueden_abrir_la_misma_sesion(tmp_path):
    from app.core.lock import SessionLock, SessionLockedError

    principal = SessionLock(tmp_path, owner="otro service.py").acquire()
    try:
        with pytest.raises(SessionLockedError) as fallo:
            SessionLock(tmp_path, owner="service.py").acquire()
        assert fallo.value.holder.owner == "otro service.py"
    finally:
        principal.release()


def test_al_soltar_el_cerrojo_el_otro_proceso_entra(tmp_path):
    from app.core.lock import SessionLock

    primero = SessionLock(tmp_path, owner="otro service.py").acquire()
    primero.release()
    segundo = SessionLock(tmp_path, owner="service.py").acquire()
    assert segundo.held
    segundo.release()
    assert not (tmp_path / "runtime.lock").exists()


def test_un_cerrojo_huerfano_no_bloquea_para_siempre(tmp_path):
    """Si el proceso murio, su cerrojo caduca. Solo se borra ESE archivo."""
    import json

    from app.core.lock import SessionLock

    (tmp_path / "device.json").write_text("{}", encoding="utf-8")
    (tmp_path / "runtime.lock").write_text(
        json.dumps({"pid": 999_999, "owner": "zombi", "acquired_at": "ayer"}),
        encoding="utf-8",
    )

    cerrojo = SessionLock(tmp_path, owner="service.py").acquire()
    assert cerrojo.held
    # Lo que NUNCA se toca:
    assert (tmp_path / "device.json").exists()
    cerrojo.release()


def test_el_cerrojo_no_se_le_quita_a_otro_proceso(tmp_path):
    import json
    import os

    from app.core.lock import SessionLock

    cerrojo = SessionLock(tmp_path, owner="otro service.py").acquire()
    # Alguien reescribe el archivo con OTRO pid.
    cerrojo.path.write_text(
        json.dumps({"pid": os.getpid() + 1, "owner": "otro", "acquired_at": "ahora"}),
        encoding="utf-8",
    )
    cerrojo.release()
    assert cerrojo.path.exists(), "no se puede borrar el cerrojo de otro proceso"
    cerrojo.path.unlink()


def test_el_modo_solo_lectura_no_pide_el_cerrojo(settings, tmp_path):
    """``--viewer`` y ``service.py --local`` conviven con la sesion abierta."""
    from app.core.lock import SessionLock
    from app.core.runtime import AppRuntime

    # El cerrojo se toma en un directorio temporal para no dejar residuos en
    # session/ si la prueba se interrumpe.
    ocupado = SessionLock(tmp_path, owner="otro-proceso").acquire()
    try:
        lector = AppRuntime(settings, owner="lector", configure_logging=False)
        lector.start_local()
        assert lector.database is not None
        assert lector.info().whatsapp_enabled is False
        lector.stop()
    finally:
        ocupado.release()


# ---------------------------------------------------------------------------
# Los dos entrypoints comparten runtime
# ---------------------------------------------------------------------------




def test_service_usa_el_mismo_env_que_main():
    """Mismo ``.env``, sin variables renombradas ni archivo aparte."""
    fuente = Path("service.py").read_text(encoding="utf-8")
    assert "load_settings()" in fuente
    for prohibido in (".flaskenv", "FLASK_ENV", ".env.api"):
        assert prohibido not in fuente, (
            f"service.py no puede tener configuracion propia ({prohibido})"
        )


def test_la_configuracion_de_la_api_tiene_valores_por_defecto(settings):
    assert settings.api_host
    assert settings.api_port > 0
    assert settings.frontend_origin.startswith("http")


# ---------------------------------------------------------------------------
# Revision manual de un chat sin ancla
# ---------------------------------------------------------------------------


def test_la_revision_manual_responde_sin_ancla(cliente, session):
    """El boton "reintentar historial" sobre un chat que no puede excavarse.

    Tiene que responder 200 y decir la verdad: sigue esperando semilla. Un
    error aqui haria que el frontend lo pintara como fallo cuando en realidad
    es el estado correcto de la conversacion.
    """
    from app.models import Chat, ChatHistoryState

    jid = "99977766655@lid"
    chat = Chat(jid=jid, chat_type="individual")
    session.add(chat)
    session.flush()
    session.add(
        ChatHistoryState(chat_id=chat.id, chat_jid=jid, history_status="waiting_seed")
    )
    session.flush()

    respuesta = cliente.post(f"/api/v1/chats/{chat.id}/history/recheck")
    assert respuesta.status_code == 200

    cuerpo = respuesta.get_json()
    assert cuerpo["seed_found"] is False
    assert cuerpo["can_dig"] is False
    assert cuerpo["status"] == "waiting_seed"
    assert cuerpo["oldest_message_timestamp"] is None
    assert cuerpo["history"]["complete"] is False, (
        "un chat sin ancla no puede anunciarse como historial completo"
    )


def test_la_revision_de_un_chat_inexistente_da_404(cliente):
    assert cliente.post("/api/v1/chats/999999/history/recheck").status_code == 404


def test_un_chat_sin_mensajes_no_desaparece_del_listado(cliente, session):
    """Ocultarlo seria mentir por omision: el chat existe y tiene historial."""
    from app.models import Chat

    jid = "99966655544@lid"
    session.add(Chat(jid=jid, chat_type="individual", name="Sin mensajes"))
    session.flush()

    cuerpo = cliente.get("/api/v1/chats?limit=1000").get_json()
    assert jid in {c["jid"] for c in cuerpo["chats"]}


def test_las_metricas_separan_backfill_de_live(cliente):
    """Mezclarlas fue el fallo: un TIMEOUT decia "nuevos=6" por los live."""
    cuerpo = cliente.get("/api/v1/sync/status").get_json()

    diag = cuerpo["diagnostics"]
    for campo in (
        "live_incoming_seen",
        "live_outgoing_seen",
        "live_incoming_persisted",
        "live_outgoing_persisted",
        "no_session",
        "mac_failures",
        "sender_key_missing",
    ):
        assert campo in diag, f"falta el contador {campo}"

    metricas = cuerpo.get("backfill_metrics")
    assert metricas is not None
    for campo in ("requests", "responses", "timeouts", "inserted_from_history"):
        assert campo in metricas, f"falta la metrica {campo}"
    assert "in_flight" in metricas, "hay que poder ver que chat se esta excavando"


# ---------------------------------------------------------------------------
# Reintento de UN adjunto, y reconciliacion por cursor
# ---------------------------------------------------------------------------


def _adjunto(session, chat_id, *, estado="failed", con_datos=True):
    from app.models import MediaFile, Message as MessageRow

    mensaje = MessageRow(
        chat_id=chat_id,
        chat_jid=CHAT_JID,
        whatsapp_message_id=f"RETRYWAMID{estado}{int(con_datos)}",
        message_type="image",
        timestamp=1_788_500_000,
        from_me=False,
        source="live",
    )
    session.add(mensaje)
    session.flush()

    fila = MediaFile(
        message_id=mensaje.id,
        chat_id=chat_id,
        whatsapp_message_id=mensaje.whatsapp_message_id,
        media_type="image",
        mime_type="image/jpeg",
        download_status=estado,
        direct_path="/v/t62/abc" if con_datos else None,
        media_key=b"k" * 32 if con_datos else None,
    )
    session.add(fila)
    session.flush()
    return fila


def test_reintentar_un_adjunto_fallido_lo_encola(cliente, session, chat_con_mensajes):
    fila = _adjunto(session, chat_con_mensajes, estado="failed")

    respuesta = cliente.post(f"/api/v1/media/{fila.id}/retry")
    assert respuesta.status_code == 202
    cuerpo = respuesta.get_json()
    assert cuerpo["status"] == "queued"
    assert cuerpo["media_id"] == fila.id

    session.expire_all()
    assert fila.download_status == "pending"
    assert fila.download_attempts == 0, (
        "el tope de intentos es para los automaticos, no para lo que pide "
        "una persona"
    )


def test_un_adjunto_terminal_si_se_puede_reintentar_a_mano(
    cliente, session, chat_con_mensajes
):
    """Terminal quiere decir "no se reintenta solo", no "prohibido"."""
    fila = _adjunto(session, chat_con_mensajes, estado="unavailable")

    respuesta = cliente.post(f"/api/v1/media/{fila.id}/retry")
    assert respuesta.status_code == 202
    assert respuesta.get_json()["status"] == "queued"


def test_un_adjunto_ya_en_cola_no_se_encola_dos_veces(
    cliente, session, chat_con_mensajes
):
    fila = _adjunto(session, chat_con_mensajes, estado="pending")

    respuesta = cliente.post(f"/api/v1/media/{fila.id}/retry")
    assert respuesta.status_code == 202
    assert respuesta.get_json()["status"] == "already_pending"


def test_sin_clave_ni_ruta_no_se_finge_un_reintento(
    cliente, session, chat_con_mensajes
):
    """Decir "encolado" sobre algo que no se puede bajar seria mentir."""
    fila = _adjunto(session, chat_con_mensajes, estado="failed", con_datos=False)

    respuesta = cliente.post(f"/api/v1/media/{fila.id}/retry")
    assert respuesta.status_code == 409
    assert respuesta.get_json()["error"]["code"] == "MEDIA_METADATA_INSUFFICIENT"

    session.expire_all()
    assert fila.download_status == "failed", "no se toca lo que no se puede arreglar"


def test_reintentar_un_adjunto_inexistente_da_404(cliente):
    assert cliente.post("/api/v1/media/999999/retry").status_code == 404


def test_recuperar_por_mensaje_resuelve_el_adjunto(
    cliente, session, chat_con_mensajes
):
    fila = _adjunto(session, chat_con_mensajes, estado="failed")

    respuesta = cliente.post(f"/api/v1/messages/{fila.message_id}/media/recover")
    assert respuesta.status_code == 202
    assert respuesta.get_json()["media_id"] == fila.id


def test_recuperar_un_mensaje_sin_adjunto_da_404(cliente, session, chat_con_mensajes):
    from app.models import Message as MessageRow

    mensaje = MessageRow(
        chat_id=chat_con_mensajes,
        chat_jid=CHAT_JID,
        whatsapp_message_id="SINADJUNTO001",
        message_type="text",
        text="hola",
        timestamp=1_788_500_001,
        from_me=False,
        source="live",
    )
    session.add(mensaje)
    session.flush()

    assert (
        cliente.post(f"/api/v1/messages/{mensaje.id}/media/recover").status_code == 404
    )


def test_se_puede_preguntar_que_llego_despues(cliente, chat_con_mensajes):
    """Reconciliacion tras perder la conexion SSE, sin recargar el chat.

    PostgreSQL es la fuente de verdad; el flujo de eventos es solo transporte.
    """
    completo = cliente.get(f"/api/v1/chats/{chat_con_mensajes}/messages").get_json()
    assert completo["messages"], "el chat de la prueba tiene mensajes"

    primero = completo["messages"][0]
    despues = cliente.get(
        f"/api/v1/chats/{chat_con_mensajes}/messages"
        f"?after_timestamp={primero['timestamp']}&after_id={primero['id']}"
    ).get_json()

    ids_despues = {m["id"] for m in despues["messages"]}
    assert primero["id"] not in ids_despues, "el cursor es exclusivo"
    assert ids_despues <= {m["id"] for m in completo["messages"]}


def test_la_respuesta_trae_el_cursor_de_reconciliacion(cliente, chat_con_mensajes):
    cuerpo = cliente.get(f"/api/v1/chats/{chat_con_mensajes}/messages").get_json()
    cursor = cuerpo.get("sync_cursor")
    assert cursor is not None
    assert "after_timestamp" in cursor and "after_id" in cursor


def test_las_dos_direcciones_no_se_mezclan(cliente, chat_con_mensajes):
    respuesta = cliente.get(
        f"/api/v1/chats/{chat_con_mensajes}/messages"
        "?before_timestamp=1&before_id=1&after_timestamp=1&after_id=1"
    )
    assert respuesta.status_code == 400


def test_after_timestamp_sin_after_id_se_rechaza(cliente, chat_con_mensajes):
    """La paginacion es por clave compuesta: el timestamp solo no desempata."""
    respuesta = cliente.get(
        f"/api/v1/chats/{chat_con_mensajes}/messages?after_timestamp=1"
    )
    assert respuesta.status_code == 400
