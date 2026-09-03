"""Clasificador central de mensajes.

Una sola funcion decide si algo es una conversacion o fontaneria del
protocolo. Antes esa decision estaba repartida en comprobaciones sueltas
(``if text is None``, ``if type == 'unknown'``, ``if jid == ...``) y el
resultado fue que 75 Peer Data Operations acabaron guardadas como mensajes
del chat personal.

REGLA: la decision se toma mirando el PROTOBUF, no
por si el texto viene vacio. Un mensaje sin texto puede ser una imagen, una
ubicacion o un evento visible; y un mensaje "unknown" puede ser un tipo real
que aun no interpretamos. Nada se descarta por no entenderlo: solo se
descarta lo que se demuestra que es interno.

Evidencia que motiva el modulo (auditoria del 2026-09-02)::

    [SELF-AUDIT] 75 filas en el chat propio
    tipo=unknown  source=live  from_me=False  con_texto=0  raw_proto=0

    hex del evento: 620e1007420a...
                    ^^ 0x62 = (12 << 3) | 2  -> campo 12 = protocolMessage

Son los ecos de nuestras propias peticiones ON_DEMAND.
"""

from __future__ import annotations

from enum import Enum

from app.core.message_parser import (
    MESSAGE_FIELD_NAMES,
    top_level_fields,
    unwrap_message,
)

# Campos de Message que NUNCA son una conversacion: son control del protocolo.
#   2  senderKeyDistributionMessage  reparto de clave de grupo
#  12  protocolMessage               revokes, history sync, peer data, app state
#  35  messageContextInfo            metadatos que acompanan, no contenido
PROTOCOL_ONLY_FIELDS = frozenset({2, 12, 35})

# Acompanantes que pueden convivir con contenido visible sin anularlo.
COMPANION_FIELDS = frozenset({35})


class MessageClass(str, Enum):
    """Que es un mensaje, y por tanto que se hace con el."""

    VISIBLE_TEXT = "VISIBLE_TEXT"
    VISIBLE_MEDIA = "VISIBLE_MEDIA"
    VISIBLE_CONTACT = "VISIBLE_CONTACT"
    VISIBLE_LOCATION = "VISIBLE_LOCATION"
    VISIBLE_POLL = "VISIBLE_POLL"
    VISIBLE_SYSTEM_EVENT = "VISIBLE_SYSTEM_EVENT"
    # Tipo real de WhatsApp que aun no sabemos pintar. SE CONSERVA y se
    # muestra como "[Mensaje no compatible]": es contenido del usuario.
    UNSUPPORTED_VISIBLE = "UNSUPPORTED_VISIBLE"

    # --- Internos: se procesan pero NO se guardan como mensaje de chat ---
    PROTOCOL_INTERNAL = "PROTOCOL_INTERNAL"
    SIGNAL_CONTROL = "SIGNAL_CONTROL"
    # No se pudo determinar. Se CONSERVA para revisarlo, no se borra.
    UNKNOWN_NEEDS_REVIEW = "UNKNOWN_NEEDS_REVIEW"


VISIBLE_CLASSES = frozenset(
    {
        MessageClass.VISIBLE_TEXT,
        MessageClass.VISIBLE_MEDIA,
        MessageClass.VISIBLE_CONTACT,
        MessageClass.VISIBLE_LOCATION,
        MessageClass.VISIBLE_POLL,
        MessageClass.VISIBLE_SYSTEM_EVENT,
        MessageClass.UNSUPPORTED_VISIBLE,
        MessageClass.UNKNOWN_NEEDS_REVIEW,
    }
)

INTERNAL_CLASSES = frozenset(
    {MessageClass.PROTOCOL_INTERNAL, MessageClass.SIGNAL_CONTROL}
)

# Tipo normalizado -> clase visible.
_VISIBLE_BY_TYPE = {
    "text": MessageClass.VISIBLE_TEXT,
    "image": MessageClass.VISIBLE_MEDIA,
    "video": MessageClass.VISIBLE_MEDIA,
    "gif": MessageClass.VISIBLE_MEDIA,
    "audio": MessageClass.VISIBLE_MEDIA,
    "voice_note": MessageClass.VISIBLE_MEDIA,
    "sticker": MessageClass.VISIBLE_MEDIA,
    "document": MessageClass.VISIBLE_MEDIA,
    "contact": MessageClass.VISIBLE_CONTACT,
    "location": MessageClass.VISIBLE_LOCATION,
    "poll": MessageClass.VISIBLE_POLL,
    "system": MessageClass.VISIBLE_SYSTEM_EVENT,
    "edited": MessageClass.VISIBLE_TEXT,
    "reaction": MessageClass.PROTOCOL_INTERNAL,  # pywhats lo emite aparte
    "protocol": MessageClass.PROTOCOL_INTERNAL,
    "senderkey": MessageClass.SIGNAL_CONTROL,
}


def classify_message_bytes(message_bytes: bytes | None) -> MessageClass:
    """Clasifica a partir del ``Message`` E2E serializado.

    Es el camino preferente: decide mirando que campos trae el protobuf.
    """
    if not message_bytes:
        return MessageClass.UNKNOWN_NEEDS_REVIEW

    inner, _wrappers = unwrap_message(message_bytes)
    present = {number for number, _wire, _payload in top_level_fields(inner)}
    if not present:
        return MessageClass.UNKNOWN_NEEDS_REVIEW

    meaningful = present - COMPANION_FIELDS
    if not meaningful:
        # Solo messageContextInfo: no hay contenido.
        return MessageClass.PROTOCOL_INTERNAL

    # Si TODO lo que trae es control, es interno. Si ademas hay contenido
    # visible, manda el contenido: un mensaje normal puede venir acompanado
    # de un reparto de clave de grupo.
    if meaningful <= PROTOCOL_ONLY_FIELDS:
        if 2 in meaningful and 12 not in meaningful:
            return MessageClass.SIGNAL_CONTROL
        return MessageClass.PROTOCOL_INTERNAL

    visible_fields = meaningful - PROTOCOL_ONLY_FIELDS
    field_number = sorted(visible_fields)[0]
    proto_name = MESSAGE_FIELD_NAMES.get(field_number)
    if proto_name is None:
        # Campo que ni la referencia conocia. Es contenido: se conserva.
        return MessageClass.UNSUPPORTED_VISIBLE

    from app.core.message_parser import _NORMALIZED_TYPES

    normalized = _NORMALIZED_TYPES.get(proto_name, proto_name)
    return _VISIBLE_BY_TYPE.get(normalized, MessageClass.UNSUPPORTED_VISIBLE)


def classify_parsed(message: object) -> MessageClass:
    """Clasifica un :class:`ParsedMessage` ya normalizado (History Sync).

    Se apoya en ``raw_proto``, que conserva el ``WebMessageInfo`` original.
    """
    message_type = getattr(message, "message_type", "unknown")

    if message_type == "system":
        # messageStubType: alta/baja de grupo, cambio de numero, llamada
        # perdida. WhatsApp los muestra, asi que son visibles.
        return MessageClass.VISIBLE_SYSTEM_EVENT

    known = _VISIBLE_BY_TYPE.get(message_type)
    if known is not None:
        return known
    if message_type == "unknown":
        return MessageClass.UNKNOWN_NEEDS_REVIEW
    return MessageClass.UNSUPPORTED_VISIBLE


def is_visible(message_class: MessageClass) -> bool:
    """``True`` si debe guardarse y mostrarse como mensaje de conversacion."""
    return message_class in VISIBLE_CLASSES


def is_internal(message_class: MessageClass) -> bool:
    """``True`` si es fontaneria: se procesa pero NO va al chat."""
    return message_class in INTERNAL_CLASSES
