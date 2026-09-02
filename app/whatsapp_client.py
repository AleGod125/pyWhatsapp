"""Cliente de WhatsApp: pywhats corriendo en su propio hilo con event loop.

Modelo de concurrencia (seccion 34 del brief):

    hilo principal            hilo "wa-client"
    ---------------           ----------------
    Tkinter mainloop   <--- queue.Queue ---   asyncio + pywhats
           |                                        ^
           +--------- llamadas thread-safe ---------+
                      (asyncio.run_coroutine_threadsafe)

Ningun widget de Tkinter se toca desde este hilo. Los eventos salen por una
``queue.Queue`` y es la GUI quien la vacia con ``root.after``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings
from app.logging_setup import get_logger

log = get_logger("WA")

# Eventos REALES de pywhats 0.2.0, verificados en el paquete instalado. No se
# inventa ninguno. Los ocho ultimos no aparecen en un grep de _emit("...")
# porque se emiten con el nombre en una variable; se confirmaron en
# appstate/events.py:52-66 y messaging/receiver.py:466,655,666.
PYWHATS_EVENTS = (
    # Ciclo de vida
    "qr",
    "paired",
    "connected",
    "disconnected",
    "logged_out",
    # Mensajeria
    "message",
    "reaction",
    "message_edit",
    "message_revoke",
    "receipt",
    # Presencia
    "presence",
    "chat_presence",
    # Sincronizacion
    "history_sync",
    "decrypt_error",
    # App state: 'contact' y 'pushname' son la fuente de los nombres que
    # necesita el sidebar (seccion 36 del brief).
    "contact",
    "pushname",
    "mute",
    "pin",
    "archive",
)


@dataclass
class ClientEvent:
    """Evento reenviado a la GUI/aplicacion desde el hilo del cliente."""

    name: str
    payload: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


class WhatsAppClient:
    """Envoltorio de ``pywhats.Client`` con ciclo de vida propio.

    Responsabilidad unica: hablar el protocolo y publicar eventos. No sabe
    nada de PostgreSQL ni de Tkinter.
    """

    def __init__(self, settings: Settings, events: queue.Queue[ClientEvent]) -> None:
        self._settings = settings
        self._events = events
        self._client: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        # Marca que _main() ya retorno: evita programar un disconnect()
        # sobre un event loop que esta a punto de cerrarse.
        self._finished = threading.Event()
        # Corrutina opcional que se lanza cuando la sesion queda conectada.
        # La usa el servicio de multimedia, que necesita el event loop del
        # cliente pero NO debe bloquear al receptor de mensajes.
        self.post_connect: Any = None
        # Handlers sincronos que corren en el HILO DEL CLIENTE antes de
        # publicar el evento a la GUI. Sirven para persistir en PostgreSQL sin
        # pasar por el hilo de Tkinter.
        self.sinks: dict[str, Any] = {}
        # Aviso a los servicios de fondo (backfill, multimedia) para que
        # dejen de trabajar antes de cerrar la sesion.
        self.on_shutdown: Any = None

    # -- Estado --------------------------------------------------------------

    @property
    def session_exists(self) -> bool:
        """``True`` si hay un DeviceStore persistido reutilizable."""
        return self._settings.session_file.exists()

    @property
    def device(self) -> Any:
        return self._client.device if self._client is not None else None

    # -- Arranque ------------------------------------------------------------

    def start(self) -> None:
        """Lanza el hilo del cliente. No bloquea."""
        if self._thread is not None:
            raise RuntimeError("el cliente ya esta arrancado")
        self._thread = threading.Thread(target=self._run, name="wa-client", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._ready.set()
        try:
            loop.run_until_complete(self._main())
        except Exception as exc:  # noqa: BLE001 - se reporta, no se traga
            log.exception("El cliente de WhatsApp termino con error")
            self._publish("client_error", exc)
        finally:
            self._finished.set()
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
                self._loop = None
            self._publish("client_stopped", None)

    async def _shutdown(self, timeout: float) -> None:
        """Cierre ordenado DENTRO del event loop.

        El orden importa y es el que pedia el encargo: primero se detienen los
        trabajos de fondo (backfill, multimedia), luego se cierra la sesion, y
        solo despues el loop puede terminar. La version anterior programaba
        ``disconnect()`` desde fuera y cerraba el loop sin esperarlo, de ahi el
        "Task was destroyed but it is pending!".
        """
        if self.on_shutdown is not None:
            try:
                self.on_shutdown()
            except Exception:  # noqa: BLE001
                log.debug("El aviso de parada a los workers fallo")

        # Se cancela todo lo que quede en vuelo salvo esta propia corrutina.
        current = asyncio.current_task()
        pendientes = [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ]
        for task in pendientes:
            task.cancel()
        if pendientes:
            await asyncio.gather(*pendientes, return_exceptions=True)
            log.debug("Canceladas %d tareas en vuelo", len(pendientes))

        if self._client is not None:
            try:
                await asyncio.wait_for(self._client.disconnect(), timeout=timeout)
                log.info("Cliente desconectado limpiamente")
            except (TimeoutError, asyncio.TimeoutError):
                log.warning("La desconexion no termino en %.0fs", timeout)
            except Exception as exc:  # noqa: BLE001
                log.debug("Desconexion terminada con %s", type(exc).__name__)

    async def _main(self) -> None:
        from pywhats import Client

        session_path = str(self._settings.session_file)
        existed = self.session_exists
        log.info(
            "Sesion %s en %s",
            "existente detectada" if existed else "no encontrada",
            self._settings.session_file,
        )
        self._publish("session_state", {"exists": existed, "path": session_path})

        self._client = Client(session_path=session_path)
        self._register_handlers(self._client)

        # connect() cubre los dos caminos: si no hay device valido hace el
        # pairing (emitiendo 'qr'), y si lo hay hace login directo.
        if existed:
            await self._client.connect()
        else:
            await self._connect_with_pairing_retries()

        if self.post_connect is not None:
            # En segundo plano: los mensajes live siguen siendo prioritarios.
            asyncio.create_task(
                self._run_post_connect(), name="post-connect"
            )

        await self._client.wait_closed()

    async def _run_post_connect(self) -> None:
        """Trabajo posterior a la conexion (multimedia). Nunca tumba la sesion."""
        try:
            await self.post_connect(self._client)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("El trabajo posterior a la conexion fallo; la sesion sigue")

    async def _connect_with_pairing_retries(self) -> None:
        """Reintenta el pairing cuando el QR caduca sin escanearse.

        pywhats da 120 s por intento (``QR_TOTAL_TIMEOUT_SECONDS``) y no es
        configurable: el valor queda capturado en el ``__init__`` generado del
        dataclass ``Pairer``, asi que reasignar el atributo de clase no surte
        efecto (comprobado).

        Alargar ese plazo tampoco serviria: los ``ref`` que entrega el servidor
        caducan por su cuenta, de modo que esperar mas solo deja en pantalla un
        QR muerto. Lo que hace falta es pedir refs NUEVOS, y eso es volver a
        ejecutar el pairing.

        ``connect()`` es reentrante mientras no haya JID: reutiliza el mismo
        DeviceStore (las claves del companion no cambian) y construye un
        ``Pairer`` nuevo. El bucle esta acotado por configuracion y solo
        reacciona al timeout: cualquier otro ``PairingFailed`` (por ejemplo un
        405) se propaga en el primer intento, sin reintentos.
        """
        from pywhats.errors import PairingFailed

        attempts = max(1, self._settings.pairing_max_attempts)
        for attempt in range(1, attempts + 1):
            try:
                await self._client.connect()
                return
            except PairingFailed as exc:
                is_timeout = "timed out" in str(exc).lower()
                if not is_timeout or attempt >= attempts or self._stopping.is_set():
                    raise
                log.warning(
                    "El QR caduco sin escanearse (intento %d/%d). Generando uno nuevo...",
                    attempt,
                    attempts,
                )
                self._publish("qr_expired", {"attempt": attempt, "attempts": attempts})

    def _register_handlers(self, client: Any) -> None:
        """Suscribe un reenviador por cada evento real de pywhats."""

        def make_handler(event_name: str):
            async def handler(*args: Any) -> None:
                # pywhats invoca con 0 o 1 argumento segun el evento.
                payload = args[0] if args else None

                sink = self.sinks.get(event_name)
                if sink is not None:
                    try:
                        # A un hilo aparte: el sink escribe en PostgreSQL y
                        # bloquear el event loop retrasaria la recepcion del
                        # siguiente mensaje.
                        result = await asyncio.to_thread(sink, payload)
                        if result is not None:
                            self._publish(f"{event_name}_stored", result)
                    except Exception:  # noqa: BLE001 - el receptor manda
                        log.exception("El sink de %r fallo; se sigue escuchando", event_name)

                self._publish(event_name, payload)

            return handler

        for name in PYWHATS_EVENTS:
            client.on(name)(make_handler(name))

    # -- Publicacion de eventos ----------------------------------------------

    def _publish(self, name: str, payload: Any, **extra: Any) -> None:
        """Encola un evento para el hilo de la GUI. Nunca bloquea ni lanza."""
        try:
            self._events.put_nowait(ClientEvent(name=name, payload=payload, extra=extra))
        except Exception:  # noqa: BLE001 - una cola llena no debe tumbar el receptor
            log.warning("No se pudo encolar el evento %s", name)

    # -- Parada ordenada -----------------------------------------------------

    def stop(self, timeout: float = 10.0) -> None:
        """Cierra el cliente respetando la semantica de pywhats.

        Se usa ``client.disconnect()``, que es la API real del paquete, y se
        espera al teardown. No se sustituye por un ``sleep``.
        """
        if self._stopping.is_set():
            return
        self._stopping.set()

        loop = self._loop
        client = self._client

        if loop is not None and not self._finished.is_set() and loop.is_running():
            # El cierre se ejecuta DENTRO del loop y se espera aqui: asi no
            # queda ninguna corrutina pendiente cuando el loop termine.
            future = asyncio.run_coroutine_threadsafe(self._shutdown(timeout), loop)
            try:
                future.result(timeout=timeout + 5)
            except Exception as exc:  # noqa: BLE001
                log.debug("Cierre ordenado incompleto: %s", type(exc).__name__)
            if self._thread is not None:
                self._thread.join(timeout=timeout)
            return

        if self._finished.is_set():
            # El cliente ya termino por su cuenta (el servidor cerro la sesion,
            # o el pairing fallo). Programar aqui un disconnect() solo crearia
            # una tarea que el loop, ya cerrandose, destruiria sin ejecutar:
            # justo el "Task was destroyed but it is pending" que aparecia.
            log.debug("El cliente ya habia terminado; no hace falta desconectar")
        elif loop is not None and client is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(client.disconnect(), loop)
            try:
                future.result(timeout=timeout)
                log.info("Cliente desconectado limpiamente")
            except (asyncio.CancelledError, concurrent.futures.CancelledError):
                # El loop se cerro mientras se desconectaba. La sesion ya no
                # esta viva, que es lo que se buscaba: no es un fallo.
                log.debug("Desconexion cancelada por el cierre del loop")
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "La desconexion no completo limpiamente: %s",
                    exc or type(exc).__name__,
                )

        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning("El hilo del cliente sigue vivo tras %.0fs", timeout)


# ---------------------------------------------------------------------------
# Preparacion del entorno de pywhats
# ---------------------------------------------------------------------------


def prepare_pywhats(settings: Settings) -> dict[str, Any]:
    """Aplica compatibilidades y resuelve la version ANTES de crear el Client.

    El orden importa: ``wa_version.apply()`` tiene que ejecutarse antes de que
    se construya ningun ``ClientPayload``, porque ``pywhats.pairing`` copio
    ``WA_WEB_VERSION`` a su espacio de nombres al importarse.

    Devuelve un resumen de lo aplicado, para el log de arranque.
    """
    from app.compat import apply_all
    from app.compat import wa_version as wa_version_compat

    summary: dict[str, Any] = {"compat": [], "wa_version": None, "wa_version_origin": None}

    summary["compat"] = apply_all(settings)

    if settings.compat_wa_version:
        version, origin = wa_version_compat.resolve_and_apply(
            settings.wa_version_cache, timeout=settings.wa_version_fetch_timeout
        )
        summary["wa_version"] = version
        summary["wa_version_origin"] = origin
    else:
        import pywhats.version

        log.warning(
            "COMPAT_WA_VERSION desactivado: se usara la version fija de pywhats %s, "
            "que el servidor rechaza con 405",
            pywhats.version.WA_WEB_VERSION,
        )
        summary["wa_version"] = pywhats.version.WA_WEB_VERSION
        summary["wa_version_origin"] = "pywhats"

    return summary


def archive_session(settings: Settings, reason: str) -> Path | None:
    """Mueve la sesion actual a diagnostics/ en vez de borrarla.

    Se usa solo cuando el usuario lo pide de forma explicita (``--fresh``).
    Nunca de forma automatica ante un error: un 401 puede ser transitorio y
    tirar la sesion convierte un diagnostico en una perdida.
    """
    from datetime import datetime

    if not settings.session_file.exists():
        return None

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = settings.diagnostics_dir / f"session-{stamp}-{reason}"
    destination.mkdir(parents=True, exist_ok=True)

    moved = []
    for path in settings.session_dir.iterdir():
        if path.is_file():
            path.replace(destination / path.name)
            moved.append(path.name)
    log.info("Sesion archivada en %s (%d archivos: %s)", destination, len(moved), ", ".join(moved))
    return destination
