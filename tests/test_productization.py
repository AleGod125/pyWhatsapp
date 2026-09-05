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

from dataclasses import dataclass
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")

from app.services import repository as repo  # noqa: E402
from app.services.repository import ChatSummary, IncomingMessage  # noqa: E402

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
    from app.gui.chat_view import PAGE_SIZE

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








# ---------------------------------------------------------------------------
# Eventos de sistema (secciones 5, 6 y 31)
# ---------------------------------------------------------------------------


def test_llamada_perdida_se_clasifica_desde_el_stub():
    from app.core.system_message import CALL_KINDS, SystemMessageClassifier

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
    from app.core.system_message import SystemMessageClassifier

    evento = SystemMessageClassifier().classify_stub(9999)
    assert evento.known is False
    assert evento.label == "Evento del sistema"
    assert evento.stub_type == 9999


def test_los_stubs_verificados_salen_de_datos_reales():
    """Los marcados VERIFICADO se comprobaron contra este backup."""
    from app.core.system_message import STUB_TYPES

    for numero in (1, 24, 27, 28, 39, 72, 75):
        assert STUB_TYPES[numero].verified is True, (
            f"el stub {numero} aparece en la base y debe estar verificado"
        )


def test_el_protocolo_interno_sigue_oculto():
    """Un PeerDataOperation nuestro NO es un mensaje del chat (seccion 6)."""
    from app.core.message_classifier import (
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
    from app.core.message_classifier import MessageClass, classify_parsed, is_visible

    @dataclass
    class Falso:
        message_type: str = "system"

    clase = classify_parsed(Falso())
    assert clase is MessageClass.VISIBLE_SYSTEM_EVENT
    assert is_visible(clase)


def test_el_evento_de_llamada_se_lee_del_call_log():
    """``callLogMesssage`` con outcome MISSED e isVideo."""
    from app.core.system_message import classify_call_log

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
    from app.core.previews import preview_for

    assert preview_for(tipo, None) == esperado


def test_la_previa_nunca_dice_unknown_para_un_tipo_conocido():
    from app.core.previews import preview_for

    for tipo in ("image", "video", "audio", "sticker", "document", "poll"):
        previa = preview_for(tipo, None)
        assert "unknown" not in previa.lower()
        assert previa != "[unknown]"


def test_el_texto_manda_sobre_la_etiqueta_de_tipo():
    from app.core.previews import preview_for

    assert preview_for("image", "mira esta foto") == "mira esta foto"


def test_la_previa_de_un_evento_de_sistema_dice_que_paso():
    from app.core.previews import preview_for

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
















def test_la_miniatura_se_regenera_si_cambia_el_original(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    from app.services.thumbnails import cache_key

    original = tmp_path / "foto.jpg"
    Image.new("RGB", (400, 400), "#111111").save(original)
    primera = cache_key(original, (320, 320))

    Image.new("RGB", (800, 800), "#eeeeee").save(original)
    assert cache_key(original, (320, 320)) != primera




# ---------------------------------------------------------------------------
# Arranque: sin la espera de 180 segundos (seccion 18)
# ---------------------------------------------------------------------------



def test_historial_confirmado_no_espera_bootstrap():
    import asyncio
    import time

    from app.services.history_gate import InitialHistoryGate

    gate = InitialHistoryGate(settle_seconds=1.0, max_wait=180.0, already_confirmed=True)
    comenzo = time.monotonic()
    assert asyncio.run(gate.wait()) is True
    assert time.monotonic() - comenzo < 1.0, "no debe esperar nada"


def test_pairing_nuevo_si_vuelve_a_esperar():
    """Sin confirmacion previa, la barrera espera de verdad."""
    import asyncio
    import time

    from app.services.history_gate import InitialHistoryGate

    gate = InitialHistoryGate(settle_seconds=0.2, max_wait=2.0)
    comenzo = time.monotonic()
    assert asyncio.run(gate.wait()) is False
    assert time.monotonic() - comenzo >= 1.5, "debe agotar su plazo esperando"


def test_la_confirmacion_va_por_huella_de_sesion(session):
    """Una huella distinta (pairing nuevo) NO hereda la confirmacion.

    Usa la sesion transaccional del test, no la base directamente. La version
    anterior llamaba a ``confirm_initial_history(database, ...)``, que hace
    COMMIT de verdad: escribia en la base de produccion y luego "limpiaba"
    dejando la clave a NULL. Se encontro esa fila fantasma en una auditoria.
    Una prueba no puede escribir fuera de su transaccion.
    """
    from contextlib import contextmanager

    from app.services.history_gate import confirm_initial_history, initial_history_confirmed

    class BaseDeLaPrueba:
        """Expone ``transaction()`` sobre la sesion que se revierte al final."""

        def __init__(self, sesion) -> None:
            self._sesion = sesion

        def transaction(self):
            @contextmanager
            def scope():
                yield self._sesion
                self._sesion.flush()

            return scope()

    base = BaseDeLaPrueba(session)

    confirm_initial_history(base, "huella-de-prueba-a", chunks=3)
    assert initial_history_confirmed(base, "huella-de-prueba-a") is True
    assert initial_history_confirmed(base, "huella-de-prueba-b") is False
    assert initial_history_confirmed(base, None) is False


# ---------------------------------------------------------------------------
# Mantenimiento seguro (secciones 16 y 17)
# ---------------------------------------------------------------------------


def test_el_mantenimiento_no_rehace_su_propio_trabajo(database, settings):
    """La segunda pasada no vuelve a tocar lo que arreglo la primera.

    Antes esto exigia ``changed == 0`` a secas, y eso solo es cierto sobre una
    base quieta. Con ``service.py`` en marcha entran mensajes mientras corre el
    test: la segunda pasada encuentra trabajo NUEVO -- legitimo, de un chat que
    acaba de recibir su primer mensaje -- y el test fallaba culpando al
    mantenimiento de algo que no habia hecho.

    Lo que de verdad se quiere comprobar es que no se rehace: que ningun chat
    aparece arreglado dos veces. Eso si vale con la base viva.
    """
    from app.services.maintenance_service import MaintenanceService

    servicio = MaintenanceService(database, settings)
    primera = servicio.run_all()
    segunda = servicio.run_all()

    assert primera.errors == []
    assert segunda.errors == []

    repetidos = set(primera.seeds_recovered_chats) & set(segunda.seeds_recovered_chats)
    assert not repetidos, (
        f"la segunda pasada volvio a despertar los mismos chats: {len(repetidos)}"
    )

    # Y lo que se recalcula sin depender de actividad externa tiene que estar
    # ya estable: si esto cambia en la segunda pasada, el mantenimiento se
    # esta contradiciendo a si mismo.
    for campo in (
        "cursors_updated",
        "reclassified",
        "aliases_linked",
        "usernames_filled",
        "cursor_incoherent_fixed",
    ):
        assert getattr(segunda, campo) == 0, (
            f"'{campo}' sigue cambiando en la segunda pasada: {segunda}"
        )


def test_el_mantenimiento_no_es_destructivo():
    """No hay ni un DELETE en el modulo. Es la garantia de la seccion 16."""
    import app.services.maintenance_service as modulo

    # La ruta se pregunta al propio modulo en vez de escribirla a mano: asi
    # reorganizar el paquete no convierte esta garantia en un test que pasa
    # porque ya no encuentra el archivo.
    fuente = Path(modulo.__file__).read_text(encoding="utf-8")
    cuerpo = "\n".join(
        linea for linea in fuente.splitlines()
        if not linea.strip().startswith("#")
    )
    for prohibido in ("delete(", "DELETE FROM", "session.delete", "drop("):
        assert prohibido not in cuerpo, (
            f"el mantenimiento automatico no puede contener {prohibido!r}"
        )


@dataclass
class _Fuente:
    nombre: str
    texto: str


def _fuentes_del_arranque_automatico() -> list[_Fuente]:
    """Los archivos que SI corren solos al arrancar.

    Se localizan por modulo, no por ruta escrita a mano: reorganizar el
    paquete no puede convertir esta garantia en un test que pasa porque ya no
    encuentra el archivo.
    """
    import app.core.orchestrator as orquestador
    import app.services.maintenance_service as mantenimiento

    fuentes = [
        _Fuente("service.py", Path("service.py").read_text(encoding="utf-8")),
        _Fuente(
            "maintenance_service",
            Path(mantenimiento.__file__).read_text(encoding="utf-8"),
        ),
    ]
    for modulo in (orquestador,):
        fuentes.append(
            _Fuente(modulo.__name__, Path(modulo.__file__).read_text(encoding="utf-8"))
        )
    return fuentes


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
    for fuente in _fuentes_del_arranque_automatico():
        for invocacion in invocaciones:
            assert invocacion not in fuente.texto, (
                f"{fuente.nombre} no puede invocar repair_db: es destructivo "
                "y va a mano"
            )
        # Y tampoco por subproceso, que seria la puerta de atras.
        assert "repair_db.py" not in fuente.texto.replace("``repair_db.py``", ""), (
            f"{fuente.nombre} no puede lanzar repair_db.py como subproceso"
        )


def test_el_mantenimiento_conserva_los_mensajes(database, settings, session):
    """Contar antes y despues: la reconciliacion no puede perder filas."""
    from sqlalchemy import func, select

    from app.services.maintenance_service import MaintenanceService
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

    from app.services import repository

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

    from app.services.repository import get_messages_before

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












def test_la_huella_de_disco_coincide_con_la_del_dispositivo_vivo(settings):
    """Ambas huellas DEBEN salir iguales o la marca nunca casaria.

    ``app.core.identity`` la calcula del ``device.json`` antes de conectar,
    para decidir si hay que esperar el bootstrap; ``BackfillService`` la
    calcula del dispositivo vivo, y es la que se escribe al confirmarlo. Si divergieran,
    la marca se guardaria bajo una huella y se buscaria bajo otra: la espera de
    180 segundos volveria en cada arranque sin que nada lo delatara.

    Se llama al metodo REAL del backfill en vez de repetir su formula aqui:
    una copia de la formula se queda atras en cuanto una de las dos cambia, y
    entonces la prueba pasa mientras el sistema esta roto. Se midio: al anadir
    el ``registration_id`` a la huella, esta prueba seguia comparando la
    version vieja consigo misma.
    """
    import json
    import types

    if not settings.session_file.exists():
        pytest.skip("no hay sesion guardada en este equipo")

    from app.core.identity import session_fingerprint
    from app.services.backfill_service import BackfillService

    datos = json.loads(settings.session_file.read_text(encoding="utf-8"))
    jid = datos.get("jid") or {}
    if not jid.get("user"):
        pytest.skip("la sesion guardada no tiene JID")

    # El dispositivo tal y como lo veria el backfill ya conectado.
    dispositivo = types.SimpleNamespace(
        jid=types.SimpleNamespace(
            user=jid["user"], server=jid.get("server", "s.whatsapp.net")
        ),
        device_id=datos.get("device_id", ""),
        registration_id=datos.get("registration_id", ""),
    )
    servicio = object.__new__(BackfillService)
    servicio._client = types.SimpleNamespace(device=dispositivo)

    assert session_fingerprint(settings) == servicio.session_fingerprint()


def test_la_huella_distingue_identidades_con_la_misma_ranura(tmp_path):
    """Dos vinculaciones pueden recibir el MISMO device_id.

    El servidor reutiliza el numero de ranura: al desvincular todos los
    dispositivos la numeracion vuelve atras. Sin el registration_id, esas dos
    identidades compartirian huella y la segunda daria por confirmado el
    historial inicial de la primera.
    """
    import json
    import types

    from app.core.identity import session_fingerprint

    def huella_de(registration_id: int) -> str:
        ruta = tmp_path / f"device-{registration_id}.json"
        ruta.write_text(
            json.dumps(
                {
                    "jid": {"user": "573000000", "server": "s.whatsapp.net"},
                    "device_id": 5,  # LA MISMA ranura en las dos
                    "registration_id": registration_id,
                }
            ),
            encoding="utf-8",
        )
        return session_fingerprint(types.SimpleNamespace(session_file=ruta))

    assert huella_de(111) != huella_de(222)


def test_las_dos_definiciones_de_tener_ancla_son_la_misma(database, settings):
    """El chat que oscilaba para siempre.

    ``classify`` degradaba a 'waiting_seed' contando MENSAJES con
    identificador real; ``seed_from_messages`` promovia a 'pending' mirando el
    CURSOR canonico. Un chat con cursor guardado y sin mensajes cumplia las dos
    condiciones a la vez, asi que cada pasada de mantenimiento lo bajaba y lo
    subia. En la pantalla se veia como un chat que iba y venia entre "Esperando
    referencia" y "Pendiente de recuperacion" sin que nadie tocara nada.
    """
    import inspect

    from app.services.seed_recovery import SeedRecovery

    fuente = inspect.getsource(SeedRecovery.classify)
    assert "get_valid_history_cursor" in fuente, (
        "classify tiene que usar la funcion canonica, no contar mensajes"
    )


def test_el_mantenimiento_no_deja_ningun_chat_oscilando(database, settings):
    """Dos pasadas seguidas: ningun chat despertado dos veces."""
    from app.services.maintenance_service import MaintenanceService

    servicio = MaintenanceService(database, settings)
    primera = servicio.run_all()
    segunda = servicio.run_all()
    tercera = servicio.run_all()

    assert not (set(primera.seeds_recovered_chats) & set(segunda.seeds_recovered_chats))
    assert not (set(segunda.seeds_recovered_chats) & set(tercera.seeds_recovered_chats))
