"""Estado de la sesion: unica autoridad sobre lo que la GUI puede mostrar.

REGLA: que existan ``session/device.json`` y filas en
PostgreSQL NO significa que la sesion sea valida. Solo lo es cuando el
servidor acepta el login.

Por que hace falta una senal explicita
-------------------------------------
pywhats emite ``connected`` en cuanto termina el handshake Noise, ANTES de
que el servidor conteste. Si la sesion esta revocada, la secuencia real es::

    connected                       <- pywhats, optimista
    receiver: server <failure> reason=401 -- login rejected
    logged_out 401

Por eso ``connected`` no puede abrir el visor: es provisional. La confirmacion
de verdad es el ``<success>`` que procesa ``SessionActivator.on_success`` (el
servidor no lo envia si rechaza el login), y el rechazo es ``logged_out``.

Los datos locales NUNCA se borran al invalidarse una sesion: PostgreSQL es el
backup y sobrevive a la desvinculacion.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from app.core.logging_setup import get_logger

log = get_logger("WA")


class AppState(str, Enum):
    """Estados explicitos de la aplicacion."""

    STARTING = "STARTING"
    CHECKING_SESSION = "CHECKING_SESSION"
    # No hay device.json: nunca se vinculo este equipo.
    NO_SESSION = "NO_SESSION"
    # Hace falta vincular (no hay sesion, o la que habia ya no vale).
    PAIRING_REQUIRED = "PAIRING_REQUIRED"
    # El flujo de vinculacion esta en marcha pero todavia no hay QR.
    PAIRING = "PAIRING"
    # Hay un QR vigente esperando a que lo escaneen.
    QR_READY = "QR_READY"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    # Conectado, pero el History Sync inicial aun esta llegando. Lanzar el
    # backfill aqui produce "0 candidatos" y luego el bootstrap llega tarde.
    WAITING_INITIAL_HISTORY = "WAITING_INITIAL_HISTORY"
    SESSION_INVALID = "SESSION_INVALID"
    DISCONNECTED = "DISCONNECTED"
    # El socket se murio y se esta volviendo a levantar con la MISMA sesion.
    # Es distinto de DISCONNECTED (ahi no se esta haciendo nada) y sobre todo
    # de CONNECTED: Flask puede seguir escuchando con WhatsApp muerto, y decir
    # "conectado" en ese estado enganaria al frontend.
    RECONNECTING = "RECONNECTING"
    ERROR = "ERROR"


# Estados en los que se puede mostrar el visor de conversaciones.
# El visor puede abrirse mientras llega el historial inicial: la sesion ya
# esta confirmada por el servidor y hay datos locales que mostrar.
VIEWER_ALLOWED = frozenset({AppState.CONNECTED, AppState.WAITING_INITIAL_HISTORY})

# Estados terminales de una sesion que ya no sirve. Entrar en uno de ellos
# avanza la generacion y caduca cualquier trabajo en vuelo.
#
# PAIRING y QR_READY NO estan aqui a proposito: son fases EN CURSO de una
# vinculacion, no una sesion muerta. Meterlas haria avanzar la generacion cada
# vez que rota el QR (cada 20 s), y eso invalidaria trabajo que si es valido.
SESSION_DEAD = frozenset(
    {AppState.SESSION_INVALID, AppState.PAIRING_REQUIRED, AppState.NO_SESSION}
)

# Estados en los que la vinculacion esta en marcha.
PAIRING_STATES = frozenset({AppState.PAIRING, AppState.QR_READY})

# Estados en los que hace falta vincular, sea porque no hay sesion o porque la
# que habia dejo de valer.
NEEDS_PAIRING = frozenset(
    {AppState.NO_SESSION, AppState.PAIRING_REQUIRED, AppState.SESSION_INVALID}
)

# Estados que implican que este equipo esta vinculado a una cuenta.
LINKED_STATES = frozenset(
    {
        AppState.CONNECTING,
        AppState.CONNECTED,
        AppState.WAITING_INITIAL_HISTORY,
        AppState.DISCONNECTED,
        AppState.RECONNECTING,
        AppState.CHECKING_SESSION,
    }
)


@dataclass(frozen=True)
class StateChange:
    previous: AppState
    current: AppState
    generation: int
    reason: str | None = None


class SessionState:
    """Maquina de estados con generacion, para descartar eventos tardios.

    Cada vez que la sesion muere o se reinicia, la generacion avanza. Un
    worker lento que termine despues trae su generacion vieja y su resultado
    se ignora, en vez de repintar chats sobre una pantalla de "sesion
    invalida".
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = AppState.STARTING
        self._generation = 0
        self._listeners: list[Callable[[StateChange], None]] = []

    # -- Consulta ------------------------------------------------------------

    @property
    def state(self) -> AppState:
        with self._lock:
            return self._state

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def viewer_allowed(self) -> bool:
        """``True`` solo si la sesion esta confirmada por el servidor."""
        return self.state in VIEWER_ALLOWED

    def is_current(self, generation: int) -> bool:
        """``False`` si este resultado pertenece a una sesion ya superada."""
        return generation == self.generation

    # -- Transiciones --------------------------------------------------------

    def on_change(self, listener: Callable[[StateChange], None]) -> None:
        self._listeners.append(listener)

    def set(self, new_state: AppState, *, reason: str | None = None) -> StateChange:
        with self._lock:
            previous = self._state
            if new_state in SESSION_DEAD and previous not in SESSION_DEAD:
                # La sesion muere: todo lo que estuviera en vuelo caduca.
                self._generation += 1
            self._state = new_state
            change = StateChange(
                previous=previous,
                current=new_state,
                generation=self._generation,
                reason=reason,
            )

        if previous != new_state:
            log.info(
                "Estado: %s -> %s%s",
                previous.value,
                new_state.value,
                f" ({reason})" if reason else "",
            )
        for listener in list(self._listeners):
            try:
                listener(change)
            except Exception:  # noqa: BLE001 - un listener roto no bloquea el estado
                log.exception("Un listener de estado fallo")
        return change


# ---------------------------------------------------------------------------
# Senal de sesion aceptada por el servidor
# ---------------------------------------------------------------------------

_success_callback: Callable[[], None] | None = None
_MARKER = "_whatsapp_backup_success_hook"


def set_success_callback(callback: Callable[[], None] | None) -> None:
    global _success_callback
    _success_callback = callback


def install_success_hook() -> bool:
    """Avisa cuando llega el ``<success>`` del servidor. Idempotente.

    Es la unica confirmacion fiable de que el login fue aceptado: si la
    sesion esta revocada el servidor manda ``<failure reason="401">`` y este
    camino no se ejecuta nunca.

    pywhats no expone ese momento como evento, asi que se envuelve
    ``SessionActivator.on_success``. El comportamiento original se mantiene
    intacto: solo se anade el aviso.
    """
    from pywhats.messaging.activator import SessionActivator

    original = SessionActivator.on_success
    if getattr(original, _MARKER, False):
        return True

    async def on_success(self: Any, node: Any) -> None:
        if _success_callback is not None:
            try:
                _success_callback()
            except Exception:  # noqa: BLE001 - no romper la activacion
                log.exception("El callback de <success> fallo")
        await original(self, node)

    setattr(on_success, _MARKER, True)
    SessionActivator.on_success = on_success  # type: ignore[method-assign]
    return True
