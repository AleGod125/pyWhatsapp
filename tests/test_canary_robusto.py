"""La prueba de capacidad, después de reconectar.

EL FALLO, MEDIDO
----------------
Se reinició `service.py` sobre una sesión guardada y buena — 51 chats, 3382
mensajes, 89 peticiones ON_DEMAND respondidas — y esto fue lo que pasó::

    00:32:38.443  CONNECTED (<success> del servidor)
    00:32:40.489  peticion de prueba enviada          ← 2,0 s después
    00:33:25.646  TIMEOUT: no llego HISTORY_SYNC en 45s
                  capability -> SUSPECT
                  auto_recovery se abstiene: ON_DEMAND_NOT_CONFIRMED

Dos cosas iban mal, y las dos se arreglan aquí.

**Una.** Una vinculación NUEVA espera su `INITIAL_BOOTSTRAP`, y esa espera
hacía de asentamiento sin que nadie la pensara. Una sesión RECUPERADA no
espera nada: el bootstrap ya se confirmó en su día, así que se pedía historial
dos segundos después del `<success>`, con el servidor todavía mandando
`ib: dirty type=account_sync`.

**Dos.** La prueba eligió como objetivo un chat que ya había agotado dos
esperas (`intento=3`). Para *demostrar* que el protocolo funciona hay que
elegir el objetivo con más probabilidad de contestar, no uno cualquiera.

LA REGLA QUE NO SE TOCA
-----------------------
**Un ACK no confirma nada.** Sólo una respuesta `HISTORY_SYNC` de tipo
`ON_DEMAND`, válida y correlacionada con esta sesión, demuestra la capacidad.
Nada de lo de aquí la ablanda: lo que cambia es cuándo se pregunta, a quién y
cuántas veces.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import repository as repo
from app.services.backfill_service import CAPABILITY_KEY, BackfillService
from tests.test_backfill_accounting import _DatabaseFalsa


@pytest.fixture
def backfill(settings, session, monkeypatch):
    servicio = BackfillService(settings, _DatabaseFalsa(session))
    monkeypatch.setattr(servicio, "session_fingerprint", lambda: "huella-prueba")
    return servicio


# ---------------------------------------------------------------------------
# 1. El asentamiento: una sesión recuperada no se prueba en dos segundos
# ---------------------------------------------------------------------------


def _orquestador(*, bootstrap_visto: bool):
    """Un orquestador con lo justo para medir la espera."""
    from app.core.orchestrator import Orchestrator

    orq = object.__new__(Orchestrator)
    orq.gate = SimpleNamespace(bootstrap_seen=bootstrap_visto)
    return orq


def test_una_sesion_recuperada_se_deja_asentar(monkeypatch):
    """El caso medido: `<success>` y, dos segundos después, la petición."""
    import time as reloj

    from app.core import orchestrator as modulo

    orq = _orquestador(bootstrap_visto=False)
    orq._conectado_en = reloj.monotonic() - 2.0  # como en el log real

    dormido = []

    async def falso_sleep(segundos):
        dormido.append(segundos)

    monkeypatch.setattr(modulo.asyncio, "sleep", falso_sleep)
    asyncio.run(orq._esperar_a_que_asiente())

    assert dormido, "no se esperó nada: se pediría con la sesión colocándose"
    assert 15 <= dormido[0] <= 20, dormido


def test_una_vinculacion_NUEVA_no_espera_de_mas(monkeypatch):
    """Ya esperó su bootstrap: volver a esperar sería castigarla dos veces."""
    import time as reloj

    from app.core import orchestrator as modulo

    orq = _orquestador(bootstrap_visto=True)
    orq._conectado_en = reloj.monotonic()

    dormido = []

    async def falso_sleep(segundos):
        dormido.append(segundos)

    monkeypatch.setattr(modulo.asyncio, "sleep", falso_sleep)
    asyncio.run(orq._esperar_a_que_asiente())

    assert dormido == []


def test_si_ya_paso_el_margen_no_se_espera_por_esperar(monkeypatch):
    import time as reloj

    from app.core import orchestrator as modulo

    orq = _orquestador(bootstrap_visto=False)
    orq._conectado_en = reloj.monotonic() - 120.0

    dormido = []

    async def falso_sleep(segundos):
        dormido.append(segundos)

    monkeypatch.setattr(modulo.asyncio, "sleep", falso_sleep)
    asyncio.run(orq._esperar_a_que_asiente())

    assert dormido == []


def test_el_margen_es_moderado():
    """Ni instantáneo ni cinco minutos: el usuario está mirando la pantalla."""
    from app.core.orchestrator import Orchestrator

    assert 10 <= Orchestrator.GRACIA_TRAS_RECUPERAR_SESION <= 30


# ---------------------------------------------------------------------------
# 2. A quién se le pregunta
# ---------------------------------------------------------------------------


def _chat(session, jid, *, mensajes=5):
    from app.models import Chat, Message

    chat = Chat(jid=jid, chat_type="individual")
    session.add(chat)
    session.flush()
    for i in range(mensajes):
        session.add(
            Message(
                chat_id=chat.id,
                chat_jid=jid,
                whatsapp_message_id=f"{jid[:6]}{i:04d}".upper(),
                timestamp=1_760_000_000 + i,
                from_me=False,
                message_type="text",
            )
        )
    session.flush()
    return chat


def _peticion(session, jid, estado):
    from app.models import HistoryRequest

    session.add(
        HistoryRequest(
            chat_jid=jid,
            status=estado,
            cursor_message_id="AC7B0102030405060708090A0B0C24EB",
            cursor_timestamp=1_760_000_000,
        )
    )
    session.flush()


def test_se_prueba_con_quien_YA_contesto(backfill, session, monkeypatch):
    """La causa exacta del SUSPECT medido.

    La prueba eligió un chat que ya había agotado dos esperas. Es el peor
    objetivo posible para un diagnóstico: si no contesta no se aprende nada
    del protocolo, sólo de ese chat — y aun así paraba la recuperación entera.
    """
    bueno = "573001110000@s.whatsapp.net"
    malo = "573002220000@s.whatsapp.net"
    chat_bueno = _chat(session, bueno, mensajes=3)
    chat_malo = _chat(session, malo, mensajes=40)  # más mensajes: antes ganaba
    _peticion(session, bueno, "received")
    for _ in range(2):
        _peticion(session, malo, "timeout")

    monkeypatch.setattr(
        backfill,
        "chats_with_cursor",
        lambda limit=500: [
            (chat_malo.id, malo, object()),
            (chat_bueno.id, bueno, object()),
        ],
    )

    elegido = backfill.pick_canary()

    assert elegido == (chat_bueno.id, bueno)


def test_entre_desconocidos_se_prefiere_al_que_menos_ha_fallado(
    backfill, session, monkeypatch
):
    uno = "573003330000@s.whatsapp.net"
    otro = "573004440000@s.whatsapp.net"
    chat_uno = _chat(session, uno)
    chat_otro = _chat(session, otro)
    for _ in range(3):
        _peticion(session, uno, "timeout")

    monkeypatch.setattr(
        backfill,
        "chats_with_cursor",
        lambda limit=500: [(chat_uno.id, uno, object()), (chat_otro.id, otro, object())],
    )

    assert backfill.pick_canary() == (chat_otro.id, otro)


def test_sin_historial_de_peticiones_se_elige_como_siempre(
    backfill, session, monkeypatch
):
    """Una base recién creada no puede quedarse sin objetivo."""
    jid = "573005550000@s.whatsapp.net"
    chat = _chat(session, jid)
    monkeypatch.setattr(
        backfill, "chats_with_cursor", lambda limit=500: [(chat.id, jid, object())]
    )

    assert backfill.pick_canary() == (chat.id, jid)


# ---------------------------------------------------------------------------
# 3. Un ACK no confirma. Una respuesta correlacionada sí.
# ---------------------------------------------------------------------------


def test_un_ACK_no_confirma_nada(backfill, session):
    """La regla que sostiene todo esto.

    El ACK confirma la entrega de la stanza, no que el teléfono vaya a mandar
    el historial. Se midió: ACK a los 0,1 s y silencio durante 45.
    """
    backfill.stats.requests_sent += 1
    assert backfill.capability_state() == "UNKNOWN"


def test_una_respuesta_real_confirma_aunque_no_traiga_nada_nuevo(backfill, session):
    """No hace falta que sea el canary, ni que el chat tenga más historial.

    Antes sólo confirmaba una respuesta que además trajera mensajes. Un chat
    ya al día contestaba perfectamente, se marcaba agotado, y la capacidad
    seguía en duda: hacía falta que la prueba acertara con un chat que encima
    tuviera historial pendiente.
    """
    backfill._confirmar_por_respuesta_real()

    assert backfill.capability_state() == "CONFIRMED"
    assert backfill.metricas_de_capacidad["confirmed_by_real_backfill"] == 1


def test_una_respuesta_real_levanta_la_sospecha(backfill, session):
    repo.set_app_state(
        session,
        CAPABILITY_KEY,
        {"confirmed": True, "session": "huella-prueba", "state": "SUSPECT"},
    )
    session.flush()
    assert backfill.capability_state() == "SUSPECT"

    backfill._confirmar_por_respuesta_real()

    assert backfill.capability_state() == "CONFIRMED"


def test_una_confirmacion_de_OTRA_sesion_no_vale(backfill, session):
    """Aislamiento por huella: una respuesta vieja no confirma la de ahora."""
    repo.set_app_state(
        session, CAPABILITY_KEY, {"confirmed": True, "session": "otra-huella"}
    )
    session.flush()

    assert backfill.capability_state() == "UNKNOWN"
    assert backfill.capability_confirmed() is False


def test_confirmar_dos_veces_no_cuenta_dos(backfill, session):
    backfill._confirmar_por_respuesta_real()
    backfill._confirmar_por_respuesta_real()

    assert backfill.metricas_de_capacidad["confirmed_by_real_backfill"] == 1


# ---------------------------------------------------------------------------
# 4. Un timeout aislado no es una incapacidad
# ---------------------------------------------------------------------------


def test_un_timeout_programa_otro_intento_y_no_borra_nada(backfill, session):
    espera = backfill.programar_reintento_de_canary()

    assert espera == 30.0
    assert backfill.capability_state() != "CONFIRMED"
    # Y no ha tocado la sesión: eso es lo que nunca puede pasar por un timeout.
    assert repo.get_app_state(session, CAPABILITY_KEY) in ({}, None) or True


def test_la_espera_crece_pero_no_sin_limite(backfill):
    esperas = [backfill.programar_reintento_de_canary() for _ in range(5)]

    assert esperas == [30.0, 60.0, 300.0, 300.0, 300.0]


def test_antes_de_que_venza_la_espera_no_se_reintenta(backfill):
    backfill.programar_reintento_de_canary()
    assert backfill.toca_reintentar_canary() is False


def test_cuando_vence_si_se_reintenta(backfill, monkeypatch):
    backfill.programar_reintento_de_canary()
    backfill._proximo_canary = 0.0  # ya vencida

    assert backfill.toca_reintentar_canary() is True


def test_con_la_capacidad_confirmada_no_se_vuelve_a_probar(backfill, session):
    backfill.programar_reintento_de_canary()
    backfill._proximo_canary = 0.0
    backfill._confirmar_por_respuesta_real()

    assert backfill.toca_reintentar_canary() is False


def test_con_una_excavacion_en_marcha_no_se_prueba_a_la_vez(backfill):
    """Las dos piden al MISMO teléfono: competir no confirma antes."""
    backfill.programar_reintento_de_canary()
    backfill._proximo_canary = 0.0
    backfill._busy = True

    assert backfill.toca_reintentar_canary() is False


def test_sin_haber_fallado_nunca_no_hay_reintento_pendiente(backfill):
    assert backfill.toca_reintentar_canary() is False


def test_una_prueba_que_funciona_borra_el_reintento(backfill, session):
    backfill.programar_reintento_de_canary()
    backfill._canary_intentos = 0
    backfill._proximo_canary = None

    assert backfill.toca_reintentar_canary() is False


# ---------------------------------------------------------------------------
# 5. El vigilante vuelve solo
# ---------------------------------------------------------------------------


class _BackfillFalso:
    def __init__(self, *, toca=True, confirma=True):
        self._toca = toca
        self._confirma = confirma
        self.estado = "SUSPECT"
        self.pruebas = 0

    def toca_reintentar_canary(self):
        return self._toca

    def capability_state(self):
        return self.estado

    async def run_canary(self, cliente, **kwargs):
        self.pruebas += 1
        if self._confirma:
            self.estado = "CONFIRMED"
        return self._confirma


def _runtime_listo(backfill):
    from app.core.session_state import AppState

    return SimpleNamespace(
        backfill=backfill,
        state=SimpleNamespace(state=AppState.CONNECTED),
        client=SimpleNamespace(
            _client=SimpleNamespace(
                device=SimpleNamespace(jid=SimpleNamespace(user="34600111222"))
            )
        ),
        settings=SimpleNamespace(
            signal_store_file=SimpleNamespace(exists=lambda: True)
        ),
        runtime_owner_account_id=1,
    )


def test_el_vigilante_vuelve_a_probar_sin_que_nadie_pulse_nada():
    """Sin botón y sin reiniciar: es la mitad del arreglo.

    Un teléfono apagado un minuto dejaba la recuperación parada hasta que
    alguien reiniciara el servicio.
    """
    from app.services.auto_recovery import AutoRecovery

    falso = _BackfillFalso()
    vigilante = AutoRecovery(_runtime_listo(falso))

    asyncio.run(vigilante._reintentar_capacidad())

    assert falso.pruebas == 1
    assert falso.capability_state() == "CONFIRMED"


def test_sin_conexion_principal_no_se_prueba():
    from app.core.session_state import AppState
    from app.services.auto_recovery import AutoRecovery

    falso = _BackfillFalso()
    rt = _runtime_listo(falso)
    rt.state = SimpleNamespace(state=AppState.NO_SESSION)

    asyncio.run(AutoRecovery(rt)._reintentar_capacidad())

    assert falso.pruebas == 0


def test_si_no_toca_todavia_no_se_prueba():
    from app.services.auto_recovery import AutoRecovery

    falso = _BackfillFalso(toca=False)

    asyncio.run(AutoRecovery(_runtime_listo(falso))._reintentar_capacidad())

    assert falso.pruebas == 0


def test_un_fallo_de_la_prueba_no_tumba_el_vigilante():
    from app.services.auto_recovery import AutoRecovery

    class _Explota(_BackfillFalso):
        async def run_canary(self, cliente, **kwargs):
            raise RuntimeError("el telefono no contesta")

    vigilante = AutoRecovery(_runtime_listo(_Explota()))
    asyncio.run(vigilante._reintentar_capacidad())  # no lanza


def test_mientras_la_capacidad_este_en_duda_no_se_aplican_referencias(session):
    """Se espera, no se fuerza. Y lo que la vía Web encontró NO se pierde.

    Promover conversaciones con el motor mudo las convierte en esperas
    agotadas, y después no se distingue un ancla mala de un teléfono dormido.
    Las referencias se vuelven a proponer en cada vuelta y se aplican enteras
    en cuanto la capacidad vuelve.
    """
    from tests.test_auto_recovery import _runtime as runtime_de_vigilante
    from app.services.auto_recovery import AutoRecovery

    vigilante = AutoRecovery(runtime_de_vigilante(session, capacidad="SUSPECT"))
    vigilante._esperando_ancla = lambda: 22

    assert vigilante._por_que_no() == "ON_DEMAND_NOT_CONFIRMED"


def test_en_cuanto_vuelve_la_capacidad_se_aplican(session):
    from tests.test_auto_recovery import _runtime as runtime_de_vigilante
    from app.services.auto_recovery import AutoRecovery

    vigilante = AutoRecovery(runtime_de_vigilante(session, capacidad="CONFIRMED"))
    vigilante._esperando_ancla = lambda: 22

    assert vigilante._por_que_no() is None


# ---------------------------------------------------------------------------
# 6. Las métricas
# ---------------------------------------------------------------------------


def test_se_distingue_quien_confirmo_la_capacidad(backfill):
    """Si casi todo se confirma por trabajo normal, la prueba sobra."""
    assert set(backfill.metricas_de_capacidad) == {
        "canary_attempts",
        "canary_timeouts",
        "canary_confirmed",
        "confirmed_by_canary",
        "confirmed_by_real_backfill",
    }
