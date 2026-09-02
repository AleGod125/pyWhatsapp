"""Enrutado por estado y virtualizacion del visor.

Dos comportamientos que se comprueban aqui:

* Si ya hay sesion vinculada, la aplicacion NO debe pedir el QR: tiene que
  abrir los chats directamente.
* Una conversacion larga no puede crear miles de widgets de golpe, y al cargar
  mensajes anteriores el viewport debe quedarse donde estaba.
"""

from __future__ import annotations

import queue

import pytest

tk = pytest.importorskip("tkinter")

from app import repository as repo  # noqa: E402
from app.repository import IncomingMessage  # noqa: E402

CHAT_JID = "34600999888@s.whatsapp.net"


# ---------------------------------------------------------------------------
# Enrutado por estado
# ---------------------------------------------------------------------------


def test_sin_sesion_hay_que_vincular(tmp_path, settings):
    """Sin device.json, ``session_exists`` es False -> se pide QR."""
    from dataclasses import replace

    from app.whatsapp_client import WhatsAppClient

    isolated = replace(settings, session_dir=tmp_path / "vacio")
    client = WhatsAppClient(isolated, queue.Queue())
    assert client.session_exists is False


def test_con_sesion_no_se_pide_qr(tmp_path, settings):
    """Con device.json presente se va directo a los chats."""
    from dataclasses import replace

    from app.whatsapp_client import WhatsAppClient

    session_dir = tmp_path / "con-sesion"
    session_dir.mkdir()
    (session_dir / "device.json").write_text("{}", encoding="utf-8")

    isolated = replace(settings, session_dir=session_dir)
    client = WhatsAppClient(isolated, queue.Queue())
    assert client.session_exists is True


# ---------------------------------------------------------------------------
# Vistas
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app(tk_app):
    """La ventana compartida de la suite (ver ``tk_app`` en conftest).

    Antes cada modulo creaba y destruia su propio ``Tk()``, y encadenar varios
    interpretes Tcl en un mismo proceso hacia que en Windows fallaran de forma
    intermitente con "Can't find a usable init.tcl".
    """
    return tk_app


def test_cambiar_de_vista_no_abre_ventanas(app):
    """Una sola ventana: cambiar de vista es cambiar de frame."""
    toplevels_before = len(app.root.winfo_children())

    app.show_viewer(connected=False)
    app.root.update()
    app.show_pairing()
    app.root.update()
    app.show_viewer(connected=True)
    app.root.update()

    assert len(app.root.winfo_children()) == toplevels_before
    assert app.pairing.winfo_ismapped() == 0, "la vista del QR debe quedar oculta"
    assert app.viewer.winfo_ismapped() == 1


def test_el_titulo_refleja_el_estado(app):
    app.show_viewer(connected=True)
    app.root.update()
    assert "Conectado" in app.root.title()

    app.show_pairing()
    app.root.update()
    assert "Vincular" in app.root.title()


# ---------------------------------------------------------------------------
# Virtualizacion
# ---------------------------------------------------------------------------


@pytest.fixture
def big_chat(session):
    """Un chat con 1.000 mensajes, dentro de la transaccion del test."""
    chat_id = repo.upsert_chat(
        session, jid=CHAT_JID, name="Chat grande", chat_type="individual"
    )
    repo.bulk_upsert_messages(
        session,
        {CHAT_JID: chat_id},
        [
            IncomingMessage(
                chat_jid=CHAT_JID,
                timestamp=1_700_000_000 + i * 60,
                source="initial_history",
                whatsapp_message_id=f"BIG{i:05d}",
                text=f"mensaje {i}",
                message_type="text",
                from_me=(i % 3 == 0),
            )
            for i in range(1000)
        ],
    )
    session.flush()
    return chat_id


def test_no_se_cargan_todos_los_mensajes(session, big_chat):
    """Abrir un chat de 1.000 mensajes trae solo una pagina."""
    from app.chat_view import PAGE_SIZE

    assert repo.count_messages(session, CHAT_JID) == 1000
    page = repo.get_recent_messages(session, big_chat, limit=PAGE_SIZE)
    assert len(page) == PAGE_SIZE, "no se debe traer la conversacion entera"
    # Y es la cola de la conversacion, que es lo que el usuario espera ver.
    assert page[-1].text == "mensaje 999"


def test_paginacion_recorre_toda_la_conversacion(session, big_chat):
    """Paginando hacia atras se llega al principio sin repetir ni perder filas."""
    from app.chat_view import PAGE_SIZE

    seen: list[int] = []
    page = repo.get_recent_messages(session, big_chat, limit=PAGE_SIZE)
    while page:
        seen.extend(row.id for row in page)
        page = repo.get_messages_before(
            session, big_chat, page[0].timestamp, page[0].id, PAGE_SIZE
        )

    assert len(seen) == 1000, "se han perdido mensajes al paginar"
    assert len(set(seen)) == 1000, "una fila ha aparecido en dos paginas"


def test_la_pagina_no_consulta_raw_proto(session, big_chat):
    """La GUI no debe arrastrar la columna mas pesada para pintar."""
    page = repo.get_recent_messages(session, big_chat, limit=10)
    assert not hasattr(page[0], "raw_proto"), (
        "las consultas de la GUI no deben traer raw_proto"
    )


def test_el_prepend_conserva_la_posicion(app, session, big_chat):
    """Al insertar mensajes anteriores, el viewport no debe saltar."""
    from app.chat_view import PAGE_SIZE
    from app.repository import ChatSummary

    summary = ChatSummary(
        id=big_chat,
        jid=CHAT_JID,
        display_name="Chat grande",
        chat_type="individual",
        last_message=None,
        last_message_timestamp=None,
        message_count=1000,
    )
    page = repo.get_recent_messages(session, big_chat, limit=PAGE_SIZE)

    conversation = app.viewer.conversation
    conversation.open_chat(summary, page, {})
    app.root.update_idletasks()
    app.root.update()

    widgets_before = len(conversation._scroll.body.winfo_children())
    assert widgets_before <= PAGE_SIZE + 30, (
        f"{widgets_before} widgets para una pagina: se esta pintando de mas"
    )

    older = repo.get_messages_before(
        session, big_chat, page[0].timestamp, page[0].id, PAGE_SIZE
    )
    # Se mide el mensaje que el usuario tiene delante antes y despues.
    canvas = conversation._scroll.canvas
    canvas.yview_moveto(0.5)
    app.root.update_idletasks()
    height_before = conversation._scroll.body.winfo_reqheight()
    offset_before = canvas.yview()[0] * height_before

    conversation._prepend(older)
    app.root.update_idletasks()

    height_after = conversation._scroll.body.winfo_reqheight()
    offset_after = canvas.yview()[0] * height_after
    added = height_after - height_before

    # El contenido que se estaba mirando debe haberse desplazado exactamente
    # lo que ocupa lo insertado, no mas.
    drift = abs((offset_after - offset_before) - added)
    assert drift <= max(30, height_after * 0.02), (
        f"el viewport se movio {drift:.0f}px de mas al cargar mensajes anteriores"
    )


def test_cambiar_de_chat_invalida_la_carga_anterior(app, session, big_chat):
    """Cambiar de chat sube la generacion: una carga en vuelo se descarta."""
    from app.repository import ChatSummary

    conversation = app.viewer.conversation
    summary = ChatSummary(
        id=big_chat,
        jid=CHAT_JID,
        display_name="Chat grande",
        chat_type="individual",
        last_message=None,
        last_message_timestamp=None,
        message_count=1000,
    )
    page = repo.get_recent_messages(session, big_chat, limit=20)

    conversation.open_chat(summary, page, {})
    generation = conversation._generation

    conversation.open_chat(summary, page, {})
    assert conversation._generation > generation, (
        "abrir otro chat debe invalidar lo que estuviera cargandose"
    )
