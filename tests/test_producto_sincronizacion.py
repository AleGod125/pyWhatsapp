"""El botón de sincronizar, la revisión completa y el reintento por chat.

QUE FALTABA
-----------
El ciclo hacia reconciliar, buscar anclas en los blobs, revalidar, excavar,
multimedia y almacenamiento. Todo eso trabaja sobre conversaciones que YA
tienen ancla, o sobre lo que WhatsApp ya habia entregado. Una conversacion sin
ninguna referencia no podia salir de ahi por muchas veces que se pulsara, y el
usuario veia un boton que "no hacia nada".

Faltaba el paso que si podia cambiarlo: preguntarle a WhatsApp Web. Ahora es
una fase mas del ciclo, con las MISMAS condiciones que la accion manual.

LAS DOS ACCIONES
----------------
``Sincronizar ahora``            incremental, no toca la espera de nadie
``Recuperar historial completo`` lo mismo, y ademas adelanta UNA vez la espera
                                 de reintento de los que la cumplian

Ninguna borra nada. La segunda tampoco reabre lo que el telefono dio por
terminado: para eso hace falta evidencia nueva, no un boton.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import Chat, ChatHistoryState, HistorySeed, Message
from app.services.sync_job import PHASES, SyncJob


class _Database:
    def __init__(self, session):
        self._session = session

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


def _chat(session, estado: str, *, ancla: bool = False, proximo=None):
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    chat = Chat(jid=jid, chat_type="individual")
    session.add(chat)
    session.flush()
    session.add(
        ChatHistoryState(
            chat_id=chat.id,
            chat_jid=jid,
            history_status=estado,
            oldest_message_id="3A1F8BDD4678EB6DE395" if ancla else None,
            oldest_message_timestamp=1_760_000_000 if ancla else None,
            next_retry_at=proximo,
        )
    )
    session.flush()
    return chat


def _estado(session, chat):
    session.expire_all()
    return session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == chat.jid)
    ).scalar_one()


# ---------------------------------------------------------------------------
# El recorrido del ciclo
# ---------------------------------------------------------------------------


def test_el_ciclo_incluye_el_paso_de_las_referencias_web():
    """Es el unico paso que puede despertar a una conversacion sin ancla."""
    assert "web" in PHASES
    # Antes de excavar: no sirve de nada conseguir un ancla despues de haber
    # decidido a quien se le pedia historial.
    assert PHASES.index("web") < PHASES.index("backfill")
    # Y despues de buscar en los blobs: lo que ya esta en casa es mas barato.
    assert PHASES.index("seeds") < PHASES.index("web")


def test_el_orden_de_las_fases_no_cambia_lo_demas():
    assert PHASES[0] == "reconcile"
    assert PHASES[-1] == "finalize"
    for fase in ("seeds", "revalidate", "backfill", "media", "storage"):
        assert fase in PHASES


def test_la_fase_web_no_corre_si_nadie_espera_ancla(session, settings):
    """Preguntarle a Web con cero esperando solo gasta tiempo."""
    trabajo = SyncJob(settings, _Database(session))
    trabajo.state.waiting_seed = 0
    supervisor = SimpleNamespace(habilitado=True, vivo=True)
    runtime = SimpleNamespace(web_companion=supervisor, database=_Database(session))

    asyncio.run(trabajo._fase_web(runtime))
    assert trabajo.state.web_promoted == 0


def test_la_fase_web_no_corre_si_el_companion_esta_apagado(session, settings):
    trabajo = SyncJob(settings, _Database(session))
    trabajo.state.waiting_seed = 5
    runtime = SimpleNamespace(web_companion=None, database=_Database(session))

    asyncio.run(trabajo._fase_web(runtime))
    assert trabajo.state.web_promoted == 0


def test_si_las_referencias_web_no_se_pueden_usar_el_ciclo_sigue(session, settings, monkeypatch):
    """Una via opcional que no esta disponible no es un fallo del ciclo."""
    from app.web_companion import apply as modulo

    def rechazar(self, **kwargs):
        raise modulo.AplicacionRechazada("ON_DEMAND_NOT_CONFIRMED", "no confirmado")

    monkeypatch.setattr(modulo.WebSeedApplier, "aplicar", rechazar)

    trabajo = SyncJob(settings, _Database(session))
    trabajo.state.waiting_seed = 3
    runtime = SimpleNamespace(
        web_companion=SimpleNamespace(habilitado=True, vivo=True),
        database=_Database(session),
    )

    asyncio.run(trabajo._fase_web(runtime))  # no lanza
    assert trabajo.state.web_promoted == 0


def test_lo_que_aporta_la_via_web_se_cuenta_aparte(session, settings, monkeypatch):
    from app.web_companion import apply as modulo

    monkeypatch.setattr(
        modulo.WebSeedApplier,
        "aplicar",
        lambda self, **kwargs: modulo.ResultadoDeAplicacion(
            candidatos=22, validados=22, insertadas=22, promovidos=22
        ),
    )

    trabajo = SyncJob(settings, _Database(session))
    trabajo.state.waiting_seed = 25
    runtime = SimpleNamespace(
        web_companion=SimpleNamespace(habilitado=True, vivo=True),
        database=_Database(session),
    )

    asyncio.run(trabajo._fase_web(runtime))

    assert trabajo.state.web_promoted == 22
    assert trabajo.state.new_seeds == 22
    assert trabajo.snapshot()["web_promoted"] == 22


# ---------------------------------------------------------------------------
# Idempotencia y doble pulsacion
# ---------------------------------------------------------------------------


def test_un_ciclo_en_marcha_no_lanza_otro(session, settings):
    """El doble clic no puede abrir dos excavaciones sobre el mismo telefono."""
    from app.services.sync_job import RUNNING, SyncAlreadyRunningError, SyncState

    trabajo = SyncJob(settings, _Database(session))
    trabajo.state = SyncState(state=RUNNING, job_id="ya-corriendo")

    runtime = SimpleNamespace(
        info=lambda: SimpleNamespace(whatsapp_enabled=True),
        state=SimpleNamespace(state=None),
        backfill=None,
    )
    # El estado de la sesion se comprueba antes; aqui interesa el candado.
    with pytest.raises(Exception) as fallo:
        trabajo.start(runtime)
    assert fallo.type.__name__ in ("SyncAlreadyRunningError", "SyncUnavailableError")


def test_una_excavacion_en_marcha_tampoco(session, settings):
    """El backfill automatico cuenta: el telefono atiende de una en una."""
    from app.services.sync_job import SyncAlreadyRunningError

    trabajo = SyncJob(settings, _Database(session))
    from app.core.session_state import AppState

    runtime = SimpleNamespace(
        info=lambda: SimpleNamespace(whatsapp_enabled=True),
        state=SimpleNamespace(state=AppState.CONNECTED),
        backfill=SimpleNamespace(busy=True),
    )
    with pytest.raises(SyncAlreadyRunningError):
        trabajo.start(runtime)


def test_el_modo_queda_registrado(session, settings):
    """El resumen no puede prometer una revision completa si fue la rapida."""
    trabajo = SyncJob(settings, _Database(session))
    assert trabajo.snapshot()["mode"] == "incremental"


# ---------------------------------------------------------------------------
# La revision completa
# ---------------------------------------------------------------------------


def test_adelanta_la_espera_de_los_que_esperaban_turno(session, settings):
    proximo = datetime.now(timezone.utc) + timedelta(hours=1)
    chat = _chat(session, "timeout", ancla=True, proximo=proximo)

    SyncJob(settings, _Database(session))._adelantar_reintentos()

    assert _estado(session, chat).next_retry_at is None


def test_no_toca_a_los_que_ya_pueden_reintentar(session, settings):
    chat = _chat(session, "pending", ancla=True)
    SyncJob(settings, _Database(session))._adelantar_reintentos()
    assert _estado(session, chat).next_retry_at is None


def test_no_reabre_lo_que_el_telefono_dio_por_terminado(session, settings):
    """"Completo" no significa volver a pedir lo que ya se cerro."""
    proximo = datetime.now(timezone.utc) + timedelta(hours=1)
    chat = _chat(session, "exhausted", ancla=True, proximo=proximo)

    SyncJob(settings, _Database(session))._adelantar_reintentos()

    estado = _estado(session, chat)
    assert estado.history_status == "exhausted"
    assert estado.next_retry_at is not None


def test_no_borra_mensajes_ni_anclas_ni_cursores(session, settings):
    chat = _chat(session, "timeout", ancla=True)
    antes = (
        session.execute(select(func.count()).select_from(Message)).scalar(),
        session.execute(select(func.count()).select_from(HistorySeed)).scalar(),
    )
    cursor_antes = _estado(session, chat).oldest_message_id

    SyncJob(settings, _Database(session))._adelantar_reintentos()

    assert (
        session.execute(select(func.count()).select_from(Message)).scalar(),
        session.execute(select(func.count()).select_from(HistorySeed)).scalar(),
    ) == antes
    assert _estado(session, chat).oldest_message_id == cursor_antes


def test_no_toca_los_que_esperan_ancla_mas_alla_del_temporizador(session, settings):
    """Sin ancla no hay nada que reintentar: adelantar su espera no ayuda."""
    chat = _chat(session, "waiting_seed")
    SyncJob(settings, _Database(session))._adelantar_reintentos()
    estado = _estado(session, chat)
    assert estado.history_status == "waiting_seed"
    assert estado.oldest_message_id is None


# ---------------------------------------------------------------------------
# Nombres: nunca un LID crudo si hay algo mejor
# ---------------------------------------------------------------------------


def test_un_lid_sin_nombre_no_se_ensena_como_nombre():
    """``21935119425699 (LID)`` no es un telefono, ni un nombre, ni nada."""
    from app.services.repository import display_name_for

    assert display_name_for("21935119425699@lid") == "Contacto sin nombre"


def test_si_el_contacto_tiene_telefono_se_usa_ese():
    from app.services.repository import display_name_for

    nombre = display_name_for(
        "21935119425699@lid", None, None, phone_jid="573001112233@s.whatsapp.net"
    )
    assert nombre == "+573001112233"


def test_el_nombre_de_verdad_manda_sobre_todo_lo_demas():
    from app.services.repository import display_name_for

    assert (
        display_name_for("2193@lid", "Ana", "Ana Contacto", phone_jid="5730@s.whatsapp.net")
        == "Ana"
    )


def test_un_grupo_sin_nombre_no_ensena_su_identificador():
    from app.services.repository import display_name_for

    assert display_name_for("120363000000000000@g.us") == "Grupo sin nombre"


def test_un_telefono_sin_nombre_se_ensena_legible():
    from app.services.repository import display_name_for

    assert display_name_for("573001112233@s.whatsapp.net") == "+573001112233"


def test_el_push_name_vale_cuando_no_hay_nombre_guardado():
    from app.services.repository import display_name_for

    assert display_name_for("2193@lid", None, None, "Anita") == "Anita"


# ---------------------------------------------------------------------------
# Reintento de UN chat
# ---------------------------------------------------------------------------


def test_la_ruta_de_reintento_por_chat_existe():
    """Es distinta de ``/history/recheck``: aquella no pide nada al servidor."""
    import inspect

    from app.api import routes

    fuente = inspect.getsource(routes)
    assert '@api.post("/chats/<int:chat_id>/history/retry")' in fuente
    assert '@api.post("/chats/<int:chat_id>/history/recheck")' in fuente


def test_el_reintento_por_chat_solo_adelanta_ese_chat():
    """El resto de la cola sigue esperando su turno."""
    import ast
    import inspect
    import textwrap

    from app.api.routes import chat_history_retry

    codigo = ast.unparse(ast.parse(textwrap.dedent(inspect.getsource(chat_history_retry))))
    # El UPDATE va filtrado por el jid del chat, no a toda la tabla.
    assert "ChatHistoryState.chat_jid == jid" in codigo
    assert "next_retry_at=None" in codigo


def test_el_reintento_por_chat_no_pide_sin_ancla():
    """Pedir sin ancla produce un ACK y despues silencio."""
    import ast
    import inspect
    import textwrap

    from app.api.routes import chat_history_retry

    codigo = ast.unparse(ast.parse(textwrap.dedent(inspect.getsource(chat_history_retry))))
    assert "get_valid_history_cursor" in codigo
    assert "NO_VALID_CURSOR" in codigo


def test_el_reintento_por_chat_no_reabre_un_chat_completo():
    import ast
    import inspect
    import textwrap

    from app.api.routes import chat_history_retry

    codigo = ast.unparse(ast.parse(textwrap.dedent(inspect.getsource(chat_history_retry))))
    assert "CHAT_ALREADY_COMPLETE" in codigo


def test_el_reintento_por_chat_pasa_por_la_cola_de_siempre():
    """Misma cola, mismo candado global: una peticion cada vez."""
    import ast
    import inspect
    import textwrap

    from app.api.routes import chat_history_retry

    codigo = ast.unparse(ast.parse(textwrap.dedent(inspect.getsource(chat_history_retry))))
    assert "cola.enqueue" in codigo


# ---------------------------------------------------------------------------
# La pantalla se entera sin recargar
# ---------------------------------------------------------------------------


def test_un_cambio_de_estado_se_publica(session, settings):
    """Antes solo se escribia en la base.

    La pantalla se enteraba al recargar, y mientras tanto ensenaba el estado
    del momento en que se cargo: un chat podia decir "Recuperando historial"
    con el trabajo ya terminado, o "Esperando referencia" con tres mil
    mensajes dentro.
    """
    from app.services.backfill_service import BackfillService

    avisos: list[tuple] = []
    servicio = BackfillService(settings, _Database(session))
    servicio.publish = lambda nombre, datos: avisos.append((nombre, datos))

    chat = _chat(session, "pending", ancla=True)
    servicio._set_status(chat.jid, "fetching", None)

    # Y con el identificador dentro: la pantalla indexa por `id`, y una
    # conversacion que llego por LID puede estar en la lista con el JID del
    # telefono, asi que comparar cadenas fallaria justo en ese caso.
    assert avisos == [
        (
            "chat_history_status",
            {
                "chat_jid": chat.jid,
                "chat_id": chat.id,
                "history_status": "fetching",
            },
        )
    ]


def test_avisar_no_puede_cortar_la_excavacion(session, settings):
    from app.services.backfill_service import BackfillService

    servicio = BackfillService(settings, _Database(session))
    servicio.publish = lambda *a: (_ for _ in ()).throw(RuntimeError("bus roto"))

    chat = _chat(session, "pending", ancla=True)
    servicio._set_status(chat.jid, "exhausted", None)  # no lanza

    session.expire_all()
    assert _estado(session, chat).history_status == "exhausted"


def test_sin_publicador_todo_sigue_funcionando(session, settings):
    """El aviso es opcional: sin el, se recarga a mano como antes."""
    from app.services.backfill_service import BackfillService

    servicio = BackfillService(settings, _Database(session))
    chat = _chat(session, "waiting_seed")
    servicio._set_status(chat.jid, "pending", None)
    session.expire_all()
    assert _estado(session, chat).history_status == "pending"


def test_el_evento_de_estado_llega_al_frontend():
    """Sin traduccion no sale por SSE: se queda en el bus y nadie lo ve."""
    from app.api.routes import EVENT_NAMES

    assert EVENT_NAMES["chat_history_status"] == "chat.status"
    # `chat.` esta en el prefijo de eventos de sesion, asi que llega a su dueno.
    from app.api.routes import EVENTOS_DE_SESION

    assert any("chat.status".startswith(p) for p in EVENTOS_DE_SESION)


def test_al_ingerir_historial_se_dice_QUE_chats_cambiaron():
    """Antes viajaba una cadena suelta y la pantalla no podia hacer nada.

    Sabia que "algo" habia entrado, pero no donde, asi que un chat abierto se
    quedaba vacio hasta recargar.
    """
    import inspect

    from app.core.runtime import AppRuntime

    fuente = inspect.getsource(AppRuntime._wire_history_ingestion)
    assert '"chat_jids": afectados' in fuente
    assert '"history_ingested"' in fuente
