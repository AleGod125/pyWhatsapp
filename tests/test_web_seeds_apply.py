"""Aplicar las referencias de WhatsApp Web: la fase que SI escribe.

QUE SE CONECTA AQUI
-------------------
El sondeo encontro 22 de 25 conversaciones sin ancla que WhatsApp Web si tiene
materializadas. Esta fase las convierte en anclas de verdad::

    candidatos Web -> validacion Python -> history_seeds -> cursor -> pending
                                                                 -> excavacion

LO QUE ESTAS PRUEBAS PROTEGEN
-----------------------------
Cuatro cosas, y las cuatro pueden hacer mucho dano si se rompen:

* que "Probar cobertura Web" siga sin escribir nada;
* que aplicar dos veces no duplique ni reinicie nada;
* que no se promueva ni una conversacion si ON_DEMAND no esta confirmado --
  promover 22 con el motor mudo produce 22 esperas agotadas y despues no hay
  forma de saber si el ancla era mala o si el telefono estaba dormido;
* que las peticiones salgan de una en una, y que si el telefono se duerme la
  tanda se pare sin deshacer lo que ya se consiguio.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import Chat, ChatHistoryState, HistorySeed, Message
from app.web_companion.apply import AplicacionRechazada, WebSeedApplier

ANCLA = "3A1F8BDD4678EB6DE395"


# ---------------------------------------------------------------------------
# Andamio
# ---------------------------------------------------------------------------


class _Database:
    def __init__(self, session):
        self._session = session

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


class _Supervisor:
    """Contesta lo que se le diga. No lanza ningun proceso."""

    def __init__(self, respuesta, *, listo=True):
        self.respuesta = respuesta
        self.habilitado = True
        self.vivo = True
        self._listo = listo
        self.enviados: list[dict] = []

    def enviar(self, comando, *, timeout=None):
        self.enviados.append(comando)
        return self.respuesta

    def snapshot(self):
        return {"state": "connected", "web_client_ready": self._listo}


class _Backfill:
    def __init__(self, capacidad="CONFIRMED"):
        self._capacidad = capacidad
        self._client = object()
        self._timeouts_seguidos = 0
        self._last_transport_lost = False

    def capability_state(self):
        return self._capacidad


class _Cola:
    def __init__(self):
        self.encolados: list[str] = []
        self.veces = 0

    def enqueue(self, jids):
        self.veces += 1
        nuevos = [j for j in jids if j not in self.encolados]
        self.encolados.extend(nuevos)
        return nuevos

    def estado(self):
        return {"pending": len(self.encolados), "paused": False}


def _runtime(session, respuesta, *, capacidad="CONFIRMED", listo=True, cuenta=None, usuario=None):
    return SimpleNamespace(
        database=_Database(session),
        web_companion=_Supervisor(respuesta, listo=listo),
        backfill=_Backfill(capacidad),
        seed_queue=_Cola(),
        seed_collector=None,
        runtime_owner_user_id=usuario,
        runtime_owner_account_id=cuenta,
        bus=None,
    )


@pytest.fixture
def cuenta(session):
    """Un dueno real: toda ancla pertenece a alguien."""
    from app.models import User, WhatsAppAccount

    usuario = User(email=f"a{uuid.uuid4().hex[:8]}@x.test", password_hash="x")
    session.add(usuario)
    session.flush()
    wa = WhatsAppAccount(user_id=usuario.id, session_storage_key=uuid.uuid4().hex)
    session.add(wa)
    session.flush()
    return usuario.id, wa.id


def _esperando(session, cuenta_id, *, cuantas=1, prefijo="5730"):
    """Conversaciones sin ancla: el caso que motiva todo esto."""
    chats = []
    for _ in range(cuantas):
        jid = f"{prefijo}{uuid.uuid4().hex[:10]}@s.whatsapp.net"
        chat = Chat(jid=jid, chat_type="individual", whatsapp_account_id=cuenta_id)
        session.add(chat)
        session.flush()
        session.add(
            ChatHistoryState(
                chat_id=chat.id, chat_jid=jid, history_status="waiting_seed"
            )
        )
        session.flush()
        chats.append(chat)
    return chats


def _respuesta(chats, *, sin_ancla=(), **cambios):
    """Lo que devolveria el worker: unos con candidato y otros sin el."""
    filas = []
    for indice, chat in enumerate(chats):
        if chat.jid in sin_ancla:
            filas.append({"chat_jid": chat.jid, "visible": True, "candidate": None})
            continue
        candidato = {
            "chat_jid": chat.jid,
            "wa_msg_id": f"{ANCLA[:-2]}{indice:02X}",
            "timestamp": 1_760_000_000 + indice,
            "from_me": False,
            "source": "web_store",
        }
        candidato.update(cambios)
        filas.append({"chat_jid": chat.jid, "visible": True, "candidate": candidato})
    return {
        "event": "seed_probe_result",
        "summary": {
            "waiting": len(chats),
            "visible_store": len(chats),
            "with_messages": len(chats) - len(sin_ancla),
        },
        "chats": filas,
    }


def _estados(session, chats):
    """Los estados SOLO de las conversaciones de esta prueba.

    La sesion de pruebas comparte la base con lo que ya hay dentro (se
    deshace al terminar, pero se ve mientras corre), asi que contar la tabla
    entera mezclaria las 25 conversaciones reales con las de aqui.
    """
    jids = [c.jid for c in chats]
    return dict(
        session.execute(
            select(ChatHistoryState.history_status, func.count())
            .where(ChatHistoryState.chat_jid.in_(jids))
            .group_by(ChatHistoryState.history_status)
        ).all()
    )


# ---------------------------------------------------------------------------
# El caso real: 25 esperando, 22 con referencia
# ---------------------------------------------------------------------------


def test_aplica_las_22_y_deja_esperando_las_3(session, cuenta):
    """La forma exacta de lo medido: 25 esperando, 22 con mensajes en Web."""
    usuario_id, cuenta_id = cuenta
    chats = _esperando(session, cuenta_id, cuantas=25)
    sin_ancla = {c.jid for c in chats[-3:]}
    rt = _runtime(
        session, _respuesta(chats, sin_ancla=sin_ancla), cuenta=cuenta_id, usuario=usuario_id
    )

    salida = WebSeedApplier(rt).aplicar().to_json()

    assert salida["candidates"] == 22
    assert salida["validated"] == 22
    assert salida["inserted"] == 22
    assert salida["promoted_to_pending"] == 22
    assert salida["rejected"] == 0
    assert salida["still_waiting_without_seed"] == 3
    assert salida["on_demand_capability"] == "CONFIRMED"

    session.expire_all()
    assert _estados(session, chats) == {"pending": 22, "waiting_seed": 3}


def test_los_3_sin_referencia_no_se_tocan(session, cuenta):
    """No se fabrica un ancla para llegar a 25. Se quedan esperando."""
    usuario_id, cuenta_id = cuenta
    chats = _esperando(session, cuenta_id, cuantas=5)
    sin_ancla = {c.jid for c in chats[-3:]}
    rt = _runtime(
        session, _respuesta(chats, sin_ancla=sin_ancla), cuenta=cuenta_id, usuario=usuario_id
    )
    WebSeedApplier(rt).aplicar()

    session.expire_all()
    for chat in chats[-3:]:
        estado = session.execute(
            select(ChatHistoryState).where(ChatHistoryState.chat_jid == chat.jid)
        ).scalar_one()
        assert estado.history_status == "waiting_seed"
        assert estado.oldest_message_id is None
    anclas = session.execute(
        select(func.count())
        .select_from(HistorySeed)
        .where(HistorySeed.chat_jid.in_(sin_ancla))
    ).scalar()
    assert anclas == 0


def test_solo_se_encolan_los_promovidos(session, cuenta):
    usuario_id, cuenta_id = cuenta
    chats = _esperando(session, cuenta_id, cuantas=5)
    sin_ancla = {chats[0].jid}
    rt = _runtime(
        session, _respuesta(chats, sin_ancla=sin_ancla), cuenta=cuenta_id, usuario=usuario_id
    )
    salida = WebSeedApplier(rt).aplicar()
    assert salida.promovidos == 4
    assert len(rt.seed_queue.encolados) == 4
    assert chats[0].jid not in rt.seed_queue.encolados


def test_el_ancla_queda_con_su_procedencia(session, cuenta):
    """Trazabilidad: de donde salio cada ancla se puede saber despues."""
    usuario_id, cuenta_id = cuenta
    chats = _esperando(session, cuenta_id)
    rt = _runtime(session, _respuesta(chats), cuenta=cuenta_id, usuario=usuario_id)
    WebSeedApplier(rt).aplicar()

    ancla = session.execute(
        select(HistorySeed).where(HistorySeed.chat_jid == chats[0].jid)
    ).scalar_one()
    assert ancla.source == "web_store"
    assert ancla.from_me is False
    assert ancla.timestamp == 1_760_000_000
    assert ancla.whatsapp_account_id == cuenta_id


def test_el_cursor_lo_elige_la_funcion_canonica(session, cuenta):
    """No hay una segunda implementacion de cursor.

    Si la eleccion de aqui y la del motor pudieran discrepar, el chat pasaria
    a ``pending`` con un ancla y se pediria con otra.
    """
    from app.history.cursor import get_valid_history_cursor

    usuario_id, cuenta_id = cuenta
    chats = _esperando(session, cuenta_id)
    rt = _runtime(session, _respuesta(chats), cuenta=cuenta_id, usuario=usuario_id)
    WebSeedApplier(rt).aplicar()

    session.expire_all()
    estado = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == chats[0].jid)
    ).scalar_one()
    canonico = get_valid_history_cursor(
        session, chat_id=chats[0].id, chat_jid=chats[0].jid
    )
    assert canonico is not None
    assert estado.oldest_message_id == canonico.message_id
    assert estado.oldest_message_timestamp == canonico.timestamp


# ---------------------------------------------------------------------------
# Idempotencia
# ---------------------------------------------------------------------------


def test_aplicar_dos_veces_no_duplica_nada(session, cuenta):
    usuario_id, cuenta_id = cuenta
    chats = _esperando(session, cuenta_id, cuantas=3)
    rt = _runtime(session, _respuesta(chats), cuenta=cuenta_id, usuario=usuario_id)

    primera = WebSeedApplier(rt).aplicar().to_json()
    assert primera["inserted"] == 3
    assert primera["promoted_to_pending"] == 3

    # La segunda vez ya no hay nadie esperando: el sondeo devuelve vacio,
    # igual que en la vida real.
    anclas_antes = session.execute(select(func.count()).select_from(HistorySeed)).scalar()
    segunda = WebSeedApplier(rt).aplicar().to_json()
    anclas_despues = session.execute(select(func.count()).select_from(HistorySeed)).scalar()

    assert anclas_antes == anclas_despues
    assert segunda["inserted"] == 0


def test_reaplicar_el_mismo_candidato_no_reinicia_el_historial(session, cuenta):
    """Aunque el chat siga esperando, la misma ancla no se escribe dos veces."""
    usuario_id, cuenta_id = cuenta
    chats = _esperando(session, cuenta_id)
    rt = _runtime(session, _respuesta(chats), cuenta=cuenta_id, usuario=usuario_id)
    WebSeedApplier(rt).aplicar()

    # Se devuelve a mano a 'waiting_seed' para forzar el segundo intento.
    session.expire_all()
    estado = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == chats[0].jid)
    ).scalar_one()
    estado.history_status = "waiting_seed"
    estado.attempt_count = 2
    session.flush()

    segunda = WebSeedApplier(rt).aplicar().to_json()
    assert segunda["already_present"] == 1
    assert segunda["inserted"] == 0
    anclas = session.execute(
        select(func.count())
        .select_from(HistorySeed)
        .where(HistorySeed.chat_jid == chats[0].jid)
    ).scalar()
    assert anclas == 1


def test_la_cola_no_recibe_el_mismo_chat_dos_veces(session, cuenta):
    usuario_id, cuenta_id = cuenta
    chats = _esperando(session, cuenta_id, cuantas=2)
    rt = _runtime(session, _respuesta(chats), cuenta=cuenta_id, usuario=usuario_id)
    WebSeedApplier(rt).aplicar()
    WebSeedApplier(rt).aplicar()
    assert len(rt.seed_queue.encolados) == len(set(rt.seed_queue.encolados))


# ---------------------------------------------------------------------------
# Validacion: Node propone, Python decide
# ---------------------------------------------------------------------------


def test_un_candidato_invalido_no_arrastra_a_los_demas(session, cuenta):
    usuario_id, cuenta_id = cuenta
    chats = _esperando(session, cuenta_id, cuantas=3)
    respuesta = _respuesta(chats)
    # Uno con identificador inventado.
    respuesta["chats"][1]["candidate"]["wa_msg_id"] = "opaque-1234"
    rt = _runtime(session, respuesta, cuenta=cuenta_id, usuario=usuario_id)

    salida = WebSeedApplier(rt).aplicar().to_json()
    assert salida["validated"] == 2
    assert salida["promoted_to_pending"] == 2
    session.expire_all()
    assert _estados(session, chats)["waiting_seed"] == 1


def test_una_marca_en_milisegundos_se_rechaza(session, cuenta):
    """Adivinar la unidad produce un ancla que el servidor nunca responde."""
    usuario_id, cuenta_id = cuenta
    chats = _esperando(session, cuenta_id)
    rt = _runtime(
        session,
        _respuesta(chats, timestamp=1_760_000_000_000),
        cuenta=cuenta_id,
        usuario=usuario_id,
    )
    salida = WebSeedApplier(rt).aplicar().to_json()
    assert salida["validated"] == 0
    session.expire_all()
    assert _estados(session, chats) == {"waiting_seed": 1}


def test_un_identificador_local_no_sirve_de_ancla(session, cuenta):
    usuario_id, cuenta_id = cuenta
    chats = _esperando(session, cuenta_id)
    rt = _runtime(
        session, _respuesta(chats, wa_msg_id="temp-abc"), cuenta=cuenta_id, usuario=usuario_id
    )
    assert WebSeedApplier(rt).aplicar().validados == 0


def test_from_me_true_tambien_vale(session, cuenta):
    usuario_id, cuenta_id = cuenta
    chats = _esperando(session, cuenta_id)
    rt = _runtime(session, _respuesta(chats, from_me=True), cuenta=cuenta_id, usuario=usuario_id)
    WebSeedApplier(rt).aplicar()
    ancla = session.execute(
        select(HistorySeed).where(HistorySeed.chat_jid == chats[0].jid)
    ).scalar_one()
    assert ancla.from_me is True


def test_un_grupo_ancla_en_el_grupo(session, cuenta):
    usuario_id, cuenta_id = cuenta
    jid = f"1203630{uuid.uuid4().hex[:10]}@g.us"
    chat = Chat(jid=jid, chat_type="group", whatsapp_account_id=cuenta_id)
    session.add(chat)
    session.flush()
    session.add(
        ChatHistoryState(chat_id=chat.id, chat_jid=jid, history_status="waiting_seed")
    )
    session.flush()

    rt = _runtime(session, _respuesta([chat]), cuenta=cuenta_id, usuario=usuario_id)
    assert WebSeedApplier(rt).aplicar().promovidos == 1
    ancla = session.execute(
        select(HistorySeed).where(HistorySeed.chat_jid == jid)
    ).scalar_one()
    assert ancla.chat_jid.endswith("@g.us")


def test_un_lid_se_resuelve_al_chat_canonico(session, cuenta):
    """Web puede dar el contacto por LID donde Python usa el telefono."""
    from app.models import Contact

    usuario_id, cuenta_id = cuenta
    chats = _esperando(session, cuenta_id)
    lid = f"649401{uuid.uuid4().hex[:8]}@lid"
    session.add(Contact(jid=chats[0].jid, lid=lid))
    session.flush()

    respuesta = _respuesta(chats)
    respuesta["chats"][0]["candidate"]["chat_jid"] = lid
    rt = _runtime(session, respuesta, cuenta=cuenta_id, usuario=usuario_id)

    salida = WebSeedApplier(rt).aplicar()
    assert salida.validados == 1, "el alias tiene que resolver al chat de Python"
    session.expire_all()
    assert _estados(session, chats) == {"pending": 1}


# ---------------------------------------------------------------------------
# Condiciones previas
# ---------------------------------------------------------------------------


def test_sin_on_demand_confirmado_no_se_escribe_NADA(session, cuenta):
    """Promover 22 con el motor mudo produce 22 esperas agotadas."""
    usuario_id, cuenta_id = cuenta
    chats = _esperando(session, cuenta_id, cuantas=3)
    rt = _runtime(
        session, _respuesta(chats), capacidad="SUSPECT", cuenta=cuenta_id, usuario=usuario_id
    )

    antes = session.execute(select(func.count()).select_from(HistorySeed)).scalar()
    with pytest.raises(AplicacionRechazada) as fallo:
        WebSeedApplier(rt).aplicar()

    assert fallo.value.code == "ON_DEMAND_NOT_CONFIRMED"
    assert session.execute(select(func.count()).select_from(HistorySeed)).scalar() == antes
    session.expire_all()
    assert _estados(session, chats) == {"waiting_seed": 3}
    assert rt.web_companion.enviados == [], "ni siquiera se pregunta al worker"


def test_con_on_demand_confirmado_si_promueve(session, cuenta):
    usuario_id, cuenta_id = cuenta
    chats = _esperando(session, cuenta_id, cuantas=3)
    rt = _runtime(session, _respuesta(chats), cuenta=cuenta_id, usuario=usuario_id)
    assert WebSeedApplier(rt).aplicar().promovidos == 3


def test_si_el_companion_no_esta_listo_no_se_aplica(session, cuenta):
    usuario_id, cuenta_id = cuenta
    chats = _esperando(session, cuenta_id)
    rt = _runtime(session, _respuesta(chats), listo=False, cuenta=cuenta_id, usuario=usuario_id)
    with pytest.raises(AplicacionRechazada) as fallo:
        WebSeedApplier(rt).aplicar()
    assert fallo.value.code == "WEB_COMPANION_NOT_READY"


def test_se_vuelve_a_medir_justo_antes_de_escribir(session, cuenta):
    """No se aplica una foto de hace un rato.

    Entre medir y escribir el usuario recibe mensajes, un chat puede despertar
    solo y otro dejar de estar visible.
    """
    usuario_id, cuenta_id = cuenta
    chats = _esperando(session, cuenta_id)
    rt = _runtime(session, _respuesta(chats), cuenta=cuenta_id, usuario=usuario_id)
    WebSeedApplier(rt).aplicar()
    assert [c["cmd"] for c in rt.web_companion.enviados] == ["probe_waiting_seeds"]


# ---------------------------------------------------------------------------
# El sondeo sigue sin escribir
# ---------------------------------------------------------------------------


def test_probar_cobertura_sigue_siendo_solo_lectura(session, cuenta):
    """La medicion no puede acabar mutando la base por pulsar otro boton."""
    from app.web_companion.probe import WebCompanionProbe

    usuario_id, cuenta_id = cuenta
    chats = _esperando(session, cuenta_id, cuantas=3)
    rt = _runtime(session, _respuesta(chats), cuenta=cuenta_id, usuario=usuario_id)

    def contar():
        return (
            session.execute(select(func.count()).select_from(HistorySeed)).scalar(),
            session.execute(select(func.count()).select_from(Message)).scalar(),
            _estados(session, chats),
        )

    antes = contar()
    medido = WebCompanionProbe(rt.database, rt.web_companion).sondear(cuenta_id)
    session.expire_all()

    assert medido["seed_usable"] == 3
    assert medido["read_only"] is True
    assert medido["mutations"] == 0
    assert contar() == antes


def test_el_sondeo_y_la_aplicacion_son_dos_rutas_distintas():
    """Que sean dos evita que una medicion escriba sin querer."""
    import inspect

    from app.api import web_companion_routes as rutas

    fuente = inspect.getsource(rutas)
    assert '@web_companion.post("/web-companion/probe")' in fuente
    assert '@web_companion.post("/web-companion/seeds/apply")' in fuente
    # El sondeo no llama al aplicador.
    sondeo = fuente.split('def sondear', 1)[1].split("@web_companion", 1)[0]
    assert "Applier" not in sondeo
    assert "aplicar" not in sondeo


# ---------------------------------------------------------------------------
# La cola: de una en una, y pausa si el telefono se duerme
# ---------------------------------------------------------------------------


class _BackfillDeCola:
    """Cuenta cuantas excavaciones hay vivas a la vez."""

    def __init__(self, *, timeouts=(), transporte_perdido_en=None):
        self._client = object()
        self._timeouts_seguidos = 0
        self._last_transport_lost = False
        self.vivas = 0
        self.maximo = 0
        self.procesados: list[str] = []
        self._timeouts = list(timeouts)
        self._transporte_en = transporte_perdido_en
        self.capacidad = "CONFIRMED"

    def capability_state(self):
        return self.capacidad

    async def _process_chat(self, chat_id, chat_jid, max_rounds):
        self.vivas += 1
        self.maximo = max(self.maximo, self.vivas)
        await asyncio.sleep(0)
        self.procesados.append(chat_jid)
        turno = len(self.procesados)
        if turno in self._timeouts:
            self._timeouts_seguidos += 1
        else:
            self._timeouts_seguidos = 0
        if self._transporte_en == turno:
            self._last_transport_lost = True
        self.vivas -= 1


def _cola_con(session, backfill, chats):
    from app.services.seed_queue import SeedBackfillQueue

    cola = SeedBackfillQueue(_Database(session), backfill, lambda: None)
    cola.enqueue([c.jid for c in chats])
    return cola


def _vaciar(cola):
    import app.services.seed_queue as modulo

    original = modulo.DEBOUNCE_SECONDS
    modulo.DEBOUNCE_SECONDS = 0
    try:
        asyncio.run(cola._vaciar())
    finally:
        modulo.DEBOUNCE_SECONDS = original


def _con_ancla(session, cuenta_id, cuantas):
    """Conversaciones ya promovidas, listas para excavar."""
    chats = []
    for indice in range(cuantas):
        jid = f"5731{uuid.uuid4().hex[:10]}@s.whatsapp.net"
        chat = Chat(jid=jid, chat_type="individual", whatsapp_account_id=cuenta_id)
        session.add(chat)
        session.flush()
        session.add(
            ChatHistoryState(
                chat_id=chat.id,
                chat_jid=jid,
                history_status="pending",
                oldest_message_id=f"{ANCLA[:-2]}{indice:02X}",
                oldest_message_timestamp=1_760_000_000 + indice,
            )
        )
        session.flush()
        chats.append(chat)
    return chats


def test_nunca_hay_dos_peticiones_a_la_vez(session, cuenta):
    """22 chats no son 22 peticiones en paralelo."""
    _, cuenta_id = cuenta
    chats = _con_ancla(session, cuenta_id, 22)
    backfill = _BackfillDeCola()
    cola = _cola_con(session, backfill, chats)

    _vaciar(cola)

    assert len(backfill.procesados) == 22
    assert backfill.maximo == 1, "solo puede excavarse un chat cada vez"


def test_dos_timeouts_seguidos_paran_la_tanda(session, cuenta):
    """El telefono dormido no puede quemar los 22 chats de la tanda."""
    _, cuenta_id = cuenta
    chats = _con_ancla(session, cuenta_id, 10)
    backfill = _BackfillDeCola(timeouts=(1, 2))
    cola = _cola_con(session, backfill, chats)

    _vaciar(cola)

    assert cola.pausada is True
    assert cola.motivo_pausa == "telefono"
    assert len(backfill.procesados) == 2, "no se intentan los ocho restantes"
    assert cola.estado()["pending"] == 8, "los que quedan siguen en la cola"


def test_un_timeout_suelto_no_para_nada(session, cuenta):
    """Puede ser de ESE chat. Para eso esta la espera de reintento."""
    _, cuenta_id = cuenta
    chats = _con_ancla(session, cuenta_id, 5)
    backfill = _BackfillDeCola(timeouts=(1,))
    cola = _cola_con(session, backfill, chats)

    _vaciar(cola)

    assert cola.pausada is False
    assert len(backfill.procesados) == 5


def test_si_se_cae_el_transporte_se_para_enseguida(session, cuenta):
    """El telefono no tuvo ocasion de contestar: no es culpa del ancla."""
    _, cuenta_id = cuenta
    chats = _con_ancla(session, cuenta_id, 6)
    backfill = _BackfillDeCola(transporte_perdido_en=1)
    cola = _cola_con(session, backfill, chats)

    _vaciar(cola)

    assert cola.pausada is True
    assert cola.motivo_pausa == "transporte"
    assert len(backfill.procesados) == 1


def test_lo_ya_recuperado_no_se_deshace(session, cuenta):
    """Cinco completos y se cae la linea: los cinco se quedan."""
    _, cuenta_id = cuenta
    chats = _con_ancla(session, cuenta_id, 12)
    backfill = _BackfillDeCola(timeouts=(6, 7))
    cola = _cola_con(session, backfill, chats)

    _vaciar(cola)

    assert cola.pausada is True
    assert len(backfill.procesados) == 7
    assert cola.estado()["pending"] == 5
    session.expire_all()
    # Ni un estado revertido: no hay rollback de lo conseguido.
    assert _estados(session, chats) == {"pending": 12}


def test_reanudar_exige_sesion_y_capacidad(session, cuenta):
    _, cuenta_id = cuenta
    chats = _con_ancla(session, cuenta_id, 4)
    backfill = _BackfillDeCola(timeouts=(1, 2))
    cola = _cola_con(session, backfill, chats)
    _vaciar(cola)
    assert cola.pausada is True

    backfill.capacidad = "SUSPECT"
    assert cola.reanudar() is False, "no se reanuda para repetir el mismo fallo"
    assert cola.pausada is True

    backfill._client = None
    backfill.capacidad = "CONFIRMED"
    assert cola.reanudar() is False, "sin sesion no hay a quien pedir"

    backfill._client = object()
    assert cola.reanudar() is True
    assert cola.pausada is False


def test_al_reanudar_continua_con_los_que_quedaban(session, cuenta):
    _, cuenta_id = cuenta
    chats = _con_ancla(session, cuenta_id, 6)
    backfill = _BackfillDeCola(timeouts=(1, 2))
    cola = _cola_con(session, backfill, chats)
    _vaciar(cola)
    assert len(backfill.procesados) == 2

    backfill._timeouts_seguidos = 0
    cola.reanudar()
    _vaciar(cola)

    assert len(backfill.procesados) == 6
    assert cola.estado()["pending"] == 0


def test_una_cola_pausada_no_arranca_sola(session, cuenta):
    _, cuenta_id = cuenta
    chats = _con_ancla(session, cuenta_id, 3)
    backfill = _BackfillDeCola(timeouts=(1, 2))
    cola = _cola_con(session, backfill, chats)
    _vaciar(cola)

    procesados = len(backfill.procesados)
    cola.enqueue([chats[0].jid])  # llega trabajo nuevo mientras esta parada
    _vaciar(cola)
    assert len(backfill.procesados) == procesados


def test_la_pausa_se_puede_contar(session, cuenta):
    _, cuenta_id = cuenta
    chats = _con_ancla(session, cuenta_id, 5)
    backfill = _BackfillDeCola(timeouts=(1, 2))
    cola = _cola_con(session, backfill, chats)
    avisos: list[tuple] = []
    cola.publish = lambda nombre, datos: avisos.append((nombre, datos))

    _vaciar(cola)

    assert cola.estado()["waiting_for_phone"] is True
    assert avisos and avisos[0][0] == "history.waiting_for_phone"
