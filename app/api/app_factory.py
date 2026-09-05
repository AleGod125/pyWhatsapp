"""Fabrica de la aplicacion Flask.

Recibe un ``AppRuntime`` ya construido y monta la API encima. La fabrica NO
arranca WhatsApp: eso lo decide quien llama (``service.py``), y lo hace en
segundo plano para que el servidor HTTP escuche enseguida.
"""

from __future__ import annotations

from typing import Any

from flask import Flask, jsonify

from app.api.auth_routes import auth_api
from app.api.storage_routes import storage_api
from app.api.routes import api
from app.api.serializers import API_PREFIX
from app.auth.web import comprobar_csrf
from app.core.logging_setup import get_logger

log = get_logger("API")


def _secreto_de_sesion(settings: Any) -> str:
    """Clave para firmar la cookie de Flask.

    Se deriva de ``APP_ENCRYPTION_KEY`` si existe, para no pedir dos secretos
    distintos. Si no hay ninguno se genera uno efimero: el OAuth funciona
    dentro del proceso, pero cualquier flujo a medias se rompe al reiniciar.
    Es lo correcto para desarrollo y se avisa.
    """
    import hashlib
    import secrets

    clave = getattr(settings, "app_encryption_key", None)
    if clave:
        return hashlib.sha256(f"session:{clave}".encode()).hexdigest()
    log.warning(
        "Sin APP_ENCRYPTION_KEY: la clave de sesion sera efimera y los flujos "
        "de OAuth a medias se perderan al reiniciar. Ver docs/GOOGLE_OAUTH_SETUP.md."
    )
    return secrets.token_hex(32)


def create_app(runtime: Any, *, cors_origin: str | None = None) -> Flask:
    """Construye la aplicacion Flask sobre un runtime dado.

    :param runtime: instancia de :class:`app.core.runtime.AppRuntime`.
    :param cors_origin: origen del frontend. Por defecto, ``FRONTEND_ORIGIN``.
    """
    app = Flask(__name__)
    app.config["RUNTIME"] = runtime
    # El JSON sale con acentos de verdad, no escapados: los nombres de los
    # contactos y los mensajes son texto humano.
    app.json.ensure_ascii = False
    # Sin ordenar las claves: el orden en que se construyen es mas legible.
    app.json.sort_keys = False

    origen = cors_origin or runtime.settings.frontend_origin
    _configurar_cors(app, origen)

    # Firma la cookie de Flask, que solo lleva el estado TEMPORAL del OAuth
    # (state, verificador PKCE, nonce). La sesion del usuario NO viaja ahi:
    # va en su propia cookie con un token opaco.
    app.secret_key = _secreto_de_sesion(runtime.settings)

    app.register_blueprint(api)
    app.register_blueprint(auth_api, url_prefix=API_PREFIX)
    app.register_blueprint(storage_api, url_prefix=API_PREFIX)
    # Las preferencias del usuario: tema, idioma, tipografia, alias. Viven en
    # `app_state` bajo una clave por usuario, asi que no hubo migracion.
    from app.api.preferences_routes import preferences as preferences_api

    app.register_blueprint(preferences_api, url_prefix=API_PREFIX)

    # Web Companion. Se monta SIEMPRE para que el panel pueda preguntar su
    # estado y recibir "disabled" en vez de un 404, que no distingue "apagado"
    # de "esta version no lo tiene".
    from app.api.web_companion_routes import web_companion as web_companion_api

    app.register_blueprint(web_companion_api, url_prefix=API_PREFIX)

    # Las peticiones que cambian estado necesitan token CSRF. Se instala aqui
    # y no en cada ruta: una ruta nueva queda protegida por omision, que es
    # justo al reves de tener que acordarse de protegerla.
    app.before_request(comprobar_csrf)

    # Herramientas de diagnostico. Se montan siempre, y cada ruta se protege
    # sola: la de app-state exige COMPAT_APPSTATE_SEEDS y devuelve 409 sin el.
    #
    # Antes el blueprint entero dependia de ese interruptor, asi que la unica
    # forma de averiguar por que ON_DEMAND no responde era arrancar con una
    # bandera que no tiene nada que ver con ON_DEMAND.
    from app.experimental.diagnostics_api import diagnostics as diagnostics_bp

    app.register_blueprint(diagnostics_bp)
    log.debug("Endpoints de diagnostico montados")

    @app.get("/")
    def raiz():
        """Portada minima: dice donde esta la API y que version es."""
        return jsonify(
            {
                "service": "whatsapp-backup",
                "api": "/api/v1",
                "health": "/api/v1/health",
                "events": "/api/v1/events/stream",
            }
        )

    @app.errorhandler(404)
    def no_encontrado(_error):
        return jsonify({"error": "recurso no encontrado"}), 404

    @app.errorhandler(500)
    def error_interno(error):  # pragma: no cover - se prueba a mano
        log.exception("Error no controlado en la API: %s", error)
        return jsonify({"error": "error interno del servidor"}), 500

    return app


def _configurar_cors(app: Flask, origen: str) -> None:
    """Permite al frontend hablar con la API.

    Se limita al origen configurado, no a ``*``. Con ``*`` el navegador ni
    siquiera permite enviar cookies, y aunque lo permitiera, cualquier pagina
    podria leer el historial completo desde el navegador del usuario.
    """
    try:
        from flask_cors import CORS
    except ImportError:  # pragma: no cover - flask-cors va en requirements
        log.warning(
            "flask-cors no esta instalado: el frontend en %s no podra llamar "
            "a la API desde el navegador. Instala las dependencias.",
            origen,
        )
        return

    CORS(
        app,
        resources={r"/api/*": {"origins": [origen]}},
        # La sesion viaja en una cookie, asi que el navegador tiene que poder
        # enviarla en las peticiones entre origenes. Exige un origen concreto:
        # credenciales y ``*`` son incompatibles por especificacion.
        supports_credentials=True,
        allow_headers=["Content-Type", "X-CSRF-Token"],
    )
    log.info("CORS habilitado para %s", origen)
