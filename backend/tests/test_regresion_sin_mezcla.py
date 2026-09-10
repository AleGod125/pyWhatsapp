"""Dos WhatsApp con los MISMOS contactos y conversaciones distintas.

EL ESCENARIO
------------
Un usuario de Google con dos cuentas de WhatsApp: la personal --grupo de
futbol con amigos-- y la del trabajo. **Los mismos contactos en las dos**, pero
conversaciones que no tienen nada que ver.

Lo que se veia: un chat mostraba futbol, luego trabajo, luego futbol otra vez.
Mensajes de las dos cuentas intercalados en el mismo hilo.

LO QUE ESTA PRUEBA FIJA
-----------------------
1. el mismo contacto en dos cuentas son DOS conversaciones, no una;
2. el mismo identificador de mensaje en las dos cuentas NO se descarta como
   duplicado -- que es lo que hacia desaparecer mensajes;
3. los mensajes de una no aparecen en la otra;
4. y el adjunto de una tampoco.

POR QUE EL ESQUEMA YA BASTA PARA ESTO
-------------------------------------
`chats` tiene `UNIQUE (whatsapp_account_id, jid)`, asi que `chat_id` es un
identificador que YA lleva la cuenta dentro. Deduplicar mensajes por
`(chat_id, wamid)` es per-cuenta de forma transitiva.

Lo que fallaba no era el esquema: era la ATRIBUCION -- quien decide bajo que
cuenta se escribe. Eso lo cubre `test_firma_de_cuenta_en_eventos.py`. Esto de
aqui comprueba la otra mitad: que con la atribucion correcta, el
almacenamiento de verdad los separa.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from app.models import Chat, Message, WhatsAppAccount
from app.models.schema import MediaFile


#: El contacto que las dos cuentas tienen en la agenda.
CONTACTO_COMUN = "573001234567@s.whatsapp.net"


class _Base:
    def __init__(self, session):
        self._session = session

    def transaction(self):
        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


@pytest.fixture
def dos_whatsapp(session, cuenta):
    """La cuenta personal y la del trabajo, del MISMO usuario."""
    trabajo_id = uuid.uuid4()
    trabajo = WhatsAppAccount(
        id=trabajo_id,
        user_id=cuenta.user_id,
        session_status="linked",
        session_storage_key=f"accounts/{trabajo_id}",
        display_name="Trabajo",
    )
    session.add(trabajo)
    session.flush()
    return cuenta, trabajo


def _chat(session, cuenta_id, jid=CONTACTO_COMUN):
    fila = Chat(jid=jid, chat_type="individual", whatsapp_account_id=cuenta_id)
    session.add(fila)
    session.flush()
    return fila


def _mensaje(session, chat, wamid, texto, ts=1700000000):
    fila = Message(
        chat_id=chat.id,
        chat_jid=chat.jid,
        whatsapp_message_id=wamid,
        message_type="text",
        text=texto,
        timestamp=ts,
        from_me=False,
        source="initial_history",
    )
    session.add(fila)
    session.flush()
    return fila


# ---------------------------------------------------------------------------
# 1. El mismo contacto son dos conversaciones
# ---------------------------------------------------------------------------


def test_el_mismo_contacto_en_dos_cuentas_son_dos_chats(session, dos_whatsapp):
    personal, trabajo = dos_whatsapp

    uno = _chat(session, personal.id)
    otro = _chat(session, trabajo.id)

    assert uno.id != otro.id, (
        "el mismo contacto colapso en una sola conversacion: futbol y trabajo "
        "acabarian en el mismo hilo"
    )
    assert uno.jid == otro.jid, "y sin embargo es el mismo contacto"


def test_dentro_de_UNA_cuenta_el_contacto_sigue_siendo_uno(session, cuenta):
    """Separar por cuenta no puede duplicar dentro de la misma cuenta."""
    from sqlalchemy.exc import IntegrityError

    _chat(session, cuenta.id)

    with pytest.raises(IntegrityError):
        _chat(session, cuenta.id)
        session.flush()


# ---------------------------------------------------------------------------
# 2. El mismo identificador de mensaje NO se descarta
# ---------------------------------------------------------------------------


def test_el_mismo_wamid_convive_en_las_dos_cuentas(session, dos_whatsapp):
    """En un grupo, el identificador es el MISMO para todos los que lo reciben.

    Deduplicando por `chat_jid` --que es identico entre cuentas-- el mensaje
    que le llega a la segunda se descartaba como duplicado del de la primera,
    en silencio. Por `chat_id` no puede pasar.
    """
    personal, trabajo = dos_whatsapp
    uno = _chat(session, personal.id)
    otro = _chat(session, trabajo.id)

    mismo_id = "3EB0ABCDEF1234567890"
    a = _mensaje(session, uno, mismo_id, "gol en el minuto 90")
    b = _mensaje(session, otro, mismo_id, "adjunto el informe")

    assert a.id != b.id, "un mensaje se descarto como duplicado del de la otra cuenta"
    assert a.text == "gol en el minuto 90"
    assert b.text == "adjunto el informe"


def test_dentro_de_UNA_cuenta_el_wamid_sigue_deduplicando(session, cuenta):
    """La otra mitad: el mismo mensaje dos veces en la misma cuenta es uno."""
    from sqlalchemy.exc import IntegrityError

    chat = _chat(session, cuenta.id)
    _mensaje(session, chat, "3EB0REPETIDO", "hola")

    with pytest.raises(IntegrityError):
        _mensaje(session, chat, "3EB0REPETIDO", "hola otra vez")
        session.flush()


def test_la_dedupe_es_por_chat_id_y_no_hay_respaldo():
    """El respaldo por `chat_jid` era una bomba: se quito.

    Si el indice faltara, deduplicar por jid cruzaria mensajes entre cuentas
    sin que nada lo dijera. Ahora eso es un error de arranque, no un silencio.
    """
    from app.services.account_scope import destino_de_dedupe_de_mensaje

    columnas = [c.name for c in destino_de_dedupe_de_mensaje()]

    assert columnas == ["chat_id", "whatsapp_message_id"]

    import inspect

    from app.services import account_scope

    fuente = inspect.getsource(account_scope.destino_de_dedupe_de_mensaje)
    assert "RuntimeError" in fuente, "volvio el respaldo silencioso por chat_jid"
    assert "return [Message.chat_jid" not in fuente


# ---------------------------------------------------------------------------
# 3. Lo de una cuenta no aparece en la otra
# ---------------------------------------------------------------------------


def test_los_mensajes_no_se_cruzan(session, dos_whatsapp):
    """El sintoma exacto: futbol, trabajo, futbol en el mismo hilo."""
    from sqlalchemy import select

    personal, trabajo = dos_whatsapp
    futbol = _chat(session, personal.id)
    curro = _chat(session, trabajo.id)

    for i, texto in enumerate(["quien juega el sabado", "yo me apunto"]):
        _mensaje(session, futbol, f"F{i}", texto, ts=1700000000 + i)
    for i, texto in enumerate(["te paso el presupuesto", "revisado"]):
        _mensaje(session, curro, f"T{i}", texto, ts=1700000000 + i)

    del_personal = session.execute(
        select(Message).where(Message.chat_id == futbol.id)
    ).scalars().all()
    del_trabajo = session.execute(
        select(Message).where(Message.chat_id == curro.id)
    ).scalars().all()

    assert {m.text for m in del_personal} == {"quien juega el sabado", "yo me apunto"}
    assert {m.text for m in del_trabajo} == {"te paso el presupuesto", "revisado"}
    assert not any(m.chat_id == curro.id for m in del_personal)
    assert not any(m.chat_id == futbol.id for m in del_trabajo)


def test_el_adjunto_lleva_su_cuenta(session, dos_whatsapp):
    """`media_files` ya no depende de que alguien se acuerde de unir con chats."""
    from sqlalchemy import select

    personal, trabajo = dos_whatsapp
    futbol = _chat(session, personal.id)
    curro = _chat(session, trabajo.id)
    m1 = _mensaje(session, futbol, "F-IMG", "foto del equipo")
    m2 = _mensaje(session, curro, "T-PDF", "el contrato")

    session.add(
        MediaFile(
            message_id=m1.id,
            chat_id=futbol.id,
            whatsapp_account_id=personal.id,
            media_type="image",
            download_status="pending",
        )
    )
    session.add(
        MediaFile(
            message_id=m2.id,
            chat_id=curro.id,
            whatsapp_account_id=trabajo.id,
            media_type="document",
            download_status="pending",
        )
    )
    session.flush()

    # Una consulta que NO une con `chats` y aun asi acota bien.
    suyos = session.execute(
        select(MediaFile).where(MediaFile.whatsapp_account_id == personal.id)
    ).scalars().all()

    assert len(suyos) == 1
    assert suyos[0].media_type == "image"


# ---------------------------------------------------------------------------
# 4. Y el esquema lo impide, no solo el codigo
# ---------------------------------------------------------------------------


def test_un_telefono_no_se_vincula_dos_veces(session, cuenta):
    """Ya no es solo una comprobacion en codigo: la base lo rechaza.

    Y va sobre `phone_number`, NO sobre `wa_pn`: este ultimo lleva el
    identificador de DISPOSITIVO pegado y cambia en cada re-vinculacion, asi
    que con la unicidad ahi bastaba volver a escanear para colar otra fila del
    mismo telefono. Se midio con la restriccion ya puesta.
    """
    from sqlalchemy.exc import IntegrityError

    cuenta.wa_pn = "573008927374@s.whatsapp.net"
    cuenta.phone_number = "573008927374"
    session.flush()

    gemela_id = uuid.uuid4()
    session.add(
        WhatsAppAccount(
            id=gemela_id,
            user_id=cuenta.user_id,
            session_status="linked",
            session_storage_key=f"accounts/{gemela_id}",
            # OTRO dispositivo del mismo numero: `wa_pn` distinto, persona
            # identica. Es el caso que se colaba.
            wa_pn="573008927374:30@s.whatsapp.net",
            phone_number="573008927374",
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_varias_cuentas_SIN_vincular_conviven(session, cuenta):
    """`wa_pn` NULL no choca: es el estado de quien acaba de pulsar "Agregar".

    PostgreSQL trata los NULL como distintos en una restriccion unica, y aqui
    eso es justo lo que se quiere.
    """
    cuenta.wa_pn = None
    cuenta.phone_number = None
    session.flush()

    for _ in range(3):
        ident = uuid.uuid4()
        session.add(
            WhatsAppAccount(
                id=ident,
                user_id=cuenta.user_id,
                session_status="never_linked",
                session_storage_key=f"accounts/{ident}",
                wa_pn=None,
                phone_number=None,
            )
        )
    session.flush()  # no debe lanzar
