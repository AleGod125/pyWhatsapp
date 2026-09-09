"""Una referencia guardada en un blob ya leido NO puede quedar enterrada.

EL FALLO, MEDIDO SOBRE LA BASE REAL
-----------------------------------
La conversacion ``170686312136883@lid`` llevaba **120 referencias validas**
dentro de blobs marcados como escaneados, y seguia en ``waiting_seed`` sin
ninguna forma de salir de ahi.

La causa es una carrera que se da SIEMPRE en un arranque limpio: el
``INITIAL_BOOTSTRAP`` llega antes de que existan las filas de conversacion,
asi que sus referencias se rechazan con "no se pudo resolver el chat"... y el
blob queda marcado como leido igualmente. Cuando la conversacion aparece un
segundo despues, nadie vuelve a abrir ese archivo. Nunca.

Y el ahorro que justificaba saltarselos no existia: releer los 192 blobs
enteros cuesta 0,4 segundos frente a 0,1 de comparar huellas.

LO QUE PROTEGEN ESTAS PRUEBAS
-----------------------------
Que una referencia real acabe aplicada aunque llegara antes de tiempo, y
--igual de importante-- que no se invente ninguna cuando no la hay.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.history.seed_collector import RecentSeedCollector, SeedCandidate
from app.models import Chat, ChatHistoryState, HistorySeed

ANCLA = "2A2B4C5F24E0F2E04AA2"
CUANDO = 1788655482


class _DatabaseDeSesion:
    """Reutiliza la sesion del test: nada se escribe fuera de la transaccion."""

    def __init__(self, session):
        self._session = session

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


def _chat_esperando(session, cuenta, jid: str) -> Chat:
    chat = Chat(jid=jid, chat_type="individual", name="Conversacion sin historial", whatsapp_account_id=cuenta.id)
    session.add(chat)
    session.flush()
    session.add(
        ChatHistoryState(chat_id=chat.id, chat_jid=jid, history_status="waiting_seed")
    )
    session.flush()
    return chat


def _colector(session, cuenta, usuario) -> RecentSeedCollector:
    colector = RecentSeedCollector(_DatabaseDeSesion(session))
    colector.account_id = cuenta
    colector.user_id = usuario
    return colector


@pytest.fixture
def escenario(session, runtime, cuenta):
    """Una conversacion que espera, con dueno real, lista para recibir anclas.

    La cuenta la trae la fixture, no se rebusca en la base. Antes se cogia
    "la primera que hubiera", que en la practica era la cuenta REAL de quien
    ejecutaba la suite: la prueba pasaba prestada y dejaba de pasar en cuanto
    la maquina no tenia ningun WhatsApp vinculado.
    """
    # Un identificador propio de cada ejecucion: la suite comparte base con el
    # entorno local, donde la conversacion del caso real ya existe.
    jid = f"{uuid.uuid4().int % 10**15}@lid"
    chat = _chat_esperando(session, cuenta, jid)
    return {
        "session": session,
        "chat": chat,
        "jid": jid,
        "colector": _colector(session, cuenta.id, cuenta.user_id),
    }


def _estado(escenario) -> ChatHistoryState:
    return (
        escenario["session"]
        .execute(
            select(ChatHistoryState).where(
                ChatHistoryState.chat_id == escenario["chat"].id
            )
        )
        .scalar_one()
    )


def _observar(escenario, wamid=ANCLA, cuando=CUANDO, fuente="initial_bootstrap"):
    return escenario["colector"].observe(
        SeedCandidate(
            chat_jid=escenario["jid"],
            wa_msg_id=wamid,
            timestamp=cuando,
            from_me=False,
            source=fuente,
        )
    )


# ---------------------------------------------------------------------------
# El rescate
# ---------------------------------------------------------------------------


def test_una_referencia_real_saca_a_la_conversacion_de_la_espera(escenario):
    """LA REGLA. Es el caso exacto de 170686312136883@lid."""
    resultado = _observar(escenario)

    assert resultado.aceptada
    assert resultado.desperto, "la conversacion tenia que despertar"
    assert _estado(escenario).history_status == "pending"


def test_la_referencia_queda_guardada_para_el_motor(escenario):
    """No basta con cambiar el estado: el cursor tiene que ser el mismo."""
    _observar(escenario)
    estado = _estado(escenario)
    assert estado.oldest_message_id == ANCLA
    assert int(estado.oldest_message_timestamp) == CUANDO


def test_reevaluar_una_conversacion_que_YA_tenia_su_ancla_la_rescata(escenario):
    """La repesca del ciclo manual, en una linea.

    La referencia esta guardada desde hace rato --llego cuando la conversacion
    todavia no existia-- y nadie la habia mirado. Volver a preguntar basta.
    """
    sesion = escenario["session"]
    sesion.add(
        HistorySeed(
            user_id=escenario["colector"].user_id,
            whatsapp_account_id=escenario["colector"].account_id,
            chat_id=escenario["chat"].id,
            chat_jid=escenario["jid"],
            wa_msg_id=ANCLA,
            timestamp=CUANDO,
            from_me=False,
            source="initial_bootstrap",
        )
    )
    sesion.flush()
    assert _estado(escenario).history_status == "waiting_seed"

    assert escenario["colector"].promote_waiting_chat(escenario["chat"].id) is True
    assert _estado(escenario).history_status == "pending"


def test_la_mas_antigua_gana(escenario):
    """Lo que queda por recuperar esta ANTES del ancla mas vieja.

    Se comprueba sobre ``get_valid_history_cursor``, que es lo que el motor
    lee cada vez que va a pedir, y no sobre la columna del estado: esa es una
    copia que solo se reescribe cuando la excavacion avanza. Si un ancla mas
    vieja aparece despues de que la conversacion ya despertara, la columna se
    queda como estaba y aun asi se pide desde la vieja, que es lo correcto.
    """
    from app.history.cursor import get_valid_history_cursor

    _observar(escenario)
    _observar(escenario, wamid="3B3C5D6027F1A3B15CC3", cuando=CUANDO - 9000)

    ancla = get_valid_history_cursor(
        escenario["session"], chat_id=escenario["chat"].id, chat_jid=escenario["jid"]
    )
    assert ancla is not None
    assert ancla.wa_msg_id == "3B3C5D6027F1A3B15CC3"
    assert ancla.timestamp == CUANDO - 9000


# ---------------------------------------------------------------------------
# LO QUE NO SE HACE: inventar
# ---------------------------------------------------------------------------


def test_SIN_REFERENCIA_REAL_SE_SIGUE_ESPERANDO(escenario):
    """La prueba que mas importa de este parche.

    De 32 conversaciones esperando en la base real, el rescate saca a UNA. Las
    otras 31 no tienen ni una referencia en ningun sitio, y lo correcto es que
    sigan esperando: un identificador inventado no traeria historial, traeria
    una peticion que el telefono rechaza y un chat marcado como listo que no
    lo esta.
    """
    assert escenario["colector"].promote_waiting_chat(escenario["chat"].id) is False
    assert _estado(escenario).history_status == "waiting_seed"


@pytest.mark.parametrize("wamid", ["", "   ", "local-123", "fake-abc"])
def test_un_candidato_invalido_no_promueve(escenario, wamid):
    """Ni ids locales, ni vacios, ni fabricados."""
    _observar(escenario, wamid=wamid)
    assert _estado(escenario).history_status == "waiting_seed"


def test_una_marca_de_tiempo_no_sustituye_a_una_referencia(escenario):
    """Sin identificador real no hay ancla, por muy buena que sea la fecha."""
    _observar(escenario, wamid="", fuente="live")
    assert _estado(escenario).history_status == "waiting_seed"


def test_la_misma_ancla_dos_veces_es_una(escenario):
    """Releer todos los blobs pasa la misma referencia muchas veces."""
    for _ in range(3):
        _observar(escenario)
    guardadas = (
        escenario["session"]
        .execute(select(HistorySeed).where(HistorySeed.chat_id == escenario["chat"].id))
        .scalars()
        .all()
    )
    assert len(guardadas) == 1
    assert escenario["colector"].metricas.duplicadas == 2
