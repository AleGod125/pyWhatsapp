"""Revisar de golpe todos los chats que esperan un ancla. Solo con lo local.

QUE HACE
--------
Recorre los chats en ``waiting_seed`` y le pasa a cada uno la MISMA revision
que el boton individual (``HistoryRecheck``): resolver alias, buscar un
mensaje con ID real de WhatsApp, y si no lo hay, reinterpretar los blobs de
History Sync que ya estan en disco. Si aparece un ancla, el chat vuelve a la
cola de excavacion y pywhats pide su historial.

QUE **NO** HACE, Y ES EL PUNTO
------------------------------
No habla con el servidor, no vincula ningun dispositivo, no lanza Node y no
pide un segundo codigo QR. Trabaja sobre datos que WhatsApp YA entrego.

Por eso este modulo no importa nada de ``web_seed_provider`` ni de
``history_recovery``: la ruta normal del producto tiene que funcionar entera
sin la sesion auxiliar, y una prueba lo comprueba leyendo este archivo.

POR QUE VUELVE A ENCONTRAR COSAS
--------------------------------
Dos razones reales, no optimismo:

* el normalizador ha mejorado desde que se guardaron los blobs, asi que
  reinterpretarlos puede sacar mensajes que la primera pasada no entendio;
* un alias puede haber aparecido despues (``lid_bridge`` va aprendiendo el
  ``lid_map``), y el ancla puede estar guardada bajo el OTRO identificador
  del mismo contacto.

Si aun asi no aparece nada, el chat se queda en ``waiting_seed``. Eso no es un
error: es la situacion real de esa conversacion, y es reintentable.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import select

from app.core.logging_setup import get_logger
from app.models import SEEDLESS_STATUSES, Chat, ChatHistoryState

log = get_logger("BACKFILL")

ETIQUETA = "[RECHECK]"

#: Estados por chat que puede devolver la revision.
ESTADOS_CHAT = (
    "waiting_seed",  # en cola, o sigue sin ancla
    "rechecking",  # revisandose ahora
    "seed_found",  # aparecio un ancla
    "fetching_history",  # ya esta en la cola de excavacion
    "error",
)


@dataclass
class ChatProgress:
    """Como va un chat concreto dentro de la revision."""

    chat_id: int
    name: str | None
    state: str

    def to_json(self) -> dict[str, Any]:
        return {"id": self.chat_id, "name": self.name, "state": self.state}


@dataclass
class RecheckJob:
    """Una revision, global o de un solo chat.

    Misma forma que devolvia la recuperacion auxiliar, a proposito: el
    frontend no tiene que aprender un segundo vocabulario para lo mismo.
    """

    job_id: str
    total: int = 0
    state: str = "starting"
    processed: int = 0
    recovered: int = 0
    still_waiting: int = 0
    errors: int = 0
    messages_recovered: int = 0
    #: La automatica se salto porque acababa de correr. NO es un fallo: es lo
    #: que evita que un F5 repetido reinterprete los blobs una y otra vez.
    skipped: bool = False
    skipped_reason: str | None = None
    current: ChatProgress | None = None
    chats: list[ChatProgress] = field(default_factory=list)
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "state": self.state,
            "total": self.total,
            "processed": self.processed,
            "recovered": self.recovered,
            "still_waiting": self.still_waiting,
            "errors": self.errors,
            "messages_recovered": self.messages_recovered,
            "skipped": self.skipped,
            "skipped_reason": self.skipped_reason,
            "current_chat": self.current.to_json() if self.current else None,
            "chats": [c.to_json() for c in self.chats],
            "error": self.error,
            "elapsed_seconds": int((self.finished_at or time.time()) - self.started_at),
        }


class PendingRecheckService:
    """Revisa en bloque los chats que esperan un ancla."""

    def __init__(
        self,
        settings: Any,
        database: Any,
        *,
        publish: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._publish = publish
        self._trabajos: dict[str, RecheckJob] = {}
        self._activo: str | None = None
        self._ultimo: RecheckJob | None = None
        self._candado = threading.Lock()

    # -- Consulta ------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._activo is not None

    def get(self, job_id: str) -> RecheckJob | None:
        return self._trabajos.get(job_id)

    def active_job(self) -> RecheckJob | None:
        return self._trabajos.get(self._activo) if self._activo else None

    # -- Arranque ------------------------------------------------------------

    def start(self, runtime: Any, *, auto: bool = False) -> RecheckJob:
        """Arranca la revision. Devuelve el trabajo enseguida.

        Reinterpretar los blobs de decenas de chats tarda, asi que no se hace
        dentro de la peticion: el navegador se quedaria colgado sin poder leer
        el progreso.

        ``auto=True`` es la que dispara el panel al abrirse. Respeta una espera
        entre ejecuciones, porque un refresco repetido reinterpretaria los
        mismos blobs una y otra vez sin que hubiera cambiado nada. El boton
        manual no la respeta: si el usuario lo pulsa, es que quiere mirar YA.
        """
        with self._candado:
            if self._activo is not None:
                if auto:
                    # Ya hay una: engancharse a esa es justo lo que se quiere.
                    return self._trabajos[self._activo]
                raise RuntimeError("ya hay una revision en marcha")

            if auto:
                ocupado = self._otro_trabajo_activo(runtime)
                if ocupado:
                    # Reinterpretar blobs mientras el backfill excava es leer
                    # los mismos archivos y competir por la base sin ganar
                    # nada: lo que despierte, despertara igual despues.
                    aplazada = RecheckJob(
                        job_id=uuid.uuid4().hex[:8],
                        state="completed",
                        skipped=True,
                        skipped_reason=f"hay {ocupado} en marcha",
                        finished_at=time.time(),
                    )
                    self._trabajos[aplazada.job_id] = aplazada
                    log.debug(
                        "%s Revision automatica aplazada: %s en marcha",
                        ETIQUETA,
                        ocupado,
                    )
                    return aplazada

                espera = self._espera_restante()
                if espera > 0:
                    reciente = RecheckJob(
                        job_id=uuid.uuid4().hex[:8],
                        state="completed",
                        skipped=True,
                        skipped_reason=f"se reviso hace menos de {int(espera)}s",
                        finished_at=time.time(),
                    )
                    self._trabajos[reciente.job_id] = reciente
                    log.debug(
                        "%s Revision automatica omitida: quedan %ds de espera",
                        ETIQUETA,
                        int(espera),
                    )
                    return reciente

            pendientes = self._pendientes()
            trabajo = RecheckJob(
                job_id=uuid.uuid4().hex[:8],
                total=len(pendientes),
                chats=[
                    ChatProgress(c["chat_id"], c["name"], "waiting_seed")
                    for c in pendientes
                ],
            )
            self._trabajos[trabajo.job_id] = trabajo

            if not pendientes:
                trabajo.state = "completed"
                trabajo.finished_at = time.time()
                self._ultimo = trabajo
                self._emitir("history.recheck.completed", trabajo.to_json())
                return trabajo

            self._activo = trabajo.job_id

        hilo = threading.Thread(
            target=self._envolver,
            args=(trabajo, pendientes, runtime),
            name="pending-recheck",
            daemon=True,
        )
        hilo.start()
        return trabajo

    def _envolver(
        self, trabajo: RecheckJob, pendientes: list[dict[str, Any]], runtime: Any
    ) -> None:
        try:
            self._revisar(trabajo, pendientes, runtime)
        except Exception as exc:  # noqa: BLE001 - un fallo aqui no puede tumbar el servicio
            log.exception("%s La revision fallo", ETIQUETA)
            trabajo.state = "failed"
            trabajo.error = str(exc)
            self._emitir("history.recheck.completed", trabajo.to_json())
        finally:
            trabajo.finished_at = time.time()
            with self._candado:
                self._activo = None
                self._ultimo = trabajo

    # -- El trabajo ----------------------------------------------------------

    def _revisar(
        self, trabajo: RecheckJob, pendientes: list[dict[str, Any]], runtime: Any
    ) -> None:
        from app.services.history_recheck import HistoryRecheck

        revisor = HistoryRecheck(self._database, self._settings)
        trabajo.state = "running"
        self._emitir("history.recheck.started", trabajo.to_json())
        log.info(
            "%s Revisando %d conversacion(es) que esperan un ancla. Todo local: "
            "no se pide nada al servidor.",
            ETIQUETA,
            trabajo.total,
        )

        despertados: list[str] = []
        for chat in pendientes:
            progreso = next(
                c for c in trabajo.chats if c.chat_id == chat["chat_id"]
            )
            progreso.state = "rechecking"
            trabajo.current = progreso
            self._emitir("history.recheck.progress", trabajo.to_json())

            try:
                resultado = revisor.recheck(chat["chat_id"])
            except Exception:  # noqa: BLE001 - un chat roto no corta la revision
                log.exception("%s No se pudo revisar el chat %s", ETIQUETA, chat["chat_id"])
                progreso.state = "error"
                trabajo.errors += 1
                resultado = None

            if resultado is not None:
                trabajo.messages_recovered += resultado.mensajes_recuperados
                if resultado.puede_excavar:
                    progreso.state = "fetching_history"
                    trabajo.recovered += 1
                    despertados.append(resultado.chat_jid)
                else:
                    progreso.state = "waiting_seed"
                    trabajo.still_waiting += 1

            trabajo.processed += 1
            self._emitir("history.recheck.progress", trabajo.to_json())

        trabajo.current = None
        trabajo.state = "completed"

        # Lo que despierta se excava con el motor de siempre: ON_DEMAND
        # secuencial, cursor recalculado tras cada respuesta.
        if despertados:
            self._encolar(runtime, despertados)
            self._emitir(
                "history.backfill.started",
                {"job_id": trabajo.job_id, "chats": len(despertados)},
            )

        log.info(
            "%s Revision terminada: %d con ancla nueva, %d siguen esperando, "
            "%d error(es), %d mensaje(s) reinterpretados",
            ETIQUETA,
            trabajo.recovered,
            trabajo.still_waiting,
            trabajo.errors,
            trabajo.messages_recovered,
        )
        if trabajo.still_waiting:
            log.info(
                "%s %d conversacion(es) siguen sin ancla. NO estan vacias: "
                "WhatsApp todavia no ha entregado un mensaje desde el que pedir "
                "lo anterior. Despiertan solas en cuanto llegue uno real.",
                ETIQUETA,
                trabajo.still_waiting,
            )
        self._emitir("history.recheck.completed", trabajo.to_json())

    # -- Piezas --------------------------------------------------------------

    def _otro_trabajo_activo(self, runtime: Any) -> str | None:
        """Que otro trabajo pesado esta corriendo, si es que hay alguno.

        Solo mira; no espera ni cancela nada. El boton manual no consulta esto
        a proposito: si el usuario lo pulsa, quiere mirar aunque haya ruido.
        """
        trabajo = getattr(runtime, "sync_job", None)
        if trabajo is not None and getattr(trabajo, "running", False):
            return "una sincronizacion"

        # ``busy``, no ``running``: es como se llama en BackfillService, y
        # ademas cubre las peticiones ON_DEMAND vivas, no solo el bucle.
        backfill = getattr(runtime, "backfill", None)
        if backfill is not None and getattr(backfill, "busy", False):
            return "una excavacion"
        return None

    def _espera_restante(self) -> float:
        """Segundos que faltan para poder volver a revisar automaticamente."""
        margen = float(
            getattr(self._settings, "auto_recheck_cooldown_seconds", 0.0) or 0.0
        )
        if margen <= 0 or self._ultimo is None or self._ultimo.finished_at is None:
            return 0.0
        return max(0.0, margen - (time.time() - self._ultimo.finished_at))

    def _pendientes(self) -> list[dict[str, Any]]:
        """Los chats que esperan un ancla, con su nombre para poder mostrarlos."""
        with self._database.transaction() as session:
            filas = session.execute(
                select(Chat.id, Chat.name, Chat.jid)
                .join(ChatHistoryState, ChatHistoryState.chat_jid == Chat.jid)
                .where(ChatHistoryState.history_status.in_(SEEDLESS_STATUSES))
                .order_by(Chat.id)
            ).all()
            return [
                {"chat_id": f[0], "name": f[1], "chat_jid": f[2]} for f in filas
            ]

    def _encolar(self, runtime: Any, jids: list[str]) -> None:
        """Manda los despertados a la cola de excavacion, si la hay."""
        cola = getattr(runtime, "seed_queue", None)
        if cola is None:
            log.warning(
                "%s Aparecieron %d ancla(s) pero no hay cola de excavacion; "
                "se excavaran en el proximo ciclo automatico",
                ETIQUETA,
                len(jids),
            )
            return
        cola.enqueue(jids)

    def _emitir(self, nombre: str, datos: dict[str, Any]) -> None:
        if self._publish is None:
            return
        try:
            self._publish(nombre, datos)
        except Exception:  # noqa: BLE001 - avisar no puede romper el trabajo
            log.debug("No se pudo publicar %s", nombre, exc_info=True)
