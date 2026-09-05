"""Que le paso a cada mensaje que no se pudo descifrar. Sin contenido.

POR QUE HACE FALTA
------------------
Antes solo se contaba: "N fallos de descifrado en esta sesion". Con eso no se
puede responder a la unica pregunta que importa —**¿ese mensaje acabo
llegando?**— ni distinguir dos situaciones muy distintas:

* el emisor reenvio y el mensaje entro autenticado  -> no se perdio nada
* se pidio el reenvio y nunca llego                 -> falta un mensaje

EL RECORRIDO
------------
::

    original_failed  ->  retry_sent  ->  retry_received  ->  retry_success
                                                         \\
                                                          ->  retry_failed

``retry_failed`` es un reenvio que TAMPOCO se pudo descifrar. Que no llegue
nada no es un estado: es la ausencia de uno, y se mide como
``sin_respuesta``.

LO QUE NO HACE
--------------
No descifra, no reenvia, no toca Signal y no guarda ni un byte de contenido:
solo el identificador del mensaje, el motivo tecnico y cuantas veces paso.
Que un mensaje llegue a ``retry_success`` NO lo persiste: lo persiste el
camino normal, el mismo de siempre, una sola vez.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("SIGNAL")

ORIGINAL_FAILED = "original_failed"
RETRY_SENT = "retry_sent"
RETRY_RECEIVED = "retry_received"
RETRY_SUCCESS = "retry_success"
RETRY_FAILED = "retry_failed"

ESTADOS = (
    ORIGINAL_FAILED,
    RETRY_SENT,
    RETRY_RECEIVED,
    RETRY_SUCCESS,
    RETRY_FAILED,
)


@dataclass
class Intento:
    """Un mensaje que no cuadro, y como acabo."""

    wamid: str
    estado: str = ORIGINAL_FAILED
    motivo: str = ""
    #: Cuantas veces ha fallado ESTE mensaje. Es el numero que va en el acuse
    #: de reintento: mandar siempre 1 le dice al emisor que es la primera vez.
    intentos: int = 1
    origen: str = "peer"
    enc_type: str = ""
    dispositivo: int | None = None
    primer_fallo: float = field(default_factory=time.monotonic)
    ultimo_cambio: float = field(default_factory=time.monotonic)

    @property
    def resuelto(self) -> bool:
        return self.estado == RETRY_SUCCESS

    def to_json(self) -> dict[str, Any]:
        return {
            "message_id": self.wamid,
            "state": self.estado,
            "attempts": self.intentos,
            "source_device": self.origen,
            "enc_type": self.enc_type,
            "device": self.dispositivo,
            "reason": self.motivo[:80],
            "age_seconds": int(time.monotonic() - self.primer_fallo),
        }


class RetryTracker:
    """Sigue el recorrido de los mensajes que hubo que pedir otra vez.

    Acotado a proposito: un fallo de hace horas no aporta nada y retenerlos
    todos seria una fuga de memoria en una sesion larga.
    """

    MAXIMO = 500

    def __init__(self, maximo: int | None = None) -> None:
        self._intentos: dict[str, Intento] = {}
        self._candado = threading.Lock()
        self._maximo = maximo or self.MAXIMO
        #: Recuento por estado final, acumulado. No se poda.
        self.totales: dict[str, int] = {}

    # -- Entradas ------------------------------------------------------------

    def fallo(
        self,
        wamid: Any,
        motivo: str,
        *,
        origen: str = "peer",
        enc_type: str = "",
        dispositivo: int | None = None,
    ) -> Intento | None:
        """Un mensaje no se pudo descifrar.

        Si es la primera vez, empieza su recorrido. Si ya habia fallado, es un
        reenvio que TAMPOCO cuadro: sube el contador y pasa a
        ``retry_failed``, que no es lo mismo que un mensaje nuevo roto.
        """
        if not wamid:
            return None
        clave = str(wamid)
        with self._candado:
            actual = self._intentos.get(clave)
            if actual is None:
                actual = Intento(
                    wamid=clave,
                    motivo=motivo,
                    origen=origen,
                    enc_type=enc_type,
                    dispositivo=dispositivo,
                )
                self._intentos[clave] = actual
                self._contar(ORIGINAL_FAILED)
                self._podar()
            else:
                actual.intentos += 1
                actual.motivo = motivo
                actual.enc_type = enc_type or actual.enc_type
                actual.estado = RETRY_FAILED
                actual.ultimo_cambio = time.monotonic()
                self._contar(RETRY_FAILED)
            return actual

    def acuse_enviado(self, wamid: Any) -> None:
        """Se pidio el reenvio."""
        self._avanzar(wamid, RETRY_SENT)

    def llegado(self, wamid: Any) -> None:
        """Llego algo con ese identificador. Todavia no se sabe si cuadra."""
        self._avanzar(wamid, RETRY_RECEIVED)

    def recuperado(self, wamid: Any) -> Intento | None:
        """El reenvio llego AUTENTICADO. Ese mensaje ya no falta.

        Se saca del seguimiento: su recorrido termino bien y retenerlo solo
        haria que un fallo posterior con el mismo identificador pareciera un
        reintento cuando no lo es.
        """
        if not wamid:
            return None
        clave = str(wamid)
        with self._candado:
            intento = self._intentos.pop(clave, None)
            if intento is None:
                return None
            intento.estado = RETRY_SUCCESS
            intento.ultimo_cambio = time.monotonic()
            self._contar(RETRY_SUCCESS)
        log.info(
            "mensaje recuperado tras %d reintento(s) (origen=%s)",
            intento.intentos,
            intento.origen,
        )
        return intento

    # -- Consulta ------------------------------------------------------------

    def intentos_de(self, wamid: Any) -> int:
        """Cuantas veces ha fallado ese mensaje. 0 si no ha fallado nunca.

        Es lo que tiene que ir en el acuse de reintento: un contador que
        siempre dice 1 le esta diciendo al emisor que es la primera vez.
        """
        if not wamid:
            return 0
        with self._candado:
            intento = self._intentos.get(str(wamid))
            return intento.intentos if intento else 0

    def estado_de(self, wamid: Any) -> str | None:
        with self._candado:
            intento = self._intentos.get(str(wamid))
            return intento.estado if intento else None

    @property
    def sin_resolver(self) -> int:
        with self._candado:
            return len(self._intentos)

    def pendientes(self) -> list[dict[str, Any]]:
        """Los que siguen sin llegar. Sin contenido, con el id truncado."""
        with self._candado:
            return [i.to_json() for i in self._intentos.values()]

    def resumen(self) -> dict[str, Any]:
        with self._candado:
            sin_respuesta = sum(
                1 for i in self._intentos.values() if i.estado == RETRY_SENT
            )
            return {
                **{estado: self.totales.get(estado, 0) for estado in ESTADOS},
                "sin_resolver": len(self._intentos),
                "sin_respuesta": sin_respuesta,
            }

    # -- Piezas --------------------------------------------------------------

    def _avanzar(self, wamid: Any, estado: str) -> None:
        if not wamid:
            return
        with self._candado:
            intento = self._intentos.get(str(wamid))
            if intento is None or intento.estado == estado:
                return
            intento.estado = estado
            intento.ultimo_cambio = time.monotonic()
            self._contar(estado)

    def _contar(self, estado: str) -> None:
        self.totales[estado] = self.totales.get(estado, 0) + 1

    def _podar(self) -> None:
        while len(self._intentos) > self._maximo:
            self._intentos.pop(next(iter(self._intentos)))
