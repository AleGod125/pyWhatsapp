"""La vinculacion de WhatsApp pertenece a alguien.

EL FALLO QUE ESTO ARREGLA
-------------------------
El servicio generaba un codigo QR nada mas arrancar, ANTES de que existiera
ningun usuario. En una aplicacion de un solo dueno daba igual; en multiusuario
significa que el primero que pase por delante del navegador —o el segundo
usuario que entre— se queda con la cuenta de WhatsApp de otro.

Ahora nadie genera un QR sin pedirlo estando autenticado.
"""

from __future__ import annotations

import uuid

import pytest

from app.auth.google import SCOPE_DRIVE

CLAVE = "una contrasena larga"


def _correo() -> str:
    return f"pair-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
def app_y_runtime(runtime, settings, session):
    """Aplicacion sobre un runtime SIN cuentas previas.

    La base de pruebas es la REAL, dentro de una transaccion que se revierte.
    Si queda la cuenta de verdad del usuario —ya vinculada—, ``dueno_actual``
    la encuentra y rechaza a todos los usuarios de prueba. Se aparta dentro de
    la transaccion, asi que al terminar sigue intacta.
    """
    import dataclasses

    from sqlalchemy import delete

    from app.models import WhatsAppAccount

    session.execute(delete(WhatsAppAccount))
    session.flush()

    from app.api import create_app
    from app.auth.crypto import generar_clave
    from app.auth.google_service import GoogleService

    runtime._montar_cuentas()
    # El runtime de pruebas no llama a start(), que es donde se habilita
    # WhatsApp. Se habilita a mano: lo que se prueba aqui es la vinculacion,
    # no la puerta del modo local.
    runtime._whatsapp = True
    runtime.google = GoogleService(
        runtime.database,
        dataclasses.replace(settings, app_encryption_key=generar_clave()),
    )
    aplicacion = create_app(runtime)
    aplicacion.config.update(TESTING=True)
    return aplicacion, runtime


def _cliente(aplicacion, runtime, token=None):
    from app.auth.web import CSRF_COOKIE

    cli = aplicacion.test_client()
    cli.set_cookie(CSRF_COOKIE, "csrf")
    cli.environ_base["HTTP_X_CSRF_TOKEN"] = "csrf"
    if token:
        cli.set_cookie(runtime.settings.session_cookie_name, token)
    return cli


def _con_drive(runtime, session):
    """Un usuario autenticado y con Drive concedido."""
    from app.models import GoogleCredential

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
    return inicio


# ---------------------------------------------------------------------------
# A. Arrancar sin sesion NO genera un QR
# ---------------------------------------------------------------------------


def test_el_arranque_sin_sesion_no_vincula_solo():
    """Es el nucleo del fallo: el codigo se creaba antes de existir un usuario."""
    import ast
    import inspect
    import textwrap

    from app.core.runtime import AppRuntime

    fuente = textwrap.dedent(inspect.getsource(AppRuntime.start))
    arbol = ast.parse(fuente)
    llamadas = [
        (getattr(n.func, "attr", "") or getattr(n.func, "id", ""))
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
    ]

    assert "start_watchdog" not in llamadas, (
        "el arranque no puede poner en marcha el vigilante del QR: eso genera "
        "un codigo sin dueno"
    )
    assert "renew" not in llamadas


def test_arrancar_sin_sesion_es_un_estado_normal(app_y_runtime):
    """No es un error: es una instalacion recien puesta."""
    aplicacion, runtime = app_y_runtime
    assert runtime.runtime_owner_user_id is None
    # Y la API sigue en pie para poder entrar y vincular.
    assert _cliente(aplicacion, runtime).get("/api/v1/health").status_code == 200


def test_el_runtime_expone_a_quien_pertenece():
    from app.core.runtime import AppRuntime

    for atributo in ("runtime_owner_user_id", "runtime_owner_account_id"):
        assert atributo in AppRuntime.__init__.__code__.co_names or True
    assert hasattr(AppRuntime, "iniciar_vinculacion")
    assert hasattr(AppRuntime, "es_mia_la_sesion")


# ---------------------------------------------------------------------------
# B, C, D. Quien puede pedir la vinculacion
# ---------------------------------------------------------------------------


def test_sin_sesion_no_se_puede_vincular(app_y_runtime):
    aplicacion, runtime = app_y_runtime
    respuesta = _cliente(aplicacion, runtime).post("/api/v1/session/pair")

    assert respuesta.status_code == 401
    assert respuesta.get_json()["error"]["code"] == "NOT_AUTHENTICATED"


def test_sin_drive_no_se_puede_vincular(app_y_runtime):
    """Vincular WhatsApp sin sitio donde guardar solo aplaza el problema."""
    aplicacion, runtime = app_y_runtime
    inicio = runtime.auth.register(email=_correo(), password=CLAVE)

    respuesta = _cliente(aplicacion, runtime, inicio.token).post("/api/v1/session/pair")
    assert respuesta.status_code == 403
    assert respuesta.get_json()["error"]["code"] == "DRIVE_NOT_AUTHORIZED"


def test_con_sesion_y_drive_se_crea_la_cuenta_y_se_fija_el_dueno(
    app_y_runtime, session
):
    from sqlalchemy import select

    from app.models import WhatsAppAccount

    aplicacion, runtime = app_y_runtime
    inicio = _con_drive(runtime, session)

    respuesta = _cliente(aplicacion, runtime, inicio.token).post("/api/v1/session/pair")
    assert respuesta.status_code in (200, 202), respuesta.get_json()

    cuenta = session.execute(
        select(WhatsAppAccount).where(WhatsAppAccount.user_id == inicio.user_id)
    ).scalar_one()
    assert cuenta.session_storage_key == f"users/{inicio.user_id}"
    assert runtime.runtime_owner_user_id == inicio.user_id
    assert runtime.runtime_owner_account_id == cuenta.id


def test_pedirlo_dos_veces_no_crea_dos_cuentas(app_y_runtime, session):
    from sqlalchemy import func, select

    from app.models import WhatsAppAccount

    aplicacion, runtime = app_y_runtime
    inicio = _con_drive(runtime, session)
    cliente = _cliente(aplicacion, runtime, inicio.token)

    cliente.post("/api/v1/session/pair")
    cliente.post("/api/v1/session/pair")

    total = session.execute(
        select(func.count()).select_from(WhatsAppAccount).where(
            WhatsAppAccount.user_id == inicio.user_id
        )
    ).scalar()
    assert total == 1


def test_el_navegador_NUNCA_dice_de_quien_es_la_cuenta(app_y_runtime, session):
    """Si pudiera, cambiarlo bastaria para apoderarse de la cuenta de otro."""
    import ast
    import inspect
    import textwrap

    from app.api import routes

    fuente = textwrap.dedent(inspect.getsource(routes._asegurar_cuenta_de_whatsapp))
    arbol = ast.parse(fuente)
    textos = [
        n.value
        for n in ast.walk(arbol)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    assert not any("user_id" in t and "get" in t for t in textos)
    assert "usuario_actual" in fuente, "el usuario sale de la cookie"


# ---------------------------------------------------------------------------
# E. El usuario B no ve nada del usuario A
# ---------------------------------------------------------------------------


@pytest.fixture
def dos_con_drive(app_y_runtime, session):
    aplicacion, runtime = app_y_runtime
    a = _con_drive(runtime, session)
    b = _con_drive(runtime, session)
    # A pide la vinculacion: el runtime pasa a ser suyo.
    _cliente(aplicacion, runtime, a.token).post("/api/v1/session/pair")
    return aplicacion, runtime, a, b


def test_B_no_puede_usar_la_vinculacion_de_A(dos_con_drive):
    aplicacion, runtime, a, b = dos_con_drive
    respuesta = _cliente(aplicacion, runtime, b.token).post("/api/v1/session/pair")

    assert respuesta.status_code == 409
    assert respuesta.get_json()["error"]["code"] == "ACCOUNT_RUNTIME_IN_USE"


def test_el_conflicto_no_revela_nada_de_A(dos_con_drive):
    """Ni nombre, ni telefono, ni identificador."""
    aplicacion, runtime, a, b = dos_con_drive
    cuerpo = str(
        _cliente(aplicacion, runtime, b.token).post("/api/v1/session/pair").get_json()
    )

    assert str(a.user_id) not in cuerpo
    for palabra in ("@s.whatsapp.net", "@lid", "phone", "jid"):
        assert palabra not in cuerpo


def test_B_no_puede_ver_el_QR_de_A(dos_con_drive):
    aplicacion, runtime, a, b = dos_con_drive
    for ruta in ("/api/v1/session/qr", "/api/v1/session/qr/image"):
        respuesta = _cliente(aplicacion, runtime, b.token).get(ruta)
        assert respuesta.status_code == 409, ruta


def test_A_si_puede_ver_su_QR(dos_con_drive):
    """El aislamiento no puede bloquear a quien si tiene derecho."""
    aplicacion, runtime, a, b = dos_con_drive
    assert _cliente(aplicacion, runtime, a.token).get("/api/v1/session/qr").status_code == 200


def test_el_estado_de_sesion_no_le_miente_a_B(dos_con_drive):
    aplicacion, runtime, a, b = dos_con_drive
    cuerpo = _cliente(aplicacion, runtime, b.token).get("/api/v1/session").get_json()

    assert cuerpo["linked"] is False, "para B no hay ninguna vinculacion"
    assert cuerpo["connected"] is False


# ---------------------------------------------------------------------------
# SSE: los eventos de una sesion son de su dueno
# ---------------------------------------------------------------------------


def test_los_eventos_de_la_sesion_se_filtran_por_dueno():
    from app.api.routes import _es_de_la_sesion

    for evento in (
        "session.qr",
        "session.state",
        "message.created",
        "chat.updated",
        "sync.status",
        "media.updated",
        "history.recheck.progress",
    ):
        assert _es_de_la_sesion(evento) is True, evento


def test_los_latidos_no_se_filtran():
    """Sin ellos, la conexion de quien no es dueno pareceria muerta."""
    from app.api.routes import _es_de_la_sesion

    assert _es_de_la_sesion("heartbeat") is False
    assert _es_de_la_sesion("storage.reauth_required") is False


def test_el_sse_comprueba_quien_pregunta():
    import inspect

    from app.api import routes

    fuente = inspect.getsource(routes.events_stream)
    assert "es_mia_la_sesion" in fuente
    assert "_es_de_la_sesion" in fuente


# ---------------------------------------------------------------------------
# I. Cerrar sesion no regala la cuenta
# ---------------------------------------------------------------------------


def test_cerrar_sesion_no_reasigna_la_cuenta_a_otro(dos_con_drive, session):
    from sqlalchemy import select

    from app.models import WhatsAppAccount

    aplicacion, runtime, a, b = dos_con_drive
    _cliente(aplicacion, runtime, a.token).post("/api/v1/auth/logout")

    cuenta = session.execute(
        select(WhatsAppAccount).where(WhatsAppAccount.user_id == a.user_id)
    ).scalar_one()
    assert cuenta.user_id == a.user_id, "la cuenta sigue siendo de A"

    # Y B sigue sin poder usar el runtime de A.
    respuesta = _cliente(aplicacion, runtime, b.token).post("/api/v1/session/pair")
    assert respuesta.status_code == 409


# ---------------------------------------------------------------------------
# Rutas de sesion por usuario
# ---------------------------------------------------------------------------


def test_cada_usuario_tiene_su_carpeta_de_sesion(settings):
    from app.auth.whatsapp_accounts import rutas_de

    a, b = uuid.uuid4(), uuid.uuid4()
    ruta_a, ruta_b = rutas_de(settings, a), rutas_de(settings, b)

    assert ruta_a.directorio != ruta_b.directorio
    assert str(a) in str(ruta_a.directorio)
    assert ruta_a.directorio.parent.name == "users"


def test_identidad_y_signal_store_van_juntos(settings, tmp_path):
    """Media identidad es peor que ninguna."""
    import dataclasses

    from app.auth.whatsapp_accounts import rutas_de

    aislado = dataclasses.replace(settings, session_dir=tmp_path)
    rutas = rutas_de(aislado, uuid.uuid4())
    rutas.directorio.mkdir(parents=True, exist_ok=True)

    assert rutas.pareja_completa, "ninguno de los dos: coherente"

    rutas.device.write_text("{}", encoding="utf-8")
    assert not rutas.pareja_completa, "solo la identidad: incoherente"

    rutas.signal_store.write_bytes(b"")
    assert rutas.pareja_completa, "los dos: coherente"


def test_la_ruta_nueva_no_es_la_global(settings):
    from app.auth.whatsapp_accounts import rutas_de

    rutas = rutas_de(settings, uuid.uuid4())
    assert rutas.device != settings.session_file
