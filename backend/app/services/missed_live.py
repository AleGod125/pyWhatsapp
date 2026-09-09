"""Un mensaje en vivo que no se pudo descifrar deja un agujero. Se cierra.

EL CASO, MEDIDO
---------------
Un dispositivo de un contacto (``206566***:44@lid``) lleva desde el 3 de
septiembre mandando ``PreKeySignalMessage`` con la misma base key y la misma
clave de un solo uso, la ``17``, consumida hace dias. Sin la parte privada de
esa clave el X3DH **no se puede completar**: no falta codigo, falta un secreto
que ya no existe. Son 89 mensajes indescifrables.

De esos 89, 18 acabaron apareciendo igual: 13 por un reenvio que si se
descifro y 5 por la excavacion. Los otros 71 no van a llegar solos.

POR QUE NO SE ARREGLA REABRIENDO EL HISTORIAL
---------------------------------------------
La tentacion es poner el chat de ``exhausted`` a ``pending`` y que el motor
vuelva a pedir. No sirve: ``ON_DEMAND`` excava **hacia atras** desde el ancla
mas antigua, y el servidor ya contesto que por abajo no queda nada. Volveria a
contestar lo mismo, y de paso se habria borrado un veredicto correcto.

Lo que falta esta **por arriba**, en el borde reciente. Para eso ya existe el
motor --``rellenar_borde_reciente``--, que parte de una referencia NUEVA y baja
hasta empalmar con lo guardado. Lo que no existia era quien le dijera que hay
un agujero: la deteccion solo miraba el indice de WhatsApp Web, y con el
segundo dispositivo apagado no hay indice que mirar.

Un fallo de descifrado es mejor evidencia que el indice de Web. No es una
sospecha por comparar fechas: **sabemos** que llego un mensaje concreto, con su
identificador y su hora, y que no se pudo leer.

LO QUE NO SE HACE
-----------------
* No se toca Signal. Esto ocurre despues, con lo que el receptor ya reporto.
* No se reabre ningun ``exhausted``: el veredicto del historial antiguo sigue
  siendo cierto y es otro frente.
* No se fabrica ninguna referencia. El ancla del hueco es un mensaje real ya
  guardado, con su WAMID de verdad.
* No se acepta ni un mensaje sin autenticar: lo indescifrable sigue
  indescifrable, y lo que se recupera viene por el historial, firmado.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("PLAN_E")

#: Motivos de fallo que dejan un agujero que NO se va a cerrar solo.
#:
#: Una clave de un solo uso consumida no vuelve: el emisor puede reenviar mil
#: veces que el resultado sera el mismo. Un fallo de MAC queda fuera a
#: proposito --ahi la sesion existe y el reenvio si puede salir bien-- y
#: tambien "no session", que es justo el caso que el acuse con material
#: publico resuelve.
MOTIVOS_SIN_REMEDIO = ("unknown one-time pre-key",)

#: Cuanto se recuerda un agujero sin cerrar. Un dia: pasado ese tiempo, o se
#: cerro o el mensaje ya no esta en el borde reciente.
VIGENCIA_SEGUNDOS = 86_400.0

#: Tope de conversaciones anotadas, para no crecer sin fin.
MAXIMO_DE_CHATS = 200


@dataclass
class Agujero:
    """Lo que se sabe de los mensajes perdidos de una conversacion."""

    chat_jid: str
    #: Una huella por mensaje que no se pudo leer.
    #:
    #: NO son WAMID: en la capa donde se detecta el fallo el identificador de
    #: WhatsApp todavia no se conoce --va dentro de lo cifrado--. Es una huella
    #: del texto cifrado, que sirve para lo unico que hace falta aqui: no
    #: contar dos veces el mismo mensaje cuando el emisor lo reenvia.
    perdidos: set[str] = field(default_factory=set)
    primero: float = field(default_factory=time.time)
    ultimo: float = field(default_factory=time.time)
    #: Veces que se ha intentado cerrar. Evita insistir para siempre.
    intentos: int = 0

    @property
    def cuantos(self) -> int:
        return len(self.perdidos)


def chat_del_remitente(sender: Any) -> str | None:
    """La conversacion a la que pertenece un remitente. ``None`` si no se sabe.

    Un dispositivo se escribe ``usuario.44@lid`` o ``usuario:44@lid``, y la
    conversacion es del usuario, no del dispositivo: los mensajes de los
    dispositivos 0, 14 y 44 del mismo contacto son la misma conversacion.

    En un grupo el remitente es un participante y la conversacion es el grupo,
    que no se puede deducir de aqui. Se devuelve ``None`` antes que acertar por
    casualidad: anotar el agujero en la conversacion equivocada seria pedir
    historial de quien no lo perdio.
    """
    texto = str(sender or "").strip()
    if not texto or "@" not in texto:
        return None
    usuario, _, servidor = texto.partition("@")
    if servidor == "g.us":
        return None
    for separador in (".", ":"):
        usuario = usuario.split(separador)[0]
    return f"{usuario}@{servidor}" if usuario else None


def sin_remedio(motivo: Any) -> bool:
    """Si ese fallo describe un agujero que el reenvio NO va a cerrar."""
    texto = str(motivo or "").lower()
    return any(m in texto for m in MOTIVOS_SIN_REMEDIO)


class AgujerosEnVivo:
    """Que conversaciones perdieron mensajes en vivo, y cuales.

    Vive en memoria a proposito: es informacion del borde reciente, se cierra
    en minutos y no merece una tabla. Si el proceso se reinicia antes de
    cerrarla, el agujero se volvera a detectar en cuanto el emisor insista --y
    con una clave consumida, insiste siempre.
    """

    def __init__(self, *, vigencia: float = VIGENCIA_SEGUNDOS) -> None:
        self._agujeros: dict[str, Agujero] = {}
        self._candado = threading.Lock()
        self._vigencia = vigencia

    def anotar(self, sender: Any, huella: Any, motivo: Any) -> str | None:
        """Anota un mensaje perdido. Devuelve la conversacion, o ``None``.

        Solo cuenta lo que no tiene arreglo por reenvio: un fallo pasajero se
        resuelve solo, y anotarlo pediria historial sin motivo.
        """
        if not huella or not sin_remedio(motivo):
            return None
        chat_jid = chat_del_remitente(sender)
        if chat_jid is None:
            return None

        with self._candado:
            self._podar()
            agujero = self._agujeros.get(chat_jid)
            if agujero is None:
                if len(self._agujeros) >= MAXIMO_DE_CHATS:
                    self._agujeros.pop(next(iter(self._agujeros)))
                agujero = Agujero(chat_jid=chat_jid)
                self._agujeros[chat_jid] = agujero
            nuevo = str(huella) not in agujero.perdidos
            agujero.perdidos.add(str(huella))
            agujero.ultimo = time.time()

        if nuevo:
            log.debug(
                "[LIVE] agujero en %s: %s no se pudo descifrar (%s)",
                chat_jid,
                str(huella)[:12],
                str(motivo)[:60],
            )
        return chat_jid

    def cerrado(self, chat_jid: str) -> None:
        """El agujero se cerro: se olvida."""
        with self._candado:
            self._agujeros.pop(chat_jid, None)

    def intento(self, chat_jid: str) -> int:
        """Anota que se ha intentado cerrar, y devuelve cuantas van."""
        with self._candado:
            agujero = self._agujeros.get(chat_jid)
            if agujero is None:
                return 0
            agujero.intentos += 1
            return agujero.intentos

    def pendientes(self) -> list[Agujero]:
        """Los agujeros vigentes, del mas antiguo al mas reciente."""
        with self._candado:
            self._podar()
            return sorted(self._agujeros.values(), key=lambda a: a.primero)

    def resumen(self) -> dict[str, Any]:
        pendientes = self.pendientes()
        return {
            "chats": len(pendientes),
            "messages": sum(a.cuantos for a in pendientes),
        }

    def _podar(self) -> None:
        """Quita lo caducado. Se llama con el candado tomado."""
        limite = time.time() - self._vigencia
        for jid in [j for j, a in self._agujeros.items() if a.ultimo < limite]:
            self._agujeros.pop(jid, None)


# ---------------------------------------------------------------------------
# El registro compartido
# ---------------------------------------------------------------------------
#
# Uno por proceso. Lo alimenta la capa que ve el fallo --que es la unica que
# tiene delante el remitente y el motivo-- y lo consulta quien cierra los
# agujeros. Un singleton evita pasar el objeto por seis capas que no lo usan
# para nada mas.

_registro = AgujerosEnVivo()


def registro() -> AgujerosEnVivo:
    return _registro


def anotar_perdido(sender: Any, huella: Any, motivo: Any) -> str | None:
    """Atajo para quien ve el fallo. Nunca lanza."""
    try:
        return _registro.anotar(sender, huella, motivo)
    except Exception:  # noqa: BLE001 - anotar no puede tumbar la recepcion
        log.debug("No se pudo anotar el mensaje perdido", exc_info=True)
        return None


def olvidar_todo() -> None:
    """Para las pruebas y para un re-emparejamiento."""
    _registro._agujeros.clear()
