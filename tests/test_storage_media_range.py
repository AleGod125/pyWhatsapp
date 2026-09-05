"""Servir multimedia por rangos, desde la cache o desde Drive.

Un video de 2 GB no puede descargarse entero para entregar diez segundos. El
rango se traduce a los trozos cifrados que lo cubren y solo se piden esos.
"""

from __future__ import annotations

import io
import os
import uuid

import pytest

from app.storage.encryption import CHUNK_PLAIN_BYTES, StorageEncryption
from app.storage.media import MediaStorage
from tests.test_storage_pipeline import DriveFalso

CLAVE = "una contrasena larga"


class DriveContado(DriveFalso):
    """Cuenta cuantos bytes se han pedido, para demostrar que no se baja todo."""

    def __init__(self):
        super().__init__()
        self.bytes_pedidos = 0

    def read_range(self, file_id, inicio, fin):
        datos = super().read_range(file_id, inicio, fin)
        self.bytes_pedidos += len(datos)
        return datos


@pytest.fixture
def video(runtime, session, settings, tmp_path):
    """Un adjunto de 2,5 MB ya cifrado y 'subido'."""
    import dataclasses

    from app.models import Chat, MediaFile, Message, WhatsAppAccount
    from app.storage.service import StorageService

    runtime._montar_cuentas()
    inicio = runtime.auth.register(
        email=f"r-{uuid.uuid4().hex[:8]}@example.com", password=CLAVE
    )
    cuenta = WhatsAppAccount(
        user_id=inicio.user_id, session_status="linked",
        session_storage_key=f"users/{inicio.user_id}",
    )
    session.add(cuenta)
    session.flush()
    chat = Chat(jid=f"5732{uuid.uuid4().hex[:8]}@s.whatsapp.net",
                chat_type="individual", whatsapp_account_id=cuenta.id)
    session.add(chat)
    session.flush()
    mensaje = Message(chat_id=chat.id, chat_jid=chat.jid, timestamp=1_760_000_000,
                      from_me=False, message_type="video", source="live")
    session.add(mensaje)
    session.flush()

    ajustes = dataclasses.replace(settings, media_dir=tmp_path / "media")
    (tmp_path / "media").mkdir(parents=True, exist_ok=True)
    almacenamiento = StorageService(runtime.database, ajustes)

    original = os.urandom(int(CHUNK_PLAIN_BYTES * 2.5))
    cifrado = almacenamiento.cifrado_de(inicio.user_id)
    partes = list(cifrado.encrypt_stream(io.BytesIO(original), plaintext_size=len(original)))

    drive = DriveContado()
    drive.archivos["video-1"] = b"".join(partes)

    fila = MediaFile(
        message_id=mensaje.id, chat_id=chat.id, media_type="video",
        mime_type="video/mp4", file_size=len(original),
        download_status="downloaded", storage_status="ready",
        drive_file_id="video-1", stored_bytes=len(b"".join(partes)),
    )
    session.add(fila)
    session.flush()

    return {
        "fila": fila, "original": original, "drive": drive,
        "cache": MediaStorage(runtime.database, ajustes, almacenamiento),
        "user_id": inicio.user_id,
    }


def test_un_rango_devuelve_exactamente_esos_bytes(video):
    inicio, fin = 1000, 1999
    trozo = video["cache"].leer_rango(
        video["fila"], inicio=inicio, fin=fin,
        almacenamiento=video["drive"], user_id=video["user_id"],
    )
    assert trozo.datos == video["original"][inicio : fin + 1]
    assert trozo.total == len(video["original"])


def test_no_se_descarga_el_archivo_entero(video):
    """La razon de ser del troceado."""
    completo = len(video["drive"].archivos["video-1"])
    video["cache"].leer_rango(
        video["fila"], inicio=100, fin=1099,
        almacenamiento=video["drive"], user_id=video["user_id"],
    )
    assert video["drive"].bytes_pedidos < completo / 2, (
        f"se pidieron {video['drive'].bytes_pedidos} de {completo} bytes"
    )


def test_un_rango_a_caballo_entre_dos_trozos(video):
    inicio = CHUNK_PLAIN_BYTES - 500
    fin = CHUNK_PLAIN_BYTES + 500
    trozo = video["cache"].leer_rango(
        video["fila"], inicio=inicio, fin=fin,
        almacenamiento=video["drive"], user_id=video["user_id"],
    )
    assert trozo.datos == video["original"][inicio : fin + 1]


def test_el_ultimo_trozo_esta_completo(video):
    total = len(video["original"])
    trozo = video["cache"].leer_rango(
        video["fila"], inicio=total - 100, fin=total - 1,
        almacenamiento=video["drive"], user_id=video["user_id"],
    )
    assert trozo.datos == video["original"][-100:]


def test_content_range_tiene_la_forma_correcta(video):
    trozo = video["cache"].leer_rango(
        video["fila"], inicio=0, fin=99,
        almacenamiento=video["drive"], user_id=video["user_id"],
    )
    assert trozo.content_range == f"bytes 0-99/{len(video['original'])}"


def test_la_cache_local_tiene_prioridad(video, tmp_path, settings):
    """Es lo mas rapido y no gasta cupo de Google."""
    ruta = tmp_path / "media" / "local.bin"
    ruta.write_bytes(video["original"])
    video["fila"].local_path = "local.bin"

    trozo = video["cache"].leer_rango(
        video["fila"], inicio=50, fin=149,
        almacenamiento=video["drive"], user_id=video["user_id"],
    )
    assert trozo.datos == video["original"][50:150]
    assert video["drive"].bytes_pedidos == 0, "no se toco Drive"


def test_sin_cache_ni_drive_se_dice_claramente(video):
    video["fila"].drive_file_id = None
    with pytest.raises(FileNotFoundError):
        video["cache"].leer_rango(
            video["fila"], inicio=0, fin=10,
            almacenamiento=video["drive"], user_id=video["user_id"],
        )


def test_otro_usuario_no_puede_descifrarlo(video, runtime):
    """La clave es por usuario: con la de otro, el contenido no se abre."""
    from app.storage.encryption import EncryptionError

    otro = runtime.auth.register(
        email=f"x-{uuid.uuid4().hex[:8]}@example.com", password=CLAVE
    )
    with pytest.raises(EncryptionError):
        video["cache"].leer_rango(
            video["fila"], inicio=0, fin=99,
            almacenamiento=video["drive"], user_id=otro.user_id,
        )
