"""Los NOMBRES de las conversaciones, que es lo que el usuario ve.

EL FALLO, MEDIDO SOBRE LA BASE REAL
-----------------------------------
Tras una extraccion completa con Baileys::

    chats            205   (todos con al menos un mensaje)
    mensajes        5452
    contactos          4   <- los cuatro, de grupos
    con display_name   0
    con lid            0

El panel enseñaba identificadores porque la tabla de contactos estaba vacia.
Y estaba vacia por tres fugas distintas, cada una con su prueba aqui:

1. ``contacts.set`` NO EXISTE en Baileys 6.7.24. La agenda viaja dentro de
   ``messaging-history.set``, de donde solo se leian pares ``[jid, nombre]``
   -- se tiraba el ``lid``, que es justo la pieza que hace falta.
2. ``guardar_par_lid`` hacia un ``UPDATE ... WHERE jid = pn``: solo rellenaba
   el hueco de un contacto que YA existiera. En una instalacion nueva no
   existe ninguno, asi que cada par que llegaba se descartaba.
3. El cosechador del par solo corria sobre los mensajes EN VIVO. Los 5452 del
   historial pasaron de largo.

POR QUE IMPORTA EL ``@lid``
---------------------------
Las conversaciones individuales llegan identificadas por ``@lid`` mientras que
el nombre esta guardado contra el numero. Sin la correspondencia, el JOIN no
casa y no hay nombre que enseñar aunque este en la tabla.
"""

from __future__ import annotations

import pytest

from app.services import repository as repo


class _FakeDatabase:
    """Reutiliza la sesion transaccional de la prueba en vez de abrir otra."""

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
# El par PN <-> LID
# ---------------------------------------------------------------------------


def test_el_par_crea_el_contacto_si_no_existia(session, cuenta):
    """La fuga que dejo `contacts` con cuatro filas y 205 chats.

    El par llega en la clave de cada mensaje. Si no hay donde ponerlo, se
    crea: son dos identificadores del mismo telefono, no un dato de nadie.
    """
    from app.services.contacts_service import guardar_par_lid

    escrito = guardar_par_lid(
        _FakeDatabase(session),
        "82025587417265@lid",
        "573001234567@s.whatsapp.net",
        whatsapp_account_id=cuenta.id,
    )

    assert escrito is True
    fila = _contacto(session, "573001234567@s.whatsapp.net")
    assert fila is not None, "el par se descarto por no tener donde ponerlo"
    assert fila.lid == "82025587417265@lid"
    assert fila.display_name is None, "no se inventa un nombre"


def test_el_par_no_pisa_el_nombre_que_ya_hubiera(session, cuenta):
    from app.services.contacts_service import guardar_par_lid

    repo.upsert_contact(
        session,
        whatsapp_account_id=cuenta.id,
        jid="573001234567@s.whatsapp.net",
        display_name="Mamá",
    )
    session.flush()

    guardar_par_lid(
        _FakeDatabase(session),
        "82025587417265@lid",
        "573001234567@s.whatsapp.net",
        whatsapp_account_id=cuenta.id,
    )

    fila = _contacto(session, "573001234567@s.whatsapp.net")
    assert fila.display_name == "Mamá"
    assert fila.lid == "82025587417265@lid"


@pytest.mark.parametrize(
    "lid,pn",
    [
        ("573001234567@s.whatsapp.net", "573001234567@s.whatsapp.net"),  # dos PN
        ("82025587417265@lid", "82025587417265@lid"),  # dos LID
        (None, "573001234567@s.whatsapp.net"),
        ("82025587417265@lid", None),
    ],
)
def test_un_par_que_no_lo_es_no_se_guarda(session, cuenta, lid, pn):
    """Mezclar los dos espacios de identificadores corrompe la tabla."""
    from app.services.contacts_service import guardar_par_lid

    assert (
        guardar_par_lid(
            _FakeDatabase(session), lid, pn, whatsapp_account_id=cuenta.id
        )
        is False
    )


# ---------------------------------------------------------------------------
# La resolucion del nombre
# ---------------------------------------------------------------------------


def test_un_chat_por_LID_toma_el_nombre_del_contacto_por_TELEFONO(session, cuenta):
    """El JOIN que estaba dando en vacio.

    El chat viene por `@lid`, el nombre esta guardado contra el numero, y lo
    unico que los une es `contacts.lid`.
    """
    from app.models import Chat, Message

    repo.upsert_contact(
        session,
        whatsapp_account_id=cuenta.id,
        jid="573001234567@s.whatsapp.net",
        lid="82025587417265@lid",
        display_name="Isaac",
    )
    chat = Chat(
        jid="82025587417265@lid",
        chat_type="individual",
        whatsapp_account_id=cuenta.id,
    )
    session.add(chat)
    session.flush()
    session.add(
        Message(
            chat_id=chat.id,
            chat_jid=chat.jid,
            whatsapp_message_id="M1",
            message_type="text",
            text="hola",
            timestamp=1,
        )
    )
    session.flush()

    fila = _resumen(session, cuenta, "82025587417265@lid")
    assert fila is not None
    assert fila.display_name == "Isaac"


def test_sin_el_par_no_se_ensena_el_identificador_interno():
    """``21935119425699 (LID)`` no le dice nada a nadie."""
    nombre = repo.display_name_for("21935119425699@lid")
    assert nombre == "Desconocido (lid)"
    assert "21935119425699" not in nombre


def test_con_el_telefono_conocido_se_ensena_el_numero():
    """Es legible y es la misma persona. No se DEDUCE del LID: viene del par."""
    assert (
        repo.display_name_for(
            "21935119425699@lid", None, None, phone_jid="573001112233@s.whatsapp.net"
        )
        == "+573001112233"
    )


def test_el_nombre_de_la_agenda_manda_sobre_el_publico():
    assert repo.display_name_for("x@lid", None, "Mamá", "Rosa M.") == "Mamá"
    assert repo.display_name_for("x@lid", None, None, "Rosa M.") == "Rosa M."


# ---------------------------------------------------------------------------
# El ruido que tapaba las conversaciones de verdad
# ---------------------------------------------------------------------------


def test_una_conversacion_con_solo_avisos_del_sistema_no_se_lista(session, cuenta):
    """141 de 205 tenian exactamente un aviso de cifrado y nada mas.

    WhatsApp los reparte a contactos con los que nunca se ha hablado. No son
    conversaciones, y tapaban las 41 que si lo eran.
    """
    from app.models import Chat, Message

    for jid, tipo in (("111@lid", "system"), ("222@lid", "text")):
        chat = Chat(jid=jid, chat_type="individual", whatsapp_account_id=cuenta.id)
        session.add(chat)
        session.flush()
        session.add(
            Message(
                chat_id=chat.id,
                chat_jid=jid,
                whatsapp_message_id=f"M-{jid}",
                message_type=tipo,
                text="x",
                timestamp=1,
            )
        )
    session.flush()

    visibles = {
        c.jid
        for c in repo.list_chat_summaries(
            session, accounts=[cuenta.id], solo_con_mensajes=True
        )
    }
    assert "222@lid" in visibles
    assert "111@lid" not in visibles

    todas = {c.jid for c in repo.list_chat_summaries(session, accounts=[cuenta.id])}
    assert "111@lid" in todas, "ocultar no es borrar: siguen ahi"


@pytest.mark.parametrize("jid", ["0@s.whatsapp.net", "status@broadcast"])
def test_lo_que_no_es_una_conversacion_nunca_se_lista(session, cuenta, jid):
    """`0@s.whatsapp.net` se pintaba como un contacto llamado "+0"."""
    from app.models import Chat, Message

    chat = Chat(jid=jid, chat_type="individual", whatsapp_account_id=cuenta.id)
    session.add(chat)
    session.flush()
    session.add(
        Message(
            chat_id=chat.id,
            chat_jid=jid,
            whatsapp_message_id=f"M-{jid}",
            message_type="text",
            text="x",
            timestamp=1,
        )
    )
    session.flush()

    for solo in (True, False):
        listados = {
            c.jid
            for c in repo.list_chat_summaries(
                session, accounts=[cuenta.id], solo_con_mensajes=solo
            )
        }
        assert jid not in listados, f"solo_con_mensajes={solo}"


# ---------------------------------------------------------------------------
# Ayudas
# ---------------------------------------------------------------------------


def _contacto(session, jid):
    from sqlalchemy import select

    from app.models import Contact

    return session.execute(
        select(Contact).where(Contact.jid == jid)
    ).scalars().first()


def _resumen(session, cuenta, jid):
    for fila in repo.list_chat_summaries(session, accounts=[cuenta.id]):
        if fila.jid == jid:
            return fila
    return None

# ---------------------------------------------------------------------------
# Los dos caminos que pintan una fila tienen que decidir igual
# ---------------------------------------------------------------------------


def test_el_listado_y_los_avisos_en_vivo_usan_LA_MISMA_regla(session, cuenta):
    """El filtro se colaba por la puerta de atras.

    Una fila del sidebar llega por dos sitios: `/chats` y los avisos en vivo,
    que la mandan ya montada para no obligar a recargar. El listado filtraba y
    los avisos no, asi que durante una excavacion aparecian "+0" y chats con
    un solo aviso de cifrado -- justo lo que el filtro existe para evitar.
    """
    from app.models import Chat, Message

    creados = {}
    for jid, tipo in (
        ("visible@lid", "text"),
        ("solo-sistema@lid", "system"),
        ("0@s.whatsapp.net", "text"),
        ("status@broadcast", "text"),
    ):
        chat = Chat(jid=jid, chat_type="individual", whatsapp_account_id=cuenta.id)
        session.add(chat)
        session.flush()
        session.add(
            Message(
                chat_id=chat.id,
                chat_jid=jid,
                whatsapp_message_id=f"M-{jid}",
                message_type=tipo,
                text="x",
                timestamp=1,
            )
        )
        creados[jid] = chat.id
    session.flush()

    # El listado, que filtra en SQL.
    del_listado = {
        c.jid
        for c in repo.list_chat_summaries(
            session, accounts=[cuenta.id], solo_con_mensajes=True
        )
    }

    # Y el camino de una sola fila, que es el de los avisos en vivo.
    de_uno_en_uno = {
        jid
        for jid, chat_id in creados.items()
        if repo.se_lista(repo.chat_summary(session, chat_id))
    }

    assert de_uno_en_uno == {"visible@lid"}
    assert del_listado & set(creados) == de_uno_en_uno, (
        "el listado y los avisos en vivo no filtran igual"
    )


def test_se_lista_cuenta_los_mensajes_REALES(session, cuenta):
    """Un chat con mil avisos del sistema sigue sin ser una conversacion."""
    from app.models import Chat, Message

    chat = Chat(jid="ruido@lid", chat_type="individual", whatsapp_account_id=cuenta.id)
    session.add(chat)
    session.flush()
    for i in range(5):
        session.add(
            Message(
                chat_id=chat.id,
                chat_jid="ruido@lid",
                whatsapp_message_id=f"S{i}",
                message_type="system",
                text="cifrado",
                timestamp=i,
            )
        )
    session.flush()

    resumen = repo.chat_summary(session, chat.id)
    assert resumen.message_count == 5
    assert resumen.real_message_count == 0
    assert repo.se_lista(resumen) is False
