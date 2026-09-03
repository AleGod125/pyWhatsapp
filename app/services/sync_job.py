"""Sincronizacion manual: un ciclo completo, a peticion.

QUE ES Y QUE NO ES
------------------
Es el boton "sincronizar ahora". NO es lo que hace llegar los mensajes nuevos:
eso ocurre solo, siempre que la sesion este conectada, por el receptor en vivo.
Este ciclo sirve para lo que NO es automatico e inmediato:

* reconciliar lo derivado (contadores, cursores, previas, alias);
* pedirle al telefono el historial que falte (backfill ON_DEMAND);
* recoger la multimedia pendiente.

NO EJECUTA SCRIPTS
------------------
Ni ``subprocess``, ni ``os.system``, ni ``probe_chat.py``. Llama a los mismos
servicios que usa el arranque: ``MaintenanceService``, ``BackfillService`` y
``MediaService``. Lanzar un script seria arrancar un segundo proceso que
pelearia por el cerrojo de la sesion con el que ya la tiene.

NO BLOQUEA LA PETICION HTTP
---------------------------
El ciclo puede durar minutos. ``start()`` vuelve enseguida y el trabajo sigue
en el event loop del cliente de WhatsApp, que es donde tiene que estar: el
backfill es asincrono y necesita ESE loop, no uno nuevo.

UNA SOLA A LA VEZ
-----------------
Dos ciclos simultaneos se pisarian los cursores y duplicarian peticiones al
telefono. El estado lo impide y la API lo traduce a un 409.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from app.core.logging_setup import get_logger

log = get_logger("SYNC")

# Estados del ciclo.
IDLE = "idle"
RUNNING = "running"
COMPLETE = "complete"
ERROR = "error"

# Fases, en el orden en que ocurren.
PHASES = ("reconcile", "revalidate", "backfill", "media", "finalize")


@dataclass
class SyncState:
    """Lo que se puede contar de un ciclo. Todo son hechos, no estimaciones."""

    state: str = IDLE
    job_id: str | None = None
    phase: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    chats_total: int = 0
    chats_processed: int = 0
    messages_new: int = 0
    media_pending: int = 0
    last_error: str | None = None
    # Lo que reconcilio la pasada segura, para poder decir que cambio.
    reconciled: str | None = None
    # EL RESULTADO, contado por lo que de verdad paso con cada chat.
    #
    # "Sincronizacion terminada" a secas escondia treinta conversaciones que
    # siguen sin una linea de historial. Un chat que espera semilla NO esta
    # sincronizado, no esta agotado, y no puede desaparecer del resumen.
    synced: int = 0
    waiting_seed: int = 0
    timeouts: int = 0
    errors: int = 0
    pending: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "job_id": self.job_id,
            "phase": self.phase,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "chats_total": self.chats_total,
            "chats_processed": self.chats_processed,
            "messages_new": self.messages_new,
            "media_pending": self.media_pending,
            # El desglose va SIEMPRE, tambien cuando el ciclo termina bien.
            "result": {
                "synced": self.synced,
                "waiting_seed": self.waiting_seed,
                "timeouts": self.timeouts,
                "errors": self.errors,
                "pending": self.pending,
            },
            "last_error": self.last_error,
            "reconciled": self.reconciled,
        }


class SyncAlreadyRunningError(RuntimeError):
    """Ya hay un ciclo en marcha."""


class SyncUnavailableError(RuntimeError):
    """No se puede sincronizar: sin WhatsApp, o sin conexion."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class SyncJob:
    """Ejecuta el ciclo manual y lleva su estado.

    Vive en el runtime, no en la peticion: el estado tiene que sobrevivir a la
    respuesta HTTP para que ``GET /sync/status`` pueda contar como va.
    """

    def __init__(
        self,
        settings: Any,
        database: Any,
        *,
        publish: Callable[..., None] | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._publish = publish or (lambda *_a, **_k: None)
        self._lock = threading.Lock()
        self.state = SyncState()

    # -- Consulta ------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.state.state == RUNNING

    def snapshot(self) -> dict[str, Any]:
        return self.state.to_json()

    # -- Arranque ------------------------------------------------------------

    def start(self, runtime: Any) -> str:
        """Lanza el ciclo. Devuelve el ``job_id``.

        :raises SyncUnavailableError: en modo local, o sin conexion.
        :raises SyncAlreadyRunningError: si ya hay uno en marcha.
        """
        self._comprobar_disponible(runtime)

        # El backfill AUTOMATICO tambien cuenta. Se midio el fallo: mientras
        # el del arranque excavaba, un POST /sync/run lanzo un segundo
        # backfill sobre el mismo chat. El telefono atiende las peticiones
        # ON_DEMAND de una en una, y dos respuestas cruzadas ya no se pueden
        # atribuir a la peticion que las pidio.
        backfill = getattr(runtime, "backfill", None)
        if backfill is not None and getattr(backfill, "busy", False):
            raise SyncAlreadyRunningError(
                "Ya hay una excavacion de historial en marcha; espera a que "
                "termine antes de lanzar otra."
            )

        with self._lock:
            if self.running:
                raise SyncAlreadyRunningError(
                    "Ya hay una sincronizacion en curso."
                )
            job_id = uuid.uuid4().hex[:12]
            self.state = SyncState(
                state=RUNNING,
                job_id=job_id,
                phase=PHASES[0],
                started_at=_ahora(),
            )

        self._emitir()
        log.info("Sincronizacion manual iniciada (job=%s)", job_id)

        # El ciclo va al event loop del cliente: el backfill es asincrono y
        # necesita ESE loop, el mismo que mantiene viva la sesion.
        loop = getattr(runtime.client, "_loop", None)
        if loop is None or loop.is_closed():
            self._terminar_con_error("el cliente de WhatsApp no tiene event loop")
            raise SyncUnavailableError(
                "SESSION_NOT_CONNECTED",
                "La sesion de WhatsApp no esta lista para sincronizar.",
            )
        asyncio.run_coroutine_threadsafe(self._ciclo(runtime), loop)
        return job_id

    @staticmethod
    def _comprobar_disponible(runtime: Any) -> None:
        from app.core.session_state import AppState

        if not runtime.info().whatsapp_enabled:
            raise SyncUnavailableError(
                "WHATSAPP_DISABLED",
                "El backend esta en modo local y no puede sincronizar WhatsApp.",
            )
        if runtime.state.state is not AppState.CONNECTED:
            # Sin conexion NO se arranca el backfill: pedirle historial al
            # telefono sin sesion solo produce timeouts y ensucia el estado de
            # los chats.
            raise SyncUnavailableError(
                "SESSION_NOT_CONNECTED",
                "WhatsApp no esta conectado; no se puede sincronizar.",
            )

    # -- El ciclo ------------------------------------------------------------

    async def _ciclo(self, runtime: Any) -> None:
        try:
            await self._fase_reconciliar()
            await self._fase_revalidar(runtime)
            await self._fase_backfill(runtime)
            await self._fase_media(runtime)
            await self._fase_final()
        except asyncio.CancelledError:
            self._terminar_con_error("sincronizacion cancelada")
            raise
        except Exception as exc:  # noqa: BLE001 - el fallo se reporta, no se traga
            log.exception("La sincronizacion manual fallo")
            self._terminar_con_error(str(exc)[:300])

    async def _fase_reconciliar(self) -> None:
        """Reconciliacion SEGURA. Nunca borra nada."""
        self._fase("reconcile")
        from app.services.maintenance_service import MaintenanceService

        informe = await asyncio.to_thread(
            MaintenanceService(self._database, self._settings).run_all
        )
        self.state.reconciled = str(informe)
        self._emitir()

    async def _fase_revalidar(self, runtime: Any) -> None:
        """Vuelve a mirar que chats pueden pedir historial."""
        self._fase("revalidate")
        backfill = runtime.backfill
        if backfill is None:
            return
        await asyncio.to_thread(backfill.revalidate_for_new_session)
        self.state.chats_total = await asyncio.to_thread(self._contar_candidatos)
        self._emitir()

    def _contar_candidatos(self) -> int:
        """Chats que pueden pedir historial. Una consulta, no la tabla entera."""
        from sqlalchemy import func, select

        from app.models import ChatHistoryState

        try:
            with self._database.transaction() as session:
                return session.execute(
                    select(func.count())
                    .select_from(ChatHistoryState)
                    .where(
                        ChatHistoryState.history_status.in_(
                            ("pending", "server_limited", "timeout", "error")
                        )
                    )
                ).scalar_one()
        except Exception:  # noqa: BLE001 - un contador no puede parar el ciclo
            return 0

    async def _fase_backfill(self, runtime: Any) -> None:
        """Historial ON_DEMAND. NO se toca el wire protocol."""
        self._fase("backfill")
        backfill = runtime.backfill
        if backfill is None or runtime.client is None:
            return

        antes = int(getattr(backfill.stats, "messages_new", 0) or 0)
        cliente = getattr(runtime.client, "_client", None)
        if cliente is None:
            log.warning("Sin cliente de pywhats; se omite el backfill")
            return

        await backfill.run(cliente)

        despues = int(getattr(backfill.stats, "messages_new", 0) or 0)
        self.state.messages_new = max(0, despues - antes)
        self.state.chats_processed = int(
            getattr(backfill.stats, "chats_processed", 0) or 0
        )
        self._emitir()
        self._publish("backfill_progress", self.snapshot())

    async def _fase_media(self, runtime: Any) -> None:
        """Recoge lo que quede pendiente de descargar."""
        self._fase("media")
        media = runtime.orchestrator.media if runtime.orchestrator else None
        if media is None:
            return
        if media.pending_count():
            await media.run()
        self.state.media_pending = media.pending_count()
        self._emitir()

    async def _fase_final(self) -> None:
        """Segunda reconciliacion: lo recien traido cambia contadores."""
        self._fase("finalize")
        from app.services.maintenance_service import MaintenanceService

        await asyncio.to_thread(
            MaintenanceService(self._database, self._settings).run_all
        )

        # El recuento se hace AL FINAL y sobre la base, no sobre lo que el
        # backfill creyo hacer: si un chat sigue esperando semilla, tiene que
        # aparecer en el resumen, no desaparecer detras de un "terminado".
        desglose = await asyncio.to_thread(self._contar_resultado)
        with self._lock:
            self.state.synced = desglose.get("synced", 0)
            self.state.waiting_seed = desglose.get("waiting_seed", 0)
            self.state.timeouts = desglose.get("timeouts", 0)
            self.state.errors = desglose.get("errors", 0)
            self.state.pending = desglose.get("pending", 0)
            self.state.state = COMPLETE
            self.state.phase = None
            self.state.finished_at = _ahora()

        log.info(
            "Sincronizacion terminada (job=%s): sincronizados=%d "
            "pendientes_de_semilla=%d timeouts=%d errores=%d por_excavar=%d "
            "mensajes_nuevos=%d",
            self.state.job_id,
            self.state.synced,
            self.state.waiting_seed,
            self.state.timeouts,
            self.state.errors,
            self.state.pending,
            self.state.messages_new,
        )
        if self.state.waiting_seed:
            log.info(
                "%d conversacion(es) siguen SIN historial: WhatsApp aun no ha "
                "dado una referencia con la que pedirlo. NO estan vacias y NO "
                "estan sincronizadas.",
                self.state.waiting_seed,
            )
        self._emitir()

    def _contar_resultado(self) -> dict[str, int]:
        """Cuenta los chats por lo que REALMENTE les paso.

        Se lee de ``chat_history_state``, que es donde vive la verdad, en vez
        de sumar lo que el backfill fue apuntando: un chat puede haber
        cambiado de estado por otra via (una semilla en vivo, por ejemplo) y
        el resumen tiene que reflejar el final, no el recorrido.
        """
        from sqlalchemy import func, select

        from app.models import COMPLETE_STATUSES, SEEDLESS_STATUSES, ChatHistoryState

        conteo: dict[str, int] = {
            "synced": 0,
            "waiting_seed": 0,
            "timeouts": 0,
            "errors": 0,
            "pending": 0,
        }
        try:
            with self._database.transaction() as session:
                filas = session.execute(
                    select(ChatHistoryState.history_status, func.count())
                    .group_by(ChatHistoryState.history_status)
                ).all()
        except Exception:  # noqa: BLE001 - contar no puede tumbar el ciclo
            log.debug("No se pudo contar el resultado de la sincronizacion")
            return conteo

        for estado, cuantos in filas:
            if estado in COMPLETE_STATUSES:
                conteo["synced"] += cuantos
            elif estado in SEEDLESS_STATUSES:
                conteo["waiting_seed"] += cuantos
            elif estado == "timeout":
                conteo["timeouts"] += cuantos
            elif estado == "error":
                conteo["errors"] += cuantos
            else:
                conteo["pending"] += cuantos
        return conteo

    # -- Utilidades ----------------------------------------------------------

    def _fase(self, nombre: str) -> None:
        self.state.phase = nombre
        log.info("Sincronizacion: fase %s", nombre)
        self._emitir()

    def _terminar_con_error(self, mensaje: str) -> None:
        with self._lock:
            self.state.state = ERROR
            self.state.phase = None
            self.state.last_error = mensaje
            self.state.finished_at = _ahora()
        self._emitir()

    def _emitir(self) -> None:
        self._publish("sync_progress", self.snapshot())


def _ahora() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()
