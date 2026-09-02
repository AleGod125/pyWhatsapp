"""Persistencia sobre PostgreSQL real.

Nada de SQLite aqui: el comportamiento que importa (indice UNIQUE parcial,
ON CONFLICT, JSONB, BYTEA, CHECK) es especifico de PostgreSQL.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app import repository as repo
from app.models import Chat, Message
from app.repository import IncomingMessage

CHAT_JID = "34600111222@s.whatsapp.net"


def _chat(session) -> int:
    return repo.upsert_chat(session, jid=CHAT_JID, name="Contacto", chat_type="individual")


def _msg(**kwargs) -> IncomingMessage:
    kwargs.setdefault("chat_jid", CHAT_JID)
    kwargs.setdefault("source", "initial_history")
    return IncomingMessage(**kwargs)


# ---------------------------------------------------------------------------
# Conexion y esquema
# ---------------------------------------------------------------------------


def test_conexion_y_migraciones(database):
    health = database.health()
    assert health["database"] == "whatsapp_backup"
    assert health["server_version"].startswith("18")
    assert database.applied_migration() is not None, "no hay revision de Alembic aplicada"


def test_no_hay_sqlite_como_base_de_la_app(settings):
    """La configuracion debe apuntar a PostgreSQL, nunca a SQLite."""
    assert settings.database_url.startswith("postgresql")
    assert "sqlite" not in settings.database_url.lower()


# ---------------------------------------------------------------------------
# Chats y contactos
# ---------------------------------------------------------------------------


def test_upsert_chat_es_idempotente(session):
    first = _chat(session)
    second = repo.upsert_chat(session, jid=CHAT_JID, chat_type="individual")
    assert first == second
    assert session.execute(select(Chat).where(Chat.jid == CHAT_JID)).scalar_one().name == "Contacto"


def test_upsert_chat_no_degrada_datos_conocidos(session):
    """Un update parcial no debe borrar el nombre ya resuelto."""
    _chat(session)
    repo.upsert_chat(session, jid=CHAT_JID, last_message="hola", last_message_timestamp=100)
    chat = session.execute(select(Chat).where(Chat.jid == CHAT_JID)).scalar_one()
    assert chat.name == "Contacto", "el nombre no debe perderse"
    assert chat.chat_type == "individual", "'unknown' no debe pisar un tipo conocido"


def test_last_message_timestamp_solo_avanza(session):
    _chat(session)
    repo.upsert_chat(session, jid=CHAT_JID, last_message_timestamp=200)
    repo.upsert_chat(session, jid=CHAT_JID, last_message_timestamp=100)  # llega tarde
    chat = session.execute(select(Chat).where(Chat.jid == CHAT_JID)).scalar_one()
    assert chat.last_message_timestamp == 200


def test_upsert_contact_conserva_pushname(session):
    repo.upsert_contact(session, jid=CHAT_JID, push_name="Ana")
    repo.upsert_contact(session, jid=CHAT_JID, lid="123@lid")
    contact = session.execute(
        select(repo.Contact).where(repo.Contact.jid == CHAT_JID)
    ).scalar_one()
    assert contact.push_name == "Ana"
    assert contact.lid == "123@lid"


# ---------------------------------------------------------------------------
# Mensajes: deduplicacion
# ---------------------------------------------------------------------------


def test_insercion_en_lote(session):
    chat_id = _chat(session)
    ids = {CHAT_JID: chat_id}
    batch = [
        _msg(whatsapp_message_id=f"AC{i}", timestamp=100 + i, text=f"m{i}") for i in range(50)
    ]
    assert repo.bulk_upsert_messages(session, ids, batch) == 50
    assert repo.count_messages(session, CHAT_JID) == 50


def test_deduplicacion_por_id_real(session):
    """History Sync y live pueden traer el mismo mensaje: no debe duplicarse."""
    chat_id = _chat(session)
    ids = {CHAT_JID: chat_id}

    repo.bulk_upsert_messages(
        session, ids, [_msg(whatsapp_message_id="AC1", timestamp=100, text="hola")]
    )
    inserted = repo.bulk_upsert_messages(
        session,
        ids,
        [_msg(whatsapp_message_id="AC1", timestamp=100, text="hola", source="live")],
    )
    assert inserted == 0, "el duplicado no debe insertar fila nueva"
    assert repo.count_messages(session, CHAT_JID) == 1


def test_upsert_rellena_huecos_sin_pisar_lo_existente(session):
    chat_id = _chat(session)
    ids = {CHAT_JID: chat_id}

    repo.bulk_upsert_messages(
        session, ids, [_msg(whatsapp_message_id="AC1", timestamp=100, text="original")]
    )
    repo.bulk_upsert_messages(
        session,
        ids,
        [
            _msg(
                whatsapp_message_id="AC1",
                timestamp=100,
                text="otro texto",
                raw_proto=b"\x01\x02\x03",
                source="live",
            )
        ],
    )
    message = session.execute(
        select(Message).where(Message.whatsapp_message_id == "AC1")
    ).scalar_one()
    assert message.text == "original", "no se pisa el texto ya guardado"
    assert message.raw_proto == b"\x01\x02\x03", "se rellena el hueco que faltaba"


def test_mensajes_sin_id_no_colisionan_entre_si(session):
    """El indice es PARCIAL: los mensajes sin ID real no se deduplican.

    Su identidad es la PK de PostgreSQL. Inventarles una clave falsearia el
    mensaje, asi que se aceptan como filas distintas.
    """
    chat_id = _chat(session)
    ids = {CHAT_JID: chat_id}
    batch = [
        _msg(whatsapp_message_id=None, timestamp=100, text="a"),
        _msg(whatsapp_message_id=None, timestamp=100, text="b"),
    ]
    assert repo.bulk_upsert_messages(session, ids, batch) == 2
    assert repo.count_messages(session, CHAT_JID) == 2


def test_id_vacio_es_rechazado_por_la_base(session):
    """Un ID vacio es un error de ingesta: para 'sin ID' esta NULL."""
    chat_id = _chat(session)
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO messages (chat_id, chat_jid, whatsapp_message_id, message_type, "
                '"timestamp", from_me, source) VALUES (:c, :j, :w, :t, :ts, false, :s)'
            ),
            {"c": chat_id, "j": CHAT_JID, "w": "", "t": "text", "ts": 100, "s": "live"},
        )


# ---------------------------------------------------------------------------
# Fidelidad: raw_proto y JSONB
# ---------------------------------------------------------------------------


def test_raw_proto_y_jsonb_se_conservan(session):
    chat_id = _chat(session)
    blob = bytes(range(256))  # incluye 0x00 y 0xff
    metadata = {"tipo": "imagen", "anidado": {"width": 640}, "lista": [1, 2, 3]}

    repo.bulk_upsert_messages(
        session,
        {CHAT_JID: chat_id},
        [
            _msg(
                whatsapp_message_id="AC1",
                timestamp=100,
                raw_proto=blob,
                raw_metadata=metadata,
            )
        ],
    )
    message = session.execute(
        select(Message).where(Message.whatsapp_message_id == "AC1")
    ).scalar_one()
    assert message.raw_proto == blob, "BYTEA debe conservar los bytes exactos"
    assert message.raw_metadata == metadata


def test_borrar_chat_arrastra_sus_mensajes(session):
    chat_id = _chat(session)
    repo.bulk_upsert_messages(
        session, {CHAT_JID: chat_id}, [_msg(whatsapp_message_id="AC1", timestamp=100)]
    )
    session.flush()
    session.execute(text("DELETE FROM chats WHERE id = :i"), {"i": chat_id})
    assert repo.count_messages(session, CHAT_JID) == 0


# ---------------------------------------------------------------------------
# CURSOR HISTORICO -- el caso del brief (seccion 29)
# ---------------------------------------------------------------------------


def test_cursor_historico_ignora_ids_sinteticos(session):
    """Dataset exacto del brief.

        timestamp=100  id=AC100
        timestamp=90   id sintetico
        timestamp=95   id=AC095

    Esperado:
        mensaje mas antiguo almacenado -> timestamp 90
        cursor valido mas antiguo      -> AC095, timestamp 95
    """
    chat_id = _chat(session)
    repo.bulk_upsert_messages(
        session,
        {CHAT_JID: chat_id},
        [
            _msg(whatsapp_message_id="AC100", timestamp=100),
            # Sin ID real: el sintetico va en su propia columna, jamas en
            # whatsapp_message_id.
            _msg(
                whatsapp_message_id=None,
                synthetic_identifier="opaque-deadbeef",
                timestamp=90,
            ),
            _msg(whatsapp_message_id="AC095", timestamp=95),
        ],
    )

    assert repo.get_oldest_stored_timestamp(session, CHAT_JID) == 90

    cursor = repo.get_oldest_valid_history_cursor(session, CHAT_JID)
    assert cursor is not None
    assert cursor.message_id == "AC095"
    assert cursor.timestamp == 95


def test_cursor_rechaza_prefijos_sinteticos_almacenados(session):
    """Defensa en profundidad: aunque un 'opaque-' se colara en la columna."""
    chat_id = _chat(session)
    repo.bulk_upsert_messages(
        session,
        {CHAT_JID: chat_id},
        [
            _msg(whatsapp_message_id="opaque-abc123", timestamp=90),
            _msg(whatsapp_message_id="AC095", timestamp=95),
        ],
    )
    cursor = repo.get_oldest_valid_history_cursor(session, CHAT_JID)
    assert cursor is not None and cursor.message_id == "AC095"


def test_chat_sin_cursor_valido(session):
    """Un chat sin ningun ID real es 'no_valid_cursor', no un error."""
    chat_id = _chat(session)
    repo.bulk_upsert_messages(
        session,
        {CHAT_JID: chat_id},
        [_msg(whatsapp_message_id=None, synthetic_identifier="opaque-1", timestamp=90)],
    )
    assert repo.get_oldest_valid_history_cursor(session, CHAT_JID) is None
    assert repo.get_oldest_stored_timestamp(session, CHAT_JID) == 90


@pytest.mark.parametrize(
    "message_id,expected",
    [
        ("3EB0C767D82B1B", True),
        ("AC095", True),
        (None, False),
        ("", False),
        ("opaque-deadbeef", False),
        ("OPAQUE-DEADBEEF", False),  # sin distinguir mayusculas
        ("synthetic-1", False),
        ("local-42", False),
    ],
)
def test_validacion_de_id_de_cursor(message_id, expected):
    assert repo.is_valid_history_cursor_id(message_id) is expected


# ---------------------------------------------------------------------------
# Estado de historial y app_state
# ---------------------------------------------------------------------------


def test_estado_de_historial_se_recalcula(session):
    chat_id = _chat(session)
    repo.get_or_create_history_state(session, chat_id=chat_id, chat_jid=CHAT_JID)
    repo.bulk_upsert_messages(
        session,
        {CHAT_JID: chat_id},
        [
            _msg(whatsapp_message_id=None, timestamp=90),
            _msg(whatsapp_message_id="AC095", timestamp=95),
            _msg(whatsapp_message_id="AC100", timestamp=100),
        ],
    )
    repo.refresh_history_state(session, CHAT_JID)

    state = session.execute(
        select(repo.ChatHistoryState).where(repo.ChatHistoryState.chat_jid == CHAT_JID)
    ).scalar_one()
    session.refresh(state)
    assert state.message_count == 3
    # El cursor apunta al mas antiguo UTILIZABLE, no al mas antiguo almacenado.
    assert state.oldest_message_id == "AC095"
    assert state.oldest_message_timestamp == 95
    assert state.newest_message_timestamp == 100


def test_app_state_persiste_flags(session):
    """Clave propia del test: la real puede existir ya por una ejecucion real."""
    key = "test_flag_only_for_pytest"
    assert repo.get_app_state(session, key) is None
    repo.set_app_state(session, key, {"confirmed": True})
    assert repo.get_app_state(session, key) == {"confirmed": True}
    repo.set_app_state(session, key, {"confirmed": False})
    assert repo.get_app_state(session, key) == {"confirmed": False}


# ---------------------------------------------------------------------------
# Paginacion (capa GUI, sin tocar WhatsApp)
# ---------------------------------------------------------------------------


def test_paginacion_hacia_atras(session):
    chat_id = _chat(session)
    repo.bulk_upsert_messages(
        session,
        {CHAT_JID: chat_id},
        [_msg(whatsapp_message_id=f"AC{i:04d}", timestamp=1000 + i) for i in range(500)],
    )
    session.flush()

    page = repo.get_recent_messages(session, chat_id, limit=200)
    assert len(page) == 200
    assert page[0].timestamp < page[-1].timestamp, "orden cronologico ascendente"
    assert page[-1].timestamp == 1499, "la primera pagina son los mas recientes"

    older = repo.get_messages_before(
        session, chat_id, before_timestamp=page[0].timestamp, before_id=page[0].id, limit=200
    )
    assert len(older) == 200
    assert older[-1].timestamp < page[0].timestamp, "no debe solaparse con la pagina anterior"

    oldest = repo.get_messages_before(
        session, chat_id, before_timestamp=older[0].timestamp, before_id=older[0].id, limit=200
    )
    assert len(oldest) == 100, "quedaban 100"


def test_paginacion_desempata_timestamps_iguales(session):
    """History Sync entrega muchos mensajes con el mismo timestamp."""
    chat_id = _chat(session)
    repo.bulk_upsert_messages(
        session,
        {CHAT_JID: chat_id},
        [_msg(whatsapp_message_id=f"AC{i}", timestamp=100) for i in range(10)],
    )
    session.flush()

    page = repo.get_recent_messages(session, chat_id, limit=4)
    older = repo.get_messages_before(
        session, chat_id, before_timestamp=page[0].timestamp, before_id=page[0].id, limit=4
    )
    assert len(older) == 4
    seen = {row.id for row in page} & {row.id for row in older}
    assert not seen, "una fila no puede aparecer en dos paginas"
