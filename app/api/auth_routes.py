"""Endpoints de cuentas, Google y estado de onboarding.

El navegador nunca manda quien es: lo decide el servidor a partir de la cookie
de sesion. Ver :mod:`app.auth.web`.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, jsonify, redirect, request, session

from app.auth.crypto import (
    comparar_seguro,
    nuevo_estado_oauth,
    nuevo_token_de_sesion,
    verificador_pkce,
)
from app.auth.google import GoogleError
from app.auth.service import AuthError
from app.auth.web import (
    LimitadorDeIntentos,
    borrar_cookie_de_sesion,
    clave_de_limite,
    poner_cookie_de_sesion,
    requiere_sesion,
    token_de_sesion,
    usuario_actual,
)
from app.core.logging_setup import get_logger

log = get_logger("AUTH")

auth_api = Blueprint("auth_api", __name__)

#: Ventanas deliberadamente holgadas: el objetivo es frenar un ataque por
#: fuerza bruta, no castigar a quien se equivoca escribiendo su contrasena.
LIMITE_LOGIN = LimitadorDeIntentos(maximo=10, ventana_segundos=60)
LIMITE_REGISTRO = LimitadorDeIntentos(maximo=5, ventana_segundos=300)
LIMITE_OAUTH = LimitadorDeIntentos(maximo=20, ventana_segundos=60)

#: Claves de la sesion de Flask donde vive el estado del OAuth en curso. Es
#: una cookie firmada y de un solo uso: nada de esto se guarda en la base.
OAUTH_STATE = "oauth_state"
OAUTH_VERIFIER = "oauth_verifier"
OAUTH_NONCE = "oauth_nonce"


def _runtime() -> Any:
    return current_app.config["RUNTIME"]


def _error(code: str, message: str, status: int, **extra: Any):
    cuerpo: dict[str, Any] = {"error": {"code": code, "message": message}}
    cuerpo.update(extra)
    return jsonify(cuerpo), status


def _sin_auth():
    return _error(
        "AUTH_UNAVAILABLE",
        "La autenticacion no esta disponible: falta la base de datos.",
        503,
    )


def _usuario_json(usuario: Any) -> dict[str, Any]:
    """Lo que el frontend necesita. Nunca el hash ni los tokens."""
    return {
        "id": str(usuario.id),
        "email": usuario.email,
        "display_name": usuario.display_name,
        "avatar_url": usuario.avatar_url,
        "auth_provider": usuario.auth_provider,
        "email_verified": bool(usuario.email_verified),
    }


def _json() -> dict[str, Any]:
    datos = request.get_json(silent=True)
    return datos if isinstance(datos, dict) else {}


# ---------------------------------------------------------------------------
# Cuentas locales
# ---------------------------------------------------------------------------


@auth_api.post("/auth/register")
def register():
    auth = _runtime().auth
    if auth is None:
        return _sin_auth()

    clave = clave_de_limite("register")
    if not LIMITE_REGISTRO.permitido(clave):
        return _error(
            "RATE_LIMITED", "Demasiados intentos. Espera un momento.", 429
        )
    LIMITE_REGISTRO.anotar(clave)

    datos = _json()
    try:
        sesion = auth.register(
            email=datos.get("email", ""),
            password=datos.get("password", ""),
            display_name=datos.get("display_name"),
        )
    except AuthError as exc:
        return _error(exc.code, exc.message, exc.status)

    usuario = auth.resolver(sesion.token)
    respuesta = jsonify({"user": _usuario_json(usuario)} if usuario else {})
    return poner_cookie_de_sesion(respuesta, sesion.token, sesion.expires_at), 201


@auth_api.post("/auth/login")
def login():
    auth = _runtime().auth
    if auth is None:
        return _sin_auth()

    datos = _json()
    correo = str(datos.get("email", ""))
    # Se limita por IP+correo: asi un atacante no agota el cupo de una victima
    # concreta desde fuera, ni prueba mil correos desde la misma IP.
    clave = clave_de_limite("login", correo)
    if not LIMITE_LOGIN.permitido(clave):
        return _error(
            "RATE_LIMITED", "Demasiados intentos. Espera un momento.", 429
        )
    LIMITE_LOGIN.anotar(clave)

    try:
        sesion = auth.login(email=correo, password=str(datos.get("password", "")))
    except AuthError as exc:
        return _error(exc.code, exc.message, exc.status)

    LIMITE_LOGIN.limpiar(clave)
    usuario = auth.resolver(sesion.token)
    respuesta = jsonify({"user": _usuario_json(usuario)} if usuario else {})
    return poner_cookie_de_sesion(respuesta, sesion.token, sesion.expires_at)


@auth_api.post("/auth/logout")
def logout():
    """Cierra la sesion web. NO desvincula WhatsApp ni desconecta Google.

    Son acciones distintas, y mezclarlas haria que cerrar sesion en un
    ordenador prestado costara volver a escanear un QR.
    """
    auth = _runtime().auth
    if auth is not None:
        auth.revocar(token_de_sesion())
    return borrar_cookie_de_sesion(jsonify({"ok": True}))


@auth_api.get("/auth/me")
def me():
    usuario = usuario_actual()
    if usuario is None:
        return _error("NOT_AUTHENTICATED", "No hay sesion iniciada.", 401)
    return jsonify({"user": _usuario_json(usuario)})


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------


@auth_api.get("/auth/google/start")
def google_start():
    """Manda al usuario a Google. Sirve para entrar Y para conectar Drive.

    Si ya hay sesion iniciada, al volver se enlaza Google con ESA cuenta. Si
    no la hay, se entra (o se crea la cuenta) con la identidad de Google.
    """
    rt = _runtime()
    google = rt.google
    if google is None:
        return _sin_auth()
    if not google.client.configurado:
        return _error(
            "GOOGLE_NOT_CONFIGURED",
            "Google no esta configurado en el servidor. "
            "Ver docs/GOOGLE_OAUTH_SETUP.md.",
            503,
        )

    clave = clave_de_limite("oauth")
    if not LIMITE_OAUTH.permitido(clave):
        return _error("RATE_LIMITED", "Demasiados intentos.", 429)
    LIMITE_OAUTH.anotar(clave)

    estado = nuevo_estado_oauth()
    verificador, reto = verificador_pkce()
    nonce = nuevo_token_de_sesion()

    session[OAUTH_STATE] = estado
    session[OAUTH_VERIFIER] = verificador
    session[OAUTH_NONCE] = nonce

    # Si el usuario ya tiene refresh token no hace falta volver a molestarle
    # con la pantalla de consentimiento. Si NO lo tiene, hay que forzarla:
    # Google solo entrega refresh token cuando el usuario consiente.
    usuario = usuario_actual()
    tiene_refresh = False
    if usuario is not None:
        tiene_refresh = google.estado(usuario.id).token_valid

    return redirect(
        google.client.url_de_autorizacion(
            state=estado,
            code_challenge=reto,
            nonce=nonce,
            forzar_consentimiento=not tiene_refresh,
        )
    )


@auth_api.get("/auth/google/callback")
def google_callback():
    """Vuelta de Google. Termina redirigiendo al frontend, nunca con JSON.

    En la URL de vuelta no viaja ningun token: solo un codigo de resultado que
    el frontend traduce a un mensaje. Los tokens se quedan aqui.
    """
    rt = _runtime()
    google = rt.google
    auth = rt.auth
    destino = rt.settings.frontend_url.rstrip("/") + "/auth/google/callback"

    def volver(estado: str) -> Any:
        return redirect(f"{destino}?status={estado}")

    if google is None or auth is None:
        return volver("unavailable")

    if request.args.get("error"):
        # El usuario cancelo en la pantalla de Google. No es un fallo.
        log.info("El usuario cancelo la autorizacion de Google")
        return volver("cancelled")

    esperado = session.pop(OAUTH_STATE, None)
    verificador = session.pop(OAUTH_VERIFIER, None)
    nonce = session.pop(OAUTH_NONCE, None)
    recibido = request.args.get("state", "")

    if not esperado or not recibido or not comparar_seguro(esperado, recibido):
        # Sin state valido esto no es una vuelta de un flujo que empezamos
        # nosotros: podria ser un intento de enlazar la cuenta de un atacante.
        log.warning("Callback de Google con state invalido")
        return volver("invalid_state")

    codigo = request.args.get("code", "")
    if not codigo or not verificador:
        return volver("invalid_request")

    try:
        tokens = google.client.canjear_codigo(
            code=codigo, code_verifier=verificador, nonce=nonce or ""
        )
    except GoogleError as exc:
        log.warning("Fallo el canje del codigo de Google: %s", exc.code)
        return volver("exchange_failed")

    if not tokens.google_subject:
        return volver("no_identity")

    try:
        usuario = _resolver_usuario_de_google(rt, tokens)
    except AuthError as exc:
        log.info("No se pudo enlazar la cuenta de Google: %s", exc.code)
        return volver(exc.code.lower())

    google.guardar(usuario.id, tokens)

    # Abrir sesion web con esa cuenta.
    sesion = auth.abrir_sesion_para(usuario.id)
    respuesta = current_app.make_response(
        volver("connected" if tokens.drive_autorizado else "drive_denied")
    )
    return poner_cookie_de_sesion(respuesta, sesion.token, sesion.expires_at)


def _resolver_usuario_de_google(rt: Any, tokens: Any) -> Any:
    """Decide a QUE cuenta pertenece esta identidad de Google.

    Tres casos, y el tercero es el delicado:

    1. Ya hay sesion iniciada -> se enlaza Google con esa cuenta.
    2. Hay una cuenta con ese ``google_subject`` -> es esa.
    3. Hay una cuenta LOCAL con el mismo correo -> se enlaza, pero solo
       porque Google afirma que ese correo esta verificado. Sin esa
       verificacion, cualquiera que registre el correo ajeno en Google podria
       apoderarse de la cuenta local.
    """
    from sqlalchemy import select

    from app.models import User

    actual = usuario_actual()
    with rt.database.transaction() as session_db:
        if actual is not None:
            usuario = session_db.get(User, actual.id)
            if usuario is not None:
                otro = session_db.execute(
                    select(User.id).where(
                        User.google_subject == tokens.google_subject,
                        User.id != usuario.id,
                    )
                ).scalar_one_or_none()
                if otro is not None:
                    raise AuthError(
                        "GOOGLE_ALREADY_LINKED",
                        "Esa cuenta de Google ya pertenece a otro usuario.",
                        409,
                    )
                _marcar_google(usuario, tokens)
                session_db.flush()
                session_db.expunge(usuario)
                return usuario

        usuario = session_db.execute(
            select(User).where(User.google_subject == tokens.google_subject)
        ).scalar_one_or_none()
        if usuario is not None:
            if not usuario.is_active:
                raise AuthError("ACCOUNT_DISABLED", "Cuenta desactivada.", 403)
            session_db.expunge(usuario)
            return usuario

        correo = (tokens.email or "").strip()
        if correo:
            local = session_db.execute(
                select(User).where(User.email == correo)
            ).scalar_one_or_none()
            if local is not None:
                if not tokens.id_token_claims.get("email_verified"):
                    raise AuthError(
                        "EMAIL_NOT_VERIFIED",
                        "Google no confirma que ese correo sea tuyo. Inicia "
                        "sesion con tu contrasena y conecta Google desde ahi.",
                        409,
                    )
                _marcar_google(local, tokens)
                session_db.flush()
                session_db.expunge(local)
                return local

        if not correo:
            raise AuthError("NO_EMAIL", "Google no devolvio un correo.", 400)

        nuevo = User(
            email=correo,
            password_hash=None,
            display_name=tokens.id_token_claims.get("name"),
            avatar_url=tokens.id_token_claims.get("picture"),
            auth_provider="google",
            google_subject=tokens.google_subject,
            email_verified=bool(tokens.id_token_claims.get("email_verified")),
        )
        session_db.add(nuevo)
        session_db.flush()
        session_db.expunge(nuevo)
        log.info("Cuenta creada desde Google")
        return nuevo


def _marcar_google(usuario: Any, tokens: Any) -> None:
    usuario.google_subject = tokens.google_subject
    usuario.auth_provider = "both" if usuario.password_hash else "google"
    if tokens.id_token_claims.get("email_verified"):
        usuario.email_verified = True
    if not usuario.avatar_url:
        usuario.avatar_url = tokens.id_token_claims.get("picture")
    if not usuario.display_name:
        usuario.display_name = tokens.id_token_claims.get("name")


@auth_api.get("/auth/google/status")
@requiere_sesion
def google_status():
    google = _runtime().google
    if google is None:
        return _sin_auth()
    return jsonify(google.estado(usuario_actual().id).to_json())


@auth_api.post("/auth/google/verify")
@requiere_sesion
def google_verify():
    """Comprueba DE VERDAD que Drive responde, con una llamada real.

    Que el login de Google funcionara no dice nada sobre Drive: son permisos
    distintos y el usuario puede haber negado el segundo.
    """
    google = _runtime().google
    if google is None:
        return _sin_auth()
    funciona, motivo = google.comprobar_drive(usuario_actual().id)
    return jsonify({"drive_ok": funciona, "reason": motivo})


@auth_api.post("/auth/google/disconnect")
@requiere_sesion
def google_disconnect():
    google = _runtime().google
    if google is None:
        return _sin_auth()
    return jsonify({"disconnected": google.desconectar(usuario_actual().id)})


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------


@auth_api.get("/onboarding/status")
def onboarding_status():
    """El backend es la fuente de verdad de por donde va el usuario.

    El frontend NO deduce el siguiente paso: lo pregunta. Asi las reglas viven
    una sola vez y no se pueden saltar cambiando el enrutador del navegador.
    """
    rt = _runtime()
    usuario = usuario_actual()

    if usuario is None:
        return jsonify(
            {
                "authenticated": False,
                "google_connected": False,
                "drive_authorized": False,
                "whatsapp_linked": False,
                "next_step": "login",
            }
        )

    estado = rt.google.estado(usuario.id) if rt.google else None
    drive = bool(estado and estado.drive_authorized)
    vinculado = _whatsapp_vinculado(rt, usuario)

    if not drive:
        siguiente = "connect_google"
    elif not vinculado:
        siguiente = "pairing"
    else:
        siguiente = "dashboard"

    return jsonify(
        {
            "authenticated": True,
            "user": _usuario_json(usuario),
            "google_connected": bool(estado and estado.google_connected),
            "drive_authorized": drive,
            "whatsapp_linked": vinculado,
            "next_step": siguiente,
        }
    )


def _whatsapp_vinculado(rt: Any, usuario: Any) -> bool:
    """Si ESTE usuario ya vinculo una cuenta de WhatsApp.

    Es una pregunta sobre la CUENTA, no sobre el socket: significa "ya
    vinculo", no "el websocket esta conectado en este milisegundo". Por eso
    una desconexion temporal no cambia la respuesta.

    Que exista un ``device.json`` en disco NO basta: puede ser de otra
    persona. La propiedad se lee de la base.
    """
    from sqlalchemy import select

    from app.models import WhatsAppAccount
    from app.models.accounts import LINKED_STATUSES

    if rt.database is None:
        return False

    # Reconciliacion defensiva: si el runtime esta conectado y es de ESTE
    # usuario pero la base dice que no, se corrige. Cubre el caso de una
    # vinculacion que se completo antes de que existiera este puente.
    _reconciliar_si_hace_falta(rt, usuario)

    with rt.database.transaction() as session_db:
        estados = session_db.execute(
            select(WhatsAppAccount.session_status).where(
                WhatsAppAccount.user_id == usuario.id
            )
        ).scalars().all()
    return any(e in LINKED_STATUSES for e in estados)


def _reconciliar_si_hace_falta(rt: Any, usuario: Any) -> None:
    """Corrige la base cuando el runtime dice conectado y ella no.

    Solo si la sesion conectada es DE ESTE usuario. Nunca se adopta una
    sesion huerfana por quien resulte estar mirando el navegador: eso
    entregaria la cuenta de WhatsApp de alguien al primero que entrara.
    """
    cuentas = getattr(rt, "whatsapp_accounts", None)
    if cuentas is None or rt.state.state.value != "CONNECTED":
        return
    if rt.runtime_owner_user_id is None or rt.runtime_owner_user_id != usuario.id:
        return
    try:
        cuentas.marcar_vinculada(usuario.id, pn=None, lid=None)
        log.info("[APP] Cuenta del runtime reconciliada como vinculada")
    except Exception:  # noqa: BLE001 - reconciliar no puede tumbar el estado
        log.debug("No se pudo reconciliar la cuenta del runtime")
