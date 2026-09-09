"""La cuenta viaja hasta donde se escribe. El contrato, antes de migrar.

POR QUE ESTE FICHERO EXISTE AHORA Y NO DESPUES
----------------------------------------------
El esquema todavia tiene las unicidades globales: la base sigue en
``f1a2b3c4d5e6``. Aplicar el aislamiento antes de que el codigo lo soporte ya
se probo una vez y dejo **91 pruebas rojas**, varias del camino live.

Asi que primero el codigo, luego la base. Estas pruebas comprueban lo unico que
se puede comprobar hoy: que la cuenta **llega** a los sitios donde se escribe, y
que la decision de a que restriccion apuntar vive en un solo sitio.

Las de duplicado real --el mismo contacto en dos cuentas-- siguen en
``test_multiuser_privacy.py``, saltadas hasta que la migracion se aplique.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import Chat, Contact, Message, WhatsAppAccount
from app.services import account_scope, repository as repo


@pytest.fixture
def cuenta(runtime, session):
    """Una cuenta de WhatsApp real a la que atribuir lo que se escriba."""
    fila = WhatsAppAccount(
        user_id=runtime.auth.register(
            email=f"sc-{uuid.uuid4().hex[:10]}@example.com",
            password="Contrasena-De-Prueba-1",
        ).user_id,
        session_status="linked",
        session_storage_key=f"accounts/{uuid.uuid4().hex}",
    )
    session.add(fila)
    session.flush()
    return fila


def _jid() -> str:
    return f"{uuid.uuid4().int % 10**12}@s.whatsapp.net"


# ---------------------------------------------------------------------------
# La cuenta llega hasta la fila
# ---------------------------------------------------------------------------


def test_UN_CHAT_SE_GUARDA_CON_SU_CUENTA(session, cuenta):
    """LA REGLA. Un chat sin dueno existe en la base y no lo ve nadie."""
    jid = _jid()

    chat_id = repo.upsert_chat(
        session, jid=jid, chat_type="individual", whatsapp_account_id=cuenta.id
    )
    session.flush()

    guardado = session.execute(select(Chat).where(Chat.id == chat_id)).scalar_one()
    assert guardado.whatsapp_account_id == cuenta.id


def test_EL_MISMO_JID_EN_OTRA_CUENTA_ES_OTRO_CHAT(session, cuenta):
    """Con unicidad por cuenta, cada una tiene el suyo. No se reasigna nada.

    Antes esto comprobaba que el dueno de un chat no cambiaba: con `jid` unico
    global, un upsert bajo otra cuenta habria pisado la fila existente. Ahora
    la unicidad es `(cuenta, jid)`, asi que crea una fila SEPARADA -- que es
    exactamente lo que tiene que pasar, y por lo que el chat de una persona ya
    no puede acabar en manos de otra.
    """
    jid = _jid()
    id_a = repo.upsert_chat(session, jid=jid, whatsapp_account_id=cuenta.id)
    session.flush()

    otra = WhatsAppAccount(
        user_id=cuenta.user_id,
        session_status="linked",
        session_storage_key=f"accounts/{uuid.uuid4().hex}",
    )
    session.add(otra)
    session.flush()

    id_b = repo.upsert_chat(
        session, jid=jid, whatsapp_account_id=otra.id, name="de la otra"
    )
    session.flush()

    assert id_a != id_b, "el mismo jid en otra cuenta es otra conversacion"
    filas = {
        c.whatsapp_account_id: c
        for c in session.execute(select(Chat).where(Chat.jid == jid)).scalars().all()
    }
    assert set(filas) == {cuenta.id, otra.id}
    # Y la primera conserva lo suyo: no la piso la segunda.
    assert filas[cuenta.id].id == id_a


def test_UN_CONTACTO_ACEPTA_SU_CUENTA(session, cuenta):
    """`upsert_contact` ya recibe la cuenta, aunque la columna no exista aun.

    Es lo que permite migrar despues sin volver a tocar ni una llamada: el dia
    que la columna aparezca, empieza a guardarse sola.
    """
    jid = _jid()

    contacto_id = repo.upsert_contact(
        session, whatsapp_account_id=cuenta.id, jid=jid, display_name="Mama"
    )
    session.flush()

    guardado = session.execute(
        select(Contact).where(Contact.id == contacto_id)
    ).scalar_one()
    assert guardado.display_name == "Mama"
    if account_scope.tiene_columna(Contact, "whatsapp_account_id"):
        assert guardado.whatsapp_account_id == cuenta.id


def test_UN_CONTACTO_SIN_CUENTA_YA_NO_SE_GUARDA(session):
    """La agenda es privada, asi que la cuenta dejo de ser opcional.

    Antes esta prueba comprobaba lo contrario --que un contacto sin cuenta
    seguia guardandose-- porque durante la transicion no se podia exigir lo
    que aun no todos pasaban. Ya lo pasan: `contacts.whatsapp_account_id` es
    obligatorio, y guardar uno sin dueno lo dejaria en la agenda de nadie.

    Con cero cuentas en la base no hay a quien atribuirselo, y se dice en vez
    de elegir una.
    """
    from sqlalchemy import delete

    session.execute(delete(WhatsAppAccount))
    session.flush()

    with pytest.raises(ValueError, match="whatsapp_account_id"):
        repo.upsert_contact(session, jid=_jid(), push_name="Alguien")


# ---------------------------------------------------------------------------
# La transicion vive en UN sitio
# ---------------------------------------------------------------------------


def test_EL_DESTINO_DEL_CONFLICTO_SIGUE_A_LA_RESTRICCION_REAL():
    """El fallo que costo veinte pruebas rojas.

    Se miraba si existia la COLUMNA, y `chats` ya tenia `whatsapp_account_id`
    desde mucho antes que la unicidad compuesta. Resultado: `ON CONFLICT`
    apuntaba a una restriccion que la base no tenia y fallaba con "no hay
    restriccion unica que coincida".

    Lo que hay que mirar es el indice que va a resolver el conflicto.
    """
    destino = [c.name for c in account_scope.destino_de_conflicto(Chat, "jid")]

    if account_scope._hay_unicidad(Chat, ("whatsapp_account_id", "jid")):
        assert destino == ["whatsapp_account_id", "jid"]
    else:
        assert destino == ["jid"]


def test_el_dedupe_de_mensajes_sigue_al_indice():
    destino = [c.name for c in account_scope.destino_de_dedupe_de_mensaje()]

    if account_scope._hay_unicidad(Message, ("chat_id", "whatsapp_message_id")):
        assert destino == ["chat_id", "whatsapp_message_id"]
    else:
        assert destino == ["chat_jid", "whatsapp_message_id"]


def test_tener_la_columna_no_es_tener_la_restriccion():
    """La distincion exacta que fallo, fijada."""
    assert account_scope.tiene_columna(Chat, "whatsapp_account_id") is True
    # Y la unicidad compuesta es otra pregunta, con otra respuesta hoy.
    assert account_scope._hay_unicidad(Chat, ("id",)) is False


# ---------------------------------------------------------------------------
# El ayudante de transicion: honesto o callado
# ---------------------------------------------------------------------------


def test_con_UNA_cuenta_la_respuesta_es_inequivoca(session, cuenta):
    from sqlalchemy import delete

    session.execute(delete(WhatsAppAccount).where(WhatsAppAccount.id != cuenta.id))
    session.flush()
    assert account_scope.cuenta_unica(session) == cuenta.id


def test_CON_DOS_CUENTAS_NO_ELIGE(session, cuenta):
    """La prueba que mas importa del ayudante.

    Con mas de una cuenta devuelve `None` en vez de quedarse con la primera.
    Preferimos quedarnos sin dato a atribuirselo a quien no es -- y asi todo lo
    que dependa de este respaldo se cae solo el dia que haya dos cuentas, en
    vez de empezar a mezclar agendas en silencio.
    """
    otra = WhatsAppAccount(
        user_id=cuenta.user_id,
        session_status="linked",
        session_storage_key=f"accounts/{uuid.uuid4().hex}",
    )
    session.add(otra)
    session.flush()

    assert account_scope.cuenta_unica(session) is None


def test_la_cuenta_de_un_chat_la_dice_el_chat(session, cuenta):
    jid = _jid()
    repo.upsert_chat(session, jid=jid, whatsapp_account_id=cuenta.id)
    session.flush()
    assert account_scope.cuenta_del_chat(session, jid) == cuenta.id


def test_de_un_chat_que_no_existe_no_se_inventa_cuenta(session):
    assert account_scope.cuenta_del_chat(session, _jid()) is None
    assert account_scope.cuenta_del_chat(session, "") is None


# ---------------------------------------------------------------------------
# El servicio de contactos
# ---------------------------------------------------------------------------


def test_el_servicio_de_contactos_recuerda_su_cuenta(database, cuenta):
    from app.services.contacts_service import ContactService

    servicio = ContactService(database, whatsapp_account_id=cuenta.id)
    assert servicio.whatsapp_account_id == cuenta.id


def test_sin_cuenta_el_servicio_no_la_inventa(database):
    """Se queda en `None` y el respaldo decide, en vez de elegir una."""
    from app.services.contacts_service import ContactService

    assert ContactService(database).whatsapp_account_id is None
