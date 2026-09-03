"""Ciclo de vida de la vinculacion: QR vigente, caducidad y renovacion.

QUE RESUELVE
------------
Antes, el primer QR solo existia si alguien pedia ``POST /session/pair``, y el
frontend no tenia forma de saber si el que estaba mostrando seguia sirviendo.
Aqui se lleva la cuenta: cuando se genero, hasta cuando vale, cuantos van, y
cuando hay que pedir otro.

QUE NO TOCA
-----------
NADA de criptografia. El QR lo produce pywhats con su ``Pairer``; este modulo
solo observa los que llegan y anota metadatos. El restart 515, el
``pair-device-sign`` y el flujo Signal siguen exactamente igual.

EL QR NO SE GUARDA EN DISCO
---------------------------
El payload es una credencial: quien lo tenga puede vincular un dispositivo a
la cuenta. Vive en memoria, no se persiste, no se registra en los logs y no
sale por la API como texto: solo como imagen.

SOBRE LA VENTANA DE 5 MINUTOS
-----------------------------
Es una ventana de EXPERIENCIA, no la vida real del ``ref``. pywhats rota el
codigo mucho antes (el primero a los 60 s, luego cada 20 s) y cada rotacion
publica un QR nuevo que sustituye al anterior e incrementa la generacion. La
ventana es el tope: pasado ese plazo sin ningun QR nuevo, el que hay se declara
caducado y se pide otro. Reportar 300 s y NO renovar seria mentir; lo que hace
que el numero sea cierto es la renovacion.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.core.logging_setup import get_logger

log = get_logger("PAIRING")

# Ventana de validez que se le promete al frontend, en segundos.
DEFAULT_QR_TTL = 300.0

# Cada cuanto revisa el vigilante si hay que renovar.
WATCHDOG_INTERVAL = 5.0


@dataclass(frozen=True)
class QRSnapshot:
    """Lo que se puede contar del QR vigente SIN revelar el payload."""

    available: bool
    generation: int
    generated_at: str | None
    expires_at: str | None
    expires_in_seconds: int


class PairingManager:
    """Estado del QR y renovacion automatica.

    Sin estado en disco y sin historial: solo el QR actual. Un QR anterior no
    sirve para nada y guardarlo solo seria material sensible de sobra.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_QR_TTL,
        on_renew: Callable[[], None] | None = None,
        publish: Callable[..., None] | None = None,
    ) -> None:
        self._ttl = max(30.0, float(ttl_seconds))
        self._on_renew = on_renew
        self._publish = publish or (lambda *_a, **_k: None)

        self._lock = threading.RLock()
        self._payload: str | None = None
        self._generation = 0
        self._generated_at: datetime | None = None
        self._expires_at: datetime | None = None
        self._linked = False

        self._watchdog: threading.Thread | None = None
        self._stop = threading.Event()
        # Impide que dos hilos arranquen dos vinculaciones a la vez.
        self._renewing = threading.Lock()
        # Generacion del intento de conexion. Sube con cada reinicio, y todo
        # callback recuerda la suya: uno tardio de una generacion vieja se
        # ignora en vez de tumbar el estado del intento que si prospero.
        self._connection_generation = 1
        # ``True`` desde el pair-success. A partir de ahi, la vinculacion esta
        # CERRADA: ningun callback viejo puede reabrirla.
        self._committed = False

    # -- Consulta ------------------------------------------------------------

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def connection_generation(self) -> int:
        """Generacion del intento de conexion en curso."""
        with self._lock:
            return self._connection_generation

    @property
    def committed(self) -> bool:
        """``True`` si ya hubo pair-success: la vinculacion esta cerrada."""
        with self._lock:
            return self._committed

    def is_current(self, generation: int) -> bool:
        """``False`` si ese callback pertenece a un intento ya superado."""
        with self._lock:
            return generation == self._connection_generation

    def next_generation(self) -> int:
        """Abre un intento nuevo. Invalida los callbacks de los anteriores."""
        with self._lock:
            self._connection_generation += 1
            return self._connection_generation

    def commit(self) -> None:
        """Llego el pair-success: se cierra la vinculacion.

        Se para el vigilante y se invalida el QR. Lo que venga despues es cosa
        de la CONEXION (el 515, el reconnect, el <success>), no del pairing:
        un reintento aqui abriria una segunda vinculacion y el usuario tendria
        que escanear otra vez. Paso de verdad.
        """
        with self._lock:
            if self._committed:
                return
            self._committed = True
            self._payload = None
            self._expires_at = None
        self.stop()
        log.info(
            "pair-success: vinculacion cerrada (generacion %d); "
            "no se generaran mas QR",
            self.connection_generation,
        )

    @property
    def available(self) -> bool:
        """Hay un QR vigente y NO caducado."""
        with self._lock:
            return self._payload is not None and self._seconds_left() > 0

    @property
    def expired(self) -> bool:
        """Hubo un QR pero ya no vale. Distinto de "nunca hubo"."""
        with self._lock:
            return self._payload is not None and self._seconds_left() <= 0

    def payload(self) -> str | None:
        """El payload, SOLO para renderizar la imagen. Nunca para el JSON."""
        with self._lock:
            if self._payload is None or self._seconds_left() <= 0:
                return None
            return self._payload

    def _seconds_left(self) -> float:
        if self._expires_at is None:
            return 0.0
        return (self._expires_at - datetime.now(timezone.utc)).total_seconds()

    def snapshot(self) -> QRSnapshot:
        with self._lock:
            restante = max(0, int(self._seconds_left()))
            vigente = self._payload is not None and restante > 0
            return QRSnapshot(
                available=vigente,
                generation=self._generation,
                generated_at=self._generated_at.astimezone().isoformat()
                if self._generated_at
                else None,
                expires_at=self._expires_at.astimezone().isoformat()
                if vigente and self._expires_at
                else None,
                expires_in_seconds=restante if vigente else 0,
            )

    # -- Eventos entrantes ---------------------------------------------------

    def note_qr(self, payload: str) -> int:
        """Llego un QR nuevo de pywhats. Devuelve la generacion.

        Cada payload es un codigo DISTINTO: el anterior deja de servir en
        cuanto el servidor rota el ``ref``. Por eso la generacion sube con
        cada uno, y por eso el frontend tiene que recargar la imagen: seguir
        mostrando el viejo deja al usuario escaneando algo que ya no funciona.
        """
        if not payload:
            return self.generation
        with self._lock:
            if self._linked or self._committed:
                # Ya escaneado o vinculado: un QR tardio se ignora en vez de
                # reabrir la pantalla de vinculacion.
                return self._generation
            ahora = datetime.now(timezone.utc)
            self._payload = payload
            self._generation += 1
            self._generated_at = ahora
            self._expires_at = ahora + timedelta(seconds=self._ttl)
            generacion = self._generation
            restante = int(self._ttl)

        log.info("QR generation=%d expires_in=%ds", generacion, restante)
        self._publish("pairing_qr_ready", {"generation": generacion})
        return generacion

    def note_linked(self) -> None:
        """La sesion quedo conectada: se invalida el QR y se para el vigilante.

        Un QR vivo despues de vincular no sirve para nada y seria material
        sensible retenido sin motivo.
        """
        with self._lock:
            ya_estaba = self._linked
            self._linked = True
            self._payload = None
            self._expires_at = None
        self.stop()
        if not ya_estaba:
            log.info("Sesion vinculada: QR invalidado y renovacion detenida")

    def note_unlinked(self) -> None:
        """La sesion dejo de valer: vuelve a hacer falta vincular.

        Tambien se abre la vinculacion otra vez: un 401 REAL de la conexion
        que si prospero significa que hay que volver a empezar.
        """
        with self._lock:
            self._linked = False
            self._committed = False

    def invalidate(self) -> None:
        with self._lock:
            self._payload = None
            self._expires_at = None

    # -- Renovacion ----------------------------------------------------------

    def start_watchdog(self) -> None:
        """Vigila la caducidad y pide un QR nuevo cuando hace falta.

        Un solo hilo, y ``_renewing`` garantiza que no se lancen dos
        vinculaciones a la vez aunque llegue una peticion manual a la vez que
        salta el vigilante.
        """
        if self._on_renew is None:
            return
        with self._lock:
            if self._watchdog is not None and self._watchdog.is_alive():
                return
            self._stop.clear()
            self._watchdog = threading.Thread(
                target=self._vigilar, name="pairing-watchdog", daemon=True
            )
            self._watchdog.start()
        log.info("Vigilante de vinculacion en marcha (TTL=%ds)", int(self._ttl))

    def _vigilar(self) -> None:
        while not self._stop.wait(WATCHDOG_INTERVAL):
            with self._lock:
                if self._linked or self._committed:
                    # Ya escaneado: lo que falta es la conexion, no otro QR.
                    return
                caducado = self._payload is not None and self._seconds_left() <= 0
                # Sin QR y sin vinculo tampoco se puede quedar: pasa cuando el
                # flujo murio y se invalido el codigo. Antes esta rama no
                # existia y, si la renovacion inmediata fallaba, no habia
                # segunda oportunidad hasta reiniciar el proceso.
                sin_ninguno = self._payload is None and self._generation > 0
            if caducado:
                log.info("El QR caduco sin escanearse; se pide uno nuevo")
                self.renew()
            elif sin_ninguno and not self.renewing:
                log.info("No hay QR vigente y sigue sin vincularse; se pide uno")
                self.renew()

    def renew(self) -> bool:
        """Pide una vinculacion nueva. ``False`` si ya habia una en marcha.

        El cerrojo no es decorativo: dos renovaciones simultaneas abririan dos
        conexiones al servidor y producirian dos QR distintos, de los cuales
        solo uno serviria.
        """
        if self._on_renew is None:
            return False
        if self.committed:
            log.debug("La vinculacion ya esta cerrada; no se genera otro QR")
            return False
        if not self._renewing.acquire(blocking=False):
            log.debug("Ya hay una vinculacion arrancando; no se lanza otra")
            return False
        try:
            self.invalidate()
            self._on_renew()
            return True
        except Exception:  # noqa: BLE001 - reintentara en la siguiente vuelta
            log.exception("No se pudo reiniciar la vinculacion")
            return False
        finally:
            self._renewing.release()

    @property
    def renewing(self) -> bool:
        """``True`` si hay una vinculacion arrancando ahora mismo."""
        bloqueado = self._renewing.acquire(blocking=False)
        if bloqueado:
            self._renewing.release()
        return not bloqueado

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._watchdog = None
