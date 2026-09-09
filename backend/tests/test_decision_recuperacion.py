"""¿Se sigue pidiendo historial, o ya no? La tabla, sin base de datos.

EL PROBLEMA QUE RESUELVE
-----------------------
Se midió: conversaciones que se quedaban a medias y sólo avanzaban cuando el
usuario pulsaba «Recuperar historial completo». Que el trabajo continúe no
puede depender de que alguien mire la pantalla.

Y no hace falta inteligencia para decidirlo: hace falta estado. Con el ancla,
lo que contestó el servidor, lo que trajo y cuántas esperas se han agotado, la
respuesta es una tabla. Estas pruebas son esa tabla, caso por caso.

LO QUE PROTEGEN
---------------
Sobre todo **cuándo se para**. Un controlador que sigue pidiendo sin freno
machaca el teléfono del usuario con peticiones que ya sabe que no van a traer
nada, y eso es peor que quedarse corto.
"""

from __future__ import annotations

import pytest

from app.history.decision import (
    ESPERAR,
    EXCAVAR,
    HUECO_ANTIGUO,
    HUECO_RECIENTE,
    MAX_ESPERAS_AGOTADAS,
    MAX_RONDAS_SIN_AVANCE,
    MOTOR_EN_DUDA,
    PARAR,
    SIN_ANCLA,
    Situacion,
    decidir,
    huecos,
    puede_despertar,
)


def _situacion(**cambios) -> Situacion:
    base = {
        "chat_jid": "5730111@s.whatsapp.net",
        "history_status": "pending",
        "tiene_cursor": True,
        "capacidad": "CONFIRMED",
    }
    base.update(cambios)
    return Situacion(**base)


# ---------------------------------------------------------------------------
# Lo que impide pedir
# ---------------------------------------------------------------------------


def test_sin_ancla_no_se_pide_nada():
    """No es un fallo del chat: es que todavía no hay con qué."""
    decision = decidir(_situacion(tiene_cursor=False))
    assert decision.accion == SIN_ANCLA
    assert decision.hay_que_pedir is False


def test_con_una_peticion_en_el_aire_se_espera():
    """El teléfono atiende de una en una."""
    assert decidir(_situacion(history_status="fetching")).accion == ESPERAR


def test_mientras_cumple_su_espera_no_se_insiste():
    """Insistir antes de tiempo ocupa la única ranura sin que nada cambie."""
    assert decidir(_situacion(espera_cumplida=False)).accion == ESPERAR


# ---------------------------------------------------------------------------
# Lo que dice que ya no hace falta
# ---------------------------------------------------------------------------


def test_agotado_es_terminal():
    assert decidir(_situacion(history_status="exhausted")).accion == PARAR


@pytest.mark.parametrize("tipo", [1, 3])
def test_FINAL_para(tipo):
    """1 y 3 son FINAL: no queda nada por ese lado."""
    assert decidir(_situacion(tipo_de_fin=tipo)).accion == PARAR


@pytest.mark.parametrize("tipo", [0, 2])
def test_MORE_no_para(tipo):
    assert decidir(_situacion(tipo_de_fin=tipo)).accion == EXCAVAR


def test_tres_respuestas_seguidas_sin_nada_nuevo_paran():
    """El freno del bucle.

    Una sola respuesta vacía puede ser un hueco del propio historial. Tres
    seguidas son el servidor diciendo que por ahí ya no hay nada, y seguir
    sería girar en vacío contra el teléfono del usuario.
    """
    assert (
        decidir(_situacion(rondas_sin_avance=MAX_RONDAS_SIN_AVANCE)).accion == PARAR
    )
    assert (
        decidir(_situacion(rondas_sin_avance=MAX_RONDAS_SIN_AVANCE - 1, tipo_de_fin=0)).accion
        == EXCAVAR
    )


def test_MORE_con_cero_mensajes_se_reintenta_pero_contado():
    """Se insiste, y se dice cuántas veces queda. No es un bucle."""
    for ronda in range(MAX_RONDAS_SIN_AVANCE):
        decision = decidir(_situacion(tipo_de_fin=0, rondas_sin_avance=ronda))
        if ronda < MAX_RONDAS_SIN_AVANCE:
            assert decision.accion == EXCAVAR
    assert decidir(_situacion(tipo_de_fin=0, rondas_sin_avance=MAX_RONDAS_SIN_AVANCE)).accion == (
        PARAR
    )


def test_demasiadas_esperas_agotadas_tambien_paran():
    assert (
        decidir(_situacion(esperas_agotadas=MAX_ESPERAS_AGOTADAS)).accion == PARAR
    )


# ---------------------------------------------------------------------------
# La seguridad que no se toca
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("capacidad", ["SUSPECT", "UNKNOWN"])
def test_con_el_motor_en_duda_no_se_fuerza(capacidad):
    """La capacidad la confirma una respuesta real, nunca un ACK ni un botón."""
    decision = decidir(_situacion(capacidad=capacidad, ultimo_recuento=50))
    assert decision.accion == MOTOR_EN_DUDA
    assert decision.hay_que_pedir is False


# ---------------------------------------------------------------------------
# Lo que autoriza a seguir
# ---------------------------------------------------------------------------


def test_si_la_ultima_trajo_mensajes_se_sigue_SOLO():
    """Justo lo que antes había que pedir a mano."""
    decision = decidir(_situacion(ultimo_recuento=50))
    assert decision.accion == EXCAVAR
    assert decision.hay_que_pedir is True


def test_con_ancla_y_sin_haber_pedido_nada_se_pide():
    assert decidir(_situacion(ultimo_recuento=None)).accion == EXCAVAR


def test_cada_decision_dice_por_que():
    """Un «no se pide» sin motivo obliga a leer el código para entenderlo."""
    for situacion in (
        _situacion(tiene_cursor=False),
        _situacion(history_status="exhausted"),
        _situacion(capacidad="SUSPECT"),
        _situacion(ultimo_recuento=50),
    ):
        assert decidir(situacion).motivo


# ---------------------------------------------------------------------------
# Los dos huecos, que NO son el mismo problema
# ---------------------------------------------------------------------------


def test_si_Web_ve_algo_mas_nuevo_falta_el_borde_reciente():
    """El caso medido, con sus números.

    Una conversación con 254 mensajes, marcada como terminada porque el
    servidor contestó ``COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY`` —así
    que por abajo estaba de verdad completa— y WhatsApp Web viendo un mensaje
    once días más nuevo que el último guardado.
    """
    marcas = huecos(
        mas_nuevo_guardado=1_787_000_000,  # 24 de agosto
        mas_viejo_guardado=1_786_000_000,
        cursor_timestamp=1_786_000_000,
        web_mas_nuevo=1_788_000_000,  # 4 de septiembre
    )
    assert HUECO_RECIENTE in marcas
    assert HUECO_ANTIGUO not in marcas


def test_si_el_ancla_apunta_mas_atras_falta_historia_vieja():
    marcas = huecos(
        mas_nuevo_guardado=1_788_000_000,
        mas_viejo_guardado=1_787_000_000,
        cursor_timestamp=1_786_000_000,
        web_mas_nuevo=1_788_000_000,
    )
    assert HUECO_ANTIGUO in marcas
    assert HUECO_RECIENTE not in marcas


def test_sin_nada_guardado_y_con_Web_viendo_algo_tambien_falta_el_borde():
    marcas = huecos(
        mas_nuevo_guardado=None,
        mas_viejo_guardado=None,
        cursor_timestamp=None,
        web_mas_nuevo=1_788_000_000,
    )
    assert marcas == [HUECO_RECIENTE]


def test_al_dia_no_hay_ningun_hueco():
    assert (
        huecos(
            mas_nuevo_guardado=1_788_000_000,
            mas_viejo_guardado=1_786_000_000,
            cursor_timestamp=1_786_000_000,
            web_mas_nuevo=1_788_000_000,
        )
        == []
    )


# ---------------------------------------------------------------------------
# Despertar a una conversación terminal
# ---------------------------------------------------------------------------


def test_una_referencia_nueva_SI_la_despierta():
    """Evidencia nueva. Eso sí es motivo."""
    assert puede_despertar(marcas=[], cursor_nuevo=True) is True


def test_que_pase_el_tiempo_NO_la_despierta():
    """Si el mantenimiento la reabriera, volvería a pedir lo que ya tiene.

    Es exactamente el bucle que costó una fase entera: un chat con cursor y
    sin mensajes oscilaba en cada pasada, para siempre.
    """
    assert puede_despertar(marcas=[], cursor_nuevo=False) is False


def test_un_hueco_reciente_por_si_solo_tampoco_la_despierta():
    """Excavar hacia atrás no cierra un hueco que está por arriba.

    Reabrir por eso sería insistir con la única herramienta que no puede
    arreglarlo.
    """
    assert puede_despertar(marcas=[HUECO_RECIENTE], cursor_nuevo=False) is False
