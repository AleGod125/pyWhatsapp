"""Revalidar un chat que se quedo sin ancla, cuando el usuario lo pide.

QUE ES UN CHAT SIN ANCLA
------------------------
``HISTORY_SYNC_ON_DEMAND`` va anclado por definicion: necesita el ``id`` y la
marca de tiempo de un mensaje REAL para decir "dame lo anterior a esto". Un
chat que llego del bootstrap como pura metadata no tiene ese punto de partida,
y en ``PeerDataOperationRequestType`` no existe ninguna otra operacion de
historial::

    UPLOAD_STICKER=0  SEND_RECENT_STICKER_BOOTSTRAP=1  GENERATE_LINK_PREVIEW=2
    HISTORY_SYNC_ON_DEMAND=3  PLACEHOLDER_MESSAGE_RESEND=4

No hay "dame los ultimos N de este chat". Por eso ``waiting_seed`` no es un
error nuestro ni un estado provisional que se pueda forzar: describe la
situacion real de la conversacion.

QUE HACE ESTA REVISION
----------------------
Volver a mirar, con lo que sabemos HOY, si ha aparecido un ancla:

  1. resuelve los alias del chat (el mismo contacto puede estar por telefono y
     por LID, y el ancla puede haber entrado por el otro identificador);
  2. busca un mensaje con ID REAL de WhatsApp en cualquiera de ellos;
  3. si no lo hay, reinterpreta los blobs de History Sync ya guardados en
     disco: son datos que WhatsApp YA entrego, y el normalizador ha mejorado
     desde que se guardaron;
  4. recalcula el cursor y, si existe, deja el chat listo para excavar.

Si tras todo eso sigue sin ancla, el chat se queda en ``waiting_seed``. NO se
fabrica un cursor: se probo, y el servidor responde con un ACK y luego no
envia nada, que es el fallo que mas tiempo costo diagnosticar.

NO SE PIDE NADA AL SERVIDOR
---------------------------
Esta revision es puramente local. Quien pide es el backfill, y solo cuando ya
hay un ancla de verdad.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from app.core.logging_setup import get_logger
from app.models import Chat, ChatHistoryState
from app.history.cursor import persist_cursor
from app.services.seed_recovery import DESPIERTAN

log = get_logger("BACKFILL")


@dataclass
class RecheckResult:
    """Que se encontro al revisar, en terminos que no prometen de mas."""

    chat_jid: str
    estado_anterior: str | None
    estado: str
    aliases: list[str] = field(default_factory=list)
    blobs_revisados: int = 0
    mensajes_recuperados: int = 0
    cursor_encontrado: bool = False
    cursor_id: str | None = None
    cursor_timestamp: int | None = None

    @property
    def puede_excavar(self) -> bool:
        return self.cursor_encontrado

    def to_json(self) -> dict[str, Any]:
        return {
            "chat_jid": self.chat_jid,
            "previous_status": self.estado_anterior,
            "status": self.estado,
            "aliases": self.aliases,
            "blobs_reviewed": self.blobs_revisados,
            "messages_recovered": self.mensajes_recuperados,
            "seed_found": self.cursor_encontrado,
            "can_dig": self.puede_excavar,
            "oldest_message_timestamp": self.cursor_timestamp,
        }


class HistoryRecheck:
    """Revisa un chat concreto a peticion del usuario."""

    def __init__(
        self, database: Any, settings: Any, whatsapp_account_id: Any = None
    ) -> None:
        self._database = database
        self._settings = settings
        self._cuenta_id = whatsapp_account_id

    # -- 1. Alias ------------------------------------------------------------

    def aliases_de(self, session, chat_jid: str) -> list[str]:
        """Todos los identificadores conocidos del mismo contacto.

        Delega en :func:`app.history.cursor.aliases_de`: la resolucion de
        alias tiene que ser la misma aqui y en el motor, o el boton diria que
        hay ancla y la excavacion no la encontraria.
        """
        from app.history.cursor import aliases_de

        return aliases_de(session, chat_jid)

    # -- 3. Blobs ya guardados ----------------------------------------------

    def _reinterpretar_blobs(self, session, aliases: set[str]) -> tuple[int, int]:
        """Reprocesa los blobs de disco que mencionen alguno de los alias.

        Devuelve ``(blobs_revisados, mensajes_nuevos)``. No descarga nada: los
        ``.pb`` de ``data/history/`` son lo que WhatsApp ya entrego.
        """
        # Mismo sitio que usa ``scripts/ingest_blobs.py``: no hay un ajuste
        # aparte para esto, y tener dos verdades sobre donde viven los blobs
        # seria peor que derivarlo del mismo ``data_dir``.
        directorio: Path = self._settings.data_dir / "history"
        if not directorio.exists():
            return 0, 0

        from app.compat.history_compat import parse_full
        from app.services.history_service import ingest_history_sync

        revisados = recuperados = 0
        for archivo in sorted(directorio.glob("*.pb")):
            try:
                sync = parse_full(archivo.read_bytes())
            except Exception as exc:  # noqa: BLE001 - un blob ilegible no corta
                log.debug("Blob %s ilegible: %s", archivo.name, exc)
                continue

            interesa = any(
                getattr(c, "jid", None) in aliases for c in sync.conversations
            )
            if not interesa:
                continue

            revisados += 1
            try:
                resultado = ingest_history_sync(
                    session,
                    sync,
                    own_jid=self._own_jid(),
                    signal_db=getattr(self._settings, "signal_store_file", None),
                    # De quien son los chats que salgan de reinterpretar los
                    # blobs. Sin esto quedarian sin dueno y el filtro de
                    # propiedad los dejaria invisibles: existirian en la base
                    # y no los veria nadie.
                    whatsapp_account_id=self._cuenta_id,
                )
                recuperados += getattr(resultado, "messages_inserted", 0)
            except Exception:  # noqa: BLE001
                log.exception("No se pudo reinterpretar %s", archivo.name)

        return revisados, recuperados

    def _own_jid(self) -> str | None:
        from app.core.identity import own_jid

        return own_jid(self._settings)

    # -- Orquestacion --------------------------------------------------------

    def recheck(self, chat_id: int) -> RecheckResult | None:
        """Revisa un chat. ``None`` si el chat no existe.

        Todo ocurre en UNA transaccion: si la reinterpretacion de blobs falla
        a medias no queda un cursor apuntando a mensajes que no se guardaron.
        """
        with self._database.transaction() as session:
            fila = session.execute(
                select(Chat.jid).where(Chat.id == chat_id)
            ).scalar_one_or_none()
            if fila is None:
                return None
            chat_jid = fila

            estado_anterior = session.execute(
                select(ChatHistoryState.history_status).where(
                    ChatHistoryState.chat_jid == chat_jid
                )
            ).scalar_one_or_none()

            aliases = self.aliases_de(session, chat_jid)
            resultado = RecheckResult(
                chat_jid=chat_jid,
                estado_anterior=estado_anterior,
                estado=estado_anterior or "pending",
                aliases=aliases,
            )

            # 2. Ancla entre lo que ya tenemos.
            cursor = self._buscar_cursor(session, chat_jid)

            # 3. Solo si no hay ancla se vuelven a leer los blobs: reprocesar
            #    115 archivos no es gratis y no aporta nada si ya se puede
            #    excavar.
            if cursor is None:
                revisados, recuperados = self._reinterpretar_blobs(
                    session, set(aliases)
                )
                resultado.blobs_revisados = revisados
                resultado.mensajes_recuperados = recuperados
                if recuperados:
                    session.flush()
                    cursor = self._buscar_cursor(session, chat_jid)

            # 4. Con ancla, el chat vuelve a la cola de excavacion.
            if cursor is not None:
                resultado.cursor_encontrado = True
                resultado.cursor_id = cursor.message_id
                resultado.cursor_timestamp = cursor.timestamp
                if estado_anterior in DESPIERTAN or estado_anterior is None:
                    # El cursor primero, el estado despues: es el mismo
                    # orden que en el resto del sistema.
                    persist_cursor(session, chat_jid, cursor)
                    session.execute(
                        update(ChatHistoryState)
                        .where(ChatHistoryState.chat_jid == chat_jid)
                        .values(
                            history_status="pending",
                            consecutive_no_progress=0,
                            attempt_count=0,
                            next_retry_at=None,
                        )
                    )
                    resultado.estado = "pending"
                    log.info(
                        "%s: aparecio un ancla real; vuelve a 'pending'",
                        _corto(chat_jid),
                    )
                else:
                    resultado.estado = estado_anterior
            else:
                # Sigue sin ancla. Se dice tal cual: no hay forma protocolar
                # de pedir historial sin un mensaje real desde el que anclar.
                if estado_anterior in (None, "pending", "no_valid_cursor"):
                    session.execute(
                        update(ChatHistoryState)
                        .where(ChatHistoryState.chat_jid == chat_jid)
                        .values(history_status="waiting_seed")
                    )
                    resultado.estado = "waiting_seed"
                log.info(
                    "%s: sigue sin ancla tras revisar %d blob(s)",
                    _corto(chat_jid),
                    resultado.blobs_revisados,
                )

            return resultado

    def _buscar_cursor(self, session, chat_jid: str) -> Any:
        """El ancla mas antigua del contacto, con LA definicion de siempre.

        Los alias los resuelve la funcion central; aqui se le pasa el JID del
        chat, que es el que ademas permite mirar su catalogo de semillas.
        """
        from app.history.cursor import get_valid_history_cursor

        return get_valid_history_cursor(session, chat_jid=chat_jid)


def _corto(jid: str) -> str:
    user, _, server = jid.partition("@")
    return f"{user[:6]}***@{server}" if server else f"{user[:6]}***"
