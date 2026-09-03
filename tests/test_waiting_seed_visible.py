"""Un chat sin historial no puede esconderse detras de "sincronizado".

LA GARANTIA
-----------
"Sincronizacion terminada" a secas escondia treinta conversaciones que siguen
sin una sola linea de historial. Un chat que espera semilla:

* NO esta sincronizado
* NO esta agotado
* NO esta vacio
* y NO puede desaparecer del resumen ni del listado

Los tres controles (Isaac Virtual Tec, Angel Electrico, ubernel) tienen que
seguir visibles hasta que algo los recupere de verdad.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import COMPLETE_STATUSES, Chat, ChatHistoryState

FANTASMA = "99988811122@lid"


@pytest.fixture
def chat_fantasma(session):
    chat = Chat(jid=FANTASMA, chat_type="individual", name="Contacto fantasma")
    session.add(chat)
    session.flush()
    session.add(
        ChatHistoryState(
            chat_id=chat.id, chat_jid=FANTASMA, history_status="waiting_seed"
        )
    )
    session.flush()
    return chat


# ---------------------------------------------------------------------------
# El listado
# ---------------------------------------------------------------------------


def test_el_sidebar_sabe_que_espera_semilla(session, chat_fantasma):
    """Sin esto el frontend solo ve una vista previa vacia.

    Y entonces pinta "Mensaje no compatible", que dice algo falso: el chat no
    tiene un mensaje ilegible, tiene CERO mensajes.
    """
    from app.services import repository as repo

    fila = next(
        c for c in repo.list_chat_summaries(session, limit=1000) if c.jid == FANTASMA
    )
    assert fila.history_status == "waiting_seed"
    assert fila.message_count == 0


def test_la_api_lo_expone_en_la_fila(session, chat_fantasma):
    from app.api.serializers import chat_to_json
    from app.services import repository as repo

    fila = next(
        c for c in repo.list_chat_summaries(session, limit=1000) if c.jid == FANTASMA
    )
    cuerpo = chat_to_json(fila)

    assert cuerpo["history_status"] == "waiting_seed"
    assert cuerpo["waiting_seed"] is True
    assert cuerpo["history_complete"] is False
    assert cuerpo["message_count"] == 0


def test_un_chat_agotado_no_se_marca_como_pendiente(session):
    from app.api.serializers import chat_to_json
    from app.services import repository as repo

    jid = "99977766655@lid"
    chat = Chat(jid=jid, chat_type="individual")
    session.add(chat)
    session.flush()
    session.add(
        ChatHistoryState(chat_id=chat.id, chat_jid=jid, history_status="exhausted")
    )
    session.flush()

    fila = next(c for c in repo.list_chat_summaries(session, limit=1000) if c.jid == jid)
    cuerpo = chat_to_json(fila)
    assert cuerpo["waiting_seed"] is False
    assert cuerpo["history_complete"] is True


def test_no_desaparece_del_listado_por_tener_cero_mensajes(session, chat_fantasma):
    from app.services import repository as repo

    jids = {c.jid for c in repo.list_chat_summaries(session, limit=1000)}
    assert FANTASMA in jids


# ---------------------------------------------------------------------------
# El resumen de la sincronizacion
# ---------------------------------------------------------------------------


def test_el_resumen_cuenta_los_que_esperan_semilla(session, settings, chat_fantasma):
    from app.services.sync_job import SyncJob
    from tests.test_backfill_accounting import _DatabaseFalsa

    conteo = SyncJob(settings, _DatabaseFalsa(session))._contar_resultado()

    assert conteo["waiting_seed"] >= 1
    assert set(conteo) == {"synced", "waiting_seed", "timeouts", "errors", "pending"}


def test_esperar_semilla_no_cuenta_como_sincronizado(session, settings, chat_fantasma):
    from app.services.sync_job import SyncJob
    from tests.test_backfill_accounting import _DatabaseFalsa

    antes = SyncJob(settings, _DatabaseFalsa(session))._contar_resultado()

    # El mismo chat, ahora agotado de verdad.
    session.execute(
        ChatHistoryState.__table__.update()
        .where(ChatHistoryState.chat_jid == FANTASMA)
        .values(history_status="exhausted")
    )
    session.flush()
    despues = SyncJob(settings, _DatabaseFalsa(session))._contar_resultado()

    assert despues["synced"] == antes["synced"] + 1
    assert despues["waiting_seed"] == antes["waiting_seed"] - 1


def test_el_desglose_viaja_siempre_en_el_estado():
    """Tambien cuando el ciclo termina bien: es cuando mas se esconderia."""
    from app.services.sync_job import SyncState

    cuerpo = SyncState(state="complete", waiting_seed=30, synced=7, timeouts=2).to_json()

    assert cuerpo["result"]["waiting_seed"] == 30
    assert cuerpo["result"]["synced"] == 7
    assert cuerpo["result"]["timeouts"] == 2


def test_waiting_seed_nunca_se_convierte_en_completo():
    """La lista de estados completos no puede incluir los que esperan semilla."""
    from app.models import SEEDLESS_STATUSES

    assert not (set(SEEDLESS_STATUSES) & set(COMPLETE_STATUSES))


def test_el_recuento_sale_de_la_base_no_de_lo_que_creyo_el_backfill():
    """Un chat puede cambiar de estado por otra via (una semilla en vivo)."""
    import inspect

    from app.services.sync_job import SyncJob

    fuente = inspect.getsource(SyncJob._contar_resultado)
    assert "ChatHistoryState" in fuente
    assert "history_status" in fuente
