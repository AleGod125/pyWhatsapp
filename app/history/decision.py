"""¿Se sigue pidiendo historial de esta conversación, o ya no? Una regla, no una IA.

POR QUÉ EXISTE
--------------
Se midió: conversaciones que se quedaban a medias, y el usuario tenía que
pulsar «Recuperar historial completo» para que aparecieran más mensajes. Que
el trabajo avance o no dependía de que alguien mirara la pantalla y pulsara.

La decisión no necesita inteligencia: necesita estado. En cada momento se sabe
si hay ancla, qué contestó el servidor la última vez, cuántos mensajes trajo,
cuántas esperas se han agotado y si la capacidad está confirmada. Con eso la
respuesta es una tabla, y una tabla se puede leer, probar y discutir.

LAS SEÑALES
-----------
Todas salen de ``chat_history_state`` y de ``history_requests``; ninguna se
inventa aquí:

``history_status``            dónde está según el motor de siempre
``cursor``                    si hay con qué pedir, según LA función canónica
``last_response_count``       qué trajo la última respuesta válida
``consecutive_no_progress``   respuestas válidas seguidas sin nada nuevo
``attempt_count``             esperas agotadas acumuladas
``next_retry_at``             cuándo vuelve a tocarle
``capability``                si ON_DEMAND ha demostrado que responde

LO QUE NO HACE
--------------
No pide historial, no escribe, no elige cursor y no inventa ninguna
referencia. Devuelve una decisión; ejecutarla es de otro.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Lo que dice el servidor al cerrar un bloque. 1 y 3 son FINAL —ya no queda
#: nada por ese lado—; 0 y 2 son MORE.
TIPOS_FINALES = {1, 3}

# -- Las decisiones posibles ------------------------------------------------

#: Hay ancla, la capacidad responde y queda historial: se pide.
EXCAVAR = "EXCAVAR"
#: Se pidió y todavía no ha contestado, o está cumpliendo su espera.
ESPERAR = "ESPERAR"
#: Sin referencia real no se puede pedir nada. No es un error del chat.
SIN_ANCLA = "SIN_ANCLA"
#: El servidor dijo que no queda más, o se dejó de avanzar. Terminal.
PARAR = "PARAR"
#: El motor está en duda: se espera a que una respuesta real lo confirme.
MOTOR_EN_DUDA = "MOTOR_EN_DUDA"

#: Respuestas válidas seguidas sin un solo mensaje nuevo antes de parar.
#:
#: Tres, no una. Un bloque vacío puede ser un hueco del propio historial; tres
#: seguidos son el servidor diciendo que ya no hay nada por ahí. Y con tope,
#: porque sin él esto gira en vacío indefinidamente contra el teléfono del
#: usuario.
MAX_RONDAS_SIN_AVANCE = 3

#: Esperas agotadas seguidas antes de dejar de insistir con ese chat. El
#: reintento con espera creciente lo lleva el motor; esto es el tope final.
MAX_ESPERAS_AGOTADAS = 5


@dataclass(frozen=True)
class Situacion:
    """Lo que se sabe de una conversación en este momento.

    Un objeto plano a propósito: la decisión se puede probar sin base de
    datos, sin red y sin teléfono.
    """

    chat_jid: str
    history_status: str | None = None
    tiene_cursor: bool = False
    #: Qué trajo la última respuesta válida. ``None`` si no ha habido ninguna.
    ultimo_recuento: int | None = None
    #: ``endOfHistoryTransferType`` de la última respuesta, si se conoce.
    tipo_de_fin: int | None = None
    rondas_sin_avance: int = 0
    esperas_agotadas: int = 0
    #: Si su espera de reintento ya venció.
    espera_cumplida: bool = True
    #: Hay una petición enviada sin respuesta todavía.
    peticion_viva: bool = False
    capacidad: str = "CONFIRMED"


@dataclass(frozen=True)
class Decision:
    """Qué hacer, y por qué. El motivo viaja para poder explicarlo."""

    accion: str
    motivo: str

    @property
    def hay_que_pedir(self) -> bool:
        return self.accion == EXCAVAR

    @property
    def hay_que_rellenar_el_borde(self) -> bool:
        return self.accion == RELLENAR_BORDE


def decidir(situacion: Situacion) -> Decision:
    """La tabla entera, en el orden en que se pregunta.

    El orden importa y no es arbitrario: primero lo que impide pedir —no hay
    con qué, o ya hay algo en marcha—, después lo que dice que ya no hace
    falta, y sólo al final lo que autoriza a seguir. Al revés se pediría
    historial de conversaciones que ya lo tenían todo.
    """
    # 1) Sin referencia no se puede pedir NADA. No es un fallo del chat: es
    #    que todavía no ha aparecido una, y aparecerá o no por otra vía.
    if not situacion.tiene_cursor:
        return Decision(SIN_ANCLA, "no hay ninguna referencia con la que pedir")

    # 2) Ya hay una petición en el aire. El teléfono atiende de una en una.
    #    `fetching` significa exactamente eso, venga marcado como venga.
    if situacion.peticion_viva or situacion.history_status == "fetching":
        return Decision(ESPERAR, "hay una peticion esperando respuesta")

    # 3) Está cumpliendo su espera de reintento. Insistir antes de tiempo
    #    ocupa la única ranura sin que nada haya cambiado.
    if not situacion.espera_cumplida:
        return Decision(ESPERAR, "todavia esta cumpliendo su espera de reintento")

    # 4) El servidor dijo que no queda más. Es terminal y no se reabre por
    #    mantenimiento: hace falta evidencia nueva.
    if situacion.history_status == "exhausted":
        return Decision(PARAR, "el servidor dijo que no queda mas historial")
    if situacion.tipo_de_fin in TIPOS_FINALES:
        return Decision(PARAR, "la ultima respuesta vino marcada como FINAL")

    # 5) Se dejó de avanzar. Respuestas válidas, pero ni un mensaje nuevo.
    if situacion.rondas_sin_avance >= MAX_RONDAS_SIN_AVANCE:
        return Decision(
            PARAR,
            f"{situacion.rondas_sin_avance} respuestas seguidas sin nada nuevo",
        )

    # 6) Demasiadas esperas agotadas seguidas. El motor ya reintenta con
    #    espera creciente; esto es el tope, para no insistir para siempre.
    if situacion.esperas_agotadas >= MAX_ESPERAS_AGOTADAS:
        return Decision(
            PARAR, f"{situacion.esperas_agotadas} esperas agotadas seguidas"
        )

    # 7) El motor está en duda. NO se fuerza: la capacidad la confirma una
    #    respuesta real correlacionada, nunca un ACK.
    if situacion.capacidad != "CONFIRMED":
        return Decision(
            MOTOR_EN_DUDA, f"la capacidad ON_DEMAND esta en {situacion.capacidad}"
        )

    # 8) La última respuesta trajo mensajes: hay más por ahí. Se sigue solo,
    #    que es justo lo que antes había que pedir a mano.
    if situacion.ultimo_recuento:
        return Decision(EXCAVAR, "la ultima respuesta trajo mensajes")

    # 9) El servidor dijo MORE y no trajo nada. Se reintenta, pero contado:
    #    el tope de la regla 5 es lo que impide el bucle.
    if situacion.tipo_de_fin is not None:
        return Decision(
            EXCAVAR,
            f"el servidor dijo que queda mas (intento {situacion.rondas_sin_avance + 1}"
            f" de {MAX_RONDAS_SIN_AVANCE})",
        )

    # 10) Tiene ancla y nunca se le ha pedido nada.
    return Decision(EXCAVAR, "tiene referencia y no se le ha pedido todavia")


def situacion_de(estado: Any, *, tiene_cursor: bool, capacidad: str) -> Situacion:
    """Traduce una fila de ``chat_history_state`` a lo que mira la decisión.

    Vive aquí para que la tabla de arriba no tenga que saber de SQLAlchemy y
    se pueda probar con objetos planos.
    """
    from app.history.cursor import espera_cumplida

    ultima = getattr(estado, "last_response_count", None)
    return Situacion(
        chat_jid=getattr(estado, "chat_jid", ""),
        history_status=getattr(estado, "history_status", None),
        tiene_cursor=tiene_cursor,
        ultimo_recuento=int(ultima) if ultima is not None else None,
        tipo_de_fin=None,
        rondas_sin_avance=int(getattr(estado, "consecutive_no_progress", 0) or 0),
        esperas_agotadas=int(getattr(estado, "attempt_count", 0) or 0),
        espera_cumplida=espera_cumplida(getattr(estado, "next_retry_at", None)),
        peticion_viva=getattr(estado, "history_status", None) == "fetching",
        capacidad=capacidad,
    )


# ---------------------------------------------------------------------------
# Huecos: falta historia vieja o falta el borde reciente. NO es lo mismo.
# ---------------------------------------------------------------------------

#: WhatsApp Web ve un mensaje MÁS NUEVO que el último guardado aquí.
HUECO_RECIENTE = "RECENT_GAP_DETECTED"
#: El ancla apunta más atrás de lo que hay guardado: queda historia vieja.
HUECO_ANTIGUO = "OLD_HISTORY_PENDING"


def huecos(
    *,
    mas_nuevo_guardado: int | None,
    mas_viejo_guardado: int | None,
    cursor_timestamp: int | None,
    web_mas_nuevo: int | None,
) -> list[str]:
    """Qué le falta, en las dos direcciones. Se miran por separado.

    Se midió el caso que obliga a distinguirlas: una conversación con 254
    mensajes, marcada como terminada porque el servidor contestó
    ``COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY`` —así que por abajo
    estaba de verdad completa— y con WhatsApp Web viendo un mensaje **once
    días más nuevo** que el último guardado.

    Eso no lo arregla excavar: ``ON_DEMAND`` va hacia atrás desde el ancla y
    nunca alcanza lo que está por encima. Llamarlo «incompleto» a secas
    llevaba a pedir otra vez lo que ya se tenía.
    """
    marcas: list[str] = []
    if web_mas_nuevo and (
        mas_nuevo_guardado is None or int(web_mas_nuevo) > int(mas_nuevo_guardado)
    ):
        marcas.append(HUECO_RECIENTE)
    if (
        cursor_timestamp is not None
        and mas_viejo_guardado is not None
        and int(cursor_timestamp) < int(mas_viejo_guardado)
    ):
        marcas.append(HUECO_ANTIGUO)
    return marcas


# ---------------------------------------------------------------------------
# La segunda dimensión: el borde reciente
# ---------------------------------------------------------------------------
#
# `decidir` contesta por el historial ANTIGUO: ¿queda algo por abajo? Esta otra
# contesta por el borde RECIENTE: ¿falta algo por arriba? Son independientes, y
# meterlas en la misma bandera obligaba a elegir cuál de las dos verdades
# contar — una conversación puede estar completa por abajo y con hueco por
# arriba a la vez, que es exactamente el caso que se midió.

#: No hay nada que rellenar por arriba.
SIN_HUECO = "SIN_HUECO"
#: Hay hueco y se puede pedir: se encola el relleno.
RELLENAR_BORDE = "RELLENAR_BORDE"
#: Hay hueco pero ahora no se puede: se dice por qué.
BORDE_EN_ESPERA = "BORDE_EN_ESPERA"


def decidir_borde(
    *,
    hay_hueco: bool,
    ancla_utilizable: bool,
    capacidad: str,
    peticion_viva: bool,
    por_live_perdido: bool = False,
) -> Decision:
    """¿Se rellena el borde reciente de esta conversación?

    NO mira el estado del historial antiguo a propósito. Que esté ``exhausted``
    es información sobre el otro frente y no dice nada de éste: de hecho el
    caso típico es justamente ése —completa por abajo, con hueco por arriba—.
    """
    if not hay_hueco:
        return Decision(SIN_HUECO, "lo guardado esta al dia con lo que ve Web")
    if not ancla_utilizable:
        # Sin una referencia real y más nueva no se pide nada. No se fabrica
        # ninguna: un ancla inventada recibe confirmación y después silencio.
        return Decision(BORDE_EN_ESPERA, "no hay una referencia real mas nueva")
    if peticion_viva:
        return Decision(BORDE_EN_ESPERA, "hay una peticion esperando respuesta")
    if capacidad != "CONFIRMED":
        return Decision(BORDE_EN_ESPERA, f"la capacidad esta en {capacidad}")
    if por_live_perdido:
        # Se sabe que falta algo concreto: llego, no se pudo autenticar y su
        # reenvio nunca vino. No es una sospecha por comparar marcas.
        return Decision(
            RELLENAR_BORDE, "un mensaje en vivo no se recupero y dejo un agujero"
        )
    return Decision(RELLENAR_BORDE, "Web ve mensajes mas nuevos que los guardados")


def puede_despertar(*, marcas: list[str], cursor_nuevo: bool) -> bool:
    """Si una conversación terminal tiene motivo para volver a intentarlo.

    La regla: **evidencia nueva, no mantenimiento**. Que pase el tiempo no es
    un motivo; una referencia que antes no existía, sí. Sin esto una
    conversación agotada se reabriría en cada pasada y volvería a pedir lo que
    ya tiene, que es exactamente el bucle que costó una fase entera.

    Un hueco reciente NO despierta por sí solo: excavar hacia atrás no lo
    cierra, así que reabrir por eso sería insistir sin poder arreglarlo.
    """
    return bool(cursor_nuevo)


# ---------------------------------------------------------------------------
# Aplicar la tabla a lo que hay en la base
# ---------------------------------------------------------------------------
#
# Vive aquí, y no en quien la usa, por la misma razón por la que la tabla vive
# aquí: **una sola definición**. El cursor se lee con LA función canónica
# —``get_valid_history_cursor``—, que es la que usan el canary, la excavación
# y la cola. Dos definiciones de «tiene con qué seguir» es exactamente el
# fallo que costó una fase entera.
#
# Se LEE. No se escribe, no se persiste ningún cursor y no se toca ningún
# estado: esto devuelve una lista de conversaciones y quien la reciba decide
# qué hacer con ella.

#: Estados que no entran en la decisión: los terminales y los que ni siquiera
#: tienen referencia. Esos los mueve otra cosa —una referencia nueva—, no esto.
FUERA_DE_LA_DECISION = ("exhausted", "no_valid_cursor", "waiting_seed")


def chats_que_pueden_seguir(
    database: Any, *, capacidad: str, limite: int = 500
) -> tuple[list[str], dict[str, int]]:
    """Las conversaciones a las que toca volver a pedirles historial.

    Devuelve ``(jids, recuento_por_decision)``. El recuento se devuelve para
    poder decir en el registro por qué NO siguen las demás: «0 conversaciones
    siguen» no dice nada, y «12 esperando, 3 en duda» dice dónde mirar.
    """
    from sqlalchemy import select

    from app.history.cursor import get_valid_history_cursor
    from app.models import ChatHistoryState

    seguir: list[str] = []
    motivos: dict[str, int] = {}
    with database.transaction() as sesion:
        estados = (
            sesion.execute(
                select(ChatHistoryState)
                .where(ChatHistoryState.history_status.notin_(FUERA_DE_LA_DECISION))
                .limit(limite)
            )
            .scalars()
            .all()
        )
        for estado in estados:
            cursor = get_valid_history_cursor(sesion, chat_jid=estado.chat_jid)
            decision = decidir(
                situacion_de(
                    estado, tiene_cursor=cursor is not None, capacidad=capacidad
                )
            )
            motivos[decision.accion] = motivos.get(decision.accion, 0) + 1
            if decision.hay_que_pedir and estado.chat_jid:
                seguir.append(estado.chat_jid)
    return seguir, motivos
