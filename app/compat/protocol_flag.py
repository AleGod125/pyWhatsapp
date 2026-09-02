"""Expone si un mensaje live era, en realidad, control del protocolo.

EL PROBLEMA
-----------
pywhats emite los ProtocolMessage por el evento ``message`` a proposito. Su
propio docstring lo dice (``receiver.py:691``)::

    The message itself still flows on to the normal event + receipt path,
    as in whatsmeow.

El evento ``Message`` que llega es un dataclass ya normalizado (id, chat,
sender, text, timestamp, from_me, media) sin los bytes originales, asi que
desde fuera un Peer Data Operation es indistinguible de un mensaje vacio.
Resultado medido: 75 ecos de nuestras propias peticiones ON_DEMAND guardados
como mensajes del chat personal, todos con tipo "unknown".

LA SENAL
--------
``Receiver._handle_protocol_message(proto, sender)`` recibe el protobuf ya
parseado, y se invoca inmediatamente antes de construir el ``Message``. Entre
esas dos lineas no hay ningun ``await`` (receiver.py:487-516), asi que dentro
de la misma corrutina no puede intercalarse otro mensaje: apuntar el resultado
en la instancia es determinista.

Se registra ``proto.HasField("protocol_message")``, no el valor de retorno:
ese solo es True para las formas que pywhats sabe manejar, y un
PeerDataOperationRequest no es una de ellas.
"""

from __future__ import annotations

from typing import Any

from app.logging_setup import get_logger

log = get_logger("COMPAT")

_MARKER = "_whatsapp_backup_protocol_flag"

# Estado a nivel de modulo: hay un unico receiver por proceso y entre la
# anotacion y su lectura no hay ningun await, asi que no puede intercalarse
# otro mensaje. Evita tener que hacer llegar la instancia del receiver hasta
# la capa de persistencia.
_last_raw: bytes | None = None


def last_raw_message() -> bytes | None:
    """Bytes del ultimo ``Message`` E2E descifrado, o ``None``.

    Con ellos la capa de persistencia puede clasificar por protobuf real y,
    de paso, guardar ``raw_proto`` tambien para los mensajes en vivo.
    """
    return _last_raw


def apply() -> bool:
    """Instala la captura. Idempotente y sin cambiar comportamiento.

    El gancho es ``_extract_text`` y no ``_handle_protocol_message``, y la
    diferencia importa: el camino de los mensajes de GRUPO (``skmsg``,
    receiver.py:565-589) construye el ``Message`` sin pasar por
    ``_handle_protocol_message``. Con el gancho anterior esos mensajes
    dejaban ``_last_raw`` con el valor del mensaje ANTERIOR, asi que la
    clasificacion podia mirar el protobuf equivocado.

    ``_extract_text(proto)`` se invoca exactamente una vez por mensaje en
    AMBOS caminos (lineas 455 y 575), justo antes de construir el evento.
    """
    import pywhats.messaging.receiver as receiver_module

    original = receiver_module._extract_text
    if getattr(original, _MARKER, False):
        return True

    def _extract_text(proto: Any) -> str:
        global _last_raw
        try:
            _last_raw = proto.SerializeToString()
        except Exception:  # noqa: BLE001 - nunca romper la recepcion
            _last_raw = None
        return original(proto)

    setattr(_extract_text, _MARKER, True)
    receiver_module._extract_text = _extract_text

    log.info("Deteccion de mensajes de protocolo en vivo aplicada")
    return True
