"""Bus de eventos con reparto a varios consumidores.

POR QUE HACE FALTA
------------------
Hasta ahora ``WhatsAppClient`` escribia en UNA ``queue.Queue`` que vaciaba
Tkinter. Con dos adaptadores (la ventana y la API) eso ya no sirve: el primero
que saque un evento se lo lleva y el otro no lo ve nunca.

El bus reparte una COPIA a cada suscriptor. La ventana tiene su cola y cada
cliente SSE la suya.

COMPATIBLE CON LO QUE YA HABIA
------------------------------
Expone ``put_nowait``, asi que se le puede pasar tal cual a ``WhatsAppClient``
donde antes iba una ``queue.Queue``. No hubo que tocar la capa de protocolo
para esto, que es justamente lo que no se quiere tocar.

NUNCA BLOQUEA AL PRODUCTOR
--------------------------
Las colas de los suscriptores estan acotadas. Si un consumidor se atasca (un
navegador que dejo de leer el SSE), se descarta su evento mas antiguo en vez
de frenar al receptor de WhatsApp. Perder una notificacion de refresco en una
pestana abandonada es aceptable; frenar la recepcion de mensajes no lo es.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator

from app.core.logging_setup import get_logger

log = get_logger("APP")

# Cuantos eventos aguanta cada suscriptor antes de empezar a descartar los
# suyos mas antiguos.
SUBSCRIBER_QUEUE_SIZE = 512

# Cuantos eventos recientes se recuerdan para quien llegue tarde.
REPLAY_SIZE = 50


@dataclass
class Event:
    """Un evento del sistema. Mismo contrato que ``ClientEvent``."""

    name: str
    payload: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


class Subscription:
    """Cola propia de un consumidor. Se cierra sola al salir del ``with``."""

    def __init__(self, bus: "EventBus", maxsize: int = SUBSCRIBER_QUEUE_SIZE) -> None:
        self._bus = bus
        self.queue: queue.Queue[Any] = queue.Queue(maxsize=maxsize)
        self.dropped = 0

    def get(self, timeout: float | None = None) -> Any | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def get_nowait(self) -> Any | None:
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            return None

    def close(self) -> None:
        self._bus.unsubscribe(self)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *_excinfo: Any) -> None:
        self.close()


class EventBus:
    """Reparte eventos a todos los suscriptores vivos."""

    def __init__(self, *, replay: int = REPLAY_SIZE) -> None:
        self._lock = threading.RLock()
        self._subscribers: list[Subscription] = []
        self._replay_size = replay
        self._recent: list[Any] = []
        # Ultimo evento de cada nombre. Sirve para que un cliente que se
        # conecta a mitad sepa el estado ACTUAL sin esperar al siguiente
        # cambio: sin esto, abrir el navegador entre dos transiciones dejaba
        # la pantalla en blanco hasta que pasara algo.
        self._latest: dict[str, Any] = {}

    # -- Produccion ----------------------------------------------------------

    def put_nowait(self, event: Any) -> None:
        """Firma compatible con ``queue.Queue``, para ``WhatsAppClient``."""
        self.publish_event(event)

    def publish(self, name: str, payload: Any = None, **extra: Any) -> None:
        self.publish_event(Event(name=name, payload=payload, extra=extra))

    def publish_event(self, event: Any) -> None:
        nombre = getattr(event, "name", None)
        with self._lock:
            if nombre:
                self._latest[nombre] = event
            self._recent.append(event)
            if len(self._recent) > self._replay_size:
                del self._recent[: len(self._recent) - self._replay_size]
            destinatarios = list(self._subscribers)

        for suscriptor in destinatarios:
            self._entregar(suscriptor, event)

    @staticmethod
    def _entregar(suscriptor: Subscription, event: Any) -> None:
        try:
            suscriptor.queue.put_nowait(event)
            return
        except queue.Full:
            pass
        # Cola llena: se tira lo mas viejo de ESE consumidor y se mete lo
        # nuevo. El productor no espera a nadie.
        try:
            suscriptor.queue.get_nowait()
            suscriptor.queue.put_nowait(event)
            suscriptor.dropped += 1
        except (queue.Empty, queue.Full):  # pragma: no cover - carrera benigna
            suscriptor.dropped += 1

    # -- Consumo -------------------------------------------------------------

    def subscribe(self, *, replay: bool = False) -> Subscription:
        """Nueva cola. Con ``replay`` se precargan los eventos recientes."""
        suscriptor = Subscription(self)
        with self._lock:
            self._subscribers.append(suscriptor)
            historial = list(self._recent) if replay else []
        for event in historial:
            self._entregar(suscriptor, event)
        return suscriptor

    def unsubscribe(self, suscriptor: Subscription) -> None:
        with self._lock:
            if suscriptor in self._subscribers:
                self._subscribers.remove(suscriptor)



    def stream(
        self, *, timeout: float = 1.0, replay: bool = False
    ) -> Iterator[Any | None]:
        """Iterador infinito de eventos. Emite ``None`` cuando no hay nada.

        Ese ``None`` no es relleno: es lo que permite al generador SSE mandar
        un latido y darse cuenta de que el cliente se ha ido. Sin el, el
        generador se quedaria bloqueado para siempre en una conexion muerta.
        """
        with self.subscribe(replay=replay) as suscriptor:
            while True:
                yield suscriptor.get(timeout=timeout)
