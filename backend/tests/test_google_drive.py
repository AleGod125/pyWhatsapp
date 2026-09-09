"""Google OAuth, autorizacion de Drive y estado de onboarding.

Nada de esto habla con Google de verdad: se sustituye el cliente HTTP. Lo que
se comprueba es NUESTRA logica, que es donde estan los fallos que importan —
sobrescribir un refresh token con ``None``, dar por autorizado Drive porque el
login funciono, aceptar un token emitido para otra aplicacion.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.auth.crypto import TokenCipher, generar_clave, verificador_pkce
from app.auth.google import (
    SCOPE_DRIVE,
    GoogleError,
    GoogleOAuthClient,
    TokensDeGoogle,
    token_vivo,
)

CLAVE = "una contrasena larga"


def _correo() -> str:
    return f"g-{uuid.uuid4().hex[:10]}@example.com"


# ---------------------------------------------------------------------------
# Cifrado de tokens
# ---------------------------------------------------------------------------


def test_los_tokens_no_se_guardan_en_claro():
    cipher = TokenCipher(generar_clave())
    cifrado = cipher.encrypt("1//refresh-secreto")

    assert b"refresh-secreto" not in cifrado
    assert cipher.decrypt(cifrado) == "1//refresh-secreto"


def test_otra_clave_no_puede_descifrar():
    """Es lo que hace que la base filtrada no entregue el Drive del usuario."""
    cifrado = TokenCipher(generar_clave()).encrypt("secreto")
    assert TokenCipher(generar_clave()).decrypt(cifrado) is None


def test_un_token_ilegible_no_lanza():
    """Clave rotada o fila corrupta: hay que reconectar, no caerse."""
    assert TokenCipher(generar_clave()).decrypt(b"basura") is None
    assert TokenCipher(generar_clave()).decrypt(None) is None


def test_sin_clave_se_dice_lo_que_hay_que_hacer():
    from app.auth.crypto import ClaveDeCifradoInvalida

    with pytest.raises(ClaveDeCifradoInvalida) as fallo:
        TokenCipher(None)
    assert "APP_ENCRYPTION_KEY" in str(fallo.value)


def test_pkce_produce_un_reto_derivado_del_verificador():
    import base64
    import hashlib

    verificador, reto = verificador_pkce()
    esperado = (
        base64.urlsafe_b64encode(hashlib.sha256(verificador.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert reto == esperado
    assert verificador != reto


# ---------------------------------------------------------------------------
# El ID token
# ---------------------------------------------------------------------------


def _id_token(**claims) -> str:
    import base64
    import json

    base = {
        "iss": "https://accounts.google.com",
        "aud": "cliente-de-prueba",
        "exp": int(time.time()) + 600,
        "sub": "1234567890",
        "email": "ana@example.com",
        "email_verified": True,
    }
    base.update(claims)
    carga = base64.urlsafe_b64encode(json.dumps(base).encode()).decode().rstrip("=")
    return f"cabecera.{carga}.firma"


@pytest.fixture
def cliente_google(settings):
    import dataclasses

    return GoogleOAuthClient(
        dataclasses.replace(
            settings,
            google_client_id="cliente-de-prueba",
            google_client_secret="secreto",
        )
    )


def test_un_id_token_correcto_se_acepta(cliente_google):
    claims = cliente_google._afirmaciones_del_id_token(_id_token(nonce="n1"), nonce="n1")
    assert claims["sub"] == "1234567890"


def test_un_token_de_otra_aplicacion_se_rechaza(cliente_google):
    """Sin comprobar ``aud`` se podria entrar con credenciales de otro servicio."""
    with pytest.raises(GoogleError) as fallo:
        cliente_google._afirmaciones_del_id_token(
            _id_token(aud="otra-aplicacion", nonce="n1"), nonce="n1"
        )
    assert fallo.value.code == "GOOGLE_BAD_AUDIENCE"


def test_un_emisor_que_no_es_google_se_rechaza(cliente_google):
    with pytest.raises(GoogleError) as fallo:
        cliente_google._afirmaciones_del_id_token(
            _id_token(iss="https://malicioso.example", nonce="n1"), nonce="n1"
        )
    assert fallo.value.code == "GOOGLE_BAD_ISSUER"


def test_un_token_caducado_se_rechaza(cliente_google):
    with pytest.raises(GoogleError) as fallo:
        cliente_google._afirmaciones_del_id_token(
            _id_token(exp=int(time.time()) - 10, nonce="n1"), nonce="n1"
        )
    assert fallo.value.code == "GOOGLE_TOKEN_EXPIRED"


def test_un_nonce_que_no_coincide_se_rechaza(cliente_google):
    """Ata la respuesta a ESTA peticion: un token capturado antes no sirve."""
    with pytest.raises(GoogleError) as fallo:
        cliente_google._afirmaciones_del_id_token(
            _id_token(nonce="viejo"), nonce="nuevo"
        )
    assert fallo.value.code == "GOOGLE_BAD_NONCE"


# ---------------------------------------------------------------------------
# Scopes: identidad y Drive no son lo mismo
# ---------------------------------------------------------------------------


def test_se_pide_drive_file_y_no_drive_entero():
    """Minimo privilegio: solo los archivos que crea la aplicacion."""
    from app.auth.google import SCOPES

    assert SCOPE_DRIVE in SCOPES
    assert "https://www.googleapis.com/auth/drive" not in SCOPES


def test_identidad_concedida_y_drive_negado_se_distinguen():
    """Google puede dar una y negar la otra. Confundirlas promete un
    almacenamiento que no existe."""
    solo_identidad = TokensDeGoogle(
        access_token="a", expires_in=3600, scope="openid email profile"
    )
    con_drive = TokensDeGoogle(
        access_token="a", expires_in=3600, scope=f"openid email {SCOPE_DRIVE}"
    )
    assert solo_identidad.drive_autorizado is False
    assert con_drive.drive_autorizado is True


def test_la_url_de_autorizacion_pide_offline_y_pkce(cliente_google):
    url = cliente_google.url_de_autorizacion(
        state="s", code_challenge="c", nonce="n", forzar_consentimiento=True
    )
    assert "access_type=offline" in url, "sin esto no hay refresh token"
    assert "code_challenge_method=S256" in url
    assert "prompt=consent" in url
    assert "secreto" not in url, "el client_secret NO viaja al navegador"


def test_sin_consentimiento_forzado_no_se_molesta_al_usuario(cliente_google):
    url = cliente_google.url_de_autorizacion(
        state="s", code_challenge="c", nonce="n", forzar_consentimiento=False
    )
    assert "prompt=consent" not in url


# ---------------------------------------------------------------------------
# Persistencia y refresco
# ---------------------------------------------------------------------------


@pytest.fixture
def google(runtime, settings, monkeypatch):
    import dataclasses

    from app.auth.google_service import GoogleService

    runtime._montar_cuentas()
    servicio = GoogleService(
        runtime.database,
        dataclasses.replace(settings, app_encryption_key=generar_clave()),
    )
    return servicio


@pytest.fixture
def usuario(runtime):
    runtime._montar_cuentas()
    return runtime.auth.register(email=_correo(), password=CLAVE)


def _tokens(**cambios) -> TokensDeGoogle:
    base = dict(
        access_token="ya29.acceso",
        refresh_token="1//refresh",
        expires_in=3600,
        scope=f"openid email profile {SCOPE_DRIVE}",
        id_token_claims={"sub": "sub-123"},
    )
    base.update(cambios)
    return TokensDeGoogle(**base)


def test_guardar_cifra_los_dos_tokens(google, usuario, session):
    from sqlalchemy import select

    from app.models import GoogleCredential

    google.guardar(usuario.user_id, _tokens())
    fila = session.execute(
        select(GoogleCredential).where(GoogleCredential.user_id == usuario.user_id)
    ).scalar_one()

    assert b"ya29.acceso" not in fila.access_token_encrypted
    assert b"1//refresh" not in fila.refresh_token_encrypted


def test_un_refresh_ausente_NO_borra_el_guardado(google, usuario, session):
    """El fallo clasico.

    Google solo entrega refresh token en el primer consentimiento. Escribir el
    ``None`` de un login posterior encima deja al usuario sin acceso duradero
    en cuanto caduque el access token, y sin ninguna senal de por que.
    """
    from sqlalchemy import select

    from app.models import GoogleCredential

    google.guardar(usuario.user_id, _tokens())
    antes = session.execute(
        select(GoogleCredential.refresh_token_encrypted).where(
            GoogleCredential.user_id == usuario.user_id
        )
    ).scalar_one()

    google.guardar(usuario.user_id, _tokens(refresh_token=None))
    despues = session.execute(
        select(GoogleCredential.refresh_token_encrypted).where(
            GoogleCredential.user_id == usuario.user_id
        )
    ).scalar_one()

    assert despues == antes, "se ha perdido el refresh token"


def test_un_refresh_nuevo_si_sustituye_al_viejo(google, usuario):
    google.guardar(usuario.user_id, _tokens())
    google.guardar(usuario.user_id, _tokens(refresh_token="1//nuevo"))
    assert google.access_token(usuario.user_id) == "ya29.acceso"


def test_el_estado_no_devuelve_ningun_token(google, usuario):
    google.guardar(usuario.user_id, _tokens())
    cuerpo = google.estado(usuario.user_id).to_json()

    texto = str(cuerpo)
    assert "ya29" not in texto
    assert "1//refresh" not in texto
    assert cuerpo["drive_authorized"] is True


def test_sin_conexion_el_estado_lo_dice(google, usuario):
    estado = google.estado(usuario.user_id)
    assert estado.google_connected is False
    assert estado.drive_authorized is False


def test_drive_negado_se_refleja_en_el_estado(google, usuario):
    google.guardar(usuario.user_id, _tokens(scope="openid email profile"))
    estado = google.estado(usuario.user_id)

    assert estado.google_connected is True
    assert estado.drive_authorized is False, "identidad no implica Drive"


def test_un_access_caducado_se_renueva_solo(google, usuario, session, monkeypatch):
    from sqlalchemy import select

    from app.models import GoogleCredential

    google.guardar(usuario.user_id, _tokens())
    fila = session.execute(
        select(GoogleCredential).where(GoogleCredential.user_id == usuario.user_id)
    ).scalar_one()
    fila.access_token_expires_at = datetime.now(timezone.utc) - timedelta(hours=2)
    session.flush()

    monkeypatch.setattr(
        google._client,
        "refrescar",
        lambda refresh: _tokens(access_token="ya29.renovado", refresh_token=None),
    )
    assert google.access_token(usuario.user_id) == "ya29.renovado"


def test_si_el_refresh_esta_revocado_hay_que_reconectar(
    google, usuario, session, monkeypatch
):
    from sqlalchemy import select

    from app.models import GoogleCredential

    google.guardar(usuario.user_id, _tokens())
    fila = session.execute(
        select(GoogleCredential).where(GoogleCredential.user_id == usuario.user_id)
    ).scalar_one()
    fila.access_token_expires_at = datetime.now(timezone.utc) - timedelta(hours=2)
    session.flush()

    def revocado(_refresh):
        raise GoogleError("GOOGLE_TOKEN_REJECTED", "revocado")

    monkeypatch.setattr(google._client, "refrescar", revocado)
    assert google.access_token(usuario.user_id) is None


def test_sin_refresh_token_no_se_puede_renovar(google, usuario):
    google.guardar(usuario.user_id, _tokens(refresh_token=None, expires_in=0))
    assert google.access_token(usuario.user_id) is None


def test_un_token_a_punto_de_caducar_no_se_da_por_vivo():
    """Uno que caduca en 20 segundos puede morir a mitad de la peticion."""
    assert token_vivo(datetime.now(timezone.utc) + timedelta(seconds=20)) is False
    assert token_vivo(datetime.now(timezone.utc) + timedelta(hours=1)) is True
    assert token_vivo(None) is False


# ---------------------------------------------------------------------------
# Comprobacion real de Drive
# ---------------------------------------------------------------------------


def test_drive_se_comprueba_con_una_llamada_de_verdad(google, usuario, monkeypatch):
    """Que el login funcione no dice NADA sobre Drive."""
    google.guardar(usuario.user_id, _tokens())
    llamadas = []
    monkeypatch.setattr(
        google._client, "probar_drive", lambda t: llamadas.append(t) or {"user": {}}
    )

    ok, motivo = google.comprobar_drive(usuario.user_id)
    assert ok is True and motivo is None
    assert llamadas, "no se llego a llamar a Drive"


def test_sin_scope_de_drive_no_se_intenta_siquiera(google, usuario, monkeypatch):
    google.guardar(usuario.user_id, _tokens(scope="openid email profile"))
    monkeypatch.setattr(
        google._client,
        "probar_drive",
        lambda t: pytest.fail("no deberia llamarse sin scope"),
    )

    ok, motivo = google.comprobar_drive(usuario.user_id)
    assert ok is False
    assert "Drive" in motivo


def test_desconectar_borra_las_credenciales_pero_no_la_cuenta(
    google, usuario, session, monkeypatch
):
    from sqlalchemy import select

    from app.models import GoogleCredential, User

    google.guardar(usuario.user_id, _tokens())
    monkeypatch.setattr(google._client, "revocar", lambda t: True)

    assert google.desconectar(usuario.user_id) is True
    assert (
        session.execute(
            select(GoogleCredential).where(
                GoogleCredential.user_id == usuario.user_id
            )
        ).scalar_one_or_none()
        is None
    )
    assert session.get(User, usuario.user_id) is not None, "la cuenta se conserva"


def test_desconectar_intenta_revocar_en_google(google, usuario, monkeypatch):
    revocados = []
    google.guardar(usuario.user_id, _tokens())
    monkeypatch.setattr(google._client, "revocar", lambda t: revocados.append(t) or True)

    google.desconectar(usuario.user_id)
    assert revocados, "hay que pedirle a Google que invalide el token"


def test_si_google_no_responde_las_credenciales_se_borran_igual(
    google, usuario, monkeypatch
):
    """Dejarlas porque Google no contesto seria peor que quitarlas."""
    google.guardar(usuario.user_id, _tokens())
    monkeypatch.setattr(google._client, "revocar", lambda t: False)

    assert google.desconectar(usuario.user_id) is True
    assert google.estado(usuario.user_id).google_connected is False


# ---------------------------------------------------------------------------
# Onboarding: el backend decide el siguiente paso
# ---------------------------------------------------------------------------


@pytest.fixture
def app_y_runtime(runtime, settings):
    import dataclasses

    from app.api import create_app
    from app.auth.google_service import GoogleService

    runtime._montar_cuentas()
    runtime.google = GoogleService(
        runtime.database,
        dataclasses.replace(settings, app_encryption_key=generar_clave()),
    )
    aplicacion = create_app(runtime)
    aplicacion.config.update(TESTING=True)
    return aplicacion, runtime


def _cliente(aplicacion, runtime, token=None):
    cli = aplicacion.test_client()
    if token:
        cli.set_cookie(runtime.settings.session_cookie_name, token)
    return cli


def test_sin_sesion_el_siguiente_paso_es_login(app_y_runtime):
    aplicacion, runtime = app_y_runtime
    cuerpo = _cliente(aplicacion, runtime).get("/api/v1/onboarding/status").get_json()

    assert cuerpo["authenticated"] is False
    assert cuerpo["next_step"] == "login"


def test_autenticado_sin_drive_va_a_conectar_google(app_y_runtime):
    aplicacion, runtime = app_y_runtime
    inicio = runtime.auth.register(email=_correo(), password=CLAVE)
    cuerpo = (
        _cliente(aplicacion, runtime, inicio.token)
        .get("/api/v1/onboarding/status")
        .get_json()
    )

    assert cuerpo["authenticated"] is True
    assert cuerpo["drive_authorized"] is False
    assert cuerpo["next_step"] == "connect_google"


def test_con_drive_pero_sin_whatsapp_va_al_qr(app_y_runtime):
    aplicacion, runtime = app_y_runtime
    inicio = runtime.auth.register(email=_correo(), password=CLAVE)
    runtime.google.guardar(inicio.user_id, _tokens())

    cuerpo = (
        _cliente(aplicacion, runtime, inicio.token)
        .get("/api/v1/onboarding/status")
        .get_json()
    )
    assert cuerpo["drive_authorized"] is True
    assert cuerpo["whatsapp_linked"] is False
    assert cuerpo["next_step"] == "pairing"


def test_con_todo_listo_va_al_panel(app_y_runtime, session):
    from app.models import WhatsAppAccount

    aplicacion, runtime = app_y_runtime
    inicio = runtime.auth.register(email=_correo(), password=CLAVE)
    runtime.google.guardar(inicio.user_id, _tokens())
    session.add(
        WhatsAppAccount(
            user_id=inicio.user_id,
            session_status="linked",
            session_storage_key=f"users/{inicio.user_id}",
        )
    )
    session.flush()

    cuerpo = (
        _cliente(aplicacion, runtime, inicio.token)
        .get("/api/v1/onboarding/status")
        .get_json()
    )
    assert cuerpo["next_step"] == "dashboard"


def test_la_vinculacion_de_otro_no_cuenta_como_mia(app_y_runtime, session):
    """CASO 41: el equipo tiene WhatsApp de A; entra B."""
    from app.models import WhatsAppAccount

    aplicacion, runtime = app_y_runtime
    a = runtime.auth.register(email=_correo(), password=CLAVE)
    b = runtime.auth.register(email=_correo(), password=CLAVE)
    runtime.google.guardar(b.user_id, _tokens())
    session.add(
        WhatsAppAccount(
            user_id=a.user_id,
            session_status="linked",
            session_storage_key=f"users/{a.user_id}",
        )
    )
    session.flush()

    cuerpo = (
        _cliente(aplicacion, runtime, b.token)
        .get("/api/v1/onboarding/status")
        .get_json()
    )
    assert cuerpo["whatsapp_linked"] is False, "B no puede heredar la sesion de A"
    assert cuerpo["next_step"] == "pairing"


def test_el_estado_de_onboarding_no_lleva_tokens(app_y_runtime):
    aplicacion, runtime = app_y_runtime
    inicio = runtime.auth.register(email=_correo(), password=CLAVE)
    runtime.google.guardar(inicio.user_id, _tokens())

    texto = str(
        _cliente(aplicacion, runtime, inicio.token)
        .get("/api/v1/onboarding/status")
        .get_json()
    )
    assert "ya29" not in texto and "1//refresh" not in texto
