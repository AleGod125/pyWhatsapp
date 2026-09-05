"""El boton de sincronizar: lo que puede hacer, y lo que dice cuando no puede.

EL PROBLEMA QUE SE ESTA ARREGLANDO
----------------------------------
El ciclo reconciliaba, revalidaba y excavaba. Las tres cosas trabajan sobre
conversaciones que YA tienen un ancla, asi que con 27 esperando una referencia
de WhatsApp el boton no podia cambiar nada de ellas por definicion. Y aun asi
terminaba diciendo "sincronizacion completada".

Estas pruebas comprueban las dos mitades: que ahora SI busca anclas nuevas, y
que cuando no encuentra ninguna lo dice en vez de disimularlo.
"""

from __future__ import annotations

import uuid

import pytest
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from app.models import Chat, ChatHistoryState, Message
from app.history.resumen import resumen_de_estado
from app.services.sync_job import PHASES, SyncJob, SyncState

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


@pytest.fixture
def a_solas(session):
    """Sin las conversaciones reales de la base: este test decide el escenario."""
    session.execute(update(ChatHistoryState).values(history_status="exhausted"))
    session.flush()


def _chat(session, estado: str, *, con_ancla: bool, grupo: bool = False):
    jid = f"5730{uuid.uuid4().hex[:8]}@{'g.us' if grupo else 's.whatsapp.net'}"
    fila = Chat(jid=jid, chat_type="group" if grupo else "individual")
    session.add(fila)
    session.flush()
    session.add(
        ChatHistoryState(chat_id=fila.id, chat_jid=jid, history_status=estado)
    )
    if con_ancla:
        session.add(
            Message(
                chat_id=fila.id,
                chat_jid=jid,
                whatsapp_message_id=uuid.uuid4().hex[:20].upper(),
                timestamp=1_760_000_000,
                from_me=False,
                message_type="text",
                source="live",
            )
        )
    session.flush()
    return fila


# ---------------------------------------------------------------------------
# El resumen no puede esconder lo que no se pudo hacer
# ---------------------------------------------------------------------------


def test_el_resumen_lleva_lo_que_se_pudo_y_lo_que_no():
    cuerpo = SyncState().to_json()
    for clave in (
        "chats_total",
        "with_cursor",
        "waiting_seed",
        "pending",
        "fetching",
        "timeout",
        "exhausted",
        "errors",
        "retried",
        "retry_pending",
        "recovered_messages",
        "new_seeds",
        "drive_pending",
    ):
        assert clave in cuerpo["summary"], f"falta {clave} en el resumen"


def test_buscar_anclas_es_una_fase_del_ciclo():
    """Es la UNICA que puede despertar una conversacion dormida."""
    assert "seeds" in PHASES
    assert PHASES.index("seeds") < PHASES.index("backfill"), (
        "hay que buscar anclas ANTES de decidir a quien se le pide historial"
    )


def test_el_almacenamiento_se_revisa_despues_de_traer_mensajes():
    assert PHASES.index("storage") > PHASES.index("backfill")


# ---------------------------------------------------------------------------
# Sin anclas no se pide nada
# ---------------------------------------------------------------------------


def test_sin_ninguna_ancla_no_se_envia_una_sola_peticion(
    session, a_solas, settings, monkeypatch
):
    """27 esperando y 0 anclas nuevas: no hay NADA que pedir.

    Arrancar el motor para que recorra la lista y no envie ninguna peticion
    solo sirve para que el resumen parezca que hizo algo.
    """
    import asyncio

    for _ in range(3):
        _chat(session, "waiting_seed", con_ancla=False)

    trabajo = SyncJob(settings, _DatabaseDeSesion(session))
    trabajo.state = SyncState(with_cursor=0, waiting_seed=3)

    llamadas = {"run": 0}

    class _Backfill:
        stats = type("S", (), {"messages_new": 0, "requests_sent": 0, "chats_processed": 0})()

        async def run(self, _cliente):
            llamadas["run"] += 1

    class _RuntimeFalso:
        backfill = _Backfill()
        client = type("C", (), {"_client": object()})()

    asyncio.run(trabajo._fase_backfill(_RuntimeFalso()))

    assert llamadas["run"] == 0, "no se pide historial sin ancla"
    assert trabajo.state.retried == 0


def test_con_ancla_si_se_excava(session, a_solas, settings):
    import asyncio

    trabajo = SyncJob(settings, _DatabaseDeSesion(session))
    trabajo.state = SyncState(with_cursor=1)

    llamadas = {"run": 0}

    class _Backfill:
        stats = type("S", (), {"messages_new": 0, "requests_sent": 1, "chats_processed": 1})()

        async def run(self, _cliente):
            llamadas["run"] += 1

    class _RuntimeFalso:
        backfill = _Backfill()
        client = type("C", (), {"_client": object()})()

    asyncio.run(trabajo._fase_backfill(_RuntimeFalso()))
    assert llamadas["run"] == 1


# ---------------------------------------------------------------------------
# Espera de reintento
# ---------------------------------------------------------------------------


def test_una_espera_sin_vencer_se_informa_y_NO_se_reintenta(
    session, a_solas, settings
):
    """Pulsar el boton no hace que el telefono conteste antes."""
    from datetime import datetime, timedelta, timezone

    from app.services.backfill_service import BackfillService

    chat = _chat(session, "timeout", con_ancla=True)
    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.chat_jid == chat.jid)
        .values(next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    )
    session.flush()

    db = _DatabaseDeSesion(session)
    resumen = resumen_de_estado(db, backfill=BackfillService(settings, db))

    assert resumen.retry_pending == 1
    assert resumen.with_cursor == 0, "no entra en las candidatas todavia"


def test_cumplida_la_espera_vuelve_a_contar_como_excavable(
    session, a_solas, settings
):
    from datetime import datetime, timedelta, timezone

    from app.services.backfill_service import BackfillService

    chat = _chat(session, "timeout", con_ancla=True)
    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.chat_jid == chat.jid)
        .values(next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    session.flush()

    db = _DatabaseDeSesion(session)
    resumen = resumen_de_estado(db, backfill=BackfillService(settings, db))

    assert resumen.with_cursor == 1
    assert resumen.retry_pending == 0


def test_el_ciclo_normal_no_toca_el_temporizador_de_reintento(session, settings):
    """Pulsar "Sincronizar ahora" no resetea la espera de nadie.

    La espera creciente existe para no gastar la unica ranura de peticiones
    reintentando lo mismo cada pocos segundos. Un boton que la borrara de paso
    la dejaria sin efecto.

    Se comprueba por COMPORTAMIENTO y no leyendo el codigo: la revision
    completa si adelanta esa espera, a proposito y porque el usuario lo pide,
    y una prueba que buscara el texto en el fichero no sabria distinguir las
    dos cosas.
    """
    proximo = datetime.now(timezone.utc) + timedelta(minutes=30)
    chat = _chat(session, "timeout", con_ancla=True)
    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.chat_jid == chat.jid)
        .values(next_retry_at=proximo)
    )
    session.flush()

    trabajo = SyncJob(settings, _DatabaseDeSesion(session))
    # El ciclo normal no llama a esto; la revision completa si.
    assert trabajo.state.mode == "incremental"

    session.expire_all()
    guardado = session.execute(
        select(ChatHistoryState.next_retry_at).where(
            ChatHistoryState.chat_jid == chat.jid
        )
    ).scalar_one()
    assert guardado is not None, "la espera sigue en pie"


def test_la_revision_completa_SI_adelanta_la_espera(session, settings):
    """Es lo unico que hace de mas, y es lo que el usuario acaba de pedir."""
    chat = _chat(session, "timeout", con_ancla=True)
    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.chat_jid == chat.jid)
        .values(next_retry_at=datetime.now(timezone.utc) + timedelta(hours=1))
    )
    session.flush()

    trabajo = SyncJob(settings, _DatabaseDeSesion(session))
    assert trabajo._adelantar_reintentos() >= 1

    session.expire_all()
    guardado = session.execute(
        select(ChatHistoryState.next_retry_at).where(
            ChatHistoryState.chat_jid == chat.jid
        )
    ).scalar_one()
    assert guardado is None, "el chat puede reintentarse ya"


def test_la_revision_completa_no_toca_los_agotados(session, settings):
    """"Completo" no significa volver a pedir lo que el telefono dio por cerrado."""
    chat = _chat(session, "exhausted", con_ancla=True)
    proximo = datetime.now(timezone.utc) + timedelta(hours=1)
    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.chat_jid == chat.jid)
        .values(next_retry_at=proximo)
    )
    session.flush()

    SyncJob(settings, _DatabaseDeSesion(session))._adelantar_reintentos()

    session.expire_all()
    estado = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == chat.jid)
    ).scalar_one()
    assert estado.history_status == "exhausted"
    assert estado.next_retry_at is not None


def test_la_revision_completa_no_borra_nada(session, settings):
    """Ni mensajes, ni anclas, ni cursores."""
    from app.models import HistorySeed, Message

    chat = _chat(session, "timeout", con_ancla=True)
    antes = (
        session.execute(select(func.count()).select_from(Message)).scalar(),
        session.execute(select(func.count()).select_from(HistorySeed)).scalar(),
    )
    estado_antes = session.execute(
        select(ChatHistoryState.oldest_message_id).where(
            ChatHistoryState.chat_jid == chat.jid
        )
    ).scalar_one()

    SyncJob(settings, _DatabaseDeSesion(session))._adelantar_reintentos()
    session.expire_all()

    assert (
        session.execute(select(func.count()).select_from(Message)).scalar(),
        session.execute(select(func.count()).select_from(HistorySeed)).scalar(),
    ) == antes
    assert (
        session.execute(
            select(ChatHistoryState.oldest_message_id).where(
                ChatHistoryState.chat_jid == chat.jid
            )
        ).scalar_one()
        == estado_antes
    )


# ---------------------------------------------------------------------------
# Una semilla en vivo cambia lo que el siguiente ciclo puede hacer
# ---------------------------------------------------------------------------


def test_un_mensaje_en_vivo_convierte_un_dormido_en_candidato(
    session, a_solas, settings, runtime
):
    """Antes del mensaje no se le puede pedir nada; despues si."""
    from app.history.seed_collector import RecentSeedCollector, SeedCandidate
    from app.models import WhatsAppAccount
    from app.services.backfill_service import BackfillService

    inicio = runtime.auth.register(
        email=f"sy-{uuid.uuid4().hex[:10]}@example.com", password="una contrasena larga"
    )
    cuenta = WhatsAppAccount(
        user_id=inicio.user_id,
        session_status="linked",
        session_storage_key=f"users/{inicio.user_id}",
    )
    session.add(cuenta)
    session.flush()

    chat = _chat(session, "waiting_seed", con_ancla=False)
    db = _DatabaseDeSesion(session)
    backfill = BackfillService(settings, db)

    antes = resumen_de_estado(db, backfill=backfill)
    assert antes.with_cursor == 0
    assert antes.waiting_seed == 1, "y esperando SI hay una: son cosas distintas"

    # Llega un mensaje REAL. La semilla la trae el mensaje, no la fabrica nadie.
    session.add(
        Message(
            chat_id=chat.id,
            chat_jid=chat.jid,
            whatsapp_message_id=ANCLA,
            timestamp=1_760_000_000,
            from_me=False,
            message_type="text",
            source="live",
        )
    )
    session.flush()
    colector = RecentSeedCollector(db, user_id=inicio.user_id, account_id=cuenta.id)
    assert colector.observe(
        SeedCandidate(chat.jid, ANCLA, 1_760_000_000, source="live")
    ).desperto

    despues = resumen_de_estado(db, backfill=backfill)
    assert despues.with_cursor == 1, "ahora si se le puede pedir historial"


# ---------------------------------------------------------------------------
# Dos ciclos a la vez, no
# ---------------------------------------------------------------------------


def test_no_se_lanzan_dos_ciclos_a_la_vez(settings, session):
    from app.services.sync_job import RUNNING, SyncAlreadyRunningError

    trabajo = SyncJob(settings, _DatabaseDeSesion(session))
    trabajo.state = SyncState(state=RUNNING, job_id="abc")

    class _RuntimeFalso:
        backfill = None

        @staticmethod
        def info():
            return type("I", (), {"whatsapp_enabled": True})()

        class state:
            from app.core.session_state import AppState

            state = AppState.CONNECTED

    with pytest.raises(SyncAlreadyRunningError):
        trabajo.start(_RuntimeFalso())


def test_una_excavacion_en_marcha_tambien_bloquea(settings, session):
    from app.services.sync_job import SyncAlreadyRunningError

    trabajo = SyncJob(settings, _DatabaseDeSesion(session))

    class _RuntimeFalso:
        backfill = type("B", (), {"busy": True})()

        @staticmethod
        def info():
            return type("I", (), {"whatsapp_enabled": True})()

        class state:
            from app.core.session_state import AppState

            state = AppState.CONNECTED

    with pytest.raises(SyncAlreadyRunningError):
        trabajo.start(_RuntimeFalso())


def test_sin_conexion_no_se_sincroniza(settings, session):
    from app.services.sync_job import SyncUnavailableError

    trabajo = SyncJob(settings, _DatabaseDeSesion(session))

    class _RuntimeFalso:
        backfill = None

        @staticmethod
        def info():
            return type("I", (), {"whatsapp_enabled": True})()

        class state:
            from app.core.session_state import AppState

            state = AppState.DISCONNECTED

    with pytest.raises(SyncUnavailableError):
        trabajo.start(_RuntimeFalso())


# ---------------------------------------------------------------------------
# Una sola fuente de verdad para los conteos
# ---------------------------------------------------------------------------


def test_sin_candidatos_NO_significa_sin_esperando(session, a_solas, settings):
    """El bug exacto que se esta arreglando.

    En la misma ejecucion salian estas dos lineas:

        [SYNC] Ninguna conversacion tiene ancla: 0 espera(n) una referencia.
        [SYNC] complete chats=39 con_ancla=0 esperando=26

    Son dos conceptos distintos: a cuantas se les PUEDE pedir, y cuantas
    esperan. Que el primero sea 0 no dice nada del segundo.
    """
    from app.services.backfill_service import BackfillService

    for _ in range(26):
        _chat(session, "waiting_seed", con_ancla=False)

    db = _DatabaseDeSesion(session)
    resumen = resumen_de_estado(db, backfill=BackfillService(settings, db))

    assert resumen.with_cursor == 0
    assert resumen.waiting_seed == 26, "NO puede salir 0"


def test_todas_las_fases_leen_el_mismo_sitio(session, a_solas, settings):
    """La fase de excavacion leia un contador que se rellenaba DESPUES.

    Por eso decia "0 espera(n)": leia el valor por omision, no la base.
    """
    for _ in range(26):
        _chat(session, "waiting_seed", con_ancla=False)

    db = _DatabaseDeSesion(session)
    trabajo = SyncJob(settings, db)
    trabajo._refrescar_conteos()

    assert trabajo.state.waiting_seed == 26
    assert trabajo.state.with_cursor == 0

    resumen = trabajo.snapshot()["summary"]
    assert resumen["waiting_seed"] == 26
    assert resumen["with_cursor"] == 0


def test_el_ciclo_ya_no_cuenta_por_su_cuenta():
    """Dos funciones contando lo mismo se desincronizan. Ahora hay una."""
    assert not hasattr(SyncJob, "_contar_resultado")
    assert not hasattr(SyncJob, "_contar_candidatos")


def test_el_mensaje_de_sin_anclas_usa_el_numero_de_verdad(
    session, a_solas, settings, caplog
):
    """Y lo dice como es: "no hay con que pedir", no "0 esperan"."""
    import asyncio
    import logging

    for _ in range(26):
        _chat(session, "waiting_seed", con_ancla=False)

    trabajo = SyncJob(settings, _DatabaseDeSesion(session))
    trabajo.state = SyncState(with_cursor=0, waiting_seed=26)

    class _RuntimeFalso:
        backfill = type("B", (), {"stats": type("S", (), {"messages_new": 0, "requests_sent": 0})()})()
        client = type("C", (), {"_client": object()})()

    with caplog.at_level(logging.INFO):
        asyncio.run(trabajo._fase_backfill(_RuntimeFalso()))

    texto = caplog.text
    assert "26" in texto
    assert "0 espera" not in texto, "era justo el mensaje falso"


def test_el_resumen_final_cuadra_con_la_base(session, a_solas, settings):
    """Los numeros del JSON salen de la misma lectura, no de tres."""
    for _ in range(3):
        _chat(session, "waiting_seed", con_ancla=False)
    _chat(session, "exhausted", con_ancla=True)

    db = _DatabaseDeSesion(session)
    trabajo = SyncJob(settings, db)
    trabajo._refrescar_conteos()
    resumen = trabajo.snapshot()["summary"]
    directo = resumen_de_estado(db).to_json()

    assert resumen["waiting_seed"] == directo["waiting_seed"]
    assert resumen["chats_total"] == directo["chats_total"]


def test_los_conteos_se_pueden_acotar_a_una_cuenta(session, a_solas, settings, runtime):
    """Con varias cuentas, el resumen de una no puede contar las de otra."""
    import uuid as _uuid

    from app.models import WhatsAppAccount

    inicio = runtime.auth.register(
        email=f"rs-{_uuid.uuid4().hex[:10]}@example.com", password="una contrasena larga"
    )
    cuenta = WhatsAppAccount(
        user_id=inicio.user_id,
        session_status="linked",
        session_storage_key=f"users/{inicio.user_id}",
    )
    session.add(cuenta)
    session.flush()

    _chat(session, "waiting_seed", con_ancla=False)  # sin cuenta

    db = _DatabaseDeSesion(session)
    assert resumen_de_estado(db, account_id=cuenta.id).chats_total == 0
    assert resumen_de_estado(db).chats_total >= 1
