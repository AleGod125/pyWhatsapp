"""El multimedia SALIENTE llega entero, no solo el mensaje.

EL FALLO
--------
Tras arreglar el enrutado, los textos salientes aparecian bien pero las
imagenes salian como "Imagen no disponible" y las notas de voz como "Mensaje
no compatible". El mensaje se guardaba con chat correcto, ``from_me=True`` y
tipo correcto; lo que faltaba era la fila de ``media_files``, asi que no habia
nada que descargar.

La causa estaba en una linea, repetida en los dos registradores::

    Message.chat_jid == jid_to_string(message.chat)

``message.chat`` es lo que dice pywhats, o sea NUESTRO identificador, porque un
mensaje saliente llega como copia desde nuestro propio dispositivo. La fila ya
se habia guardado bajo el chat del DESTINATARIO, asi que la busqueda no
encontraba nada, se devolvia ``False`` y el adjunto desaparecia sin un error.

Y no faltaba ningun dato. En la traza del protobuf real estaban ``media_key``,
``direct_path``, los dos hashes, la ``url``, la miniatura y el ``ptt``.

Estas pruebas no tocan Signal ni el protocolo: solo el registro del adjunto.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import MediaFile
from tests.test_live_types import FakeDatabase, MediaFalsa, _fila
from tests.test_outgoing_routing import (
    ISAAC,
    ISAAC_LID,
    OWN_LID,
    OWN_PN,
    entrante,
    envuelto,
    evento,
)

from app.services.live_service import LiveMessageService


@pytest.fixture
def servicio(session):
    return LiveMessageService(FakeDatabase(session), own_jid=OWN_PN, own_lid=OWN_LID)


@pytest.fixture
def crudo(monkeypatch):
    import app.compat.protocol_flag as protocol_flag

    def poner(datos: bytes):
        monkeypatch.setattr(protocol_flag, "last_raw_message", lambda: datos)

    return poner


def _media(session, wamid: str):
    return session.execute(
        select(MediaFile).where(MediaFile.whatsapp_message_id == wamid)
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# El adjunto se registra, y en el chat correcto
# ---------------------------------------------------------------------------


def test_la_imagen_saliente_deja_fila_de_adjunto(servicio, session, crudo):
    """Esto es lo que faltaba: sin fila no hay descarga y sale "no disponible"."""
    crudo(envuelto(ISAAC_LID, imagen=True))
    resultado = servicio.handle(evento(id="MEDIAOUT001"))
    session.flush()

    fila = _media(session, "MEDIAOUT001")
    assert fila is not None, "el adjunto saliente tiene que registrarse"
    assert fila.media_type == "image"
    assert fila.download_status == "pending"
    assert fila.chat_id == resultado["chat_id"], (
        "el adjunto va al chat del destinatario, igual que el mensaje"
    )


def test_la_nota_de_voz_saliente_deja_fila_de_adjunto(servicio, session, crudo):
    crudo(envuelto(ISAAC_LID, audio_ptt=True))
    servicio.handle(evento(id="MEDIAOUT002"))
    session.flush()

    fila = _media(session, "MEDIAOUT002")
    assert fila is not None
    assert fila.media_type == "voice_note", (
        "una nota de voz es un audioMessage con ptt; el tipo tiene que "
        "coincidir con el del camino entrante"
    )


def test_el_adjunto_cuelga_del_mensaje_correcto(servicio, session, crudo):
    """La busqueda iba por ``message.chat``, que somos nosotros: no casaba."""
    crudo(envuelto(ISAAC_LID, imagen=True))
    servicio.handle(evento(id="MEDIAOUT003"))
    session.flush()

    mensaje = _fila(session, "MEDIAOUT003")
    adjunto = _media(session, "MEDIAOUT003")
    assert adjunto.message_id == mensaje.id


def test_no_se_pierden_los_datos_de_descarga(servicio, session, crudo):
    """media_key, direct_path y los hashes son lo que permite bajar el archivo.

    Se comprueba su PRESENCIA, nunca su valor: son material sensible y no
    aparecen ni en el log ni aqui.
    """
    from tests.test_outgoing_routing import mensaje_interno
    from pywhats.proto import Message as WAMessage

    interno = WAMessage()
    interno.image_message.mimetype = "image/jpeg"
    interno.image_message.media_key = b"k" * 32
    interno.image_message.direct_path = "/v/t62.7118-24/abc"
    interno.image_message.file_sha256 = b"s" * 32
    interno.image_message.file_enc_sha256 = b"e" * 32
    interno.image_message.file_length = 143766

    envoltorio = WAMessage()
    envoltorio.device_sent_message.destination_jid = ISAAC_LID
    envoltorio.device_sent_message.message.CopyFrom(interno)

    crudo(envoltorio.SerializeToString())
    servicio.handle(evento(id="MEDIAOUT004"))
    session.flush()

    fila = _media(session, "MEDIAOUT004")
    assert fila.media_key is not None
    assert fila.direct_path is not None
    assert fila.file_sha256 is not None
    assert fila.file_enc_sha256 is not None
    assert fila.file_size == 143766
    assert fila.mime_type == "image/jpeg"


def test_la_duracion_de_la_nota_de_voz_se_conserva(servicio, session, crudo):
    from pywhats.proto import Message as WAMessage

    interno = WAMessage()
    interno.audio_message.mimetype = "audio/ogg; codecs=opus"
    interno.audio_message.ptt = True
    interno.audio_message.seconds = 2
    interno.audio_message.media_key = b"k" * 32
    interno.audio_message.direct_path = "/v/t62/abc"

    envoltorio = WAMessage()
    envoltorio.device_sent_message.destination_jid = ISAAC_LID
    envoltorio.device_sent_message.message.CopyFrom(interno)

    crudo(envoltorio.SerializeToString())
    servicio.handle(evento(id="MEDIAOUT005"))
    session.flush()

    fila = _media(session, "MEDIAOUT005")
    assert fila.duration_seconds == 2


# ---------------------------------------------------------------------------
# El camino entrante no se toca
# ---------------------------------------------------------------------------


def test_el_multimedia_entrante_sigue_igual(servicio, session, crudo):
    """Control: pywhats SI sabe extraer el adjunto de un mensaje entrante."""
    crudo(entrante(imagen=True))
    servicio.handle(
        evento(
            id="MEDIAIN001",
            chat=ISAAC,
            sender=ISAAC,
            media=MediaFalsa("image", mimetype="image/jpeg"),
        )
    )
    session.flush()

    fila = _media(session, "MEDIAIN001")
    assert fila is not None
    assert fila.media_type == "image"
    assert fila.download_status == "pending"


def test_un_texto_no_registra_ningun_adjunto(servicio, session, crudo):
    crudo(envuelto(ISAAC_LID, texto="hola"))
    servicio.handle(evento(id="MEDIAOUT006", text="hola"))
    session.flush()

    assert _media(session, "MEDIAOUT006") is None


# ---------------------------------------------------------------------------
# El reparador de los que ya se guardaron sin adjunto
# ---------------------------------------------------------------------------


def test_el_reparador_encuentra_los_adjuntos_sin_fila(session, servicio, crudo, monkeypatch):
    """Los que se perdieron antes del arreglo se recuperan del protobuf."""
    import app.services.live_service as live_service
    from scripts.repair_missing_media import aplicar, auditar

    # Se reproduce el fallo: el registro del adjunto no llega a ocurrir.
    monkeypatch.setattr(
        live_service.LiveMessageService,
        "_register_parsed_media",
        lambda *a, **k: False,
    )
    crudo(envuelto(ISAAC_LID, imagen=True))
    servicio.handle(evento(id="MEDIAREP001"))
    session.flush()
    assert _media(session, "MEDIAREP001") is None, "asi quedaba antes del arreglo"

    candidatos = [c for c in auditar(session) if c.whatsapp_message_id == "MEDIAREP001"]
    assert len(candidatos) == 1
    assert candidatos[0].media_type == "image"

    aplicar(session, candidatos)
    session.flush()

    recuperado = _media(session, "MEDIAREP001")
    assert recuperado is not None
    assert recuperado.download_status == "pending"


def test_el_reparador_no_inventa_adjuntos_para_los_textos(session, servicio, crudo):
    """Un texto no tiene adjunto; el reparador no puede fabricarle uno."""
    from scripts.repair_missing_media import auditar

    crudo(envuelto(ISAAC_LID, texto="hola"))
    servicio.handle(evento(id="MEDIAREP002", text="hola"))
    session.flush()

    assert not [
        c for c in auditar(session) if c.whatsapp_message_id == "MEDIAREP002"
    ]


def test_el_reparador_no_toca_los_que_ya_tienen_fila(session, servicio, crudo):
    from scripts.repair_missing_media import auditar

    crudo(envuelto(ISAAC_LID, imagen=True))
    servicio.handle(evento(id="MEDIAREP003"))
    session.flush()
    assert _media(session, "MEDIAREP003") is not None

    assert not [
        c for c in auditar(session) if c.whatsapp_message_id == "MEDIAREP003"
    ]


# ---------------------------------------------------------------------------
# Nada sensible en los logs
# ---------------------------------------------------------------------------


def test_el_registro_no_escribe_material_sensible(servicio, session, crudo, caplog):
    """Ni media_key, ni hashes, ni direct_path: solo si estan."""
    import logging

    crudo(envuelto(ISAAC_LID, imagen=True))
    with caplog.at_level(logging.DEBUG):
        servicio.handle(evento(id="MEDIALOG001"))
    session.flush()

    texto = "\n".join(r.getMessage() for r in caplog.records)
    for prohibido in ("media_key=", "file_enc_sha256=", "direct_path=/"):
        assert prohibido not in texto


def test_el_reparador_no_imprime_material_sensible(capsys):
    """El informe dice si el dato ESTA, nunca cual es."""
    from pathlib import Path

    fuente = Path("scripts/repair_missing_media.py").read_text(encoding="utf-8")
    # Se busca en las cadenas que se imprimen, no en la prosa del docstring.
    impresiones = [
        linea for linea in fuente.splitlines() if linea.strip().startswith("print(")
    ]
    unidas = " ".join(impresiones)
    assert "media.media_key" not in unidas
    assert "media.direct_path" not in unidas
    assert "tiene_clave" in fuente, "se informa de la PRESENCIA, no del valor"
