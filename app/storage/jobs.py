"""Cola de subidas, en PostgreSQL. El patron *outbox*.

POR QUE EN LA BASE Y NO EN MEMORIA
----------------------------------
Con una ``Queue()`` en memoria, cerrar el servicio pierde todo lo pendiente.
Los mensajes seguirian en PostgreSQL pero nadie volveria a intentar subirlos:
quedarian marcados como pendientes para siempre, sin que nada lo delatara.

Escribiendo el mensaje y su trabajo en la MISMA transaccion no puede existir
un mensaje sin trabajo. Y al arrancar, lo que quedo a medias se recoge solo.

POR QUE UN TRABAJO POR ENTIDAD
------------------------------
``UNIQUE(job_type, entity_id)``. Sin eso, cada reintento crearia un trabajo
nuevo y acabarian subiendose copias del mismo segmento a Drive. El trabajo
es el mismo; lo que cambia es cuantas veces se ha intentado.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, update

from app.core.logging_setup import get_logger
from app.models.storage import StorageJob

log = get_logger("STORAGE")

#: Esperas entre reintentos. Crecen para no machacar a Google cuando algo va
#: mal de verdad, y paran en 15 minutos: mas alla, reintentar mas despacio no
#: arregla nada y solo retrasa la recuperacion cuando el problema pase.
ESPERAS = (5, 15, 45, 120, 300, 900)

#: A partir de aqui el trabajo se marca ``failed`` y deja de reintentarse
#: solo. No se pierde nada: el contenido sigue en PostgreSQL y se puede
#: reencolar a mano.
MAX_INTENTOS = 12


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def espera_para(intento: int, *, retry_after: float | None = None) -> float:
    """Segundos hasta el proximo intento, con dispersion.

    Si Google manda ``Retry-After`` se respeta: es el propio servidor diciendo
    cuando volver, y adelantarse solo consigue otro rechazo.

    La dispersion evita que, tras un corte, todos los trabajos pendientes
    salgan a la vez en el mismo instante.
    """
    if retry_after and retry_after > 0:
        return float(retry_after)
    base = ESPERAS[min(intento, len(ESPERAS) - 1)]
    return base * (0.75 + random.random() * 0.5)


class StorageJobQueue:
    """Alta, reclamo y cierre de trabajos. No sube nada."""

    def __init__(self, database: Any) -> None:
        self._database = database

    # -- Alta ---------------------------------------------------------------

    def encolar(
        self,
        sesion: Any,
        *,
        user_id: uuid.UUID,
        account_id: uuid.UUID | None,
        job_type: str,
        entity_id: str,
        payload_bytes: int = 0,
        detail: dict | None = None,
    ) -> StorageJob:
        """Crea el trabajo DENTRO de la transaccion que le pasen.

        Recibe la sesion en vez de abrir una propia a proposito: es lo que
        permite que el mensaje y su trabajo entren o no entren juntos.
        """
        existente = sesion.execute(
            select(StorageJob).where(
                StorageJob.job_type == job_type, StorageJob.entity_id == entity_id
            )
        ).scalar_one_or_none()

        if existente is not None:
            # Ya se subio: no se vuelve a encolar. Es lo que hace que
            # reprocesar el mismo mensaje no duplique archivos en Drive.
            if existente.status == "complete":
                return existente
            existente.status = "pending"
            existente.next_retry_at = None
            existente.updated_at = _ahora()
            sesion.flush()
            return existente

        trabajo = StorageJob(
            user_id=user_id,
            whatsapp_account_id=account_id,
            job_type=job_type,
            entity_id=entity_id,
            status="pending",
            payload_bytes=payload_bytes,
            detail=detail,
        )
        sesion.add(trabajo)
        sesion.flush()
        return trabajo

    # -- Reclamo ------------------------------------------------------------

    def reclamar(self, *, limite: int = 5) -> list[StorageJob]:
        """Coge trabajos listos y los marca en proceso, de forma atomica.

        ``FOR UPDATE SKIP LOCKED`` deja que varios trabajadores compartan la
        cola sin cogerse el mismo trabajo: el segundo salta los que ya tiene
        otro en vez de esperar a que lo suelte.
        """
        ahora = _ahora()
        with self._database.transaction() as sesion:
            candidatos = (
                sesion.execute(
                    select(StorageJob)
                    .where(
                        StorageJob.status == "pending",
                        or_(
                            StorageJob.next_retry_at.is_(None),
                            StorageJob.next_retry_at <= ahora,
                        ),
                    )
                    .order_by(StorageJob.created_at)
                    .limit(limite)
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            for trabajo in candidatos:
                trabajo.status = "processing"
                trabajo.attempts += 1
                trabajo.updated_at = ahora
            sesion.flush()
            for trabajo in candidatos:
                sesion.expunge(trabajo)
            return list(candidatos)

    # -- Cierre -------------------------------------------------------------

    def completar(self, job_id: uuid.UUID) -> None:
        with self._database.transaction() as sesion:
            sesion.execute(
                update(StorageJob)
                .where(StorageJob.id == job_id)
                .values(status="complete", last_error=None, updated_at=_ahora())
            )

    def reintentar(
        self, job_id: uuid.UUID, motivo: str, *, retry_after: float | None = None
    ) -> None:
        """Devuelve el trabajo a la cola con espera creciente."""
        with self._database.transaction() as sesion:
            trabajo = sesion.get(StorageJob, job_id)
            if trabajo is None:
                return
            if trabajo.attempts >= MAX_INTENTOS:
                trabajo.status = "failed"
                trabajo.last_error = motivo[:500]
                trabajo.updated_at = _ahora()
                log.warning(
                    "Trabajo de subida agotado tras %d intentos (%s). El "
                    "contenido NO se ha perdido: sigue en PostgreSQL.",
                    trabajo.attempts,
                    trabajo.job_type,
                )
                sesion.flush()
                return
            espera = espera_para(trabajo.attempts, retry_after=retry_after)
            trabajo.status = "pending"
            trabajo.next_retry_at = _ahora() + timedelta(seconds=espera)
            trabajo.last_error = motivo[:500]
            trabajo.updated_at = _ahora()
            sesion.flush()

    def pausar_todos(self, user_id: uuid.UUID, motivo: str) -> int:
        """Deja los trabajos en espera SIN perderlos.

        Es lo que ocurre cuando Google revoca el acceso: seguir reintentando
        solo gasta cupo, y borrarlos perderia contenido que no esta subido.
        Al reconectar se reanudan.
        """
        with self._database.transaction() as sesion:
            filas = sesion.execute(
                update(StorageJob)
                .where(
                    StorageJob.user_id == user_id,
                    StorageJob.status.in_(("pending", "processing")),
                )
                .values(status="paused", last_error=motivo[:500], updated_at=_ahora())
            ).rowcount
        if filas:
            log.warning(
                "%d subida(s) en pausa: %s. No se ha perdido nada.", filas, motivo
            )
        return filas or 0

    def reanudar(self, user_id: uuid.UUID) -> int:
        """Tras reconectar Google."""
        with self._database.transaction() as sesion:
            return (
                sesion.execute(
                    update(StorageJob)
                    .where(
                        StorageJob.user_id == user_id,
                        StorageJob.status.in_(("paused", "failed")),
                    )
                    .values(
                        status="pending",
                        next_retry_at=None,
                        attempts=0,
                        updated_at=_ahora(),
                    )
                ).rowcount
                or 0
            )

    # -- Recuperacion tras un cierre brusco ---------------------------------

    def recuperar_huerfanos(self, *, antiguedad_segundos: int = 300) -> int:
        """Trabajos que quedaron ``processing`` cuando el proceso murio.

        Nadie los va a terminar: el trabajador que los tenia ya no existe. Se
        devuelven a la cola. El margen evita robarle un trabajo a un
        trabajador que sigue vivo y solo esta tardando.
        """
        limite = _ahora() - timedelta(seconds=antiguedad_segundos)
        with self._database.transaction() as sesion:
            filas = sesion.execute(
                update(StorageJob)
                .where(
                    StorageJob.status == "processing",
                    StorageJob.updated_at < limite,
                )
                .values(status="pending", next_retry_at=None, updated_at=_ahora())
            ).rowcount
        if filas:
            log.info("%d subida(s) a medias recuperadas tras el reinicio", filas)
        return filas or 0

    # -- Consulta -----------------------------------------------------------

    def resumen(self, user_id: uuid.UUID) -> dict[str, Any]:
        with self._database.transaction() as sesion:
            filas = sesion.execute(
                select(StorageJob.status, func.count(), func.coalesce(
                    func.sum(StorageJob.payload_bytes), 0
                ))
                .where(StorageJob.user_id == user_id)
                .group_by(StorageJob.status)
            ).all()
        por_estado = {estado: (cuenta, bytes_) for estado, cuenta, bytes_ in filas}
        pendientes = sum(
            cuenta for estado, (cuenta, _) in por_estado.items()
            if estado in ("pending", "processing", "paused")
        )
        bytes_pendientes = sum(
            bytes_ for estado, (_, bytes_) in por_estado.items()
            if estado in ("pending", "processing", "paused")
        )
        return {
            "pending_jobs": pendientes,
            "failed_jobs": por_estado.get("failed", (0, 0))[0],
            "paused_jobs": por_estado.get("paused", (0, 0))[0],
            "complete_jobs": por_estado.get("complete", (0, 0))[0],
            "pending_bytes": int(bytes_pendientes),
        }

    def bytes_pendientes(self, user_id: uuid.UUID) -> int:
        with self._database.transaction() as sesion:
            return int(
                sesion.execute(
                    select(func.coalesce(func.sum(StorageJob.payload_bytes), 0)).where(
                        StorageJob.user_id == user_id,
                        StorageJob.status.in_(("pending", "processing", "paused")),
                    )
                ).scalar()
                or 0
            )
