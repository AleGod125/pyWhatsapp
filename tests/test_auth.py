"""Cuentas, sesiones web y aislamiento entre usuarios.

Lo que se fija aqui no es comodidad: es que la copia de una persona no llegue
a otra. Un fallo en cualquiera de estas pruebas significa que alguien puede
leer los mensajes de alguien.
"""

from __future__ import annotations

import uuid

import pytest

from app.auth.crypto import hash_de_token, nuevo_token_de_sesion
from app.auth.passwords import PasswordHasherService, validar_password
from app.auth.service import AuthError, AuthService


def _correo() -> str:
    return f"u-{uuid.uuid4().hex[:10]}@example.com"


CLAVE = "una contrasena larga"


@pytest.fixture
def auth(settings, session):
    return AuthService(_Shim(session), settings)


class _Shim:
    """Base que reutiliza la transaccion de la prueba (siempre se revierte)."""

    def __init__(self, session):
        self._session = session

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


# ---------------------------------------------------------------------------
# Contrasenas
# ---------------------------------------------------------------------------


def test_la_contrasena_no_se_guarda_en_claro():
    hasher = PasswordHasherService()
    guardado = hasher.hash(CLAVE)
    assert CLAVE not in guardado
    assert guardado.startswith("$argon2id$"), "tiene que ser Argon2id"


def test_una_contrasena_correcta_se_reconoce():
    hasher = PasswordHasherService()
    assert hasher.verify(hasher.hash(CLAVE), CLAVE).valida is True


def test_una_contrasena_incorrecta_se_rechaza():
    hasher = PasswordHasherService()
    assert hasher.verify(hasher.hash(CLAVE), "otra cosa distinta").valida is False


def test_dos_hashes_de_la_misma_clave_son_distintos():
    """Cada uno lleva su sal: dos cuentas con la misma clave no se delatan."""
    hasher = PasswordHasherService()
    assert hasher.hash(CLAVE) != hasher.hash(CLAVE)


def test_sin_contrasena_guardada_no_entra_nadie():
    """Cuenta creada con Google. Ninguna cadena puede validar contra NULL."""
    hasher = PasswordHasherService()
    for intento in ("", "None", "null", CLAVE):
        assert hasher.verify(None, intento).valida is False


def test_un_hash_corrupto_no_tumba_el_login():
    hasher = PasswordHasherService()
    assert hasher.verify("no soy un hash", CLAVE).valida is False


@pytest.mark.parametrize("mala", ["", "corta", "1234567"])
def test_una_contrasena_demasiado_corta_se_rechaza(mala):
    assert validar_password(mala) is not None


def test_se_prioriza_la_longitud_sobre_los_simbolos():
    """Una frase larga sin simbolos vale. ``Password1!`` es peor y no lo parece."""
    assert validar_password("caballo correcto grapa") is None


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------


def test_registrarse_crea_la_cuenta_y_abre_sesion(auth, session):
    from app.models import User

    correo = _correo()
    inicio = auth.register(email=correo, password=CLAVE, display_name="Ana")

    usuario = session.get(User, inicio.user_id)
    assert usuario.email == correo
    assert usuario.auth_provider == "local"
    assert auth.resolver(inicio.token).id == inicio.user_id


def test_el_correo_no_distingue_mayusculas(auth):
    """Sin CITEXT, Ana@x.com y ana@x.com serian dos cuentas distintas."""
    correo = _correo()
    auth.register(email=correo, password=CLAVE)
    with pytest.raises(AuthError) as fallo:
        auth.register(email=correo.upper(), password=CLAVE)
    assert fallo.value.code == "EMAIL_TAKEN"


def test_un_correo_repetido_se_rechaza(auth):
    correo = _correo()
    auth.register(email=correo, password=CLAVE)
    with pytest.raises(AuthError) as fallo:
        auth.register(email=correo, password=CLAVE)
    assert fallo.value.status == 409


@pytest.mark.parametrize("malo", ["sinarroba", "a@b", "@x.com", "", "a b@x.com"])
def test_un_correo_invalido_se_rechaza(auth, malo):
    with pytest.raises(AuthError) as fallo:
        auth.register(email=malo, password=CLAVE)
    assert fallo.value.code == "INVALID_EMAIL"


def test_una_contrasena_debil_no_crea_cuenta(auth):
    with pytest.raises(AuthError) as fallo:
        auth.register(email=_correo(), password="corta")
    assert fallo.value.code == "WEAK_PASSWORD"


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def test_login_correcto_abre_sesion(auth):
    correo = _correo()
    auth.register(email=correo, password=CLAVE)
    inicio = auth.login(email=correo, password=CLAVE)
    assert auth.resolver(inicio.token) is not None


def test_login_con_contrasena_mala_falla(auth):
    correo = _correo()
    auth.register(email=correo, password=CLAVE)
    with pytest.raises(AuthError) as fallo:
        auth.login(email=correo, password="no es la mia")
    assert fallo.value.status == 401


def test_el_error_no_revela_si_el_correo_existe(auth):
    """Si distinguiera, el formulario seria un buscador de cuentas."""
    correo = _correo()
    auth.register(email=correo, password=CLAVE)

    with pytest.raises(AuthError) as existe:
        auth.login(email=correo, password="incorrecta")
    with pytest.raises(AuthError) as no_existe:
        auth.login(email=_correo(), password="incorrecta")

    assert existe.value.code == no_existe.value.code
    assert existe.value.message == no_existe.value.message


def test_una_cuenta_desactivada_no_entra(auth, session):
    from app.models import User

    correo = _correo()
    inicio = auth.register(email=correo, password=CLAVE)
    session.get(User, inicio.user_id).is_active = False
    session.flush()

    with pytest.raises(AuthError) as fallo:
        auth.login(email=correo, password=CLAVE)
    assert fallo.value.code == "ACCOUNT_DISABLED"


# ---------------------------------------------------------------------------
# Sesiones
# ---------------------------------------------------------------------------


def test_en_la_base_no_se_guarda_el_token(auth, session):
    """Una filtracion de user_sessions no puede entregar sesiones vivas."""
    from sqlalchemy import select

    from app.models import UserSession

    inicio = auth.register(email=_correo(), password=CLAVE)
    guardados = session.execute(select(UserSession.token_hash)).scalars().all()

    assert inicio.token not in guardados
    assert hash_de_token(inicio.token) in guardados


def test_un_token_inventado_no_vale(auth):
    auth.register(email=_correo(), password=CLAVE)
    assert auth.resolver(nuevo_token_de_sesion()) is None
    assert auth.resolver("") is None
    assert auth.resolver(None) is None


def test_una_sesion_revocada_deja_de_valer(auth):
    inicio = auth.register(email=_correo(), password=CLAVE)
    assert auth.resolver(inicio.token) is not None

    assert auth.revocar(inicio.token) is True
    assert auth.resolver(inicio.token) is None


def test_una_sesion_caducada_deja_de_valer(auth, session):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.models import UserSession

    inicio = auth.register(email=_correo(), password=CLAVE)
    fila = session.execute(
        select(UserSession).where(UserSession.token_hash == hash_de_token(inicio.token))
    ).scalar_one()
    fila.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.flush()

    assert auth.resolver(inicio.token) is None


def test_desactivar_la_cuenta_invalida_sus_sesiones(auth, session):
    from app.models import User

    inicio = auth.register(email=_correo(), password=CLAVE)
    session.get(User, inicio.user_id).is_active = False
    session.flush()
    assert auth.resolver(inicio.token) is None


def test_revocar_todas_cierra_las_demas(auth):
    correo = _correo()
    primera = auth.register(email=correo, password=CLAVE)
    segunda = auth.login(email=correo, password=CLAVE)

    assert auth.revocar_todas(primera.user_id) >= 2
    assert auth.resolver(primera.token) is None
    assert auth.resolver(segunda.token) is None


# ---------------------------------------------------------------------------
# La API exige sesion
# ---------------------------------------------------------------------------


@pytest.fixture
def anonimo(runtime):
    """Cliente SIN cookie. Es lo que ve alguien que no ha entrado."""
    from app.api import create_app

    runtime._montar_cuentas()
    aplicacion = create_app(runtime)
    aplicacion.config.update(TESTING=True)
    return aplicacion.test_client()


@pytest.mark.parametrize(
    "ruta",
    [
        "/api/v1/chats",
        "/api/v1/chats/1",
        "/api/v1/chats/1/messages",
        "/api/v1/media/1",
        "/api/v1/media/1/file",
        "/api/v1/sync/status",
        "/api/v1/session",
        "/api/v1/session/qr",
        "/api/v1/auth/me",
    ],
)
def test_sin_sesion_todo_responde_401(anonimo, ruta):
    respuesta = anonimo.get(ruta)
    assert respuesta.status_code == 401, ruta
    assert respuesta.get_json()["error"]["code"] in (
        "NOT_AUTHENTICATED",
        "DRIVE_NOT_AUTHORIZED",
    )


def test_health_sigue_abierto(anonimo):
    """Es lo que consulta el frontend ANTES de saber si hay sesion."""
    assert anonimo.get("/api/v1/health").status_code == 200


def test_que_exista_device_json_no_abre_la_puerta(anonimo, runtime):
    """CASO 3: sesion de WhatsApp en disco pero sin usuario -> login.

    Antes bastaba con que hubiera un ``device.json`` para ver el panel. Eso
    convertia el archivo en la credencial, y cualquiera con acceso al equipo
    leia la copia entera.
    """
    runtime.settings.session_file.write_text('{"jid": {"user": "x"}}', encoding="utf-8")
    assert anonimo.get("/api/v1/chats").status_code == 401


def test_sin_drive_no_se_ve_el_panel(anonimo, runtime, session):
    """CASO 2: autenticado pero sin Drive -> 403, no 401.

    Son destinos distintos: 401 manda al formulario de acceso y 403 a conectar
    Google. Confundirlos deja al usuario dando vueltas.
    """
    inicio = runtime.auth.register(email=_correo(), password=CLAVE)
    anonimo.set_cookie(runtime.settings.session_cookie_name, inicio.token)

    respuesta = anonimo.get("/api/v1/chats")
    assert respuesta.status_code == 403
    assert respuesta.get_json()["error"]["code"] == "DRIVE_NOT_AUTHORIZED"


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def test_un_post_sin_token_csrf_se_rechaza(anonimo, runtime):
    inicio = runtime.auth.register(email=_correo(), password=CLAVE)
    anonimo.set_cookie(runtime.settings.session_cookie_name, inicio.token)
    anonimo.set_cookie("whatsapp_backup_csrf", "el-token")

    # Sin la cabecera: otro origen puede provocar la peticion, pero no puede
    # LEER la cookie para copiarla.
    respuesta = anonimo.post("/api/v1/sync/run")
    assert respuesta.status_code == 403
    assert respuesta.get_json()["error"]["code"] == "CSRF_FAILED"


def test_un_post_con_token_correcto_pasa_el_csrf(anonimo, runtime):
    inicio = runtime.auth.register(email=_correo(), password=CLAVE)
    anonimo.set_cookie(runtime.settings.session_cookie_name, inicio.token)
    anonimo.set_cookie("whatsapp_backup_csrf", "el-token")

    respuesta = anonimo.post(
        "/api/v1/sync/run", headers={"X-CSRF-Token": "el-token"}
    )
    # Pasa el CSRF; lo siguiente que encuentra es la falta de Drive.
    assert respuesta.status_code != 403 or (
        respuesta.get_json()["error"]["code"] != "CSRF_FAILED"
    )


def test_las_lecturas_no_necesitan_csrf(anonimo, runtime):
    inicio = runtime.auth.register(email=_correo(), password=CLAVE)
    anonimo.set_cookie(runtime.settings.session_cookie_name, inicio.token)
    assert anonimo.get("/api/v1/auth/me").status_code == 200


# ---------------------------------------------------------------------------
# La cookie
# ---------------------------------------------------------------------------


def test_la_cookie_de_sesion_es_httponly(anonimo, runtime):
    correo = _correo()
    runtime.auth.register(email=correo, password=CLAVE)
    respuesta = anonimo.post(
        "/api/v1/auth/login", json={"email": correo, "password": CLAVE}
    )
    cabecera = "".join(respuesta.headers.getlist("Set-Cookie"))

    assert "HttpOnly" in cabecera, "un script no puede poder leer la sesion"
    assert "SameSite=Lax" in cabecera


def test_la_respuesta_de_login_no_lleva_el_hash(anonimo, runtime):
    correo = _correo()
    runtime.auth.register(email=correo, password=CLAVE)
    cuerpo = anonimo.post(
        "/api/v1/auth/login", json={"email": correo, "password": CLAVE}
    ).get_json()

    texto = str(cuerpo)
    assert "argon2" not in texto
    assert "password" not in texto


def test_logout_borra_la_cookie_y_revoca(anonimo, runtime):
    correo = _correo()
    runtime.auth.register(email=correo, password=CLAVE)
    respuesta = anonimo.post(
        "/api/v1/auth/login", json={"email": correo, "password": CLAVE}
    )
    assert anonimo.get("/api/v1/auth/me").status_code == 200

    # El navegador copia la cookie CSRF a la cabecera; aqui se hace lo mismo
    # leyendola de la respuesta del login.
    anonimo.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": _csrf_de(respuesta)}
    )
    assert anonimo.get("/api/v1/auth/me").status_code == 401


def test_logout_no_desvincula_whatsapp():
    """Cerrar sesion en un equipo prestado no puede costar volver a escanear."""
    import inspect

    from app.api import auth_routes

    fuente = inspect.getsource(auth_routes.logout)
    for prohibido in ("archive_session", "restart_pairing", "desconectar"):
        assert prohibido not in fuente


def _csrf_de(respuesta) -> str:
    """El token CSRF que el servidor acaba de emitir en Set-Cookie."""
    for cabecera in respuesta.headers.getlist("Set-Cookie"):
        if cabecera.startswith("whatsapp_backup_csrf="):
            return cabecera.split("=", 1)[1].split(";")[0]
    return ""
