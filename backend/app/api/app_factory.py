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
    # Y el registro de runtimes POR CUENTA, que es la fuente de verdad para
    # todo lo que dependa de una sesion de WhatsApp. El runtime que llega se
    # adopta tal cual: es el de la cuenta que ya existia, con su sesion
    # abierta, y rehacerlo seria dejarla sin poder abrir su Signal Store.
    from app.api.account_runtime import montar_registro

    montar_registro(app, runtime)
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

    # Las cuentas de WhatsApp del usuario: listar, anadir, renombrar y cambiar
    # cual esta mirando. Sin esto no hay forma de elegir, y sin forma de
    # elegir varias cuentas solo producen estados que nadie puede resolver.
    from app.api.accounts_routes import accounts as accounts_api

    app.register_blueprint(accounts_api, url_prefix=API_PREFIX)

    # El codigo de acceso de los chats restringidos. Es LOCAL: no es el codigo
    # secreto de WhatsApp y no puede serlo --WhatsApp manda un derivado opaco,
    # no el codigo-- pero tapa la seccion igual que WhatsApp Web tapa la suya.
    from app.api.chat_lock_routes import chat_lock as chat_lock_api

    app.register_blueprint(chat_lock_api, url_prefix=API_PREFIX)

    # Las peticiones que cambian estado necesitan token CSRF. Se instala aqui
    # y no en cada ruta: una ruta nueva queda protegida por omision, que es
    # justo al reves de tener que acordarse de protegerla.
    app.before_request(comprobar_csrf)

    # AQUI SE MONTABAN LOS ENDPOINTS DE DIAGNOSTICO, Y SE RETIRARON.
    #
    # `app/experimental/diagnostics_api.py` exponia `/ondemand/probe` y
    # `/ondemand/canary`: dos POST que disparan una peticion de historial
    # sobre la cuenta vinculada. El comentario de aqui decia que "cada ruta se
    # protege sola", y no era cierto -- ninguna de las dos llevaba
    # `@requiere_sesion`, y el unico `before_request` global es el de CSRF,
    # que para un `curl` desde fuera no es ninguna barrera.
    #
    # Con la API publicada en un dominio, eso es superficie abierta que
    # ademas no tenia ni una prueba. Era una herramienta para diagnosticar
    # ON_DEMAND a mano; ese trabajo ya se hace con el log.

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
        # TODA cabecera propia tiene que estar aqui, y el fallo no se parece a
        # un fallo de CORS.
        #
        # Se midio: al empezar a mandar `X-WhatsApp-Account` sin anadirla, el
        # navegador rechazo el preflight y bloqueo TODAS las peticiones antes
        # de enviarlas. El servidor no registro ni una --no llegaron-- y el
        # frontend, viendo que todo fallaba, se comporto como si no hubiera
        # sesion y mandaba al login. Ni un error en el log del backend.
        allow_headers=["Content-Type", "X-CSRF-Token", "X-WhatsApp-Account"],
    )
    log.info("CORS habilitado para %s", origen)
