"""Cliente de WhatsApp: pywhats corriendo en su propio hilo con event loop.

Modelo de concurrencia:

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

from app.core.config import Settings
from app.core.logging_setup import get_logger

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
    # necesita el sidebar.
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
        # Reconexion. El socket se muere solo (WiFi que cae, portatil que se
        # suspende, el servidor que cierra): se midio "app ping failed 3/3 ->
        # peer presumed dead -> closing connection", y despues de eso el
        # proceso seguia vivo pero sordo. Detectar el socket muerto estaba
        # bien; lo que faltaba era volver a levantarlo.
        self._logged_out = False
        self._reconnects = 0
        # Aviso a los servicios de fondo (backfill, multimedia) para que
        # dejen de trabajar antes de cerrar la sesion.
        self.on_shutdown: Any = None
        # Lo crea _main() dentro de su propio loop: un asyncio.Event debe
        # nacer en el loop que lo va a esperar.
        self._shutdown_requested: asyncio.Event | None = None
        self._shutdown_timeout = 10.0

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

        self._shutdown_requested = asyncio.Event()

        if self.post_connect is not None:
            # En segundo plano: los mensajes live siguen siendo prioritarios.
            asyncio.create_task(
                self._run_post_connect(), name="post-connect"
            )

        # Se espera a que la sesion se cierre O a que alguien pida parar. Asi
        # el cierre ordenado ocurre DENTRO de esta corrutina y el loop no
        # termina con _shutdown() a medias (el "Task was destroyed").
        while True:
            # Se espera a que la sesion se cierre O a que alguien pida parar.
            # Asi el cierre ordenado ocurre DENTRO de esta corrutina y el loop
            # no termina con _shutdown() a medias (el "Task was destroyed").
            closed = asyncio.create_task(
                self._client.wait_closed(), name="wait-closed"
            )
            requested = asyncio.create_task(
                self._shutdown_requested.wait(), name="shutdown-requested"
            )
            try:
                done, _pending = await asyncio.wait(
                    {closed, requested}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for task in (closed, requested):
                    if not task.done():
                        task.cancel()
                # CancelledError propia del cierre: se absorbe a proposito.
                # Una cancelacion inesperada seguiria propagandose.
                await asyncio.gather(closed, requested, return_exceptions=True)

            if requested in done:
                log.info("Parada solicitada: cerrando la sesion")
                await self._shutdown(self._shutdown_timeout)
                return

            # El socket se cerro sin que nadie lo pidiera. Lo primero es
            # cortar lo que este esperando una respuesta que ya no puede
            # llegar: si no, esas esperas agotan su tiempo y se apuntan como
            # "el telefono no contesto", que seria una conclusion falsa.
            self._publish("transport_lost", None)

            if self._logged_out:
                log.warning(
                    "La sesion fue rechazada por el servidor: no se reconecta"
                )
                # Y se cierra ORDENADAMENTE, aunque no vayamos a reconectar.
                #
                # Salir sin cerrar dejaba el Signal Store abierto, porque quien
                # lo cierra es ``Client.disconnect()``. Con el handle vivo,
                # archivar la sesion revocada no podia mover
                # ``device.json.signal.db``: se saltaba por bloqueado y el
                # pairing siguiente nacia con un device.json NUEVO sobre un
                # store VIEJO. Se midio esa mezcla: un dispositivo recien
                # vinculado con 14 sesiones Signal y 8 sender keys heredadas,
                # y "unknown one-time pre-key id 66" en cada mensaje.
                #
                # De paso se cancelan las tareas en vuelo, que era el origen de
                # los "Task was destroyed but it is pending! post-connect".
                await self._shutdown(self._shutdown_timeout)
                return
            if not await self._reconectar():
                return

    # Espera entre intentos de reconexion. Escala corta al principio (un
    # corte de WiFi de dos segundos no debe costar un minuto de silencio) y
    # techo bajo al final, para no golpear el servidor si el corte dura horas.
    RECONEXION_ESPERAS = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)
    RECONEXION_MAXIMA = 30.0

    def _espera_reconexion(self, intento: int) -> float:
        """Segundos antes del intento ``intento`` (1-based), con jitter.

        El jitter evita que, si algo tira la conexion de varios clientes a la
        vez, todos vuelvan a llamar en el mismo instante.
        """
        import random

        indice = min(intento, len(self.RECONEXION_ESPERAS)) - 1
        base = self.RECONEXION_ESPERAS[max(0, indice)]
        return base + random.uniform(0.0, base * 0.25)

    async def _reconectar(self) -> bool:
        """Levanta la sesion otra vez, con la MISMA identidad y el mismo Signal.

        No se borra nada: ni ``device.json``, ni el Signal Store, ni las
        prekeys. Un socket muerto no invalida la vinculacion, y tratarlo como
        si lo hiciera obligaria a escanear un QR cada vez que se cae el WiFi.

        Devuelve ``False`` cuando hay que rendirse (parada solicitada o sesion
        rechazada); ``True`` cuando ya esta reconectado.
        """
        from pywhats import Client

        while not self._shutdown_requested.is_set():
            self._reconnects += 1
            espera = self._espera_reconexion(self._reconnects)
            log.warning(
                "Conexion perdida. Reintento %d en %.0fs (la sesion NO se toca)",
                self._reconnects,
                espera,
            )
            self._publish(
                "reconnecting",
                {"attempt": self._reconnects, "delay_seconds": espera},
            )

            try:
                await asyncio.wait_for(
                    self._shutdown_requested.wait(), timeout=espera
                )
                # Han pedido parar mientras se esperaba.
                await self._shutdown(self._shutdown_timeout)
                return False
            except asyncio.TimeoutError:
                pass

            try:
                self._client = Client(session_path=str(self._settings.session_file))
                self._register_handlers(self._client)
                await self._client.connect()
            except Exception as exc:  # noqa: BLE001 - se reintenta
                log.warning("Reintento %d fallido: %s", self._reconnects, exc)
                continue

            log.info("Reconectado tras %d intento(s)", self._reconnects)
            self._reconnects = 0
            self._publish("reconnected", None)

            # El backfill y la multimedia tienen una referencia al cliente
            # ANTERIOR. ``post_connect`` la renueva y, de paso, reconcilia lo
            # que haya entrado mientras el socket estaba muerto: los eventos
            # de ese rato no llegaron, y no se pueden dar por recibidos.
            if self.post_connect is not None:
                # Se avisa de que esto es una RECONEXION: hay que reconciliar
                # lo que entro mientras el socket estaba muerto.
                destino = getattr(self.post_connect, "__self__", None)
                if destino is not None:
                    setattr(destino, "_reconexion", True)
                asyncio.create_task(
                    self._run_post_connect(), name="post-connect-reconexion"
                )
            return True

        return False

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
                # pywhats invoca con 0, 1 o 2 argumentos segun el evento.
                # ``decrypt_error`` manda (message_id, motivo): quedarse solo
                # con el primero perdia justo el dato que dice POR QUE fallo.
                payload = args[0] if args else None
                detalles = {"args": list(args[1:])} if len(args) > 1 else {}

                if event_name == "logged_out":
                    # El servidor rechazo la sesion. Aqui NO se reconecta:
                    # reintentar con una sesion muerta es el bucle que ya
                    # costo 74 logins en segundos.
                    self._logged_out = True

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

                self._publish(event_name, payload, **detalles)

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
        event = self._shutdown_requested

        if loop is not None and event is not None and not self._finished.is_set():
            # Solo se SENALIZA. El cierre lo ejecuta _main() dentro del loop,
            # asi que cuando el hilo termine no puede quedar nada pendiente.
            self._shutdown_timeout = timeout
            loop.call_soon_threadsafe(event.set)
            if self._thread is not None:
                self._thread.join(timeout=timeout + 10)
                if self._thread.is_alive():
                    log.warning("El hilo del cliente sigue vivo tras %.0fs", timeout)
                else:
                    log.info("Hilo del cliente terminado limpiamente")
            return

        if self._finished.is_set():
            # El cliente ya termino por su cuenta (el servidor cerro la sesion,
            # o el pairing fallo). Programar aqui un disconnect() solo crearia
            # una tarea que el loop, ya cerrandose, destruiria sin ejecutar:
            # justo el "Task was destroyed but it is pending" que aparecia.
            log.debug("El cliente ya habia terminado; no hace falta desconectar")
        elif loop is not None and self._client is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._client.disconnect(), loop)
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

    NUNCA lanza. Se midio por que hace falta: al archivar tras un 401, Windows
    devolvia ``WinError 32`` sobre ``compat_prekey.db`` porque nuestro propio
    proceso lo tenia abierto. La excepcion abortaba el resto del manejo del
    error y el sistema entraba en un bucle de reintentos.

    Si un archivo esta bloqueado se salta y se deja constancia: llevarse el
    ``device.json`` ya basta para que el siguiente arranque vincule limpio, y
    un registro de prekeys huerfano no impide nada.

    Devuelve ``None`` si no habia nada que archivar, y en ese caso no deja
    carpeta vacia detras (se llegaron a crear 99 en un bucle).
    """
    from datetime import datetime

    if not settings.session_file.exists():
        return None

    # El registro de prekeys es nuestro y lo tenemos abierto: cerrarlo antes
    # evita el bloqueo en Windows.
    try:
        from app.compat import prekey_compat

        if getattr(prekey_compat, "_registry", None) is not None:
            prekey_compat._registry.close()
            prekey_compat._registry = None
    except Exception:  # noqa: BLE001 - cerrarlo es una mejora, no un requisito
        log.debug("No se pudo cerrar el registro de prekeys antes de archivar")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = settings.diagnostics_dir / f"session-{stamp}-{reason}"
    destination.mkdir(parents=True, exist_ok=True)

    moved: list[str] = []
    bloqueados: list[str] = []
    for path in sorted(settings.session_dir.iterdir()):
        if not path.is_file():
            continue
        try:
            path.replace(destination / path.name)
            moved.append(path.name)
        except OSError as exc:
            # Bloqueado por otro proceso (o por nosotros). No es motivo para
            # abortar: lo importante es que device.json salga de en medio.
            bloqueados.append(path.name)
            log.debug("No se pudo archivar %s: %s", path.name, exc)

    if not moved:
        # Sin nada movido, la carpeta sobra.
        try:
            destination.rmdir()
        except OSError:  # pragma: no cover
            pass
        log.warning(
            "No se pudo archivar la sesion (%d archivos bloqueados)", len(bloqueados)
        )
        return None

    log.info(
        "Sesion archivada en %s (%d archivos: %s%s)",
        destination,
        len(moved),
        ", ".join(moved),
        f"; bloqueados: {', '.join(bloqueados)}" if bloqueados else "",
    )
    return destination
