"""Leer un blob de History Sync entero, con sus mensajes dentro.

QUE ES
------
El lector de los blobs que WhatsApp entrega comprimidos: saca las
conversaciones, sus metadatos y los ``WebMessageInfo`` en crudo, y archiva el
blob antes de interpretarlo.

DE DONDE VIENE
--------------
Era ``app/compat/history_compat.py``, un parche a ``pywhats``, porque aquella
libreria contaba los mensajes del blob y los TIRABA -- su propio docstring lo
admitia: *"kept opaque for now and only counted"*.

Al soltar pywhats, el parche desaparecio y el lector se quedo: no tenia nada
de aquella libreria mas que el descriptor protobuf, que ahora vive en
``app/wa/proto``. Sigue siendo el unico sitio donde se decide que trae un
blob, y por el pasan tanto los archivos viejos como lo que llega hoy.

EL ARCHIVADO ES EL SEGURO
-------------------------
El blob se guarda ANTES de interpretarlo. No es paranoia: cuando la base se
vacio, esos archivos eran la unica copia que quedaba, y de ellos se
recuperaron 5920 mensajes que ya no estaban en ninguna otra parte.

EL MARCADOR DE FIN
------------------
Los campos 8 y 11 de cada conversacion dicen si el telefono da el historial
por terminado. Es lo unico que distingue "no queda nada" de "queda mas en el
telefono", y de el cuelga todo el estado ``exhausted``. Se leen a mano porque
ningun descriptor generado los expone.
"""

from __future__ import annotations

import contextvars
import hashlib
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.core.logging_setup import get_logger

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
_blob_dir: Path | None = None

# Lo que traia la NOTIFICACION que provoco este blob, para poder pegarselo.
#
# El aviso y el blob son dos cosas distintas: la notificacion llega por el
# socket con los campos 8 y 12 (``originalMessageID`` y
# ``peerDataRequestSessionID``), y el blob se descarga despues por HTTP. Sin
# guardarlo aqui, el identificador de la peticion se pierde por el camino y la
# unica correlacion posible vuelve a ser adivinar por JID de chat.
#
# Es un ContextVar y no una global porque cada notificacion se atiende en su
# propia tarea: dos blobs simultaneos no pueden pisarse el identificador.
_notificacion: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "history_sync_notification", default=None
)

_MARKER_SYNCER = "_whatsapp_backup_history_notif_patch"

#: Nombres de ``HistorySyncNotification.SyncType``, por valor.
_TIPOS_DE_AVISO = {
    0: "INITIAL_BOOTSTRAP",
    1: "INITIAL_STATUS_V3",
    2: "FULL",
    3: "RECENT",
    4: "PUSH_NAME",
    5: "NON_BLOCKING_DATA",
    6: "ON_DEMAND",
}


def leer_notificacion(notif: Any) -> dict[str, Any]:
    """Los campos utiles del aviso, incluidos los que pywhats no modela.

    ``HistorySyncNotification`` de pywhats 0.2.0 llega hasta el campo 8. El 12
    -- ``peerDataRequestSessionID`` -- sigue en el mensaje como campo
    desconocido, asi que se reserializa y se reparsea con nuestro descriptor.

    Nunca se devuelven ni la clave de medios ni la ruta: son credenciales de
    descarga.
    """
    datos: dict[str, Any] = {
        "sync_type": None,
        "chunk_order": None,
        "original_message_id": None,
        "peer_session_id": None,
        "file_length": None,
    }
    try:
        valor = getattr(notif, "sync_type", None)
        datos["sync_type"] = _TIPOS_DE_AVISO.get(int(valor), str(valor)) if valor is not None else None
        datos["chunk_order"] = int(getattr(notif, "chunk_order", 0) or 0)
        datos["file_length"] = int(getattr(notif, "file_length", 0) or 0)
    except Exception:  # noqa: BLE001 - leer no puede cortar la descarga
        pass

    try:
        from app.models.proto import OnDemandNotification

        extra = OnDemandNotification()
        extra.ParseFromString(notif.SerializeToString())
        if extra.HasField("originalMessageID"):
            datos["original_message_id"] = extra.originalMessageID
        if extra.HasField("peerDataRequestSessionID"):
            datos["peer_session_id"] = extra.peerDataRequestSessionID
    except Exception:  # noqa: BLE001
        log.debug("No se pudieron leer los campos 8/12 del aviso de History Sync")
    return datos




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
    from app.wa.proto import HistorySync as HistorySyncProto

    proto = HistorySyncProto()
    proto.ParseFromString(raw)

    sync_type = _sync_type_name(proto.sync_type)
    def _end_marker(conversation: Any) -> tuple[int | None, bool]:
        """Lee los campos 8 y 11, que el descriptor de pywhats no expone.

        Se reserializa la conversacion y se reparsea con nuestro descriptor:
        protobuf conserva los campos desconocidos, asi que siguen ahi.
        """
        from app.models.proto import ConversationEndMarker

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

    aviso = _notificacion.get() or {}
    return FullHistorySync(
        sync_type=sync_type,
        chunk_order=int(proto.chunk_order),
        progress=int(proto.progress),
        conversations=conversations,
        pushnames=[(p.id, p.pushname) for p in proto.pushnames],
        # Vienen del AVISO, no del blob: el blob no los lleva.
        original_message_id=aviso.get("original_message_id"),
        peer_session_id=aviso.get("peer_session_id"),
    )


def parse_full_json(datos: dict[str, Any]) -> FullHistorySync:
    """El mismo blob, pero como lo entrega Baileys: JSON, no protobuf.

    UNA SOLA IMPLEMENTACION, DOS ORIGENES
    --------------------------------------
    Este parseo vivia duplicado dentro de ``BaileysClient._historial``, con
    sus propios ``HistorialCompleto``/``ConversacionDelHistorial`` -- una
    copia letra por letra de estas clases con otro nombre. Se unifica aqui
    porque hace falta desde DOS sitios que no pueden discrepar: la llegada en
    vivo del worker, y la recuperacion de blobs archivados en disco
    (``blob_reingest``). Dos implementaciones de "que es un History Sync"
    serian dos ocasiones para que una se quede atras cuando la otra cambie.

    ``validate=True`` en el b64decode NO es un detalle: sin el, decodificar
    descarta los caracteres que no reconoce y devuelve basura en vez de
    fallar. Esa basura entraria en la ingesta como un mensaje, y un mensaje
    inventado es peor que uno que falta.
    """
    import base64

    conversations = []
    for c in datos.get("conversations") or []:
        mensajes = []
        for par in c.get("messages") or []:
            try:
                mensajes.append((base64.b64decode(par[0], validate=True), int(par[1])))
            except Exception:  # noqa: BLE001 - un mensaje roto no tira el lote
                continue
        conversations.append(
            HistoryConversation(
                jid=c.get("jid") or "",
                name=c.get("name"),
                last_message_timestamp=c.get("last_message_timestamp"),
                unread_count=c.get("unread_count"),
                messages=mensajes,
                end_of_history_type=c.get("end_of_history_type"),
                end_of_history=bool(c.get("end_of_history")),
            )
        )
    return FullHistorySync(
        sync_type=datos.get("sync_type") or "DESCONOCIDO",
        chunk_order=int(datos.get("chunk_order") or 0),
        progress=int(datos.get("progress") or 0),
        conversations=conversations,
        pushnames=[tuple(p) for p in (datos.get("pushnames") or []) if len(p) == 2],
    )


def _sync_type_name(value: int) -> str:
    from app.wa.proto import HistorySync as HistorySyncProto

    try:
        return str(HistorySyncProto.HistorySyncType.keys()[value])
    except (IndexError, TypeError):
        return str(value)


