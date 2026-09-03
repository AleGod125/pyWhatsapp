"""Un chat que consigue su ancla se excava, y solo el.

EL FALLO
--------
"Juan Andrés" estaba en ``waiting_seed`` sin un solo mensaje. Llegaron tres
mensajes reales, la semilla aparecio y el estado paso a ``pending``... y ahi
se quedo. El backfill solo mira los chats al arrancar y en el ciclo manual, y
en el telefono ese chat tiene historial anterior que nadie pidio.

Aparecer la semilla y pedir historial eran dos cosas desconectadas. Esta cola
las une, sin convertirlo en un backfill de los cuarenta chats cada vez que
llega un mensaje: eso seria bombardear el telefono del usuario, que es quien
atiende las peticiones ``ON_DEMAND``.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.models import Chat, ChatHistoryState, Message as MessageRow
from app.services.seed_queue import SeedBackfillQueue

DORMIDO = "99933322211@lid"
OTRO = "99922211100@lid"


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


class _BackfillFalso:
    """Registra a quien se le pidio historial, sin hablar con nadie."""

    def __init__(self, con_cliente: bool = True):
        self._client = object() if con_cliente else None
        self.procesados: list[str] = []

    async def _process_chat(self, chat_id, chat_jid, max_rounds):
        self.procesados.append(chat_jid)


@pytest.fixture
def chats(session):
    for jid in (DORMIDO, OTRO):
        chat = Chat(jid=jid, chat_type="individual")
        session.add(chat)
        session.flush()
        session.add(
            ChatHistoryState(
                chat_id=chat.id, chat_jid=jid, history_status="waiting_seed"
            )
        )
    session.flush()


def _con_ancla(session, jid: str, wamid: str) -> None:
    """Le da al chat un mensaje con ID REAL de WhatsApp."""
    chat_id = session.execute(select(Chat.id).where(Chat.jid == jid)).scalar_one()
    session.add(
        MessageRow(
            chat_id=chat_id,
            chat_jid=jid,
            whatsapp_message_id=wamid,
            message_type="text",
            text="hola",
            timestamp=1_788_400_000,
            from_me=False,
            source="live",
        )
    )
    session.flush()


def _vaciar(cola: SeedBackfillQueue) -> None:
    """Ejecuta el vaciado sin esperar los segundos de agrupacion."""
    import app.services.seed_queue as modulo

    original = modulo.DEBOUNCE_SECONDS
    modulo.DEBOUNCE_SECONDS = 0.0
    try:
        asyncio.run(cola._vaciar())
    finally:
        modulo.DEBOUNCE_SECONDS = original


# ---------------------------------------------------------------------------
# Encolar
# ---------------------------------------------------------------------------


def test_encolar_no_bloquea_ni_lanza(session, chats):
    """Se llama desde el hilo del receptor: aqui no puede pasar nada lento."""
    cola = SeedBackfillQueue(_DatabaseDeSesion(session), _BackfillFalso())
    assert cola.enqueue([DORMIDO]) == [DORMIDO]


def test_el_mismo_chat_varias_veces_se_excava_una(session, chats):
    """Una rafaga de cinco mensajes en un chat es UNA excavacion."""
    backfill = _BackfillFalso()
    cola = SeedBackfillQueue(_DatabaseDeSesion(session), backfill)
    _con_ancla(session, DORMIDO, "QUEUEWAMID001")

    cola.enqueue([DORMIDO])
    cola.enqueue([DORMIDO])
    cola.enqueue([DORMIDO])
    _vaciar(cola)

    assert backfill.procesados == [DORMIDO]


def test_encolar_nada_no_hace_nada(session):
    cola = SeedBackfillQueue(_DatabaseDeSesion(session), _BackfillFalso())
    assert cola.enqueue([]) == []
    assert cola.enqueue(None) == []
    assert cola.enqueue([None, ""]) == []


# ---------------------------------------------------------------------------
# Excavar
# ---------------------------------------------------------------------------


def test_un_chat_con_ancla_se_excava(session, chats):
    backfill = _BackfillFalso()
    cola = SeedBackfillQueue(_DatabaseDeSesion(session), backfill)
    _con_ancla(session, DORMIDO, "QUEUEWAMID002")

    cola.enqueue([DORMIDO])
    _vaciar(cola)

    assert backfill.procesados == [DORMIDO]
    assert cola.excavados == 1


def test_un_chat_sin_ancla_no_se_le_pide_nada(session, chats):
    """Pedir sin ancla es lo que produce el ACK vacio. No se pide."""
    backfill = _BackfillFalso()
    cola = SeedBackfillQueue(_DatabaseDeSesion(session), backfill)

    cola.enqueue([DORMIDO])
    _vaciar(cola)

    assert backfill.procesados == []


def test_solo_se_excava_el_chat_encolado(session, chats):
    """NO es un backfill global: el otro chat dormido no se toca."""
    backfill = _BackfillFalso()
    cola = SeedBackfillQueue(_DatabaseDeSesion(session), backfill)
    _con_ancla(session, DORMIDO, "QUEUEWAMID003")
    _con_ancla(session, OTRO, "QUEUEWAMID004")

    cola.enqueue([DORMIDO])
    _vaciar(cola)

    assert backfill.procesados == [DORMIDO]
    assert OTRO not in backfill.procesados


def test_un_chat_agotado_no_se_vuelve_a_pedir(session, chats):
    backfill = _BackfillFalso()
    cola = SeedBackfillQueue(_DatabaseDeSesion(session), backfill)
    _con_ancla(session, DORMIDO, "QUEUEWAMID005")
    session.execute(
        ChatHistoryState.__table__.update()
        .where(ChatHistoryState.chat_jid == DORMIDO)
        .values(history_status="exhausted")
    )
    session.flush()

    cola.enqueue([DORMIDO])
    _vaciar(cola)

    assert backfill.procesados == []


def test_sin_cliente_conectado_no_se_pide_nada(session, chats):
    """Sin conexion no hay a quien pedirle: se deja para el ciclo normal."""
    backfill = _BackfillFalso(con_cliente=False)
    cola = SeedBackfillQueue(_DatabaseDeSesion(session), backfill)
    _con_ancla(session, DORMIDO, "QUEUEWAMID006")

    cola.enqueue([DORMIDO])
    _vaciar(cola)

    assert backfill.procesados == []


def test_un_chat_inexistente_no_revienta(session):
    backfill = _BackfillFalso()
    cola = SeedBackfillQueue(_DatabaseDeSesion(session), backfill)

    cola.enqueue(["99900000000@lid"])
    _vaciar(cola)

    assert backfill.procesados == []


def test_un_fallo_en_un_chat_no_arrastra_al_resto(session, chats):
    """La cola no puede pararse porque un chat de en medio falle."""
    _con_ancla(session, DORMIDO, "QUEUEWAMID007")
    _con_ancla(session, OTRO, "QUEUEWAMID008")

    class _Explosivo(_BackfillFalso):
        async def _process_chat(self, chat_id, chat_jid, max_rounds):
            self.procesados.append(chat_jid)
            if len(self.procesados) == 1:
                raise RuntimeError("fallo simulado")

    backfill = _Explosivo()
    cola = SeedBackfillQueue(_DatabaseDeSesion(session), backfill)
    cola.enqueue([DORMIDO, OTRO])
    _vaciar(cola)

    assert len(backfill.procesados) == 2


# ---------------------------------------------------------------------------
# Recuperacion de los que despertaron mientras esto estaba roto
# ---------------------------------------------------------------------------


def test_la_reconciliacion_despierta_un_chat_que_ya_tenia_ancla(session, chats):
    """No hace falta pedirle al usuario que vuelva a escribir en el chat."""
    from app.services.seed_recovery import SeedRecovery

    _con_ancla(session, DORMIDO, "QUEUEWAMID009")

    recuperacion = SeedRecovery(_DatabaseDeSesion(session))
    informe = recuperacion.seed_from_messages(recuperacion.pending_seedless())

    assert DORMIDO in informe.chats
    estado = session.execute(
        select(ChatHistoryState.history_status).where(
            ChatHistoryState.chat_jid == DORMIDO
        )
    ).scalar_one()
    assert estado == "pending", "pending, NO 'completo': aun queda por excavar"


def test_un_chat_sin_mensajes_sigue_esperando(session, chats):
    """No se inventa cursor: el que no tiene ancla se queda como esta."""
    from app.services.seed_recovery import SeedRecovery

    recuperacion = SeedRecovery(_DatabaseDeSesion(session))
    informe = recuperacion.seed_from_messages([OTRO])

    assert informe.sembrados == 0
    estado = session.execute(
        select(ChatHistoryState.history_status).where(ChatHistoryState.chat_jid == OTRO)
    ).scalar_one()
    assert estado == "waiting_seed"
