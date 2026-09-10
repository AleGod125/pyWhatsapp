"""La excavacion de un WhatsApp no toca la del otro.

EL PROBLEMA, Y POR QUE NO SE VEIA CON UNA CUENTA
------------------------------------------------
Desde que la unicidad de `chats` es ``(whatsapp_account_id, jid)``, **el mismo
jid existe una vez por cada cuenta que hable con ese contacto**. Eso es lo que
se buscaba: la conversacion de Dora con Marta y la de Ale con Marta son dos
conversaciones distintas.

Pero quedaba codigo de cuando solo podia haber una cuenta, que sigue buscando
el estado del historial ``WHERE chat_jid = ...``. Con un telefono, "el chat
con ese jid" y "el chat de esta cuenta con ese jid" eran lo mismo. Con dos --y
dos telefonos de la misma casa comparten casi toda la agenda-- deja de serlo.

Lo que hacia:

* ``reconcile_stuck_fetching`` recogia los jid atascados y hacia
  ``UPDATE ... WHERE chat_jid IN (...)``. La conversacion de A se quedaba
  atascada y la de B, que iba bien, cambiaba de estado sin motivo. Y el
  destino se decidia con el ancla de una de las dos: si A tenia ancla y B no,
  la de B acababa en ``pending``, o sea en la cola para pedir historial sin
  con que pedirlo.
* ``_bump_no_progress`` buscaba con ``scalar_one_or_none()`` por jid. Con dos
  filas eso no devuelve la equivocada: **lanza** ``MultipleResultsFound``, y
  el ciclo de esa conversacion muere sin decir por que.

``chat_id`` es unico y ya lleva la cuenta dentro. Es el criterio correcto.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from app.models import Chat, ChatHistoryState, WhatsAppAccount


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
def dos_cuentas(session, cuenta):
    """Dos telefonos del mismo usuario, como Dora y Ale."""
    otra_id = uuid.uuid4()
    otra = WhatsAppAccount(
        id=otra_id,
        user_id=cuenta.user_id,
        session_status="linked",
        session_storage_key=f"accounts/{otra_id}",
        display_name="Ale",
    )
    session.add(otra)
    session.flush()
    return cuenta, otra


def _chat(session, cuenta_id, jid, *, estado, ancla=None):
    """Un chat con su fila de estado. `ancla` la hace excavable."""
    fila = Chat(jid=jid, chat_type="individual", whatsapp_account_id=cuenta_id)
    session.add(fila)
    session.flush()
    estado_fila = ChatHistoryState(
        chat_id=fila.id,
        chat_jid=jid,
        history_status=estado,
        oldest_message_id=ancla,
        oldest_message_timestamp=1700000000 if ancla else None,
    )
    session.add(estado_fila)
    session.flush()
    return fila, estado_fila


# ---------------------------------------------------------------------------
# 1. Reconciliar lo atascado de una cuenta no toca la otra
# ---------------------------------------------------------------------------


def test_desatascar_un_chat_no_toca_el_mismo_contacto_de_la_otra_cuenta(
    session, settings, dos_cuentas
):
    """El caso exacto: dos telefonos que hablan con la misma persona."""
    from app.services.maintenance_service import MaintenanceService

    mia, suya = dos_cuentas
    marta = "573001112233@s.whatsapp.net"

    # La conversacion de A esta atascada; la de B va bien.
    _, atascado = _chat(session, mia.id, marta, estado="fetching", ancla="ANCLA-A")
    _, sano = _chat(session, suya.id, marta, estado="exhausted", ancla="ANCLA-B")

    servicio = MaintenanceService(_Base(session), settings)
    servicio.reconcile_stuck_fetching(_informe())

    session.expire_all()
    assert atascado.history_status == "pending", "el atascado no se desatasco"
    assert sano.history_status == "exhausted", (
        "se cambio el estado de la conversacion de la OTRA cuenta, que no "
        "estaba atascada"
    )


def test_el_ancla_que_decide_es_la_DE_ESA_conversacion(
    session, settings, dos_cuentas
):
    """Si A tiene ancla y B no, la de B no puede acabar en `pending`.

    `pending` la mete en la cola para pedir historial. Sin ancla no hay con
    que pedirlo, asi que se queda dando vueltas sin avanzar nunca.
    """
    from app.services.maintenance_service import MaintenanceService

    mia, suya = dos_cuentas
    marta = "573001112233@s.whatsapp.net"

    _, con = _chat(session, mia.id, marta, estado="fetching", ancla="ANCLA-A")
    _, sin = _chat(session, suya.id, marta, estado="fetching", ancla=None)

    servicio = MaintenanceService(_Base(session), settings)
    servicio.reconcile_stuck_fetching(_informe())

    session.expire_all()
    assert con.history_status == "pending", "el que tiene ancla vuelve a la cola"
    assert sin.history_status == "waiting_seed", (
        "el que NO tiene ancla acabo en 'pending': pediria historial sin cursor"
    )


def _informe():
    from app.services.maintenance_service import ReconcileReport

    return ReconcileReport()


# ---------------------------------------------------------------------------
# 2. Contar intentos fallidos no cruza cuentas
# ---------------------------------------------------------------------------


def test_contar_un_intento_fallido_no_revienta_con_dos_cuentas(
    session, settings, dos_cuentas
):
    """`scalar_one_or_none()` por jid LANZA en cuanto hay dos filas.

    No es que devuelva la equivocada: mata el ciclo de esa conversacion sin
    dejar el motivo real en ninguna parte.
    """
    from app.services.backfill_service import BackfillService

    mia, suya = dos_cuentas
    marta = "573001112233@s.whatsapp.net"
    _, mio = _chat(session, mia.id, marta, estado="pending", ancla="A")
    _, suyo = _chat(session, suya.id, marta, estado="pending", ancla="B")

    motor = object.__new__(BackfillService)
    motor._database = _Base(session)
    motor.whatsapp_account_id = mia.id

    cuantos = motor._bump_no_progress(marta)

    session.expire_all()
    assert cuantos == 1
    assert mio.consecutive_no_progress == 1
    assert suyo.consecutive_no_progress == 0, (
        "se conto el intento fallido en la cuenta equivocada: esa "
        "conversacion se daria por agotada sin haberlo intentado"
    )


def test_sin_cuenta_puesta_sigue_funcionando(session, settings, cuenta):
    """Una sola cuenta es el caso de siempre y no puede romperse."""
    from app.services.backfill_service import BackfillService

    marta = "573009998877@s.whatsapp.net"
    _, estado = _chat(session, cuenta.id, marta, estado="pending", ancla="A")

    motor = object.__new__(BackfillService)
    motor._database = _Base(session)
    motor.whatsapp_account_id = None

    assert motor._bump_no_progress(marta) == 1
    session.expire_all()
    assert estado.consecutive_no_progress == 1


# ---------------------------------------------------------------------------
# 3. Y los candidatos a excavar son solo los propios
# ---------------------------------------------------------------------------


def test_el_motor_de_una_cuenta_no_ve_los_chats_de_la_otra(
    session, settings, dos_cuentas
):
    """Pedirle a un telefono el historial de un chat que no conoce es ACK y nada."""
    from app.services.backfill_service import BackfillService

    mia, suya = dos_cuentas
    _chat(session, mia.id, "573001112233@s.whatsapp.net", estado="pending", ancla="A")
    _chat(session, suya.id, "573004445566@s.whatsapp.net", estado="pending", ancla="B")

    motor = object.__new__(BackfillService)
    motor._database = _Base(session)
    motor.whatsapp_account_id = mia.id
    motor._own_jids = set()

    jids = {jid for _, jid in motor.chats_to_process()}

    assert "573001112233@s.whatsapp.net" in jids
    assert "573004445566@s.whatsapp.net" not in jids, (
        "el motor de una cuenta cogio la conversacion de la otra"
    )
