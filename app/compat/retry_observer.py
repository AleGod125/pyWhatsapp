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

import time
from dataclasses import dataclass
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("SIGNAL")

_MARCA = "_whatsapp_backup_retry_observer"

#: El seguimiento al que se le cuenta. Lo fija ``apply``.
_tracker: Any = None

#: Los ajustes, para poder leer el material publico del acuse con claves.
_settings: Any = None

#: Dos acuses del MISMO mensaje separados por menos de esto son el mismo
#: acuse mandado dos veces. Se midio: el mismo WAMID recibio dos en 253 ms,
#: y el segundo no aporta nada -- el emisor todavia no ha tenido tiempo ni de
#: leer el primero.
VENTANA_ANTIREBOTE = 3.0


@dataclass
class _EstadoDeReintento:
    """Lo que llevamos hecho por un mensaje concreto."""

    intentos: int = 0
    ultimo_envio: float = 0.0
    claves_enviadas: bool = False


#: Por WAMID. Se poda solo para no crecer sin fin.
_estados: dict[str, _EstadoDeReintento] = {}
MAXIMO_DE_ESTADOS = 500


def reiniciar() -> None:
    """Olvida lo contado. Para las pruebas y para un re-emparejamiento."""
    _estados.clear()


def _estado_de(wamid: str) -> _EstadoDeReintento:
    estado = _estados.get(wamid)
    if estado is None:
        if len(_estados) >= MAXIMO_DE_ESTADOS:
            _estados.pop(next(iter(_estados)))
        estado = _EstadoDeReintento()
        _estados[wamid] = estado
    return estado


def _no_vale_la_pena_insistir(sender: Any) -> bool:
    """Si ya se pidio de sobra por un establecimiento que no puede completarse.

    Ante la duda se contesta ``False``: dejar de mandar acuses que si servian
    seria peor que mandar alguno de mas.
    """
    try:
        from pywhats.messaging.addressing import session_id

        from app.compat.prekey_compat import insistir_es_inutil

        return insistir_es_inutil(session_id(sender))
    except Exception:  # noqa: BLE001 - no saberlo no puede cortar el acuse
        return False


def _sin_sesion_con(receptor: Any, sender: Any) -> bool:
    """Si no existe NINGUN registro de sesion con ese remitente.

    Se mira el almacen, que es donde esta el hecho. No se deduce del texto del
    error ni se toca nada: es una lectura.

    Ante la duda se contesta ``False``, que deja la regla de siempre. Adjuntar
    material publico de mas gasta una clave de un solo uso; no adjuntarlo
    cuando hacia falta solo repite el acuse simple del intento siguiente.
    """
    try:
        from pywhats.messaging.addressing import session_id

        return receptor._sessions.load(session_id(sender)) is None
    except Exception:  # noqa: BLE001 - no poder mirarlo no cambia el acuse
        return False


def apply(tracker: Any, settings: Any = None) -> bool:
    """Instala la observación y la corrección del contador. Idempotente."""
    global _tracker, _settings
    _tracker = tracker
    if settings is not None:
        _settings = settings

    import pywhats.messaging.receiver as receiver_module

    original = receiver_module.Receiver._send_retry_receipt
    if getattr(original, _MARCA, False):
        return True

    async def _send_retry_receipt(self, node, *, sender):  # type: ignore[no-untyped-def]
        wamid = node.get_str("id")
        if not wamid:
            return None

        estado = _estado_de(wamid)
        ahora = time.monotonic()

        # Un establecimiento que no puede completarse no se arregla pidiendo.
        #
        # Si el emisor insiste con una clave de un solo uso ya consumida, su
        # parte privada no existe y el X3DH no se puede derivar. Se midio: 69
        # acuses al mismo dispositivo, 17 con material publico, y la base key
        # nunca cambio. Cada acuse con material gasta una clave de un solo
        # uso, asi que a partir de cierto punto insistir solo cuesta.
        #
        # Se sigue intentando descifrar cada mensaje: si el emisor rehace el
        # saludo se vera al momento, porque la base key sera otra y el
        # recuento empieza de cero.
        if _no_vale_la_pena_insistir(sender):
            log.debug(
                "[SIGNAL] acuse de reintento omitido id=%s: el emisor insiste "
                "con una clave de un solo uso ya consumida",
                wamid[:8],
            )
            return None

        # UN acuse por mensaje a la vez. Se midio el mismo WAMID recibiendo
        # dos en 253 ms; el segundo no aporta nada, porque el emisor todavia
        # no ha tenido tiempo ni de leer el primero.
        if estado.ultimo_envio and (ahora - estado.ultimo_envio) < VENTANA_ANTIREBOTE:
            log.debug(
                "[SIGNAL] acuse de reintento omitido id=%s: ya se mando hace %.0f ms",
                wamid[:8],
                (ahora - estado.ultimo_envio) * 1000,
            )
            return None

        intentos = 0
        try:
            if _tracker is not None:
                intentos = _tracker.intentos_de(wamid)
        except Exception:  # noqa: BLE001 - no saberlo no puede cortar el acuse
            intentos = 0
        intentos = max(intentos, estado.intentos + 1)
        estado.intentos = intentos

        # A partir del segundo intento se adjunta el material publico, que es
        # lo que hacen Baileys y whatsmeow: si el acuse simple no basto, el
        # emisor necesita con que rehacer el saludo.
        from app.compat.retry_keys import INTENTO_DESDE_EL_QUE_SE_ADJUNTA

        con_claves = intentos >= INTENTO_DESDE_EL_QUE_SE_ADJUNTA

        # ... salvo cuando NO HAY SESION NINGUNA con ese remitente.
        #
        # EL BLOQUEO, MEDIDO
        # ------------------
        # Esperar al segundo intento supone que habra un segundo intento. Para
        # las copias de nuestro propio telefono no lo hay: el telefono manda
        # la copia UNA vez, no la descifra nadie, y como nunca vuelve a
        # mandarla el contador se queda clavado en 1 para siempre.
        #
        # En el registro local: 303 acuses a nuestro propio LID, los 303 de
        # 100 bytes --el acuse simple-- y ni uno con material. El telefono los
        # confirma con un ack y no reenvia nada, porque sin nuestras claves
        # publicas no tiene con que rehacer el saludo.
        #
        # La razon de esperar era no regalar una clave de un solo uso en cada
        # mensaje que llegue desordenado. Ese razonamiento vale cuando hay una
        # sesion: el mensaje puede venir fuera de orden. Sin sesion no hay
        # nada con lo que estar desordenado, y el material publico es
        # justamente lo unico que puede desatascarlo.
        #
        # OJO: esto NO toca el fallo de MAC. Un MAC que no cuadra significa
        # que la sesion EXISTE y esta desincronizada, no que falte; ese caso
        # sigue la regla de siempre y el mensaje sigue sin aceptarse.
        if not con_claves and _sin_sesion_con(self, sender):
            con_claves = True
            log.debug(
                "[SIGNAL] acuse id=%s: no hay sesion con el remitente, "
                "se adjunta el material publico ya en el primer intento",
                wamid[:8],
            )

        if intentos > 1 or con_claves:
            enviado = await _enviar_con_contador(
                self, node, sender, intentos, con_claves=con_claves
            )
            if enviado:
                estado.claves_enviadas = estado.claves_enviadas or con_claves
            else:
                # Si por lo que sea no se pudo, se manda el de siempre: es
                # mejor un acuse con el contador mal que ningún acuse.
                await original(self, node, sender=sender)
        else:
            await original(self, node, sender=sender)

        estado.ultimo_envio = time.monotonic()

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


async def _enviar_con_contador(
    receptor: Any, node: Any, sender: Any, intentos: int, *, con_claves: bool = False
) -> bool:
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

        # El material publico, sólo si toca. Si no se puede reunir, se manda
        # el acuse sin él: un acuse sin claves sigue siendo mejor que ninguno.
        claves_puestas = False
        material = None
        if con_claves and _settings is not None:
            from app.compat.retry_keys import bloque_de_claves, material_de

            material = material_de(_settings)
            bloque = bloque_de_claves(material) if material is not None else None
            if bloque is not None:
                hijos.append(bloque)
                claves_puestas = True

        atributos: dict[str, Any] = {"id": wamid, "type": "retry", "to": sender}
        # Los atributos de ENRUTADO se copian tal cual venian.
        #
        # `participant` ya se copiaba: en un grupo el emisor real va ahi y sin
        # el el servidor no sabe a quien pedirle el reenvio.
        #
        # `recipient` NO se copiaba, y es el que falta para una copia del
        # telefono propio. Cuando el mensaje lo escribes tu, la stanza va de
        # tu cuenta a tu cuenta y quien distingue de que conversacion se trata
        # es ese atributo. El propio pywhats ya lo reconoce como atributo de
        # enrutado --`_build_ack_node` copia `participant`, `recipient` y
        # `type` al construir el ack-- pero su acuse de reintento se dejaba
        # `recipient` fuera. Esa asimetria es la unica diferencia estructural
        # que se ha encontrado entre lo que mandamos y lo que manda un cliente
        # moderno.
        #
        # Es una copia condicional: si el atributo no viene, no se anade nada
        # y la stanza queda EXACTAMENTE como estaba. Para un contacto normal
        # --el camino que hoy funciona-- no cambia ni un byte.
        for atributo in ("participant", "recipient"):
            if atributo in node.attrs:
                atributos[atributo] = node.attrs[atributo]

        acuse = Node(tag="receipt", attrs=atributos, content=hijos)
        await receptor._transport.send(encode(acuse))

        # Y se deja constancia de lo que salio, para poder correlacionar si el
        # telefono contesta. Es diagnostico: no cambia ninguna decision.
        try:
            from app.compat.own_retry_trace import anotar_envio

            anotar_envio(
                wamid=wamid,
                destino=sender,
                atributos_del_mensaje=node.attrs,
                atributos_del_acuse=atributos,
                intentos=intentos,
                material=material if con_claves else None,
            )
        except Exception:  # noqa: BLE001 - el diagnostico no puede cortar nada
            log.debug("No se pudo anotar el acuse", exc_info=True)
    except Exception:  # noqa: BLE001 - que lo mande el original
        log.debug("Fallo enviando el acuse con contador real", exc_info=True)
        return False

    if claves_puestas:
        log.info(
            "[OWN_LIVE] reestableciendo sesion segura con el telefono "
            "(acuse id=%s intento=%d, con material publico)",
            wamid[:8],
            intentos,
        )
    else:
        log.info(
            "[SIGNAL] acuse de reintento id=%s intento=%d",
            wamid[:8],
            intentos,
        )
    return True
