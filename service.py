"""Backend HTTP de whatsapp_backup.

    py service.py                 API + sesion de WhatsApp
    py service.py --local         API sobre PostgreSQL, SIN abrir la sesion
    py service.py --check         verifica entorno y sale

Escucha en ``http://API_HOST:API_PORT`` (por defecto 127.0.0.1:5000) y sirve
``/api/v1``.

MISMA CONFIGURACION QUE main.py
-------------------------------
Se lee EL MISMO ``.env``. No hay archivo aparte para Flask ni variables
renombradas. Lo unico que se anadio fueron ``API_HOST``, ``API_PORT`` y
``FRONTEND_ORIGIN``.

MISMOS SERVICIOS QUE main.py
----------------------------
Ambos entrypoints construyen un ``AppRuntime`` y no duplican nada:

                       +-- main.py     -> Tkinter
    AppRuntime/Services +
                       +-- service.py  -> Flask REST + SSE

EL SERVIDOR NO ESPERA A WHATSAPP
--------------------------------
La sesion se abre en un hilo aparte. El puerto queda escuchando de inmediato y
``GET /api/v1/session`` responde ``CONNECTING`` mientras tanto. Bloquear el
arranque hasta que WhatsApp conteste dejaria la API muda hasta tres minutos.

UN SOLO DUENO DE LA SESION
--------------------------
``main.py`` y ``service.py`` no pueden abrir el companion a la vez: se
corromperia el estado del protocolo. Lo impide ``session/runtime.lock``. Si
otro proceso la tiene, esto lo dice y se ofrece a arrancar en modo solo
lectura.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from typing import Any

from app.core.config import ConfigError, load_settings
from app.core.database import DatabaseError
from app.core.lock import SessionLockedError, explain, probe
from app.core.logging_setup import get_logger

log = get_logger("API")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="service.py",
        description="API HTTP del backup local de WhatsApp",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help=(
            "servir solo la copia local de PostgreSQL, sin abrir la sesion de "
            "WhatsApp. Util con main.py abierto en otra ventana."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verificar entorno, PostgreSQL y migraciones, y salir",
    )
    parser.add_argument("--host", help="sobrescribe API_HOST solo en esta ejecucion")
    parser.add_argument(
        "--port", type=int, help="sobrescribe API_PORT solo en esta ejecucion"
    )
    return parser.parse_args(argv)


def _verificar_migraciones(runtime: Any) -> bool:
    revision = runtime.database.applied_migration()
    if revision is None:
        log.error(
            "no hay ninguna migracion aplicada en esta base de datos.\n"
            "        Ejecuta:  python -m alembic upgrade head"
        )
        return False
    log.info("Migraciones verificadas (revision=%s)", revision)
    return True


def _arrancar_whatsapp_en_segundo_plano(runtime: Any) -> threading.Thread:
    """Abre la sesion sin hacer esperar al servidor HTTP."""

    def trabajo() -> None:
        try:
            runtime.start(connect=True)
            log.info("Sesion de WhatsApp lanzada; el estado se sigue en /api/v1/session")
        except SessionLockedError as exc:
            # No deberia llegarse aqui: el arranque comprueba el cerrojo antes
            # de nada. Si pasa, es que otro proceso lo tomo en la ventana entre
            # la comprobacion y este momento. Se dice claramente en vez de
            # quedarse a medias haciendo de visor sin avisar.
            log.error("%s", explain(exc))
            log.error(
                "Otro proceso tomo la sesion durante el arranque. Esta "
                "instancia NO controla WhatsApp. Cierrala y vuelve a "
                "intentarlo, o usa 'py service.py --local' si solo quieres leer."
            )
        except Exception:  # noqa: BLE001 - la API no cae porque WhatsApp falle
            log.exception("No se pudo abrir la sesion de WhatsApp")

    hilo = threading.Thread(target=trabajo, name="whatsapp-runtime", daemon=True)
    hilo.start()
    return hilo


# Codigos de salida, para poder ramificar desde un script sin leer el texto.
SALIDA_OK = 0
SALIDA_YA_HAY_SERVICIO = 5
SALIDA_PUERTO_OCUPADO = 6
SALIDA_BIND_REMOTO = 2


def _puerto_ocupado(host: str, port: int) -> bool:
    """Si ya hay algo escuchando ahi.

    Se comprueba ANTES de arrancar Flask. Sin esto, un segundo arranque falla
    con un traceback de socket a mitad de log, o peor: queda escuchando en
    otro sitio y el usuario prueba contra la instancia equivocada.
    """
    import socket

    destino = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sonda:
        sonda.settimeout(1.0)
        try:
            return sonda.connect_ex((destino.strip("[]"), int(port))) == 0
        except OSError:
            return False


def _es_local(host: str) -> bool:
    """Si escuchar en ``host`` deja la API accesible solo desde esta maquina.

    ``0.0.0.0`` y ``::`` son todas las interfaces, no "ninguna": es el error
    clasico. Se compara contra la lista de lo que SI es local en vez de contra
    una lista de lo que no, que siempre se queda corta.
    """
    return (host or "").strip().lower() in {
        "127.0.0.1",
        "localhost",
        "::1",
        "[::1]",
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    print("[API] Iniciando...")

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"[CONFIG] ERROR: {exc}", file=sys.stderr)
        return 2

    # SINGLETON. Se comprueba ANTES de construir nada.
    #
    # Antes esto se miraba en un hilo de fondo, DESPUES de que Flask ya
    # estuviera escuchando, asi que una segunda instancia se quedaba viva
    # sirviendo la API en modo lectura sin decirlo. Se midio: dos service.py a
    # la vez, uno del .venv y otro del Python global, y no habia forma de
    # saber cual controlaba la sesion ni contra cual se estaba probando.
    #
    # Nunca se mata al otro proceso: solo se informa y se sale.
    # ``--check`` solo verifica configuracion y base de datos: no abre la
    # sesion ni escucha en ningun puerto, asi que no puede estorbar a nadie y
    # tiene que poder ejecutarse con el servicio en marcha.
    if not args.local and not args.check:
        titular = probe(settings.session_dir)
        if titular is not None:
            print(
                f"[API] ERROR: ya hay un proceso con la sesion de WhatsApp "
                f"(PID {titular.pid}).",
                file=sys.stderr,
            )
            print(titular.describe(), file=sys.stderr)
            print(
                "[API] No se iniciara una segunda instancia: dos procesos "
                "sobre la misma sesion corromperian el estado del protocolo.",
                file=sys.stderr,
            )
            print(
                "[API] Si solo quieres leer la copia local, usa: "
                "py service.py --local",
                file=sys.stderr,
            )
            return SALIDA_YA_HAY_SERVICIO

    # Y el puerto, tambien antes de nada: si esta ocupado, decirlo aqui es
    # mucho mas util que un traceback de socket al final del arranque.
    host_previsto = args.host or settings.api_host
    puerto_previsto = args.port or settings.api_port
    if not args.check and _puerto_ocupado(host_previsto, puerto_previsto):
        print(
            f"[API] ERROR: el puerto {puerto_previsto} ya esta en uso en "
            f"{host_previsto}.",
            file=sys.stderr,
        )
        print(
            "[API] Probablemente haya otra instancia escuchando. Cierrala, o "
            "arranca esta con --port OTRO.",
            file=sys.stderr,
        )
        return SALIDA_PUERTO_OCUPADO

    # La MISMA fabrica que usan las pruebas de integracion: si el cableado
    # se duplicara, una prueba podria pasar sobre un runtime que no es el que
    # se ejecuta de verdad.
    from app.core.runtime import build_service_runtime

    try:
        runtime = build_service_runtime(settings)
    except DatabaseError as exc:
        # No se llama a runtime.stop(): si la fabrica fallo, la variable no
        # llega a existir. Y no hace falta soltar nada, porque hasta aqui no
        # se ha tomado el cerrojo ni se ha abierto ningun puerto.
        get_logger("DB").error("%s", exc)
        return 3

    if not _verificar_migraciones(runtime):
        runtime.stop()
        return 3

    if args.check:
        salud = runtime.database.health()
        log.info(
            "Comprobacion correcta. PostgreSQL %s, base=%s",
            salud["server_version"],
            salud["database"],
        )
        log.info(
            "La API escucharia en http://%s:%d/api/v1",
            args.host or settings.api_host,
            args.port or settings.api_port,
        )
        runtime.stop()
        return 0

    if not args.local:
        _arrancar_whatsapp_en_segundo_plano(runtime)
    else:
        log.info("PostgreSQL listo (modo local)")
        log.info("Runtime WhatsApp deshabilitado: no se abrira la sesion")

    from app.core.runtime import build_service_app

    flask_app = build_service_app(runtime)

    host = args.host or settings.api_host
    port = args.port or settings.api_port

    if not _es_local(host) and not settings.allow_remote_api:
        log.error(
            "Escuchar en %s expondria la copia entera de tus conversaciones a "
            "la red. La API no pide contrasena. Si de verdad lo quieres, pon "
            "ALLOW_REMOTE_API=true en el .env; si no, deja API_HOST=127.0.0.1.",
            host,
        )
        runtime.stop()
        return SALIDA_BIND_REMOTO
    if not _es_local(host):
        log.warning(
            "ATENCION: la API escucha en %s, accesible desde la red, y NO pide "
            "autenticacion. Cualquiera que llegue a este puerto puede leer "
            "todos tus mensajes y descargar tu multimedia.",
            host,
        )

    def apagar(_sig: int, _frame: Any) -> None:
        log.info("Senal recibida; cerrando...")
        runtime.stop()
        sys.exit(0)

    for nombre in ("SIGINT", "SIGTERM"):
        senal = getattr(signal, nombre, None)
        if senal is not None:
            try:
                signal.signal(senal, apagar)
            except ValueError:  # pragma: no cover - hilo secundario
                pass

    log.info("API escuchando en http://%s:%d/api/v1", host, port)
    log.info("CORS permitido para %s", settings.frontend_origin)
    try:
        # threaded=True es necesario: el SSE mantiene una conexion abierta por
        # cliente y con un solo hilo bloquearia al resto de peticiones.
        # use_reloader=False a proposito: el recargador arranca un SEGUNDO
        # proceso, y ese segundo proceso intentaria tomar el cerrojo de la
        # sesion que ya tiene el primero.
        flask_app.run(
            host=host,
            port=port,
            threaded=True,
            use_reloader=False,
            debug=False,
        )
    except KeyboardInterrupt:
        log.info("Interrumpido por el usuario")
    finally:
        runtime.stop()
        log.info("Terminado")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
