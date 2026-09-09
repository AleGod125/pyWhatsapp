"""El boton de excavar todo: que reabra lo cortado y nada mas.

EL FALLO QUE CUBRE, TAL Y COMO SE VIVIO
---------------------------------------
Una excavacion se corta --se pierde la conexion, el telefono se duerme, el
plazo vence antes de que llegue la respuesta-- y las conversaciones afectadas
quedan en ``timeout`` o ``error``. Nadie vuelve a por ellas: la cola de
excavacion mira las que estan ``pending``, y esas ya no lo estan. El usuario
ve una copia a medias y la unica salida que tenia era borrar la base de datos.

El boton existe para eso. Lo que se prueba aqui es la parte dificil de acertar:

* que reabra SOLO lo que describe un corte, no lo que describe un final;
* que NO se lleve por delante las conversaciones de otra cuenta;
* que una conversacion sin ancla vaya a ``waiting_seed`` y no a ``pending``,
  porque ``pending`` la mete en la cola para pedir sin con que;
* que no borre absolutamente nada.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from app.models import Chat, ChatHistoryState, Message
from app.services.sync_job import SyncJob


class _Db:
    """La sesion de la prueba con la forma que esperan los servicios."""

    def __init__(self, session):
        self._session = session

    def transaction(self):
        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


def _chat(session, cuenta, *, estado: str, con_ancla: bool = True) -> Chat:
    fila = Chat(
        jid=f"{uuid.uuid4().int % 10**14}@s.whatsapp.net",
        chat_type="individual",
        name="Marta",
        whatsapp_account_id=cuenta.id,
    )
    session.add(fila)
    session.flush()
    session.add(
        ChatHistoryState(
            chat_id=fila.id,
            chat_jid=fila.jid,
            history_status=estado,
            # Los dos topes, ya alcanzados: es como queda una conversacion
            # despues de varios cortes seguidos.
            attempt_count=5,
            consecutive_no_progress=3,
            last_error="lo que fuera",
        )
    )
    if con_ancla:
        # El ancla es un mensaje real con ID de WhatsApp: es lo unico desde lo
        # que se puede pedir historial.
        session.add(
            Message(
                chat_id=fila.id,
                chat_jid=fila.jid,
                whatsapp_message_id=f"WAMID{uuid.uuid4().hex[:16].upper()}",
                timestamp=1_700_000_000,
                from_me=False,
                message_type="text",
                text="hola",
                source="live",
            )
        )
    session.flush()
    return fila


def _trabajo(session) -> SyncJob:
    trabajo = SyncJob.__new__(SyncJob)
    trabajo._database = _Db(session)
    return trabajo


def _estado(session, chat: Chat) -> ChatHistoryState:
    session.expire_all()
    return session.execute(
        ChatHistoryState.__table__.select().where(
            ChatHistoryState.chat_id == chat.id
        )
    ).one()


# ---------------------------------------------------------------------------
# Que se reabre y que no
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("estado", ["timeout", "error", "server_limited"])
def test_lo_que_se_corto_vuelve_a_la_cola(session, cuenta, estado):
    """Los tres describen un corte, no un final."""
    chat = _chat(session, cuenta, estado=estado)

    assert _trabajo(session)._reabrir_atascados(cuenta.id) == 1

    fila = _estado(session, chat)
    assert fila.history_status == "pending"
    assert fila.next_retry_at is None
    assert fila.consecutive_no_progress == 0
    assert fila.last_error is None
    # Y el contador que la habria dejado muerta para siempre.
    assert fila.attempt_count == 0


def test_lo_que_el_telefono_dio_por_terminado_NO_se_reabre(session, cuenta):
    """`exhausted` es una respuesta, no un corte.

    WhatsApp contesto COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY. Volver a
    pedirlo gasta una peticion para recibir cero mensajes.
    """
    chat = _chat(session, cuenta, estado="exhausted")

    assert _trabajo(session)._reabrir_atascados(cuenta.id) == 0
    assert _estado(session, chat).history_status == "exhausted"


def test_lo_que_ya_estaba_pendiente_no_cuenta_como_reabierto(session, cuenta):
    chat = _chat(session, cuenta, estado="pending")

    assert _trabajo(session)._reabrir_atascados(cuenta.id) == 0
    assert _estado(session, chat).history_status == "pending"


def test_sin_ancla_se_va_a_waiting_seed_y_no_a_pending(session, cuenta):
    """Decir `pending` sin ancla la mete en la cola para pedir sin con que.

    Eso produce un ACK del servidor y despues silencio, que es exactamente el
    fallo que hacia creer que la conversacion se estaba excavando.
    """
    chat = _chat(session, cuenta, estado="timeout", con_ancla=False)

    # No cuenta como lista para pedir: no lo esta.
    assert _trabajo(session)._reabrir_atascados(cuenta.id) == 0
    assert _estado(session, chat).history_status == "waiting_seed"


# ---------------------------------------------------------------------------
# Multiusuario
# ---------------------------------------------------------------------------


def test_no_se_reabre_la_conversacion_de_OTRA_cuenta(session, cuenta):
    """La tabla de estados es comun; el boton no.

    Reabrir la conversacion de otra persona seria pedirle historial a un
    telefono que no es el suyo.
    """
    from app.models import User, WhatsAppAccount

    otro = User(email=f"o-{uuid.uuid4().hex[:8]}@x.com", password_hash="x")
    session.add(otro)
    session.flush()
    id_ajena = uuid.uuid4()
    ajena = WhatsAppAccount(
        id=id_ajena,
        user_id=otro.id,
        session_status="linked",
        session_storage_key=f"accounts/{id_ajena}",
    )
    session.add(ajena)
    session.flush()

    mio = _chat(session, cuenta, estado="timeout")
    suyo = _chat(session, ajena, estado="timeout")

    assert _trabajo(session)._reabrir_atascados(cuenta.id) == 1

    assert _estado(session, mio).history_status == "pending"
    assert _estado(session, suyo).history_status == "timeout"


def test_la_espera_de_reintento_tambien_se_acota_por_cuenta(session, cuenta):
    from datetime import datetime, timedelta, timezone

    from app.models import User, WhatsAppAccount

    otro = User(email=f"o-{uuid.uuid4().hex[:8]}@x.com", password_hash="x")
    session.add(otro)
    session.flush()
    id_ajena = uuid.uuid4()
    ajena = WhatsAppAccount(
        id=id_ajena,
        user_id=otro.id,
        session_status="linked",
        session_storage_key=f"accounts/{id_ajena}",
    )
    session.add(ajena)
    session.flush()

    luego = datetime.now(timezone.utc) + timedelta(hours=1)
    mio = _chat(session, cuenta, estado="pending")
    suyo = _chat(session, ajena, estado="pending")
    for chat in (mio, suyo):
        _estado(session, chat)  # materializa
    session.execute(
        ChatHistoryState.__table__.update()
        .where(ChatHistoryState.chat_id.in_([mio.id, suyo.id]))
        .values(next_retry_at=luego)
    )
    session.flush()

    assert _trabajo(session)._adelantar_reintentos(cuenta.id) == 1

    assert _estado(session, mio).next_retry_at is None
    assert _estado(session, suyo).next_retry_at is not None


# ---------------------------------------------------------------------------
# Lo que NO puede pasar
# ---------------------------------------------------------------------------


def test_reabrir_no_borra_ni_un_mensaje(session, cuenta):
    """El usuario pidio esto literalmente: sin borrar la base de datos."""
    chat = _chat(session, cuenta, estado="timeout")
    antes = session.execute(
        Message.__table__.select().where(Message.chat_jid == chat.jid)
    ).all()
    assert antes, "la prueba no vale si no habia mensajes que conservar"

    _trabajo(session)._reabrir_atascados(cuenta.id)

    despues = session.execute(
        Message.__table__.select().where(Message.chat_jid == chat.jid)
    ).all()
    assert len(despues) == len(antes)


def test_el_ancla_se_conserva_para_seguir_donde_se_quedo(session, cuenta):
    """Sin el ancla la excavacion empezaria de cero y repetiria lo que ya hay."""
    from app.history.cursor import get_valid_history_cursor

    chat = _chat(session, cuenta, estado="timeout")
    antes = get_valid_history_cursor(session, chat_id=chat.id)
    assert antes is not None

    _trabajo(session)._reabrir_atascados(cuenta.id)

    assert get_valid_history_cursor(session, chat_id=chat.id) is not None


def test_los_estados_reabribles_son_solo_esos_tres():
    """Una lista que crece sin querer reabre cosas que no debe."""
    assert SyncJob.ATASCADOS == ("timeout", "error", "server_limited")
    assert "exhausted" not in SyncJob.ATASCADOS
    # `fetching` lo reconcilia la fase 1, y lo hace mejor: distingue las que
    # conservan ancla de las que no. Un reseteo a ciegas aqui lo estropearia.
    assert "fetching" not in SyncJob.ATASCADOS


# ---------------------------------------------------------------------------
# Que el modo profundo lo llame de verdad
# ---------------------------------------------------------------------------


def test_el_modo_profundo_reabre_y_el_incremental_no():
    import inspect

    fuente = inspect.getsource(SyncJob.start)
    assert "_reabrir_atascados" in fuente
    # Dentro del `if profundo:`, no fuera: la busqueda rapida no reabre nada.
    posicion_if = fuente.index("if profundo:")
    assert fuente.index("_reabrir_atascados") > posicion_if


def test_el_boton_se_acota_a_la_cuenta_que_lo_pulsa():
    import inspect

    fuente = inspect.getsource(SyncJob.start)
    assert "runtime_owner_account_id" in fuente


def test_el_estado_publica_cuantas_se_reabrieron():
    """Sin el numero, el usuario no sabe si el boton hizo algo."""
    from app.services.sync_job import SyncState

    assert "chats_reopened" in SyncState().to_json()


# ---------------------------------------------------------------------------
# El aviso en vivo tiene que decir LO MISMO que la consulta
# ---------------------------------------------------------------------------


def test_el_aviso_por_SSE_lleva_el_estado_del_ciclo():
    """Sin esto el cartel de "espera" se apagaba solo a mitad de la excavacion.

    El frontend trata ``sync.status`` como el estado COMPLETO y lo sustituye
    entero. El aviso por SSE se construia solo con ``sync_to_json``, que no
    sabe nada del ciclo manual --ni ``state``, ni ``mode``, ni ``phase``-- asi
    que cada aviso borraba lo que la pantalla sabia: desaparecia el cartel y
    volvia a aparecer el boton con la excavacion todavia corriendo.

    Lo mas absurdo es que el ciclo SI publicaba su instantanea (``_emitir``);
    era el traductor el que la tiraba para reconstruir el evento sin ella.
    """
    import inspect

    from app.api import routes

    fuente = inspect.getsource(routes.eventos_para)
    assert "estado_de_sync(rt)" in fuente
    assert "sync_to_json(rt)" not in fuente, (
        "el aviso volveria a perder el estado del ciclo"
    )


def test_la_consulta_y_el_aviso_usan_LA_MISMA_funcion():
    """Dos formas de construir la respuesta eran dos verdades distintas."""
    import inspect

    from app.api import routes

    assert "estado_de_sync(rt)" in inspect.getsource(routes.sync_status)
    assert "estado_de_sync(rt)" in inspect.getsource(routes.events_stream)


def test_el_estado_de_sync_incluye_el_modo_y_la_fase():
    """`mode` es lo que distingue la busqueda rapida de la excavacion.

    Y es lo que hace que el boton siga tapado despues de un F5: si el modo
    viviera solo en el navegador, recargar la pagina lo devolveria.
    """
    from app.api.routes import estado_de_sync
    from app.services.sync_job import SyncJob, SyncState

    class _Trabajo:
        def snapshot(self):
            estado = SyncState(state="running", mode="full", phase="backfill")
            return estado.to_json()

    class _Rt:
        orchestrator = None
        sync_job = _Trabajo()

    cuerpo = estado_de_sync(_Rt())
    assert cuerpo["state"] == "running"
    assert cuerpo["mode"] == "full"
    assert cuerpo["phase"] == "backfill"
    # Y lo que ya traia sigue estando: no se sustituye, se completa.
    assert "history" in cuerpo
    assert SyncJob is not None


def test_reabrir_desbloquea_la_conversacion_ANTE_EL_MOTOR(session, cuenta):
    """El fallo mas caro: cinco cortes de red y la conversacion queda muerta.

    Volverla a `pending` no basta. El motor decide antes de mirar el estado, y
    con `attempt_count` en el tope (regla 6 de `app/history/decision.py`)
    responde PARAR aunque el telefono este delante contestando. Sin poner ese
    contador a cero, el boton reabre una conversacion que el motor sigue
    negandose a excavar, y el usuario ve el mismo resultado de antes.
    """
    from app.history.decision import EXCAVAR, PARAR, decidir, situacion_de

    chat = _chat(session, cuenta, estado="timeout")

    antes = decidir(situacion_de(_fila(session, chat), tiene_cursor=True, capacidad="CONFIRMED"))
    assert antes.accion == PARAR, "la prueba no vale si no estaba bloqueada"

    _trabajo(session)._reabrir_atascados(cuenta.id)

    despues = decidir(situacion_de(_fila(session, chat), tiene_cursor=True, capacidad="CONFIRMED"))
    assert despues.accion == EXCAVAR


def _fila(session, chat):
    from sqlalchemy import select

    session.expire_all()
    return session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_id == chat.id)
    ).scalar_one()
