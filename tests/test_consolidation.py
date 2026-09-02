"""Regresiones de la fase de consolidacion.

Cubren los fallos concretos que la auditoria del 2026-09-02 dejo demostrados:

* 75 Peer Data Operations guardadas como mensajes del chat propio;
* el canary eligiendo nuestra propia cuenta y acabando en ACK + timeout;
* fechas desordenadas al cargar mensajes anteriores.
"""

from __future__ import annotations

import pytest

from app.message_classifier import (
    MessageClass,
    classify_message_bytes,
    is_internal,
    is_visible,
)

# ---------------------------------------------------------------------------
# Utillaje: construir Message E2E reales con el protobuf de pywhats
# ---------------------------------------------------------------------------


def field_key(number: int, wire_type: int = 2) -> bytes:
    """Clave de campo protobuf como varint.

    Los campos por encima del 15 no caben en un byte, asi que hay que
    codificarlos de verdad en vez de hacer ``bytes([(n << 3) | 2])``.
    """
    key = (number << 3) | wire_type
    out = bytearray()
    while True:
        byte = key & 0x7F
        key >>= 7
        out.append(byte | (0x80 if key else 0))
        if not key:
            return bytes(out)


def message_with_text(text: str = "hola") -> bytes:
    from pywhats.proto import Message

    proto = Message()
    proto.conversation = text
    return proto.SerializeToString()


def message_with_image() -> bytes:
    from pywhats.proto import Message

    proto = Message()
    proto.image_message.mimetype = "image/jpeg"
    proto.image_message.file_length = 1234
    return proto.SerializeToString()


def peer_data_operation_request() -> bytes:
    """Exactamente la peticion ON_DEMAND que emitimos nosotros."""
    from app.backfill_service import build_on_demand_message

    message = build_on_demand_message(
        chat_jid="34600111222@s.whatsapp.net",
        oldest_message_id="3EB0C767D82B1B",
        oldest_from_me=False,
        oldest_timestamp=1_700_000_000,
        count=50,
    )
    return message.SerializeToString()


def history_sync_notification() -> bytes:
    from pywhats.proto import Message, ProtocolMessage

    proto = Message()
    proto.protocol_message.type = ProtocolMessage.Type.HISTORY_SYNC_NOTIFICATION
    proto.protocol_message.history_sync_notification.direct_path = "/v/t62.x"
    return proto.SerializeToString()


def app_state_key_share() -> bytes:
    from pywhats.proto import Message, ProtocolMessage

    proto = Message()
    proto.protocol_message.type = ProtocolMessage.Type.APP_STATE_SYNC_KEY_SHARE
    return proto.SerializeToString()


def sender_key_distribution() -> bytes:
    from pywhats.proto import Message

    proto = Message()
    proto.sender_key_distribution_message.group_id = "123@g.us"
    return proto.SerializeToString()


# ---------------------------------------------------------------------------
# TEST B / C / E -- el protocolo interno no es un mensaje
# ---------------------------------------------------------------------------


def test_B_peer_data_operation_no_es_mensaje():
    """La causa exacta de las 75 filas del chat propio."""
    clase = classify_message_bytes(peer_data_operation_request())
    assert clase is MessageClass.PROTOCOL_INTERNAL
    assert is_internal(clase)
    assert not is_visible(clase)


def test_C_history_sync_notification_no_es_mensaje():
    clase = classify_message_bytes(history_sync_notification())
    assert clase is MessageClass.PROTOCOL_INTERNAL
    assert is_internal(clase)


def test_E_app_state_y_sender_key_son_internos():
    assert is_internal(classify_message_bytes(app_state_key_share()))
    assert classify_message_bytes(sender_key_distribution()) is MessageClass.SIGNAL_CONTROL


# ---------------------------------------------------------------------------
# TEST D -- lo visible se conserva, incluso sin texto
# ---------------------------------------------------------------------------


def test_D_texto_e_imagen_son_visibles():
    assert classify_message_bytes(message_with_text()) is MessageClass.VISIBLE_TEXT
    assert classify_message_bytes(message_with_image()) is MessageClass.VISIBLE_MEDIA


def test_D_un_tipo_desconocido_no_se_descarta():
    """Un campo que no conocemos es contenido del usuario: se conserva."""
    from pywhats.proto import Message

    proto = Message()
    proto.conversation = "x"
    raw = bytearray(proto.SerializeToString())
    # Campo 91, inexistente en la referencia: length-delimited, 1 byte.
    raw += field_key(91) + bytes([1, 0x41])

    clase = classify_message_bytes(bytes(raw))
    assert is_visible(clase), "un tipo sin interpretar NO puede filtrarse"


def test_D_sin_bytes_queda_para_revision():
    """Sin evidencia no se descarta: se marca para revisar."""
    clase = classify_message_bytes(None)
    assert clase is MessageClass.UNKNOWN_NEEDS_REVIEW
    assert is_visible(clase)


def test_el_contexto_no_anula_el_contenido():
    """messageContextInfo acompana; no convierte el mensaje en protocolo."""
    from pywhats.proto import Message

    proto = Message()
    proto.conversation = "hola"
    raw = bytearray(proto.SerializeToString())
    raw += field_key(35) + bytes([0])  # messageContextInfo vacio

    assert classify_message_bytes(bytes(raw)) is MessageClass.VISIBLE_TEXT


# ---------------------------------------------------------------------------
# TEST A / G -- identidad propia y seleccion del canary
# ---------------------------------------------------------------------------


def test_A_pn_y_lid_propios_se_reconocen(settings, database):
    from app.backfill_service import BackfillService

    service = BackfillService(settings, database)
    service.set_own_identity("573002389304@s.whatsapp.net", "86531142340710@lid")

    assert not service.is_backfill_candidate("573002389304@s.whatsapp.net")
    assert not service.is_backfill_candidate("86531142340710@lid")
    # Un tercero si es candidato.
    assert service.is_backfill_candidate("34600111222@s.whatsapp.net")


def test_G_el_canary_nunca_elige_el_chat_propio(settings, database):
    """El ultimo canary eligio nuestra cuenta y acabo en ACK + timeout."""
    from app.backfill_service import BackfillService

    service = BackfillService(settings, database)
    service.set_own_identity("573002389304@s.whatsapp.net", "86531142340710@lid")

    for jid in (
        "573002389304@s.whatsapp.net",
        "86531142340710@lid",
        "status@broadcast",
        "123@newsletter",
    ):
        assert not service.is_backfill_candidate(jid), jid


# ---------------------------------------------------------------------------
# TEST H / I / M -- orden cronologico
# ---------------------------------------------------------------------------


def test_H_filas_desordenadas_se_ordenan(session):
    """Insertadas en desorden, deben leerse cronologicamente."""
    from app import repository as repo
    from app.repository import IncomingMessage

    jid = "34600777888@s.whatsapp.net"
    chat_id = repo.upsert_chat(session, jid=jid, chat_type="individual")

    base = 1_754_000_000  # 10 ago aprox
    momentos = [
        base + 86400 * 2 + 3600 * 10,  # 12 ago 10:00
        base + 3600 * 8,               # 10 ago 08:00
        base + 86400 + 3600 * 22,      # 11 ago 22:00
        base + 3600 * 12,              # 10 ago 12:00
        base + 86400 + 3600 * 9,       # 11 ago 09:00
    ]
    repo.bulk_upsert_messages(
        session,
        {jid: chat_id},
        [
            IncomingMessage(
                chat_jid=jid, timestamp=ts, source="on_demand",
                whatsapp_message_id=f"ORD{i}", text=f"m{i}", message_type="text",
            )
            for i, ts in enumerate(momentos)
        ],
    )
    session.flush()

    page = repo.get_recent_messages(session, chat_id, limit=200)
    stamps = [m.timestamp for m in page]
    assert stamps == sorted(stamps), "la pagina debe venir en orden ascendente"
    assert stamps == sorted(momentos)


def test_M_mismo_timestamp_orden_estable_por_id(session):
    from app import repository as repo
    from app.repository import IncomingMessage

    jid = "34600777999@s.whatsapp.net"
    chat_id = repo.upsert_chat(session, jid=jid, chat_type="individual")
    repo.bulk_upsert_messages(
        session,
        {jid: chat_id},
        [
            IncomingMessage(
                chat_jid=jid, timestamp=1_754_000_000, source="on_demand",
                whatsapp_message_id=f"SAME{i}", text=f"m{i}", message_type="text",
            )
            for i in range(6)
        ],
    )
    session.flush()

    primera = [m.id for m in repo.get_recent_messages(session, chat_id, limit=200)]
    segunda = [m.id for m in repo.get_recent_messages(session, chat_id, limit=200)]
    assert primera == segunda, "el orden debe ser estable entre consultas"
    assert primera == sorted(primera), "con igual timestamp desempata el id"


def test_I_separadores_en_orden_al_insertar_arriba(session):
    """El bug real: al cargar anteriores las fechas salian intercaladas.

    Se comprueba sobre el modelo de datos, que es lo que decide el orden:
    la pagina anterior debe ser INTEGRAMENTE mas antigua que la actual.
    """
    from app import repository as repo
    from app.repository import IncomingMessage

    jid = "34600777000@s.whatsapp.net"
    chat_id = repo.upsert_chat(session, jid=jid, chat_type="individual")
    base = 1_754_000_000
    repo.bulk_upsert_messages(
        session,
        {jid: chat_id},
        [
            IncomingMessage(
                chat_jid=jid, timestamp=base + i * 3600, source="on_demand",
                whatsapp_message_id=f"SEQ{i:03d}", text=f"m{i}", message_type="text",
            )
            for i in range(120)
        ],
    )
    session.flush()

    reciente = repo.get_recent_messages(session, chat_id, limit=50)
    anterior = repo.get_messages_before(
        session, chat_id, reciente[0].timestamp, reciente[0].id, 50
    )

    assert anterior[-1].timestamp < reciente[0].timestamp
    assert anterior == sorted(anterior, key=lambda m: (m.timestamp, m.id))
    combinado = anterior + reciente
    assert combinado == sorted(combinado, key=lambda m: (m.timestamp, m.id))


# ---------------------------------------------------------------------------
# TEST K / L -- paginacion keyset
# ---------------------------------------------------------------------------


def test_K_L_paginacion_completa_sin_duplicados(session):
    from app import repository as repo
    from app.repository import IncomingMessage

    jid = "34600666000@s.whatsapp.net"
    chat_id = repo.upsert_chat(session, jid=jid, chat_type="individual")
    total = 452
    repo.bulk_upsert_messages(
        session,
        {jid: chat_id},
        [
            IncomingMessage(
                chat_jid=jid, timestamp=1_754_000_000 + i * 60, source="on_demand",
                whatsapp_message_id=f"PAG{i:04d}", text=f"m{i}", message_type="text",
            )
            for i in range(total)
        ],
    )
    session.flush()

    vistos: list[int] = []
    tamanos: list[int] = []
    page = repo.get_recent_messages(session, chat_id, limit=200)
    while page:
        vistos.extend(m.id for m in page)
        tamanos.append(len(vistos))
        page = repo.get_messages_before(session, chat_id, page[0].timestamp, page[0].id, 200)

    assert tamanos == [200, 400, 452], f"progresion inesperada: {tamanos}"
    assert len(set(vistos)) == total, "una fila aparecio en dos paginas"


# ---------------------------------------------------------------------------
# TEST Q / R -- fin de historial
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "end_type,queda_mas",
    [
        (0, True),   # COMPLETE_BUT_MORE_MESSAGES_REMAIN_ON_PRIMARY
        (1, False),  # COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY
        (2, True),   # COMPLETE_ON_DEMAND_SYNC_BUT_MORE_MSG_REMAIN
        (3, False),  # ...NO_ACCESS
    ],
)
def test_Q_R_fin_de_historial(end_type, queda_mas):
    from app.backfill_service import _MORE_REMAINS

    assert (end_type in _MORE_REMAINS) is queda_mas
