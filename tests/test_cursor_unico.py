"""El cursor de historial: UNA definicion, persistida, y que sobrevive.

EL BUG QUE SE ESTA COMPROBANDO
------------------------------
En la misma ejecucion aparecian estas dos cosas::

    CANARY: no hay ningun chat con cursor valido
    ... y despues el backfill encontraba uno y lo mandaba.

Eran dos definiciones distintas de "tiene cursor". Casi todas estas pruebas
comprueban que ya no pueden discrepar, y que un timeout o un reinicio no le
cuestan el ancla a nadie.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.history.cursor import (
    RETRY_BACKOFF_SECONDS,
    CursorInfo,
    anotar_intento_fallido,
    espera_cumplida,
    get_valid_history_cursor,
    persist_cursor,
    proxima_espera,
)
from app.models import Chat, ChatHistoryState, Contact, HistorySeed, Message

ANCLA = "3A1F8BDD4678EB6DE395"
ANTIGUA = "3A1F8BDD4678EB6DE100"


class _DatabaseDeSesion:
    """Reutiliza la sesion del test: nada se escribe fuera de la transaccion."""

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
def dueno(session, runtime):
    """Un usuario real con su cuenta. Las anclas pertenecen a alguien."""
    from app.models import WhatsAppAccount

    inicio = runtime.auth.register(
        email=f"cur-{uuid.uuid4().hex[:10]}@example.com", password="una contrasena larga"
    )
    cuenta = WhatsAppAccount(
        user_id=inicio.user_id,
        session_status="linked",
        session_storage_key=f"users/{inicio.user_id}",
    )
    session.add(cuenta)
    session.flush()
    return inicio.user_id, cuenta.id


@pytest.fixture
def a_solas(session):
    """Deja la base SIN otros chats, dentro de la transaccion del test.

    La suite corre contra la base real, que tiene conversaciones de verdad. Un
    test sobre "que elige el canary" no puede depender de ellas: cambiarian el
    resultado sin que el test hubiera cambiado.

    No se BORRA nada: se marcan como agotadas, que es como el motor las
    excluye de sus candidatas. Y todo se revierte al terminar el test.
    """
    session.execute(update(ChatHistoryState).values(history_status="exhausted"))
    session.flush()
    yield


@pytest.fixture
def chat(session):
    jid = f"5730{uuid.uuid4().hex[:8]}@s.whatsapp.net"
    fila = Chat(jid=jid, chat_type="individual")
    session.add(fila)
    session.flush()
    session.add(
        ChatHistoryState(chat_id=fila.id, chat_jid=jid, history_status="waiting_seed")
    )
    session.flush()
    return fila


def _mensaje(session, chat, wamid: str, ts: int, from_me: bool = False):
    session.add(
        Message(
            chat_id=chat.id,
            chat_jid=chat.jid,
            whatsapp_message_id=wamid,
            timestamp=ts,
            from_me=from_me,
            message_type="text",
            source="live",
        )
    )
    session.flush()


def _semilla(session, chat, dueno, wamid: str, ts: int, fuente: str = "live"):
    usuario, cuenta = dueno
    session.add(
        HistorySeed(
            user_id=usuario,
            whatsapp_account_id=cuenta,
            chat_id=chat.id,
            chat_jid=chat.jid,
            wa_msg_id=wamid,
            timestamp=ts,
            source=fuente,
        )
    )
    session.flush()


def _estado(session, chat) -> ChatHistoryState:
    return session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == chat.jid)
    ).scalar_one()


# ---------------------------------------------------------------------------
# UNA definicion
# ---------------------------------------------------------------------------


def test_sin_nada_no_hay_cursor(session, chat):
    """Y se dice tal cual. No se fabrica uno para tener algo que enviar."""
    assert get_valid_history_cursor(session, chat_jid=chat.jid) is None


def test_un_mensaje_real_es_cursor(session, chat):
    _mensaje(session, chat, ANCLA, 1_760_000_000)
    cursor = get_valid_history_cursor(session, chat_jid=chat.jid)
    assert cursor is not None
    assert cursor.wa_msg_id == ANCLA and cursor.source == "message"


def test_una_semilla_SIN_mensaje_tambien_es_cursor(session, chat, dueno):
    """El catalogo del Plan E cuenta.

    Antes el motor solo miraba ``messages``, asi que un ancla que existiera
    unicamente en ``history_seeds`` era invisible para quien pide.
    """
    _semilla(session, chat, dueno, ANCLA, 1_760_000_000)
    cursor = get_valid_history_cursor(session, chat_jid=chat.jid)
    assert cursor is not None and cursor.source == "seed"


def test_se_elige_el_ancla_mas_ANTIGUA(session, chat, dueno):
    """18:28, 18:20 y 17:00 -> el cursor es el de las 17:00.

    Se excava hacia atras: lo que queda por recuperar esta ANTES de la mas
    antigua. Partir de la mas reciente obligaria a recorrer otra vez lo que ya
    se tiene.
    """
    _mensaje(session, chat, ANCLA, 1_760_002_800)
    _mensaje(session, chat, "3A1F8BDD4678EB6DE200", 1_760_002_000)
    _semilla(session, chat, dueno, ANTIGUA, 1_760_000_000)

    cursor = get_valid_history_cursor(session, chat_jid=chat.jid)
    assert cursor.wa_msg_id == ANTIGUA and cursor.timestamp == 1_760_000_000


def test_un_id_fabricado_no_es_cursor(session, chat):
    """Un ancla inventada recibe ACK del servidor y despues silencio."""
    _mensaje(session, chat, "opaque-1", 1_760_000_000)
    assert get_valid_history_cursor(session, chat_jid=chat.jid) is None


def test_el_ancla_puede_estar_bajo_el_OTRO_identificador(session, chat):
    """Telefono y LID son el mismo contacto y la misma conversacion."""
    lid = f"9998{uuid.uuid4().hex[:8]}@lid"
    session.add(Contact(jid=chat.jid, lid=lid))
    session.add(
        Message(
            chat_id=chat.id,
            chat_jid=lid,
            whatsapp_message_id=ANCLA,
            timestamp=1_760_000_000,
            from_me=False,
            message_type="text",
            source="live",
        )
    )
    session.flush()

    cursor = get_valid_history_cursor(session, chat_jid=chat.jid)
    assert cursor is not None and cursor.wa_msg_id == ANCLA


def test_canary_y_backfill_ven_EXACTAMENTE_lo_mismo(session, a_solas, chat, settings):
    """La prueba del bug original, con el mismo escenario que lo produjo.

    Un chat individual con UN solo mensaje: la excavacion lo aceptaba y el
    canary lo descartaba por su filtro de ">=2 mensajes", y despues informaba
    de ello como "no hay ningun chat con cursor valido".
    """
    from app.services.backfill_service import BackfillService

    _mensaje(session, chat, ANCLA, 1_760_000_000)
    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.chat_jid == chat.jid)
        .values(history_status="pending")
    )
    session.flush()

    servicio = BackfillService(settings, _DatabaseDeSesion(session))
    con_cursor = {(cid, jid) for cid, jid, _ in servicio.chats_with_cursor()}
    elegido = servicio.pick_canary()

    assert (chat.id, chat.jid) in con_cursor, "la excavacion si lo ve"
    assert elegido is not None, "y el canary tambien tiene que verlo"

    # Y no solo "alguno": el cursor que elige cada uno es el MISMO.
    del_canary = get_valid_history_cursor(session, chat_id=elegido[0])
    del_backfill = next(c for cid, _, c in servicio.chats_with_cursor() if cid == elegido[0])
    assert del_canary.wa_msg_id == del_backfill.wa_msg_id
    assert del_canary.timestamp == del_backfill.timestamp


def test_el_canary_prefiere_el_chat_mas_facil_de_verificar(session, a_solas, settings):
    """Individual y con varios mensajes. Preferencia, NO requisito."""
    from app.services.backfill_service import BackfillService

    grande = Chat(jid=f"5731{uuid.uuid4().hex[:8]}@s.whatsapp.net", chat_type="individual")
    pequeno = Chat(jid=f"5732{uuid.uuid4().hex[:8]}@s.whatsapp.net", chat_type="individual")
    session.add_all([grande, pequeno])
    session.flush()
    for fila in (grande, pequeno):
        session.add(
            ChatHistoryState(chat_id=fila.id, chat_jid=fila.jid, history_status="pending")
        )
    session.flush()
    _mensaje(session, pequeno, "3A1F8BDD4678EB6DE900", 1_760_000_000)
    for i in range(3):
        _mensaje(session, grande, f"3A1F8BDD4678EB6DE8{i:02d}", 1_760_000_000 + i)

    elegido = BackfillService(settings, _DatabaseDeSesion(session)).pick_canary()
    assert elegido is not None and elegido[1] == grande.jid


# ---------------------------------------------------------------------------
# Persistencia: el cursor sobrevive al proceso
# ---------------------------------------------------------------------------


def test_el_cursor_se_guarda_en_el_estado_del_chat(session, chat):
    persist_cursor(
        session, chat.jid, CursorInfo(ANCLA, 1_760_000_000, True, source="live")
    )
    session.flush()

    estado = _estado(session, chat)
    assert estado.oldest_message_id == ANCLA
    assert estado.oldest_message_timestamp == 1_760_000_000
    assert estado.oldest_from_me is True, (
        "from_me viaja en la peticion; sin guardarlo salia un False por defecto"
    )


def test_el_cursor_sobrevive_a_un_reinicio(session, chat):
    """Sin objetos en memoria: solo lo que quedo en PostgreSQL.

    Se borran el mensaje y la semilla que lo originaron y se comprueba que el
    cursor persistido sigue sirviendo. Es lo que pasa cuando ``service.py``
    arranca de cero: no hay runtime del que recalcularlo.
    """
    from sqlalchemy import delete

    _mensaje(session, chat, ANCLA, 1_760_000_000)
    cursor = get_valid_history_cursor(session, chat_jid=chat.jid)
    persist_cursor(session, chat.jid, cursor)
    session.flush()

    session.execute(delete(Message).where(Message.chat_jid == chat.jid))
    session.flush()
    session.expire_all()

    recuperado = get_valid_history_cursor(session, chat_jid=chat.jid)
    assert recuperado is not None
    assert recuperado.wa_msg_id == ANCLA and recuperado.source == "state"


def test_la_semilla_en_vivo_se_persiste_ANTES_de_cambiar_de_estado(session, chat):
    """El orden importa.

    Si el proceso muere entre las dos cosas, es preferible un chat que sigue
    esperando con su ancla ya guardada —recuperable— a uno marcado como listo
    para excavar sin nada con que hacerlo.
    """
    import inspect

    from app.history.seed_collector import RecentSeedCollector

    fuente = inspect.getsource(RecentSeedCollector.promote_waiting_chat)
    assert fuente.index("persist_cursor") < fuente.index('history_status = "pending"')


def test_una_semilla_en_vivo_deja_el_cursor_completo(session, chat, dueno):
    from app.history.seed_collector import RecentSeedCollector, SeedCandidate

    colector = RecentSeedCollector(
        _DatabaseDeSesion(session), user_id=dueno[0], account_id=dueno[1]
    )
    colector.observe(
        SeedCandidate(chat.jid, ANCLA, 1_760_000_000, from_me=True, source="live")
    )
    session.flush()

    estado = _estado(session, chat)
    assert estado.history_status == "pending"
    assert estado.oldest_message_id == ANCLA
    assert estado.oldest_from_me is True
    assert estado.cursor_source in ("seed", "message")


# ---------------------------------------------------------------------------
# Timeout: cambia el estado, NO el ancla
# ---------------------------------------------------------------------------


def test_un_timeout_NO_borra_el_cursor(session, chat, settings):
    """Un timeout dice que el telefono no contesto, no que el ancla sea mala."""
    from app.services.backfill_service import BackfillService

    _mensaje(session, chat, ANCLA, 1_760_000_000)
    cursor = get_valid_history_cursor(session, chat_jid=chat.jid)
    persist_cursor(session, chat.jid, cursor)
    session.flush()

    servicio = BackfillService(settings, _DatabaseDeSesion(session))
    servicio._set_status(chat.jid, "timeout", "sin respuesta ON_DEMAND")
    servicio._anotar_reintento(chat.jid)
    session.flush()

    estado = _estado(session, chat)
    assert estado.history_status == "timeout"
    assert estado.oldest_message_id == ANCLA
    assert estado.oldest_message_timestamp == 1_760_000_000
    assert get_valid_history_cursor(session, chat_jid=chat.jid) is not None


def test_un_timeout_solo_cambia_estado_intentos_y_espera(session, chat):
    _mensaje(session, chat, ANCLA, 1_760_000_000)
    persist_cursor(session, chat.jid, get_valid_history_cursor(session, chat_jid=chat.jid))
    session.flush()

    intento, proximo = anotar_intento_fallido(session, chat.jid)
    session.flush()

    estado = _estado(session, chat)
    assert intento == 1 and estado.attempt_count == 1
    assert estado.last_attempt_at is not None
    assert estado.next_retry_at == proximo
    assert estado.oldest_message_id == ANCLA


def test_la_espera_entre_reintentos_CRECE(session, chat):
    """Insistir cada minuto no hace que el telefono conteste.

    Y si consume la unica ranura de peticiones, que es de una en una.
    """
    esperas = [proxima_espera(n) for n in range(1, 6)]
    assert esperas[:4] == list(RETRY_BACKOFF_SECONDS)
    assert esperas == sorted(esperas), "nunca decrece"
    assert esperas[-1] == RETRY_BACKOFF_SECONDS[-1], "y se estabiliza en el tope"


def test_un_chat_en_espera_no_se_reintenta_todavia(session, a_solas, chat, settings):
    from app.services.backfill_service import BackfillService

    _mensaje(session, chat, ANCLA, 1_760_000_000)
    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.chat_jid == chat.jid)
        .values(
            history_status="timeout",
            next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )
    session.flush()

    servicio = BackfillService(settings, _DatabaseDeSesion(session))
    assert chat.jid not in {j for _, j in servicio.chats_to_process()}


def test_cumplida_la_espera_vuelve_a_ser_candidato(session, a_solas, chat, settings):
    from app.services.backfill_service import BackfillService

    _mensaje(session, chat, ANCLA, 1_760_000_000)
    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.chat_jid == chat.jid)
        .values(
            history_status="timeout",
            next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    )
    session.flush()

    servicio = BackfillService(settings, _DatabaseDeSesion(session))
    assert chat.jid in {j for _, j in servicio.chats_to_process()}


def test_sin_espera_anotada_se_puede_reintentar(session):
    assert espera_cumplida(None) is True


# ---------------------------------------------------------------------------
# Reconciliacion: el estado no puede mentir
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("estado_falso", ["pending", "timeout"])
def test_un_estado_que_promete_ancla_sin_tenerla_se_corrige(
    session, chat, settings, estado_falso
):
    """``pending`` sin ancla se encola y descubre alli que no hay con que.

    ``timeout`` sin ancla es peor: dice que se pidio algo que no se pudo
    pedir.
    """
    from app.services.maintenance_service import MaintenanceService, ReconcileReport

    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.chat_jid == chat.jid)
        .values(history_status=estado_falso)
    )
    session.flush()

    MaintenanceService(
        _DatabaseDeSesion(session), settings
    ).reconcile_cursor_coherence(ReconcileReport())

    assert _estado(session, chat).history_status == "waiting_seed"


def test_un_pending_CON_ancla_no_se_toca(session, chat, settings):
    from app.services.maintenance_service import MaintenanceService, ReconcileReport

    _mensaje(session, chat, ANCLA, 1_760_000_000)
    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.chat_jid == chat.jid)
        .values(history_status="pending")
    )
    session.flush()

    MaintenanceService(
        _DatabaseDeSesion(session), settings
    ).reconcile_cursor_coherence(ReconcileReport())

    assert _estado(session, chat).history_status == "pending"


def test_un_chat_con_semilla_no_se_degrada_a_waiting_seed(session, chat, dueno, settings):
    """Tiene cero mensajes y aun asi tiene ancla: el catalogo cuenta.

    Degradarlo le quitaria lo unico con lo que se puede pedir su historial.
    """
    from app.services.seed_recovery import SeedRecovery

    _semilla(session, chat, dueno, ANCLA, 1_760_000_000, fuente="blob_scan")
    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.chat_jid == chat.jid)
        .values(history_status="pending")
    )
    session.flush()

    SeedRecovery(_DatabaseDeSesion(session)).classify()
    assert _estado(session, chat).history_status == "pending"


# ---------------------------------------------------------------------------
# El ciclo completo: live -> pending -> timeout -> reinicio -> reintento
# ---------------------------------------------------------------------------


def test_live_luego_timeout_luego_reinicio_y_sigue_habiendo_ancla(
    session, a_solas, chat, dueno, settings
):
    """El recorrido entero, tal y como le paso a la conversacion real."""
    from app.history.seed_collector import RecentSeedCollector, SeedCandidate
    from app.services.backfill_service import BackfillService

    db = _DatabaseDeSesion(session)

    # 1. Llega un mensaje REAL. Esa es la semilla; no la fabrica nadie.
    _mensaje(session, chat, ANCLA, 1_760_000_000)
    colector = RecentSeedCollector(db, user_id=dueno[0], account_id=dueno[1])
    assert colector.observe(
        SeedCandidate(chat.jid, ANCLA, 1_760_000_000, source="live")
    ).desperto
    session.flush()
    assert _estado(session, chat).history_status == "pending"

    # 2. Se pide y no llega respuesta.
    servicio = BackfillService(settings, db)
    servicio._set_status(chat.jid, "timeout", "sin respuesta ON_DEMAND")
    servicio._anotar_reintento(chat.jid)
    session.flush()
    assert _estado(session, chat).history_status == "timeout"

    # 3. Reinicio: se olvida todo lo que hubiera en memoria.
    session.expire_all()
    del colector, servicio

    # 4. El ancla sigue ahi, y el chat sigue siendo reintentable.
    estado = _estado(session, chat)
    assert estado.oldest_message_id == ANCLA
    assert get_valid_history_cursor(session, chat_jid=chat.jid) is not None
    assert estado.attempt_count == 1 and estado.next_retry_at is not None

    # 5. Cumplida la espera, vuelve a la cola. Con el MISMO cursor.
    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.chat_jid == chat.jid)
        .values(next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    session.flush()
    nuevo = BackfillService(settings, _DatabaseDeSesion(session))
    assert chat.jid in {j for _, j in nuevo.chats_to_process()}
    assert next(c for _, j, c in nuevo.chats_with_cursor() if j == chat.jid).wa_msg_id == ANCLA
