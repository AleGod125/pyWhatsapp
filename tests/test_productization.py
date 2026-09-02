"""Pruebas de la fase de productizacion (seccion 40).

Cubren cuatro bloques:

* scroll automatico y conservacion de la posicion;
* clasificacion de eventos de sistema y de multimedia en la GUI;
* arranque sin la espera de 180 segundos;
* mantenimiento seguro: idempotente y NO destructivo.

Los de base de datos corren contra el PostgreSQL real dentro de una
transaccion que se revierte, igual que el resto de la suite.
"""

from __future__ import annotations

import queue
from dataclasses import dataclass
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")

from app import repository as repo  # noqa: E402
from app.repository import ChatSummary, IncomingMessage  # noqa: E402

CHAT_JID = "34600777888@s.whatsapp.net"
TOTAL = 452


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(viewer_app):
    """El visor mapeado y de tamano conocido (ver ``viewer_app`` en conftest).

    Estas pruebas miden posiciones de scroll, y eso solo significa algo sobre
    una ventana que de verdad se muestra.

    Al terminar se devuelve el visor a su fabrica de sesiones original: varias
    pruebas le enchufan la sesion del test, que al revertirse dejaria al resto
    de la suite consultando sobre una conexion cerrada.
    """
    fabrica = viewer_app.viewer._session_factory
    abierto = viewer_app.viewer._current
    yield viewer_app
    viewer_app.viewer._session_factory = fabrica
    viewer_app.viewer._current = abierto


@pytest.fixture
def chat_largo(session):
    chat_id = repo.upsert_chat(session, jid=CHAT_JID, chat_type="individual")
    repo.bulk_upsert_messages(
        session,
        {CHAT_JID: chat_id},
        [
            IncomingMessage(
                chat_jid=CHAT_JID,
                timestamp=1_754_000_000 + i * 60,
                source="on_demand",
                whatsapp_message_id=f"PRD{i:04d}",
                text=f"mensaje {i}",
                message_type="text",
            )
            for i in range(TOTAL)
        ],
    )
    session.flush()
    return chat_id


class _SesionCompartida:
    """Envuelve la sesion del test para que la GUI no la cierre.

    El visor abre una sesion por consulta y la cierra al terminar. La del test
    vive dentro de una transaccion que se revierte al final, asi que si la
    cerrara se perderia todo lo que la prueba acaba de escribir.
    """

    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    def __getattr__(self, nombre):
        return getattr(self._session, nombre)

    def close(self):  # noqa: D102 - a proposito no hace nada
        return None


def _abrir(app, session, chat_id, total=TOTAL):
    from app.chat_view import PAGE_SIZE

    panel = app.viewer.conversation
    panel.bind_loader(
        lambda cid, ts, mid, limit: repo.get_messages_before(session, cid, ts, mid, limit)
    )
    panel.bind_media_loader(lambda ids: repo.media_for_messages(session, ids))
    resumen = ChatSummary(
        id=chat_id, jid=CHAT_JID, display_name="Prueba",
        chat_type="individual", last_message=None,
        last_message_timestamp=None, message_count=total,
    )
    panel.open_chat(
        resumen,
        repo.get_recent_messages(session, chat_id, limit=PAGE_SIZE),
        {}, {}, repo.get_chat_stats(session, chat_id),
    )
    # El visor tiene que saber que chat esta abierto y con que sesion mirar,
    # o los refrescos incrementales no encuentran nada.
    app.viewer._current = resumen
    app.viewer._session_factory = _SesionCompartida(session)
    app.root.update()
    return panel


# ---------------------------------------------------------------------------
# Scroll automatico (secciones 1 a 4)
# ---------------------------------------------------------------------------


def test_scroll_automatico_llega_a_452_sin_pulsar_el_boton(
    app, session, chat_largo, monkeypatch
):
    """200 -> 400 -> 452 con eventos de scroll, sin tocar el boton.

    Cada iteracion simula que el usuario ha seguido subiendo: la vista cambia
    y ademas se deja pasar el cooldown. Es lo que ocurre de verdad al arrastrar
    la barra hacia arriba.
    """
    panel = _abrir(app, session, chat_largo)
    # Puede que el scroll automatico ya haya traido una pagina durante el
    # ``update()``: eso es exactamente lo que se quiere. Lo que se comprueba
    # es que se llega al final sin tocar el boton.
    inicial = panel._rendered
    assert 0 < inicial < TOTAL

    # monkeypatch, no asignacion directa: el panel es de ambito de modulo y
    # dejarle un ``yview`` falso pegado envenena las pruebas siguientes.
    vistas = [(0.0, 0.4), (0.01, 0.41), (0.02, 0.42), (0.03, 0.43)]
    for vista in vistas:
        monkeypatch.setattr(panel._scroll.canvas, "yview", lambda v=vista: v)
        panel._last_prefetch_at = 0.0
        panel._maybe_prefetch()
        app.root.update()
        if panel._rendered >= TOTAL:
            break

    assert panel._rendered == TOTAL, (
        f"el scroll automatico se quedo en {panel._rendered} de {TOTAL}"
    )
    assert panel._exhausted is True


def test_al_llegar_al_final_no_se_vuelve_a_consultar(
    app, session, chat_largo, monkeypatch
):
    """Con todo cargado, ni un SELECT mas (seccion 4)."""
    panel = _abrir(app, session, chat_largo)

    consultas = {"n": 0}
    original = panel._loader

    def contando(*args):
        consultas["n"] += 1
        return original(*args)

    panel.bind_loader(contando)

    while panel._rendered < TOTAL:
        panel.load_previous_page()
    consultas_al_terminar = consultas["n"]

    for i in range(5):
        monkeypatch.setattr(
            panel._scroll.canvas, "yview", lambda v=(i * 0.01, 0.4 + i * 0.01): v
        )
        panel._last_prefetch_at = 0.0
        panel._maybe_prefetch()

    assert consultas["n"] == consultas_al_terminar, (
        "con el historial local agotado no puede lanzarse ninguna consulta mas"
    )


def test_el_prepend_conserva_la_posicion_visual(
    app, session, chat_largo, monkeypatch
):
    """Al insertar arriba, el mensaje que se miraba sigue donde estaba.

    Se compara el desplazamiento EN PIXELES, no la fraccion: la fraccion
    cambia por definicion cuando el contenido crece.

    El scroll automatico se desactiva aqui a proposito: si no, los eventos de
    ``update_idletasks`` cargan paginas por su cuenta y la medicion deja de
    corresponder a una sola insercion. Que haga eso es lo correcto; solo
    estorba a esta medida concreta.
    """
    import app.chat_view as chat_view

    monkeypatch.setattr(chat_view, "AUTO_PREFETCH", False)
    panel = _abrir(app, session, chat_largo)
    canvas = panel._scroll.canvas

    canvas.yview_moveto(0.10)
    app.root.update()

    # Se sigue a un widget concreto: el primer mensaje que ya estaba pintado.
    # Su posicion EN PANTALLA es lo que el usuario percibe, y es lo que debe
    # mantenerse. Medirlo asi tambien evita depender de la altura solicitada
    # del contenedor, que no siempre esta al dia justo despues de insertar.
    ancla = panel._scroll.body.winfo_children()[0]
    alto_antes = panel._scroll.content_height()
    pantalla_antes = ancla.winfo_y() - canvas.yview()[0] * alto_antes

    traidos = panel.load_previous_page()
    app.root.update()
    assert traidos == 200

    alto_despues = panel._scroll.content_height()
    pantalla_despues = ancla.winfo_y() - canvas.yview()[0] * alto_despues

    assert abs(pantalla_despues - pantalla_antes) <= 30, (
        "el viewport salto: el mensaje que se estaba leyendo se ha movido "
        f"{pantalla_despues - pantalla_antes:.0f} px"
    )


# ---------------------------------------------------------------------------
# Eventos de sistema (secciones 5, 6 y 31)
# ---------------------------------------------------------------------------


def test_llamada_perdida_se_clasifica_desde_el_stub():
    from app.system_message import CALL_KINDS, SystemMessageClassifier

    clasificador = SystemMessageClassifier()

    voz = clasificador.classify_stub(40)
    assert voz.kind == "call_missed_voice"
    assert voz.label == "Llamada perdida"
    assert voz.icon == "📞"
    assert voz.kind in CALL_KINDS

    video = clasificador.classify_stub(41)
    assert video.kind == "call_missed_video"
    assert video.label == "Videollamada perdida"
    assert video.icon == "📹"


def test_stub_desconocido_no_se_inventa():
    """Lo que no se sabe se dice que no se sabe, conservando el numero."""
    from app.system_message import SystemMessageClassifier

    evento = SystemMessageClassifier().classify_stub(9999)
    assert evento.known is False
    assert evento.label == "Evento del sistema"
    assert evento.stub_type == 9999


def test_los_stubs_verificados_salen_de_datos_reales():
    """Los marcados VERIFICADO se comprobaron contra este backup."""
    from app.system_message import STUB_TYPES

    for numero in (1, 24, 27, 28, 39, 72, 75):
        assert STUB_TYPES[numero].verified is True, (
            f"el stub {numero} aparece en la base y debe estar verificado"
        )


def test_el_protocolo_interno_sigue_oculto():
    """Un PeerDataOperation nuestro NO es un mensaje del chat (seccion 6)."""
    from app.message_classifier import (
        MessageClass,
        classify_message_bytes,
        is_internal,
        is_visible,
    )

    # protocolMessage (campo 12) y nada mas: es el eco de una peticion ON_DEMAND.
    solo_protocolo = bytes([(12 << 3) | 2, 4, 0x10, 0x02, 0x18, 0x00])
    clase = classify_message_bytes(solo_protocolo)
    assert clase is MessageClass.PROTOCOL_INTERNAL
    assert is_internal(clase) and not is_visible(clase)


def test_un_evento_de_sistema_visible_no_se_descarta():
    from app.message_classifier import MessageClass, classify_parsed, is_visible

    @dataclass
    class Falso:
        message_type: str = "system"

    clase = classify_parsed(Falso())
    assert clase is MessageClass.VISIBLE_SYSTEM_EVENT
    assert is_visible(clase)


def test_el_evento_de_llamada_se_lee_del_call_log():
    """``callLogMesssage`` con outcome MISSED e isVideo."""
    from app.system_message import classify_call_log

    # isVideo=1 (campo 1), callOutcome=1 MISSED (campo 2), durationSecs=18.
    payload = bytes([0x08, 0x01, 0x10, 0x01, 0x18, 18])
    evento = classify_call_log(payload)
    assert evento.kind == "call_missed_video"
    assert evento.label == "Videollamada perdida"
    assert evento.duration_seconds == 18
    assert "0:18" in evento.display


# ---------------------------------------------------------------------------
# Previas por tipo (secciones 32 y 33)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tipo,esperado",
    [
        ("image", "📷 Imagen"),
        ("video", "🎥 Video"),
        ("audio", "🔊 Audio"),
        ("voice_note", "🎤 Nota de voz"),
        ("sticker", "Sticker"),
        ("document", "📄 Documento"),
        ("location", "📍 Ubicacion"),
        ("poll", "📊 Encuesta"),
        ("call", "📞 Llamada"),
    ],
)
def test_previa_por_tipo_de_media(tipo, esperado):
    from app.previews import preview_for

    assert preview_for(tipo, None) == esperado


def test_la_previa_nunca_dice_unknown_para_un_tipo_conocido():
    from app.previews import preview_for

    for tipo in ("image", "video", "audio", "sticker", "document", "poll"):
        previa = preview_for(tipo, None)
        assert "unknown" not in previa.lower()
        assert previa != "[unknown]"


def test_el_texto_manda_sobre_la_etiqueta_de_tipo():
    from app.previews import preview_for

    assert preview_for("image", "mira esta foto") == "mira esta foto"


def test_la_previa_de_un_evento_de_sistema_dice_que_paso():
    from app.previews import preview_for

    previa = preview_for("system", None, metadata={"stub_type": 40})
    assert "Llamada perdida" in previa


# ---------------------------------------------------------------------------
# Multimedia en la GUI (secciones 8 a 12)
# ---------------------------------------------------------------------------


@dataclass
class MediaFalsa:
    media_type: str
    download_status: str
    local_path: str | None = None
    file_name: str | None = None
    file_size: int | None = None
    duration_seconds: int | None = None
    width: int | None = None
    height: int | None = None


def _textos(widget) -> str:
    """Todo el texto de un arbol de widgets, concatenado."""
    partes = []
    try:
        texto = widget.cget("text")
        if texto:
            partes.append(str(texto))
    except tk.TclError:
        pass
    for hijo in widget.winfo_children():
        partes.append(_textos(hijo))
    return " ".join(partes)


def _burbuja(app):
    panel = app.viewer.conversation
    contenedor = tk.Frame(panel._scroll.body, bg="#ffffff")
    contenedor.pack()
    return panel, contenedor


def test_imagen_no_disponible_muestra_aviso_claro(app):
    """El mensaje NO desaparece porque el archivo ya no este (seccion 12)."""
    panel, bubble = _burbuja(app)
    panel._render_media(bubble, MediaFalsa("image", "unavailable"))
    app.root.update_idletasks()

    texto = _textos(bubble)
    assert "no disponible" in texto.lower()
    assert "🖼" in texto
    bubble.destroy()


def test_video_no_disponible_muestra_aviso_claro(app):
    panel, bubble = _burbuja(app)
    panel._render_media(bubble, MediaFalsa("video", "expired"))
    app.root.update_idletasks()

    assert "no disponible" in _textos(bubble).lower()
    bubble.destroy()


def test_video_descargado_crea_tarjeta_con_accion(app, tmp_path):
    panel, bubble = _burbuja(app)
    panel.set_media_root(tmp_path)
    (tmp_path / "v.mp4").write_bytes(b"0" * 1024)

    panel._render_media(
        bubble,
        MediaFalsa("video", "downloaded", "v.mp4", file_size=1024, duration_seconds=75),
    )
    app.root.update_idletasks()

    texto = _textos(bubble)
    assert "Video" in texto
    assert "1:15" in texto, "la duracion debe verse"
    assert "Reproducir" in texto
    bubble.destroy()


def test_audio_descargado_crea_tarjeta_con_duracion(app, tmp_path):
    panel, bubble = _burbuja(app)
    panel.set_media_root(tmp_path)
    (tmp_path / "a.ogg").write_bytes(b"0" * 512)

    panel._render_media(
        bubble, MediaFalsa("voice_note", "downloaded", "a.ogg", duration_seconds=18)
    )
    app.root.update_idletasks()

    texto = _textos(bubble)
    assert "Nota de voz" in texto
    assert "0:18" in texto
    assert "Reproducir" in texto
    bubble.destroy()


def test_documento_descargado_muestra_nombre_y_tamano(app, tmp_path):
    panel, bubble = _burbuja(app)
    panel.set_media_root(tmp_path)
    (tmp_path / "d.pdf").write_bytes(b"0" * (2 * 1024 * 1024))

    panel._render_media(
        bubble,
        MediaFalsa(
            "document", "downloaded", "d.pdf", file_name="informe.pdf",
            file_size=2 * 1024 * 1024,
        ),
    )
    app.root.update_idletasks()

    texto = _textos(bubble)
    assert "informe.pdf" in texto
    assert "2.0 MB" in texto
    bubble.destroy()


def test_imagen_descargada_genera_miniatura_cacheada(app, tmp_path):
    """Se crea el archivo derivado y NO se vuelve a generar (seccion 37)."""
    Image = pytest.importorskip("PIL.Image")
    from app.thumbnails import ensure_thumbnail, thumbnail_path

    original = tmp_path / "foto.jpg"
    Image.new("RGB", (1200, 900), "#336699").save(original)

    destino = ensure_thumbnail(tmp_path, original, (320, 320))
    assert destino is not None and destino.exists()
    assert destino == thumbnail_path(tmp_path, original, (320, 320))

    with Image.open(destino) as miniatura:
        assert miniatura.width <= 320 and miniatura.height <= 320

    marca = destino.stat().st_mtime_ns
    assert ensure_thumbnail(tmp_path, original, (320, 320)) == destino
    assert destino.stat().st_mtime_ns == marca, "no debe regenerarse"


def test_la_imagen_se_situa_respecto_al_contenido_no_a_su_burbuja(app, tmp_path):
    """El hueco de una imagen cuelga de su burbuja, no del hilo.

    Medir su posicion con ``winfo_y()`` devolvia siempre unos pocos pixeles,
    asi que TODA imagen parecia estar lejisimos del viewport y el pintor
    perezoso no materializaba ninguna. La posicion tiene que calcularse
    respecto al contenido desplazable.
    """
    Image = pytest.importorskip("PIL.Image")

    panel = app.viewer.conversation
    panel.set_media_root(tmp_path)
    (tmp_path / "img.jpg").parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (600, 400), "#224466").save(tmp_path / "img.jpg")

    panel._scroll.clear()
    panel._pending_thumbs.clear()
    panel._photos.clear()

    holder = tk.Frame(panel._scroll.body, bg="#ffffff")
    holder.pack(fill="x", pady=400)  # empuja la burbuja hacia abajo
    bubble = tk.Frame(holder, bg="#ffffff")
    bubble.pack()
    panel._render_media(
        bubble,
        MediaFalsa("image", "downloaded", "img.jpg", width=600, height=400),
    )
    app.root.update()

    assert panel._pending_thumbs, "deberia haber quedado una miniatura pendiente"
    contenedor = panel._pending_thumbs[0]["container"]
    dentro_de_la_burbuja = contenedor.winfo_y()
    dentro_del_hilo = panel._offset_in_body(contenedor)

    assert dentro_del_hilo is not None
    assert dentro_del_hilo > dentro_de_la_burbuja, (
        "la posicion en el hilo tiene que reflejar el desplazamiento real, "
        f"no los {dentro_de_la_burbuja} px que mide dentro de su burbuja"
    )

    holder.destroy()
    panel._pending_thumbs.clear()


def test_la_miniatura_se_regenera_si_cambia_el_original(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    from app.thumbnails import cache_key

    original = tmp_path / "foto.jpg"
    Image.new("RGB", (400, 400), "#111111").save(original)
    primera = cache_key(original, (320, 320))

    Image.new("RGB", (800, 800), "#eeeeee").save(original)
    assert cache_key(original, (320, 320)) != primera


def test_cambiar_de_chat_suelta_las_imagenes(app, session, chat_largo):
    """Sin esto las ``PhotoImage`` se acumulan y la memoria solo sube."""
    panel = _abrir(app, session, chat_largo)
    panel._photos["ficticia"] = object()
    panel._pending_thumbs.append({"widget": None})

    _abrir(app, session, chat_largo)
    assert panel._photos == {}
    assert panel._pending_thumbs == []


# ---------------------------------------------------------------------------
# Arranque: sin la espera de 180 segundos (seccion 18)
# ---------------------------------------------------------------------------



def test_historial_confirmado_no_espera_bootstrap():
    import asyncio
    import time

    from app.history_gate import InitialHistoryGate

    gate = InitialHistoryGate(settle_seconds=1.0, max_wait=180.0, already_confirmed=True)
    comenzo = time.monotonic()
    assert asyncio.run(gate.wait()) is True
    assert time.monotonic() - comenzo < 1.0, "no debe esperar nada"


def test_pairing_nuevo_si_vuelve_a_esperar():
    """Sin confirmacion previa, la barrera espera de verdad."""
    import asyncio
    import time

    from app.history_gate import InitialHistoryGate

    gate = InitialHistoryGate(settle_seconds=0.2, max_wait=2.0)
    comenzo = time.monotonic()
    assert asyncio.run(gate.wait()) is False
    assert time.monotonic() - comenzo >= 1.5, "debe agotar su plazo esperando"


def test_la_confirmacion_va_por_huella_de_sesion(database):
    """Una huella distinta (pairing nuevo) NO hereda la confirmacion."""
    from app.history_gate import confirm_initial_history, initial_history_confirmed

    original = initial_history_confirmed(database, "huella-de-prueba-a")
    try:
        confirm_initial_history(database, "huella-de-prueba-a", chunks=3)
        assert initial_history_confirmed(database, "huella-de-prueba-a") is True
        assert initial_history_confirmed(database, "huella-de-prueba-b") is False
        assert initial_history_confirmed(database, None) is False
    finally:
        if not original:
            from app import repository as repo_mod
            from app.history_gate import INITIAL_HISTORY_KEY

            with database.transaction() as sesion:
                repo_mod.set_app_state(sesion, INITIAL_HISTORY_KEY, None)


# ---------------------------------------------------------------------------
# Mantenimiento seguro (secciones 16 y 17)
# ---------------------------------------------------------------------------


def test_el_mantenimiento_es_idempotente(database, settings):
    from app.maintenance_service import MaintenanceService

    servicio = MaintenanceService(database, settings)
    servicio.run_all()
    segunda = servicio.run_all()

    assert segunda.errors == []
    assert segunda.changed == 0, (
        f"la segunda pasada cambio cosas, no es idempotente: {segunda}"
    )


def test_el_mantenimiento_no_es_destructivo():
    """No hay ni un DELETE en el modulo. Es la garantia de la seccion 16."""
    fuente = Path("app/maintenance_service.py").read_text(encoding="utf-8")
    cuerpo = "\n".join(
        linea for linea in fuente.splitlines()
        if not linea.strip().startswith("#")
    )
    for prohibido in ("delete(", "DELETE FROM", "session.delete", "drop("):
        assert prohibido not in cuerpo, (
            f"el mantenimiento automatico no puede contener {prohibido!r}"
        )


def test_repair_db_no_se_ejecuta_automaticamente():
    """Ninguna ruta automatica IMPORTA ni llama la herramienta destructiva.

    Se busca la invocacion, no la palabra: estos modulos SI la mencionan en su
    documentacion, para dejar escrito que existe y que se ejecuta a mano. Esa
    mencion es deseable; lo que no puede haber es una llamada.
    """
    invocaciones = (
        "import repair_db",
        "from repair_db",
        "repair_db.main",
        "repair_db.apply",
    )
    for archivo in ("main.py", "app/orchestrator.py", "app/maintenance_service.py"):
        fuente = Path(archivo).read_text(encoding="utf-8")
        for invocacion in invocaciones:
            assert invocacion not in fuente, (
                f"{archivo} no puede invocar repair_db: es destructivo y va a mano"
            )
        # Y tampoco por subproceso, que seria la puerta de atras.
        assert "repair_db.py" not in fuente.replace("``repair_db.py``", ""), (
            f"{archivo} no puede lanzar repair_db.py como subproceso"
        )


def test_el_mantenimiento_conserva_los_mensajes(database, settings, session):
    """Contar antes y despues: la reconciliacion no puede perder filas."""
    from sqlalchemy import func, select

    from app.maintenance_service import MaintenanceService
    from app.models import MediaFile, Message

    def cuenta(modelo):
        with database.transaction() as sesion:
            return sesion.execute(select(func.count()).select_from(modelo)).scalar_one()

    mensajes_antes, media_antes = cuenta(Message), cuenta(MediaFile)
    MaintenanceService(database, settings).run_all()
    assert cuenta(Message) == mensajes_antes
    assert cuenta(MediaFile) >= media_antes, "solo puede anadir filas, nunca quitar"


# ---------------------------------------------------------------------------
# Escalabilidad (secciones 21 y 22)
# ---------------------------------------------------------------------------


def test_la_gui_nunca_hace_un_select_global_de_mensajes():
    """Ninguna consulta de la GUI puede recorrer la tabla entera."""
    import inspect

    from app import repository

    for nombre in (
        "get_recent_messages",
        "get_messages_before",
        "media_for_messages",
        "chat_summary",
    ):
        fuente = inspect.getsource(getattr(repository, nombre))
        assert ".where(" in fuente, f"{nombre} debe filtrar"
    # Las dos de paginacion, ademas, acotan explicitamente.
    for nombre in ("get_recent_messages", "get_messages_before"):
        assert ".limit(" in inspect.getsource(getattr(repository, nombre))


def test_la_paginacion_usa_keyset_y_no_offset():
    import inspect

    from app.repository import get_messages_before

    fuente = inspect.getsource(get_messages_before)
    assert ".offset(" not in fuente, "OFFSET degrada con cientos de miles de filas"
    assert "Message.timestamp <" in fuente and "Message.id <" in fuente


def test_existen_los_indices_que_sostienen_las_consultas(database):
    """Los indices de la seccion 22, comprobados en el motor real."""
    from sqlalchemy import text

    esperados = {
        "messages": ("ix_messages_chat_id_timestamp", "uq_messages_chat_wamid"),
        "media_files": ("ix_media_files_chat_id",),
        "chats": ("ix_chats_last_message_timestamp",),
    }
    with database.engine.connect() as conexion:
        for tabla, indices in esperados.items():
            presentes = {
                fila[0]
                for fila in conexion.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = :t"),
                    {"t": tabla},
                )
            }
            for indice in indices:
                assert indice in presentes, f"falta {indice} en {tabla}"


def test_media_files_tiene_indice_por_mensaje(database):
    """``media_for_messages`` busca por ``message_id``: debe estar indexado."""
    from sqlalchemy import text

    with database.engine.connect() as conexion:
        definiciones = [
            fila[0]
            for fila in conexion.execute(
                text("SELECT indexdef FROM pg_indexes WHERE tablename = 'media_files'")
            )
        ]
    assert any("message_id" in definicion for definicion in definiciones), (
        "sin indice por message_id la carga de adjuntos de una pagina escanea la tabla"
    )


# ---------------------------------------------------------------------------
# Sidebar, busqueda y barra de estado (secciones 23, 24, 29, 30 y 34)
# ---------------------------------------------------------------------------


def test_el_sidebar_actualiza_solo_la_fila_afectada(app, session, chat_largo):
    """Actualizar una fila no puede reconstruir la lista entera (seccion 23)."""
    app.viewer.refresh_chats()
    app.root.update()

    resumen = repo.chat_summary(session, chat_largo)
    assert resumen is not None

    lista = app.viewer.chats
    lista.set_chats([resumen])
    app.root.update()
    fila = lista._rows[chat_largo]
    widget_antes = fila["preview"]

    cambiado = repo.ChatSummary(
        id=resumen.id, jid=resumen.jid, display_name=resumen.display_name,
        chat_type=resumen.chat_type, last_message="📷 Imagen",
        last_message_timestamp=1_754_999_999, message_count=resumen.message_count + 1,
    )
    assert lista.update_chat(cambiado) is True
    app.root.update()

    assert lista._rows[chat_largo]["preview"] is widget_antes, (
        "la fila debe reutilizarse, no recrearse"
    )
    assert widget_antes.cget("text") == "📷 Imagen"


def test_un_chat_que_no_esta_en_la_lista_pide_recarga_completa(app, session):
    """Si la fila no existe, ``update_chat`` lo dice en vez de fingir."""
    lista = app.viewer.chats
    lista.set_chats([])
    app.root.update()

    inexistente = repo.ChatSummary(
        id=-1, jid="0@s.whatsapp.net", display_name="Nadie",
        chat_type="individual", last_message=None,
        last_message_timestamp=None, message_count=0,
    )
    assert lista.update_chat(inexistente) is False


def test_la_busqueda_no_consulta_en_cada_tecla(app, monkeypatch):
    """Una consulta por pausa, no por pulsacion (seccion 24)."""
    consultas = {"n": 0}

    def contando(search=None):
        consultas["n"] += 1
        return 0

    monkeypatch.setattr(app.viewer, "refresh_chats", contando)

    for texto in ("m", "ma", "mar", "marc", "marco"):
        app.viewer._on_search(texto)
    assert consultas["n"] == 0, "no puede consultarse antes de la pausa"

    # Se deja vencer el retardo: solo entonces se consulta, y una sola vez.
    app.root.after(app.viewer.SEARCH_DEBOUNCE_MS + 120, app.root.quit)
    app.root.mainloop()
    assert consultas["n"] == 1, f"deberia haber UNA consulta, hubo {consultas['n']}"


def test_la_barra_de_estado_resume_el_trabajo_de_fondo(app):
    from app.orchestrator import RuntimeStatus

    barra = app.viewer.status_bar
    app.viewer.update_status(
        RuntimeStatus(
            connection="Conectado", connected=True, history="sincronizando",
            media_pending=23, backfill="excavando",
        )
    )
    app.root.update()
    detalle = barra._detail.cget("text")
    assert "Historial: sincronizando" in detalle
    assert "23 pendientes" in detalle
    assert "Backfill: excavando" in detalle

    app.viewer.update_status(
        RuntimeStatus(
            connection="Conectado", connected=True, history="sincronizado",
            history_done=True, media_pending=0, backfill="terminado",
            backfill_done=True,
        )
    )
    app.root.update()
    assert "Sincronizacion completa" in barra._detail.cget("text")


def test_un_mensaje_nuevo_se_anade_al_final_sin_repintar(app, session, chat_largo):
    """Llega un mensaje: se anade UNA burbuja, no se recarga el chat."""
    panel = _abrir(app, session, chat_largo)
    pintados_antes = panel._rendered
    hijos_antes = len(panel._scroll.body.winfo_children())

    repo.bulk_upsert_messages(
        session,
        {CHAT_JID: chat_largo},
        [
            IncomingMessage(
                chat_jid=CHAT_JID, timestamp=1_755_999_999, source="live",
                whatsapp_message_id="PRDLIVE01", text="mensaje recien llegado",
                message_type="text",
            )
        ],
    )
    session.flush()

    assert app.viewer.append_new_message(chat_largo) is True
    app.root.update()

    assert panel._rendered == pintados_antes + 1
    assert len(panel._scroll.body.winfo_children()) > hijos_antes
    # Y no se vuelve a anadir si se pide otra vez: ya esta pintado.
    assert app.viewer.append_new_message(chat_largo) is False


def test_la_huella_de_disco_coincide_con_la_del_dispositivo_vivo(settings):
    """Ambas huellas DEBEN salir iguales o la marca nunca casaria.

    ``main`` la calcula del ``device.json`` antes de conectar, para decidir si
    hay que esperar el bootstrap; ``BackfillService`` la calcula del
    dispositivo vivo, y es la que se escribe al confirmarlo. Si divergieran,
    la marca se guardaria bajo una huella y se buscaria bajo otra: la espera de
    180 segundos volveria en cada arranque sin que nada lo delatara.
    """
    import hashlib
    import json

    if not settings.session_file.exists():
        pytest.skip("no hay sesion guardada en este equipo")

    from main import session_fingerprint_from_disk

    datos = json.loads(settings.session_file.read_text(encoding="utf-8"))
    jid = datos.get("jid") or {}
    if not jid.get("user"):
        pytest.skip("la sesion guardada no tiene JID")

    # Misma formula que BackfillService.session_fingerprint(), pero partiendo
    # de los mismos campos que tendria el dispositivo ya cargado.
    crudo = f"{jid['user']}:{jid.get('server')}:{datos.get('device_id', '')}"
    como_el_backfill = hashlib.sha256(crudo.encode()).hexdigest()[:16]

    assert session_fingerprint_from_disk(settings) == como_el_backfill
