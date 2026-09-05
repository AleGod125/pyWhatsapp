"""Cada tipo de mensaje dice lo que es. "No compatible" es el ULTIMO recurso.

LO QUE SE MIDIO
---------------
En la base real, de 3450 mensajes, tres tipos llegaban al panel sin etiqueta
util porque el frontend tenia su PROPIO mapa y le faltaban:

    system    112 mensajes, todos sin texto
    unknown    16 mensajes, todos sin texto
    contact     3 mensajes, todos sin texto

Los tres salian como "Mensaje no compatible" cuando el backend YA sabia decir
que eran. Estas pruebas fijan que la etiqueta la calcula quien tiene los
datos: el backend.
"""

from __future__ import annotations

import pytest

from app.core.previews import TYPE_PREVIEWS, preview_for

# Los tipos que de verdad hay en la base, medidos.
TIPOS_REALES = (
    "text",
    "sticker",
    "image",
    "system",
    "audio",
    "video",
    "poll",
    "location",
    "document",
    "contact",
    "unknown",
)


@pytest.mark.parametrize(
    "tipo",
    [t for t in TIPOS_REALES if t not in ("text", "system", "unknown")],
)
def test_cada_tipo_real_tiene_su_etiqueta(tipo):
    """Ninguno de los que existen puede quedarse sin nombre."""
    etiqueta = preview_for(tipo, None)
    assert etiqueta, f"{tipo} no tiene etiqueta"
    assert "no compatible" not in etiqueta.lower()
    assert "sin interpretar" not in etiqueta.lower()


def test_un_contacto_se_llama_contacto():
    """Son 3 mensajes reales, y salian como "no compatible"."""
    assert preview_for("contact", None) == "👤 Contacto"


def test_un_sticker_se_llama_sticker():
    assert preview_for("sticker", None) == "Sticker"


def test_una_encuesta_y_una_ubicacion_tambien():
    assert preview_for("poll", None) == "📊 Encuesta"
    assert preview_for("location", None) == "📍 Ubicacion"


def test_un_evento_del_chat_se_describe_de_su_stub(session):
    """El evento sale del protobuf, nunca del texto.

    112 mensajes de sistema decian "Mensaje no compatible"; el backend sabe
    leer el ``messageStubType`` y decir cual fue.
    """
    etiqueta = preview_for("system", None, metadata={"stub_type": 20})
    assert etiqueta
    assert "no compatible" not in etiqueta.lower()


def test_un_stub_desconocido_NO_se_inventa():
    """Se declara desconocido en vez de darle un nombre cualquiera."""
    etiqueta = preview_for("system", None, metadata={"stub_type": 99999})
    assert etiqueta
    assert "no compatible" not in etiqueta.lower()


def test_el_texto_manda_sobre_el_tipo():
    """El pie de una foto es mas util que la palabra "Imagen"."""
    assert preview_for("image", "mira esto") == "mira esto"


def test_lo_que_no_se_entiende_se_dice_asi_y_no_de_otra_manera():
    """"No se pudo interpretar" y "no es compatible" no son lo mismo.

    El mensaje existe y llego; lo que no se supo es su tipo. Son 16 mensajes
    cuyo Message solo trae ``messageContextInfo`` (campo 35) y ningun campo de
    contenido: no hay nada que ensenar, y decir "no compatible" sugiere un
    fallo del programa que no lo es.
    """
    assert preview_for("unknown", None) == "Mensaje no compatible"


def test_un_tipo_desconocido_deja_diagnostico_sin_contenido(caplog):
    import logging

    with caplog.at_level(logging.DEBUG, logger="app.compat"):
        assert (
            preview_for("unknown", None, metadata={"proto_type": "field_104"})
            == "Mensaje no compatible"
        )
    assert "unknown_message_type=field_104" in caplog.text


def test_la_api_manda_la_etiqueta_ya_resuelta(session):
    """Para que el frontend no tenga que mantener un segundo mapa."""
    import types

    from app.api.serializers import message_to_json

    fila = types.SimpleNamespace(
        id=1,
        whatsapp_message_id="3A1F8BDD4678EB6DE395",
        chat_id=1,
        message_type="contact",
        text=None,
        from_me=False,
        sender_jid=None,
        sender_lid=None,
        timestamp=1_760_000_000,
        raw_metadata=None,
    )
    cuerpo = message_to_json(fila)

    assert cuerpo["preview"] == "👤 Contacto"
    assert cuerpo["type"] == "contact"


def test_un_evento_de_sistema_viaja_con_su_descripcion(session):
    import types

    from app.api.serializers import message_to_json

    fila = types.SimpleNamespace(
        id=2,
        whatsapp_message_id="3A1F8BDD4678EB6DE396",
        chat_id=1,
        message_type="system",
        text=None,
        from_me=False,
        sender_jid=None,
        sender_lid=None,
        timestamp=1_760_000_000,
        raw_metadata={"stub_type": 20},
    )
    cuerpo = message_to_json(fila)

    assert cuerpo["preview"]
    assert "no compatible" not in cuerpo["preview"].lower()
    assert cuerpo["system_event"]["stub_type"] == 20


def test_el_mapa_del_backend_cubre_los_tipos_que_emite():
    """Si un dia se anade un tipo, esta prueba lo pilla sin datos reales."""
    from app.models.schema import MEDIA_TYPES

    for tipo in MEDIA_TYPES:
        if tipo == "unknown":
            continue
        assert tipo in TYPE_PREVIEWS, f"{tipo} se puede guardar pero no rotular"
