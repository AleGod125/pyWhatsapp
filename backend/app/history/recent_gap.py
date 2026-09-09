"""El borde reciente: lo que quedó ARRIBA, no lo que falta abajo.

EL CASO QUE LO MOTIVA, MEDIDO
-----------------------------
Una conversación con 254 mensajes, del 12 al 24 de agosto. Siete peticiones,
siete respuestas, cero esperas agotadas, y el servidor cerrando con
``COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY``: por abajo estaba **de
verdad** completa.

Y WhatsApp Web viendo un mensaje del 4 de septiembre. Once días por encima del
último guardado::

        ┌──────────────── lo que hay ────────────────┐
        12 ago .................................. 24 ago
                                                     │
                                                  [ HUECO ]
                                                     │
                                                  4 sep   ← lo que ve Web

``ON_DEMAND`` excava **hacia atrás** desde su ancla, así que nunca alcanza lo
que está por encima del mensaje más nuevo. Pulsar «Recuperar historial
completo» repetía justamente la operación que no puede cerrar ese hueco.

DOS DIMENSIONES, NO UNA BANDERA
-------------------------------
Una conversación tiene dos frentes independientes, y confundirlos fue el error:

``old_history``   ¿queda historia vieja? La lleva el motor de siempre, y su
                  ``exhausted`` significa «el servidor dijo que no hay más
                  por abajo». Eso sigue siendo cierto.
``recent_gap``    ¿falta el borde reciente? Es lo de aquí, y no toca nada de
                  lo anterior.

Una conversación puede estar **completa por abajo y con hueco por arriba** a la
vez. Meterlo todo en un estado obligaba a elegir cuál de las dos verdades
contar.

LO QUE NO CAMBIA
----------------
* ``get_valid_history_cursor`` sigue devolviendo el ancla MÁS ANTIGUA, que es
  la que sirve para excavar hacia atrás. Aquí no se toca;
* el ``exhausted`` histórico no se reabre;
* la petición se construye y se correlaciona **exactamente igual**: mismo
  ``_request_once``, mismo turno único, mismo waiter antes del envío, misma
  huella de sesión. Lo único distinto es de qué ancla se parte;
* no se fabrica ninguna referencia. El ancla del hueco es un mensaje real que
  Web vio, con su WAMID, su marca y su dirección.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("PLAN_E")

# -- Estado del borde reciente. Deliberadamente aparte del histórico. -------

#: No se sabe de ningún hueco.
NINGUNO = "none"
#: Web ve algo más nuevo que lo guardado.
DETECTADO = "detected"
#: Se está pidiendo.
RELLENANDO = "fetching"
#: Se empalmó con lo que ya había: el hueco está cerrado.
COMPLETO = "complete"
#: El servidor dijo FINAL antes de empalmar. NO es lo mismo que completo.
LIMITE_DEL_SERVIDOR = "server_limit"
#: Respuestas válidas que no traen nada nuevo.
SIN_AVANCE = "no_progress"
#: Se agotó la espera.
AGOTADO = "timeout"

#: Rondas con respuesta válida y cero mensajes nuevos antes de rendirse.
MAX_RONDAS_SIN_AVANCE = 3

#: Bloques que se piden como mucho para cerrar un hueco. Un borde reciente son
#: días, no años: si en veinte bloques de cincuenta no se ha empalmado, lo que
#: hay no es un borde sino otra cosa, y seguir sería excavar a ciegas.
MAX_BLOQUES = 20

# ---------------------------------------------------------------------------
# De donde salio el hueco. El mecanismo de relleno es EL MISMO; lo que cambia
# es por que se detecto, y eso hace falta para poder medir cada via por
# separado sin montar un segundo motor.
# ---------------------------------------------------------------------------

#: El indice de WhatsApp Web vio un mensaje mas nuevo que lo guardado.
POR_INDICE_WEB = "historical_recent_gap"
#: Un mensaje en vivo no se pudo descifrar y su reenvio nunca llego, asi que
#: hay un agujero en el borde que el receptor no va a cerrar solo.
POR_LIVE_PERDIDO = "missed_live"

#: Margen al comparar marcas de tiempo. Se prefiere SIEMPRE empalmar por WAMID
#: exacto; esto es el respaldo para cuando el mensaje del empalme no está
#: guardado con el mismo identificador.
MARGEN_DE_EMPALME_SEGUNDOS = 60


@dataclass
class ResultadoDeRelleno:
    """Qué pasó al intentar cerrar el hueco."""

    estado: str = NINGUNO
    bloques: int = 0
    mensajes: int = 0
    #: Cómo se supo que ya se había empalmado.
    empalme: str | None = None
    motivo: str | None = None
    #: Por qué se detectó el hueco. El relleno es el mismo en los dos casos.
    origen: str = POR_INDICE_WEB

    def to_json(self) -> dict[str, Any]:
        return {
            "recent_gap": self.estado,
            "blocks": self.bloques,
            "messages": self.mensajes,
            "overlap": self.empalme,
            "reason": self.motivo,
            "detected_by": self.origen,
        }


@dataclass(frozen=True)
class AnclaDelHueco:
    """El mensaje real desde el que se excava hacia atrás para cerrar el hueco.

    NO es el cursor histórico y no lo sustituye. Vive lo que dura la
    operación: no hay tabla, no hay migración y no hay nada que pueda quedar
    escrito por error, que es justo el riesgo de tocar el cursor de verdad.
    """

    chat_id: int
    chat_jid: str
    wa_msg_id: str
    timestamp: int
    from_me: bool
    source: str
    #: La sesión en la que se obtuvo. Un ancla de otra vinculación no vale.
    session_fingerprint: str | None = None

    # El motor construye la petición leyendo estos dos nombres. Se exponen
    # igual que ``CursorInfo`` para poder reutilizar ``_request_once`` tal
    # cual: la forma de la petición es lo único que no se puede tocar.
    @property
    def message_id(self) -> str:
        return self.wa_msg_id


def hay_hueco(*, db_mas_nuevo: int | None, web_mas_nuevo: int | None) -> bool:
    """Web ve algo más nuevo que lo guardado aquí.

    Hace falta que HAYA algo guardado. Una conversación sin ningún mensaje no
    tiene borde: tiene todo por recuperar, y de eso se encarga el camino
    normal, que ancla en su referencia y baja. Se comprobó contra la base real:
    marcarlas como hueco reciente ponía dos vías a pedir lo mismo del mismo
    chat, y la que sobra gasta una petición que le hace falta a otra.
    """
    if not web_mas_nuevo or db_mas_nuevo is None:
        return False
    return int(web_mas_nuevo) > int(db_mas_nuevo)


def ancla_valida(candidato: Any, *, db_mas_nuevo: int | None) -> str | None:
    """Por qué ese candidato NO sirve como ancla del hueco, o ``None``.

    Se exige lo mismo que a cualquier referencia —identificador real, marca en
    segundos, dirección explícita— y además que sea de verdad **más nueva** que
    lo guardado: anclar en algo que ya se tiene no cierra ningún hueco y gasta
    una petición.
    """
    from app.history.seed_collector import SeedCandidate, validar

    if candidato is None:
        return "sin candidato"

    wa_msg_id = getattr(candidato, "wa_msg_id", None)
    timestamp = getattr(candidato, "timestamp", None)
    from_me = getattr(candidato, "from_me", None)
    if from_me is None:
        # No se supone `False`: viaja en la petición y equivocarse ahí cuesta
        # una petición que el servidor confirma y nunca responde.
        return "no se sabe de quien es el mensaje"

    motivo = validar(
        SeedCandidate(
            chat_jid=getattr(candidato, "chat_jid", "") or "",
            wa_msg_id=wa_msg_id,
            timestamp=timestamp,
            from_me=bool(from_me),
            source=getattr(candidato, "source", "web_store"),
            message_type=getattr(candidato, "message_type", None),
        )
    )
    if motivo is not None:
        return motivo

    if db_mas_nuevo is not None and int(timestamp) <= int(db_mas_nuevo):
        return "no es mas nuevo que lo que ya hay"
    return None


def empalmo(
    *,
    wamids_recibidos: list[str],
    wamids_conocidos: set[str],
    marca_mas_antigua_recibida: int | None,
    db_mas_nuevo: int | None,
) -> str | None:
    """Cómo se supo que el bloque ya alcanzó lo que había. ``None`` si no.

    Se prefiere **siempre** el identificador exacto: dos mensajes pueden
    compartir segundo, y parar por marca cuando no se ha empalmado de verdad
    dejaría un hueco más pequeño pero igual de invisible.

    La marca es el respaldo, y con margen: si el bloque ya llegó por debajo del
    mensaje más nuevo que hay guardado, lo que viene por detrás ya se tiene.
    """
    for wamid in wamids_recibidos:
        if wamid and wamid in wamids_conocidos:
            return "wamid"

    if (
        marca_mas_antigua_recibida is not None
        and db_mas_nuevo is not None
        and int(marca_mas_antigua_recibida)
        <= int(db_mas_nuevo) + MARGEN_DE_EMPALME_SEGUNDOS
    ):
        return "timestamp"
    return None


def decidir_siguiente(
    *,
    mensajes_del_bloque: int,
    rondas_sin_avance: int,
    tipo_de_fin: int | None,
    bloques: int,
) -> tuple[bool, str, str | None]:
    """¿Se pide otro bloque? Devuelve ``(seguir, estado, motivo)``.

    Misma forma que la tabla del historial antiguo, y por las mismas razones:
    con progreso se sigue, sin progreso se cuenta, y siempre hay un tope. Lo
    que cambia es qué significa FINAL aquí — ver abajo.
    """
    from app.history.decision import TIPOS_FINALES

    if bloques >= MAX_BLOQUES:
        return (
            False,
            LIMITE_DEL_SERVIDOR,
            f"no se empalmo en {MAX_BLOQUES} bloques",
        )

    if tipo_de_fin in TIPOS_FINALES:
        # FINAL aquí NO significa «completo». Significa que el servidor dejó de
        # dar antes de que empalmáramos, y decir «completo» sería mentir sobre
        # un hueco que sigue ahí.
        return (
            False,
            LIMITE_DEL_SERVIDOR,
            "el servidor dijo FINAL antes de empalmar",
        )

    if mensajes_del_bloque > 0:
        return (True, RELLENANDO, None)

    if rondas_sin_avance >= MAX_RONDAS_SIN_AVANCE:
        return (
            False,
            SIN_AVANCE,
            f"{rondas_sin_avance} bloques seguidos sin nada nuevo",
        )
    return (True, RELLENANDO, None)


def hueco_por_live_perdido(
    *,
    fallos_sin_recuperar: int,
    db_mas_nuevo: int | None,
    web_mas_nuevo: int | None,
) -> bool:
    """Un mensaje en vivo se perdió y el borde quedó con un agujero.

    LA DIFERENCIA CON EL OTRO HUECO
    -------------------------------
    El de WhatsApp Web se detecta comparando marcas: Web ve algo más nuevo. Éste
    parte de otra evidencia — **sabemos** que llegó un mensaje, que no se pudo
    autenticar y que su reenvío nunca llegó. Eso es un agujero conocido, no
    sospechado.

    Se midió sobre la sesión real: ``reintentos=22 recuperados=0``. El acuse
    sale, el servidor lo acepta y no vuelve nada. Sin esta red, esos mensajes
    no los cierra nadie: el receptor en vivo ya pasó de largo y la excavación
    histórica va hacia atrás desde el ancla, así que nunca alcanza el borde.

    Hace falta lo mismo que para el otro: **una referencia real más nueva**. Sin
    ella no se pide nada — no se fabrica un ancla para tapar un agujero.
    """
    if fallos_sin_recuperar <= 0:
        return False
    return hay_hueco(db_mas_nuevo=db_mas_nuevo, web_mas_nuevo=web_mas_nuevo)
