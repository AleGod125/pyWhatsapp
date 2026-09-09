"""El arranque rápido, probado con un bootstrap sintético.

POR QUE SINTETICO
-----------------
Hubo vinculaciones en las que WhatsApp sencillamente **no envió**
``INITIAL_BOOTSTRAP``: el cliente hizo exactamente lo mismo que en las que sí
funcionaron —se compararon las dos ventanas del registro línea a línea— y del
servidor no llegó nada.

Eso no puede seguir siendo la excusa para no tener el camino probado. Aquí el
bootstrap se fabrica, con mensajes de verdad —``WebMessageInfo`` serializado,
no un objeto de mentira— y se comprueba lo que tiene que pasar cuando llega.

LO QUE SE FIJA
--------------
* **toda** conversación se crea, tenga referencia o no (F6);
* las que no la tienen quedan esperando referencia, no descartadas (F8);
* las que sí la tienen entregan un ancla REAL, con su identificador y su marca;
* el planificador sabe cuáles puede excavar y cuáles no.

Y lo que no se fija: nada de esto inventa un identificador. Un ancla fabricada
recibe confirmación del servidor y después silencio, que es el fallo más caro
de diagnosticar del proyecto.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.wa.historial import FullHistorySync, HistoryConversation
from app.models import Chat, ChatHistoryState, Message, WhatsAppAccount
from app.services.history_service import ingest_history_sync

CLAVE = "Contrasena-De-Prueba-1"

#: Cuántas conversaciones trae el bootstrap de mentira, y cuántas con mensaje.
#: Es el escenario que pide F32: diez, seis con referencia y cuatro sin ella.
CONVERSACIONES = 10
CON_MENSAJE = 6


def _correo() -> str:
    return f"fb-{uuid.uuid4().hex[:10]}@example.com"


def _mensaje_real(chat_jid: str, wamid: str, marca: int) -> bytes:
    """Un ``WebMessageInfo`` de verdad, serializado.

    Se construye con el protobuf real y no con un objeto simulado: lo que se
    quiere comprobar es que el analizador saca de ahí un identificador
    utilizable, y con un doble eso no se probaría.
    """
    from app.models.proto.whatsapp_backup_pb2 import WebMessageInfo

    mensaje = WebMessageInfo()
    # Los nombres son los del .proto real, no los de la convencion de Python:
    # `ID`, `remoteJID`, `messageTimestamp`. Adivinarlos era el error facil.
    mensaje.key.ID = wamid
    mensaje.key.remoteJID = chat_jid
    mensaje.key.fromMe = False
    mensaje.messageTimestamp = marca
    # `message` es `bytes` en nuestro .proto: llevamos un subconjunto y el
    # cuerpo del mensaje no se modela. Se escribe el campo 1 de `Message`
    # --`conversation`, cadena-- a mano, que es lo que el analizador lee para
    # decidir el tipo.
    mensaje.message = bytes([0x0A, 0x04]) + b"hola"
    return mensaje.SerializeToString()


def _bootstrap() -> FullHistorySync:
    """Diez conversaciones: seis con mensaje real, cuatro vacías."""
    conversaciones = []
    for indice in range(CONVERSACIONES):
        jid = f"3460000{indice:04d}@s.whatsapp.net"
        mensajes = []
        if indice < CON_MENSAJE:
            mensajes = [
                (
                    _mensaje_real(jid, f"3EB0SINTETICO{indice:07d}", 1_760_000_000 + indice),
                    indice,
                )
            ]
        conversaciones.append(
            HistoryConversation(
                jid=jid,
                name=f"Contacto {indice}",
                last_message_timestamp=1_760_000_000 + indice,
                unread_count=0,
                messages=mensajes,
            )
        )
    return FullHistorySync(
        sync_type="INITIAL_BOOTSTRAP",
        chunk_order=0,
        progress=0,
        conversations=conversaciones,
        pushnames=[],
    )


@pytest.fixture
def cuenta(runtime, session):
    session.execute(delete(WhatsAppAccount))
    session.flush()
    runtime._montar_cuentas()
    inicio = runtime.auth.register(email=_correo(), password=CLAVE)
    fila = WhatsAppAccount(
        user_id=inicio.user_id,
        session_status="linked",
        session_storage_key=f"users/{inicio.user_id}",
    )
    session.add(fila)
    session.flush()
    return fila


# ---------------------------------------------------------------------------
# El descubrimiento (F6)
# ---------------------------------------------------------------------------


def test_se_crean_TODAS_las_conversaciones(cuenta, session):
    """Incluidas las que no traen ni un mensaje.

    Una conversación sin referencia sigue siendo una conversación: el usuario
    la tiene, la ve en su teléfono y espera verla aquí. Descartarla porque
    todavía no se le puede pedir el pasado deja un panel a medias.
    """
    resultado = ingest_history_sync(
        session, _bootstrap(), whatsapp_account_id=cuenta.id
    )
    session.flush()

    creados = session.query(Chat).filter(Chat.whatsapp_account_id == cuenta.id).count()
    assert creados == CONVERSACIONES
    assert resultado.messages_inserted == CON_MENSAJE


def test_cada_conversacion_tiene_su_estado(cuenta, session):
    """Sin estado no entra en ninguna cola: existiria y no la miraria nadie."""
    ingest_history_sync(session, _bootstrap(), whatsapp_account_id=cuenta.id)
    session.flush()

    ids = [
        c.id for c in session.query(Chat).filter(Chat.whatsapp_account_id == cuenta.id)
    ]
    estados = (
        session.query(ChatHistoryState)
        .filter(ChatHistoryState.chat_id.in_(ids))
        .count()
    )
    assert estados == CONVERSACIONES


def test_el_nombre_y_la_marca_llegan(cuenta, session):
    """Es lo que hace util el panel el primer segundo."""
    ingest_history_sync(session, _bootstrap(), whatsapp_account_id=cuenta.id)
    session.flush()

    chats = session.query(Chat).filter(Chat.whatsapp_account_id == cuenta.id).all()
    assert all(c.name for c in chats)
    assert all(c.last_message_timestamp for c in chats)


# ---------------------------------------------------------------------------
# Las referencias (F7)
# ---------------------------------------------------------------------------


def test_los_mensajes_traen_identificador_REAL(cuenta, session):
    """El ancla sale del mensaje, no se fabrica."""
    from app.services.repository import is_valid_history_cursor_id

    ingest_history_sync(session, _bootstrap(), whatsapp_account_id=cuenta.id)
    session.flush()

    mensajes = session.query(Message).all()
    assert len(mensajes) == CON_MENSAJE
    for mensaje in mensajes:
        assert mensaje.whatsapp_message_id
        assert is_valid_history_cursor_id(mensaje.whatsapp_message_id)
        assert mensaje.timestamp > 0


def test_las_conversaciones_vacias_no_inventan_ancla(cuenta, session):
    """Cuatro sin mensaje: cuatro sin identificador. Ni uno de regalo."""
    ingest_history_sync(session, _bootstrap(), whatsapp_account_id=cuenta.id)
    session.flush()

    con_mensajes = {m.chat_jid for m in session.query(Message).all()}
    todos = {
        c.jid for c in session.query(Chat).filter(Chat.whatsapp_account_id == cuenta.id)
    }
    sin_mensajes = todos - con_mensajes
    assert len(sin_mensajes) == CONVERSACIONES - CON_MENSAJE


# ---------------------------------------------------------------------------
# Lo que ve el planificador (F8, F11)
# ---------------------------------------------------------------------------


def test_el_planificador_ordena_lo_descubierto(cuenta, session):
    """De diez conversaciones, el orden lo decide quien las va a excavar."""
    from app.history.scheduler import HistoryScheduler

    ingest_history_sync(session, _bootstrap(), whatsapp_account_id=cuenta.id)
    session.flush()

    chats = session.query(Chat).filter(Chat.whatsapp_account_id == cuenta.id).all()
    candidatos = [
        {
            "chat_id": c.id,
            "chat_jid": c.jid,
            "ultimo_mensaje": c.last_message_timestamp,
            "reintentando": False,
        }
        for c in chats
    ]

    planificador = HistoryScheduler()
    # El usuario abre la ultima de la lista, que por actividad seria la primera
    # en ser atendida de todas formas: se elige una del medio para que se note.
    elegida = candidatos[3]["chat_jid"]
    planificador.marcar_interactiva(elegida)

    orden = [t.chat_jid for t in planificador.ordenar(candidatos)]
    assert orden[0] == elegida
    assert len(orden) == CONVERSACIONES


def test_ingerir_dos_veces_no_duplica(cuenta, session):
    """Una pasada nueva no puede multiplicar lo que ya estaba."""
    ingest_history_sync(session, _bootstrap(), whatsapp_account_id=cuenta.id)
    session.flush()
    ingest_history_sync(session, _bootstrap(), whatsapp_account_id=cuenta.id)
    session.flush()

    chats = session.query(Chat).filter(Chat.whatsapp_account_id == cuenta.id).count()
    mensajes = session.query(Message).count()
    assert chats == CONVERSACIONES
    assert mensajes == CON_MENSAJE


def test_las_conversaciones_quedan_con_dueno(cuenta, session):
    """Sin dueno existen en la base y no las ve nadie."""
    ingest_history_sync(session, _bootstrap(), whatsapp_account_id=cuenta.id)
    session.flush()

    huerfanos = (
        session.query(Chat).filter(Chat.whatsapp_account_id.is_(None)).count()
    )
    assert huerfanos == 0
