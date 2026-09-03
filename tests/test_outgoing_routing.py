"""Un mensaje que YO envio desde el telefono va al destinatario, no a mi.

EL FALLO
--------
WhatsApp reparte una copia de lo que envias a cada dispositivo vinculado. Esa
copia llega con NUESTRO propio identificador como remitente, asi que pywhats
la archivaba en el chat con uno mismo (``receiver.py:394``:
``chat_jid = sender_jid``) y la marcaba recibida (``from_me=False``, fijo en
las lineas 512 y 587). Tres mensajes para Isaac acabaron en el chat personal.

El destinatario venia escrito en el protobuf todo el rato::

    top-level fields = [(31, device_sent_message), (35, ?)]
      destination_jid = 64940106866902@lid
      inner message   = [(6, extended_text_message)]

En la misma tanda medida hay una imagen con ``destination_jid`` igual a la
identidad propia: esa SI es una nota para uno mismo y su sitio es el chat
personal. Por eso la regla no es "todo lo saliente fuera de aqui" sino "al
destino que declara el protobuf".

Nada de esto toca Signal: ocurre despues del descifrado, sobre bytes en claro.
"""

from __future__ import annotations

import pytest

from pywhats.events import JID, Message

from app.core.device_sent import route, unwrap
from app.services.live_service import LiveMessageService

from tests.test_live_types import FakeDatabase, _fila

OWN_PN = "573002389304@s.whatsapp.net"
OWN_LID = "86531142340710@lid"
OWN_JID = JID(user="86531142340710", server="lid")
ISAAC_LID = "64940106866902@lid"
ISAAC = JID(user="64940106866902", server="lid")
GRUPO_JID = "120363111222333444@g.us"


# ---------------------------------------------------------------------------
# Construccion de protobufs con el DESCRIPTOR REAL del paquete instalado.
# Nada de bytes a mano: si pywhats cambiara la forma, estas pruebas se enteran.
# ---------------------------------------------------------------------------


def mensaje_interno(
    *, texto: str | None = None, imagen: bool = False, audio_ptt: bool = False
) -> bytes:
    from pywhats.proto import Message as WAMessage

    proto = WAMessage()
    if texto is not None:
        proto.extended_text_message.text = texto
    elif imagen:
        proto.image_message.mimetype = "image/jpeg"
    elif audio_ptt:
        proto.audio_message.mimetype = "audio/ogg"
        proto.audio_message.ptt = True
    return proto.SerializeToString()


def envuelto(destino: str, **interno) -> bytes:
    """``DeviceSentMessage`` real: campo 31, con destino y mensaje dentro."""
    from pywhats.proto import Message as WAMessage

    proto = WAMessage()
    proto.device_sent_message.destination_jid = destino
    proto.device_sent_message.message.ParseFromString(mensaje_interno(**interno))
    return proto.SerializeToString()


def entrante(**interno) -> bytes:
    return mensaje_interno(**interno)


def evento(**kwargs) -> Message:
    """El evento tal y como lo emite pywhats: remitente = nosotros mismos."""
    base = dict(
        id="3EB0OUTGOING001",
        chat=OWN_JID,
        sender=OWN_JID,
        text="",
        timestamp=1_788_400_000,
        from_me=False,  # pywhats lo deja fijo; es justo lo que se corrige
        media=None,
        quoted=None,
    )
    base.update(kwargs)
    return Message(**base)


@pytest.fixture
def servicio(session):
    return LiveMessageService(FakeDatabase(session), own_jid=OWN_PN, own_lid=OWN_LID)


@pytest.fixture
def crudo(monkeypatch):
    """Permite fijar los bytes que devuelve ``last_raw_message()``."""
    import app.compat.protocol_flag as protocol_flag

    def poner(datos: bytes):
        monkeypatch.setattr(protocol_flag, "last_raw_message", lambda: datos)

    return poner


# ---------------------------------------------------------------------------
# La forma real
# ---------------------------------------------------------------------------


def test_los_numeros_de_campo_salen_del_descriptor_instalado():
    """No se inventa el 31 ni el 1: se comprueban contra pywhats."""
    from pywhats.proto import Message as WAMessage

    from app.core import device_sent

    campo = WAMessage.DESCRIPTOR.fields_by_number[device_sent.CAMPO_DEVICE_SENT]
    assert campo.name == "device_sent_message"

    dentro = {f.number: f.name for f in campo.message_type.fields}
    assert dentro[device_sent.CAMPO_DESTINO] == "destination_jid"
    assert dentro[device_sent.CAMPO_MENSAJE_INTERNO] == "message"


def test_unwrap_extrae_el_destino_declarado():
    destino, interno = unwrap(envuelto(ISAAC_LID, texto="hola"))
    assert destino == ISAAC_LID
    assert interno  # el mensaje real viaja dentro


def test_un_mensaje_entrante_no_lleva_envoltorio():
    assert unwrap(entrante(texto="hola")) is None


@pytest.mark.parametrize("basura", [None, b"", b"\xff\xff\xff", b"no es protobuf"])
def test_bytes_ilegibles_no_revientan(basura):
    """La recepcion es lo prioritario: ante la duda, no se mueve nada."""
    assert unwrap(basura) is None
    decidido = route(basura, chat_jid=ISAAC_LID)
    assert decidido.chat_jid == ISAAC_LID
    assert decidido.es_saliente is False


# ---------------------------------------------------------------------------
# La decision de enrutado
# ---------------------------------------------------------------------------


def test_saliente_va_al_destino_no_al_remitente():
    decidido = route(
        envuelto(ISAAC_LID, texto="hola"),
        chat_jid=OWN_LID,
        own_identifiers=frozenset({OWN_PN, OWN_LID}),
    )
    assert decidido.chat_jid == ISAAC_LID
    assert decidido.es_saliente is True
    assert decidido.reenrutado is True


def test_saliente_a_uno_mismo_se_queda_en_el_chat_propio():
    """El auto-mensaje es real: hay una imagen medida con este destino."""
    decidido = route(
        envuelto(OWN_LID, imagen=True),
        chat_jid=OWN_LID,
        own_identifiers=frozenset({OWN_PN, OWN_LID}),
    )
    assert decidido.chat_jid == OWN_LID
    assert decidido.es_saliente is True
    assert decidido.es_auto_mensaje is True
    assert decidido.reenrutado is False


def test_el_auto_mensaje_se_reconoce_por_el_otro_identificador():
    """El destino puede venir por telefono aunque el chat sea por LID."""
    decidido = route(
        envuelto(OWN_PN, texto="nota"),
        chat_jid=OWN_LID,
        own_identifiers=frozenset({OWN_PN, OWN_LID}),
    )
    assert decidido.es_auto_mensaje is True


def test_el_sufijo_de_dispositivo_no_confunde_la_identidad():
    decidido = route(
        envuelto("86531142340710.79@lid", texto="nota"),
        chat_jid=OWN_LID,
        own_identifiers=frozenset({OWN_PN, OWN_LID}),
    )
    assert decidido.es_auto_mensaje is True


def test_entrante_conserva_su_conversacion():
    decidido = route(
        entrante(texto="hola"),
        chat_jid=ISAAC_LID,
        own_identifiers=frozenset({OWN_PN, OWN_LID}),
    )
    assert decidido.chat_jid == ISAAC_LID
    assert decidido.es_saliente is False


def test_saliente_a_un_grupo_va_al_grupo():
    """La regla no es "1-1": el destino declarado tambien puede ser un grupo."""
    decidido = route(
        envuelto(GRUPO_JID, texto="hola"),
        chat_jid=OWN_LID,
        own_identifiers=frozenset({OWN_PN, OWN_LID}),
    )
    assert decidido.chat_jid == GRUPO_JID
    assert decidido.es_saliente is True


def test_un_envoltorio_sin_destino_no_mueve_nada():
    """Sin destino declarado se prefiere dejarlo quieto a inventar el chat."""
    from pywhats.proto import Message as WAMessage

    proto = WAMessage()
    proto.device_sent_message.message.ParseFromString(mensaje_interno(texto="x"))
    decidido = route(proto.SerializeToString(), chat_jid=OWN_LID)
    assert decidido.chat_jid == OWN_LID
    assert decidido.es_saliente is False


# ---------------------------------------------------------------------------
# De extremo a extremo, contra la base
# ---------------------------------------------------------------------------


def test_el_texto_saliente_se_guarda_en_el_chat_del_destinatario(
    servicio, session, crudo
):
    crudo(envuelto(ISAAC_LID, texto="OUTGOING_TEST_001"))
    resultado = servicio.handle(evento(id="WAMIDOUT001", text="OUTGOING_TEST_001"))

    assert resultado is not None
    assert resultado["chat_jid"] == ISAAC_LID

    fila = _fila(session, "WAMIDOUT001")
    assert fila.chat_jid == ISAAC_LID
    assert fila.from_me is True


def test_el_entrante_sigue_yendo_a_su_chat(servicio, session, crudo):
    """Control: el arreglo no puede mover lo que ya funcionaba."""
    crudo(entrante(texto="INCOMING_TEST_001"))
    resultado = servicio.handle(
        evento(id="WAMIDIN001", chat=ISAAC, sender=ISAAC, text="INCOMING_TEST_001")
    )

    assert resultado["chat_jid"] == ISAAC_LID
    fila = _fila(session, "WAMIDIN001")
    assert fila.chat_jid == ISAAC_LID
    assert fila.from_me is False


def test_el_auto_mensaje_se_guarda_en_el_chat_propio(servicio, session, crudo):
    crudo(envuelto(OWN_LID, imagen=True))
    resultado = servicio.handle(evento(id="WAMIDSELF001"))

    assert resultado["chat_jid"] == OWN_LID
    fila = _fila(session, "WAMIDSELF001")
    assert fila.chat_jid == OWN_LID
    assert fila.from_me is True


@pytest.mark.parametrize(
    "nombre,interno,tipo",
    [
        ("imagen", {"imagen": True}, "image"),
        ("nota_de_voz", {"audio_ptt": True}, "voice_note"),
    ],
)
def test_el_multimedia_saliente_va_al_destinatario(
    servicio, session, crudo, nombre, interno, tipo
):
    crudo(envuelto(ISAAC_LID, **interno))
    wamid = f"WAMIDOUTMEDIA{nombre.upper()}"
    resultado = servicio.handle(evento(id=wamid))

    assert resultado["chat_jid"] == ISAAC_LID
    fila = _fila(session, wamid)
    assert fila.chat_jid == ISAAC_LID
    assert fila.from_me is True
    assert fila.message_type == tipo


def test_el_saliente_no_se_duplica_si_luego_llega_por_historial(
    servicio, session, crudo
):
    """History Sync y live se solapan a proposito: se deduplica por WAMID."""
    from sqlalchemy import func, select

    from app.models import Message as MessageRow

    crudo(envuelto(ISAAC_LID, texto="repetido"))
    servicio.handle(evento(id="WAMIDDUP001", text="repetido"))
    servicio.handle(evento(id="WAMIDDUP001", text="repetido"))
    session.flush()

    cuantos = session.execute(
        select(func.count())
        .select_from(MessageRow)
        .where(MessageRow.whatsapp_message_id == "WAMIDDUP001")
    ).scalar_one()
    assert cuantos == 1
    assert servicio.stats.duplicates >= 1


def test_los_contadores_separan_las_dos_direcciones(servicio, session, crudo):
    """Que los salientes se perdieran paso inadvertido porque el total subia."""
    crudo(entrante(texto="hola"))
    servicio.handle(evento(id="WAMIDCNT001", chat=ISAAC, sender=ISAAC, text="hola"))

    crudo(envuelto(ISAAC_LID, texto="adios"))
    servicio.handle(evento(id="WAMIDCNT002", text="adios"))

    assert servicio.stats.incoming_seen == 1
    assert servicio.stats.outgoing_seen == 1
    assert servicio.stats.outgoing_rerouted == 1


# ---------------------------------------------------------------------------
# Canonicalizacion PN <-> LID
# ---------------------------------------------------------------------------


def test_un_destino_por_telefono_resuelve_al_chat_por_lid_ya_existente(session):
    """Un contacto NO puede acabar partido en dos conversaciones."""
    from app.models import Chat, Contact
    from app.services.chat_alias import canonical_chat_jid

    # Identificadores ficticios: el chat de Isaac existe de verdad en la base
    # y reutilizarlo chocaria con la restriccion de unicidad.
    session.add(Chat(jid="99900011122@lid", chat_type="individual"))
    session.add(Contact(jid="34600777888@s.whatsapp.net", lid="99900011122@lid"))
    session.flush()

    resuelto = canonical_chat_jid(session, "34600777888@s.whatsapp.net")
    assert resuelto == "99900011122@lid"


def test_sin_correspondencia_conocida_se_respeta_el_identificador(session):
    """No se deduce un LID de un telefono: no son convertibles."""
    from app.services.chat_alias import canonical_chat_jid

    assert (
        canonical_chat_jid(session, "34600111222@s.whatsapp.net")
        == "34600111222@s.whatsapp.net"
    )


def test_un_grupo_nunca_se_traduce(session):
    from app.services.chat_alias import canonical_chat_jid

    assert canonical_chat_jid(session, GRUPO_JID) == GRUPO_JID


def test_el_sufijo_de_dispositivo_se_quita_del_chat(session):
    from app.services.chat_alias import canonical_chat_jid

    assert canonical_chat_jid(session, "64940106866902.3@lid") == "64940106866902@lid"


# ---------------------------------------------------------------------------
# Invariante de regresion
# ---------------------------------------------------------------------------


def test_nunca_se_deduce_la_conversacion_solo_del_remitente(servicio, session, crudo):
    """La invariante, escrita como prueba.

    Si un dia alguien vuelve a enrutar por ``sender_jid``, este test cae: el
    remitente aqui somos nosotros y el destino es otro.
    """
    crudo(envuelto(ISAAC_LID, texto="x"))
    resultado = servicio.handle(evento(id="WAMIDINV001", text="x"))

    assert resultado["chat_jid"] != OWN_LID, (
        "se volvio a enrutar por el remitente en vez de por el destino declarado"
    )
    assert resultado["chat_jid"] == ISAAC_LID
