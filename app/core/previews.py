"""Vista previa de un mensaje: una sola definicion para toda la aplicacion.

Antes el sidebar construia la previa en SQL con ``'[' || message_type || ']'``,
asi que un mensaje que el parser no habia sabido normalizar aparecia como
``[unknown]`` y un ``deviceSentMessage`` con una foto tambien. Es informacion
que la aplicacion SI tiene: el tipo esta en la fila y el evento de sistema se
puede leer del protobuf.

Regla: si el clasificador conoce el tipo, la previa lo dice. ``[unknown]`` solo
queda para lo que de verdad no se ha podido determinar (seccion 33).
"""

from __future__ import annotations

# Etiqueta por tipo normalizado. El icono va delante porque en una lista
# estrecha se distingue mejor un simbolo que una palabra.
TYPE_PREVIEWS: dict[str, str] = {
    "image": "📷 Imagen",
    "video": "🎥 Video",
    "gif": "🎥 GIF",
    "audio": "🔊 Audio",
    "voice_note": "🎤 Nota de voz",
    "sticker": "Sticker",
    "document": "📄 Documento",
    "location": "📍 Ubicacion",
    "contact": "👤 Contacto",
    "poll": "📊 Encuesta",
    "call": "📞 Llamada",
    "reaction": "Reaccion",
    "edited": "Mensaje editado",
    "device_sent": "Mensaje propio",
    "album": "📷 Album",
}

# Etiqueta de ultimo recurso. NO se usa para nada que el clasificador conozca.
FALLBACK_PREVIEW = "Mensaje"
UNKNOWN_PREVIEW = "Mensaje sin interpretar"

PREVIEW_MAX = 70


def preview_for(
    message_type: str,
    text: str | None = None,
    *,
    raw_proto: bytes | None = None,
    metadata: dict | None = None,
) -> str:
    """Texto corto que representa el mensaje en el sidebar.

    El texto manda cuando existe: el pie de foto de una imagen es mas util
    que la palabra "Imagen". Cuando no hay texto se usa el tipo, y para los
    eventos de sistema se pregunta al clasificador, que sabe leer el
    ``messageStubType``.
    """
    if text:
        limpio = " ".join(text.split())
        if limpio:
            return limpio[:PREVIEW_MAX]

    if message_type == "system":
        from app.core.system_message import describe_system_message

        evento = describe_system_message(raw_proto, metadata)
        return evento.display

    etiqueta = TYPE_PREVIEWS.get(message_type)
    if etiqueta is not None:
        return etiqueta
    if message_type in ("unknown", "", None):
        return UNKNOWN_PREVIEW
    if message_type == "protocol":
        # No deberia llegar al sidebar (es interno), pero si llega se dice lo
        # que es en lugar de fingir que era un mensaje.
        return "Mensaje de protocolo"
    return FALLBACK_PREVIEW


def bubble_placeholder(message_type: str) -> str:
    """Texto de la burbuja cuando el mensaje no tiene ni texto ni adjunto."""
    etiqueta = TYPE_PREVIEWS.get(message_type)
    if etiqueta is not None:
        return etiqueta
    if message_type == "unknown":
        return "Mensaje no compatible con esta version"
    return FALLBACK_PREVIEW
