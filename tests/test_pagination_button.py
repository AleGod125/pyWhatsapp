"""Boton de paginacion, estados terminales de multimedia y capability.

Cubre las tareas A, B y C de la fase de consolidacion.
"""

from __future__ import annotations

import queue

import pytest

tk = pytest.importorskip("tkinter")

from app.services import repository as repo  # noqa: E402
from app.services.repository import IncomingMessage  # noqa: E402

CHAT_JID = "34600555444@s.whatsapp.net"
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
def chat_452(session):
    """El caso real: VirtualTec Marco con 452 mensajes almacenados."""
    chat_id = repo.upsert_chat(session, jid=CHAT_JID, chat_type="individual")
    repo.bulk_upsert_messages(
        session,
        {CHAT_JID: chat_id},
        [
            IncomingMessage(
                chat_jid=CHAT_JID, timestamp=1_754_000_000 + i * 60,
                source="on_demand", whatsapp_message_id=f"BTN{i:04d}",
                text=f"m{i}", message_type="text",
            )
            for i in range(TOTAL)
        ],
    )
    session.flush()
    return chat_id


@pytest.fixture
def sin_scroll_automatico(monkeypatch):
    """Apaga el prefetch para poder medir el boton en aislamiento."""
    import app.gui.chat_view as chat_view

    monkeypatch.setattr(chat_view, "AUTO_PREFETCH", False)


# ---------------------------------------------------------------------------
# A -- paginacion manual contra PostgreSQL
# ---------------------------------------------------------------------------


def test_A_progresion_200_400_452(app, session, chat_452, sin_scroll_automatico):
    """200 -> 400 -> 452 PULSANDO el boton, y despues se agota.

    Aqui se prueba el respaldo manual, asi que el scroll automatico se apaga:
    con el visor mapeado cargaria paginas por su cuenta y ya no se estaria
    midiendo lo que pulsa el usuario.
    """
    from app.gui.chat_view import LOAD_DONE_LABEL, PAGE_SIZE
    from app.services.repository import ChatSummary

    stats = repo.get_chat_stats(session, chat_452)
    assert stats.total == TOTAL

    summary = ChatSummary(
        id=chat_452, jid=CHAT_JID, display_name="VirtualTec Marco",
        chat_type="individual", last_message=None,
        last_message_timestamp=None, message_count=TOTAL,
    )
    page = repo.get_recent_messages(session, chat_452, limit=PAGE_SIZE)

    conversation = app.viewer.conversation
    conversation.bind_loader(
        lambda cid, ts, mid, limit: repo.get_messages_before(session, cid, ts, mid, limit)
    )
    conversation.open_chat(summary, page, {}, {}, stats)
    app.root.update()
    assert conversation._rendered == 200

    assert conversation.load_previous_page() == 200
    app.root.update()
    assert conversation._rendered == 400

    assert conversation.load_previous_page() == 52
    app.root.update()
    assert conversation._rendered == TOTAL

    # Ya no queda nada local: el boton queda deshabilitado.
    assert conversation.load_previous_page() == 0
    app.root.update()
    assert conversation._exhausted is True
    assert conversation._load_button.cget("text") == LOAD_DONE_LABEL


def test_A_el_boton_no_contacta_con_whatsapp(app, session, chat_452):
    """El boton SOLO pagina PostgreSQL. Nunca dispara ON_DEMAND."""
    from app.gui.chat_view import PAGE_SIZE
    from app.services.repository import ChatSummary

    llamadas: list[tuple] = []

    def loader(cid, ts, mid, limit):
        llamadas.append((cid, ts, mid, limit))
        return repo.get_messages_before(session, cid, ts, mid, limit)

    summary = ChatSummary(
        id=chat_452, jid=CHAT_JID, display_name="X", chat_type="individual",
        last_message=None, last_message_timestamp=None, message_count=TOTAL,
    )
    conversation = app.viewer.conversation
    conversation.bind_loader(loader)
    conversation.open_chat(
        summary, repo.get_recent_messages(session, chat_452, limit=PAGE_SIZE),
        {}, {}, repo.get_chat_stats(session, chat_452),
    )
    conversation.load_previous_page()

    assert len(llamadas) == 1, "debe consultar la base exactamente una vez"
    # El loader es el de PostgreSQL: no hay ninguna via a WhatsApp aqui.


def test_A_boton_y_scroll_comparten_implementacion():
    """Una sola funcion: el scroll llamara a la misma que el boton."""
    import inspect

    from app.gui.chat_view import ConversationPanel

    fuente = inspect.getsource(ConversationPanel._maybe_prefetch)
    assert "load_previous_page" in fuente


# ---------------------------------------------------------------------------
# B -- estados terminales de multimedia
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mensaje,esperado",
    [
        ("HTTP Error 404: Not Found", "unavailable"),
        ("HTTP Error 410: Gone", "expired"),
        ("Connection reset by peer", "failed"),
        ("timed out", "failed"),
    ],
)
def test_B_estados_terminales(settings, database, mensaje, esperado):
    from app.services.media_service import MediaService

    service = MediaService(settings, database, client=None)
    registrados: list[tuple] = []
    service._fail = lambda mid, status, msg: registrados.append((status, msg))

    service._classify_failure(1, RuntimeError(mensaje))
    assert registrados[0][0] == esperado


def test_B_terminales_no_se_reintentan(settings, database, session):
    """404/410 son definitivos: no vuelven a la cola en el siguiente arranque."""
    import inspect

    from app.services.media_service import MediaService

    fuente = inspect.getsource(MediaService.pending_ids)
    assert '"pending", "failed"' in fuente
    assert "unavailable" not in fuente.split("MediaFile.download_status.in_")[1][:60]


def test_B_las_estadisticas_distinguen_caducado_de_no_disponible():
    from app.services.media_service import MediaStats

    stats = MediaStats(downloaded=10, unavailable=3, expired=2, failed=1, deduplicated=4)
    assert stats.processed == 20
    texto = str(stats)
    assert "no disponibles=3" in texto and "caducados=2" in texto


# ---------------------------------------------------------------------------
# C -- capability ligada a la sesion
# ---------------------------------------------------------------------------


def test_C_capability_sin_sesion_no_se_confirma(settings, database):
    from app.services.backfill_service import BackfillService

    service = BackfillService(settings, database)
    service._client = None
    assert service.session_fingerprint() is None
    assert service.capability_confirmed() is False


def test_C_el_fingerprint_cambia_al_revincular(settings, database):
    from types import SimpleNamespace

    from app.services.backfill_service import BackfillService

    service = BackfillService(settings, database)

    def device(user, device_id):
        return SimpleNamespace(
            jid=SimpleNamespace(user=user, server="s.whatsapp.net"), device_id=device_id
        )

    service._client = SimpleNamespace(device=device("573002389304", 75))
    primera = service.session_fingerprint()

    # Misma cuenta, otro pairing -> otro device_id -> otra huella.
    service._client = SimpleNamespace(device=device("573002389304", 80))
    assert service.session_fingerprint() != primera

    # El fingerprint no puede contener el identificador en claro.
    assert "573002389304" not in primera
