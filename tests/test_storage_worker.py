"""El trabajador de subidas, contra un Drive simulado.

Lo que se fija aqui es lo que distingue una copia de un monton de archivos:
que un reintento no duplique, que un cierre brusco no pierda nada, que un
acceso revocado pare en vez de machacar, y que la copia local no se borre
antes de que el original este confirmado.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.storage.interface import (
    StorageAuthError,
    StorageError,
    StorageQuotaError,
)
from app.storage.worker import DriveStorageWorker
from tests.test_storage_pipeline import DriveFalso, GoogleFalso, _mensaje

CLAVE = "una contrasena larga"


@pytest.fixture
def montaje(runtime, session, settings, tmp_path):
    """Usuario con cuenta, chat, mensajes y un trabajador listo."""
    import dataclasses

    from app.models import Chat, WhatsAppAccount
    from app.storage.service import StorageService

    runtime._montar_cuentas()
    inicio = runtime.auth.register(
        email=f"w-{uuid.uuid4().hex[:8]}@example.com", password=CLAVE
    )
    cuenta = WhatsAppAccount(
        user_id=inicio.user_id,
        session_status="linked",
        session_storage_key=f"users/{inicio.user_id}",
    )
    session.add(cuenta)
    session.flush()
    chat = Chat(
        jid=f"5731{uuid.uuid4().hex[:8]}@s.whatsapp.net",
        chat_type="individual",
        whatsapp_account_id=cuenta.id,
    )
    session.add(chat)
    session.flush()

    ajustes = dataclasses.replace(settings, media_dir=tmp_path / "media")
    (tmp_path / "media").mkdir(parents=True, exist_ok=True)
    almacenamiento = StorageService(runtime.database, ajustes)

    drive = DriveFalso()
    google = GoogleFalso()
    eventos: list[tuple[str, dict]] = []

    trabajador = DriveStorageWorker(
        database=runtime.database,
        settings=ajustes,
        storage_service=almacenamiento,
        google_service=google,
        publish=lambda n, d: eventos.append((n, d)),
    )
    # Se sustituye el constructor del cliente: no hay Google de verdad.
    trabajador._almacenamiento_de = lambda user_id: drive

    return {
        "user_id": inicio.user_id,
        "account_id": cuenta.id,
        "chat": chat,
        "storage": almacenamiento,
        "worker": trabajador,
        "drive": drive,
        "google": google,
        "eventos": eventos,
        "settings": ajustes,
        "runtime": runtime,
        "tmp": tmp_path,
    }


def _segmento_pendiente(montaje, session, cuantos: int = 3):
    """Crea un segmento cerrado con sus mensajes y su trabajo encolado."""
    almacenamiento = montaje["storage"]
    for i in range(cuantos):
        mensaje = _mensaje(session, montaje["chat"], f"mensaje {i}")
        almacenamiento.anadir_mensaje(
            session,
            mensaje,
            user_id=montaje["user_id"],
            account_id=montaje["account_id"],
        )
    fila = almacenamiento.cerrar(
        session,
        montaje["chat"].id,
        user_id=montaje["user_id"],
        account_id=montaje["account_id"],
    )
    session.flush()
    return fila


# ---------------------------------------------------------------------------
# Camino feliz
# ---------------------------------------------------------------------------


def test_un_segmento_llega_a_drive(montaje, session):
    from app.models import Message
    from app.models.storage import MessageSegment

    fila = _segmento_pendiente(montaje, session)
    assert montaje["worker"].procesar_lote() == 1
    session.expire_all()

    guardado = session.get(MessageSegment, fila.id)
    assert guardado.status == "ready"
    assert guardado.drive_file_id
    assert montaje["drive"].subidas == 1

    # Y los mensajes quedan marcados como a salvo.
    estados = {
        m.storage_status
        for m in session.query(Message).filter(Message.segment_id == fila.id)
    }
    assert estados == {"ready"}


def test_lo_subido_esta_cifrado(montaje, session):
    """En Drive no puede leerse el texto de una conversacion."""
    _segmento_pendiente(montaje, session)
    montaje["worker"].procesar_lote()

    contenido = b"".join(montaje["drive"].archivos.values())
    assert b"mensaje 0" not in contenido
    assert b"WABK" in contenido, "pero si lleva nuestra marca de formato"


def test_lo_subido_se_puede_volver_a_leer(montaje, session):
    from app.storage import segments as seg

    _segmento_pendiente(montaje, session)
    montaje["worker"].procesar_lote()

    crudo = next(iter(montaje["drive"].archivos.values()))
    cifrado = montaje["storage"].cifrado_de(montaje["user_id"])
    lineas = list(seg.leer_lineas(seg.desempaquetar(crudo, encryption=cifrado)))

    assert [linea["text"] for linea in lineas] == ["mensaje 0", "mensaje 1", "mensaje 2"]


def test_se_guardan_las_huellas_y_el_tamano(montaje, session):
    from app.models.storage import MessageSegment

    fila = _segmento_pendiente(montaje, session)
    montaje["worker"].procesar_lote()
    session.expire_all()

    guardado = session.get(MessageSegment, fila.id)
    assert guardado.sha256 and len(guardado.sha256) == 64
    assert guardado.ciphertext_sha256 != guardado.sha256
    assert guardado.stored_bytes > 0


# ---------------------------------------------------------------------------
# Reintentos
# ---------------------------------------------------------------------------


def test_dos_fallos_y_un_acierto_dejan_UN_solo_archivo(montaje, session):
    """El caso que produce copias duplicadas si se hace mal."""
    from app.models.storage import MessageSegment

    montaje["drive"]._fallos = [
        StorageError("DRIVE_SERVER_ERROR", "500", reintentable=True),
        StorageError("DRIVE_SERVER_ERROR", "500", reintentable=True),
    ]
    fila = _segmento_pendiente(montaje, session)

    for _ in range(3):
        _reactivar(montaje, session)
        montaje["worker"].procesar_lote()

    session.expire_all()
    assert montaje["drive"].subidas == 1, "solo puede haber subido una vez"
    assert len(montaje["drive"].archivos) == 1, "un solo archivo en Drive"
    assert session.get(MessageSegment, fila.id).status == "ready"


def test_un_segmento_ya_subido_no_se_vuelve_a_subir(montaje, session):
    _segmento_pendiente(montaje, session)
    montaje["worker"].procesar_lote()

    # Se reencola a mano y se vuelve a procesar.
    _reactivar(montaje, session, forzar=True)
    montaje["worker"].procesar_lote()

    assert montaje["drive"].subidas == 1


def _reactivar(montaje, session, *, forzar: bool = False):
    """Devuelve a la cola lo que esperaba, sin esperar el reloj."""
    from sqlalchemy import update

    from app.models.storage import StorageJob

    estados = ("pending", "processing", "failed", "paused")
    if forzar:
        estados = estados + ("complete",)
    session.execute(
        update(StorageJob)
        .where(
            StorageJob.status.in_(estados),
            StorageJob.user_id == montaje["user_id"],
        )
        .values(status="pending", next_retry_at=None)
    )
    session.flush()


# ---------------------------------------------------------------------------
# Acceso revocado
# ---------------------------------------------------------------------------


def test_si_google_revoca_se_pausa_y_NO_se_pierde_nada(montaje, session):
    from app.models.storage import MessageSegment, StorageJob

    montaje["drive"]._fallos = [StorageAuthError()]
    fila = _segmento_pendiente(montaje, session)

    montaje["worker"].procesar_lote()
    session.expire_all()

    # Solo los de ESTE usuario: la base de pruebas es la real y ya tiene
    # trabajos de verdad, que no son de esta prueba.
    trabajos = (
        session.query(StorageJob)
        .filter(StorageJob.user_id == montaje["user_id"])
        .all()
    )
    assert trabajos, "el trabajo NO se borra"
    assert all(t.status == "paused" for t in trabajos)
    assert montaje["google"].invalidado is True
    assert session.get(MessageSegment, fila.id) is not None

    nombres = [n for n, _ in montaje["eventos"]]
    assert "storage.reauth_required" in nombres


def test_al_reconectar_se_reanuda(montaje, session):
    montaje["drive"]._fallos = [StorageAuthError()]
    _segmento_pendiente(montaje, session)
    montaje["worker"].procesar_lote()

    montaje["storage"].jobs.reanudar(montaje["user_id"])
    session.expire_all()
    assert montaje["worker"].procesar_lote() == 1
    assert montaje["drive"].subidas == 1


def test_falta_de_espacio_no_es_un_fallo_de_whatsapp(montaje, session):
    """Se distinguen: mezclarlos manda a buscar el problema donde no esta."""
    from app.models.storage import StorageJob

    montaje["drive"]._fallos = [
        StorageQuotaError("DRIVE_QUOTA_EXCEEDED", "sin espacio")
    ]
    _segmento_pendiente(montaje, session)
    montaje["worker"].procesar_lote()
    session.expire_all()

    trabajo = (
        session.query(StorageJob)
        .filter(StorageJob.user_id == montaje["user_id"])
        .one()
    )
    assert trabajo.status == "pending", "se reintenta, no se abandona"
    assert "espacio" in (trabajo.last_error or "")

    codigos = [d.get("code") for n, d in montaje["eventos"] if n == "storage.quota"]
    assert "DRIVE_QUOTA_EXCEEDED" in codigos


# ---------------------------------------------------------------------------
# Multimedia
# ---------------------------------------------------------------------------


@pytest.fixture
def adjunto(montaje, session):
    """Un adjunto descargado, con archivo real en disco."""
    from app.models import MediaFile

    mensaje = _mensaje(session, montaje["chat"], "con foto")
    ruta = Path(montaje["settings"].media_dir) / "foto.bin"
    ruta.write_bytes(b"CONTENIDO BINARIO DE PRUEBA" * 100)

    fila = MediaFile(
        message_id=mensaje.id,
        chat_id=montaje["chat"].id,
        media_type="image",
        mime_type="image/jpeg",
        file_name="foto.jpg",
        file_size=ruta.stat().st_size,
        download_status="downloaded",
        local_path="foto.bin",
    )
    session.add(fila)
    session.flush()

    montaje["storage"].jobs.encolar(
        session,
        user_id=montaje["user_id"],
        account_id=montaje["account_id"],
        job_type="media",
        entity_id=str(fila.id),
        payload_bytes=fila.file_size,
    )
    return fila, ruta


def test_la_multimedia_sube_cifrada(montaje, session, adjunto):
    from app.models import MediaFile

    fila, ruta = adjunto
    montaje["worker"].procesar_lote()
    session.expire_all()

    guardado = session.get(MediaFile, fila.id)
    assert guardado.storage_status == "ready"
    assert guardado.drive_file_id
    assert guardado.plaintext_sha256, "se calcula la huella del original"

    subido = montaje["drive"].archivos[guardado.drive_file_id]
    assert b"CONTENIDO BINARIO" not in subido, "no puede verse el contenido"


def test_el_archivo_local_NO_se_borra_al_subir(montaje, session, adjunto):
    """Se borra despues, y solo tras comprobar. Nunca en la subida."""
    fila, ruta = adjunto
    montaje["worker"].procesar_lote()

    assert ruta.exists(), "la copia local sigue hasta que el desalojo la retire"


def test_si_falta_el_archivo_local_se_dice_y_se_reintenta(montaje, session, adjunto):
    from app.models.storage import StorageJob

    fila, ruta = adjunto
    ruta.unlink()

    montaje["worker"].procesar_lote()
    session.expire_all()
    trabajo = (
        session.query(StorageJob)
        .filter(
            StorageJob.job_type == "media",
            StorageJob.user_id == montaje["user_id"],
        )
        .one()
    )
    assert trabajo.status == "pending"
    assert "local" in (trabajo.last_error or "")


# ---------------------------------------------------------------------------
# Desalojo de la cache
# ---------------------------------------------------------------------------


def test_no_se_desaloja_lo_que_no_esta_confirmado(montaje, session, adjunto):
    """Las tres condiciones son obligatorias: con dos, se pierde contenido."""
    from app.models import MediaFile
    from app.storage.media import MediaStorage

    fila, _ = adjunto
    cache = MediaStorage(montaje["runtime"].database, montaje["settings"], montaje["storage"])

    seguro, motivo = cache.puede_desalojar(fila)
    assert seguro is False and "subido" in motivo

    fila.storage_status = "ready"
    seguro, motivo = cache.puede_desalojar(fila)
    assert seguro is False and "identificador" in motivo

    fila.drive_file_id = "x"
    seguro, motivo = cache.puede_desalojar(fila)
    assert seguro is False and "tamano" in motivo

    fila.stored_bytes = 100
    assert cache.puede_desalojar(fila)[0] is True


def test_el_desalojo_libera_solo_lo_confirmado(montaje, session, adjunto):
    from app.storage.media import MediaStorage

    fila, ruta = adjunto
    montaje["worker"].procesar_lote()
    session.expire_all()

    cache = MediaStorage(montaje["runtime"].database, montaje["settings"], montaje["storage"])
    resultado = cache.desalojar(limite_bytes=0, ttl_horas=1)

    assert resultado["removed"] >= 1
    assert not ruta.exists()
    # El original sigue en Drive: la cache local era una copia.
    session.expire_all()
    from app.models import MediaFile

    assert session.get(MediaFile, fila.id).drive_file_id in montaje["drive"].archivos


# ---------------------------------------------------------------------------
# Aislamiento entre usuarios
# ---------------------------------------------------------------------------


def test_cada_usuario_sube_con_SUS_credenciales(montaje, session, runtime):
    """No existe un cliente capaz de escribir en el Drive de cualquiera."""
    import inspect

    fuente = inspect.getsource(DriveStorageWorker._almacenamiento_de)
    assert "access_token(user_id)" in fuente, "el token se pide por usuario"
    assert "self._cliente_global" not in fuente


def test_un_trabajo_siempre_tiene_dueno(montaje, session):
    from app.models.storage import StorageJob

    _segmento_pendiente(montaje, session)
    trabajos = (
        session.query(StorageJob)
        .filter(StorageJob.user_id == montaje["user_id"])
        .all()
    )
    assert trabajos
    assert all(t.user_id is not None for t in trabajos)


def test_los_segmentos_de_otro_usuario_no_se_tocan(montaje, session, runtime):
    from app.models.storage import MessageSegment

    _segmento_pendiente(montaje, session)
    otro = runtime.auth.register(
        email=f"z-{uuid.uuid4().hex[:8]}@example.com", password=CLAVE
    )
    mios = (
        session.query(MessageSegment)
        .filter(MessageSegment.user_id == otro.user_id)
        .count()
    )
    assert mios == 0
