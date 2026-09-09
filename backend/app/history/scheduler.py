"""Quién se recupera antes. Una sola cola con prioridades, y nada más.

EL PROBLEMA QUE RESUELVE
------------------------
La excavación recorría las conversaciones en el orden que devolvía la base
—por actividad, de más nueva a más vieja— y punto. Eso tiene una consecuencia
que se nota mucho al usar la aplicación: **abres una conversación y no pasa
nada**, porque la suya está en la posición treinta de una lista que se procesa
en orden y cada puesto puede costar hasta 45 segundos.

Aquí se decide el orden de otra manera: lo que el usuario está mirando va
primero. Siempre.

LAS PRIORIDADES, Y POR QUE ESAS
-------------------------------
``INTERACTIVA``  la conversación que el usuario tiene abierta. Es la única que
                 él está esperando de verdad, así que se salta la cola.
``RECIENTE``     conversaciones con actividad reciente: son las que va a abrir
                 a continuación, con más probabilidad que ninguna otra.
``FONDO``        el resto, con ancla utilizable.
``REINTENTO``    las que fallaron y ya cumplieron su espera. Van al final
                 porque ya se sabe que cuestan, y adelantarlas retrasaría a
                 conversaciones que sí van a responder.

Las que **esperan referencia no entran**. No es un descuido: sin ancla no se
puede ni formular la petición, así que ocupar un trabajador con ellas es
regalar un turno. Vuelven a la cola solas en cuanto aparece una referencia.

EL EQUILIBRIO, QUE ES LO QUE SE OLVIDA
--------------------------------------
Con prioridad estricta y un usuario navegando mucho, el fondo no se recupera
**nunca**. Por eso cada ``UNO_DE_FONDO_CADA`` turnos se cede uno al fondo
aunque haya trabajo interactivo esperando. Es una regla simple a propósito:
una que se pueda leer, contar y probar.

LO QUE ESTO NO HACE
-------------------
No pide historial, no habla con WhatsApp y no toca Signal. Decide un orden y
lo entrega. Quien pide sigue siendo el motor de siempre, con su semáforo, su
correlación y su guardia por conversación.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Iterable

from app.core.logging_setup import get_logger

log = get_logger("BACKFILL")


class Prioridad(IntEnum):
    """Menor número, antes se atiende."""

    INTERACTIVA = 0
    RECIENTE = 1
    FONDO = 2
    REINTENTO = 3


#: Cada cuántos turnos se cede uno al fondo aunque haya interactivas.
#:
#: Tres es un número escogido para que se note poco y sirva de algo: con el
#: usuario abriendo conversaciones sin parar, el fondo sigue avanzando a un
#: cuarto de velocidad en vez de pararse del todo.
UNO_DE_FONDO_CADA = 3

#: Cuánto dura la marca de «el usuario está mirando esto». Pasado ese rato la
#: conversación vuelve a su prioridad normal: si sigue abierta, el frontend lo
#: vuelve a decir, y si no, no tiene sentido seguir tratándola como urgente.
VIGENCIA_INTERACTIVA = 180.0


@dataclass(order=True)
class Trabajo:
    """Una conversación esperando turno."""

    prioridad: int
    #: Desempate dentro de la misma prioridad: lo más reciente primero. Se
    #: guarda negado para que el orden natural de la tupla haga lo correcto.
    orden: float = 0.0
    chat_id: int = field(compare=False, default=0)
    chat_jid: str = field(compare=False, default="")

    @property
    def es_interactiva(self) -> bool:
        return self.prioridad == Prioridad.INTERACTIVA


class HistoryScheduler:
    """Decide el orden. No ejecuta nada.

    Vive dentro del runtime de la cuenta y no guarda estado en la base: lo que
    persiste —cursor, estado, reintentos— ya lo lleva ``chat_history_state``.
    Aquí sólo está lo que dura lo que dura una ejecución.
    """

    def __init__(self, *, uno_de_fondo_cada: int = UNO_DE_FONDO_CADA) -> None:
        #: Conversaciones marcadas como abiertas, con cuándo se marcaron.
        self._interactivas: dict[str, float] = {}
        self._uno_de_fondo_cada = max(1, int(uno_de_fondo_cada))
        self._turnos_interactivos = 0
        #: Sólo para poder mirarlo desde fuera y desde las pruebas.
        self.cedidos_al_fondo = 0

    # -- Lo que dice el usuario --------------------------------------------

    def marcar_interactiva(self, chat_jid: str) -> None:
        """El usuario abrió esta conversación: pasa delante de todo.

        Es idempotente a propósito. Abrirla diez veces sube la prioridad una
        vez y no encola diez peticiones: quien impide la duplicación es la
        guardia por conversación del motor, y aquí simplemente se refresca la
        marca.
        """
        if not chat_jid:
            return
        nueva = chat_jid not in self._interactivas
        self._interactivas[chat_jid] = time.monotonic()
        if nueva:
            log.info("[HISTORY] priority chat=%s interactiva", _corto(chat_jid))

    def soltar_interactiva(self, chat_jid: str) -> None:
        """Ya no la está mirando. Vuelve a su prioridad normal."""
        self._interactivas.pop(chat_jid, None)

    def es_interactiva(self, chat_jid: str) -> bool:
        marcada = self._interactivas.get(chat_jid)
        if marcada is None:
            return False
        if time.monotonic() - marcada > VIGENCIA_INTERACTIVA:
            self._interactivas.pop(chat_jid, None)
            return False
        return True

    # -- El orden -----------------------------------------------------------

    def clasificar(
        self,
        chat_jid: str,
        *,
        ultimo_mensaje: float | None = None,
        reintentando: bool = False,
        reciente_desde: float | None = None,
    ) -> Prioridad:
        """En qué cajón cae esta conversación.

        El orden de las comprobaciones importa: lo que el usuario mira gana
        SIEMPRE, incluso si además está reintentando. Es lo que está esperando.
        """
        if self.es_interactiva(chat_jid):
            return Prioridad.INTERACTIVA
        if reintentando:
            return Prioridad.REINTENTO
        if (
            reciente_desde is not None
            and ultimo_mensaje is not None
            and ultimo_mensaje >= reciente_desde
        ):
            return Prioridad.RECIENTE
        return Prioridad.FONDO

    def ordenar(self, candidatos: Iterable[dict[str, Any]]) -> list[Trabajo]:
        """De una lista de conversaciones, el orden en que hay que atenderlas.

        Cada candidato es un diccionario con ``chat_id``, ``chat_jid`` y, si se
        conocen, ``ultimo_mensaje`` y ``reintentando``. Lo que no se sepa se
        trata como fondo: nunca se inventa una prioridad alta.
        """
        materializados = list(candidatos)
        reciente_desde = _umbral_de_reciente(materializados)

        trabajos: list[Trabajo] = []
        for candidato in materializados:
            chat_jid = str(candidato.get("chat_jid") or "")
            if not chat_jid:
                continue
            ultimo = candidato.get("ultimo_mensaje")
            prioridad = self.clasificar(
                chat_jid,
                ultimo_mensaje=float(ultimo) if ultimo else None,
                reintentando=bool(candidato.get("reintentando")),
                reciente_desde=reciente_desde,
            )
            trabajos.append(
                Trabajo(
                    prioridad=int(prioridad),
                    orden=-float(ultimo or 0),
                    chat_id=int(candidato.get("chat_id") or 0),
                    chat_jid=chat_jid,
                )
            )
        trabajos.sort()
        return self._intercalar_fondo(trabajos)

    def _intercalar_fondo(self, trabajos: list[Trabajo]) -> list[Trabajo]:
        """Cede un turno al fondo cada N interactivas (F19).

        Sin esto, con el usuario abriendo conversaciones sin parar el fondo no
        avanzaría nunca. Se hace sobre la lista ya ordenada para que la regla
        sea fácil de leer: se recorre, se cuentan las interactivas seguidas, y
        al llegar al tope se adelanta el primer trabajo de fondo que haya.
        """
        if not trabajos:
            return trabajos

        salida: list[Trabajo] = []
        pendientes = list(trabajos)
        seguidas = 0

        while pendientes:
            if seguidas >= self._uno_de_fondo_cada:
                indice = next(
                    (i for i, t in enumerate(pendientes) if not t.es_interactiva), None
                )
                if indice is not None:
                    salida.append(pendientes.pop(indice))
                    self.cedidos_al_fondo += 1
                    seguidas = 0
                    continue
            siguiente = pendientes.pop(0)
            salida.append(siguiente)
            seguidas = seguidas + 1 if siguiente.es_interactiva else 0

        return salida

    def resumen(self) -> dict[str, Any]:
        return {
            "interactivas": sorted(self._interactivas),
            "uno_de_fondo_cada": self._uno_de_fondo_cada,
            "cedidos_al_fondo": self.cedidos_al_fondo,
        }


def _umbral_de_reciente(candidatos: list[dict[str, Any]]) -> float | None:
    """Desde qué marca se considera «reciente».

    Se saca de los propios datos —la mediana de las últimas actividades— en
    vez de fijar un número de días. Una cuenta con conversaciones de hoy y una
    con la última de hace un año no tienen el mismo «reciente», y una constante
    haría que en la segunda no fuese reciente ninguna.
    """
    marcas = sorted(
        float(c["ultimo_mensaje"])
        for c in candidatos
        if c.get("ultimo_mensaje")
    )
    if not marcas:
        return None
    return marcas[len(marcas) // 2]


def _corto(chat_jid: str) -> str:
    """Un identificador reconocible que no es un número de teléfono."""
    usuario, _, servidor = str(chat_jid).partition("@")
    return f"{usuario[:6]}***@{servidor}" if servidor else usuario[:6] + "***"
