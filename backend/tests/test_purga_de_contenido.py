"""El contenido sale de PostgreSQL cuando Drive lo confirma.

EL REPARTO QUE ESTO COMPLETA
----------------------------
``app/storage/reader.py`` ya lo describia: *PostgreSQL dice QUE mensajes hay y
DONDE estan; Drive tiene el contenido*. Y el camino de lectura ya estaba
puesto: al abrir una conversacion cuyo mensaje esta ``ready``, el contenido se
baja del segmento y se descifra.

Lo que faltaba era la otra mitad. El contenido se subia a Drive y se quedaba
TAMBIEN en la base, sin cifrar, para siempre. El reparto era una intencion.

LA GARANTIA QUE SE PRUEBA AQUI
------------------------------
No se vacia NADA que no este confirmado. Tres condiciones a la vez:

1. el mensaje se dio por subido (``storage_status = 'ready'``);
2. su segmento esta cerrado (``status = 'ready'``);
3. ese segmento tiene ``drive_file_id`` -- Google contesto con un archivo.

Vaciar contenido que no llego a Drive no es optimizar: es perderlo, y sin
nada que lo delate hasta que alguien abra esa conversacion meses despues.
"""

from __future__ import annotations

import uuid

import pytest

from app.models import Chat, Message
from app.models.storage import MessageSegment
from app.storage.purga import cuanto_queda_por_purgar, purgar_contenido_subido


class _Base:
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


@pytest.fixture
def montaje(session, cuenta):
    """Un chat con un mensaje, y un segmento al que engancharlo."""
    chat = Chat(
        jid=f"57{uuid.uuid4().hex[:9]}@s.whatsapp.net",
        chat_type="individual",
        whatsapp_account_id=cuenta.id,
    )
    session.add(chat)
    session.flush()

    def crear(*, estado_mensaje, estado_segmento, drive_file_id):
        segmento = MessageSegment(
            user_id=cuenta.user_id,
            chat_id=chat.id,
            chat_jid=chat.jid,
            whatsapp_account_id=cuenta.id,
            sequence_number=uuid.uuid4().int % 10**6,
            status=estado_segmento,
            drive_file_id=drive_file_id,
        )
        session.add(segmento)
        session.flush()
        mensaje = Message(
            chat_id=chat.id,
            chat_jid=chat.jid,
            whatsapp_message_id=f"M-{uuid.uuid4().hex[:12]}",
            message_type="text",
            text="una conversacion privada",
            raw_proto=b"protobuf con el mensaje entero",
            raw_metadata={"algo": "sensible"},
            timestamp=1,
            segment_id=segmento.id,
            segment_index=0,
            storage_status=estado_mensaje,
        )
        session.add(mensaje)
        session.flush()
        return mensaje.id

    return crear, _Base(session)


# ---------------------------------------------------------------------------
# Lo confirmado se libera
# ---------------------------------------------------------------------------


def test_lo_confirmado_en_Drive_sale_de_la_base(session, montaje):
    """El caso normal, y el que hace que el reparto sea real."""
    crear, base = montaje
    mid = crear(
        estado_mensaje="ready", estado_segmento="ready", drive_file_id="archivo-1"
    )

    assert purgar_contenido_subido(base) == 1

    session.expire_all()
    fila = session.get(Message, mid)
    assert fila.text is None
    assert fila.raw_proto is None
    assert fila.raw_metadata is None


def test_el_INDICE_se_conserva_entero(session, montaje):
    """Sin el, el panel no puede listar, ordenar, paginar ni deduplicar.

    Es lo que separa "el contenido vive en Drive" de "no hay copia usable".
    """
    crear, base = montaje
    mid = crear(
        estado_mensaje="ready", estado_segmento="ready", drive_file_id="archivo-1"
    )
    antes = session.get(Message, mid)
    wamid = antes.whatsapp_message_id
    chat_id = antes.chat_id
    cuando = antes.timestamp
    segmento = antes.segment_id
    linea = antes.segment_index

    purgar_contenido_subido(base)

    session.expire_all()
    fila = session.get(Message, mid)
    assert fila is not None, "se borro la fila; solo habia que vaciar el contenido"
    assert fila.whatsapp_message_id == wamid
    assert fila.chat_id == chat_id
    assert fila.timestamp == cuando
    # Y DONDE esta el contenido: sin esto no se puede volver a leer.
    assert fila.segment_id == segmento
    assert fila.segment_index == linea


# ---------------------------------------------------------------------------
# Lo NO confirmado no se toca. Las tres condiciones, una a una.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "estado_mensaje, estado_segmento, archivo, porque",
    [
        ("local", "ready", "archivo-1", "el mensaje no se ha subido"),
        ("uploading", "ready", "archivo-1", "la subida esta a medias"),
        ("ready", "building", "archivo-1", "el segmento sigue abierto"),
        ("ready", "uploading", "archivo-1", "el segmento se esta subiendo"),
        ("ready", "failed", "archivo-1", "la subida del segmento fallo"),
        ("ready", "ready", None, "Drive no devolvio identificador de archivo"),
    ],
)
def test_sin_confirmar_NO_se_vacia(
    session, montaje, estado_mensaje, estado_segmento, archivo, porque
):
    """Vaciar sin confirmacion no es liberar espacio: es perder el mensaje."""
    crear, base = montaje
    mid = crear(
        estado_mensaje=estado_mensaje,
        estado_segmento=estado_segmento,
        drive_file_id=archivo,
    )

    assert purgar_contenido_subido(base) == 0, porque

    session.expire_all()
    fila = session.get(Message, mid)
    assert fila.text == "una conversacion privada"
    assert fila.raw_proto is not None


def test_un_mensaje_SIN_segmento_no_se_vacia(session, cuenta):
    """Sin segmento no hay donde volver a leerlo. Es el caso mas peligroso."""
    chat = Chat(
        jid=f"57{uuid.uuid4().hex[:9]}@s.whatsapp.net",
        chat_type="individual",
        whatsapp_account_id=cuenta.id,
    )
    session.add(chat)
    session.flush()
    mensaje = Message(
        chat_id=chat.id,
        chat_jid=chat.jid,
        whatsapp_message_id=f"M-{uuid.uuid4().hex[:12]}",
        message_type="text",
        text="sin segmento",
        timestamp=1,
        # Marcado como subido pero sin segmento: incoherente, y por eso mismo
        # hay que ser conservador.
        storage_status="ready",
    )
    session.add(mensaje)
    session.flush()

    assert purgar_contenido_subido(_Base(session)) == 0

    session.expire_all()
    assert session.get(Message, mensaje.id).text == "sin segmento"


# ---------------------------------------------------------------------------
# Se puede repetir, y se puede medir
# ---------------------------------------------------------------------------


def test_pasar_dos_veces_no_hace_nada_la_segunda(session, montaje):
    """El barrido corre cada treinta segundos: no puede trabajar en balde."""
    crear, base = montaje
    crear(estado_mensaje="ready", estado_segmento="ready", drive_file_id="a")

    assert purgar_contenido_subido(base) == 1
    assert purgar_contenido_subido(base) == 0


def test_se_puede_saber_cuanto_queda(session, montaje):
    crear, base = montaje
    crear(estado_mensaje="ready", estado_segmento="ready", drive_file_id="a")
    crear(estado_mensaje="local", estado_segmento="ready", drive_file_id="b")

    assert cuanto_queda_por_purgar(base) == 1
    purgar_contenido_subido(base)
    assert cuanto_queda_por_purgar(base) == 0


def test_un_fallo_purgando_no_tumba_al_trabajador(montaje):
    """Liberar espacio no puede impedir una subida."""

    class _Rota:
        def transaction(self):
            raise RuntimeError("la base dijo que no")

    assert purgar_contenido_subido(_Rota()) == 0
