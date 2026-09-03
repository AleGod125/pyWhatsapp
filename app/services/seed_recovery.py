"""Chats sin ancla: por que lo estan, y como dejan de estarlo.

EL PROBLEMA, MEDIDO
-------------------
Tras un pairing nuevo, 32 de 40 chats quedaron sin poder excavarse. El caso de
control es "Isaac Virtual Tec": en el telefono tiene mensajes de domingo y
lunes, y aqui cero.

La auditoria del blob de ``INITIAL_BOOTSTRAP`` es concluyente::

    id                    = 64940106866902@lid
    conversationTimestamp = 1788199305        <- hubo actividad
    (campo 2 'messages')  = AUSENTE           <- cero mensajes

Y de los 40 chats del bootstrap, los que tienen cero mensajes persistidos
tienen tambien cero mensajes CRUDOS en el blob. **No se perdio nada al
parsear**: WhatsApp entrego esas conversaciones como metadata y punto.

POR QUE NO SE PUEDE PEDIR SIN MAS
---------------------------------
``HISTORY_SYNC_ON_DEMAND`` va anclado por definicion: necesita
``oldestMsgID`` + ``oldestMsgTimestampMS`` de un mensaje REAL. Y en
``PeerDataOperationRequestType`` no hay ninguna otra operacion de historial:

    UPLOAD_STICKER=0  SEND_RECENT_STICKER_BOOTSTRAP=1  GENERATE_LINK_PREVIEW=2
    HISTORY_SYNC_ON_DEMAND=3  PLACEHOLDER_MESSAGE_RESEND=4

No existe "dame los ultimos N de este chat". Fabricar un ancla (id vacio, el
JID, un timestamp inventado) no es una opcion: el servidor responde con un ACK
y luego no envia nada, que es justo el fallo que costo dias de diagnostico.

LO QUE SI FUNCIONA
------------------
Un chat sin ancla deja de estarlo en cuanto llega UN mensaje real, venga de
donde venga:

    mensaje en vivo  ->  ancla valida  ->  excavar hacia atras
    History Sync     ->  ancla valida  ->  excavar hacia atras

Eso es lo que hace este modulo: vigila la aparicion de la primera semilla y
pone el chat a excavar, sin esperar a que nadie reinicie nada.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import func, select, update

from app.core.logging_setup import get_logger
from app.models import ChatHistoryState, Message

log = get_logger("BACKFILL")

# Estados desde los que un chat puede "despertar" al aparecer una semilla.
DESPIERTAN = ("no_valid_cursor", "waiting_seed", "empty_confirmed")


@dataclass
class SeedReport:
    """Que cambio al buscar semillas."""

    revisados: int = 0
    sembrados: int = 0
    marcados_waiting: int = 0
    chats: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"revisados={self.revisados} sembrados={self.sembrados} "
            f"waiting_seed={self.marcados_waiting}"
        )


class SeedRecovery:
    """Detecta anclas nuevas y pone el chat a excavar.

    No pide NADA al servidor: solo mira lo que ya hay en PostgreSQL. Quien
    pide es el backfill, y solo cuando ya existe un ancla de verdad.
    """

    def __init__(self, database: Any) -> None:
        self._database = database

    # -- Clasificacion -------------------------------------------------------

    def classify(self) -> SeedReport:
        """Pone en ``waiting_seed`` los chats que NO se pueden excavar.

        El criterio es tener ANCLA, no tener mensajes. Son cosas distintas: un
        chat puede tener filas y aun asi no poder pedir historial, porque
        ninguna trae un ID real de WhatsApp y eso es lo unico que ``ON_DEMAND``
        acepta. Contar mensajes habria dado por excavable un chat que no lo es.

        ``waiting_seed`` describe la situacion del chat; ``no_valid_cursor``
        describia nuestro estado interno. Y ninguno de los dos es
        "sincronizado", que es lo que se estaba diciendo de 32 chats que en el
        telefono si tienen mensajes.
        """
        from app.services.maintenance_service import _real_wamid_filter

        informe = SeedReport()
        with self._database.transaction() as session:
            # Un solo LEFT JOIN contra los mensajes que SI servirian de ancla.
            anclas = (
                select(Message.chat_jid, func.count().label("total"))
                .where(_real_wamid_filter())
                .group_by(Message.chat_jid)
                .subquery()
            )
            filas = session.execute(
                select(ChatHistoryState.chat_jid, func.coalesce(anclas.c.total, 0))
                .outerjoin(anclas, anclas.c.chat_jid == ChatHistoryState.chat_jid)
                .where(
                    # 'pending' entra tambien: un chat recien creado que nunca
                    # llego a tener un ancla se quedaria ahi para siempre.
                    ChatHistoryState.history_status.in_(
                        ("no_valid_cursor", "pending")
                    )
                )
            ).all()

            informe.revisados = len(filas)
            esperando = [jid for jid, anclas_validas in filas if anclas_validas == 0]
            if esperando:
                session.execute(
                    update(ChatHistoryState)
                    .where(ChatHistoryState.chat_jid.in_(esperando))
                    .values(history_status="waiting_seed")
                )
                informe.marcados_waiting = len(esperando)
                log.info(
                    "%d chats sin ancla pasan a 'waiting_seed' (esperando una "
                    "semilla; NO estan sincronizados)",
                    len(esperando),
                )
        return informe

    # -- Siembra -------------------------------------------------------------

    def seed_from_messages(self, chat_jids: Iterable[str]) -> SeedReport:
        """Un chat sin ancla que YA tiene un mensaje real pasa a excavarse.

        Se llama cuando entra un mensaje en vivo o un History Sync. El ancla
        no se inventa: se comprueba que exista un mensaje con ID REAL de
        WhatsApp, que es lo unico que ``ON_DEMAND`` acepta.
        """
        deseados = [j for j in set(chat_jids) if j]
        informe = SeedReport()
        if not deseados:
            return informe

        from app.services import repository as repo

        with self._database.transaction() as session:
            dormidos = session.execute(
                select(ChatHistoryState.chat_jid).where(
                    ChatHistoryState.chat_jid.in_(deseados),
                    ChatHistoryState.history_status.in_(DESPIERTAN),
                )
            ).scalars().all()
            informe.revisados = len(dormidos)

            for chat_jid in dormidos:
                cursor = repo.get_oldest_valid_history_cursor(session, chat_jid)
                if cursor is None:
                    # Llego un mensaje, pero sin ID real de WhatsApp no sirve
                    # de ancla. Se queda esperando: es la verdad.
                    continue
                session.execute(
                    update(ChatHistoryState)
                    .where(ChatHistoryState.chat_jid == chat_jid)
                    .values(
                        history_status="pending",
                        oldest_message_id=cursor.message_id,
                        oldest_message_timestamp=cursor.timestamp,
                        consecutive_no_progress=0,
                    )
                )
                informe.sembrados += 1
                informe.chats.append(chat_jid)
                log.info(
                    "%s: aparecio una semilla real; pasa a 'pending' para excavar",
                    _corto(chat_jid),
                )
        return informe

    def pending_seedless(self) -> list[str]:
        """Chats que siguen sin ancla. Para las metricas, no para pedir nada."""
        with self._database.transaction() as session:
            return list(
                session.execute(
                    select(ChatHistoryState.chat_jid).where(
                        ChatHistoryState.history_status.in_(DESPIERTAN)
                    )
                ).scalars()
            )


def _corto(jid: str) -> str:
    """JID enmascarado: un identificador completo es un numero de telefono."""
    user, _, server = jid.partition("@")
    return f"{user[:6]}***@{server}" if server else f"{user[:6]}***"
