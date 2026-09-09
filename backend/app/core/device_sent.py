"""A que conversacion pertenece un mensaje que YO envie desde el telefono.

EL PROBLEMA, MEDIDO
-------------------
Cuando el usuario escribe desde su movil, WhatsApp reparte una copia a cada
dispositivo vinculado. Esa copia llega como un stanza cuyo remitente es
NUESTRO PROPIO dispositivo, asi que pywhats la archiva en el chat con uno
mismo (``receiver.py:394``: ``chat_jid = sender_jid  # 1-1 chat.``) y la marca
como recibida (``from_me=False``, fijo en las lineas 512 y 587).

Resultado real, con los bytes ya descifrados delante::

    wamid=AC4E7F161E3AF3887A83...
      chat_jid guardado = 86531142340710@lid    <- yo mismo
      from_me guardado  = False
      top-level fields  = [(31, device_sent_message), (35, ?)]
        destination_jid = 64940106866902@lid    <- Isaac
        inner message   = [(6, extended_text_message)]

Tres mensajes para Isaac acabaron en el chat personal. El destinatario estaba
escrito en el protobuf todo el rato.

LA FORMA REAL
-------------
El campo 31 de ``Message`` es ``DeviceSentMessage``, y el descriptor del
paquete instalado dice exactamente esto::

    31 device_sent_message  ->  1 destination_jid
                                2 message

No se deduce ni se adivina nada: si el campo 31 esta, el mensaje lo envie yo y
la conversacion es ``destination_jid``. Si no esta, es un mensaje entrante
normal y no se toca nada.

EL AUTO-MENSAJE SIGUE SIENDO AUTO-MENSAJE
-----------------------------------------
En la misma tanda medida hay una imagen con
``destination_jid = 86531142340710@lid``, que es la identidad propia: es una
nota para uno mismo y su sitio ES el chat personal. La regla lo respeta sola,
porque no dice "todo lo saliente fuera de aqui" sino "al destino que declara
el protobuf". Ese destino, a veces, soy yo.

LO QUE NO SE HACE
-----------------
No se toca Signal. Esto ocurre DESPUES del descifrado, sobre bytes que ya
estan en claro y que ya guardabamos en ``raw_proto``. No se lee ni una clave.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.message_parser import top_level_fields

# Numeros de campo verificados contra el descriptor de pywhats, no copiados
# de ninguna documentacion: ``Message.DESCRIPTOR.fields_by_number[31]`` es
# ``pywhats.proto.DeviceSentMessage`` con ``1 destination_jid`` y ``2 message``.
CAMPO_DEVICE_SENT = 31
CAMPO_DESTINO = 1
CAMPO_MENSAJE_INTERNO = 2


@dataclass(frozen=True)
class Routed:
    """A donde va un mensaje, y por que se sabe.

    ``es_saliente`` distingue "lo envie yo" de "me lo enviaron". No sale de un
    campo booleano del evento (pywhats lo deja fijo en ``False``) sino de la
    presencia del envoltorio, que es la prueba protocolar de autoria.
    """

    chat_jid: str
    es_saliente: bool
    destino: str | None = None
    interno: bytes | None = None
    es_auto_mensaje: bool = False

    @property
    def reenrutado(self) -> bool:
        """Si la conversacion difiere de la que dedujo el receptor."""
        return self.es_saliente and not self.es_auto_mensaje


def unwrap(raw_proto: bytes | None) -> tuple[str, bytes] | None:
    """``(destination_jid, mensaje_interno)`` si hay ``DeviceSentMessage``.

    ``None`` cuando no lo hay, que es el caso de todo mensaje entrante. Nunca
    lanza: un protobuf que no se deje leer no puede tumbar la recepcion, y
    devolver ``None`` deja el comportamiento anterior intacto.
    """
    if not raw_proto:
        return None
    try:
        campos = top_level_fields(raw_proto)
        envoltorio = next(
            (p for numero, _w, p in campos if numero == CAMPO_DEVICE_SENT), None
        )
        if envoltorio is None:
            return None

        interiores = top_level_fields(envoltorio)
        destino_bytes = next(
            (p for numero, _w, p in interiores if numero == CAMPO_DESTINO), None
        )
        interno = next(
            (p for numero, _w, p in interiores if numero == CAMPO_MENSAJE_INTERNO), None
        )
        if not destino_bytes:
            # Sin destino declarado no hay nada que reenrutar: se prefiere
            # dejarlo donde estaba a inventarse una conversacion.
            return None
        return destino_bytes.decode("utf-8", "replace"), (interno or b"")
    except Exception:  # noqa: BLE001 - la recepcion es lo prioritario
        return None


def route(
    raw_proto: bytes | None,
    *,
    chat_jid: str,
    own_identifiers: frozenset[str] | set[str] = frozenset(),
) -> Routed:
    """Decide conversacion y autoria a partir del protobuf ya descifrado.

    ``chat_jid`` es lo que dedujo el receptor, y es lo que se devuelve tal
    cual para cualquier mensaje entrante: esta funcion solo puede mover un
    mensaje cuando el propio protobuf declara un destinatario.

    ``own_identifiers`` son el PN y el LID propios. Solo sirven para saber si
    un mensaje saliente iba dirigido a uno mismo; no intervienen en el
    enrutado, que lo manda el ``destination_jid``.
    """
    envoltorio = unwrap(raw_proto)
    if envoltorio is None:
        return Routed(chat_jid=chat_jid, es_saliente=False)

    destino, interno = envoltorio
    propios = {i for i in own_identifiers if i}
    es_auto = _mismo_usuario(destino, propios)
    return Routed(
        chat_jid=destino,
        es_saliente=True,
        destino=destino,
        interno=interno or None,
        es_auto_mensaje=es_auto,
    )


def _usuario(jid: str) -> str:
    """Parte de usuario, sin sufijo de dispositivo.

    ``64940106866902.3@lid`` y ``64940106866902@lid`` son la misma persona en
    dos dispositivos suyos; compararlos como cadenas daria que no.
    """
    return jid.split("@")[0].split(":")[0].split(".")[0]


def _mismo_usuario(jid: str, otros: set[str]) -> bool:
    usuario = _usuario(jid)
    return any(usuario == _usuario(otro) for otro in otros)
