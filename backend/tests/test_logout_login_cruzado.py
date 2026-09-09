"""Entrar, salir y volver a entrar. Por HTTP, con las rutas de verdad.

EL ESCENARIO, TAL Y COMO SE PIDIO
---------------------------------
::

    login A   -> panel de A
    logout A
    login B   -> vincular  (NUNCA el panel de A)
    vincula B -> panel de B
    logout B
    login A   -> panel de A, SIN codigo QR
    logout A
    login B   -> panel de B, SIN codigo QR

Todo con el MISMO proceso: nada de reiniciar el servicio entre medias.

LO QUE SE ESTA COMPROBANDO DE VERDAD
------------------------------------
Que cerrar la sesion web NO es desvincular WhatsApp. Son dos cosas distintas y
mezclarlas costaria volver a escanear un codigo cada vez que alguien cierra
sesion en un ordenador compartido.

La membresia basta para volver: no hace falta ni el runtime en pie, ni el
socket conectado, ni acordarse de quien fue el ultimo.
"""

from __future__ import annotations

import uuid

import pytest

from app.auth import memberships

CLAVE = "una contrasena bastante larga"


@pytest.fixture
def cliente_web(runtime, session):
    """Un navegador: guarda cookies entre peticiones, como el de verdad."""
    from app.api import create_app

    runtime._montar_cuentas()
    aplicacion = create_app(runtime)
    aplicacion.config.update(TESTING=True)
    return aplicacion.test_client()


def _correo(quien: str) -> str:
    return f"{quien}-{uuid.uuid4().hex[:8]}@gmail.com"


def _registrar(runtime, session, correo: str):
    """Alta con Drive concedido: el emparejamiento lo exige."""
    from tests.conftest import _conceder_drive

    inicio = runtime.auth.register(email=correo, password=CLAVE)
    _conceder_drive(session, inicio.user_id)
    session.flush()
    return inicio


def _con_whatsapp(session, user_id):
    """Como queda una cuenta despues de escanear el codigo: vinculada."""
    from app.models import WhatsAppAccount

    id_cuenta = uuid.uuid4()
    cuenta = WhatsAppAccount(
        id=id_cuenta,
        user_id=user_id,
        session_status="linked",
        session_storage_key=f"accounts/{id_cuenta}",
    )
    session.add(cuenta)
    session.flush()
    memberships.conceder(session, user_id=user_id, account_id=cuenta.id)
    session.flush()
    return cuenta


def _entrar(cliente, correo: str):
    return cliente.post(
        "/api/v1/auth/login", json={"email": correo, "password": CLAVE}
    )


def _csrf(cliente) -> str:
    """El token CSRF de la cookie, como haria el navegador.

    No se esquiva la comprobacion: se cumple. Saltarsela en la prueba dejaria
    sin cubrir que el logout de verdad funciona desde el frontend.
    """
    from app.auth.web import CSRF_COOKIE

    galleta = cliente.get_cookie(CSRF_COOKIE)
    return galleta.value if galleta is not None else ""


def _salir(cliente):
    from app.auth.web import CSRF_HEADER

    return cliente.post(
        "/api/v1/auth/logout", headers={CSRF_HEADER: _csrf(cliente)}
    )


def _siguiente_paso(cliente) -> str:
    return cliente.get("/api/v1/onboarding/status").get_json()["next_step"]


# ---------------------------------------------------------------------------
# El recorrido entero, en orden
# ---------------------------------------------------------------------------


def test_el_recorrido_completo_de_los_dos(runtime, session, cliente_web):
    """A vincula, sale; B entra y NO hereda nada; los dos vuelven sin QR."""
    correo_a = _correo("viloria")
    correo_b = _correo("navarro")
    a = _registrar(runtime, session, correo_a)
    b = _registrar(runtime, session, correo_b)

    # -- A vincula su WhatsApp y entra al panel ----------------------------
    _con_whatsapp(session, a.user_id)
    assert _entrar(cliente_web, correo_a).status_code == 200
    assert _siguiente_paso(cliente_web) == "dashboard"

    # -- A cierra sesion ---------------------------------------------------
    assert _salir(cliente_web).status_code == 200

    # -- B entra: NO hereda la vinculacion de A ----------------------------
    assert _entrar(cliente_web, correo_b).status_code == 200
    assert _siguiente_paso(cliente_web) == "pairing", (
        "B no tiene WhatsApp: le toca vincular, no el panel de A"
    )
    sesion_b = cliente_web.get("/api/v1/session").get_json()
    assert sesion_b["linked"] is False
    assert sesion_b["connected"] is False
    assert sesion_b["pairing_required"] is True

    # -- B vincula el suyo -------------------------------------------------
    _con_whatsapp(session, b.user_id)
    assert _siguiente_paso(cliente_web) == "dashboard"
    assert _salir(cliente_web).status_code == 200

    # -- A vuelve: al panel, y SIN codigo QR -------------------------------
    assert _entrar(cliente_web, correo_a).status_code == 200
    assert _siguiente_paso(cliente_web) == "dashboard"
    assert cliente_web.get("/api/v1/session").get_json()["qr_available"] is False
    assert _salir(cliente_web).status_code == 200

    # -- Y B tambien --------------------------------------------------------
    assert _entrar(cliente_web, correo_b).status_code == 200
    assert _siguiente_paso(cliente_web) == "dashboard"
    assert cliente_web.get("/api/v1/session").get_json()["qr_available"] is False


# ---------------------------------------------------------------------------
# Cerrar sesion web NO desvincula WhatsApp
# ---------------------------------------------------------------------------


def test_cerrar_sesion_no_desvincula_ni_borra_nada(runtime, session, cliente_web):
    from sqlalchemy import func, select

    from app.models import Chat, Message, UserWhatsAppMembership, WhatsAppAccount

    correo = _correo("viloria")
    inicio = _registrar(runtime, session, correo)
    cuenta = _con_whatsapp(session, inicio.user_id)

    chat = Chat(
        jid=f"5730{uuid.uuid4().hex[:8]}@s.whatsapp.net",
        chat_type="individual",
        whatsapp_account_id=cuenta.id,
    )
    session.add(chat)
    session.flush()
    session.add(
        Message(
            chat_id=chat.id,
            chat_jid=chat.jid,
            whatsapp_message_id="3A1F8BDD4678EB6DE395",
            timestamp=1_760_000_000,
            from_me=False,
            message_type="text",
            source="live",
        )
    )
    session.flush()

    def _foto():
        fila = session.get(WhatsAppAccount, cuenta.id)
        return (
            fila.session_status,
            fila.session_storage_key,
            session.execute(
                select(func.count()).select_from(UserWhatsAppMembership)
                .where(UserWhatsAppMembership.whatsapp_account_id == cuenta.id)
            ).scalar(),
            session.execute(
                select(func.count()).select_from(Chat)
                .where(Chat.whatsapp_account_id == cuenta.id)
            ).scalar(),
            session.execute(
                select(func.count()).select_from(Message)
                .where(Message.chat_id == chat.id)
            ).scalar(),
        )

    _entrar(cliente_web, correo)
    antes = _foto()
    assert _salir(cliente_web).status_code == 200
    session.expire_all()

    assert _foto() == antes, "cerrar sesion web no puede tocar nada de WhatsApp"
    assert antes[0] == "linked"


def test_el_logout_no_pide_QR_la_proxima_vez(runtime, session, cliente_web):
    """Logout de Google NO es revinculacion de WhatsApp."""
    correo = _correo("viloria")
    inicio = _registrar(runtime, session, correo)
    _con_whatsapp(session, inicio.user_id)

    for _ in range(3):
        _entrar(cliente_web, correo)
        assert _siguiente_paso(cliente_web) == "dashboard"
        cuerpo = cliente_web.get("/api/v1/session").get_json()
        assert cuerpo["qr_available"] is False
        assert cuerpo["pairing_required"] is False
        _salir(cliente_web)


# ---------------------------------------------------------------------------
# Sin sesion web no se ve nada
# ---------------------------------------------------------------------------


def test_despues_de_salir_ya_no_se_puede_leer(runtime, session, cliente_web):
    correo = _correo("viloria")
    inicio = _registrar(runtime, session, correo)
    _con_whatsapp(session, inicio.user_id)

    _entrar(cliente_web, correo)
    assert cliente_web.get("/api/v1/chats").status_code == 200

    _salir(cliente_web)
    assert cliente_web.get("/api/v1/chats").status_code == 401


def test_sin_cuenta_las_rutas_de_whatsapp_contestan_que_falta_vincular(
    runtime, session, cliente_web
):
    """Nunca el runtime de otro: "no tienes vinculacion" es la respuesta."""
    correo = _correo("navarro")
    _registrar(runtime, session, correo)
    _entrar(cliente_web, correo)

    for ruta in ("/api/v1/session/qr", "/api/v1/session/qr/image"):
        respuesta = cliente_web.get(ruta)
        assert respuesta.status_code == 409, ruta
        assert respuesta.get_json()["error"]["code"] == "PAIRING_REQUIRED", ruta


def test_el_estado_de_quien_no_tiene_cuenta_no_lleva_nada_de_otro(
    runtime, session, cliente_web
):
    """Ni el estado de la sesion ajena, ni su generacion de QR, ni su fase."""
    correo_a = _correo("viloria")
    a = _registrar(runtime, session, correo_a)
    _con_whatsapp(session, a.user_id)

    correo_b = _correo("navarro")
    _registrar(runtime, session, correo_b)
    _entrar(cliente_web, correo_b)

    cuerpo = cliente_web.get("/api/v1/session").get_json()

    assert cuerpo["state"] == "PAIRING_REQUIRED"
    assert cuerpo["linked"] is False
    assert cuerpo["connected"] is False
    assert cuerpo["qr_available"] is False
    assert cuerpo["generation"] == 0
    assert cuerpo["session_file_present"] is False
    assert cuerpo["pairing_phase"] == "pairing_required"
    # Lo del PROCESO si viaja: es cierto para todo el mundo y el frontend lo
    # necesita para saber si este backend trae WhatsApp.
    assert "whatsapp_enabled" in cuerpo
    assert "session_owner_pid" in cuerpo
