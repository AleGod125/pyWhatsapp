"""Los mensajes que envío desde mi teléfono, y por qué a veces no llegaban.

LA EVIDENCIA
------------
Sobre la sesión real, tres veces seguidas::

    reintentos=22  recuperados=0  sin_resolver=22
    reintentos=14  recuperados=0  sin_resolver=14
    reintentos=5   recuperados=0  sin_resolver=5

Cero de veintidós. El acuse de reintento sale, el servidor lo acepta con
``ack->ok class=receipt``, y no vuelve nada — ni reenvío ni ``pkmsg``.

Y el diagnóstico ya decía que la dirección era la correcta::

    [LID] copia propia origen=primary_phone type=msg dispositivo=0
    direccion_signal=865311***:0@lid mapa_resuelve=True
    sesion_por_lid=True sesion_por_pn_mismo_dispositivo=True

O sea: la sesión se busca por LID, la sesión por LID existe, y aun así el MAC
falla. No es un fallo de resolución: es un ratchet desincronizado.

LO QUE SE PROTEGE AQUÍ
----------------------
Dos cosas, y ninguna ablanda la seguridad:

1. que el acuse diga cuántas veces ha fallado ESE mensaje, en vez de decir
   siempre «es la primera»;
2. que un mensaje en vivo perdido deje un agujero **detectable**, para que la
   red de seguridad pueda cerrarlo con una referencia real.

Un MAC que falla sigue fallando. Nada entra a la base sin autenticar.
"""

from __future__ import annotations

import pytest

from app.core.retry_tracker import (
    ORIGINAL_FAILED,
    RETRY_SENT,
    RETRY_SUCCESS,
    RetryTracker,
)
from app.history.decision import BORDE_EN_ESPERA, RELLENAR_BORDE, decidir_borde
from app.history.recent_gap import (
    POR_INDICE_WEB,
    POR_LIVE_PERDIDO,
    ResultadoDeRelleno,
    hueco_por_live_perdido,
)

WAMID = "ACEA26BEFF7F39248C78457A91D3D61B"
#: 13:20 y 13:36, como en el caso real.
DB_NEWEST = 1_788_000_000
WEB_NEWEST = 1_788_000_960


# ---------------------------------------------------------------------------
# El contador del acuse
# ---------------------------------------------------------------------------


def test_el_primer_fallo_cuenta_uno():
    seguimiento = RetryTracker()
    seguimiento.fallo(WAMID, "mac", origen="primary_phone", enc_type="msg", dispositivo=0)

    assert seguimiento.intentos_de(WAMID) == 1


def test_fallar_otra_vez_sube_el_contador():
    """Es el número que va en el acuse.

    Mandar siempre 1 le dice al emisor que es la primera vez, así que nunca
    alcanza la condición que le haría rehacer la sesión.
    """
    seguimiento = RetryTracker()
    for _ in range(3):
        seguimiento.fallo(WAMID, "mac", origen="primary_phone")

    assert seguimiento.intentos_de(WAMID) == 3


def test_de_un_mensaje_que_nunca_fallo_no_hay_contador():
    assert RetryTracker().intentos_de("NO-EXISTE") == 0


def test_el_acuse_solo_toma_el_relevo_a_partir_del_segundo_intento():
    """El primero ya lo construye bien pywhats: no hay nada que corregir.

    Tomar el relevo siempre significaría reimplementar una stanza que el
    servidor ya acepta, y sin ganar nada.
    """
    import inspect

    from app.compat import retry_observer

    fuente = inspect.getsource(retry_observer.apply)
    assert "intentos > 1" in fuente


def test_si_no_se_puede_construir_se_manda_el_de_siempre():
    """Un acuse con el contador mal es mejor que ningún acuse."""
    import inspect

    from app.compat import retry_observer

    fuente = inspect.getsource(retry_observer.apply)
    assert "if not enviado" in fuente
    assert "await original(self, node, sender=sender)" in fuente


def test_el_acuse_conserva_el_participante_de_un_grupo():
    """Sin él, el servidor no sabe a quién pedirle el reenvío."""
    import inspect

    from app.compat import retry_observer

    fuente = inspect.getsource(retry_observer._enviar_con_contador)
    assert '"participant"' in fuente


def test_lo_unico_que_cambia_es_el_contador():
    """El identificador, la marca, el destinatario y el registro van igual.

    Esas partes el servidor ya las acepta; cambiarlas sería reimplementar
    protocolo que funciona.
    """
    import inspect

    from app.compat import retry_observer

    fuente = inspect.getsource(retry_observer._enviar_con_contador)
    for pieza in ('"id"', '"t"', '"v"', '"type": "retry"', "registration"):
        assert pieza in fuente, f"el acuse perdio {pieza}"


# ---------------------------------------------------------------------------
# El recorrido de un mensaje que no cuadró
# ---------------------------------------------------------------------------


def test_un_mensaje_que_falla_empieza_su_recorrido():
    seguimiento = RetryTracker()
    seguimiento.fallo(WAMID, "signal message mac check failed", origen="primary_phone")

    assert seguimiento.estado_de(WAMID) == ORIGINAL_FAILED


def test_al_mandar_el_acuse_se_anota():
    seguimiento = RetryTracker()
    seguimiento.fallo(WAMID, "mac", origen="primary_phone")
    seguimiento.acuse_enviado(WAMID)

    assert seguimiento.estado_de(WAMID) == RETRY_SENT
    assert seguimiento.resumen()["retry_sent"] == 1


def test_lo_que_no_vuelve_se_cuenta_como_sin_respuesta():
    """El caso medido: 22 acuses, 0 recuperados."""
    seguimiento = RetryTracker()
    for i in range(22):
        wamid = f"{WAMID[:-2]}{i:02X}"
        seguimiento.fallo(wamid, "mac", origen="primary_phone")
        seguimiento.acuse_enviado(wamid)

    resumen = seguimiento.resumen()
    assert resumen["retry_sent"] == 22
    assert resumen.get(RETRY_SUCCESS, 0) == 0
    assert resumen["sin_respuesta"] == 22


def test_un_mensaje_recuperado_deja_de_estar_pendiente():
    seguimiento = RetryTracker()
    seguimiento.fallo(WAMID, "mac", origen="primary_phone")
    seguimiento.acuse_enviado(WAMID)
    seguimiento.recuperado(WAMID)

    assert seguimiento.resumen()["sin_resolver"] == 0


def test_el_seguimiento_no_guarda_ni_un_byte_de_contenido():
    """Es telemetría de transporte, no una copia del mensaje."""
    seguimiento = RetryTracker()
    seguimiento.fallo(WAMID, "mac", origen="primary_phone")

    for fila in seguimiento.pendientes():
        assert "text" not in fila
        assert "body" not in fila
        assert "plaintext" not in fila


# ---------------------------------------------------------------------------
# El agujero que deja un mensaje perdido
# ---------------------------------------------------------------------------


def test_un_mensaje_en_vivo_perdido_deja_un_agujero_detectable():
    """La red de seguridad.

    El receptor ya pasó de largo y la excavación histórica va hacia atrás desde
    el ancla, así que nunca alcanza el borde. Si nadie lo detecta, ese mensaje
    no lo cierra nada.
    """
    assert (
        hueco_por_live_perdido(
            fallos_sin_recuperar=2, db_mas_nuevo=DB_NEWEST, web_mas_nuevo=WEB_NEWEST
        )
        is True
    )


def test_sin_fallos_no_hay_agujero_por_esta_via():
    assert (
        hueco_por_live_perdido(
            fallos_sin_recuperar=0, db_mas_nuevo=DB_NEWEST, web_mas_nuevo=WEB_NEWEST
        )
        is False
    )


def test_con_fallos_pero_sin_referencia_mas_nueva_NO_se_pide_nada():
    """No se fabrica un ancla para tapar un agujero.

    Que sepamos que falta algo no autoriza a inventar desde dónde pedirlo.
    """
    assert (
        hueco_por_live_perdido(
            fallos_sin_recuperar=5, db_mas_nuevo=WEB_NEWEST, web_mas_nuevo=DB_NEWEST
        )
        is False
    )


def test_el_relleno_dice_por_que_via_se_detecto():
    """Mismo motor, orígenes distintos: hace falta para medirlos por separado."""
    assert ResultadoDeRelleno().to_json()["detected_by"] == POR_INDICE_WEB
    assert (
        ResultadoDeRelleno(origen=POR_LIVE_PERDIDO).to_json()["detected_by"]
        == POR_LIVE_PERDIDO
    )
    assert POR_LIVE_PERDIDO != POR_INDICE_WEB


def test_un_agujero_por_live_perdido_se_rellena_con_el_mismo_motor():
    decision = decidir_borde(
        hay_hueco=True,
        ancla_utilizable=True,
        capacidad="CONFIRMED",
        peticion_viva=False,
        por_live_perdido=True,
    )
    assert decision.accion == RELLENAR_BORDE
    assert "vivo" in decision.motivo


def test_ni_siquiera_un_agujero_conocido_se_pide_con_el_motor_en_duda():
    """La seguridad no se ablanda porque sepamos que falta algo."""
    decision = decidir_borde(
        hay_hueco=True,
        ancla_utilizable=True,
        capacidad="SUSPECT",
        peticion_viva=False,
        por_live_perdido=True,
    )
    assert decision.accion == BORDE_EN_ESPERA


# ---------------------------------------------------------------------------
# La seguridad, que no se toca
# ---------------------------------------------------------------------------


def test_no_se_toca_site_packages():
    """La corrección envuelve el método, como el resto de adaptaciones."""
    import inspect

    from app.compat import retry_observer

    fuente = inspect.getsource(retry_observer)
    assert "site-packages" not in fuente.replace("``site-packages``", "")
    assert "receiver_module.Receiver._send_retry_receipt =" in fuente


@pytest.mark.parametrize(
    "prohibido",
    [
        "mac_check = False",
        "skip_mac",
        "verify=False",
        "copy_session",
        "copiar_sesion",
    ],
)
def test_nada_de_lo_nuevo_ablanda_Signal(prohibido):
    """Un MAC que falla sigue fallando, y las sesiones no se copian.

    PN y LID del mismo aparato son direcciones criptográficas distintas: una
    tiene su ratchet y la otra el suyo. Resolver que son la misma persona NO
    autoriza a mezclarlos.
    """
    import inspect

    from app.compat import retry_observer
    from app.history import recent_gap

    for modulo in (retry_observer, recent_gap):
        assert prohibido not in inspect.getsource(modulo)


def test_el_contador_no_persiste_nada():
    """Es estado efímero: no hizo falta ninguna migración.

    Un acuse de hace tres horas no sirve de nada, y guardarlo sería una tabla
    que hay que podar.
    """
    seguimiento = RetryTracker()
    assert not hasattr(seguimiento, "_database")
    assert not hasattr(seguimiento, "session")
