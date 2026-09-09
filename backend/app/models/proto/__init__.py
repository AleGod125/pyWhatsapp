"""Descriptores protobuf propios del proyecto.

Aqui vive solo lo que pywhats 0.2.0 NO trae. Todo lo que el paquete ya define
(Message, MessageKey de e2e, HistorySync, ProtocolMessage...) se importa de
``pywhats.proto`` y no se duplica.

Ver whatsapp_backup.proto para la procedencia de los numeros de campo.
"""

from __future__ import annotations

from app.models.proto.whatsapp_backup_pb2 import (
    HISTORY_SYNC_ON_DEMAND,
    HistorySyncOnDemandRequest,
    MessageKey,
    ConversationEndMarker,
    OnDemandMessage,
    OnDemandNotification,
    OnDemandProtocolMessage,
    PeerDataOperationRequestMessage,
    WebMessageInfo,
)

__all__ = [
    "HISTORY_SYNC_ON_DEMAND",
    "HistorySyncOnDemandRequest",
    "MessageKey",
    "ConversationEndMarker",
    "OnDemandMessage",
    "OnDemandNotification",
    "OnDemandProtocolMessage",
    "PeerDataOperationRequestMessage",
    "WebMessageInfo",
]
