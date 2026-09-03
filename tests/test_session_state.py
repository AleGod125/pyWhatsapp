"""Estado de sesion y bloqueo del visor.

El fallo que motiva estos tests: con la sesion revocada, la aplicacion abria
el visor y pintaba 39 chats, y solo despues aparecia
``server <failure> reason=401 -- login rejected``. Mostrar chats mientras
WhatsApp ya ha rechazado la sesion es mentirle al usuario.

Regla verificada aqui: el visor solo se abre cuando el SERVIDOR confirma el
login, y los datos locales nunca se borran al invalidarse una sesion.
"""

from __future__ import annotations

import queue

import pytest

from app.core.session_state import AppState, SessionState

tk = pytest.importorskip("tkinter")


@pytest.fixture
def state() -> SessionState:
    return SessionState()


@pytest.fixture
def app(tk_app):
    """La ventana compartida de la suite (ver ``tk_app`` en conftest)."""
    return tk_app


# ---------------------------------------------------------------------------
# Maquina de estados
# ---------------------------------------------------------------------------


def test_arranca_sin_permitir_el_visor(state):
    assert state.state is AppState.STARTING
    assert state.viewer_allowed is False


@pytest.mark.parametrize(
    "app_state",
    [
        AppState.STARTING,
        AppState.CHECKING_SESSION,
        AppState.CONNECTING,
        AppState.SESSION_INVALID,
        AppState.PAIRING_REQUIRED,
        AppState.DISCONNECTED,
        AppState.ERROR,
    ],
)
def test_solo_connected_permite_el_visor(state, app_state):
    """CONNECTING no basta: el handshake no es la aceptacion del servidor."""
    state.set(app_state)
    assert state.viewer_allowed is False

    state.set(AppState.CONNECTED)
    assert state.viewer_allowed is True


def test_invalidar_la_sesion_avanza_la_generacion(state):
    """Una sesion muerta caduca todo lo que estuviera en vuelo."""
    state.set(AppState.CONNECTED)
    before = state.generation
    assert state.is_current(before)

    state.set(AppState.SESSION_INVALID, reason="401")
    assert state.generation > before
    assert not state.is_current(before), "un resultado viejo debe quedar obsoleto"


def test_los_listeners_reciben_el_cambio(state):
    visto = []
    state.on_change(visto.append)
    state.set(AppState.CONNECTING, reason="prueba")

    assert len(visto) == 1
    assert visto[0].previous is AppState.STARTING
    assert visto[0].current is AppState.CONNECTING
    assert visto[0].reason == "prueba"


def test_un_listener_roto_no_bloquea_el_estado(state):
    def explota(_change):
        raise RuntimeError("listener roto")

    state.on_change(explota)
    state.set(AppState.CONNECTED)
    assert state.state is AppState.CONNECTED


# ---------------------------------------------------------------------------
# TEST A -- sesion valida: el visor abre
# ---------------------------------------------------------------------------


def test_A_sesion_valida_abre_el_visor(app, state):
    app.session_state = state
    state.set(AppState.CONNECTED, reason="<success>")

    app.show_viewer(connected=True)
    app.root.update()

    assert app.viewer.winfo_ismapped() == 1


# ---------------------------------------------------------------------------
# TEST B -- 401: el visor NO abre
# ---------------------------------------------------------------------------


def test_B_login_rechazado_bloquea_el_visor(app, state):
    app.session_state = state
    state.set(AppState.CONNECTED)
    app.show_viewer(connected=True)
    app.root.update()

    # Llega el 401.
    state.set(AppState.SESSION_INVALID, reason="login rechazado 401")
    app.show_status("Esta sesion ya no esta vinculada", "...")
    app.root.update()

    assert app.viewer.winfo_ismapped() == 0, "el visor debe quedar oculto"
    assert app.status.winfo_ismapped() == 1

    # Y un intento posterior de abrirlo no debe prosperar.
    app.show_viewer(connected=False)
    app.root.update()
    assert app.viewer.winfo_ismapped() == 0
    assert state.viewer_allowed is False


# ---------------------------------------------------------------------------
# TEST C -- hay datos locales pero no sesion: no se muestran
# ---------------------------------------------------------------------------


def test_C_datos_locales_sin_sesion_no_se_muestran(app, state, session):
    """PostgreSQL con chats no autoriza a mostrarlos si la sesion murio."""
    from app.services import repository as repo

    repo.upsert_chat(session, jid="34600111222@s.whatsapp.net", chat_type="individual")
    session.flush()
    assert len(repo.list_chat_summaries(session)) >= 1, "hay datos locales"

    app.session_state = state
    state.set(AppState.PAIRING_REQUIRED, reason="sin vinculacion")

    app.show_viewer(connected=False)
    app.root.update()
    assert app.viewer.winfo_ismapped() == 0


# ---------------------------------------------------------------------------
# TEST D -- eventos tardios despues del 401
# ---------------------------------------------------------------------------


def test_D_refresco_tardio_se_ignora(app, state):
    """Un worker lento no puede repintar chats sobre la pantalla de error."""
    app.session_state = state
    state.set(AppState.SESSION_INVALID, reason="401")

    llamadas = []
    original = app.viewer.refresh_chats
    app.viewer.refresh_chats = lambda *a, **k: llamadas.append(1)
    try:
        app.schedule_sidebar_refresh(delay_ms=1)
        app.root.update()
        app.root.after(30, app.root.quit)
        app.root.mainloop()
    finally:
        app.viewer.refresh_chats = original

    assert llamadas == [], "el sidebar no debe refrescarse sin sesion activa"


def test_D_generacion_descarta_resultados_viejos(state):
    state.set(AppState.CONNECTED)
    generacion_del_worker = state.generation

    state.set(AppState.SESSION_INVALID, reason="401")

    # El worker termina AHORA, con su generacion vieja.
    assert not state.is_current(generacion_del_worker)


# ---------------------------------------------------------------------------
# TEST E -- revinculacion
# ---------------------------------------------------------------------------


def test_E_revinculacion_solo_muestra_chats_tras_connected(app, state):
    app.session_state = state

    state.set(AppState.PAIRING_REQUIRED, reason="QR")
    app.show_pairing()
    app.root.update()
    app.show_viewer(connected=False)
    app.root.update()
    assert app.viewer.winfo_ismapped() == 0, "durante el QR no hay chats"

    # paired todavia NO es connected.
    state.set(AppState.CONNECTING, reason="handshake")
    app.show_viewer(connected=False)
    app.root.update()
    assert app.viewer.winfo_ismapped() == 0, "el handshake no basta"

    # Ahora si: el servidor acepto.
    state.set(AppState.CONNECTED, reason="<success>")
    app.show_viewer(connected=True)
    app.root.update()
    assert app.viewer.winfo_ismapped() == 1


# ---------------------------------------------------------------------------
# Los datos locales sobreviven a una sesion invalida
# ---------------------------------------------------------------------------


def test_invalidar_sesion_no_borra_datos(session, state):
    """PostgreSQL es el backup: desvincularse no puede costar mensajes."""
    from app.services import repository as repo
    from app.services.repository import IncomingMessage

    jid = "34600999000@s.whatsapp.net"
    chat_id = repo.upsert_chat(session, jid=jid, chat_type="individual")
    repo.bulk_upsert_messages(
        session,
        {jid: chat_id},
        [
            IncomingMessage(
                chat_jid=jid, timestamp=1_700_000_000, source="live",
                whatsapp_message_id="KEEP1", text="hola",
            )
        ],
    )
    session.flush()
    antes = repo.count_messages(session, jid)

    state.set(AppState.SESSION_INVALID, reason="401")
    state.set(AppState.PAIRING_REQUIRED, reason="sesion archivada")

    assert repo.count_messages(session, jid) == antes == 1
