"""Exponer los mensajes individuales del blob de History Sync.

CARENCIA VERIFICADA (pywhats 0.2.0, ``pywhats/history.py``). El propio
docstring del modulo lo admite::

    The full blob also carries per-message ``WebMessageInfo`` records; those
    are kept opaque for now and only counted.

``parse_history_sync`` descomprime y parsea el blob, pero el evento
``history_sync`` que emite solo lleva un resumen::

    HistorySync(sync_type, progress, chunk_order, conversation_count,
                message_count, conversation_ids, pushnames)

El blob YA viene descargado y descifrado por pywhats con sus propias
primitivas de media. Lo unico que falta es leer el protobuf completo, que es
exactamente lo que hace este parche. NO se interceptan claves, NO se
reimplementa History Sync y NO se toca criptografia.

Estructura real del protobuf instalado (verificada, no supuesta):

    HistorySync      1 sync_type   2 conversations   5 chunk_order
                     6 progress    7 pushnames
    Conversation     1 id          2 messages        3 name
                     5 last_msg_timestamp            6 unread_count
    HistorySyncMsg   1 message (BYTES)               2 msg_order_id

Nota importante: ``HistorySyncMsg.message`` esta declarado como ``bytes``, no
como un mensaje tipado, porque pywhats NO define ``WebMessageInfo`` en ningun
sitio (solo lo menciona en comentarios). Esos bytes son el ``WebMessageInfo``
serializado y se conservan tal cual en ``messages.raw_proto``: asi el mensaje
puede reinterpretarse mas adelante sin volver a pedirlo. La normalizacion a
columnas es responsabilidad de ``history_service`` y requiere definir
localmente el descriptor de WebMessageInfo.

Ademas se guarda el blob inflado completo en ``data/history/`` antes de
procesarlo. Un fallo posterior en la normalizacion no puede costar historial.
"""

from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.logging_setup import get_logger

log = get_logger("SYNC")

_MARKER = "_whatsapp_backup_history_patch"


@dataclass
class HistoryConversation:
    """Una conversacion del blob, con sus mensajes en crudo."""

    jid: str
    name: str | None
    last_message_timestamp: int | None
    unread_count: int | None
    # (raw_webmessageinfo_bytes, msg_order_id). Los bytes son el
    # WebMessageInfo serializado, listos para raw_proto.
    messages: list[tuple[bytes, int]] = field(default_factory=list)
    # Campo 11 del protobuf. 0 = COMPLETE_BUT_MORE_MESSAGES_REMAIN_ON_PRIMARY,
    # que NO significa terminar: quedan mensajes en el telefono.
    end_of_history_type: int | None = None
    end_of_history: bool = False


@dataclass
class FullHistorySync:
    """Contenido completo del blob, mas alla del resumen de pywhats."""

    sync_type: str
    chunk_order: int
    progress: int
    conversations: list[HistoryConversation]
    pushnames: list[tuple[str, str]]
    blob_path: Path | None = None
    # Campos 8 y 12 de HistorySyncNotification, que pywhats no modela.
    original_message_id: str | None = None
    peer_session_id: str | None = None

    @property
    def message_count(self) -> int:
        return sum(len(c.messages) for c in self.conversations)


# Callback que recibe el contenido completo. Lo registra la aplicacion.
_callback: Callable[[FullHistorySync], None] | None = None
_blob_dir: Path | None = None


def set_callback(callback: Callable[[FullHistorySync], None] | None) -> None:
    """Registra quien recibe el History Sync completo."""
    global _callback
    _callback = callback


def set_blob_dir(directory: Path | None) -> None:
    """Carpeta donde se archiva cada blob inflado. ``None`` desactiva."""
    global _blob_dir
    _blob_dir = directory
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)


def _archive_blob(raw: bytes, sync_type: str, chunk_order: int) -> Path | None:
    """Guarda el protobuf inflado. Un fallo aqui no interrumpe la sincronizacion."""
    if _blob_dir is None:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    digest = hashlib.sha256(raw).hexdigest()[:12]
    path = _blob_dir / f"{stamp}-{sync_type}-chunk{chunk_order:03d}-{digest}.pb"
    try:
        path.write_bytes(raw)
        return path
    except OSError as exc:
        log.warning("No se pudo archivar el blob de History Sync: %s", exc)
        return None


def parse_full(raw: bytes) -> FullHistorySync:
    """Parsea el protobuf YA descomprimido en la estructura completa."""
    from pywhats.proto import HistorySync as HistorySyncProto

    proto = HistorySyncProto()
    proto.ParseFromString(raw)

    sync_type = _sync_type_name(proto.sync_type)
    def _end_marker(conversation: Any) -> tuple[int | None, bool]:
        """Lee los campos 8 y 11, que el descriptor de pywhats no expone.

        Se reserializa la conversacion y se reparsea con nuestro descriptor:
        protobuf conserva los campos desconocidos, asi que siguen ahi.
        """
        from app.proto import ConversationEndMarker

        marker = ConversationEndMarker()
        try:
            marker.ParseFromString(conversation.SerializeToString())
        except Exception:  # noqa: BLE001
            return None, False
        tipo = (
            marker.endOfHistoryTransferType
            if marker.HasField("endOfHistoryTransferType")
            else None
        )
        return tipo, bool(marker.endOfHistoryTransfer)

    conversations = [
        HistoryConversation(
            jid=conversation.id,
            name=conversation.name or None,
            last_message_timestamp=int(conversation.last_msg_timestamp)
            if conversation.last_msg_timestamp
            else None,
            unread_count=int(conversation.unread_count) if conversation.unread_count else None,
            messages=[
                (bytes(item.message), int(item.msg_order_id))
                for item in conversation.messages
                if item.message
            ],
            end_of_history_type=_end_marker(conversation)[0],
            end_of_history=_end_marker(conversation)[1],
        )
        for conversation in proto.conversations
    ]

    return FullHistorySync(
        sync_type=sync_type,
        chunk_order=int(proto.chunk_order),
        progress=int(proto.progress),
        conversations=conversations,
        pushnames=[(p.id, p.pushname) for p in proto.pushnames],
    )


def _sync_type_name(value: int) -> str:
    from pywhats.proto import HistorySync as HistorySyncProto

    try:
        return str(HistorySyncProto.HistorySyncType.keys()[value])
    except (IndexError, TypeError):
        return str(value)


def apply() -> bool:
    """Envuelve ``pywhats.history.parse_history_sync``. Idempotente.

    ``HistorySyncer.handle`` (history.py:99) resuelve ``parse_history_sync``
    como global del modulo en tiempo de llamada, asi que un unico parche cubre
    el camino real.
    """
    import pywhats.history

    original = pywhats.history.parse_history_sync
    if getattr(original, _MARKER, False):
        return True

    def parse_history_sync(compressed: bytes) -> Any:
        # Se replica el inflate para poder quedarse con los bytes crudos; el
        # original vuelve a hacerlo por su cuenta y devuelve su resumen
        # intacto, de modo que el evento 'history_sync' de pywhats no cambia.
        try:
            raw = zlib.decompress(compressed)
        except zlib.error:
            # Que falle aqui no debe romper el flujo: se delega y que el
            # original reporte el problema como siempre.
            return original(compressed)

        summary = original(compressed)

        try:
            full = parse_full(raw)
            full.blob_path = _archive_blob(raw, full.sync_type, full.chunk_order)
            log.info(
                "HistorySync completo type=%s chunk=%d progreso=%d conversaciones=%d mensajes=%d%s",
                full.sync_type,
                full.chunk_order,
                full.progress,
                len(full.conversations),
                full.message_count,
                f" archivado={full.blob_path.name}" if full.blob_path else "",
            )
            if _callback is not None:
                _callback(full)
        except Exception:  # noqa: BLE001 - preservar el flujo de pywhats
            log.exception("Fallo al extraer el History Sync completo; el resumen sigue siendo valido")

        return summary

    setattr(parse_history_sync, _MARKER, True)
    pywhats.history.parse_history_sync = parse_history_sync

    log.info("Adaptacion de History Sync (mensajes individuales) aplicada")
    return True
