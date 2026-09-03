"""Normalizacion de ``WebMessageInfo`` a filas de PostgreSQL.

Principio rector: un mensaje NO se descarta porque la
aplicacion no sepa interpretarlo. Los bytes originales van siempre a
``messages.raw_proto``; las columnas normalizadas son una comodidad para
buscar y pintar, no la fuente de verdad.

Por que hay un escaner de protobuf a mano
-----------------------------------------
``pywhats.proto.Message`` define 12 de los ~70 tipos que existen de verdad
(``conversation``, ``imageMessage``, ``extendedTextMessage``...). Si el tipo se
dedujera solo con ese descriptor, cualquier encuesta, ubicacion, contacto o
mensaje efimero acabaria como "unknown" y perderiamos la unica pista que
tenemos de que era.

En su lugar se leen los numeros de campo de primer nivel directamente del
protobuf serializado. Un numero de campo identifica el tipo aunque no tengamos
su descriptor. Los nombres salen de la definicion publica de whatsmeow
(``proto/waE2E/WAWebProtobufsE2E.proto``), consultada como referencia del
protocolo; ninguno esta inventado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("SYNC")

# ---------------------------------------------------------------------------
# Mapa de campos de WAWebProtobufsE2E.Message
# ---------------------------------------------------------------------------

MESSAGE_FIELD_NAMES: dict[int, str] = {
    1: "conversation",
    2: "senderKeyDistributionMessage",
    3: "imageMessage",
    4: "contactMessage",
    5: "locationMessage",
    6: "extendedTextMessage",
    7: "documentMessage",
    8: "audioMessage",
    9: "videoMessage",
    10: "call",
    11: "chat",
    12: "protocolMessage",
    13: "contactsArrayMessage",
    14: "highlyStructuredMessage",
    15: "fastRatchetKeySenderKeyDistributionMessage",
    16: "sendPaymentMessage",
    18: "liveLocationMessage",
    22: "requestPaymentMessage",
    23: "declinePaymentRequestMessage",
    24: "cancelPaymentRequestMessage",
    25: "templateMessage",
    26: "stickerMessage",
    28: "groupInviteMessage",
    29: "templateButtonReplyMessage",
    30: "productMessage",
    31: "deviceSentMessage",
    35: "messageContextInfo",
    36: "listMessage",
    37: "viewOnceMessage",
    38: "orderMessage",
    39: "listResponseMessage",
    40: "ephemeralMessage",
    41: "invoiceMessage",
    42: "buttonsMessage",
    43: "buttonsResponseMessage",
    44: "paymentInviteMessage",
    45: "interactiveMessage",
    46: "reactionMessage",
    47: "stickerSyncRmrMessage",
    48: "interactiveResponseMessage",
    49: "pollCreationMessage",
    50: "pollUpdateMessage",
    51: "keepInChatMessage",
    53: "documentWithCaptionMessage",
    54: "requestPhoneNumberMessage",
    55: "viewOnceMessageV2",
    56: "encReactionMessage",
    58: "editedMessage",
    59: "viewOnceMessageV2Extension",
    60: "pollCreationMessageV2",
    61: "scheduledCallCreationMessage",
    62: "groupMentionedMessage",
    63: "pinInChatMessage",
    64: "pollCreationMessageV3",
    65: "scheduledCallEditMessage",
    66: "ptvMessage",
    69: "callLogMesssage",
    70: "messageHistoryBundle",
    71: "encCommentMessage",
    72: "bcallMessage",
    74: "lottieStickerMessage",
    75: "eventMessage",
    77: "commentMessage",
    78: "newsletterAdminInviteMessage",
    80: "placeholderMessage",
    82: "secretEncryptedMessage",
    83: "albumMessage",
    86: "stickerPackMessage",
    88: "pollResultSnapshotMessage",
}

# Envoltorios: no son el mensaje, lo CONTIENEN. Todos son FutureProofMessage,
# cuyo campo 1 es el Message anidado. Hay que desenvolverlos o todo mensaje
# efimero (lo normal en muchos chats) se clasificaria como "ephemeralMessage".
WRAPPER_FIELDS: frozenset[int] = frozenset({37, 40, 53, 55, 59, 62, 31})

# Dentro de que campo viaja el mensaje real de cada envoltorio.
# Los FutureProofMessage lo llevan en el campo 1; DeviceSentMessage (31) lo
# lleva en el 2, porque el 1 es su destinationJid. Confundirlos deja el
# mensaje sin desenvolver y acaba clasificado como "unknown": es lo que
# pasaba con los mensajes enviados desde el propio telefono.
_WRAPPER_INNER_FIELD: dict[int, int] = {31: 2}
_DEFAULT_INNER_FIELD = 1

# Campos que acompanan al mensaje sin ser su tipo.
_IGNORED_FIELDS: frozenset[int] = frozenset({35})  # messageContextInfo

# Traduccion al vocabulario de la aplicacion. Lo que no aparezca aqui conserva
# su nombre de protobuf, que sigue siendo mas informativo que "unknown".
_NORMALIZED_TYPES: dict[str, str] = {
    "conversation": "text",
    "extendedTextMessage": "text",
    "imageMessage": "image",
    "videoMessage": "video",
    "ptvMessage": "video",
    "audioMessage": "audio",
    "documentMessage": "document",
    "documentWithCaptionMessage": "document",
    "stickerMessage": "sticker",
    "lottieStickerMessage": "sticker",
    "reactionMessage": "reaction",
    "encReactionMessage": "reaction",
    "protocolMessage": "protocol",
    "locationMessage": "location",
    "liveLocationMessage": "location",
    "contactMessage": "contact",
    "contactsArrayMessage": "contact",
    "pollCreationMessage": "poll",
    "pollCreationMessageV2": "poll",
    "pollCreationMessageV3": "poll",
    "pollUpdateMessage": "poll",
    "editedMessage": "edited",
    "senderKeyDistributionMessage": "senderkey",
    "deviceSentMessage": "device_sent",
    "albumMessage": "album",
}


# ---------------------------------------------------------------------------
# Escaner minimo de protobuf
# ---------------------------------------------------------------------------


def _read_varint(data: bytes, index: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while index < len(data):
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, index
        shift += 7
        if shift > 63:
            raise ValueError("varint demasiado largo")
    raise ValueError("varint truncado")


def top_level_fields(data: bytes) -> list[tuple[int, int, bytes | None]]:
    """``(numero_de_campo, wire_type, contenido)`` del primer nivel.

    Solo se devuelve contenido para los campos length-delimited, que son los
    que interesan (mensajes anidados y cadenas). Ante datos malformados se
    devuelve lo leido hasta el fallo en vez de reventar: preferimos una
    clasificacion parcial a perder el mensaje.
    """
    fields: list[tuple[int, int, bytes | None]] = []
    index = 0
    try:
        while index < len(data):
            key, index = _read_varint(data, index)
            number, wire = key >> 3, key & 7
            if wire == 0:
                _, index = _read_varint(data, index)
                fields.append((number, wire, None))
            elif wire == 1:
                index += 8
                fields.append((number, wire, None))
            elif wire == 2:
                length, index = _read_varint(data, index)
                payload = data[index : index + length]
                index += length
                fields.append((number, wire, payload))
            elif wire == 5:
                index += 4
                fields.append((number, wire, None))
            else:
                break  # grupos (deprecados): no se intentan
    except (ValueError, IndexError):
        log.debug("Protobuf malformado al escanear campos; se usa lo leido")
    return fields


def unwrap_message(data: bytes, *, max_depth: int = 4) -> tuple[bytes, list[str]]:
    """Desenvuelve viewOnce / ephemeral / edited y devuelve el mensaje interno.

    Tambien devuelve la lista de envoltorios atravesados, que se guarda en
    ``raw_metadata``: saber que un mensaje era "ver una vez" es informacion
    real que no se debe perder al normalizar.
    """
    wrappers: list[str] = []
    current = data
    for _ in range(max_depth):
        candidates = [
            (number, payload)
            for number, _wire, payload in top_level_fields(current)
            if number in WRAPPER_FIELDS and payload
        ]
        if not candidates:
            break
        number, payload = candidates[0]
        wrappers.append(MESSAGE_FIELD_NAMES.get(number, str(number)))
        inner_field = _WRAPPER_INNER_FIELD.get(number, _DEFAULT_INNER_FIELD)
        inner = [p for n, _w, p in top_level_fields(payload) if n == inner_field and p]
        if not inner:
            break
        current = inner[0]
    return current, wrappers


def detect_message_type(message_bytes: bytes) -> tuple[str, str | None]:
    """``(tipo_normalizado, nombre_protobuf)`` del mensaje ya desenvuelto."""
    present = [
        number
        for number, _wire, _payload in top_level_fields(message_bytes)
        if number not in _IGNORED_FIELDS
    ]
    if not present:
        return "unknown", None

    # El primero es el tipo real: los acompanantes (contextInfo) ya se filtraron.
    number = present[0]
    proto_name = MESSAGE_FIELD_NAMES.get(number)
    if proto_name is None:
        # Campo que ni whatsmeow tenia cuando se escribio esto. Se conserva el
        # numero para poder investigarlo: raw_proto sigue intacto.
        return "unknown", f"field_{number}"
    return _NORMALIZED_TYPES.get(proto_name, proto_name), proto_name


# ---------------------------------------------------------------------------
# Extraccion de texto y adjuntos
# ---------------------------------------------------------------------------


def extract_text(message_bytes: bytes) -> str | None:
    """Texto legible del mensaje, si lo tiene.

    Se usa el ``Message`` de pywhats, que si define ``conversation`` y
    ``extendedTextMessage``, y se cae con elegancia si el mensaje es de un
    tipo que ese descriptor no conoce.
    """
    from pywhats.proto import Message as MessageProto

    proto = MessageProto()
    try:
        proto.ParseFromString(message_bytes)
    except Exception:  # noqa: BLE001 - un tipo desconocido no debe perder la fila
        return None

    if proto.conversation:
        return proto.conversation
    if proto.HasField("extended_text_message") and proto.extended_text_message.text:
        return proto.extended_text_message.text

    # Los adjuntos llevan el texto en el caption.
    for attribute in ("image_message", "video_message", "document_message"):
        if proto.HasField(attribute):
            caption = getattr(proto, attribute).caption
            if caption:
                return caption
    return None


@dataclass
class ParsedMedia:
    """Metadata de adjunto extraida del mensaje."""

    media_type: str
    mime_type: str | None = None
    file_name: str | None = None
    file_size: int | None = None
    duration_seconds: int | None = None
    width: int | None = None
    height: int | None = None
    direct_path: str | None = None
    media_key: bytes | None = None
    file_sha256: bytes | None = None
    file_enc_sha256: bytes | None = None


def extract_media(message_bytes: bytes, normalized_type: str) -> ParsedMedia | None:
    """Adjunto del mensaje, si pywhats sabe describirlo.

    Solo cubre los tipos que ``pywhats.proto`` define. Para el resto se
    devuelve ``None`` y el mensaje se guarda igual: ``raw_proto`` permite
    extraer el adjunto mas adelante sin volver a pedir nada.
    """
    from pywhats.proto import Message as MessageProto

    attribute_by_type = {
        "image": "image_message",
        "video": "video_message",
        "audio": "audio_message",
        "document": "document_message",
        "sticker": "sticker_message",
    }
    attribute = attribute_by_type.get(normalized_type)
    if attribute is None:
        return None

    proto = MessageProto()
    try:
        proto.ParseFromString(message_bytes)
    except Exception:  # noqa: BLE001
        return None
    if not proto.HasField(attribute):
        return None

    node = getattr(proto, attribute)
    media_type = normalized_type
    # Distinciones que solo se ven mirando banderas del propio adjunto.
    if normalized_type == "audio" and getattr(node, "ptt", False):
        media_type = "voice_note"
    elif normalized_type == "video" and getattr(node, "gif_playback", False):
        media_type = "gif"

    return ParsedMedia(
        media_type=media_type,
        mime_type=getattr(node, "mimetype", "") or None,
        file_name=getattr(node, "file_name", "") or None,
        file_size=int(getattr(node, "file_length", 0)) or None,
        duration_seconds=int(getattr(node, "seconds", 0)) or None,
        width=int(getattr(node, "width", 0)) or None,
        height=int(getattr(node, "height", 0)) or None,
        direct_path=getattr(node, "direct_path", "") or None,
        media_key=bytes(getattr(node, "media_key", b"")) or None,
        file_sha256=bytes(getattr(node, "file_sha256", b"")) or None,
        file_enc_sha256=bytes(getattr(node, "file_enc_sha256", b"")) or None,
    )


@dataclass
class InterpretedMessage:
    """Lo que se puede saber de un ``Message`` E2E suelto, sin sobre."""

    message_type: str
    proto_type: str | None
    text: str | None
    media: ParsedMedia | None
    wrappers: list[str]


def interpret_message_bytes(message_bytes: bytes | None) -> InterpretedMessage | None:
    """Interpreta un ``Message`` E2E que NO viene dentro de un WebMessageInfo.

    Hace falta porque hay dos formas distintas de recibir lo mismo:

    * History Sync entrega ``WebMessageInfo`` (sobre con clave, marca de
      tiempo y remitente), que resuelve :func:`parse_web_message_info`;
    * el receptor en vivo entrega el ``Message`` E2E pelado, sin sobre.

    Y hace falta de verdad, no por simetria: pywhats no desenvuelve
    ``deviceSentMessage``, asi que las fotos que el usuario se envia a si
    mismo llegan con ``media=None`` y acababan guardadas como ``unknown``.
    Aqui se desenvuelve y se recupera tanto el tipo como el adjunto.
    """
    if not message_bytes:
        return None
    inner, wrappers = unwrap_message(bytes(message_bytes))
    message_type, proto_name = detect_message_type(inner)
    if message_type == "unknown" and not wrappers:
        return None

    media = extract_media(inner, message_type)
    if media is not None and media.media_type != message_type:
        # ``voice_note`` y ``gif`` no son campos del protobuf: son un
        # ``audioMessage`` con ``ptt`` y un ``videoMessage`` con
        # ``gifPlayback``, banderas que solo se ven al mirar el adjunto. Sin
        # esto un mensaje de voz salia 'audio' por este camino y 'voice_note'
        # por el de pywhats, y el mismo mensaje se tipaba distinto segun por
        # donde hubiera entrado.
        message_type = media.media_type

    return InterpretedMessage(
        message_type=message_type,
        proto_type=proto_name if proto_name and proto_name != message_type else None,
        text=extract_text(inner),
        media=media,
        wrappers=wrappers,
    )


# ---------------------------------------------------------------------------
# WebMessageInfo -> fila
# ---------------------------------------------------------------------------


@dataclass
class ParsedMessage:
    """Un ``WebMessageInfo`` listo para persistir."""

    chat_jid: str
    timestamp: int
    from_me: bool
    whatsapp_message_id: str | None
    sender_jid: str | None
    sender_lid: str | None
    message_type: str
    text: str | None
    push_name: str | None
    raw_proto: bytes
    media: ParsedMedia | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def parse_web_message_info(raw: bytes) -> ParsedMessage | None:
    """Normaliza un ``WebMessageInfo`` serializado.

    Devuelve ``None`` solo si el protobuf no se puede parsear en absoluto; un
    tipo desconocido NO es motivo para descartar el mensaje.
    """
    from app.models.proto import WebMessageInfo

    info = WebMessageInfo()
    try:
        info.ParseFromString(raw)
    except Exception:  # noqa: BLE001
        log.debug("WebMessageInfo ilegible (%d bytes); se descarta", len(raw))
        return None

    chat_jid = info.key.remoteJID
    if not chat_jid:
        return None

    # El ID solo se acepta si es real. Nunca se fabrica uno: el cursor de
    # ON_DEMAND depende de que esta columna sea de fiar.
    message_id = info.key.ID or None

    metadata: dict[str, Any] = {}
    message_type = "unknown"
    text: str | None = None
    media: ParsedMedia | None = None

    if info.message:
        inner, wrappers = unwrap_message(bytes(info.message))
        if wrappers:
            metadata["wrappers"] = wrappers
        message_type, proto_name = detect_message_type(inner)
        if proto_name and proto_name != message_type:
            metadata["proto_type"] = proto_name
        text = extract_text(inner)
        media = extract_media(inner, message_type)
    elif info.messageStubType:
        # Mensaje de sistema: alta o baja de un grupo, cambio de numero,
        # llamada perdida... Forma parte de la conversacion.
        message_type = "system"
        metadata["stub_type"] = int(info.messageStubType)
        if info.messageStubParameters:
            metadata["stub_parameters"] = list(info.messageStubParameters)

    # participant identifica al emisor real dentro de un grupo.
    participant = info.participant or info.key.participant or None
    if info.key.fromMe:
        sender = None  # somos nosotros; el JID propio lo pone el ingestor
    else:
        sender = participant or chat_jid

    sender_lid = sender if sender and sender.endswith("@lid") else None
    sender_jid = sender if sender and not sender.endswith("@lid") else None

    if info.verifiedBizName:
        metadata["verified_business_name"] = info.verifiedBizName
    if info.broadcast:
        metadata["broadcast"] = True

    return ParsedMessage(
        chat_jid=chat_jid,
        timestamp=int(info.messageTimestamp),
        from_me=bool(info.key.fromMe),
        whatsapp_message_id=message_id,
        sender_jid=sender_jid,
        sender_lid=sender_lid,
        message_type=message_type,
        text=text,
        push_name=info.pushName or None,
        raw_proto=raw,
        media=media,
        metadata=metadata or {},
    )


def classify_chat(jid: str) -> str:
    """Tipo de chat a partir del sufijo del JID. Sin conversiones."""
    if jid.endswith("@g.us"):
        return "group"
    if jid.endswith("@broadcast"):
        return "broadcast"
    if jid.endswith("@newsletter"):
        return "newsletter"
    if jid.endswith("@s.whatsapp.net") or jid.endswith("@lid") or jid.endswith("@c.us"):
        return "individual"
    return "unknown"
