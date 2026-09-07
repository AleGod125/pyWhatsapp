"""Dos personas hablando con el MISMO contacto. Ni se mezclan ni revientan.

EL FALLO, MEDIDO
----------------
Desde que la unicidad es ``(whatsapp_account_id, jid)`` --que es lo que se
buscaba: la conversacion de A con Marta y la de B con Marta son dos-- el mismo
JID existe en varias filas. Todo el codigo que resolvia ``jid -> chat_id`` con
``scalar_one_or_none()`` empezo a romperse en cuanto existieron dos cuentas::

    ERROR app.db: Reconciliacion 'cursor_coherence' fallo
    sqlalchemy.exc.MultipleResultsFound: Multiple rows were found
    ERROR app.db: Reconciliacion 'seed_states' fallo

Aparecio solo, en el mantenimiento real, al crearse la segunda cuenta.

Y EL ARREGLO FACIL ERA PEOR QUE EL FALLO
----------------------------------------
Cambiar ``scalar_one_or_none()`` por ``.first()`` habria quitado la excepcion
y dejado algo mucho peor: la base no garantiza ningun orden, asi que le
atribuiria a una persona la conversacion de otra, en silencio y sin traza. Con
cuenta se responde exacto; sin ella, solo cuando hay UNA candidata.

Lo mismo valia para los ``UPDATE ... WHERE chat_jid IN (...)``: tocaban las
filas de LAS DOS cuentas.
"""

from __future__ import annotations

import uuid

import pytest

from app.models import Chat, User, WhatsAppAccount
from app.services.account_scope import chat_id_de, cuenta_del_chat

#: El contacto que las dos personas tienen en su agenda.
CONTACTO = "573001234567@s.whatsapp.net"


def _cuenta_nueva(session) -> WhatsAppAccount:
    usuario = User(email=f"m-{uuid.uuid4().hex[:10]}@example.com", password_hash="x")
    session.add(usuario)
    session.flush()
    id_cuenta = uuid.uuid4()
    fila = WhatsAppAccount(
        id=id_cuenta,
        user_id=usuario.id,
        session_status="linked",
        session_storage_key=f"accounts/{id_cuenta}",
    )
    session.add(fila)
    session.flush()
    return fila


@pytest.fixture
def dos_cuentas_un_contacto(session):
    """A y B, cada una con SU conversacion con el mismo contacto."""
    salida = []
    for _ in range(2):
        cuenta = _cuenta_nueva(session)
        chat = Chat(
            jid=CONTACTO, chat_type="individual", whatsapp_account_id=cuenta.id
        )
        session.add(chat)
        session.flush()
        salida.append((cuenta, chat))
    return salida


def test_las_dos_conversaciones_existen_y_son_distintas(dos_cuentas_un_contacto):
    (_, chat_a), (_, chat_b) = dos_cuentas_un_contacto

    assert chat_a.id != chat_b.id
    assert chat_a.jid == chat_b.jid == CONTACTO


def test_con_la_cuenta_se_resuelve_la_conversacion_correcta(
    session, dos_cuentas_un_contacto
):
    (cuenta_a, chat_a), (cuenta_b, chat_b) = dos_cuentas_un_contacto

    assert chat_id_de(session, CONTACTO, account_id=cuenta_a.id) == chat_a.id
    assert chat_id_de(session, CONTACTO, account_id=cuenta_b.id) == chat_b.id


def test_sin_cuenta_NO_se_elige_una_al_azar(session, dos_cuentas_un_contacto):
    """Devolver "la primera" es entregarle a alguien la conversacion de otro."""
    assert chat_id_de(session, CONTACTO) is None


def test_sin_ambiguedad_si_se_responde(session):
    cuenta = _cuenta_nueva(session)
    jid = f"{uuid.uuid4().int % 10**12}@s.whatsapp.net"
    chat = Chat(jid=jid, chat_type="individual", whatsapp_account_id=cuenta.id)
    session.add(chat)
    session.flush()

    assert chat_id_de(session, jid) == chat.id


def test_cuenta_del_chat_no_revienta_con_dos(session, dos_cuentas_un_contacto):
    """Reventaba con MultipleResultsFound y tumbaba la reconciliacion entera."""
    assert cuenta_del_chat(session, CONTACTO) is None


def test_el_cursor_no_revienta_con_el_contacto_compartido(
    session, dos_cuentas_un_contacto
):
    """Es exactamente lo que tumbaba 'cursor_coherence' y 'seed_states'."""
    from app.history.cursor import get_valid_history_cursor

    # No debe lanzar. Que devuelva ancla o no depende de los datos; lo que se
    # comprueba aqui es que la ambiguedad no explota.
    get_valid_history_cursor(session, chat_jid=CONTACTO)


def test_el_cursor_distingue_las_dos_por_chat_id(session, dos_cuentas_un_contacto):
    from app.history.cursor import get_valid_history_cursor

    (_, chat_a), (_, chat_b) = dos_cuentas_un_contacto
    for chat in (chat_a, chat_b):
        get_valid_history_cursor(session, chat_jid=CONTACTO, chat_id=chat.id)


def test_ninguna_reconciliacion_escribe_por_chat_jid():
    """Un `UPDATE ... WHERE chat_jid IN (...)` toca las filas de LAS DOS."""
    import inspect

    from app.services.maintenance_service import MaintenanceService
    from app.services.seed_recovery import SeedRecovery

    for funcion in (
        MaintenanceService.reconcile_cursor_coherence,
        SeedRecovery.classify,
    ):
        codigo = chr(10).join(
            linea
            for linea in inspect.getsource(funcion).splitlines()
            if not linea.lstrip().startswith("#")
        )
        assert "ChatHistoryState.chat_jid.in_" not in codigo, funcion.__qualname__
        assert "ChatHistoryState.chat_id.in_" in codigo, funcion.__qualname__
