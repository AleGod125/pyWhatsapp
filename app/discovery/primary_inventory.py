"""El inventario que la sesión principal YA recibe y estaba tirando.

EL HALLAZGO
-----------
Se creía que la sesión principal «descubría menos» que WhatsApp Web y que por
eso hacía falta un segundo dispositivo. Medido sobre el ``INITIAL_BOOTSTRAP``
real, no es eso: **la información viene en el cable y se descarta**.

``pywhats`` modela cinco campos de una ``Conversation``::

    1 id      2 messages      3 name      5 last_msg_timestamp      6 unread_count

Y el cable trae treinta y uno. Escaneando los bytes del blob archivado, en las
41 conversaciones aparecen además::

    campo 12   marca de actividad          en 41 de 41
    campo 13   asunto del grupo            en  7
    campo 38   push name                   en  7
    campo 39   JID de teléfono (PN)        en 34
    campo 43   push name (alterno)         en  7
    campo 44   categoría                   en 33
    campo 49   LID del contacto            en 34

Los campos 39 y 49 juntos son **el par PN↔LID de cada contacto**, que es
justamente lo que costó fases enteras resolver por otras vías.

LO QUE ESTO SÍ RESUELVE
-----------------------
Descubrimiento sin segundo dispositivo: qué conversaciones existen, cómo se
llaman, cuándo tuvieron actividad y qué identificadores son la misma persona.

LO QUE NO RESUELVE, Y HAY QUE DECIRLO
-------------------------------------
**Anclas no hay.** Para una conversación sin mensajes, el cable no trae ningún
identificador de mensaje: los campos opacos son de 11 y 32 bytes, y un WAMID
son 20 o 32 caracteres hexadecimales. Ya se había comprobado antes por otra
vía, y este escaneo lo confirma desde los bytes.

Sin ancla no se puede pedir historial. Así que esto sustituye a WhatsApp Web
como **descubridor**, pero no como **fuente de referencias**.

COMO SE LEE
-----------
Sin tocar ``site-packages`` y sin reescribir el ``.proto`` de nadie: se
recorren los bytes del mensaje y se leen los campos por número. Protobuf está
diseñado para esto — un campo que el modelo no conoce sigue siendo legible—, y
así el día que pywhats amplíe su modelo esto sigue funcionando igual.

NADA SE FABRICA
---------------
Si un campo no viene, no se rellena. Una conversación sin nombre se queda sin
nombre; una sin marca de actividad se queda sin ella. Inventar aquí sería
inventar en la fuente de la que se fía todo lo demás.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

from app.core.logging_setup import get_logger

log = get_logger("APP")

# ---------------------------------------------------------------------------
# Los campos, medidos sobre el cable
# ---------------------------------------------------------------------------

#: `HistorySync.conversations`
CAMPO_CONVERSACIONES = 2

#: Dentro de una `Conversation`.
CAMPO_ID = 1
CAMPO_MENSAJES = 2
CAMPO_NOMBRE = 3
CAMPO_ULTIMO_TS = 5
CAMPO_NO_LEIDOS = 6
#: Marca de actividad. Viene en TODAS, incluidas las que no traen mensajes.
CAMPO_ACTIVIDAD = 12
#: Asunto del grupo.
CAMPO_ASUNTO = 13
#: Nombre que el contacto se puso.
CAMPO_PUSH_NAME = 38
CAMPO_PUSH_NAME_ALT = 43
#: El JID de teléfono del contacto.
CAMPO_PN = 39
#: Y su LID. Los dos juntos son el par que identifica a la misma persona.
CAMPO_LID = 49

#: Rango razonable para una marca en segundos: ~2001 a ~2096. Lo que se salga
#: no se convierte ni se adivina — se descarta.
TS_MINIMO = 1_000_000_000
TS_MAXIMO = 4_000_000_000


@dataclass
class ConversacionDescubierta:
    """Una conversación tal y como viene, sin interpretar de más."""

    raw_jid: str
    name: str | None = None
    is_group: bool = False
    pn_jid: str | None = None
    lid_jid: str | None = None
    last_timestamp: int | None = None
    unread: int = 0
    #: Cuántos mensajes traía el bootstrap para ella. Casi siempre cero.
    mensajes: int = 0
    source: str = "primary_bootstrap"

    @property
    def canonical_jid(self) -> str:
        """Con cuál de los dos identificadores se guarda.

        Se prefiere el que trae la propia conversación: es el que usa el
        servidor para hablar de ella. El par PN/LID viaja aparte para que el
        resolutor de alias de siempre pueda unirlos — aquí no se decide eso.
        """
        return self.raw_jid

    @property
    def confidence(self) -> str:
        """Cuánto se sabe de ella. Sirve para priorizar, no para filtrar."""
        if self.name and self.last_timestamp:
            return "alta"
        if self.last_timestamp:
            return "media"
        return "baja"

    def to_json(self) -> dict[str, Any]:
        return {
            "canonical_jid": self.canonical_jid,
            "raw_jid": self.raw_jid,
            "pn_jid": self.pn_jid,
            "lid_jid": self.lid_jid,
            "name": self.name,
            "is_group": self.is_group,
            "last_timestamp": self.last_timestamp,
            "messages_in_bootstrap": self.mensajes,
            "source": self.source,
            "confidence": self.confidence,
        }


@dataclass
class ResultadoDelInventario:
    """Lo que se encontró, y lo que NO se puede sacar de aquí."""

    conversaciones: list[ConversacionDescubierta] = field(default_factory=list)
    #: Conversaciones con par PN↔LID completo.
    con_par_pn_lid: int = 0
    con_nombre: int = 0
    con_actividad: int = 0
    #: Ninguna. El cable no trae identificadores de mensaje para chats sin
    #: mensajes, y se deja explícito para que nadie lo busque otra vez.
    con_ancla: int = 0

    def to_json(self) -> dict[str, Any]:
        grupos = sum(1 for c in self.conversaciones if c.is_group)
        return {
            "primary_chats": len(self.conversaciones),
            "groups": grupos,
            "individuals": len(self.conversaciones) - grupos,
            "with_name": self.con_nombre,
            "with_last_activity": self.con_actividad,
            "with_pn_lid_pair": self.con_par_pn_lid,
            "with_seed": self.con_ancla,
        }


# ---------------------------------------------------------------------------
# Leer protobuf por número de campo
# ---------------------------------------------------------------------------


def _varint(datos: bytes, i: int) -> tuple[int, int]:
    valor = 0
    desplazamiento = 0
    while i < len(datos):
        b = datos[i]
        valor |= (b & 0x7F) << desplazamiento
        i += 1
        if not b & 0x80:
            return valor, i
        desplazamiento += 7
    raise ValueError("varint incompleto")


def _campos(datos: bytes) -> Iterator[tuple[int, int, Any]]:
    """``(numero, tipo, valor)`` de cada campo del nivel superior.

    Un mensaje mal formado corta el recorrido en vez de lanzar: lo que se haya
    podido leer sigue valiendo, y perder el inventario entero por un byte
    raro sería peor.
    """
    i = 0
    while i < len(datos):
        try:
            clave, i = _varint(datos, i)
            numero, tipo = clave >> 3, clave & 7
            if tipo == 0:
                valor, i = _varint(datos, i)
            elif tipo == 2:
                largo, i = _varint(datos, i)
                valor = datos[i : i + largo]
                i += largo
            elif tipo == 5:
                valor, i = datos[i : i + 4], i + 4
            elif tipo == 1:
                valor, i = datos[i : i + 8], i + 8
            else:
                return
        except (ValueError, IndexError):
            return
        yield numero, tipo, valor


def _texto(valor: Any) -> str | None:
    if not isinstance(valor, (bytes, bytearray)):
        return None
    try:
        s = valor.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return s or None


def _marca(valor: Any) -> int | None:
    """Una marca en SEGUNDOS, o nada.

    No se divide por mil ni se adivina la unidad: eso produce un cursor que el
    servidor confirma y nunca responde, y es el error más caro que ha tenido
    este proyecto.
    """
    if not isinstance(valor, int):
        return None
    return valor if TS_MINIMO <= valor <= TS_MAXIMO else None


def _jid(valor: Any) -> str | None:
    texto = _texto(valor)
    return texto if texto and "@" in texto else None


# ---------------------------------------------------------------------------
# El inventario
# ---------------------------------------------------------------------------


def leer_conversacion(crudo: bytes) -> ConversacionDescubierta | None:
    """Una ``Conversation`` del cable, con los campos que pywhats descarta."""
    identificador = None
    conversacion = ConversacionDescubierta(raw_jid="")
    nombres: list[str] = []

    for numero, _tipo, valor in _campos(crudo):
        if numero == CAMPO_ID:
            identificador = _jid(valor)
        elif numero == CAMPO_MENSAJES:
            conversacion.mensajes += 1
        elif numero in (CAMPO_NOMBRE, CAMPO_ASUNTO):
            # El asunto del grupo y el nombre van primero: son el nombre "de
            # la conversación", no el que el contacto se puso.
            texto = _texto(valor)
            if texto:
                nombres.insert(0, texto)
        elif numero in (CAMPO_PUSH_NAME, CAMPO_PUSH_NAME_ALT):
            texto = _texto(valor)
            if texto:
                nombres.append(texto)
        elif numero == CAMPO_PN:
            conversacion.pn_jid = _jid(valor)
        elif numero == CAMPO_LID:
            conversacion.lid_jid = _jid(valor)
        elif numero in (CAMPO_ACTIVIDAD, CAMPO_ULTIMO_TS):
            marca = _marca(valor)
            if marca and (
                conversacion.last_timestamp is None or marca > conversacion.last_timestamp
            ):
                conversacion.last_timestamp = marca
        elif numero == CAMPO_NO_LEIDOS and isinstance(valor, int):
            conversacion.unread = valor

    if not identificador:
        return None
    conversacion.raw_jid = identificador
    conversacion.is_group = identificador.endswith("@g.us")
    conversacion.name = nombres[0] if nombres else None
    return conversacion


def leer_bootstrap(blob: bytes) -> ResultadoDelInventario:
    """Todas las conversaciones de un blob de ``HistorySync``.

    Se le pasan los bytes tal cual llegaron. Funciona igual sobre el blob
    archivado en disco que sobre el que acaba de entrar, y eso permite
    medirlo sin volver a vincular nada.
    """
    resultado = ResultadoDelInventario()
    vistos: set[str] = set()

    for numero, tipo, valor in _campos(blob):
        if numero != CAMPO_CONVERSACIONES or tipo != 2:
            continue
        conversacion = leer_conversacion(valor)
        if conversacion is None or conversacion.raw_jid in vistos:
            continue
        vistos.add(conversacion.raw_jid)
        resultado.conversaciones.append(conversacion)
        if conversacion.name:
            resultado.con_nombre += 1
        if conversacion.last_timestamp:
            resultado.con_actividad += 1
        if conversacion.pn_jid and conversacion.lid_jid:
            resultado.con_par_pn_lid += 1

    log.info(
        "[PRIMARY_INDEX] conversaciones=%d con_nombre=%d con_actividad=%d "
        "par_pn_lid=%d con_ancla=%d",
        len(resultado.conversaciones),
        resultado.con_nombre,
        resultado.con_actividad,
        resultado.con_par_pn_lid,
        resultado.con_ancla,
    )
    return resultado
