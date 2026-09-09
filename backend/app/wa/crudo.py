"""El protobuf del ultimo mensaje recibido, para quien lo necesite despues.

PARA QUE SIRVE
--------------
``live_service`` clasifica el mensaje por su protobuf real, no por lo que diga
el proveedor: es asi como se detectan los adjuntos que van dentro de un
``deviceSentMessage`` --las fotos que uno se envia a si mismo-- y como se
guarda ``raw_proto`` de lo que llega en vivo.

POR QUE UN HUECO GLOBAL Y NO UN PARAMETRO
------------------------------------------
Con pywhats no habia alternativa: la libreria no entregaba esos bytes por
ningun sitio, asi que un parche los capturaba al vuelo y los dejaba aqui. Con
Baileys llegan de serie con cada mensaje, pero el hueco se conserva para que
``live_service`` no cambie ni una linea.

Hay un unico receptor por proceso y entre que se anota y se lee no hay ningun
``await``, asi que no puede intercalarse otro mensaje.
"""

from __future__ import annotations

_ultimo: bytes | None = None


def registrar_crudo(datos: bytes | None) -> None:
    """Deja el protobuf del mensaje que se acaba de recibir."""
    global _ultimo
    _ultimo = datos


def last_raw_message() -> bytes | None:
    """Bytes del ultimo mensaje recibido, o ``None``."""
    return _ultimo
