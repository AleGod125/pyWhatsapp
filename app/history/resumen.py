"""Cuantas conversaciones hay en cada situacion. UNA sola definicion.

EL BUG QUE ORIGINA ESTE MODULO
------------------------------
En la misma ejecucion salian estas tres lineas::

    [PLAN_E] waiting=26 ...
    [SYNC] Ninguna conversacion tiene ancla: 0 espera(n) una referencia.
    [SYNC] complete chats=39 con_ancla=0 esperando=26 ...

El "0 espera(n)" era mentira, y no por un error de cuenta: la fase de
excavacion leia ``state.waiting_seed``, que todavia no se habia calculado —lo
rellena la fase FINAL, que corre despues—. Asi que leia el valor por omision.

Dos conceptos distintos que ademas se estaban mezclando:

``candidatos``  a cuantas conversaciones se les puede PEDIR historial ahora
``waiting``     cuantas estan esperando una referencia de WhatsApp

Que ``candidatos`` sea 0 no dice absolutamente nada sobre ``waiting``.

LA REGLA
--------
Cualquier fase que necesite contar algo llama aqui. Se lee de la base, que es
donde vive la verdad, y no de lo que una fase anterior creyera.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

from app.models import Chat, ChatHistoryState


@dataclass
class ResumenDeEstado:
    """La foto completa. Todos los numeros salen de la misma consulta."""

    chats_total: int = 0
    with_cursor: int = 0
    waiting_seed: int = 0
    pending: int = 0
    fetching: int = 0
    timeout: int = 0
    exhausted: int = 0
    errors: int = 0
    #: Tienen ancla pero su espera de reintento no ha vencido todavia.
    retry_pending: int = 0
    por_estado: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "chats_total": self.chats_total,
            "with_cursor": self.with_cursor,
            "waiting_seed": self.waiting_seed,
            "pending": self.pending,
            "fetching": self.fetching,
            "timeout": self.timeout,
            "exhausted": self.exhausted,
            "errors": self.errors,
            "retry_pending": self.retry_pending,
        }


def resumen_de_estado(
    database: Any, *, account_id: Any = None, backfill: Any = None
) -> ResumenDeEstado:
    """Cuantas conversaciones hay en cada situacion, ahora mismo.

    ``with_cursor`` sale del motor de excavacion cuando se le pasa, porque es
    EL que decide a quien se le puede pedir: calcularlo aparte volveria a
    crear dos definiciones de lo mismo, que es el bug que se esta arreglando.
    Sin motor se deja en 0 y se dice que no se pudo saber.
    """
    from app.models import COMPLETE_STATUSES, SEEDLESS_STATUSES

    resumen = ResumenDeEstado()
    with database.transaction() as sesion:
        consulta = select(func.count()).select_from(Chat)
        if account_id is not None:
            consulta = consulta.where(Chat.whatsapp_account_id == account_id)
        resumen.chats_total = int(sesion.execute(consulta).scalar() or 0)

        por_estado = select(ChatHistoryState.history_status, func.count())
        if account_id is not None:
            por_estado = por_estado.join(
                Chat, Chat.jid == ChatHistoryState.chat_jid
            ).where(Chat.whatsapp_account_id == account_id)
        resumen.por_estado = dict(
            sesion.execute(por_estado.group_by(ChatHistoryState.history_status)).all()
        )

        # Las esperas de reintento, en la misma pasada.
        from app.history.cursor import espera_cumplida

        esperas = select(ChatHistoryState.next_retry_at).where(
            ChatHistoryState.history_status.in_(("timeout", "pending"))
        )
        if account_id is not None:
            esperas = esperas.join(Chat, Chat.jid == ChatHistoryState.chat_jid).where(
                Chat.whatsapp_account_id == account_id
            )
        resumen.retry_pending = sum(
            0 if espera_cumplida(p) else 1 for p in sesion.execute(esperas).scalars()
        )

    crudos = resumen.por_estado
    resumen.waiting_seed = sum(crudos.get(e, 0) for e in SEEDLESS_STATUSES)
    resumen.pending = crudos.get("pending", 0)
    resumen.fetching = crudos.get("fetching", 0)
    resumen.timeout = crudos.get("timeout", 0)
    resumen.exhausted = sum(crudos.get(e, 0) for e in COMPLETE_STATUSES)
    resumen.errors = crudos.get("error", 0)

    if backfill is not None:
        try:
            resumen.with_cursor = len(backfill.chats_with_cursor())
        except Exception:  # noqa: BLE001 - contar no puede tumbar un ciclo
            resumen.with_cursor = 0

    return resumen
