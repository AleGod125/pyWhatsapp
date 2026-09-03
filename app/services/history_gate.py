"""Barrera que espera a que el History Sync inicial se asiente.

EL PROBLEMA MEDIDO
------------------
Tras un pairing nuevo la secuencia real fue::

    CONNECTED
    Canary omitido: capability ya confirmada
    BACKFILL Pasada 1 -> 2 candidatos (ambos self, sin cursor)
    Pasada 1: 0 mensajes nuevos -> "no queda mas que pedir"
    ... y DESPUES:
    INITIAL_BOOTSTRAP  39 conversaciones  112 mensajes

El backfill corrio antes de que existiera un solo cursor. ``connected`` no
significa "ya tenemos los cursores iniciales".

LA CONDICION
------------
No es un sleep ciego. Se exige evidencia:

1. haber recibido un INITIAL_BOOTSTRAP, y
2. que pasen ``settle_seconds`` sin ningun chunk nuevo.

Si llega otro chunk durante la ventana, el contador se reinicia. Y hay un
plazo maximo para no quedarse esperando indefinidamente si el servidor no
manda nada (cuenta ya sincronizada, por ejemplo).
"""

from __future__ import annotations

import asyncio
import time

from app.core.logging_setup import get_logger

log = get_logger("SYNC")


# Clave de ``app_state`` donde se anota que sesion ya recibio su bootstrap.
INITIAL_HISTORY_KEY = "initial_history_received"


def initial_history_confirmed(database: object, fingerprint: str | None) -> bool:
    """``True`` si ESTA sesion ya recibio su History Sync inicial.

    Se guarda por huella de sesion, no como un booleano global: un pairing
    nuevo cambia la huella y vuelve a haber que esperar el bootstrap, porque
    de verdad va a llegar otro. Reutilizar la marca de una sesion anterior
    seria saltarse una espera que si hace falta.
    """
    if not fingerprint or database is None:
        return False
    from app.services import repository as repo

    try:
        with database.transaction() as session:  # type: ignore[attr-defined]
            guardado = repo.get_app_state(session, INITIAL_HISTORY_KEY)
    except Exception:  # noqa: BLE001 - no poder leerlo solo significa esperar
        log.debug("No se pudo leer el estado del historial inicial")
        return False
    if not isinstance(guardado, dict):
        return False
    return guardado.get("fingerprint") == fingerprint


def confirm_initial_history(
    database: object, fingerprint: str | None, chunks: int = 0
) -> None:
    """Anota que esta sesion ya recibio el historial inicial."""
    if not fingerprint or database is None:
        return
    from datetime import datetime, timezone

    from app.services import repository as repo

    try:
        with database.transaction() as session:  # type: ignore[attr-defined]
            repo.set_app_state(
                session,
                INITIAL_HISTORY_KEY,
                {
                    "fingerprint": fingerprint,
                    "chunks": int(chunks),
                    "confirmed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
    except Exception:  # noqa: BLE001 - no anotarlo solo cuesta una espera mas
        log.debug("No se pudo anotar el historial inicial recibido")


class InitialHistoryGate:
    """Sabe cuando el historial inicial ha dejado de llegar.

    ``already_confirmed`` la cortocircuita. Es el arreglo de un problema de
    uso medido: en una sesion YA sincronizada el bootstrap no vuelve a
    llegar nunca, asi que la barrera agotaba sus 180 segundos en cada
    arranque y la aplicacion tardaba tres minutos en hacer nada util
   . Con la marca persistida esa espera desaparece; solo se
    espera cuando de verdad hay algo que esperar.
    """

    def __init__(
        self,
        *,
        settle_seconds: float = 8.0,
        max_wait: float = 180.0,
        already_confirmed: bool = False,
    ) -> None:
        self._settle = settle_seconds
        self._max_wait = max_wait
        self._bootstrap_seen = False
        self._last_chunk: float | None = None
        self._chunks = 0
        self._already_confirmed = already_confirmed

    @property
    def bootstrap_seen(self) -> bool:
        return self._bootstrap_seen

    @property
    def chunks(self) -> int:
        return self._chunks

    def note_history_sync(self, sync_type: str) -> None:
        """Registra un chunk. Lo llama la capa de ingesta."""
        self._chunks += 1
        self._last_chunk = time.monotonic()
        if sync_type == "INITIAL_BOOTSTRAP":
            self._bootstrap_seen = True
            log.info("INITIAL_BOOTSTRAP recibido; esperando a que se asiente")

    def settled(self) -> bool:
        if not self._bootstrap_seen or self._last_chunk is None:
            return False
        return (time.monotonic() - self._last_chunk) >= self._settle

    async def wait(self) -> bool:
        """Espera a que el historial inicial se asiente.

        Devuelve ``True`` si llego a asentarse, ``False`` si vencio el plazo
        maximo (por ejemplo, una sesion ya sincronizada que no recibe nada).
        """
        if self._already_confirmed and not self._bootstrap_seen:
            log.info(
                "Initial history ya confirmado para esta sesion; no se espera bootstrap"
            )
            return True

        deadline = time.monotonic() + self._max_wait
        log.info(
            "Esperando el History Sync inicial (settle=%.0fs, maximo=%.0fs)",
            self._settle,
            self._max_wait,
        )
        while time.monotonic() < deadline:
            if self.settled():
                log.info(
                    "Historial inicial asentado: %d chunks recibidos", self._chunks
                )
                return True
            await asyncio.sleep(1.0)

        if self._bootstrap_seen:
            log.warning(
                "El historial inicial sigue llegando tras %.0fs; se continua",
                self._max_wait,
            )
        else:
            log.info(
                "No llego INITIAL_BOOTSTRAP en %.0fs (sesion ya sincronizada); "
                "se continua con lo que hay en PostgreSQL",
                self._max_wait,
            )
        return False
