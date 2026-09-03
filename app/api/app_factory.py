"""Fabrica de la aplicacion Flask.

Recibe un ``AppRuntime`` ya construido y monta la API encima. La fabrica NO
arranca WhatsApp: eso lo decide quien llama (``service.py``), y lo hace en
segundo plano para que el servidor HTTP escuche enseguida.
"""

from __future__ import annotations

from typing import Any

from flask import Flask, jsonify

from app.api.routes import api
from app.core.logging_setup import get_logger

log = get_logger("API")


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

    app.register_blueprint(api)

    # Herramientas de diagnostico. Solo se montan si la instrumentacion esta
    # encendida: no forman parte del funcionamiento normal.
    if runtime.settings.compat_appstate_seeds:
        from app.experimental.diagnostics_api import diagnostics as diagnostics_bp

        app.register_blueprint(diagnostics_bp)
        log.info("Endpoints de diagnostico montados (COMPAT_APPSTATE_SEEDS activo)")

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

    Se limita al origen configurado, no a ``*``: esta API sirve el historial
    completo de WhatsApp y no lleva autenticacion, asi que abrirla a cualquier
    origen permitiria a una pagina cualquiera leerlo desde el navegador del
    usuario.
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
        supports_credentials=False,
    )
    log.info("CORS habilitado para %s", origen)
