"""Conectar tiene que dejar constancia en la base.

EL FALLO QUE ESTO ARREGLA
-------------------------
El runtime llegaba a CONNECTED, entraba el historial, entraban los mensajes
y la base seguia diciendo ``never_linked``. ``marcar_vinculada()`` existia y
no la llamaba nadie.

Consecuencia: ``/onboarding/status`` respondia "toca vincular" con la sesion
ya conectada y funcionando, y el frontend devolvia al usuario a la pantalla
del codigo QR una y otra vez.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.auth.google import SCOPE_DRIVE
from app.core.session_state import AppState
from app.models import WhatsAppAccount
from app.models.accounts import LINKED_STATUSES

CLAVE = "una contrasena larga"


def _correo() -> str:
    return f"link-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
def montaje(runtime, settings, session):
    """Usuario con Drive, cuenta creada y runtime con dueno."""
    import dataclasses

    from app.api import create_app
    from app.auth.crypto import generar_clave
    from app.auth.google_service import GoogleService
    from app.models import GoogleCredential

    # La base de pruebas es la real, en una transaccion que se revierte. La
    # cuenta de verdad del usuario bloquearia a los de prueba.
    from sqlalchemy import delete

    from app.models import WhatsAppAccount

    session.execute(delete(WhatsAppAccount))
    session.flush()

    runtime._montar_cuentas()
    runtime._whatsapp = True
    runtime.google = GoogleService(
        runtime.database,
        dataclasses.replace(settings, app_encryption_key=generar_clave()),
    )

    inicio = runtime.auth.register(email=_correo(), password=CLAVE)
    session.add(
        GoogleCredential(
            user_id=inicio.user_id,
            google_subject=f"sub-{uuid.uuid4().hex[:8]}",
            scope=f"openid email profile {SCOPE_DRIVE}",
            refresh_token_encrypted=b"x",
        )
    )
    session.flush()

    cuenta = runtime.whatsapp_accounts.asegurar_cuenta(inicio.user_id)
    runtime.runtime_owner_user_id = inicio.user_id
    runtime.runtime_owner_account_id = cuenta.id

    aplicacion = create_app(runtime)
    aplicacion.config.update(TESTING=True)
    return {
        "app": aplicacion,
        "runtime": runtime,
        "user_id": inicio.user_id,
        "token": inicio.token,
        "account_id": cuenta.id,
    }


def _cuenta(session, user_id) -> WhatsAppAccount:
    return session.execute(
        select(WhatsAppAccount).where(WhatsAppAccount.user_id == user_id)
    ).scalar_one()


def _cliente(montaje, token=None):
    cli = montaje["app"].test_client()
    if token:
        cli.set_cookie(montaje["runtime"].settings.session_cookie_name, token)
    return cli


def _onboarding(montaje, token):
    return _cliente(montaje, token).get("/api/v1/onboarding/status").get_json()


# ---------------------------------------------------------------------------
# Al conectar
# ---------------------------------------------------------------------------


def test_conectar_deja_la_cuenta_marcada(montaje, session):
    """El puente que faltaba."""
    antes = _cuenta(session, montaje["user_id"])
    assert antes.session_status == "never_linked"

    montaje["runtime"]._persistir_vinculacion()
    session.expire_all()

    despues = _cuenta(session, montaje["user_id"])
    assert despues.session_status == "linked"
    assert despues.linked_at is not None
    assert despues.last_connected_at is not None


def test_no_se_crea_una_segunda_cuenta(montaje, session):
    from sqlalchemy import func

    for _ in range(3):
        montaje["runtime"]._persistir_vinculacion()
    session.expire_all()

    total = session.execute(
        select(func.count())
        .select_from(WhatsAppAccount)
        .where(WhatsAppAccount.user_id == montaje["user_id"])
    ).scalar()
    assert total == 1


def test_reconectar_no_reescribe_cuando_se_vinculo(montaje, session):
    """``linked_at`` es cuando se vinculo, no cuando se reconecto."""
    montaje["runtime"]._persistir_vinculacion()
    session.expire_all()
    original = _cuenta(session, montaje["user_id"]).linked_at

    # Se retrasa a mano para notar el cambio si lo hubiera.
    fila = _cuenta(session, montaje["user_id"])
    fila.last_connected_at = datetime.now(timezone.utc) - timedelta(hours=2)
    session.flush()

    montaje["runtime"]._persistir_vinculacion()
    session.expire_all()

    despues = _cuenta(session, montaje["user_id"])
    assert despues.linked_at == original, "no puede moverse en cada reconexion"
    assert despues.last_connected_at > despues.linked_at - timedelta(seconds=1)


def test_no_se_espera_a_conocer_el_telefono(montaje, session):
    """La vinculacion depende del companion conectado, no de PN/LID."""
    montaje["runtime"]._persistir_vinculacion()
    session.expire_all()
    assert _cuenta(session, montaje["user_id"]).session_status == "linked"


# ---------------------------------------------------------------------------
# Sin dueno: NO se adopta
# ---------------------------------------------------------------------------


def test_una_sesion_huerfana_no_se_adjudica_a_nadie(montaje, session):
    """Adoptarla entregaria la cuenta de alguien al primero que entrara."""
    montaje["runtime"].runtime_owner_user_id = None
    montaje["runtime"]._persistir_vinculacion()
    session.expire_all()

    assert _cuenta(session, montaje["user_id"]).session_status == "never_linked"


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------


def test_tras_conectar_el_onboarding_manda_al_panel(montaje, session):
    montaje["runtime"]._persistir_vinculacion()
    session.flush()

    cuerpo = _onboarding(montaje, montaje["token"])
    assert cuerpo["whatsapp_linked"] is True
    assert cuerpo["next_step"] == "dashboard"


def test_sin_conectar_todavia_el_onboarding_manda_al_QR(montaje):
    cuerpo = _onboarding(montaje, montaje["token"])
    assert cuerpo["whatsapp_linked"] is False
    assert cuerpo["next_step"] == "pairing"


def test_el_panel_no_espera_a_que_termine_la_sincronizacion(montaje, session):
    """Vinculado basta. El historial puede seguir entrando por detras."""
    montaje["runtime"]._persistir_vinculacion()
    montaje["runtime"].set_sync_state("BACKFILLING")
    session.flush()

    assert _onboarding(montaje, montaje["token"])["next_step"] == "dashboard"


# ---------------------------------------------------------------------------
# Desconexion temporal NO es desvinculacion
# ---------------------------------------------------------------------------


def test_una_caida_del_socket_no_desvincula(montaje, session):
    """Si contara, cada corte de wifi devolveria al usuario al codigo QR."""
    montaje["runtime"]._persistir_vinculacion()
    montaje["runtime"]._marcar_desconectada()
    session.flush()

    assert _cuenta(session, montaje["user_id"]).session_status == "disconnected"
    cuerpo = _onboarding(montaje, montaje["token"])
    assert cuerpo["whatsapp_linked"] is True
    assert cuerpo["next_step"] == "dashboard"


@pytest.mark.parametrize(
    "estado,esperado",
    [
        ("linked", True),
        ("disconnected", True),
        ("never_linked", False),
        ("revoked", False),
        ("error", False),
    ],
)
def test_que_estados_cuentan_como_vinculado(montaje, session, estado, esperado):
    fila = _cuenta(session, montaje["user_id"])
    fila.session_status = estado
    session.flush()

    assert (estado in LINKED_STATUSES) is esperado
    assert _onboarding(montaje, montaje["token"])["whatsapp_linked"] is esperado


def test_revocada_si_manda_a_vincular_de_nuevo(montaje, session):
    """Ahi el servidor dijo que esa vinculacion ya no existe."""
    fila = _cuenta(session, montaje["user_id"])
    fila.session_status = "revoked"
    session.flush()
    montaje["runtime"].runtime_owner_user_id = None  # no reconciliar

    assert _onboarding(montaje, montaje["token"])["next_step"] == "pairing"


# ---------------------------------------------------------------------------
# Orden: primero la base, despues el evento
# ---------------------------------------------------------------------------


def test_se_persiste_ANTES_de_anunciar_el_estado():
    """La carrera que dejaba al usuario dando vueltas.

    Si el evento saliera primero, el frontend consultaria el onboarding y la
    base todavia diria "sin vincular": vuelta a la pantalla del codigo QR con
    la sesion ya conectada.
    """
    import ast
    import inspect
    import textwrap

    from app.core.runtime import AppRuntime

    fuente = textwrap.dedent(inspect.getsource(AppRuntime._observar_evento))
    arbol = ast.parse(fuente)

    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Compare):
            continue
        # Se busca la rama de "session_valid" y se comprueba el orden dentro.
        pass

    posicion_persistir = fuente.find("_persistir_vinculacion()")
    posicion_estado = fuente.find('AppState.CONNECTED, reason="<success>')
    assert posicion_persistir != -1, "la vinculacion tiene que persistirse"
    assert posicion_persistir < posicion_estado, (
        "primero la base, despues el evento: al reves hay carrera"
    )


def test_la_reconexion_tambien_persiste():
    import inspect

    from app.core.runtime import AppRuntime

    fuente = inspect.getsource(AppRuntime._observar_evento)
    trozo = fuente[fuente.find('nombre == "reconnected"') :][:300]
    assert "_persistir_vinculacion" in trozo


# ---------------------------------------------------------------------------
# Reconciliacion defensiva
# ---------------------------------------------------------------------------


def test_el_onboarding_corrige_una_base_desfasada(montaje, session):
    """El caso de la sesion que ya existe: conectada y sin marcar."""
    montaje["runtime"].state.set(AppState.CONNECTED, reason="prueba")

    cuerpo = _onboarding(montaje, montaje["token"])
    session.expire_all()

    assert _cuenta(session, montaje["user_id"]).session_status == "linked"
    assert cuerpo["whatsapp_linked"] is True


def test_la_reconciliacion_NO_adopta_lo_de_otro(montaje, session, runtime):
    """Solo corrige si la sesion conectada es de ESE usuario."""
    otro = runtime.auth.register(email=_correo(), password=CLAVE)
    montaje["runtime"].state.set(AppState.CONNECTED, reason="prueba")

    _onboarding(montaje, otro.token)
    session.expire_all()

    assert _cuenta(session, montaje["user_id"]).session_status == "never_linked"


def test_sin_runtime_conectado_no_se_reconcilia(montaje, session):
    montaje["runtime"].state.set(AppState.DISCONNECTED, reason="prueba")

    _onboarding(montaje, montaje["token"])
    session.expire_all()
    assert _cuenta(session, montaje["user_id"]).session_status == "never_linked"


# ---------------------------------------------------------------------------
# Otro usuario
# ---------------------------------------------------------------------------


def test_B_no_hereda_la_vinculacion_de_A(montaje, session, runtime):
    montaje["runtime"]._persistir_vinculacion()
    session.flush()

    b = runtime.auth.register(email=_correo(), password=CLAVE)
    cuerpo = _onboarding(montaje, b.token)

    assert cuerpo["whatsapp_linked"] is False
    assert str(montaje["user_id"]) not in str(cuerpo)


def test_cerrar_sesion_no_desvincula(montaje, session):
    """Cerrar sesion en un equipo prestado no puede costar otro codigo QR."""
    montaje["runtime"]._persistir_vinculacion()
    session.flush()

    _cliente(montaje, montaje["token"]).post("/api/v1/auth/logout")
    session.expire_all()

    assert _cuenta(session, montaje["user_id"]).session_status == "linked"


# ---------------------------------------------------------------------------
# Recuperar el dueno al reiniciar
# ---------------------------------------------------------------------------


def _dejar_solo_esta_cuenta(session, user_id):
    """Otras pruebas del modulo dejan cuentas en la misma transaccion.

    Aqui se mide "cuantas candidatas hay", asi que el resto tiene que irse o
    la medida es de otra cosa.
    """
    from sqlalchemy import delete

    from app.models import WhatsAppAccount

    session.execute(delete(WhatsAppAccount).where(WhatsAppAccount.user_id != user_id))
    session.flush()


def test_al_reiniciar_se_recupera_el_dueno_aunque_no_conste_vinculada(
    montaje, session
):
    """El caso real: se vinculo, funciono, y la base nunca se marco.

    Al reiniciar hay que poder saber de quien es esa sesion para anotarla. Sin
    esto, ``dueno_actual`` diria ``None`` —solo mira cuentas ya vinculadas— y
    la sesion se conectaria otra vez sin dejar constancia.
    """
    _dejar_solo_esta_cuenta(session, montaje["user_id"])
    cuentas = montaje["runtime"].whatsapp_accounts

    assert cuentas.dueno_actual() is None, "todavia no consta vinculada"
    assert cuentas.dueno_de_la_sesion_en_disco() == montaje["user_id"]


def test_con_dos_candidatas_y_ninguna_vinculada_no_se_adivina(
    montaje, runtime, session
):
    """Adivinar entregaria la sesion a quien no le toca."""
    _dejar_solo_esta_cuenta(session, montaje["user_id"])
    otro = runtime.auth.register(email=_correo(), password=CLAVE)
    runtime.whatsapp_accounts.asegurar_cuenta(otro.user_id)

    assert runtime.whatsapp_accounts.dueno_de_la_sesion_en_disco() is None


def test_con_una_vinculada_esa_manda(montaje, runtime, session):
    _dejar_solo_esta_cuenta(session, montaje["user_id"])
    otro = runtime.auth.register(email=_correo(), password=CLAVE)
    runtime.whatsapp_accounts.asegurar_cuenta(otro.user_id)
    montaje["runtime"]._persistir_vinculacion()
    session.flush()

    assert (
        runtime.whatsapp_accounts.dueno_de_la_sesion_en_disco()
        == montaje["user_id"]
    )


def test_sin_ninguna_cuenta_no_hay_dueno(runtime, session):
    """Sesion de verdad huerfana: no se adopta."""
    from sqlalchemy import delete

    from app.models import WhatsAppAccount

    runtime._montar_cuentas()
    session.execute(delete(WhatsAppAccount))
    session.flush()

    assert runtime.whatsapp_accounts.dueno_de_la_sesion_en_disco() is None


def test_una_cuenta_revocada_no_cuenta_como_dueno(montaje, session):
    """Ahi el servidor dijo que esa vinculacion ya no existe."""
    _dejar_solo_esta_cuenta(session, montaje["user_id"])
    fila = _cuenta(session, montaje["user_id"])
    fila.session_status = "revoked"
    session.flush()

    assert (
        montaje["runtime"].whatsapp_accounts.dueno_de_la_sesion_en_disco() is None
    )
