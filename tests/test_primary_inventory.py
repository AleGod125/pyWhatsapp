"""El inventario que la sesión principal ya recibía y estaba tirando.

EL HALLAZGO, MEDIDO SOBRE EL CABLE
----------------------------------
Se creía que la sesión principal «descubría menos» y que por eso hacía falta un
segundo dispositivo. No es eso: la información viene y se descarta.

``pywhats`` modela cinco campos de una ``Conversation`` — ``id``, ``messages``,
``name``, ``last_msg_timestamp``, ``unread_count``— y el cable trae treinta y
uno. Escaneando los bytes del blob real, en las 41 conversaciones aparecen
además la marca de actividad (campo 12, en las 41), el asunto del grupo (13),
el push name (38/43) y **el par PN↔LID** (39 y 49, en 34 de 41).

LO QUE ESTAS PRUEBAS PROTEGEN
-----------------------------
Sobre todo lo que **no** hay: anclas. Para una conversación sin mensajes el
cable no trae ningún identificador de mensaje, y sin ancla no se puede pedir
historial. Es el dato que decide si se puede quitar el segundo dispositivo, y
tiene que quedar imposible de confundir con un descuido.
"""

from __future__ import annotations

import pathlib

import pytest

from app.discovery.primary_inventory import (
    ConversacionDescubierta,
    leer_bootstrap,
    leer_conversacion,
)


# ---------------------------------------------------------------------------
# Construir protobuf a mano, para no depender de un blob concreto
# ---------------------------------------------------------------------------


def _varint(valor: int) -> bytes:
    salida = bytearray()
    while True:
        b = valor & 0x7F
        valor >>= 7
        salida.append(b | (0x80 if valor else 0))
        if not valor:
            return bytes(salida)


def _campo(numero: int, valor) -> bytes:
    if isinstance(valor, int):
        return _varint(numero << 3 | 0) + _varint(valor)
    crudo = valor.encode("utf-8") if isinstance(valor, str) else valor
    return _varint(numero << 3 | 2) + _varint(len(crudo)) + crudo


def _conversacion(**campos) -> bytes:
    orden = (
        ("id", 1),
        ("mensajes", 2),
        ("nombre", 3),
        ("ultimo_ts", 5),
        ("no_leidos", 6),
        ("actividad", 12),
        ("asunto", 13),
        ("push_name", 38),
        ("pn", 39),
        ("push_name_alt", 43),
        ("lid", 49),
    )
    salida = b""
    for clave, numero in orden:
        if clave in campos and campos[clave] is not None:
            salida += _campo(numero, campos[clave])
    return salida


def _bootstrap(*conversaciones: bytes) -> bytes:
    # Campo 1 = sync_type, campo 2 = conversations.
    return _campo(1, 0) + b"".join(_campo(2, c) for c in conversaciones)


# ---------------------------------------------------------------------------
# Lo que SÍ se saca
# ---------------------------------------------------------------------------


def test_se_lee_una_conversacion_con_lo_que_pywhats_descarta():
    crudo = _conversacion(
        id="573001112233@lid",
        actividad=1_788_621_789,
        pn="573001112233@s.whatsapp.net",
        lid="156595061317693@lid",
        push_name="@macrinaesther",
    )
    conversacion = leer_conversacion(crudo)

    assert conversacion is not None
    assert conversacion.raw_jid == "573001112233@lid"
    assert conversacion.last_timestamp == 1_788_621_789
    assert conversacion.pn_jid == "573001112233@s.whatsapp.net"
    assert conversacion.lid_jid == "156595061317693@lid"
    assert conversacion.name == "@macrinaesther"


def test_el_par_PN_LID_viene_en_la_misma_conversacion():
    """Es lo que costó fases enteras resolver por otras vías."""
    conversacion = leer_conversacion(
        _conversacion(
            id="1@lid", pn="573008844022@s.whatsapp.net", lid="156595061317693@lid"
        )
    )
    assert conversacion.pn_jid and conversacion.lid_jid
    assert conversacion.pn_jid != conversacion.lid_jid


def test_el_asunto_del_grupo_gana_al_push_name():
    """El nombre de la conversación, no el que el contacto se puso."""
    conversacion = leer_conversacion(
        _conversacion(id="120@g.us", asunto="Familia Navarro", push_name="@alguien")
    )
    assert conversacion.name == "Familia Navarro"
    assert conversacion.is_group is True


def test_un_grupo_se_reconoce_por_su_identificador():
    assert leer_conversacion(_conversacion(id="120363@g.us")).is_group is True
    assert leer_conversacion(_conversacion(id="5730011@lid")).is_group is False


def test_se_queda_con_la_marca_mas_reciente():
    conversacion = leer_conversacion(
        _conversacion(id="1@lid", ultimo_ts=1_780_000_000, actividad=1_788_000_000)
    )
    assert conversacion.last_timestamp == 1_788_000_000


# ---------------------------------------------------------------------------
# Lo que NO se inventa
# ---------------------------------------------------------------------------


def test_sin_nombre_se_queda_sin_nombre():
    """Inventar aquí sería inventar en la fuente de la que se fía todo."""
    assert leer_conversacion(_conversacion(id="1@lid")).name is None


def test_una_marca_en_milisegundos_se_descarta():
    """No se divide por mil: adivinar la unidad produce un cursor muerto."""
    conversacion = leer_conversacion(
        _conversacion(id="1@lid", actividad=1_788_000_000_000)
    )
    assert conversacion.last_timestamp is None


def test_sin_identificador_no_hay_conversacion():
    assert leer_conversacion(_conversacion(actividad=1_788_000_000)) is None


def test_algo_que_no_es_un_JID_no_se_toma_por_uno():
    assert leer_conversacion(_conversacion(id="no-es-un-jid")) is None


def test_unos_bytes_rotos_no_tumban_el_inventario():
    """Perder las cuarenta por un byte raro sería peor que perder una."""
    bueno = _conversacion(id="1@lid", actividad=1_788_000_000)
    salida = leer_bootstrap(_bootstrap(bueno) + b"\xff\xff\xff")
    assert len(salida.conversaciones) == 1


# ---------------------------------------------------------------------------
# LA pregunta: ¿hay anclas?
# ---------------------------------------------------------------------------


def test_NO_hay_ninguna_ancla_y_eso_decide_la_arquitectura():
    """El dato que dice si se puede quitar el segundo dispositivo.

    Sin ancla no se puede pedir historial. El cable no trae identificadores de
    mensaje para conversaciones sin mensajes —los campos opacos son de 11 y 32
    bytes, y un WAMID son 20 o 32 caracteres hexadecimales—, así que esta vía
    sustituye a WhatsApp Web como DESCUBRIDOR y no como fuente de referencias.

    Si algún día apareciera una, este test es el que hay que cambiar, y a
    conciencia.
    """
    salida = leer_bootstrap(
        _bootstrap(
            _conversacion(id="1@lid", actividad=1_788_000_000, pn="5@s.whatsapp.net"),
            _conversacion(id="2@lid", actividad=1_788_000_001),
        )
    )
    assert salida.con_ancla == 0
    assert salida.to_json()["with_seed"] == 0


def test_el_resumen_cuenta_lo_que_hay_y_lo_que_falta():
    salida = leer_bootstrap(
        _bootstrap(
            _conversacion(
                id="1@lid", actividad=1_788_000_000, pn="5@s.whatsapp.net", lid="1@lid"
            ),
            _conversacion(id="120@g.us", asunto="Familia", actividad=1_788_000_001),
            _conversacion(id="3@lid"),
        )
    )
    datos = salida.to_json()
    assert datos["primary_chats"] == 3
    assert datos["groups"] == 1
    assert datos["individuals"] == 2
    assert datos["with_last_activity"] == 2
    assert datos["with_pn_lid_pair"] == 1
    assert datos["with_name"] == 1


def test_no_se_repite_una_conversacion():
    salida = leer_bootstrap(
        _bootstrap(_conversacion(id="1@lid"), _conversacion(id="1@lid"))
    )
    assert len(salida.conversaciones) == 1


# ---------------------------------------------------------------------------
# Contra el blob de verdad, si está a mano
# ---------------------------------------------------------------------------


def test_contra_el_bootstrap_real():
    """La medición que sostiene el informe.

    Se salta si no hay blob archivado: es una comprobación sobre datos de una
    sesión concreta, no un requisito de la suite.
    """
    blobs = sorted(pathlib.Path("data/history").glob("*INITIAL_BOOTSTRAP*.pb"))
    if not blobs:
        pytest.skip("no hay ningun bootstrap archivado")

    salida = leer_bootstrap(blobs[0].read_bytes())

    assert len(salida.conversaciones) > 0
    # Todas con marca de actividad: es el campo 12, y viene siempre.
    assert salida.con_actividad == len(salida.conversaciones)
    # Y ninguna con ancla. Es el dato que decide.
    assert salida.con_ancla == 0


def test_la_confianza_dice_cuanto_se_sabe():
    """Sirve para priorizar, no para descartar."""
    assert (
        ConversacionDescubierta(raw_jid="1@lid", name="Ana", last_timestamp=1).confidence
        == "alta"
    )
    assert ConversacionDescubierta(raw_jid="1@lid", last_timestamp=1).confidence == "media"
    assert ConversacionDescubierta(raw_jid="1@lid").confidence == "baja"


def test_no_se_toca_site_packages():
    """Se leen los campos por número; no se reescribe el modelo de nadie."""
    import inspect

    from app.discovery import primary_inventory

    # Se mira el CODIGO, no la documentacion: el docstring habla de
    # site-packages justamente para decir que no se toca.
    fuente = inspect.getsource(primary_inventory)
    codigo = fuente.split('"""', 2)[-1]
    assert "site-packages" not in codigo
    assert "DESCRIPTOR" not in codigo
    assert "import pywhats" not in codigo
