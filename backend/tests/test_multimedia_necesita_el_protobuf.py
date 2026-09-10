"""Baileys descarga el adjunto a partir del MENSAJE ENTERO, no de sus trozos.

EL FALLO, MEDIDO EN LA BASE REAL
--------------------------------
Ni un solo adjunto se descargaba. En el log::

    [MEDIA] Multimedia: descargados=0 dedup=0 no disponibles=0 caducados=0
            fallidos=1500

Y en `media_files`, los 1210 con el mismo `last_error`::

    'descarga sin el protobuf del mensaje'   x1210

No fallaba una parte: fallaba el 100%, y fallaba antes de tocar la red.

LA CAUSA
--------
`_download_one` construia el `MediaInfo` con columnas sueltas --`direct_path`,
`media_key`, los dos hashes-- porque asi trabajaba el proveedor anterior. El
que hay ahora no: `downloadMediaMessage` de Baileys recibe el `WebMessageInfo`
completo, y de ahi saca la ruta del CDN, las claves y la peticion de resubida
al telefono cuando el CDN ya no lo sirve.

Lo llamativo es que el dato ya estaba guardado. `messages.raw_proto` lo tenia
para los 1210 --se comprobo con un `count(msg.raw_proto)` sobre la base del
usuario--; esta capa simplemente no iba a buscarlo.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid

import pytest

from app.models import Chat, MediaFile, Message
from app.services.media_service import MediaService

#: Un `WebMessageInfo` de mentira. No se decodifica en la prueba: lo que se
#: mide es que LLEGUE, no que sea valido.
PROTO = b"\x0a\x10un-protobuf-falso"


class ClienteQueApunta:
    """Se queda con el `MediaInfo` que le pasan y devuelve bytes."""

    def __init__(self, *, contenido: bytes = b"contenido descargado"):
        self.recibido = None
        self._contenido = contenido

    async def download_media(self, info):
        self.recibido = info
        return self._contenido


@pytest.fixture
def montaje(session, settings, tmp_path, cuenta):
    """Un adjunto pendiente, con su mensaje y su protobuf."""
    chat = Chat(
        jid=f"57{uuid.uuid4().hex[:9]}@s.whatsapp.net",
        chat_type="individual",
        whatsapp_account_id=cuenta.id,
    )
    session.add(chat)
    session.flush()

    def crear_adjunto(*, raw_proto: bytes | None = PROTO) -> int:
        mensaje = Message(
            chat_id=chat.id,
            chat_jid=chat.jid,
            whatsapp_message_id=f"M-{uuid.uuid4().hex[:10]}",
            message_type="image",
            timestamp=1,
            raw_proto=raw_proto,
        )
        session.add(mensaje)
        session.flush()
        adjunto = MediaFile(
            message_id=mensaje.id,
            chat_id=chat.id,
            media_type="image",
            mime_type="image/jpeg",
            direct_path="/v/t62.7118-24/una-ruta",
            media_key=b"K" * 32,
            file_sha256=b"S" * 32,
            file_enc_sha256=b"E" * 32,
            download_status="pending",
        )
        session.add(adjunto)
        session.flush()
        return adjunto.id

    ajustes = dataclasses.replace(settings, media_dir=tmp_path / "media")
    (tmp_path / "media").mkdir(parents=True, exist_ok=True)
    return crear_adjunto, ajustes


class _BaseDeLaPrueba:
    """Reutiliza la sesion transaccional en vez de abrir otra."""

    def __init__(self, session):
        self._session = session

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


# ---------------------------------------------------------------------------
# Lo que se le entrega al proveedor
# ---------------------------------------------------------------------------


def test_la_descarga_recibe_el_protobuf_del_mensaje(session, montaje):
    """El fallo que este cambio cierra: iba sin el, y sin el no hay descarga."""
    crear_adjunto, ajustes = montaje
    media_id = crear_adjunto()
    cliente = ClienteQueApunta()
    servicio = MediaService(ajustes, _BaseDeLaPrueba(session), cliente)

    asyncio.run(servicio._download_one(media_id))

    assert cliente.recibido is not None, "no se llego a intentar la descarga"
    assert cliente.recibido.raw_proto == PROTO


def test_los_otros_campos_siguen_yendo(session, montaje):
    """Anadir el protobuf no puede quitar lo que ya se mandaba.

    Los dos hashes son los que permiten comprobar que lo descargado es lo que
    decia ser; perderlos convertiria la descarga en un acto de fe.
    """
    crear_adjunto, ajustes = montaje
    cliente = ClienteQueApunta()
    servicio = MediaService(ajustes, _BaseDeLaPrueba(session), cliente)

    asyncio.run(servicio._download_one(crear_adjunto()))

    info = cliente.recibido
    assert info.direct_path == "/v/t62.7118-24/una-ruta"
    assert info.media_key == b"K" * 32
    assert info.file_sha256 == b"S" * 32
    assert info.file_enc_sha256 == b"E" * 32


def test_el_adjunto_acaba_descargado(session, montaje):
    """De punta a punta: con el protobuf, la descarga completa su ciclo."""
    crear_adjunto, ajustes = montaje
    media_id = crear_adjunto()
    servicio = MediaService(ajustes, _BaseDeLaPrueba(session), ClienteQueApunta())

    asyncio.run(servicio._download_one(media_id))

    fila = session.get(MediaFile, media_id)
    assert fila.download_status == "downloaded"
    assert fila.local_path


# ---------------------------------------------------------------------------
# Cuando el protobuf no esta
# ---------------------------------------------------------------------------


def test_sin_protobuf_NO_se_llama_al_proveedor(session, montaje):
    """Llamar sin el es gastar una peticion para que lance siempre."""
    crear_adjunto, ajustes = montaje
    cliente = ClienteQueApunta()
    servicio = MediaService(ajustes, _BaseDeLaPrueba(session), cliente)

    asyncio.run(servicio._download_one(crear_adjunto(raw_proto=None)))

    assert cliente.recibido is None


def test_sin_protobuf_el_estado_es_TERMINAL(session, montaje):
    """No se arregla reintentandolo: la fila no va a criar un protobuf sola.

    Es la misma leccion que el 403 del CDN. Un fallo permanente marcado como
    reintentable convierte el worker en un bucle que repite lo imposible cada
    veinte segundos y llena el log sin descargar nada.
    """
    crear_adjunto, ajustes = montaje
    media_id = crear_adjunto(raw_proto=None)
    servicio = MediaService(ajustes, _BaseDeLaPrueba(session), ClienteQueApunta())

    asyncio.run(servicio._download_one(media_id))

    fila = session.get(MediaFile, media_id)
    assert fila.download_status == "unavailable", (
        "'failed' se reintenta; esto no se puede arreglar reintentandolo"
    )
    assert "raw_proto" in (fila.last_error or "")


def test_lo_terminal_no_vuelve_a_la_cola(session, montaje):
    """La comprobacion que cierra el bucle: no reaparece en la ronda siguiente."""
    crear_adjunto, ajustes = montaje
    media_id = crear_adjunto(raw_proto=None)
    servicio = MediaService(ajustes, _BaseDeLaPrueba(session), ClienteQueApunta())

    asyncio.run(servicio._download_one(media_id))

    assert media_id not in servicio.pending_ids()


# ---------------------------------------------------------------------------
# Los 1210 que ya estaban marcados como perdidos
# ---------------------------------------------------------------------------
#
# El arreglo solo sirve para lo que venga a partir de ahora si los que ya
# fallaron se quedan fuera. Y se quedaban: agotaron los tres intentos contra
# un fallo que no era suyo, asi que `pending_ids` --que filtra por
# `download_attempts < 3`-- no los volveria a mirar nunca.


def _reconciliar(session, ajustes):
    from app.services.maintenance_service import MaintenanceService, ReconcileReport

    servicio = MaintenanceService(_BaseDeLaPrueba(session), ajustes)
    return servicio.reconcile_media_states(ReconcileReport())


def test_los_que_fallaron_por_ESTE_bug_vuelven_a_la_cola(session, montaje):
    crear_adjunto, ajustes = montaje
    media_id = crear_adjunto()
    fila = session.get(MediaFile, media_id)
    fila.download_status = "failed"
    fila.download_attempts = 3
    fila.last_error = "descarga sin el protobuf del mensaje"
    session.flush()

    informe = _reconciliar(session, ajustes)

    session.expire_all()
    fila = session.get(MediaFile, media_id)
    assert fila.download_status == "pending"
    assert fila.download_attempts == 0
    assert informe.media_reintentables_por_bug == 1


def test_lo_que_el_CDN_ya_no_sirve_NO_se_reabre(session, montaje):
    """Es la mitad importante: reabrirlos seria volver al bucle de siempre.

    Un 403 del CDN es terminal y esta comprobado que lo es. Solo se reabre lo
    que fallo por nuestra causa.
    """
    crear_adjunto, ajustes = montaje
    media_id = crear_adjunto()
    fila = session.get(MediaFile, media_id)
    fila.download_status = "expired"
    fila.download_attempts = 1
    fila.last_error = "HTTP Error 403: Forbidden"
    session.flush()

    informe = _reconciliar(session, ajustes)

    session.expire_all()
    assert session.get(MediaFile, media_id).download_status == "expired"
    assert informe.media_reintentables_por_bug == 0


def test_tras_reconciliar_el_adjunto_ya_es_candidato(session, montaje):
    """De punta a punta: reabrirlo tiene que servir para que se descargue."""
    crear_adjunto, ajustes = montaje
    media_id = crear_adjunto()
    fila = session.get(MediaFile, media_id)
    fila.download_status = "failed"
    fila.download_attempts = 3
    fila.last_error = "descarga sin el protobuf del mensaje"
    session.flush()

    _reconciliar(session, ajustes)

    servicio = MediaService(ajustes, _BaseDeLaPrueba(session), ClienteQueApunta())
    assert media_id in servicio.pending_ids()
