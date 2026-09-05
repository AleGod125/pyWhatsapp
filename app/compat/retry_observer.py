"""El acuse de reintento: verlo salir, y decir la verdad en el contador.

QUE FALTABA AL PRINCIPIO
------------------------
El receptor pide el reenvío de lo que no pudo descifrar, y eso funciona. Lo
que no había era forma de saber si ese reenvío LLEGÓ. Sin eso, «8 fallos de
descifrado» no distingue entre «ocho mensajes que acabaron entrando» y «ocho
mensajes que faltan».

EL CONTADOR: AHORA SÍ HAY EVIDENCIA
-----------------------------------
``Receiver._send_retry_receipt`` escribe ``count="1"`` fijo, así que un mensaje
que falla tres veces pide tres reenvíos que dicen los tres «es la primera
vez». Baileys y whatsmeow mandan el contador real, y el emisor lo usa para
decidir si basta con reenviar o si hay que rehacer la sesión entera.

Esto quedó medido en su día y sin tocar, por falta de evidencia de que fuera
ESE campo. **Ahora la hay**, sobre la sesión real::

    reintentos=22  recuperados=0  sin_resolver=22
    reintentos=14  recuperados=0  sin_resolver=14
    reintentos=5   recuperados=0  sin_resolver=5

Cero de veintidós. El acuse sale, el servidor lo acepta —``ack->ok
class=receipt``— y no vuelve nada: ni reenvío ni ``pkmsg``. Con el contador
siempre en 1, el emisor nunca alcanza la condición que le haría rehacer la
sesión.

QUE SE CAMBIA, EXACTAMENTE
--------------------------
Una cosa: que el contador diga cuántas veces ha fallado ESE mensaje. El resto
de la stanza se construye igual que en pywhats —mismo identificador, misma
marca, mismo destinatario, mismo ``<registration>``, mismo ``participant``—,
porque esas partes el servidor ya las acepta.

No se toca ``site-packages``: se envuelve el método, como el resto de
adaptaciones de este paquete.

LO QUE SIGUE FALTANDO, Y SE DICE
--------------------------------
Baileys mete además un bloque ``<keys>`` con identidad, prekey firmada, OPK y
``device-identity``. pywhats no lo manda, y lo documenta. Nuestras prekeys SÍ
están subidas al servidor —``prekey: server holds 6 OPKs``—, así que el emisor
podría pedirlas. Si aun con el contador correcto no vuelve nada, ese bloque es
el siguiente sospechoso: es reimplementar protocolo y va en su propia fase.

LO QUE NO HACE
--------------
No descifra, no toca Signal, no cambia cuándo se envía el acuse y no acepta
nada sin autenticar.
"""

from __future__ import annotations

from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("SIGNAL")

_MARCA = "_whatsapp_backup_retry_observer"

#: El seguimiento al que se le cuenta. Lo fija ``apply``.
_tracker: Any = None


def apply(tracker: Any) -> bool:
    """Instala la observación y la corrección del contador. Idempotente."""
    global _tracker
    _tracker = tracker

    import pywhats.messaging.receiver as receiver_module

    original = receiver_module.Receiver._send_retry_receipt
    if getattr(original, _MARCA, False):
        return True

    async def _send_retry_receipt(self, node, *, sender):  # type: ignore[no-untyped-def]
        wamid = node.get_str("id")
        intentos = 0
        try:
            if _tracker is not None:
                intentos = _tracker.intentos_de(wamid)
        except Exception:  # noqa: BLE001 - no saberlo no puede cortar el acuse
            intentos = 0

        # El primer intento ya lo construye bien pywhats: sólo hace falta
        # tomar el relevo cuando el contador de verdad es mayor que uno.
        if intentos > 1:
            enviado = await _enviar_con_contador(self, node, sender, intentos)
            if not enviado:
                # Si por lo que sea no se pudo, se manda el de siempre: es
                # mejor un acuse con el contador mal que ningún acuse.
                await original(self, node, sender=sender)
        else:
            await original(self, node, sender=sender)

        try:
            if _tracker is not None:
                _tracker.acuse_enviado(wamid)
        except Exception:  # noqa: BLE001 - observar no puede romper la recepción
            pass
        return None

    setattr(_send_retry_receipt, _MARCA, True)
    receiver_module.Receiver._send_retry_receipt = _send_retry_receipt  # type: ignore[assignment]
    log.debug("Seguimiento de acuses de reintento activado (con contador real)")
    return True


async def _enviar_con_contador(receptor: Any, node: Any, sender: Any, intentos: int) -> bool:
    """El MISMO acuse que construye pywhats, con el contador de verdad.

    Se replica la forma entera a propósito, en vez de tocar sólo un atributo
    de algo ya enviado: la stanza se construye una vez y se manda una vez.

    Devuelve ``False`` si no se pudo construir; entonces el llamante manda el
    original y no se pierde el acuse.
    """
    try:
        from pywhats.binary.node import Node
        from pywhats.binary.encoder import encode
    except Exception:  # noqa: BLE001 - sin las piezas, que lo mande el original
        log.debug("No se pudo construir el acuse con contador real", exc_info=True)
        return False

    wamid = node.get_str("id")
    if not wamid:
        return False

    try:
        regid = getattr(getattr(receptor, "_identity", None), "registration_id", None)
        hijos = [
            Node(
                tag="retry",
                attrs={
                    # LA diferencia. Lo demás es idéntico.
                    "count": str(intentos),
                    "id": wamid,
                    "t": node.get_str("t") or "0",
                    "v": "1",
                },
            )
        ]
        if isinstance(regid, int):
            hijos.append(Node(tag="registration", content=int(regid).to_bytes(4, "big")))

        atributos: dict[str, Any] = {"id": wamid, "type": "retry", "to": sender}
        # En un grupo el emisor real va en `participant`, y sin él el servidor
        # no sabe a quién pedirle el reenvío.
        if "participant" in node.attrs:
            atributos["participant"] = node.attrs["participant"]

        acuse = Node(tag="receipt", attrs=atributos, content=hijos)
        await receptor._transport.send(encode(acuse))
    except Exception:  # noqa: BLE001 - que lo mande el original
        log.debug("Fallo enviando el acuse con contador real", exc_info=True)
        return False

    log.info(
        "[SIGNAL] acuse de reintento id=%s intento=%d (antes siempre decia 1)",
        wamid[:8],
        intentos,
    )
    return True
