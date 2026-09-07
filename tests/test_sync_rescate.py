"""El boton tiene que poder MEDIRSE: que esperaba antes, que espera despues.

EL NUMERO QUE ENGANABA
----------------------
Una pasada real dejo esta linea::

    complete chats=41 con_ancla=0 esperando=33 anclas_nuevas=3410

No hay contradiccion --las 3410 referencias eran de verdad-- pero salian de
excavar las ocho conversaciones que YA funcionaban, no de desatascar ninguna
de las 33. El numero grande sugeria un avance que no existia.

Lo que contesta a "¿ha servido de algo pulsar?" es otra cosa: cuantas
esperaban antes, cuantas esperan despues, y cuantas cambiaron de estado. Eso
es lo que se fija aqui.

LA REPESCA
----------
Y la parte que de verdad rescata: volver a preguntar por CADA conversacion que
espera. Que no tuviera referencia la vez pasada no dice nada sobre si la tiene
ahora --puede haber llegado un mensaje en vivo, puede haberse resuelto un
alias, o puede que estuviera guardada desde el principio y nadie la mirara.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, update

from app.models import Chat, ChatHistoryState, HistorySeed
from app.services.sync_job import SyncJob, SyncState

ANCLA = "2A2B4C5F24E0F2E04AA2"
CUANDO = 1_788_655_482


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
    """Sin las conversaciones reales de la base: aqui se decide el escenario."""
    session.execute(update(ChatHistoryState).values(history_status="exhausted"))
    session.flush()


def _esperando(session, cuenta=None) -> Chat:
    jid = f"{uuid.uuid4().int % 10**15}@lid"
    fila = Chat(jid=jid, chat_type="individual", whatsapp_account_id=cuenta)
    session.add(fila)
    session.flush()
    session.add(
        ChatHistoryState(chat_id=fila.id, chat_jid=jid, history_status="waiting_seed")
    )
    session.flush()
    return fila


def _con_referencia(session, chat, cuenta, usuario):
    """Le deja guardada una referencia REAL, sin promoverla."""
    session.add(
        HistorySeed(
            user_id=usuario,
            whatsapp_account_id=cuenta,
            chat_id=chat.id,
            chat_jid=chat.jid,
            wa_msg_id=uuid.uuid4().hex[:20].upper(),
            timestamp=CUANDO,
            from_me=False,
            source="initial_bootstrap",
        )
    )
    session.flush()


def _colector(session, cuenta, usuario):
    from app.history.seed_collector import RecentSeedCollector

    colector = RecentSeedCollector(_DatabaseDeSesion(session))
    colector.account_id = cuenta
    colector.user_id = usuario
    return colector


def _estado(session, chat) -> str:
    return (
        session.execute(
            select(ChatHistoryState.history_status).where(
                ChatHistoryState.chat_id == chat.id
            )
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# La repesca (§8, §28)
# ---------------------------------------------------------------------------


def test_la_repesca_rescata_a_las_que_YA_tenian_referencia(session, cuenta, a_solas, settings):
    """El escenario de §28, con numeros.

    Diez conversaciones esperando. Cuatro tienen una referencia real guardada
    que nadie habia mirado. Se vuelve a preguntar por todas.
    """
    from app.models import WhatsAppAccount

    fila = session.execute(select(WhatsAppAccount)).scalars().first()
    cuenta, usuario = fila.id, fila.user_id

    chats = [_esperando(session, cuenta) for _ in range(10)]
    for chat in chats[:4]:
        _con_referencia(session, chat, cuenta, usuario)

    trabajo = SyncJob(settings, _DatabaseDeSesion(session))
    trabajo.state = SyncState()
    colector = _colector(session, cuenta, usuario)

    trabajo._repescar_esperando(colector)

    rescatadas = [c for c in chats if _estado(session, c) == "pending"]
    siguen = [c for c in chats if _estado(session, c) == "waiting_seed"]
    assert len(rescatadas) == 4
    assert len(siguen) == 6
    assert colector.metricas.despertados == 4


def test_LAS_QUE_NO_TIENEN_REFERENCIA_SIGUEN_ESPERANDO(session, cuenta, a_solas, settings):
    """La regla dura: sin referencia real no se promueve nada.

    Es lo que separa esto de inventarse un identificador. Un ancla fabricada
    no traeria historial: traeria una peticion que el telefono rechaza y una
    conversacion marcada como lista que no lo esta.
    """
    from app.models import WhatsAppAccount

    fila = session.execute(select(WhatsAppAccount)).scalars().first()
    chats = [_esperando(session, fila.id) for _ in range(5)]

    trabajo = SyncJob(settings, _DatabaseDeSesion(session))
    trabajo.state = SyncState()
    colector = _colector(session, fila.id, fila.user_id)

    trabajo._repescar_esperando(colector)

    assert all(_estado(session, c) == "waiting_seed" for c in chats)
    assert colector.metricas.despertados == 0


def test_repescar_sin_nadie_esperando_no_hace_nada(session, a_solas, settings):
    trabajo = SyncJob(settings, _DatabaseDeSesion(session))
    trabajo.state = SyncState()
    colector = _colector(session, None, None)

    trabajo._repescar_esperando(colector)

    assert colector.metricas.despertados == 0


def test_una_conversacion_que_falla_no_para_a_las_demas(session, cuenta, a_solas, settings):
    """Una sola conversacion problematica no puede tumbar la pasada entera."""
    from app.models import WhatsAppAccount

    fila = session.execute(select(WhatsAppAccount)).scalars().first()
    chats = [_esperando(session, fila.id) for _ in range(3)]
    for chat in chats:
        _con_referencia(session, chat, fila.id, fila.user_id)

    trabajo = SyncJob(settings, _DatabaseDeSesion(session))
    trabajo.state = SyncState()
    colector = _colector(session, fila.id, fila.user_id)

    original = colector.promote_waiting_chat
    fallado = {"veces": 0}

    def _a_veces_revienta(chat_id):
        if fallado["veces"] == 0:
            fallado["veces"] += 1
            raise RuntimeError("esta conversacion da problemas")
        return original(chat_id)

    colector.promote_waiting_chat = _a_veces_revienta
    trabajo._repescar_esperando(colector)

    assert sum(1 for c in chats if _estado(session, c) == "pending") == 2


# ---------------------------------------------------------------------------
# Lo que se puede medir (§12)
# ---------------------------------------------------------------------------


def test_el_estado_publica_el_bloque_para_medir_el_boton():
    estado = SyncState(
        waiting_before=32,
        waiting_after=31,
        promoted=1,
        new_seeds=3410,
        new_chats=0,
        recovered_messages=120,
        backfill_started=9,
    )

    bloque = estado.to_json()["recovery"]

    assert bloque == {
        "waiting_before": 32,
        "waiting_after": 31,
        "promoted": 1,
        "seeds_found": 3410,
        "new_chats": 0,
        "messages_added": 120,
        "backfill_started": 9,
        "blobs_rescanned": 0,
    }


def test_TRES_MIL_ANCLAS_CON_CERO_PROMOVIDAS_SE_VE_COMO_LO_QUE_ES():
    """El caso medido, contado sin adornos.

    3410 referencias insertadas y ni una conversacion desatascada. Las dos
    cosas son ciertas a la vez, y el bloque tiene que dejarlo claro.
    """
    bloque = SyncState(
        waiting_before=33, waiting_after=33, promoted=0, new_seeds=3410
    ).to_json()["recovery"]

    assert bloque["seeds_found"] == 3410
    assert bloque["promoted"] == 0
    assert bloque["waiting_before"] == bloque["waiting_after"] == 33
