"""Recuperar historiales pendientes: global y por conversacion.

QUE ES ESTO
-----------
Hay chats que llegaron del pairing como pura metadata, sin un solo
identificador de mensaje. ``HISTORY_SYNC_ON_DEMAND`` va anclado, asi que sin
esa primera referencia no se puede pedir nada.

La sesion auxiliar intenta conseguirla. Si lo logra, se la entrega al motor de
siempre; si no, el chat sigue esperando y se puede reintentar.

LO QUE FIJAN ESTAS PRUEBAS
--------------------------
Sobre todo la frontera, porque esto vincula un dispositivo adicional a la
cuenta del usuario:

* el UNICO efecto sobre la base es el cursor del chat;
* "no se encontro" NO cambia el estado ni cuenta como error;
* no hay un segundo extractor: se reutiliza ``SeedQueue``;
* solo una recuperacion a la vez, y un chat no se intenta dos veces.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Chat, ChatHistoryState
from app.services.history_recovery import HistoryRecoveryService
from app.services.web_seed_provider import WebSeed

FANTASMA_A = "99955544433@lid"
FANTASMA_B = "99966677788@lid"
ID_REAL = "3A1F8BDD4678EB6DE395"


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


class _RuntimeFalso:
    def __init__(self):
        self.encolados: list[str] = []
        self.seed_queue = self

    def enqueue(self, jids):
        self.encolados.extend(jids)
        return list(jids)


@pytest.fixture
def fantasmas(session):
    creados = []
    for jid, nombre in ((FANTASMA_A, "Chat fantasma A"), (FANTASMA_B, "Chat fantasma B")):
        chat = Chat(jid=jid, chat_type="individual", name=nombre)
        session.add(chat)
        session.flush()
        session.add(
            ChatHistoryState(
                chat_id=chat.id, chat_jid=jid, history_status="waiting_seed"
            )
        )
        creados.append(chat)
    session.flush()
    return creados


@pytest.fixture
def servicio(settings, session):
    return HistoryRecoveryService(settings, _DatabaseDeSesion(session))


def _estado(session, jid: str) -> str:
    return session.execute(
        select(ChatHistoryState.history_status).where(ChatHistoryState.chat_jid == jid)
    ).scalar_one()


def _semilla(jid: str = FANTASMA_A) -> WebSeed:
    return WebSeed(
        remote_jid=jid, message_id=ID_REAL, from_me=False, timestamp=1_788_400_000
    )


# ---------------------------------------------------------------------------
# Que se intenta
# ---------------------------------------------------------------------------


def test_se_intentan_los_que_esperan_referencia(servicio, session, fantasmas):
    pendientes = servicio._pendientes(None)
    jids = {p["chat_jid"] for p in pendientes}
    assert FANTASMA_A in jids and FANTASMA_B in jids


def test_un_chat_con_historial_no_se_intenta(servicio, session, fantasmas):
    """Si ya tiene referencia, la sesion auxiliar no aporta nada."""
    session.execute(
        ChatHistoryState.__table__.update()
        .where(ChatHistoryState.chat_jid == FANTASMA_A)
        .values(history_status="exhausted")
    )
    session.flush()

    jids = {p["chat_jid"] for p in servicio._pendientes(None)}
    assert FANTASMA_A not in jids
    assert FANTASMA_B in jids


def test_se_puede_pedir_uno_solo(servicio, session, fantasmas):
    pendientes = servicio._pendientes(fantasmas[0].id)
    assert len(pendientes) == 1
    assert pendientes[0]["chat_jid"] == FANTASMA_A


def test_se_llevan_todos_los_alias(servicio, session, fantasmas):
    """Un contacto aparece por telefono y por LID: los dos identifican el chat."""
    from app.models import Contact

    session.add(Contact(jid="34600123456@s.whatsapp.net", lid=FANTASMA_A))
    session.flush()

    pendientes = servicio._pendientes(fantasmas[0].id)
    assert "34600123456@s.whatsapp.net" in pendientes[0]["aliases"]


# ---------------------------------------------------------------------------
# Cuando aparece una referencia
# ---------------------------------------------------------------------------


def test_una_referencia_valida_se_guarda_como_cursor(servicio, session, fantasmas):
    runtime = _RuntimeFalso()
    assert servicio._entregar(fantasmas[0].id, _semilla(), runtime) is True
    session.flush()

    fila = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == FANTASMA_A)
    ).scalar_one()
    assert fila.history_status == "pending"
    assert fila.oldest_message_id == ID_REAL
    assert fila.oldest_message_timestamp == 1_788_400_000
    assert fila.last_seed_attempt_result == "seed_found"


def test_se_usa_el_motor_existente(servicio, session, fantasmas):
    """No hay un segundo extractor: se encola y excava pywhats."""
    runtime = _RuntimeFalso()
    servicio._entregar(fantasmas[0].id, _semilla(), runtime)

    assert runtime.encolados == [FANTASMA_A]


def test_no_se_escriben_mensajes_ni_multimedia():
    """La frontera. Es lo que impide que esto crezca hasta ser otro backup."""
    import ast
    from pathlib import Path

    arbol = ast.parse(
        Path("app/services/history_recovery.py").read_text(encoding="utf-8")
    )
    nombres = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Attribute):
            nombres.append(nodo.attr)
        elif isinstance(nodo, ast.Name):
            nombres.append(nodo.id)

    for prohibido in ("Message", "MediaFile", "bulk_upsert_messages", "ingest_history_sync"):
        assert prohibido not in nombres, (
            f"la recuperacion no puede tocar {prohibido}: solo deja el cursor"
        )
    assert "ChatHistoryState" in nombres, "el cursor SI se escribe"


# ---------------------------------------------------------------------------
# Cuando NO aparece: sigue esperando
# ---------------------------------------------------------------------------


def test_no_encontrar_no_cambia_el_estado(servicio, session, fantasmas):
    """No es un error ni un final: el chat sigue pendiente."""
    servicio._anotar_intento(FANTASMA_A, "no_seed")
    session.flush()

    assert _estado(session, FANTASMA_A) == "waiting_seed"


def test_se_guarda_cuando_se_intento(servicio, session, fantasmas):
    """Para poder decirle al usuario cuando fue, en vez de dejarle adivinando."""
    servicio._anotar_intento(FANTASMA_A, "no_seed")
    session.flush()

    fila = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == FANTASMA_A)
    ).scalar_one()
    assert fila.last_seed_attempt_result == "no_seed"
    assert fila.last_seed_attempt_at is not None


def test_no_seed_sigue_siendo_reintentable(servicio, session, fantasmas):
    servicio._anotar_intento(FANTASMA_A, "no_seed")
    session.flush()

    jids = {p["chat_jid"] for p in servicio._pendientes(None)}
    assert FANTASMA_A in jids, "un intento fallido no lo saca de la lista"


# ---------------------------------------------------------------------------
# Concurrencia
# ---------------------------------------------------------------------------


def test_solo_una_recuperacion_a_la_vez(servicio, session, fantasmas):
    servicio._activo = "otro-trabajo"
    with pytest.raises(RuntimeError, match="ya hay una recuperacion"):
        servicio.start(_RuntimeFalso())


def test_un_chat_no_se_intenta_dos_veces(servicio, session, fantasmas):
    servicio._en_curso.add(fantasmas[0].id)
    with pytest.raises(RuntimeError, match="ya se esta recuperando"):
        servicio.start(_RuntimeFalso(), fantasmas[0].id)


def test_sin_nada_pendiente_termina_en_el_acto(servicio, session):
    """Se apunta a un chat inexistente a proposito.

    Sin ``chat_id`` recorreria los que de verdad esperan referencia en la base
    de pruebas, y eso levantaria el proceso auxiliar: una prueba no puede
    abrir una conexion a WhatsApp.
    """
    trabajo = servicio.start(_RuntimeFalso(), chat_id=-1)

    assert trabajo.state == "completed"
    assert trabajo.total == 0
    assert servicio.busy is False


def test_una_prueba_nunca_levanta_el_proceso_auxiliar(servicio, session, fantasmas):
    """El arranque con lista vacia no puede llegar a lanzar Node."""
    lanzado = []
    servicio._provider.run = lambda *a, **k: lanzado.append(True) or {}

    servicio.start(_RuntimeFalso(), chat_id=-1)
    assert lanzado == []


# ---------------------------------------------------------------------------
# El estado que ve el frontend
# ---------------------------------------------------------------------------


def test_el_progreso_trae_lo_que_el_frontend_necesita():
    from app.services.history_recovery import ChatProgress, RecoveryJob

    trabajo = RecoveryJob(
        job_id="abc123",
        total=30,
        state="running",
        processed=11,
        recovered=4,
        no_seed=6,
        errors=1,
        current=ChatProgress(13, "ubernel", "recovering_seed"),
    )
    cuerpo = trabajo.to_json()

    for clave in ("job_id", "state", "total", "processed", "recovered", "no_seed", "errors"):
        assert clave in cuerpo
    assert cuerpo["current_chat"] == {
        "id": 13,
        "name": "ubernel",
        "state": "recovering_seed",
    }


def test_el_qr_auxiliar_no_sale_en_el_json():
    """Un QR es una credencial de vinculacion: se sirve como imagen."""
    from app.services.history_recovery import RecoveryJob

    trabajo = RecoveryJob(job_id="abc", qr_required=True, qr_payload="2@SECRETO")
    cuerpo = trabajo.to_json()

    assert cuerpo["qr_required"] is True
    assert "2@SECRETO" not in str(cuerpo)
    assert "qr_payload" not in cuerpo


def test_los_estados_por_chat_estan_definidos():
    from app.services.history_recovery import ESTADOS_CHAT

    assert set(ESTADOS_CHAT) >= {
        "waiting_seed",
        "recovering_seed",
        "seed_found",
        "fetching_history",
        "no_seed",
        "error",
    }


def test_los_eventos_llegan_al_frontend():
    from app.api.routes import EVENT_NAMES

    for evento in (
        "history.recovery.started",
        "history.recovery.progress",
        "history.recovery.completed",
        "history.seed.found",
        "history.seed.not_found",
        "history.backfill.started",
    ):
        assert evento in EVENT_NAMES, f"falta {evento} en la traduccion a SSE"
