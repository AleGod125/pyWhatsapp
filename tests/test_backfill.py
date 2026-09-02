"""Construccion de la peticion HISTORY_SYNC_ON_DEMAND.

Lo que se comprueba aqui es que el mensaje que sale por el cable es
exactamente el que espera el protocolo. Es critico porque pywhats no define
ese campo: si el descriptor propio estuviera mal, el servidor recibiria basura
y responderia con un ACK vacio, que es indistinguible de un timeout.
"""

from __future__ import annotations

import pytest

from app.backfill_service import (
    PEER_DATA_OPERATION_REQUEST_MESSAGE,
    build_on_demand_message,
)

CHAT = "34600111222@s.whatsapp.net"
CURSOR = "3EB0C767D82B1B4459F1"


def _decode(message):
    """Reparsea con NUESTRO descriptor lo que produjo el de pywhats."""
    from app.proto import OnDemandMessage

    decoded = OnDemandMessage()
    decoded.ParseFromString(message.SerializeToString())
    return decoded


def test_el_campo_desconocido_sobrevive_a_pywhats():
    """pywhats no define el campo 16; protobuf debe conservarlo igualmente.

    Es la pieza que hace viable todo esto sin parchear el paquete.
    """
    message = build_on_demand_message(
        chat_jid=CHAT,
        oldest_message_id=CURSOR,
        oldest_from_me=False,
        oldest_timestamp=1_700_000_000,
        count=50,
    )
    request = (
        _decode(message)
        .protocolMessage.peerDataOperationRequestMessage.historySyncOnDemandRequest
    )
    assert request.chatJID == CHAT
    assert request.oldestMsgID == CURSOR
    assert request.onDemandMsgCount == 50


def test_usa_el_tipo_de_protocolo_correcto():
    message = build_on_demand_message(
        chat_jid=CHAT, oldest_message_id=CURSOR, oldest_from_me=False,
        oldest_timestamp=1_700_000_000, count=50,
    )
    decoded = _decode(message)
    assert decoded.protocolMessage.type == PEER_DATA_OPERATION_REQUEST_MESSAGE == 16
    # HISTORY_SYNC_ON_DEMAND = 3 en PeerDataOperationRequestType.
    assert decoded.protocolMessage.peerDataOperationRequestMessage.peerDataOperationRequestType == 3


def test_el_timestamp_va_en_segundos():
    """El ancla va en SEGUNDOS, pese a que el campo se llame "...MS".

    Lo establece la bitacora de la implementacion anterior, que si recupero
    historial real. Enviar milisegundos coloca el ancla ~56.000 anos en el
    futuro: el telefono no encuentra nada anterior y devuelve un blob vacio,
    de modo que la stanza se confirma con ACK y nunca llega respuesta. Es
    justo el sintoma que se observo (8 peticiones, 6 timeouts, 0 mensajes).
    """
    message = build_on_demand_message(
        chat_jid=CHAT, oldest_message_id=CURSOR, oldest_from_me=False,
        oldest_timestamp=1_700_000_000, count=50,
    )
    request = (
        _decode(message)
        .protocolMessage.peerDataOperationRequestMessage.historySyncOnDemandRequest
    )
    assert request.oldestMsgTimestampMS == 1_700_000_000, "debe ir en segundos"
    assert request.oldestMsgTimestampMS != 1_700_000_000_000, "no multiplicar por 1000"


def test_conserva_from_me_del_ancla():
    """``oldestMsgFromMe`` describe el mensaje ancla, no quien pide."""
    message = build_on_demand_message(
        chat_jid=CHAT, oldest_message_id=CURSOR, oldest_from_me=True,
        oldest_timestamp=1_700_000_000, count=50,
    )
    request = (
        _decode(message)
        .protocolMessage.peerDataOperationRequestMessage.historySyncOnDemandRequest
    )
    assert request.oldestMsgFromMe is True


def test_el_lid_de_cuenta_es_opcional():
    sin_lid = build_on_demand_message(
        chat_jid=CHAT, oldest_message_id=CURSOR, oldest_from_me=False,
        oldest_timestamp=1_700_000_000, count=50,
    )
    assert not _decode(sin_lid).protocolMessage.peerDataOperationRequestMessage \
        .historySyncOnDemandRequest.accountLid

    con_lid = build_on_demand_message(
        chat_jid=CHAT, oldest_message_id=CURSOR, oldest_from_me=False,
        oldest_timestamp=1_700_000_000, count=50, account_lid="8653114@lid",
    )
    assert _decode(con_lid).protocolMessage.peerDataOperationRequestMessage \
        .historySyncOnDemandRequest.accountLid == "8653114@lid"


def test_el_ancla_nunca_es_sintetica():
    """Defensa del bug historico: un id 'opaque-' no debe llegar al servidor.

    El cursor lo elige ``get_oldest_valid_history_cursor``, que ya los filtra;
    esto documenta la invariante desde el lado del emisor.
    """
    from app.repository import is_valid_history_cursor_id

    assert is_valid_history_cursor_id(CURSOR)
    assert not is_valid_history_cursor_id("opaque-deadbeef")


# ---------------------------------------------------------------------------
# Correlacion de respuestas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_la_respuesta_despierta_a_su_chat(settings, database):
    """Un History Sync que contiene el chat esperado debe desbloquear la espera."""
    from app.backfill_service import BackfillService, _Pending
    from app.compat.history_compat import FullHistorySync, HistoryConversation

    service = BackfillService(settings, database)
    pending = _Pending(chat_jid=CHAT)
    service._pending[CHAT] = pending

    service.notify_history(
        FullHistorySync(
            sync_type="ON_DEMAND",
            chunk_order=0,
            progress=0,
            conversations=[
                HistoryConversation(
                    jid=CHAT, name=None, last_message_timestamp=None,
                    unread_count=None, messages=[(b"x", 1), (b"y", 2)],
                )
            ],
            pushnames=[],
        )
    )
    assert pending.event.is_set()
    assert pending.messages == 2


@pytest.mark.asyncio
async def test_otro_chat_no_despierta_la_espera(settings, database):
    """Un blob de otra conversacion no debe darse por respuesta nuestra."""
    from app.backfill_service import BackfillService, _Pending
    from app.compat.history_compat import FullHistorySync, HistoryConversation

    service = BackfillService(settings, database)
    pending = _Pending(chat_jid=CHAT)
    service._pending[CHAT] = pending

    service.notify_history(
        FullHistorySync(
            sync_type="ON_DEMAND", chunk_order=0, progress=0,
            conversations=[
                HistoryConversation(
                    jid="99999999999@s.whatsapp.net", name=None,
                    last_message_timestamp=None, unread_count=None,
                    messages=[(b"x", 1)],
                )
            ],
            pushnames=[],
        )
    )
    assert not pending.event.is_set()
