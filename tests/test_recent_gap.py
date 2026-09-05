"""El borde reciente: lo que quedó ARRIBA, y por qué es otro problema.

EL CASO MEDIDO
--------------
254 mensajes, del 12 al 24 de agosto. Siete peticiones, siete respuestas, cero
esperas agotadas, y el servidor cerrando con
``COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY``: por abajo estaba **de
verdad** completa. Y WhatsApp Web viendo un mensaje del 4 de septiembre.

``ON_DEMAND`` excava hacia atrás desde su ancla, así que nunca alcanza lo que
está por encima. Pulsar «Recuperar historial completo» repetía justamente la
operación que no puede cerrar ese hueco.

LO QUE PROTEGEN ESTAS PRUEBAS
-----------------------------
Sobre todo que las **dos dimensiones no se mezclen**: el ``exhausted`` del
historial antiguo sigue significando lo que significaba, y el relleno del borde
no lo toca ni lo reabre. Y que no se fabrique nada: sin una referencia real y
más nueva, no se pide.
"""

from __future__ import annotations

import pytest

from app.history.decision import (
    BORDE_EN_ESPERA,
    RELLENAR_BORDE,
    SIN_HUECO,
    decidir_borde,
)
from app.history.recent_gap import (
    AGOTADO,
    COMPLETO,
    LIMITE_DEL_SERVIDOR,
    MAX_BLOQUES,
    MAX_RONDAS_SIN_AVANCE,
    RELLENANDO,
    SIN_AVANCE,
    AnclaDelHueco,
    ancla_valida,
    decidir_siguiente,
    empalmo,
    hay_hueco,
)

WAMID = "AC7B0102030405060708090A0B0C24EB"
OTRO = "BD8C0102030405060708090A0B0C35FC"

#: 24 de agosto y 4 de septiembre, como en el caso real.
DB_NEWEST = 1_787_000_000
WEB_NEWEST = 1_788_000_000


class _Candidato:
    def __init__(self, **campos):
        self.chat_jid = "5730111@s.whatsapp.net"
        self.wa_msg_id = WAMID
        self.timestamp = WEB_NEWEST
        self.from_me = False
        self.source = "web_store"
        self.message_type = "text"
        for clave, valor in campos.items():
            setattr(self, clave, valor)


# ---------------------------------------------------------------------------
# Detectar el hueco
# ---------------------------------------------------------------------------


def test_si_Web_ve_algo_mas_nuevo_hay_hueco():
    assert hay_hueco(db_mas_nuevo=DB_NEWEST, web_mas_nuevo=WEB_NEWEST) is True


def test_al_dia_no_hay_hueco():
    assert hay_hueco(db_mas_nuevo=WEB_NEWEST, web_mas_nuevo=WEB_NEWEST) is False
    assert hay_hueco(db_mas_nuevo=WEB_NEWEST, web_mas_nuevo=DB_NEWEST) is False


def test_una_conversacion_SIN_mensajes_no_tiene_borde():
    """Tiene todo por recuperar, y de eso se encarga el camino normal.

    Comprobado contra la base real: marcarlas como hueco reciente ponía dos
    vías a pedir lo mismo del mismo chat, y la que sobra gasta una petición
    que le hace falta a otra.
    """
    assert hay_hueco(db_mas_nuevo=None, web_mas_nuevo=WEB_NEWEST) is False


def test_sin_Web_no_se_deduce_ningun_hueco():
    """No se supone lo que no se ha visto."""
    assert hay_hueco(db_mas_nuevo=DB_NEWEST, web_mas_nuevo=None) is False


# ---------------------------------------------------------------------------
# El ancla: real, y de verdad más nueva
# ---------------------------------------------------------------------------


def test_un_mensaje_real_y_mas_nuevo_sirve_de_ancla():
    assert ancla_valida(_Candidato(), db_mas_nuevo=DB_NEWEST) is None


def test_un_mensaje_que_ya_se_tiene_NO_sirve():
    """Anclar en algo que ya está no cierra ningún hueco y gasta una petición."""
    motivo = ancla_valida(_Candidato(timestamp=DB_NEWEST), db_mas_nuevo=DB_NEWEST)
    assert motivo == "no es mas nuevo que lo que ya hay"


def test_sin_saber_de_quien_es_el_mensaje_no_se_ancla():
    """`from_me` viaja en la petición: suponerlo cuesta una que no responde."""
    motivo = ancla_valida(_Candidato(from_me=None), db_mas_nuevo=DB_NEWEST)
    assert "de quien" in motivo


@pytest.mark.parametrize(
    "cambio",
    [
        {"wa_msg_id": None},
        {"wa_msg_id": "temp-abc"},
        {"timestamp": 0},
        {"timestamp": WEB_NEWEST * 1000},  # milisegundos
    ],
)
def test_una_referencia_inventada_o_mal_formada_no_sirve(cambio):
    """La misma validación de siempre, no una segunda más laxa."""
    assert ancla_valida(_Candidato(**cambio), db_mas_nuevo=DB_NEWEST) is not None


def test_sin_candidato_se_dice_y_ya_esta():
    assert ancla_valida(None, db_mas_nuevo=DB_NEWEST) == "sin candidato"


# ---------------------------------------------------------------------------
# El empalme
# ---------------------------------------------------------------------------


def test_un_identificador_conocido_cierra_el_hueco():
    """La señal buena: el bloque alcanzó un mensaje que ya se tenía."""
    como = empalmo(
        wamids_recibidos=[OTRO, WAMID],
        wamids_conocidos={WAMID},
        marca_mas_antigua_recibida=WEB_NEWEST,
        db_mas_nuevo=DB_NEWEST,
    )
    assert como == "wamid"


def test_si_no_hay_identificador_comun_se_mira_la_marca():
    """Respaldo, no primera opción: el mensaje del empalme puede no estar."""
    como = empalmo(
        wamids_recibidos=[OTRO],
        wamids_conocidos=set(),
        marca_mas_antigua_recibida=DB_NEWEST - 10,
        db_mas_nuevo=DB_NEWEST,
    )
    assert como == "timestamp"


def test_el_identificador_manda_sobre_la_marca():
    """Dos mensajes pueden compartir segundo; el identificador no engaña."""
    como = empalmo(
        wamids_recibidos=[WAMID],
        wamids_conocidos={WAMID},
        marca_mas_antigua_recibida=WEB_NEWEST,  # muy por encima
        db_mas_nuevo=DB_NEWEST,
    )
    assert como == "wamid"


def test_mientras_el_bloque_siga_por_encima_no_se_ha_empalmado():
    assert (
        empalmo(
            wamids_recibidos=[OTRO],
            wamids_conocidos={WAMID},
            marca_mas_antigua_recibida=WEB_NEWEST - 100,
            db_mas_nuevo=DB_NEWEST,
        )
        is None
    )


# ---------------------------------------------------------------------------
# Cuándo se para
# ---------------------------------------------------------------------------


def test_con_mensajes_nuevos_se_sigue():
    seguir, estado, _ = decidir_siguiente(
        mensajes_del_bloque=50, rondas_sin_avance=0, tipo_de_fin=0, bloques=1
    )
    assert seguir is True
    assert estado == RELLENANDO


@pytest.mark.parametrize("tipo", [1, 3])
def test_FINAL_antes_de_empalmar_NO_es_completo(tipo):
    """La distinción que evita mentir.

    El servidor dejó de dar antes de que se cerrara el hueco. Decir «completo»
    dejaría un agujero invisible que nadie volvería a mirar.
    """
    seguir, estado, motivo = decidir_siguiente(
        mensajes_del_bloque=50, rondas_sin_avance=0, tipo_de_fin=tipo, bloques=2
    )
    assert seguir is False
    assert estado == LIMITE_DEL_SERVIDOR
    assert estado != COMPLETO
    assert "FINAL" in motivo


def test_bloques_seguidos_sin_nada_nuevo_paran():
    seguir, estado, _ = decidir_siguiente(
        mensajes_del_bloque=0,
        rondas_sin_avance=MAX_RONDAS_SIN_AVANCE,
        tipo_de_fin=0,
        bloques=3,
    )
    assert seguir is False
    assert estado == SIN_AVANCE


def test_MORE_con_cero_se_reintenta_pero_contado():
    """Se insiste, y hay tope. No es un bucle."""
    for ronda in range(MAX_RONDAS_SIN_AVANCE):
        seguir, _, _ = decidir_siguiente(
            mensajes_del_bloque=0, rondas_sin_avance=ronda, tipo_de_fin=0, bloques=1
        )
        assert seguir is True


def test_hay_un_tope_de_bloques():
    """Un borde reciente son días, no años.

    Si en veinte bloques no se ha empalmado, lo que hay no es un borde: seguir
    sería excavar a ciegas contra el teléfono del usuario.
    """
    seguir, estado, motivo = decidir_siguiente(
        mensajes_del_bloque=50, rondas_sin_avance=0, tipo_de_fin=0, bloques=MAX_BLOQUES
    )
    assert seguir is False
    assert estado == LIMITE_DEL_SERVIDOR
    assert str(MAX_BLOQUES) in motivo


# ---------------------------------------------------------------------------
# La decisión, y las dos dimensiones separadas
# ---------------------------------------------------------------------------


def test_sin_hueco_no_se_pide_nada():
    decision = decidir_borde(
        hay_hueco=False, ancla_utilizable=True, capacidad="CONFIRMED", peticion_viva=False
    )
    assert decision.accion == SIN_HUECO
    assert decision.hay_que_rellenar_el_borde is False


def test_con_hueco_y_ancla_se_rellena():
    decision = decidir_borde(
        hay_hueco=True, ancla_utilizable=True, capacidad="CONFIRMED", peticion_viva=False
    )
    assert decision.accion == RELLENAR_BORDE
    assert decision.hay_que_rellenar_el_borde is True


def test_sin_ancla_real_se_espera_en_vez_de_inventar_una():
    decision = decidir_borde(
        hay_hueco=True, ancla_utilizable=False, capacidad="CONFIRMED", peticion_viva=False
    )
    assert decision.accion == BORDE_EN_ESPERA


@pytest.mark.parametrize("capacidad", ["SUSPECT", "UNKNOWN"])
def test_con_el_motor_en_duda_el_borde_tampoco_se_fuerza(capacidad):
    decision = decidir_borde(
        hay_hueco=True, ancla_utilizable=True, capacidad=capacidad, peticion_viva=False
    )
    assert decision.accion == BORDE_EN_ESPERA


def test_no_compite_con_una_peticion_en_vuelo():
    """El teléfono atiende de una en una: relleno, excavación y prueba, no."""
    decision = decidir_borde(
        hay_hueco=True, ancla_utilizable=True, capacidad="CONFIRMED", peticion_viva=True
    )
    assert decision.accion == BORDE_EN_ESPERA


def test_el_historial_agotado_NO_impide_rellenar_el_borde():
    """El caso de Isaac, en una línea.

    Que el servidor dijera que no queda historia vieja es información del OTRO
    frente. Mezclarlas obligaba a elegir cuál de las dos verdades contar.
    """
    decision = decidir_borde(
        hay_hueco=True, ancla_utilizable=True, capacidad="CONFIRMED", peticion_viva=False
    )
    assert decision.hay_que_rellenar_el_borde is True


def test_un_chat_que_solo_da_timeouts_NO_es_un_caso_de_borde_reciente():
    """El caso de Tía Nore, y por qué no se mezcla con el de Isaac.

    2 mensajes, cuatro peticiones, cuatro esperas agotadas, cero respuestas, y
    Web sin ver nada más nuevo. Lo que le falta es historia VIEJA, y su
    política es la de siempre: reintento con espera creciente y tope.
    """
    assert hay_hueco(db_mas_nuevo=WEB_NEWEST, web_mas_nuevo=DB_NEWEST) is False
    decision = decidir_borde(
        hay_hueco=False, ancla_utilizable=True, capacidad="SUSPECT", peticion_viva=False
    )
    assert decision.accion == SIN_HUECO


# ---------------------------------------------------------------------------
# El ancla no es el cursor histórico
# ---------------------------------------------------------------------------


def test_el_ancla_del_hueco_no_toca_el_cursor_historico():
    """Vive lo que dura la operación: no hay tabla ni migración.

    Es deliberado: lo que no se puede escribir no se puede escribir por error,
    y el cursor histórico es justo lo que no se puede tocar.
    """
    from app.models import ChatHistoryState

    columnas = {c.name for c in ChatHistoryState.__table__.columns}
    assert "recent_gap_anchor" not in columnas
    assert "oldest_message_id" in columnas, "el cursor historico sigue donde estaba"


def test_el_ancla_se_puede_usar_donde_se_usa_un_cursor():
    """Mismo nombre de campo: la petición se construye con el código de siempre."""
    ancla = AnclaDelHueco(
        chat_id=1,
        chat_jid="5730111@s.whatsapp.net",
        wa_msg_id=WAMID,
        timestamp=WEB_NEWEST,
        from_me=False,
        source="web_store",
    )
    assert ancla.message_id == WAMID
    assert ancla.timestamp == WEB_NEWEST


def test_el_relleno_reusa_la_peticion_de_siempre():
    """Ni segunda construcción de la petición, ni segunda correlación.

    Duplicar ese camino sería duplicar la parte que más costó entender.
    """
    import inspect

    from app.services.backfill_service import BackfillService

    fuente = inspect.getsource(BackfillService.rellenar_borde_reciente)
    assert "_request_once" in fuente
    for prohibido in ("build_on_demand_message", "persist_cursor", "HistorySeed("):
        assert prohibido not in fuente, f"el relleno alcanza {prohibido}"


def test_los_estados_del_borde_son_propios():
    """No se reutiliza el `exhausted` histórico: significan cosas distintas."""
    assert COMPLETO != "exhausted"
    assert LIMITE_DEL_SERVIDOR != COMPLETO
    assert AGOTADO != SIN_AVANCE
