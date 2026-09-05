"""Arranca, vigila y cierra el worker de Node. Nada mas.

POR QUE VIVE DENTRO DE service.py
---------------------------------
Porque el usuario ejecuta UNA cosa. Un segundo proyecto que hay que arrancar a
mano es un segundo proyecto que se queda a medias, que se olvida encendido y
que nadie sabe si estaba corriendo cuando se midio algo.

EL CANAL
--------
JSON Lines por las tuberias del proceso hijo. No hay otro servidor HTTP: un
puerto mas es un puerto mas que asegurar y que puede chocar con algo. Las
tuberias se cierran solas cuando el proceso muere y nadie de fuera puede
hablar por ellas.

``stdout`` del hijo es SOLO protocolo. Lo legible para humanos va por
``stderr`` y se reenvia al log con etiqueta ``[WEB]``.

OPCIONAL DE VERDAD
------------------
Si esto esta apagado, o Node no esta, o el worker se muere, el resto del
sistema funciona exactamente igual. Un fallo aqui se anota y se sigue: pywhats,
la API, el historial y Drive no dependen de esto para nada.

LO QUE NO HACE
--------------
No escribe en PostgreSQL, no toca la sesion de pywhats, no comparte
criptografia y no pide historial. Lanza un proceso, le habla y devuelve lo que
conteste.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("WEB")

#: Los estados que puede tener el companion. Los mismos que emite el worker,
#: mas ``disabled``, que es cosa de Python.
ESTADOS = (
    "disabled",
    "starting",
    "qr_required",
    "connected",
    "ready",
    "error",
    "stopped",
)

#: Cuanto se espera una respuesta antes de darla por perdida. El sondeo de
#: decenas de conversaciones tarda, asi que se pide por comando.
TIMEOUT_POR_DEFECTO = 30.0

#: Reintentos de arranque si el worker se muere. Creciente y con tope: un
#: bucle rapido contra un Chromium que no arranca solo quema la maquina.
BACKOFF = (5.0, 30.0, 120.0)


class WebCompanionNoDisponible(RuntimeError):
    """No se puede hablar con el worker ahora mismo."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass
class EstadoWeb:
    """Lo ultimo que se sabe del worker. Se lee sin bloquear."""

    state: str = "disabled"
    qr: str | None = None
    #: Sube con cada QR NUEVO. Es lo que le dice al navegador que la imagen
    #: cambio: sin esto, la anterior se quedaria en cache y el usuario estaria
    #: escaneando un codigo muerto.
    qr_generation: int = 0
    qr_received_at: float | None = None
    error: str | None = None
    capabilities: dict[str, Any] | None = None
    diagnostics: dict[str, Any] | None = None
    authenticated: bool = False
    web_client_ready: bool = False
    store_ready: bool = False
    probe_running: bool = False
    startup_timeout: bool = False
    pid: int | None = None
    started_at: float | None = None
    restarts: int = 0
    settings: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            # El payload NO sale por la API: se pinta en el backend y el
            # frontend pide la imagen. Mandar la cadena obliga al navegador a
            # cargar una libreria para dibujarla, y ademas deja una credencial
            # de vinculacion viajando en un JSON que cualquiera puede mirar.
            "qr_available": bool(self.qr),
            "qr_generation": self.qr_generation,
            # No se promete una caducidad: whatsapp-web.js no la da. Lo que si
            # se puede decir es cuanto lleva vivo, y el frontend repinta en
            # cuanto cambia la generacion.
            "qr_age_seconds": (
                int(time.monotonic() - self.qr_received_at) if self.qr_received_at else None
            ),
            "error": self.error,
            "capabilities": self.capabilities,
            "diagnostics": self.diagnostics,
            "authenticated": self.authenticated,
            "web_client_ready": self.web_client_ready,
            "store_ready": self.store_ready,
            "probe_running": self.probe_running,
            "startup_timeout": self.startup_timeout,
            "pid": self.pid,
            "restarts": self.restarts,
            "uptime_seconds": (
                int(time.monotonic() - self.started_at) if self.started_at else None
            ),
            "settings": dict(self.settings),
        }


class WebCompanionSupervisor:
    """El unico que habla con Node."""

    def __init__(self, settings: Any, *, raiz: Path | None = None) -> None:
        self._settings = settings
        self._raiz = Path(raiz) if raiz else Path(__file__).resolve().parents[2] / "web_companion"
        self._proceso: subprocess.Popen[str] | None = None
        self._estado = EstadoWeb()
        self._candado = threading.RLock()
        self._respuestas: Queue[dict[str, Any]] = Queue()
        self._parando = False
        self._siguiente_id = 1

    # -- Consulta ------------------------------------------------------------

    @property
    def habilitado(self) -> bool:
        return bool(getattr(self._settings, "web_companion_enabled", False))

    @property
    def vivo(self) -> bool:
        return self._proceso is not None and self._proceso.poll() is None

    def snapshot(self) -> dict[str, Any]:
        with self._candado:
            datos = self._estado.to_json()
        datos["enabled"] = self.habilitado
        datos["running"] = self.vivo
        datos["process_running"] = self.vivo
        return datos

    def qr_payload(self) -> tuple[str | None, int]:
        """El payload vigente y su generacion. Solo para dibujar la imagen.

        Se devuelve la generacion junto al payload para que quien pinte pueda
        decir CUAL pinto: entre leerlo y servirlo puede haber llegado otro.
        """
        with self._candado:
            return self._estado.qr, self._estado.qr_generation

    def comprobar_entorno(self) -> tuple[bool, str]:
        """Si se puede arrancar, y por que no si no se puede.

        Se comprueba ANTES de intentarlo para poder decir exactamente que
        falta, en vez de dejar un ``ENOENT`` suelto en el log.
        """
        if not self.habilitado:
            return False, "desactivado (WEB_COMPANION_ENABLED=false)"
        if shutil.which("node") is None:
            return False, "Node no esta instalado o no esta en el PATH"
        if not (self._raiz / "worker.js").exists():
            return False, f"falta {self._raiz / 'worker.js'}"
        if not (self._raiz / "node_modules").is_dir():
            return False, (
                "faltan las dependencias de Node: ejecuta "
                "'py tools/setup_web_companion.py'"
            )
        return True, "listo"

    # -- Ciclo de vida -------------------------------------------------------

    def permitido(self) -> bool:
        """Si la conexion principal permite que este dispositivo trabaje.

        La sesion principal manda. Sin ella, el segundo dispositivo no arranca,
        no pide codigo y no sondea: lo unico que conseguiria es confundir al
        usuario sobre cual de los dos codigos tiene que escanear.

        Sin runtime cableado se permite, para no romper el uso directo del
        supervisor en pruebas y herramientas.
        """
        runtime = getattr(self, "runtime", None)
        if runtime is None:
            return True
        from app.core.primary import primary_ready

        return primary_ready(runtime)

    def start(self) -> bool:
        """Arranca el worker. Devuelve si de verdad quedo en marcha.

        Nunca lanza: que esto falle no puede impedir que arranque el resto.
        """
        if not self.habilitado:
            self._fijar(state="disabled")
            log.info("desactivado")
            return False

        ok, motivo = self.comprobar_entorno()
        if not ok:
            self._fijar(state="error", error=motivo)
            log.warning("no se puede arrancar: %s", motivo)
            return False

        with self._candado:
            if self.vivo:
                return True
            try:
                self._proceso = subprocess.Popen(  # noqa: S603 - orden construida aqui
                    ["node", "worker.js"],
                    cwd=str(self._raiz),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=self._entorno(),
                )
            except Exception as exc:  # noqa: BLE001 - arrancar no puede tumbar nada
                self._fijar(state="error", error=str(exc)[:200])
                log.warning("no se pudo lanzar el worker: %s", exc)
                return False

            self._parando = False
            self._fijar(
                state="starting", pid=self._proceso.pid, error=None,
                started_at=time.monotonic(), authenticated=False,
                web_client_ready=False, store_ready=False, probe_running=False,
                startup_timeout=False, capabilities=None, diagnostics=None,
            )

        threading.Thread(target=self._leer_salida, name="web-companion-out", daemon=True).start()
        threading.Thread(target=self._leer_errores, name="web-companion-err", daemon=True).start()
        threading.Thread(target=self._vigilar, name="web-companion-watch", daemon=True).start()
        log.info("worker iniciado (pid=%s)", self._proceso.pid)
        return True

    def stop(self, timeout: float = 8.0) -> None:
        """Cierra el worker limpiamente. Idempotente y nunca lanza."""
        with self._candado:
            self._parando = True
            proceso = self._proceso
            self._proceso = None
        if proceso is None:
            return

        try:
            # Primero por el canal: le da ocasion de cerrar Chromium bien.
            if proceso.stdin and not proceso.stdin.closed:
                proceso.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
                proceso.stdin.flush()
                proceso.stdin.close()
        except Exception:  # noqa: BLE001
            pass

        try:
            proceso.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("el worker no cerro en %.0fs; se termina", timeout)
            proceso.kill()
            try:
                proceso.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        self._fijar(state="stopped", pid=None, started_at=None)
        log.info("worker detenido")

    def _entorno(self) -> dict[str, str]:
        """Lo que necesita el worker. Los interruptores viven en el .env."""
        entorno = dict(os.environ)
        entorno["WEB_COMPANION_SESSION_DIR"] = str(
            Path(self._settings.session_dir) / "web_companion"
        )
        for clave, ajuste in (
            ("WEB_STORE_LOAD_EARLIER", "web_store_load_earlier"),
            ("WEB_STORE_DISCOVERY_SCROLL", "web_store_discovery_scroll"),
        ):
            entorno[clave] = "true" if getattr(self._settings, ajuste, False) else "false"
        chrome = getattr(self._settings, "web_companion_chrome", "") or ""
        if chrome:
            entorno["WEB_COMPANION_CHROME"] = chrome
        return entorno

    # -- Lectura de las tuberias ---------------------------------------------

    def _leer_salida(self) -> None:
        """``stdout``: protocolo. Una linea, un evento."""
        proceso = self._proceso
        if proceso is None or proceso.stdout is None:
            return
        for linea in proceso.stdout:
            linea = linea.strip()
            if not linea:
                continue
            try:
                evento = json.loads(linea)
            except json.JSONDecodeError:
                # Algo escribio texto suelto en stdout. Se anota y se sigue:
                # el canal se recupera en la siguiente linea.
                log.debug("linea no interpretable del worker")
                continue
            self._procesar(evento)

    def _procesar(self, evento: dict[str, Any]) -> None:
        tipo = evento.get("event")
        if tipo == "state":
            self._actualizar_qr(evento.get("qr"))
            cambios = {"state": str(evento.get("state") or "error"), "error": evento.get("error")}
            for clave in (
                "capabilities", "diagnostics", "authenticated", "web_client_ready",
                "store_ready", "probe_running", "startup_timeout",
            ):
                if clave in evento:
                    cambios[clave] = evento[clave]
            self._fijar(**cambios)
            self._anunciar(evento)
        elif tipo == "starting":
            self._fijar(state="starting", pid=evento.get("pid"))
        elif tipo == "inventory_result":
            metricas = evento.get("metrics") or {}
            log.info(
                "inventory client=%d store=%d union=%d",
                int(metricas.get("web_get_chats", 0) or 0),
                int(metricas.get("web_store_chats", 0) or 0),
                int(metricas.get("union_chats", 0) or 0),
            )
        elif tipo == "seed_probe_result":
            resumen = evento.get("summary") or {}
            log.info(
                "probe waiting=%d visibles=%d mensajes=%d seeds=%d",
                int(resumen.get("waiting", 0) or 0),
                int(resumen.get("visible_store", 0) or 0),
                int(resumen.get("with_messages", 0) or 0),
                int(resumen.get("seed_usable", 0) or 0),
            )
        # Las respuestas a un comando llevan ``id`` y van a la cola de espera.
        if evento.get("id") is not None or tipo in (
            "status",
            "inventory_result",
            "seed_probe_result",
        ):
            self._respuestas.put(evento)

    def _actualizar_qr(self, payload: Any) -> None:
        """Guarda el QR vigente. Uno nuevo REEMPLAZA al anterior.

        Y cuando el worker deja de pedirlo —porque ya se vinculo— se borra:
        dejarlo ahi haria que el panel siguiera ensenando un codigo muerto.
        """
        with self._candado:
            if not payload:
                self._estado.qr = None
                self._estado.qr_received_at = None
                return
            if payload == self._estado.qr:
                return
            self._estado.qr = str(payload)
            self._estado.qr_generation += 1
            self._estado.qr_received_at = time.monotonic()

    def _anunciar(self, evento: dict[str, Any]) -> None:
        estado = evento.get("state")
        if evento.get("startup_timeout"):
            log.warning("worker activo pero WhatsApp Web no llego a ready")
        elif estado == "qr_required":
            # El QR rota cada pocos segundos. La PRIMERA vez se ve; las
            # siguientes son la misma noticia repetida y van a DEBUG.
            with self._candado:
                generacion = self._estado.qr_generation
            if generacion <= 1:
                log.info(
                    "QR requerido para el Web Companion (escanealo desde "
                    "Dispositivos vinculados; NO sustituye al principal)"
                )
            else:
                log.debug("QR renovado (generacion=%d)", generacion)
        elif estado == "ready":
            disponibles = sum(1 for v in (evento.get("capabilities") or {}).values() if v is True)
            if evento.get("store_ready"):
                log.info("Store listo capacidades=%d", disponibles)
            else:
                log.warning("client ready, pero Store no estuvo listo dentro del plazo")
        elif estado == "connected" and evento.get("web_client_ready"):
            log.info("client ready; Store esperando...")
        elif estado == "connected" and evento.get("authenticated"):
            log.info("autenticado")
        elif estado == "error":
            log.warning("error: %s", str(evento.get("error"))[:120])

    def _leer_errores(self) -> None:
        """``stderr``: lo legible. Va a DEBUG para no llenar la consola."""
        proceso = self._proceso
        if proceso is None or proceso.stderr is None:
            return
        for linea in proceso.stderr:
            texto = linea.strip()
            if texto:
                log.debug("%s", texto[:200])

    def _vigilar(self) -> None:
        """Si el worker muere, se anota y se reintenta con espera creciente.

        Nunca en bucle rapido: un Chromium que no arranca no arranca mejor por
        intentarlo cien veces seguidas.
        """
        proceso = self._proceso
        if proceso is None:
            return
        codigo = proceso.wait()
        if self._parando:
            return

        with self._candado:
            reinicios = self._estado.restarts
        self._fijar(state="error", error=f"el worker termino con codigo {codigo}", pid=None)
        log.warning("el worker murio (codigo=%s); pywhats y la API siguen igual", codigo)

        if reinicios >= len(BACKOFF):
            log.warning("no se reintenta mas; usa el panel para volver a arrancarlo")
            return
        espera = BACKOFF[reinicios]
        with self._candado:
            self._estado.restarts = reinicios + 1
        log.info("se reintentara en %.0fs", espera)
        time.sleep(espera)
        self._reintentar()

    def _reintentar(self) -> None:
        """Volver a levantar el worker, si es que toca.

        Aparte para poder comprobarlo sin arrancar un Chromium: la decision
        que importa no es cuanto se espera, es a quien se le pregunta antes.

        Y NO se reintenta si la conexion principal ya no esta. Este reinicio
        automatico es lo que produjo el fallo medido: la sesion principal cayo
        a NO_SESSION y este supervisor siguio levantando el worker con su
        espera creciente. Cada arranque sin sesion guardada publica un codigo
        QR nuevo, asi que el usuario se quedo mirando el codigo equivocado --
        el que hacia falta escanear era el principal.
        """
        if self._parando:
            return
        if not self.permitido():
            log.info(
                "no se reintenta el segundo dispositivo: la conexion principal "
                "no esta lista"
            )
            self._fijar(state="blocked_by_primary", error=None, pid=None)
            return
        self.start()

    def _fijar(self, **campos: Any) -> None:
        with self._candado:
            for clave, valor in campos.items():
                setattr(self._estado, clave, valor)

    # -- Comandos ------------------------------------------------------------

    def enviar(self, comando: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        """Manda un comando y espera su respuesta.

        :raises WebCompanionNoDisponible: si no hay worker con quien hablar.
        """
        if not self.habilitado:
            raise WebCompanionNoDisponible("WEB_COMPANION_DISABLED", "El Web Companion esta apagado.")
        if not self.vivo:
            raise WebCompanionNoDisponible(
                "WEB_COMPANION_NOT_RUNNING", "El Web Companion no esta en marcha."
            )

        with self._candado:
            identificador = self._siguiente_id
            self._siguiente_id += 1
            proceso = self._proceso

        # Se vacia lo que hubiera quedado de antes: una respuesta huerfana de
        # un comando anterior no puede contestar a este.
        while True:
            try:
                self._respuestas.get_nowait()
            except Empty:
                break

        payload = {**comando, "id": identificador}
        es_probe = comando.get("cmd") in ("inventory", "probe_waiting_seeds")
        if es_probe:
            self._fijar(probe_running=True)
        try:
            assert proceso is not None and proceso.stdin is not None
            proceso.stdin.write(json.dumps(payload) + "\n")
            proceso.stdin.flush()
        except Exception as exc:  # noqa: BLE001
            if es_probe:
                self._fijar(probe_running=False)
            raise WebCompanionNoDisponible(
                "WEB_COMPANION_WRITE_FAILED", f"No se pudo hablar con el worker: {exc}"
            ) from exc

        limite = time.monotonic() + (timeout or TIMEOUT_POR_DEFECTO)
        while time.monotonic() < limite:
            try:
                evento = self._respuestas.get(timeout=0.5)
            except Empty:
                continue
            if evento.get("id") in (identificador, None):
                if es_probe:
                    self._fijar(probe_running=False)
                return evento
        if es_probe:
            self._fijar(probe_running=False)
        raise WebCompanionNoDisponible(
            "WEB_COMPANION_TIMEOUT", "El Web Companion no contesto a tiempo."
        )
