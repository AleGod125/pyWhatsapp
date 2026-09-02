"""Modo ``peer`` para las stanzas de mensaje.

CARENCIA VERIFICADA (pywhats 0.2.0, ``messaging/sender.py:849-856``).
``Sender._build_message_node`` construye la stanza con exactamente tres
atributos::

    attrs = {"id": message_id, "to": to, "type": message_type or ...}

No existe ``category``. whatsmeow, en cambio, envia las peticiones de
Peer Data Operation con ``category="peer"`` (``sendPeerMessage``): es lo que
le dice al servidor que la stanza no es un mensaje de chat sino una operacion
entre dispositivos de la MISMA cuenta.

EVIDENCIA DE QUE HACE FALTA
---------------------------
Con la stanza normal, las peticiones HISTORY_SYNC_ON_DEMAND salen y el
servidor las confirma, pero nunca llega respuesta::

    sender: sent id=79338F7C7041F904
    sender: ack  id=79338F7C7041F904
    [BACKFILL] ronda 1: respuesta=no nuevos=0

8 peticiones -> 6 timeouts -> 0 mensajes. El ACK solo confirma que la stanza
llego; sin ``category="peer"`` el servidor no la encamina como operacion entre
pares y la descarta en silencio.

El parche es deliberadamente estrecho: solo anade el atributo, y solo mientras
un envio este marcado explicitamente como peer. Los mensajes normales salen
exactamente igual que antes.
"""

from __future__ import annotations

import contextlib
from typing import Any, Iterator

from app.logging_setup import get_logger

log = get_logger("COMPAT")

_MARKER = "_whatsapp_backup_peer_patch"
_FLAG = "_whatsapp_backup_peer_mode"


@contextlib.contextmanager
def peer_mode(sender: Any) -> Iterator[None]:
    """Marca los envios de este bloque como ``category="peer"``.

    La bandera vive en la instancia del Sender y se limpia al salir, incluso
    si el envio falla: un mensaje normal posterior no debe heredarla.
    """
    previous = getattr(sender, _FLAG, False)
    setattr(sender, _FLAG, True)
    try:
        yield
    finally:
        setattr(sender, _FLAG, previous)


class PeerShapeError(RuntimeError):
    """La stanza peer no tiene la forma que el telefono espera."""


def _collect_encs(node: Any) -> list[Any]:
    """Todos los ``<enc>`` que cuelgan de ``<participants>/<to>``.

    Debe haber exactamente uno en una operacion peer. Mas de uno significa
    que el destino se abrio en fanout a varios dispositivos.
    """
    participants = None
    for child in node.content or []:
        if getattr(child, "tag", None) == "participants":
            participants = child
            break
    if participants is None:
        return []

    return [
        grandchild
        for to_node in (participants.content or [])
        for grandchild in (getattr(to_node, "content", None) or [])
        if getattr(grandchild, "tag", None) == "enc"
    ]


def apply() -> bool:
    """Envuelve el Sender para producir el wire shape de una peer. Idempotente.

    Son TRES ajustes, y los tres hacen falta:

    1. ``category="peer"`` + ``type="text"`` en la stanza.
    2. Un solo destinatario. ``Sender._target_devices`` (sender.py:725) hace::

           if chat.device:
               return [chat]

       y ``device=0`` es FALSY en Python, asi que apuntar al dispositivo
       principal caia en el camino del fanout: usync devolvia todos los
       dispositivos de la cuenta (0, 75, 76...) y la stanza salia con varios
       ``<enc>``. Ese es el origen del aviso "Stanza peer sin un <enc> unico".
    3. Sin envoltorio ``DeviceSentMessage``. pywhats envuelve la copia que va
       a los dispositivos propios (``_build_dsm_plaintext``) porque asi es
       como se renderiza un mensaje de chat enviado desde otro dispositivo.
       Una Peer Data Operation no es un mensaje de chat: whatsmeow envia el
       ``Message`` desnudo, y envuelto el telefono no reconoce la operacion.
    """
    from pywhats.messaging.sender import Sender

    original = Sender._build_message_node
    if getattr(original, _MARKER, False):
        return True

    # -- 2. Un unico destinatario ------------------------------------------
    original_targets = Sender._target_devices

    async def _target_devices(self: Any, chat: Any) -> list[Any]:
        if getattr(self, _FLAG, False):
            # Exactamente el dispositivo indicado, sin usync ni fanout.
            log.debug("Peer: destino unico, sin fanout")
            return [chat]
        return await original_targets(self, chat)

    Sender._target_devices = _target_devices  # type: ignore[method-assign]

    # -- 3. Sin envoltorio DeviceSentMessage -------------------------------
    original_dsm = Sender._build_dsm_plaintext

    def _build_dsm_plaintext(self: Any, chat: Any, message_proto: Any) -> bytes:
        if getattr(self, _FLAG, False):
            from pywhats.messaging.padding import pad_random_max16

            return pad_random_max16(message_proto.SerializeToString())
        return original_dsm(self, chat, message_proto)

    Sender._build_dsm_plaintext = _build_dsm_plaintext  # type: ignore[method-assign]

    def _build_message_node(self: Any, **kwargs: Any) -> Any:
        node = original(self, **kwargs)
        if not getattr(self, _FLAG, False):
            return node

        # Node es un dataclass mutable: se ajusta en sitio.
        node.attrs["category"] = "peer"
        # whatsmeow envia estas stanzas siempre como "text".
        node.attrs["type"] = "text"

        encs = _collect_encs(node)
        if len(encs) != 1:
            # NO se degrada en silencio a la estructura normal. Esa fue
            # justamente la causa del fallo anterior: el servidor aceptaba la
            # stanza con un ACK y el telefono no reconocia la operacion, de
            # modo que el sintoma era un timeout sin ninguna pista.
            raise PeerShapeError(
                f"la stanza peer tiene {len(encs)} nodos <enc> y debe tener 1. "
                f"El destino se resolvio a varios dispositivos: revisa que "
                f"_target_devices este parcheado para el modo peer."
            )

        enc = encs[0]
        log.info(
            "Peer shape=bare enc_count=1 enc_type=%s category=peer type=text",
            enc.attrs.get("type"),
        )

        # Estructura peer: el <enc> cuelga DIRECTAMENTE de <message>. Sin
        # <participants> (no hay fanout: la peticion es para un solo
        # dispositivo) y sin <device-identity>.
        node.content = [enc]
        log.debug("Stanza peer reestructurada: <message category=peer><enc/></message>")
        return node

    setattr(_build_message_node, _MARKER, True)
    Sender._build_message_node = _build_message_node  # type: ignore[method-assign]

    log.info("Adaptacion de mensajes peer (category=peer) aplicada")
    return True
