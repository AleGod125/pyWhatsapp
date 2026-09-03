"""Recuperar el historial de las conversaciones que esperan una referencia.

EL PROBLEMA
-----------
Hay chats que llegaron del pairing como pura metadata: nombre, LID, telefono,
contadores. Ni un identificador de mensaje. Y ``HISTORY_SYNC_ON_DEMAND`` va
anclado por definicion: necesita el id y la marca de tiempo de un mensaje
REAL para poder pedir "lo anterior a esto".

QUE HACE ESTE SERVICIO
----------------------
Intenta conseguir esa primera referencia con la sesion auxiliar y, si la
consigue, se la entrega al motor que YA funciona::

    chat sin referencia
      -> sesion auxiliar busca UNA clave de mensaje
      -> se valida (id real, chat correcto, marca de tiempo real)
      -> se guarda como cursor
      -> SeedQueue -> BackfillService -> ON_DEMAND -> PostgreSQL

No hay un segundo extractor. La sesion auxiliar solo aporta el punto de
partida; el historial lo trae pywhats, como siempre.

LO QUE SE ESPERA DE VERDAD
--------------------------
Poco, y conviene decirlo. Se han medido las fuentes disponibles:

===========================  ==========================================
INITIAL_BOOTSTRAP            sin identificador (auditado campo a campo)
blobs de History Sync        sin identificador
PostgreSQL                   sin identificador
alias PN/LID                 sin identificador
app-state incremental        0 claves en 61 mutaciones
app-state snapshot completo  0 claves en 93 mutaciones
bootstrap de la auxiliar     sin clave para el chat de control
===========================  ==========================================

Asi que "no se encontro" sera el resultado habitual, y NO es un error: es la
respuesta honesta cuando el servidor no ha entregado ninguna referencia. El
chat sigue pendiente y se puede reintentar mas tarde, o despertar solo en
cuanto reciba un mensaje real.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.logging_setup import get_logger
from app.services.web_seed_provider import ETIQUETA, WebSeed, WebSeedProvider

log = get_logger("BACKFILL")

# Estados de un chat durante la recuperacion. ``no_seed`` es reintentable a
# proposito: no significa que el chat este vacio, sino que hoy no hay
# referencia.
ESTADOS_CHAT = (
    "waiting_seed",
    "recovering_seed",
    "seed_found",
    "fetching_history",
    "complete",
    "no_seed",
    "timeout",
    "error",
)


@dataclass
class ChatProgress:
    """Como va un chat concreto dentro del trabajo."""

    chat_id: int
    name: str
    state: str = "waiting_seed"

    def to_json(self) -> dict[str, Any]:
        return {"id": self.chat_id, "name": self.name, "state": self.state}


@dataclass
class RecoveryJob:
    """Un intento de recuperacion, global o de un solo chat."""

    job_id: str
    total: int = 0
    state: str = "starting"
    processed: int = 0
    recovered: int = 0
    no_seed: int = 0
    errors: int = 0
    qr_required: bool = False
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
            "no_seed": self.no_seed,
            "errors": self.errors,
            "qr_required": self.qr_required,
            "current_chat": self.current.to_json() if self.current else None,
            "chats": [c.to_json() for c in self.chats],
            "error": self.error,
            "elapsed_seconds": int((self.finished_at or time.time()) - self.started_at),
        }


class HistoryRecoveryService:
    """Coordina los intentos de recuperacion. Uno cada vez."""

    def __init__(self, settings: Any, database: Any, publish: Any = None) -> None:
        self._settings = settings
        self._database = database
        self._publish = publish or (lambda *a, **k: None)
        self._provider = WebSeedProvider(settings)
        self._jobs: dict[str, RecoveryJob] = {}
        self._lock = threading.Lock()
        self._activo: str | None = None
        # Chats con una recuperacion en marcha: uno no puede intentarse dos
        # veces a la vez.
        self._en_curso: set[int] = set()

    @property
    def provider(self) -> WebSeedProvider:
        return self._provider

    @property
    def busy(self) -> bool:
        return self._activo is not None

    def get(self, job_id: str) -> RecoveryJob | None:
        return self._jobs.get(job_id)

    def active_job(self) -> RecoveryJob | None:
        return self._jobs.get(self._activo) if self._activo else None

    # -- Lanzamiento ---------------------------------------------------------

    def start(self, runtime: Any, chat_id: int | None = None) -> RecoveryJob:
        """Arranca la recuperacion en su hilo. No bloquea la peticion HTTP.

        Con ``chat_id`` intenta solo esa conversacion; sin el, todas las que
        esperan referencia.
        """
        with self._lock:
            if self._activo is not None:
                raise RuntimeError("ya hay una recuperacion en marcha")
            if chat_id is not None and chat_id in self._en_curso:
                raise RuntimeError("ese chat ya se esta recuperando")

            pendientes = self._pendientes(chat_id)
            trabajo = RecoveryJob(
                job_id=uuid.uuid4().hex[:12],
                total=len(pendientes),
                chats=[ChatProgress(c["chat_id"], c["name"]) for c in pendientes],
            )
            self._jobs[trabajo.job_id] = trabajo
            self._activo = trabajo.job_id
            self._en_curso.update(c["chat_id"] for c in pendientes)

        if not pendientes:
            trabajo.state = "completed"
            trabajo.finished_at = time.time()
            self._activo = None
            return trabajo

        hilo = threading.Thread(
            target=self._ejecutar,
            args=(trabajo, pendientes, runtime),
            name=f"history-recovery-{trabajo.job_id}",
            daemon=True,
        )
        hilo.start()
        return trabajo

    # -- Ejecucion -----------------------------------------------------------

    def _ejecutar(
        self, trabajo: RecoveryJob, pendientes: list[dict[str, Any]], runtime: Any
    ) -> None:
        try:
            self._recuperar(trabajo, pendientes, runtime)
        except Exception as exc:  # noqa: BLE001 - un intento no tumba el servicio
            log.exception("%s La recuperacion fallo", ETIQUETA)
            trabajo.state = "failed"
            trabajo.error = str(exc)[:300]
            self._emitir("history.recovery.completed", trabajo.to_json())
        finally:
            trabajo.finished_at = time.time()
            with self._lock:
                self._en_curso.difference_update(c.chat_id for c in trabajo.chats)
                self._activo = None

    def _recuperar(
        self, trabajo: RecoveryJob, pendientes: list[dict[str, Any]], runtime: Any
    ) -> None:
        trabajo.state = "running"
        self._emitir("history.recovery.started", trabajo.to_json())
        log.info(
            "%s Recuperacion iniciada para %d conversacion(es)",
            ETIQUETA,
            trabajo.total,
        )

        por_id = {c.chat_id: c for c in trabajo.chats}
        for progreso in trabajo.chats:
            progreso.state = "recovering_seed"

        def al_encontrar(chat_id: int, semilla: WebSeed) -> None:
            """Cada semilla se entrega EN CUANTO aparece, no al final."""
            progreso = por_id.get(chat_id)
            if progreso is None:
                return
            progreso.state = "seed_found"
            self._emitir(
                "history.seed.found",
                {"chat_id": chat_id, "job_id": trabajo.job_id},
            )
            if self._entregar(chat_id, semilla, runtime):
                progreso.state = "fetching_history"
                trabajo.recovered += 1
                self._emitir(
                    "history.backfill.started",
                    {"chat_id": chat_id, "job_id": trabajo.job_id},
                )
            else:
                progreso.state = "error"
                trabajo.errors += 1

        def al_evento(estado: str, datos: dict[str, Any]) -> None:
            if estado == "qr_required":
                trabajo.qr_required = True
                trabajo.state = "qr_required"
                self._emitir("history.recovery.progress", trabajo.to_json())
            elif estado == "failed":
                trabajo.error = datos.get("error")

        objetivos = [
            {"chat_id": c["chat_id"], "jids": sorted(c["aliases"])}
            for c in pendientes
        ]
        encontradas = self._provider.run(
            objetivos, on_seed=al_encontrar, on_event=al_evento
        )

        # Lo que no aparecio sigue esperando: no es un error.
        for chat in pendientes:
            chat_id = chat["chat_id"]
            progreso = por_id[chat_id]
            trabajo.processed += 1
            trabajo.current = progreso
            if chat_id not in encontradas and progreso.state == "recovering_seed":
                progreso.state = "no_seed"
                trabajo.no_seed += 1
                self._anotar_intento(chat["chat_jid"], "no_seed")
                self._emitir(
                    "history.seed.not_found",
                    {"chat_id": chat_id, "job_id": trabajo.job_id},
                )
            self._emitir("history.recovery.progress", trabajo.to_json())

        trabajo.current = None
        trabajo.state = "completed"
        log.info(
            "%s Recuperacion terminada: %d recuperada(s), %d sin referencia, "
            "%d error(es)",
            ETIQUETA,
            trabajo.recovered,
            trabajo.no_seed,
            trabajo.errors,
        )
        if trabajo.no_seed:
            log.info(
                "%s %d conversacion(es) siguen sin referencia. NO estan vacias: "
                "WhatsApp no ha entregado un punto desde el que pedir su "
                "historial. Se puede reintentar, y despiertan solas si reciben "
                "un mensaje real.",
                ETIQUETA,
                trabajo.no_seed,
            )
        self._emitir("history.recovery.completed", trabajo.to_json())

    # -- Piezas --------------------------------------------------------------

    def _pendientes(self, chat_id: int | None) -> list[dict[str, Any]]:
        """Chats que esperan referencia, con todos sus alias resueltos."""
        from sqlalchemy import select

        from app.models import SEEDLESS_STATUSES, Chat, ChatHistoryState, Contact
        from app.services.history_recheck import HistoryRecheck

        revision = HistoryRecheck(self._database, self._settings)
        salida: list[dict[str, Any]] = []
        with self._database.transaction() as sesion:
            consulta = (
                select(Chat.id, Chat.jid, Chat.name, Contact.display_name)
                .join(ChatHistoryState, ChatHistoryState.chat_jid == Chat.jid)
                .outerjoin(
                    Contact, (Contact.jid == Chat.jid) | (Contact.lid == Chat.jid)
                )
                .where(
                    ChatHistoryState.history_status.in_(tuple(SEEDLESS_STATUSES))
                )
            )
            if chat_id is not None:
                consulta = consulta.where(Chat.id == chat_id)

            for cid, jid, nombre, contacto in sesion.execute(consulta).all():
                salida.append(
                    {
                        "chat_id": cid,
                        "chat_jid": jid,
                        "name": nombre or contacto or jid.split("@")[0],
                        "aliases": set(revision.aliases_de(sesion, jid)),
                    }
                )
        return salida

    def _entregar(self, chat_id: int, semilla: WebSeed, runtime: Any) -> bool:
        """Guarda la referencia como cursor y encola el chat.

        Es el UNICO efecto de la sesion auxiliar sobre la base: una fila de
        ``chat_history_state``. Ni un mensaje ni un adjunto salen de ahi.
        """
        from sqlalchemy import select, update

        from app.models import Chat, ChatHistoryState

        try:
            with self._database.transaction() as sesion:
                chat_jid = sesion.execute(
                    select(Chat.jid).where(Chat.id == chat_id)
                ).scalar_one_or_none()
                if chat_jid is None:
                    return False
                actualizadas = sesion.execute(
                    update(ChatHistoryState)
                    .where(ChatHistoryState.chat_jid == chat_jid)
                    .values(
                        history_status="pending",
                        oldest_message_id=semilla.message_id,
                        oldest_message_timestamp=semilla.timestamp,
                        consecutive_no_progress=0,
                        last_error=None,
                        last_seed_attempt_at=_ahora(),
                        last_seed_attempt_result="seed_found",
                    )
                ).rowcount
        except Exception:  # noqa: BLE001
            log.exception("%s No se pudo registrar la referencia", ETIQUETA)
            return False

        if not actualizadas:
            return False

        cola = getattr(runtime, "seed_queue", None)
        if cola is not None:
            cola.enqueue([chat_jid])
        return True

    def _anotar_intento(self, chat_jid: str, resultado: str) -> None:
        """Deja constancia del intento SIN cambiar el estado del chat.

        Sigue esperando referencia: lo unico que se guarda es cuando se probo
        por ultima vez, para poder decirselo al usuario.
        """
        from sqlalchemy import update

        from app.models import ChatHistoryState

        try:
            with self._database.transaction() as sesion:
                sesion.execute(
                    update(ChatHistoryState)
                    .where(ChatHistoryState.chat_jid == chat_jid)
                    .values(
                        last_seed_attempt_at=_ahora(),
                        last_seed_attempt_result=resultado,
                    )
                )
        except Exception:  # noqa: BLE001
            log.debug("%s No se pudo anotar el intento", ETIQUETA)

    def _emitir(self, nombre: str, datos: dict[str, Any]) -> None:
        try:
            self._publish(nombre, datos)
        except Exception:  # noqa: BLE001 - avisar no puede cortar nada
            log.debug("%s No se pudo publicar %s", ETIQUETA, nombre)


def _ahora():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
