"""Persistencia de mensajes en vivo.

Se construyen eventos con la MISMA forma que emite pywhats (``events.Message``
y ``events.MediaAttachment`` reales, no dobles) y se comprueba que acaban en
PostgreSQL sin duplicarse ni pisar lo que ya hubiera.
"""

from __future__ import annotations

import pytest
from pywhats.events import JID, MediaAttachment, Message
from sqlalchemy import select

from app.services import repository as repo
from app.services.live_service import LiveMessageService, jid_to_string, message_type_for
from app.models import MediaFile
from app.models import Message as MessageRow
from app.services.repository import IncomingMessage

CHAT = JID(user="34600111222", server="s.whatsapp.net")
CHAT_JID = "34600111222@s.whatsapp.net"
LID_CHAT = JID(user="82025587417265", server="lid")


class FakeDatabase:
    """Reutiliza la sesion transaccional del test en vez de abrir otra."""

    def __init__(self, session):
        self._session = session

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


def make_message(**kwargs) -> Message:
    defaults = dict(
        id="3EB0ABCDEF123456",
        chat=CHAT,
        sender=CHAT,
        text="hola",
        timestamp=1_700_000_000,
        from_me=False,
        media=None,
        quoted=None,
    )
    defaults.update(kwargs)
    return Message(**defaults)


# ---------------------------------------------------------------------------
# Normalizacion
# ---------------------------------------------------------------------------


def test_jid_conserva_el_servidor():
    """Un @lid no se convierte en telefono ni al reves."""
    assert jid_to_string(CHAT) == CHAT_JID
    assert jid_to_string(LID_CHAT) == "82025587417265@lid"
    assert jid_to_string(None) is None


def test_tipo_de_mensaje():
    assert message_type_for(make_message()) == "text"
    audio = MediaAttachment(
        kind="audio", direct_path="/x", media_key=b"k" * 32, file_sha256=b"s" * 32,
        file_enc_sha256=b"e" * 32, media_type="WhatsApp Audio Keys", file_length=100,
        mimetype="audio/ogg", filename="", caption="", ptt=True, mms_type="audio",
    )
    # ptt=True distingue una nota de voz de un audio adjunto.
    assert message_type_for(make_message(media=audio)) == "voice_note"
    audio.ptt = False
    assert message_type_for(make_message(media=audio)) == "audio"


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------


def test_mensaje_live_se_guarda(session):
    service = LiveMessageService(FakeDatabase(session))
    result = service.handle(make_message(text="mensaje nuevo"))

    assert result is not None and result["new"] is True
    row = session.execute(
        select(MessageRow).where(MessageRow.whatsapp_message_id == "3EB0ABCDEF123456")
    ).scalar_one()
    assert row.text == "mensaje nuevo"
    assert row.source == "live"
    assert row.chat_jid == CHAT_JID
    # El evento de pywhats no expone el WebMessageInfo en crudo.
    assert row.raw_proto is None


def test_el_chat_se_crea_y_se_actualiza(session):
    service = LiveMessageService(FakeDatabase(session))
    service.handle(make_message(text="primero", timestamp=1_700_000_000))
    service.handle(
        make_message(id="OTRO", text="segundo", timestamp=1_700_000_500)
    )

    summaries = repo.list_chat_summaries(session)
    chat = next(c for c in summaries if c.jid == CHAT_JID)
    assert chat.message_count == 2
    assert chat.last_message == "segundo", "la vista previa debe seguir al ultimo"
    assert chat.last_message_timestamp == 1_700_000_500


def test_no_duplica_lo_que_ya_llego_por_historial(session):
    """History Sync y live se solapan a proposito: no debe duplicarse."""
    chat_id = repo.upsert_chat(session, jid=CHAT_JID, chat_type="individual")
    repo.bulk_upsert_messages(
        session,
        {CHAT_JID: chat_id},
        [
            IncomingMessage(
                chat_jid=CHAT_JID,
                timestamp=1_700_000_000,
                source="initial_history",
                whatsapp_message_id="3EB0ABCDEF123456",
                text="desde historial",
                raw_proto=b"\x01\x02",
            )
        ],
    )
    session.flush()

    service = LiveMessageService(FakeDatabase(session))
    result = service.handle(make_message(text="desde live"))

    assert result is not None and result["new"] is False
    assert repo.count_messages(session, CHAT_JID) == 1

    row = session.execute(
        select(MessageRow).where(MessageRow.whatsapp_message_id == "3EB0ABCDEF123456")
    ).scalar_one()
    assert row.text == "desde historial", "no se pisa lo que ya estaba"
    assert row.raw_proto == b"\x01\x02", "el raw_proto del historial se conserva"


def test_el_lid_va_a_su_columna(session):
    """Un emisor @lid no debe acabar en sender_jid."""
    service = LiveMessageService(FakeDatabase(session))
    service.handle(make_message(chat=LID_CHAT, sender=LID_CHAT, id="LIDMSG"))

    row = session.execute(
        select(MessageRow).where(MessageRow.whatsapp_message_id == "LIDMSG")
    ).scalar_one()
    assert row.sender_lid == "82025587417265@lid"
    assert row.sender_jid is None


def test_el_adjunto_queda_pendiente_de_descarga(session):
    media = MediaAttachment(
        kind="image", direct_path="/v/t62.abc", media_key=b"k" * 32,
        file_sha256=b"s" * 32, file_enc_sha256=b"e" * 32,
        media_type="WhatsApp Image Keys", file_length=45678,
        mimetype="image/jpeg", filename="", caption="mira esto", ptt=False,
        mms_type="image",
    )
    service = LiveMessageService(FakeDatabase(session))
    service.handle(make_message(id="IMGMSG", text="", media=media))

    row = session.execute(
        select(MessageRow).where(MessageRow.whatsapp_message_id == "IMGMSG")
    ).scalar_one()
    assert row.message_type == "image"
    assert row.text == "mira esto", "el caption se usa como texto del mensaje"

    attachment = session.execute(
        select(MediaFile).where(MediaFile.whatsapp_message_id == "IMGMSG")
    ).scalar_one()
    assert attachment.download_status == "pending"
    assert attachment.media_key == b"k" * 32
    assert attachment.file_size == 45678


def test_un_mensaje_roto_no_tumba_el_receptor(session):
    """El receptor es lo prioritario: un fallo se registra y se sigue."""
    service = LiveMessageService(FakeDatabase(session))

    class Roto:
        chat = CHAT
        sender = CHAT
        id = "X"
        text = "x"
        from_me = False
        media = None

        @property
        def timestamp(self):
            raise RuntimeError("timestamp corrupto")

    assert service.handle(Roto()) is None  # no propaga
    assert service.handle(make_message(id="DESPUES")) is not None  # sigue vivo


def test_marca_el_emisor_propio(session):
    service = LiveMessageService(FakeDatabase(session), own_jid="34699000111@s.whatsapp.net")
    service.handle(make_message(id="MIO", from_me=True))

    row = session.execute(
        select(MessageRow).where(MessageRow.whatsapp_message_id == "MIO")
    ).scalar_one()
    assert row.from_me is True
    assert row.sender_jid == "34699000111@s.whatsapp.net"


@pytest.mark.parametrize(
    "count,expected", [(50, 50), (100, 100), (500, 500), (5000, 500), (1, 1)]
)
def test_tope_de_mensajes_por_peticion(monkeypatch, count, expected):
    """Un valor desmedido se acota en vez de romper la extraccion.

    El techo es 500. El servidor acota la respuesta por su cuenta de todos
    modos, asi que esto solo evita pedir una barbaridad; el limite real se
    observa en ``history_requests.response_count``.
    """
    import app.core.config as config

    monkeypatch.setenv("HISTORY_ON_DEMAND_COUNT", str(count))
    assert config._history_count() == expected
