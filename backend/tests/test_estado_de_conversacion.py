"""Archivado, restringido, fijado y silenciado: el estado que se tiraba.

DE DONDE SALE
-------------
De ninguna peticion nueva. ``proto.Conversation`` --lo que llega en cada
``messaging-history.set``-- ya trae los cuatro::

    archived      bool
    locked        bool     <- los "chats restringidos" de WhatsApp
    pinned        uint     <- marca de tiempo, no booleano
    muteEndTime   uint

El traductor copiaba jid, nombre, marca de tiempo y no leidos, y descartaba el
resto. El dato llegaba en cada lote y se perdia.

POR QUE ``locked`` SOLO PUEDE VENIR DE AQUI
-------------------------------------------
Baileys mantiene ``archived``, ``pinned`` y ``muteEndTime`` al vuelo desde el
app-state (``archiveChatAction`` y compania). La unica accion que NO procesa es
``lockChatAction``. Asi que para los chats restringidos el historial es la
unica fuente que hay, y perder el dato ahi es perderlo del todo.

LA REGLA QUE NO SE PUEDE ROMPER
-------------------------------
Aqui ``False`` y ``NULL`` son datos, no ausencias. Desarchivar un chat es
mandar ``archived=False``; dejar de fijarlo es mandar ``pinned_at=None``. Si
se aplicara la regla general de ``upsert_chat`` --"lo vacio no pisa"-- un chat
archivado no podria volver nunca a la lista normal.
"""

from __future__ import annotations

import uuid

import pytest

from app.models import Chat
from app.services import repository as repo


def _jid() -> str:
    return f"57{uuid.uuid4().hex[:9]}@s.whatsapp.net"


# ---------------------------------------------------------------------------
# Se guarda
# ---------------------------------------------------------------------------


def test_los_cuatro_campos_se_guardan(session, cuenta):
    jid = _jid()
    chat_id = repo.upsert_chat(
        session,
        jid=jid,
        whatsapp_account_id=cuenta.id,
        archived=True,
        locked=True,
        pinned_at=1725900000,
        mute_until=1726000000,
    )
    session.flush()

    fila = session.get(Chat, chat_id)
    assert fila.archived is True
    assert fila.locked is True
    assert fila.pinned_at == 1725900000
    assert fila.mute_until == 1726000000


def test_un_chat_normal_nace_sin_estado(session, cuenta):
    """El valor por defecto tiene que ser el de siempre: nada marcado."""
    chat_id = repo.upsert_chat(session, jid=_jid(), whatsapp_account_id=cuenta.id)
    session.flush()

    fila = session.get(Chat, chat_id)
    assert fila.archived is False
    assert fila.locked is False
    assert fila.pinned_at is None
    assert fila.mute_until is None


# ---------------------------------------------------------------------------
# Se puede DESHACER
# ---------------------------------------------------------------------------


def test_desarchivar_funciona(session, cuenta):
    """El caso que rompe la regla de "lo vacio no pisa".

    Si `False` se tratara como "no lo se", un chat archivado se quedaria
    archivado para siempre por mucho que el telefono dijera lo contrario.
    """
    jid = _jid()
    repo.upsert_chat(
        session, jid=jid, whatsapp_account_id=cuenta.id, archived=True, locked=False
    )
    session.flush()

    chat_id = repo.upsert_chat(
        session, jid=jid, whatsapp_account_id=cuenta.id, archived=False, locked=False
    )
    session.flush()
    session.expire_all()

    assert session.get(Chat, chat_id).archived is False


def test_dejar_de_fijar_funciona(session, cuenta):
    """`pinned_at=None` significa "ya no esta fijado", no "no lo se"."""
    jid = _jid()
    repo.upsert_chat(
        session,
        jid=jid,
        whatsapp_account_id=cuenta.id,
        archived=False,
        pinned_at=1725900000,
    )
    session.flush()

    chat_id = repo.upsert_chat(
        session, jid=jid, whatsapp_account_id=cuenta.id, archived=False, pinned_at=None
    )
    session.flush()
    session.expire_all()

    assert session.get(Chat, chat_id).pinned_at is None


# ---------------------------------------------------------------------------
# Quien NO lo sabe no lo pisa
# ---------------------------------------------------------------------------


def test_un_mensaje_en_vivo_no_desarchiva_el_chat(session, cuenta):
    """El fallo que se evitaria destrozando esto.

    Un mensaje en vivo trae texto y hora, no el estado de la conversacion. Si
    esa llamada escribiera los valores por defecto, el primer mensaje que
    llegara sacaria el chat de archivados --y peor: le quitaria el `locked` a
    un chat restringido, sacandolo a la lista normal a la vista de cualquiera.
    """
    jid = _jid()
    repo.upsert_chat(
        session,
        jid=jid,
        whatsapp_account_id=cuenta.id,
        archived=True,
        locked=True,
        pinned_at=1725900000,
        mute_until=1726000000,
    )
    session.flush()

    # Exactamente como lo llama el camino en vivo: sin estado.
    chat_id = repo.upsert_chat(
        session,
        jid=jid,
        whatsapp_account_id=cuenta.id,
        last_message="hola",
        last_message_timestamp=1726100000,
    )
    session.flush()
    session.expire_all()

    fila = session.get(Chat, chat_id)
    assert fila.archived is True
    assert fila.locked is True, "un mensaje en vivo destapo un chat restringido"
    assert fila.pinned_at == 1725900000
    assert fila.mute_until == 1726000000
    assert fila.last_message == "hola"


# ---------------------------------------------------------------------------
# El camino completo, desde el JSON de Baileys
# ---------------------------------------------------------------------------


def test_el_historial_lo_trae_y_lo_persiste(session, cuenta):
    """De punta a punta: del lote a la columna, sin tocar nada por el medio."""
    from app.services.history_service import ingest_history_sync
    from app.wa.historial import parse_full_json

    jid = _jid()
    sync = parse_full_json(
        {
            "sync_type": "RECENT",
            "progress": 100,
            "conversations": [
                {
                    "jid": jid,
                    "name": "Un chat guardado",
                    "messages": [],
                    "archived": True,
                    "locked": True,
                    "pinned_at": 1725900000,
                    "mute_until": 0,
                }
            ],
        }
    )
    assert sync is not None
    conversacion = sync.conversations[0]
    assert conversacion.archived is True
    assert conversacion.locked is True

    ingest_history_sync(session, sync, whatsapp_account_id=cuenta.id)
    session.flush()

    fila = (
        session.query(Chat)
        .filter(Chat.jid == jid, Chat.whatsapp_account_id == cuenta.id)
        .one()
    )
    assert fila.archived is True
    assert fila.locked is True
    assert fila.pinned_at == 1725900000
    assert fila.mute_until == 0, "0 es 'silenciado para siempre', no 'sin silenciar'"


def test_el_silencio_para_siempre_no_se_confunde_con_ninguno(session, cuenta):
    """WhatsApp usa 0 para "para siempre". Compararlo con la hora actual sin
    mirar antes ese caso daria "caducado hace 56 anos"."""
    jid = _jid()
    chat_id = repo.upsert_chat(
        session, jid=jid, whatsapp_account_id=cuenta.id, archived=False, mute_until=0
    )
    session.flush()

    fila = session.get(Chat, chat_id)
    assert fila.mute_until == 0
    assert fila.mute_until is not None


# ---------------------------------------------------------------------------
# El camino EN VIVO: archivar desde el telefono
# ---------------------------------------------------------------------------
#
# Baileys emitia `archive`, `pin` y `mute` desde el app-state y no los recogia
# nadie: el usuario archivaba un chat en el telefono y el panel lo seguia
# ensenando en la lista normal hasta la siguiente sincronizacion completa.


class _BaseDeLaPrueba:
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
def chat_existente(session, cuenta):
    jid = _jid()
    chat_id = repo.upsert_chat(session, jid=jid, whatsapp_account_id=cuenta.id)
    session.flush()
    return jid, chat_id, _BaseDeLaPrueba(session)


def test_archivar_en_vivo_se_guarda(session, cuenta, chat_existente):
    jid, chat_id, base = chat_existente

    repo.marcar_estado_de_chat(
        base, jid, whatsapp_account_id=cuenta.id, archived=True
    )
    session.expire_all()

    assert session.get(Chat, chat_id).archived is True


def test_desarchivar_en_vivo_tambien(session, cuenta, chat_existente):
    jid, chat_id, base = chat_existente
    repo.marcar_estado_de_chat(base, jid, whatsapp_account_id=cuenta.id, archived=True)
    repo.marcar_estado_de_chat(base, jid, whatsapp_account_id=cuenta.id, archived=False)
    session.expire_all()

    assert session.get(Chat, chat_id).archived is False


def test_fijar_guarda_CUANDO_no_solo_que_si(session, cuenta, chat_existente):
    """Entre varios fijados el orden es el de cuando se fijaron."""
    jid, chat_id, base = chat_existente

    repo.marcar_estado_de_chat(
        base, jid, whatsapp_account_id=cuenta.id, pinned=1725900000
    )
    session.expire_all()

    assert session.get(Chat, chat_id).pinned_at == 1725900000


def test_fijar_sin_fecha_usa_la_hora_actual(session, cuenta, chat_existente):
    """Un worker viejo solo manda el si/no. Tiene que seguir funcionando."""
    import time

    jid, chat_id, base = chat_existente
    antes = int(time.time())

    repo.marcar_estado_de_chat(base, jid, whatsapp_account_id=cuenta.id, pinned=True)
    session.expire_all()

    fijado = session.get(Chat, chat_id).pinned_at
    assert fijado is not None and fijado >= antes


def test_dejar_de_fijar_en_vivo(session, cuenta, chat_existente):
    jid, chat_id, base = chat_existente
    repo.marcar_estado_de_chat(base, jid, whatsapp_account_id=cuenta.id, pinned=True)
    repo.marcar_estado_de_chat(base, jid, whatsapp_account_id=cuenta.id, pinned=False)
    session.expire_all()

    assert session.get(Chat, chat_id).pinned_at is None


def test_silenciar_para_siempre_no_es_lo_mismo_que_no_silenciar(
    session, cuenta, chat_existente
):
    """La trampa: WhatsApp usa 0 para "para siempre".

    Colapsarlo con "sin silenciar" haria que un chat silenciado
    indefinidamente apareciera como normal.
    """
    jid, chat_id, base = chat_existente

    repo.marcar_estado_de_chat(base, jid, whatsapp_account_id=cuenta.id, muted=0)
    session.expire_all()
    assert session.get(Chat, chat_id).mute_until == 0

    repo.marcar_estado_de_chat(base, jid, whatsapp_account_id=cuenta.id, muted=False)
    session.expire_all()
    assert session.get(Chat, chat_id).mute_until is None


def test_no_se_inventa_un_chat_que_no_existe(session, cuenta):
    """Crearlo aqui produciria una fila sin nombre, sin mensajes y sin tipo,
    solo para anotar que esta archivada."""
    base = _BaseDeLaPrueba(session)
    desconocido = _jid()

    cambio = repo.marcar_estado_de_chat(
        base, desconocido, whatsapp_account_id=cuenta.id, archived=True
    )

    assert cambio is False
    assert session.query(Chat).filter(Chat.jid == desconocido).count() == 0


def test_no_toca_el_chat_de_OTRA_cuenta(session, cuenta, chat_existente):
    """Mismo jid en dos cuentas es lo normal: el mismo contacto por dos vias."""
    import uuid as _uuid

    from app.models import User, WhatsAppAccount

    jid, chat_id, base = chat_existente
    otro_usuario = User(
        email=f"otro-{_uuid.uuid4().hex[:10]}@example.com", password_hash="x"
    )
    session.add(otro_usuario)
    session.flush()
    otra_id = _uuid.uuid4()
    session.add(
        WhatsAppAccount(
            id=otra_id,
            user_id=otro_usuario.id,
            session_status="linked",
            session_storage_key=f"accounts/{otra_id}",
        )
    )
    session.flush()
    ajeno = repo.upsert_chat(session, jid=jid, whatsapp_account_id=otra_id)
    session.flush()

    repo.marcar_estado_de_chat(base, jid, whatsapp_account_id=cuenta.id, archived=True)
    session.expire_all()

    assert session.get(Chat, chat_id).archived is True
    assert session.get(Chat, ajeno).archived is False, "archivo el chat de otra cuenta"


# ---------------------------------------------------------------------------
# UN LOTE SIN METADATOS NO PISA A UNO QUE SI LOS TRAIA
# ---------------------------------------------------------------------------
#
# EL FALLO, MEDIDO CON DOS LOTES REALES DEL MISMO SEGUNDO
# -------------------------------------------------------
# WhatsApp manda varios `messaging-history.set` seguidos, y una conversacion
# puede aparecer en mas de uno: en el primero con su `proto.Conversation`, en
# el segundo solo por sus mensajes.
#
#     lote A (159 conv): "Steel Riders" -> archived: true
#     lote B ( 67 conv): "Steel Riders" -> archived: false   <- inventado
#
# El segundo `false` no era de WhatsApp: lo fabricaba el traductor al construir
# la conversacion con un objeto vacio (`Boolean(undefined)` es `false`). Y ese
# `false` llegaba a la base y desarchivaba el chat.
#
# De aquel lote de 159, solo 41 traian metadatos de verdad. Las otras 118
# estaban afirmando un estado que nadie les habia dicho.


def _sync(jid: str, **estado):
    from app.wa.historial import parse_full_json

    return parse_full_json(
        {
            "sync_type": "RECENT",
            "progress": 100,
            "conversations": [{"jid": jid, "messages": [], **estado}],
        }
    )


def test_un_lote_SIN_metadatos_no_desarchiva(session, cuenta):
    from app.services.history_service import ingest_history_sync

    jid = _jid()
    # Lote A: llega con su `proto.Conversation`.
    ingest_history_sync(
        session,
        _sync(jid, name="Grupo", archived=True, locked=False, pinned_at=None),
        whatsapp_account_id=cuenta.id,
    )
    session.flush()

    # Lote B: la misma conversacion, solo por sus mensajes. El traductor manda
    # nulo en los cuatro campos porque no sabe nada de su estado.
    ingest_history_sync(
        session,
        _sync(jid, archived=None, locked=None, pinned_at=None, mute_until=None),
        whatsapp_account_id=cuenta.id,
    )
    session.flush()
    session.expire_all()

    fila = (
        session.query(Chat)
        .filter(Chat.jid == jid, Chat.whatsapp_account_id == cuenta.id)
        .one()
    )
    assert fila.archived is True, "un lote que no sabia nada desarchivo el chat"


def test_un_lote_que_SI_lo_sabe_puede_desarchivar(session, cuenta):
    """La otra mitad: desarchivar de verdad tiene que seguir funcionando."""
    from app.services.history_service import ingest_history_sync

    jid = _jid()
    ingest_history_sync(
        session, _sync(jid, archived=True), whatsapp_account_id=cuenta.id
    )
    session.flush()
    ingest_history_sync(
        session, _sync(jid, archived=False), whatsapp_account_id=cuenta.id
    )
    session.flush()
    session.expire_all()

    fila = (
        session.query(Chat)
        .filter(Chat.jid == jid, Chat.whatsapp_account_id == cuenta.id)
        .one()
    )
    assert fila.archived is False


def test_el_traductor_manda_NULO_cuando_no_tiene_metadatos():
    """Se comprueba en el propio JavaScript: es donde nace el dato.

    Comprobarlo solo del lado de Python dejaria pasar una regresion en el
    traductor, que es exactamente donde estaba el fallo.
    """
    import json
    import subprocess

    guion = (
        "const t=require('./wa_baileys/traducir.js');"
        "const lote={chats:[],messages:[{key:{remoteJid:'1@s.whatsapp.net',id:'A'}}]};"
        "const r=t.traducirHistorial(lote,()=>null);"
        "console.log(JSON.stringify(r.conversations[0]));"
    )
    salida = subprocess.run(
        ["node", "-e", guion], capture_output=True, text=True, timeout=30
    )
    assert salida.returncode == 0, salida.stderr
    conversacion = json.loads(salida.stdout)
    assert conversacion["archived"] is None, "invento un estado que no sabia"
    assert conversacion["locked"] is None
    assert conversacion["pinned_at"] is None
    assert conversacion["mute_until"] is None
