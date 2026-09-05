"""Un chat sin ancla se ve, se dice la verdad sobre el, y despierta solo.

QUE ES UN CHAT SIN ANCLA
------------------------
``HISTORY_SYNC_ON_DEMAND`` necesita el id y la marca de tiempo de un mensaje
REAL para pedir "lo anterior a esto". Un chat que llego del bootstrap como
pura metadata no lo tiene, y en ``PeerDataOperationRequestType`` no hay
ninguna otra operacion de historial. Medido: 31 chats en esa situacion.

``waiting_seed`` NO es "sincronizado". Decir lo contrario era el fallo: el
frontend anunciaba historial completo sobre conversaciones que en el telefono
si tienen mensajes.

LO QUE SI DESBLOQUEA
--------------------
Un mensaje real, venga de donde venga. Y desde que los salientes se enrutan a
su destinatario, tambien vale uno que escriba el propio usuario: antes ese
mensaje caia en el chat propio y no sembraba nada.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Chat, ChatHistoryState
from app.services.seed_recovery import SeedRecovery
from tests.test_outgoing_routing import envuelto, entrante

VIEJO = "99944455566@lid"


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
def chat_dormido(session):
    """Un chat real sin un solo mensaje, marcado como esperando semilla."""
    chat = Chat(jid=VIEJO, chat_type="individual", name="Contacto viejo")
    session.add(chat)
    session.flush()
    session.add(
        ChatHistoryState(
            chat_id=chat.id, chat_jid=VIEJO, history_status="waiting_seed"
        )
    )
    session.flush()
    return chat


def _estado(session, jid: str) -> str:
    return session.execute(
        select(ChatHistoryState.history_status).where(ChatHistoryState.chat_jid == jid)
    ).scalar_one()


# ---------------------------------------------------------------------------
# Honestidad del estado
# ---------------------------------------------------------------------------


def test_waiting_seed_no_se_declara_completo(session, chat_dormido):
    from app.api.serializers import historia_to_json
    from app.services import repository as repo

    fila = repo.history_state_for(session, VIEJO)
    cuerpo = historia_to_json(fila, 0)

    assert cuerpo["status"] == "waiting_seed"
    assert cuerpo["complete"] is False, "un chat sin ancla NO es historial completo"
    assert cuerpo["can_dig"] is False
    assert cuerpo["waiting_seed"] is True


def test_un_chat_sin_mensajes_aparece_en_el_listado(session, chat_dormido):
    """No se oculta: si desaparece del sidebar, el usuario no sabe que existe."""
    from app.services import repository as repo

    listados = repo.list_chat_summaries(session, limit=1000)
    jids = {c.jid for c in listados}
    assert VIEJO in jids

    solo = next(c for c in listados if c.jid == VIEJO)
    assert solo.message_count == 0


# ---------------------------------------------------------------------------
# Siembra por actividad nueva
# ---------------------------------------------------------------------------


def _mensaje_live(session, chat_jid: str, raw: bytes, wamid: str):
    """Guarda un mensaje por el camino real, el de LiveMessageService."""
    import app.compat.protocol_flag as protocol_flag
    from pywhats.events import JID, Message

    from app.services.live_service import LiveMessageService
    from tests.test_live_types import FakeDatabase
    from tests.test_outgoing_routing import OWN_LID, OWN_PN

    original = protocol_flag.last_raw_message
    protocol_flag.last_raw_message = lambda: raw
    try:
        servicio = LiveMessageService(
            FakeDatabase(session), own_jid=OWN_PN, own_lid=OWN_LID
        )
        usuario, _, servidor = chat_jid.partition("@")
        return servicio.handle(
            Message(
                id=wamid,
                chat=JID(user=usuario, server=servidor),
                sender=JID(user=usuario, server=servidor),
                text="hola",
                timestamp=1_788_400_000,
                from_me=False,
                media=None,
                quoted=None,
            )
        )
    finally:
        protocol_flag.last_raw_message = original


def test_un_entrante_despierta_el_chat(session, chat_dormido):
    _mensaje_live(session, VIEJO, entrante(texto="hola"), "SEEDWAMID001")
    session.flush()

    informe = SeedRecovery(_DatabaseDeSesion(session)).seed_from_messages([VIEJO])
    assert informe.sembrados == 1
    assert _estado(session, VIEJO) == "pending"


def test_un_saliente_tambien_despierta_el_chat(session, chat_dormido):
    """Antes era imposible: el mensaje caia en el chat propio.

    Se manda con ``chat`` = nuestro propio identificador, que es como llega de
    verdad, y es el enrutado por ``destination_jid`` lo que lo lleva al chat
    dormido.
    """
    from tests.test_outgoing_routing import OWN_LID

    resultado = _mensaje_live(
        session, OWN_LID, envuelto(VIEJO, texto="hola"), "SEEDWAMID002"
    )
    session.flush()

    assert resultado["chat_jid"] == VIEJO, (
        "el saliente tiene que llegar al chat del destinatario para poder sembrarlo"
    )

    informe = SeedRecovery(_DatabaseDeSesion(session)).seed_from_messages(
        [resultado["chat_jid"]]
    )
    assert informe.sembrados == 1
    assert _estado(session, VIEJO) == "pending"


def test_un_mensaje_sin_id_real_no_siembra(session, chat_dormido):
    """Sin ID real de WhatsApp no hay ancla, y el chat se queda esperando."""
    from app.models import Message as MessageRow

    session.add(
        MessageRow(
            chat_id=chat_dormido.id,
            chat_jid=VIEJO,
            whatsapp_message_id=None,
            synthetic_identifier="sintetico-1",
            message_type="text",
            text="hola",
            timestamp=1_788_400_000,
            from_me=False,
            source="live",
        )
    )
    session.flush()

    informe = SeedRecovery(_DatabaseDeSesion(session)).seed_from_messages([VIEJO])
    assert informe.sembrados == 0
    assert _estado(session, VIEJO) == "waiting_seed"


# ---------------------------------------------------------------------------
# Revision manual
# ---------------------------------------------------------------------------


def test_la_revision_manual_no_inventa_cursor(session, chat_dormido, settings):
    """Sin ancla, la revision deja el chat como estaba y lo dice."""
    from app.services.history_recheck import HistoryRecheck

    revision = HistoryRecheck(_DatabaseDeSesion(session), settings)
    resultado = revision.recheck(chat_dormido.id)

    assert resultado is not None
    assert resultado.cursor_encontrado is False
    assert resultado.cursor_id is None
    assert resultado.estado == "waiting_seed"
    assert _estado(session, VIEJO) == "waiting_seed"


def test_la_revision_manual_encuentra_un_ancla_que_ya_estaba(
    session, chat_dormido, settings
):
    """Si el ancla aparecio por otra via, la revision lo detecta."""
    from app.services.history_recheck import HistoryRecheck

    _mensaje_live(session, VIEJO, entrante(texto="hola"), "SEEDWAMID003")
    session.flush()

    resultado = HistoryRecheck(_DatabaseDeSesion(session), settings).recheck(
        chat_dormido.id
    )
    assert resultado.cursor_encontrado is True
    assert resultado.cursor_id == "SEEDWAMID003"
    assert resultado.estado == "pending"
    assert _estado(session, VIEJO) == "pending"


def test_la_revision_busca_en_los_alias_del_contacto(session, chat_dormido, settings):
    """El ancla puede haber entrado por el otro identificador del contacto."""
    from app.models import Contact
    from app.services.history_recheck import HistoryRecheck

    telefono = "34600444555@s.whatsapp.net"
    session.add(Contact(jid=telefono, lid=VIEJO))
    session.add(Chat(jid=telefono, chat_type="individual"))
    session.flush()

    _mensaje_live(session, telefono, entrante(texto="hola"), "SEEDWAMID004")
    session.flush()

    resultado = HistoryRecheck(_DatabaseDeSesion(session), settings).recheck(
        chat_dormido.id
    )
    assert telefono in resultado.aliases
    assert resultado.cursor_encontrado is True
    assert resultado.cursor_id == "SEEDWAMID004"


def test_la_revision_de_un_chat_inexistente_devuelve_nada(session, settings):
    from app.services.history_recheck import HistoryRecheck

    assert HistoryRecheck(_DatabaseDeSesion(session), settings).recheck(-1) is None


# ---------------------------------------------------------------------------
# 'fetching' es transitorio: nadie puede quedarse ahi
# ---------------------------------------------------------------------------


def _atascar_en_fetching(session):
    from sqlalchemy import update

    from app.models import ChatHistoryState

    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.chat_jid == VIEJO)
        .values(history_status="fetching")
    )
    session.flush()


def test_un_fetching_atascado_CON_ancla_vuelve_a_pending(
    session, chat_dormido, settings
):
    """Se midio con "Tia Diana": el proceso murio entre pedir y responder.

    ``fetching`` lo marca el backfill justo antes de pedir. Si el proceso cae
    ahi, el chat se queda en ese estado para siempre Y ademas se vuelve
    inalcanzable: la cola de semillas salta los que ya se estan excavando.

    Con ancla vuelve a ``pending``, que es lo que era antes de pedir. Y el
    ancla NO se toca.
    """
    from app.services.maintenance_service import MaintenanceService, ReconcileReport

    _mensaje_live(session, VIEJO, entrante(texto="hola"), "3A1F8BDD4678EB6DE395")
    session.flush()
    _atascar_en_fetching(session)

    informe = MaintenanceService(
        _DatabaseDeSesion(session), settings
    ).reconcile_stuck_fetching(ReconcileReport())

    assert informe.stuck_fetching_reset >= 1
    assert _estado(session, VIEJO) == "pending", (
        "vuelve a lo que era antes de pedir; NO se marca agotado ni completo"
    )

    from app.history.cursor import get_valid_history_cursor

    cursor = get_valid_history_cursor(session, chat_jid=VIEJO)
    assert cursor is not None and cursor.wa_msg_id == "3A1F8BDD4678EB6DE395", (
        "rescatar un fetching no puede costarle el ancla al chat"
    )


def test_un_fetching_atascado_SIN_ancla_vuelve_a_waiting_seed(
    session, chat_dormido, settings
):
    """Sin ancla, ``pending`` seria mentira.

    Un chat en ``pending`` entra en la cola y se le intenta pedir historial.
    Si no tiene con que anclar, ese intento no puede hacerse: lo que le pasa
    es que espera una semilla, y eso es lo que tiene que decir.
    """
    from app.services.maintenance_service import MaintenanceService, ReconcileReport

    _atascar_en_fetching(session)

    MaintenanceService(_DatabaseDeSesion(session), settings).reconcile_stuck_fetching(
        ReconcileReport()
    )

    assert _estado(session, VIEJO) == "waiting_seed"


def test_rescatar_un_fetching_no_lo_declara_completo(session, chat_dormido, settings):
    """No se sabe si el telefono habria respondido: no se puede afirmar nada."""
    from sqlalchemy import update

    from app.models import COMPLETE_STATUSES, ChatHistoryState
    from app.services.maintenance_service import MaintenanceService, ReconcileReport

    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.chat_jid == VIEJO)
        .values(history_status="fetching")
    )
    session.flush()

    MaintenanceService(_DatabaseDeSesion(session), settings).reconcile_stuck_fetching(
        ReconcileReport()
    )
    assert _estado(session, VIEJO) not in COMPLETE_STATUSES


def test_un_timeout_no_es_historial_completo(session, chat_dormido):
    """Aunque hayan entrado mensajes en vivo durante la espera."""
    from sqlalchemy import update

    from app.api.serializers import historia_to_json
    from app.models import ChatHistoryState
    from app.services import repository as repo

    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.chat_jid == VIEJO)
        .values(history_status="timeout", last_error="sin respuesta ON_DEMAND")
    )
    session.flush()

    cuerpo = historia_to_json(repo.history_state_for(session, VIEJO), 21)

    assert cuerpo["status"] == "timeout"
    assert cuerpo["complete"] is False
    assert cuerpo["message_count"] == 21, (
        "cuantos mensajes hay y si el historial esta completo son cosas "
        "distintas: 21 mensajes en vivo no completan nada"
    )
