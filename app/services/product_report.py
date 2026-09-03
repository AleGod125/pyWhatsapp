"""Resumen medido de lo que quedo en la copia local, al terminar el backfill.

PARA QUE
--------
Despues de una prueba desde cero hace falta poder decir, con numeros y sin
adornos, que se recupero y que no. Este resumen se lee de PostgreSQL —no de
contadores en memoria, que se pierden al reiniciar— y se registra de una sola
vez, con la etiqueta ``[PRODUCT_TEST]``, para poder buscarlo en el log.

SOBRE EL VOCABULARIO
--------------------
No se dice "backup completo" ni "100%". Lo que hay es una copia local de todo
el historial y contenido recuperable que WhatsApp ha proporcionado al
companion device. ``waiting_seed`` NO es "vacio": es una conversacion para la
que el servidor todavia no ha entregado un mensaje desde el que pedir lo
anterior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

from app.core.logging_setup import get_logger
from app.models import Chat, ChatHistoryState, MediaFile, Message

log = get_logger("BACKFILL")

ETIQUETA = "[PRODUCT_TEST]"


@dataclass
class ProductReport:
    """Lo que hay en la base, contado."""

    chats_total: int = 0
    messages_total: int = 0
    media_total: int = 0
    media_downloaded: int = 0
    por_estado: dict[str, int] = field(default_factory=dict)

    @property
    def exhausted(self) -> int:
        return self.por_estado.get("exhausted", 0)

    @property
    def waiting_seed(self) -> int:
        return self.por_estado.get("waiting_seed", 0)

    @property
    def timeout(self) -> int:
        return self.por_estado.get("timeout", 0)

    @property
    def errors(self) -> int:
        return self.por_estado.get("error", 0)

    def to_json(self) -> dict[str, Any]:
        return {
            "chats_total": self.chats_total,
            "messages_total": self.messages_total,
            "media_total": self.media_total,
            "media_downloaded": self.media_downloaded,
            "exhausted": self.exhausted,
            "waiting_seed": self.waiting_seed,
            "timeout": self.timeout,
            "errors": self.errors,
            "by_status": dict(self.por_estado),
        }


def collect(session: Any) -> ProductReport:
    """Cuenta lo que hay. Solo lecturas."""
    informe = ProductReport(
        chats_total=session.execute(select(func.count(Chat.id))).scalar() or 0,
        messages_total=session.execute(select(func.count(Message.id))).scalar() or 0,
        media_total=session.execute(select(func.count(MediaFile.id))).scalar() or 0,
    )
    informe.media_downloaded = (
        session.execute(
            select(func.count(MediaFile.id)).where(MediaFile.download_status == "downloaded")
        ).scalar()
        or 0
    )
    informe.por_estado = {
        estado: total
        for estado, total in session.execute(
            select(ChatHistoryState.history_status, func.count())
            .group_by(ChatHistoryState.history_status)
        ).all()
    }
    return informe


def log_summary(database: Any) -> ProductReport | None:
    """Registra el resumen. Nunca lanza: es observacion, no parte del flujo."""
    try:
        with database.transaction() as session:
            informe = collect(session)
    except Exception:  # noqa: BLE001 - un resumen no puede tumbar el backfill
        log.debug("No se pudo reunir el resumen de producto", exc_info=True)
        return None

    log.info("%s RESULTADO FINAL", ETIQUETA)
    log.info("%s   chats_total=%d", ETIQUETA, informe.chats_total)
    log.info("%s   messages_total=%d", ETIQUETA, informe.messages_total)
    log.info(
        "%s   media_total=%d (descargados=%d)",
        ETIQUETA,
        informe.media_total,
        informe.media_downloaded,
    )
    log.info(
        "%s   exhausted=%d waiting_seed=%d timeout=%d errors=%d",
        ETIQUETA,
        informe.exhausted,
        informe.waiting_seed,
        informe.timeout,
        informe.errors,
    )
    otros = {
        k: v
        for k, v in informe.por_estado.items()
        if k not in {"exhausted", "waiting_seed", "timeout", "error"}
    }
    if otros:
        log.info("%s   otros estados: %s", ETIQUETA, otros)
    if informe.waiting_seed:
        log.info(
            "%s   %d conversacion(es) siguen esperando una referencia de "
            "WhatsApp. No estan vacias, y despiertan solas si llega un mensaje "
            "real.",
            ETIQUETA,
            informe.waiting_seed,
        )
    log.info(
        "%s Esto es la copia local de todo el historial y contenido "
        "recuperable que WhatsApp ha proporcionado al companion device.",
        ETIQUETA,
    )
    return informe
