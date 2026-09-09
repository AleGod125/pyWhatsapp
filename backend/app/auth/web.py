"""Union entre Flask y la autenticacion: cookie, CSRF y guardas de ruta.

LA COOKIE
---------
``HttpOnly`` para que ningun script pueda leerla —una inyeccion de JavaScript
no puede robar la sesion—, ``SameSite=Lax`` para que no viaje en peticiones
que inicia otro sitio, y ``Secure`` en produccion.

En localhost ``Secure`` va apagado: no hay HTTPS y la cookie no llegaria
nunca, con el efecto de que el login "no funciona" sin ningun error visible.

CSRF
----
``SameSite=Lax`` ya bloquea las peticiones POST que origina otro sitio, pero
se anade un token de doble envio: una cookie legible por JavaScript
(``whatsapp_backup_csrf``) que el frontend copia a la cabecera
``X-CSRF-Token``. Otro origen puede provocar la peticion, pero no puede LEER
la cookie para rellenar la cabecera.
"""

from __future__ import annotations

import secrets
from functools import wraps
from typing import Any, Callable

from flask import current_app, g, jsonify, request

from app.auth.crypto import comparar_seguro
from app.core.logging_setup import get_logger

log = get_logger("AUTH")

CSRF_COOKIE = "whatsapp_backup_csrf"
CSRF_HEADER = "X-CSRF-Token"

#: Metodos que no cambian nada: no necesitan token CSRF.
METODOS_SEGUROS = frozenset({"GET", "HEAD", "OPTIONS"})


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------


def _ajustes() -> Any:
    return current_app.config["RUNTIME"].settings


def poner_cookie_de_sesion(respuesta: Any, token: str, expira: Any) -> Any:
    ajustes = _ajustes()
    respuesta.set_cookie(
        ajustes.session_cookie_name,
        token,
        httponly=True,
        secure=ajustes.cookie_secure,
        samesite=ajustes.cookie_samesite,
        expires=expira,
        path="/",
    )
    # Sin valores: saber que se emitio una cookie ayuda a diagnosticar; su
    # contenido autentica a quien lo tenga.
    log.info("[AUTH] cookie de sesion emitida (secure=%s samesite=%s)",
             ajustes.cookie_secure, ajustes.cookie_samesite)
    # El token CSRF NO es HttpOnly a proposito: el frontend tiene que poder
    # leerlo para copiarlo a la cabecera. Su valor no autentica nada por si
    # solo; solo demuestra que quien pide puede leer nuestras cookies.
    respuesta.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(24),
        httponly=False,
        secure=ajustes.cookie_secure,
        samesite=ajustes.cookie_samesite,
        expires=expira,
        path="/",
    )
    return respuesta


def borrar_cookie_de_sesion(respuesta: Any) -> Any:
    ajustes = _ajustes()
    for nombre in (ajustes.session_cookie_name, CSRF_COOKIE):
        respuesta.delete_cookie(nombre, path="/")
    return respuesta


def token_de_sesion() -> str | None:
    return request.cookies.get(_ajustes().session_cookie_name)


# ---------------------------------------------------------------------------
# Usuario actual
# ---------------------------------------------------------------------------


def usuario_actual() -> Any:
    """El usuario de esta peticion, o ``None``. Se resuelve una sola vez.

    SIEMPRE desde la cookie. Nunca desde el cuerpo, la ruta o una cabecera:
    si el cliente pudiera decir quien es, cambiarlo seria todo lo que hace
    falta para leer los datos de otra persona.
    """
    if "usuario_actual" in g:
        return g.usuario_actual

    auth = current_app.config["RUNTIME"].auth
    cookie = token_de_sesion()
    g.usuario_actual = auth.resolver(cookie) if auth else None
    if cookie and g.usuario_actual is None:
        # Llego cookie pero no vale: caducada, revocada o de otra instalacion.
        # Es justo el sintoma que hay que poder distinguir de "no llego".
        log.info("[AUTH] sesion no resuelta: la cookie recibida no es valida")
    return g.usuario_actual


def _error(code: str, message: str, status: int, **extra: Any):
    cuerpo: dict[str, Any] = {"error": {"code": code, "message": message}}
    cuerpo.update(extra)
    return jsonify(cuerpo), status


# ---------------------------------------------------------------------------
# Guardas
# ---------------------------------------------------------------------------


def requiere_sesion(func: Callable) -> Callable:
    """401 si no hay sesion valida."""

    @wraps(func)
    def envoltorio(*args: Any, **kwargs: Any):
        if usuario_actual() is None:
            return _error(
                "NOT_AUTHENTICATED", "Inicia sesion para continuar.", 401
            )
        return func(*args, **kwargs)

    return envoltorio


def requiere_drive(func: Callable) -> Callable:
    """403 si el usuario no tiene Google Drive autorizado.

    Es un requisito del producto: el almacenamiento del backup es el Drive del
    usuario, asi que sin el no se entra al panel. El codigo distingue este
    caso del 401 para que el frontend mande a conectar Google en vez de al
    formulario de acceso.
    """

    @wraps(func)
    def envoltorio(*args: Any, **kwargs: Any):
        usuario = usuario_actual()
        if usuario is None:
            return _error("NOT_AUTHENTICATED", "Inicia sesion para continuar.", 401)

        google = current_app.config["RUNTIME"].google
        if google is None or not google.estado(usuario.id).drive_authorized:
            return _error(
                "DRIVE_NOT_AUTHORIZED",
                "Conecta Google Drive para continuar.",
                403,
            )
        return func(*args, **kwargs)

    return envoltorio


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def comprobar_csrf() -> Any:
    """``None`` si la peticion puede seguir; si no, la respuesta de rechazo.

    Se instala como ``before_request``. Solo mira metodos que cambian estado y
    solo cuando hay cookie de sesion: sin sesion no hay nada que falsificar.
    """
    if request.method in METODOS_SEGUROS:
        return None
    if not token_de_sesion():
        return None

    esperado = request.cookies.get(CSRF_COOKIE)
    enviado = request.headers.get(CSRF_HEADER, "")
    if not esperado or not enviado or not comparar_seguro(esperado, enviado):
        log.info("Peticion rechazada por CSRF: %s %s", request.method, request.path)
        return _error(
            "CSRF_FAILED",
            "La peticion no incluye un token CSRF valido. Recarga la pagina.",
            403,
        )
    return None


# ---------------------------------------------------------------------------
# Limite de intentos
# ---------------------------------------------------------------------------


class LimitadorDeIntentos:
    """Ventana deslizante en memoria, por clave.

    Basta para un servicio local de un solo proceso. Con varios procesos haria
    falta almacenamiento compartido, y esto dejaria de contar bien: se dice
    aqui para que nadie lo confunda con una defensa distribuida.
    """

    def __init__(self, maximo: int, ventana_segundos: float) -> None:
        self._maximo = maximo
        self._ventana = ventana_segundos
        self._intentos: dict[str, list[float]] = {}

    def permitido(self, clave: str) -> bool:
        import time

        ahora = time.monotonic()
        recientes = [
            t for t in self._intentos.get(clave, ()) if ahora - t < self._ventana
        ]
        self._intentos[clave] = recientes
        return len(recientes) < self._maximo

    def anotar(self, clave: str) -> None:
        import time

        self._intentos.setdefault(clave, []).append(time.monotonic())

    def limpiar(self, clave: str) -> None:
        """Tras un acierto: no penalizar a quien ya demostro ser el dueno."""
        self._intentos.pop(clave, None)


def clave_de_limite(*partes: str) -> str:
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?")
    return "|".join((ip.split(",")[0].strip(), *(p.lower() for p in partes if p)))
