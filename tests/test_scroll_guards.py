"""Guardas del scroll automatico.

Existen por un fallo medido: con el scroll activado y el contenido sin
desbordar el viewport, ``yview()`` devuelve ``(0.0, 1.0)``, la condicion
"estoy cerca del principio" se cumplia siempre y cada carga provocaba otra
hasta meter los 452 mensajes de golpe.
"""

from __future__ import annotations

import queue

import pytest

tk = pytest.importorskip("tkinter")

from app.services import repository as repo  # noqa: E402
from app.services.repository import ChatSummary, IncomingMessage  # noqa: E402

CHAT_JID = "34600444333@s.whatsapp.net"
TOTAL = 452


@pytest.fixture(scope="module")
def app(tk_app):
    """La ventana compartida de la suite (ver ``tk_app`` en conftest).

    Antes cada modulo creaba y destruia su propio ``Tk()``, y encadenar varios
    interpretes Tcl en un mismo proceso hacia que en Windows fallaran de forma
    intermitente con "Can't find a usable init.tcl".
    """
    return tk_app


@pytest.fixture
def conversation(app, session):
    from app.gui.chat_view import PAGE_SIZE

    chat_id = repo.upsert_chat(session, jid=CHAT_JID, chat_type="individual")
    repo.bulk_upsert_messages(
        session,
        {CHAT_JID: chat_id},
        [
            IncomingMessage(
                chat_jid=CHAT_JID, timestamp=1_754_000_000 + i * 60,
                source="on_demand", whatsapp_message_id=f"SCR{i:04d}",
                text=f"m{i}", message_type="text",
            )
            for i in range(TOTAL)
        ],
    )
    session.flush()

    panel = app.viewer.conversation
    panel.bind_loader(
        lambda cid, ts, mid, limit: repo.get_messages_before(session, cid, ts, mid, limit)
    )
    panel.open_chat(
        ChatSummary(
            id=chat_id, jid=CHAT_JID, display_name="Scroll", chat_type="individual",
            last_message=None, last_message_timestamp=None, message_count=TOTAL,
        ),
        repo.get_recent_messages(session, chat_id, limit=PAGE_SIZE),
        {}, {}, repo.get_chat_stats(session, chat_id),
    )
    app.root.update()
    return panel


def test_yview_completo_no_dispara_carga(app, conversation, monkeypatch):
    """LA guarda clave: (0.0, 1.0) significa "todo cabe", no "estoy arriba"."""
    monkeypatch.setattr(conversation._scroll.canvas, "yview", lambda: (0.0, 1.0))
    conversation._last_prefetch_at = 0.0
    antes = conversation._rendered

    for _ in range(10):
        conversation._maybe_prefetch()

    assert conversation._rendered == antes, (
        "con el contenido sin desbordar no puede cargarse ni una pagina"
    )


def test_no_hay_cascada(app, conversation, monkeypatch):
    """Diez eventos seguidos cargan UNA pagina, no diez."""
    monkeypatch.setattr(conversation._scroll.canvas, "yview", lambda: (0.0, 0.4))
    conversation._last_prefetch_at = 0.0
    antes = conversation._rendered

    for _ in range(10):
        conversation._maybe_prefetch()
        app.root.update()

    ganados = conversation._rendered - antes
    assert ganados <= 200, f"cascada: se cargaron {ganados} mensajes de golpe"
    assert ganados > 0, "deberia haber cargado una pagina"


def test_lejos_del_principio_no_carga(app, conversation, monkeypatch):
    monkeypatch.setattr(conversation._scroll.canvas, "yview", lambda: (0.8, 0.95))
    conversation._last_prefetch_at = 0.0
    antes = conversation._rendered

    conversation._maybe_prefetch()
    assert conversation._rendered == antes


def test_el_cooldown_frena_los_disparos_seguidos(app, conversation, monkeypatch):
    import time

    monkeypatch.setattr(conversation._scroll.canvas, "yview", lambda: (0.0, 0.4))
    conversation._last_prefetch_at = time.monotonic()  # recien cargado
    antes = conversation._rendered

    conversation._maybe_prefetch()
    assert conversation._rendered == antes, "el cooldown debe bloquearlo"


def test_scroll_y_boton_comparten_funcion():
    import inspect

    from app.gui.chat_view import ConversationPanel

    assert "load_previous_page" in inspect.getsource(ConversationPanel._maybe_prefetch)


def test_todo_cargado_no_vuelve_a_pedir(app, conversation, monkeypatch):
    """Si ya se muestra todo lo almacenado, no hay nada que traer."""
    monkeypatch.setattr(conversation._scroll.canvas, "yview", lambda: (0.0, 0.4))
    while conversation.load_previous_page():
        pass
    conversation._last_prefetch_at = 0.0
    antes = conversation._rendered

    conversation._maybe_prefetch()
    assert conversation._rendered == antes
    assert conversation._rendered == TOTAL
