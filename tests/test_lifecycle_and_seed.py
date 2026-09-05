"""Ciclo de vida de la vinculacion y recuperacion de semillas.

Tres bloques, todos nacidos de fallos MEDIDOS en produccion:

* doble vinculacion: un callback tardio de un intento viejo reabria el
  pairing y obligaba a escanear otra vez;
* el receptor tiene que seguir vivo durante el backfill, no despues;
* un chat sin ancla no esta "sincronizado": espera una semilla, y en cuanto
  llega un mensaje real debe volverse excavable.

Ninguna toca Signal, ni el protobuf, ni el wire de ON_DEMAND, ni los cursores
ya validados.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("flask")

from app.services import repository as repo  # noqa: E402
from app.services.repository import IncomingMessage  # noqa: E402
from tests.conftest import _DatabaseShim  # noqa: E402

CHAT_JID = "34600444555@s.whatsapp.net"


class EventoFalso:
    """Evento del bus, con su generacion opcional."""

    def __init__(self, name, payload=None, **extra):
        self.name = name
        self.payload = payload
        self.extra = dict(extra)


@pytest.fixture
def runtime(settings, database, session, tmp_path):
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
    rt._whatsapp = True
    return rt




# ---------------------------------------------------------------------------
# Doble vinculacion
# ---------------------------------------------------------------------------


def test_pair_success_cierra_la_vinculacion(runtime):
    """Despues del pair-success no se genera ni un QR mas.

    Es lo que obligaba a escanear dos veces: el flujo seguia vivo y sacaba
    codigos nuevos mientras la conexion posterior se establecia.
    """
    from app.core.session_state import AppState

    runtime.pairing._on_renew = lambda: None
    runtime.pairing.note_qr("2@" + "A" * 60 + ",B,C,D")
    assert runtime.pairing.generation == 1

    runtime._observar_evento(EventoFalso("paired", "34600111222@s.whatsapp.net"))

    assert runtime.pairing.committed is True
    assert runtime.pairing.available is False
    assert runtime.state.state is AppState.CONNECTING

    # Un QR posterior NO reabre la pantalla de vinculacion.
    runtime._observar_evento(EventoFalso("qr", "2@" + "B" * 60 + ",B,C,D"))
    assert runtime.pairing.generation == 1
    assert runtime.state.state is AppState.CONNECTING


def test_un_401_tardio_de_otra_generacion_se_ignora(runtime):
    """El caso exacto del enunciado: generacion 10 vincula, llega su 401."""
    from app.core.session_state import AppState

    runtime.pairing._on_renew = lambda: None
    vieja = runtime.pairing.connection_generation
    nueva = runtime.pairing.next_generation()
    assert nueva != vieja

    runtime.state.set(AppState.CONNECTED, reason="prueba")
    antes = runtime.counters["stale_callbacks_ignored"]

    runtime._observar_evento(EventoFalso("logged_out", 401, generation=vieja))

    assert runtime.state.state is AppState.CONNECTED, "un callback viejo no manda"
    assert runtime.counters["stale_callbacks_ignored"] == antes + 1


def test_tras_el_pair_success_no_se_reinicia_la_vinculacion(runtime):
    """Reiniciar aqui pediria un SEGUNDO escaneo."""
    reinicios: list = []
    runtime.pairing._on_renew = lambda: reinicios.append(True)
    runtime.pairing.note_qr("2@" + "C" * 60 + ",B,C,D")
    runtime.pairing.commit()

    runtime.restart_pairing()
    assert not reinicios, "no puede lanzarse otra vinculacion tras el pair-success"

    # Y el fin del cliente tampoco la reabre: lo que falta es la reconexion.
    runtime._observar_evento(EventoFalso("client_stopped", None))
    assert not reinicios


def test_un_401_no_destruye_la_sesion(runtime):
    """El bucle NO se corta archivando la sesion, sino dejando de intentar.

    Esta prueba comprobaba lo contrario: que un 401 archivara el
    ``device.json``. Archivar cerraba el bucle, si, pero destruyendo la
    vinculacion de forma automatica, y un 401 puede venir de un corte de red o
    de un rechazo temporal. Ademas fallaba: el archivado chocaba con un
    ``WinError 32`` sobre un fichero abierto por este mismo proceso, se
    abortaba a medias y el sistema entraba en bucle igualmente (74 logins y 61
    QR en segundos, 99 carpetas vacias).

    Ahora el freno es contar: al tercer rechazo de la misma sesion se para y
    se pide intervencion. Archivar queda para ``--fresh``, cuando alguien lo
    ha decidido.
    """
    from app.core.session_state import AppState

    runtime.pairing._on_renew = lambda: None
    runtime.settings.session_file.write_text('{"jid": {"user": "x"}}', encoding="utf-8")

    runtime.state.set(AppState.CONNECTED, reason="prueba")
    runtime._observar_evento(EventoFalso("logged_out", 401))

    assert runtime.state.state is AppState.SESSION_INVALID
    assert runtime.settings.session_file.exists(), (
        "un 401 no puede destruir la vinculacion por su cuenta"
    )
    assert runtime.pairing.committed is False, "hay que poder vincular otra vez"


def test_el_tercer_401_seguido_pide_una_vinculacion_nueva(runtime):
    """Al tercer rechazo de la MISMA sesion, el servidor ya no es ambiguo.

    Esta prueba comprobaba lo contrario: que el sistema se detuviera con
    ERROR sin tocar nada. Parecia lo prudente, y dejaba algo peor -- un estado
    del que no se sale solo::

        state=PAIRING  qr_available=false  session_file_present=true

    Con el ``device.json`` muerto en su sitio, el cliente nuevo hacia login en
    vez de pedir un codigo, asi que no habia QR nunca y el frontend giraba
    para siempre.

    Ahora al tercero se archiva (que NO es borrar: queda copia en
    diagnostics/) y se pide un codigo nuevo. PostgreSQL no se toca.
    """
    from app.core.session_state import AppState

    runtime.pairing._on_renew = lambda: None
    runtime.settings.session_file.write_text('{"jid": {"user": "x"}}', encoding="utf-8")

    for _ in range(runtime.MAX_RECHAZOS_MISMA_SESION):
        runtime._observar_evento(EventoFalso("logged_out", 401))

    assert runtime.state.state is not AppState.ERROR
    assert not runtime.settings.session_file.exists(), (
        "sin apartar la sesion muerta no puede haber QR nuevo"
    )
    archivadas = list(runtime.settings.diagnostics_dir.glob("session-*"))
    assert archivadas, "archivar no es borrar"


def test_la_generacion_sube_al_reiniciar(runtime):
    runtime.pairing._on_renew = lambda: None
    inicial = runtime.pairing.connection_generation
    runtime.restart_pairing()
    assert runtime.pairing.connection_generation > inicial


# ---------------------------------------------------------------------------
# Live durante backfill
# ---------------------------------------------------------------------------


def test_un_mensaje_live_se_persiste_mientras_el_backfill_corre(session, runtime):
    """Concurrencia real: el receptor NO espera a que acabe el backfill.

    Se ejecuta un backfill simulado que cede el control (``await``) y, en
    mitad, entra un mensaje. Tiene que quedar en PostgreSQL ANTES de que el
    backfill termine.
    """
    import asyncio

    chat_id = repo.upsert_chat(session, jid=CHAT_JID, chat_type="individual")
    session.flush()

    persistido_en_ronda: list[int] = []

    async def backfill_simulado() -> None:
        for ronda in range(5):
            await asyncio.sleep(0.01)  # cede el control, como el real
            if ronda == 2:
                # Entra un mensaje justo en mitad.
                repo.bulk_upsert_messages(
                    session,
                    {CHAT_JID: chat_id},
                    [
                        IncomingMessage(
                            chat_jid=CHAT_JID, timestamp=1_788_400_000,
                            source="live", whatsapp_message_id="LIVEMID001",
                            text="durante el backfill", message_type="text",
                        )
                    ],
                )
                session.flush()
                persistido_en_ronda.append(ronda)

    asyncio.run(backfill_simulado())

    assert persistido_en_ronda == [2], "el mensaje entro a mitad del backfill"
    assert repo.count_messages(session, CHAT_JID) == 1


def test_el_backfill_cede_el_control_entre_rondas():
    """No puede monopolizar el event loop, o el receptor se queda mudo."""
    import inspect

    from app.services.backfill_service import BackfillService

    fuente = inspect.getsource(BackfillService)
    assert "time.sleep(" not in fuente, (
        "time.sleep bloquea el event loop y con el, la recepcion de mensajes"
    )
    assert "await asyncio.sleep" in fuente or "await " in fuente


def test_el_worker_de_media_no_bloquea_el_bucle():
    import inspect

    from app.services.media_service import MediaService

    fuente = inspect.getsource(MediaService)
    assert "time.sleep(" not in fuente
    assert "await asyncio.sleep" in fuente


def test_los_contadores_dicen_donde_muere_un_mensaje(runtime, cliente):
    """Sin etapas, un mensaje perdido solo se puede adivinar."""
    for etapa in (
        "receiver_messages_seen",
        "live_handle_called",
        "live_persisted",
        "live_duplicates",
        "sse_message_created",
        "stale_callbacks_ignored",
    ):
        assert etapa in runtime.counters

    cuerpo = cliente.get("/api/v1/sync/status").get_json()
    assert "diagnostics" in cuerpo
    assert "receiver_messages_seen" in cuerpo["diagnostics"]
    assert "decrypt_errors" in cuerpo["diagnostics"]


def test_watching_es_un_estado_no_un_final(runtime, cliente):
    """Al acabar el backfill el servicio SIGUE vivo esperando cambios."""
    runtime.set_sync_state("WATCHING")
    assert runtime.sync_state == "WATCHING"
    assert cliente.get("/api/v1/sync/status").get_json()["sync_state"] == "WATCHING"


# ---------------------------------------------------------------------------
# SSE robusto
# ---------------------------------------------------------------------------


def test_los_eventos_sse_llevan_id_monotono(cliente):
    respuesta = cliente.get("/api/v1/events/stream")
    trozos = []
    for trozo in respuesta.response:
        trozos.append(trozo.decode("utf-8"))
        if len(trozos) >= 2:
            break
    respuesta.close()

    ids = [
        int(l.split(":", 1)[1].strip())
        for l in "".join(trozos).splitlines()
        if l.startswith("id:")
    ]
    assert len(ids) >= 2
    assert ids == sorted(ids), "los ids tienen que ser monotonos"


def test_el_latido_lleva_estado(runtime):
    """Un comentario mudo no distingue 'todo bien' de 'colgado'."""
    import inspect

    from app.api import routes

    fuente = inspect.getsource(routes.events_stream)
    assert '"heartbeat"' in fuente
    assert "session_state" in fuente and "sync_state" in fuente


def test_media_updated_se_traduce_una_sola_vez(session, runtime):
    """Dos clientes SSE traduciendo el MISMO evento no pueden duplicarlo.

    Se midio con dos pestanas abiertas: cada media.updated salia dos veces,
    mismo chat, misma media, mismo estado.
    """
    from app.api.live_events import translate

    evento = EventoFalso("media_ready", {"media_id": 1, "chat_id": 2, "status": "downloaded"})

    primera = translate(evento, runtime)
    segunda = translate(evento, runtime)
    assert segunda is primera, "la segunda traduccion reutiliza el memo"


# ---------------------------------------------------------------------------
# Semillas
# ---------------------------------------------------------------------------


def test_un_chat_sin_mensajes_no_esta_completo(session, runtime):
    """``message_count=0`` NO puede presentarse como historial sincronizado."""
    from app.api.serializers import historia_to_json
    from app.services.seed_recovery import SeedRecovery

    chat_id = repo.upsert_chat(session, jid=CHAT_JID, chat_type="individual")
    repo.get_or_create_history_state(session, chat_id=chat_id, chat_jid=CHAT_JID)
    session.flush()

    SeedRecovery(runtime.database).classify()
    estado = repo.history_state_for(session, CHAT_JID)
    cuerpo = historia_to_json(estado, 0)

    assert cuerpo["status"] == "waiting_seed"
    assert cuerpo["complete"] is False
    assert cuerpo["can_dig"] is False
    assert cuerpo["waiting_seed"] is True


def test_un_mensaje_real_convierte_el_chat_en_excavable(session, runtime):
    """WAITING_SEED -> PENDING en cuanto aparece un ancla de verdad."""
    from app.services.seed_recovery import SeedRecovery

    chat_id = repo.upsert_chat(session, jid=CHAT_JID, chat_type="individual")
    repo.get_or_create_history_state(session, chat_id=chat_id, chat_jid=CHAT_JID)
    session.flush()

    recuperador = SeedRecovery(runtime.database)
    recuperador.classify()
    assert repo.history_state_for(session, CHAT_JID)[0] == "waiting_seed"

    # Llega un mensaje con ID REAL de WhatsApp: eso es un ancla.
    repo.bulk_upsert_messages(
        session,
        {CHAT_JID: chat_id},
        [
            IncomingMessage(
                chat_jid=CHAT_JID, timestamp=1_788_500_000, source="live",
                whatsapp_message_id="SEED0001", text="hola", message_type="text",
            )
        ],
    )
    session.flush()

    informe = recuperador.seed_from_messages([CHAT_JID])
    assert informe.sembrados == 1

    estado = repo.history_state_for(session, CHAT_JID)
    assert estado[0] == "pending"
    assert estado[1] == "SEED0001", "el ancla es el ID REAL, no uno fabricado"


def test_un_mensaje_sin_id_real_no_sirve_de_ancla(session, runtime):
    """Nunca se fabrica un cursor: el servidor no lo reconoceria."""
    from app.services.seed_recovery import SeedRecovery

    chat_id = repo.upsert_chat(session, jid=CHAT_JID, chat_type="individual")
    repo.get_or_create_history_state(session, chat_id=chat_id, chat_jid=CHAT_JID)
    repo.bulk_upsert_messages(
        session,
        {CHAT_JID: chat_id},
        [
            IncomingMessage(
                chat_jid=CHAT_JID, timestamp=1_788_500_000, source="live",
                whatsapp_message_id=None, synthetic_identifier="local-1",
                text="sin id", message_type="text",
            )
        ],
    )
    session.flush()

    recuperador = SeedRecovery(runtime.database)
    recuperador.classify()
    informe = recuperador.seed_from_messages([CHAT_JID])

    assert informe.sembrados == 0
    assert repo.history_state_for(session, CHAT_JID)[0] == "waiting_seed"


def test_nunca_se_usa_un_cursor_sintetico():
    """Ni id vacio, ni el JID, ni un timestamp inventado."""
    import inspect

    from app.services import seed_recovery

    fuente = inspect.getsource(seed_recovery)
    assert "get_valid_history_cursor" in fuente, (
        "el ancla se toma de la funcion central, que rechaza los ids fabricados"
    )
    for prohibido in ('oldestMsgID=""', "synthetic-", "opaque-"):
        assert prohibido not in fuente


def test_la_funcion_central_rechaza_los_ids_fabricados():
    """Y no de palabra: se comprueba llamandola.

    Una prueba que solo mirase el codigo fuente pasaria aunque el filtro
    estuviera puesto y no se aplicara.
    """
    from app.history.cursor import CursorInfo, _del_estado

    class _Estado:
        oldest_message_timestamp = 1_760_000_000
        oldest_from_me = False

        def __init__(self, wamid):
            self.oldest_message_id = wamid

    assert _del_estado(_Estado("3A1F8BDD4678EB6DE395")) is not None
    for fabricado in ("opaque-1", "synthetic-9", "local-x", "", None):
        assert _del_estado(_Estado(fabricado)) is None
    assert isinstance(_del_estado(_Estado("3A1F8BDD4678EB6DE395")), CursorInfo)


def test_las_metricas_separan_completo_de_sin_semilla(session, cliente):
    """Agrupar todo como "terminado" era justamente el problema."""
    contadores = repo.history_counters(session)
    for clave in (
        "chats_total", "chats_complete", "chats_waiting_seed",
        "chats_no_cursor", "chats_empty_confirmed", "chats_seedless",
    ):
        assert clave in contadores

    cuerpo = cliente.get("/api/v1/sync/status").get_json()
    assert "chats" in cuerpo
    assert "chats_waiting_seed" in cuerpo["chats"]


def test_el_chat_expone_su_estado_historico(cliente, session):
    """El frontend no puede decir "sincronizado" sin saber el estado."""
    chat_id = repo.upsert_chat(session, jid=CHAT_JID, chat_type="individual")
    repo.get_or_create_history_state(session, chat_id=chat_id, chat_jid=CHAT_JID)
    session.flush()

    cuerpo = cliente.get(f"/api/v1/chats/{chat_id}").get_json()
    assert "history" in cuerpo
    for clave in ("status", "label", "complete", "can_dig", "waiting_seed"):
        assert clave in cuerpo["history"]


def test_no_existe_operacion_de_historial_sin_ancla():
    """Se deja escrito: el protocolo NO ofrece "dame los ultimos N".

    ``PeerDataOperationRequestType`` solo tiene cinco valores y uno solo es
    de historial, y va anclado por definicion. Si algun dia WhatsApp anade
    otro, esta prueba fallara y habra que mirarlo: es un recordatorio, no un
    dogma.
    """
    proto = Path("app/models/proto/whatsapp_backup.proto").read_text(encoding="utf-8")
    inicio = proto.index("enum PeerDataOperationRequestType")
    bloque = proto[inicio : proto.index("}", inicio)]
    valores = [l.split("=")[0].strip() for l in bloque.splitlines() if "=" in l]

    assert sorted(valores) == sorted([
        "UPLOAD_STICKER",
        "SEND_RECENT_STICKER_BOOTSTRAP",
        "GENERATE_LINK_PREVIEW",
        "HISTORY_SYNC_ON_DEMAND",
        "PLACEHOLDER_MESSAGE_RESEND",
    ])
    # Y la unica de historial exige ancla.
    assert "oldestMsgID" in proto


# ---------------------------------------------------------------------------
# Descifrado: transitorio vs perdido
# ---------------------------------------------------------------------------


def test_un_fallo_de_descifrado_se_clasifica_por_motivo(runtime):
    """Contar "errores" a secas no dice si es sender-key, MAC o sesion."""
    runtime._observar_evento(
        EventoFalso("decrypt_error", "WAMID001", args=["no session for peer x@lid"])
    )
    assert runtime.counters["receiver_decrypt_errors"] == 1
    assert runtime.counters["no_session"] == 1
    assert runtime.counters["decrypt_unrecovered"] == 1

    runtime._observar_evento(
        EventoFalso("decrypt_error", "WAMID002", args=["no sender-key for group"])
    )
    assert runtime.counters["sender_key_missing"] == 1

    runtime._observar_evento(
        EventoFalso("decrypt_error", "WAMID003", args=["mac check failed"])
    )
    assert runtime.counters["mac_failures"] == 1


def test_un_reintento_que_funciona_se_marca_recuperado(runtime):
    """Un fallo que el retry receipt resuelve NO es un mensaje perdido."""
    runtime._observar_evento(
        EventoFalso("decrypt_error", "WAMID010", args=["no session for peer"])
    )
    assert runtime.counters["decrypt_unrecovered"] == 1
    assert runtime.counters["decrypt_recovered"] == 0

    # El mismo mensaje llega descifrado tras el reintento.
    runtime._marcar_recuperado("WAMID010")

    assert runtime.counters["decrypt_recovered"] == 1
    assert runtime.counters["decrypt_unrecovered"] == 0


def test_el_mismo_fallo_repetido_no_cuenta_dos_veces(runtime):
    """pywhats reintenta: el mismo wamid puede fallar varias veces."""
    for _ in range(3):
        runtime._observar_evento(
            EventoFalso("decrypt_error", "WAMID020", args=["no session"])
        )
    assert runtime.counters["receiver_decrypt_errors"] == 3
    assert runtime.counters["decrypt_unrecovered"] == 1, (
        "es UN mensaje sin recuperar, no tres"
    )


def test_los_fallos_recordados_estan_acotados(runtime):
    """Una sesion larga no puede acumular identificadores sin limite."""
    for i in range(runtime.MAX_DECRYPT_PENDIENTES + 50):
        runtime._observar_evento(
            EventoFalso("decrypt_error", f"WAMID{i:05d}", args=["no session"])
        )
    assert len(runtime._decrypt_pendientes) <= runtime.MAX_DECRYPT_PENDIENTES


def test_no_se_registra_el_contenido_de_un_mensaje_fallido():
    """Un fallo de descifrado no puede filtrar texto privado."""
    import inspect

    from app.core.runtime import AppRuntime

    fuente = inspect.getsource(AppRuntime._anotar_fallo_descifrado)
    assert "text" not in fuente.replace("contenido", "")
    assert "wamid" in fuente and "motivo" in fuente


# ---------------------------------------------------------------------------
# El freno: un 401 en bucle no puede martillear el servidor
# ---------------------------------------------------------------------------


def test_los_reintentos_de_vinculacion_tienen_freno(runtime):
    """Se midieron 74 intentos de login y 61 QR en segundos. Inaceptable.

    Un bucle asi no solo es inutil: golpea un servicio ajeno y arriesga que
    la cuenta acabe limitada.
    """
    runtime.pairing._on_renew = lambda: None

    # El primero pasa; el segundo inmediato NO.
    assert runtime._puede_reintentar() is True
    assert runtime._puede_reintentar() is False, "sin espera no se reintenta"


def test_la_espera_entre_reintentos_crece(runtime):
    """5, 10, 20, 40... para no insistir al mismo ritmo indefinidamente."""
    runtime.pairing._on_renew = lambda: None

    esperas = []
    for intento in range(4):
        esperas.append(
            min(
                runtime.REINTENTO_BASE * (2 ** intento),
                runtime.REINTENTO_MAXIMO,
            )
        )
    assert esperas == sorted(esperas), "la espera tiene que crecer"
    assert esperas[-1] <= runtime.REINTENTO_MAXIMO


def test_tras_demasiados_intentos_se_para_y_se_avisa(runtime):
    """Insistir sin fin no arregla nada: se para y se pide intervencion."""
    from app.core.session_state import AppState

    runtime.pairing._on_renew = lambda: None
    runtime._reintentos_seguidos = runtime.REINTENTOS_MAXIMOS

    assert runtime._puede_reintentar() is False
    assert runtime.state.state is AppState.ERROR


def test_una_conexion_buena_borra_la_cuenta_de_intentos(runtime):
    runtime.pairing._on_renew = lambda: None
    runtime._reintentos_seguidos = 5

    runtime._observar_evento(EventoFalso("session_valid", None))

    assert runtime._reintentos_seguidos == 0


def test_archivar_no_lanza_aunque_haya_archivos_bloqueados(settings, tmp_path):
    """Windows bloqueaba compat_prekey.db y la excepcion abortaba el manejo.

    Ese aborto dejaba el estado a medias y disparaba el bucle de reintentos.
    """
    import dataclasses

    from app.whatsapp_client import archive_session

    sesion = tmp_path / "session"
    sesion.mkdir()
    (sesion / "device.json").write_text("{}", encoding="utf-8")
    (sesion / "compat_prekey.db").write_text("x", encoding="utf-8")

    aislado = dataclasses.replace(
        settings, session_dir=sesion, diagnostics_dir=tmp_path / "diagnostics"
    )
    (tmp_path / "diagnostics").mkdir()

    # Con un archivo abierto (bloqueado en Windows) no puede reventar.
    with open(sesion / "compat_prekey.db", "r", encoding="utf-8"):
        destino = archive_session(aislado, reason="prueba")

    # device.json tiene que haber salido de en medio pase lo que pase.
    assert not (sesion / "device.json").exists()
    assert destino is not None


def test_no_se_deja_una_carpeta_vacia_si_no_se_archivo_nada(settings, tmp_path):
    """Se llegaron a crear 99 carpetas vacias en un bucle."""
    import dataclasses

    from app.whatsapp_client import archive_session

    sesion = tmp_path / "session"
    sesion.mkdir()
    diagnosticos = tmp_path / "diagnostics"
    diagnosticos.mkdir()

    aislado = dataclasses.replace(
        settings, session_dir=sesion, diagnostics_dir=diagnosticos
    )

    # Sin device.json no hay nada que archivar.
    assert archive_session(aislado, reason="prueba") is None
    assert list(diagnosticos.iterdir()) == [], "no puede quedar carpeta vacia"
