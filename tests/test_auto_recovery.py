"""La recuperación ocurre sola, o espera y dice por qué.

QUE SE AUTOMATIZA
-----------------
Hasta ahora, sacar una conversación de ``waiting_seed`` requería saber que
existían dos botones y en qué orden pulsarlos. El motor no cambia: se llaman
las mismas piezas. Lo que cambia es que ya no hace falta saberlo.

LO QUE ESTAS PRUEBAS PROTEGEN
-----------------------------
Que la automatización no se vuelva imprudente. Un bucle que aplica referencias
sin comprobar nada haría exactamente el daño que costó dos fases entender:
promover veintidós conversaciones con el motor mudo las convierte en veintidós
esperas agotadas, y después no hay forma de saber si el ancla era mala o si el
teléfono estaba dormido.

Por eso lo importante aquí no es que dispare, sino **cuándo se abstiene**.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.auto_recovery import AutoRecovery


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


class _Cola:
    def __init__(self, *, pausada=False, puede_reanudar=True):
        self.pausada = pausada
        self._puede = puede_reanudar
        self.reanudadas = 0

    def reanudar(self):
        if not self._puede:
            return False
        self.reanudadas += 1
        self.pausada = False
        return True

    def estado(self):
        return {"pending": 0, "paused": self.pausada}


def _runtime(
    session,
    *,
    capacidad="CONFIRMED",
    web_habilitado=True,
    web_vivo=True,
    web_listo=True,
    conectado=True,
    bootstrap_visto=False,
    asentado=True,
    cola=None,
):
    from app.core.session_state import AppState

    supervisor = SimpleNamespace(
        habilitado=web_habilitado,
        vivo=web_vivo,
        snapshot=lambda: {"web_client_ready": web_listo, "state": "connected"},
        start=lambda: True,
    )
    # Una conexion principal COMPLETA: estado, identidad, Signal y cuenta. Las
    # cuatro, porque cada una sola se puede cumplir sin las otras y el
    # vigilante ahora las exige juntas.
    cliente = SimpleNamespace(
        _client=SimpleNamespace(
            device=SimpleNamespace(jid=SimpleNamespace(user="34600111222"))
        )
        if conectado
        else None,
        _loop=None,
    )
    return SimpleNamespace(
        database=_Database(session),
        web_companion=supervisor,
        backfill=SimpleNamespace(
            capability_state=lambda: capacidad,
            session_fingerprint=lambda: "huella",
            # La prueba de capacidad se repite sola con espera creciente. Aqui
            # nunca toca: lo que se mide es la aplicacion de referencias.
            toca_reintentar_canary=lambda: False,
            run_canary=None,
        ),
        client=cliente,
        state=SimpleNamespace(
            state=AppState.CONNECTED if conectado else AppState.NO_SESSION
        ),
        settings=SimpleNamespace(
            signal_store_file=SimpleNamespace(exists=lambda: True)
        ),
        gate=SimpleNamespace(
            bootstrap_seen=bootstrap_visto, settled=lambda: asentado
        ),
        seed_queue=cola if cola is not None else _Cola(),
        seed_collector=None,
        runtime_owner_user_id=None,
        runtime_owner_account_id=1,
    )


def _esperando(vigilante, cuantas):
    """Finge que hay N conversaciones sin ancla, sin tocar la base."""
    vigilante._esperando_ancla = lambda: cuantas  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Cuando NO se aplica
# ---------------------------------------------------------------------------


def test_sin_nadie_esperando_no_hay_nada_que_hacer(session):
    vigilante = AutoRecovery(_runtime(session))
    _esperando(vigilante, 0)
    assert vigilante._por_que_no() == "NOTHING_WAITING"


def test_sin_on_demand_confirmado_se_ESPERA(session):
    """La comprobación que costó dos fases entender.

    Promover conversaciones con el motor mudo las convierte en esperas
    agotadas, y después no se puede distinguir un ancla mala de un teléfono
    dormido.
    """
    vigilante = AutoRecovery(_runtime(session, capacidad="SUSPECT"))
    _esperando(vigilante, 22)
    assert vigilante._por_que_no() == "ON_DEMAND_NOT_CONFIRMED"


def test_una_capacidad_desconocida_tampoco_vale(session):
    vigilante = AutoRecovery(_runtime(session, capacidad="UNKNOWN"))
    _esperando(vigilante, 5)
    assert vigilante._por_que_no() == "ON_DEMAND_NOT_CONFIRMED"


def test_sin_conexion_principal_no_se_pide_nada(session):
    """Y el motivo dice cual de los dos codigos falta.

    Antes esto se llamaba SESSION_NOT_CONNECTED y solo miraba si habia objeto
    cliente. Con eso, un emparejamiento a medias -- objeto en pie, sin
    identidad -- pasaba el filtro y el vigilante seguia adelante.
    """
    vigilante = AutoRecovery(_runtime(session, conectado=False))
    _esperando(vigilante, 5)
    assert vigilante._por_que_no() == "PRIMARY_NOT_READY"


def test_sin_conexion_principal_la_fase_manda_al_codigo_QUE_TOCA(session):
    """La fase es ``pairing_primary``, no ``pairing_web``.

    Es el fallo medido entero: el usuario se quedo escaneando el codigo del
    segundo dispositivo mientras el que hacia falta era el principal.
    """
    vigilante = AutoRecovery(_runtime(session, conectado=False))
    _esperando(vigilante, 5)
    asyncio.run(vigilante._aplicar_si_procede())
    assert vigilante.estado.fase == "pairing_primary"


def test_una_identidad_a_medias_no_cuenta_como_conexion(session):
    """``device.json`` escrito sin JID utilizable NO es estar vinculado."""
    rt = _runtime(session)
    rt.client._client.device.jid = None
    rt.settings = SimpleNamespace(signal_store_file=None)
    vigilante = AutoRecovery(rt)
    _esperando(vigilante, 5)
    assert vigilante._por_que_no() == "PRIMARY_NOT_READY"


def test_mientras_llega_el_historial_inicial_no_se_compite(session):
    """El bootstrap y la vía Web piden al MISMO teléfono."""
    vigilante = AutoRecovery(
        _runtime(session, bootstrap_visto=True, asentado=False)
    )
    _esperando(vigilante, 5)
    assert vigilante._por_que_no() == "INITIAL_SYNC_RUNNING"


def test_una_sesion_ya_sincronizada_no_espera_un_bootstrap_que_no_llegara(session):
    """Sin bootstrap visto no hay con qué competir: se sigue.

    En una sesión ya sincronizada el ``INITIAL_BOOTSTRAP`` no vuelve a llegar
    nunca. Esperar a que se asiente sería esperar para siempre.
    """
    vigilante = AutoRecovery(
        _runtime(session, bootstrap_visto=False, asentado=False)
    )
    _esperando(vigilante, 5)
    assert vigilante._por_que_no() is None


def test_con_el_companion_apagado_la_app_sigue_funcionando(session):
    vigilante = AutoRecovery(_runtime(session, web_habilitado=False))
    _esperando(vigilante, 5)
    assert vigilante._por_que_no() == "WEB_COMPANION_DISABLED"


def test_si_el_companion_aun_no_esta_listo_se_vuelve_a_mirar(session):
    """No es un error: es una vía que ahora no está disponible."""
    vigilante = AutoRecovery(_runtime(session, web_listo=False))
    _esperando(vigilante, 5)
    assert vigilante._por_que_no() == "WEB_COMPANION_NOT_READY"


def test_cuando_todo_encaja_no_hay_motivo_para_esperar(session):
    vigilante = AutoRecovery(_runtime(session))
    _esperando(vigilante, 22)
    assert vigilante._por_que_no() is None


# ---------------------------------------------------------------------------
# Cuando SÍ se aplica
# ---------------------------------------------------------------------------


def _con_aplicador(monkeypatch, insertadas=22, promovidos=22):
    from app.web_companion import apply as modulo

    llamadas = []

    def falso(self, **kwargs):
        llamadas.append(1)
        return modulo.ResultadoDeAplicacion(
            candidatos=insertadas,
            validados=insertadas,
            insertadas=insertadas,
            promovidos=promovidos,
        )

    monkeypatch.setattr(modulo.WebSeedApplier, "aplicar", falso)
    return llamadas


def test_aplica_y_lo_cuenta(session, monkeypatch):
    llamadas = _con_aplicador(monkeypatch)
    vigilante = AutoRecovery(_runtime(session))
    _esperando(vigilante, 22)

    asyncio.run(vigilante._aplicar_si_procede())

    assert len(llamadas) == 1
    assert vigilante.estado.seeds_aplicadas == 22
    assert vigilante.estado.chats_promovidos == 22
    assert vigilante.estado.fase == "recovering_history"


def test_se_insiste_mientras_WhatsApp_Web_se_hidrata(session, monkeypatch):
    """El primer sondeo es el PEOR, no el definitivo.

    Medido en una sesion recien vinculada: a los 108 s habia 32 conversaciones
    visibles y solo 6 con mensajes materializados; en una vinculada el dia
    anterior, 22 de 25. WhatsApp Web va materializando los mensajes poco a
    poco.

    Antes se sondeaba una vez y, si el numero de conversaciones esperando no
    cambiaba, no se volvia a intentar nunca. Eso congelaba la peor foto.
    """
    llamadas = _con_aplicador(monkeypatch, insertadas=0, promovidos=0)
    vigilante = AutoRecovery(_runtime(session))
    _esperando(vigilante, 3)

    for _ in range(3):
        asyncio.run(vigilante._aplicar_si_procede())

    assert len(llamadas) == 3, "se sigue intentando mientras la ventana este abierta"


def test_pero_no_se_insiste_para_siempre(session, monkeypatch):
    """Varias rondas seguidas sin nada: se deja de gastar el navegador."""
    llamadas = _con_aplicador(monkeypatch, insertadas=0, promovidos=0)
    vigilante = AutoRecovery(_runtime(session))
    _esperando(vigilante, 3)

    for _ in range(AutoRecovery.RONDAS_SIN_MEJORA + 4):
        asyncio.run(vigilante._aplicar_si_procede())

    assert len(llamadas) == AutoRecovery.RONDAS_SIN_MEJORA


def test_una_ronda_que_aporta_algo_reinicia_la_cuenta(session, monkeypatch):
    """Si aparecen referencias, es que todavia se esta hidratando."""
    vigilante = AutoRecovery(_runtime(session))
    _esperando(vigilante, 10)

    _con_aplicador(monkeypatch, insertadas=0, promovidos=0)
    for _ in range(AutoRecovery.RONDAS_SIN_MEJORA - 1):
        asyncio.run(vigilante._aplicar_si_procede())
    assert vigilante._rondas_secas == AutoRecovery.RONDAS_SIN_MEJORA - 1

    llamadas = _con_aplicador(monkeypatch, insertadas=4, promovidos=4)
    asyncio.run(vigilante._aplicar_si_procede())
    assert vigilante._rondas_secas == 0

    # Y por eso puede seguir intentandolo.
    asyncio.run(vigilante._aplicar_si_procede())
    assert len(llamadas) == 2


def test_la_ventana_se_cuenta_desde_que_la_sesion_web_esta_lista(session):
    """No desde que arranca el proceso.

    Entre arrancar y tener la sesion Web puede pasar todo el rato que el
    usuario tarde en escanear el segundo codigo.
    """
    vigilante = AutoRecovery(_runtime(session, web_listo=False))
    _esperando(vigilante, 5)
    assert vigilante._web_lista_desde is None

    vigilante._por_que_no()  # todavia no esta lista
    assert vigilante._web_lista_desde is None

    listo = AutoRecovery(_runtime(session))
    _esperando(listo, 5)
    listo._por_que_no()
    assert listo._web_lista_desde is not None


def test_si_aparecen_conversaciones_nuevas_si_se_vuelve_a_intentar(session, monkeypatch):
    llamadas = _con_aplicador(monkeypatch, insertadas=0, promovidos=0)
    vigilante = AutoRecovery(_runtime(session))

    _esperando(vigilante, 3)
    asyncio.run(vigilante._aplicar_si_procede())
    _esperando(vigilante, 7)  # llego un chat nuevo
    asyncio.run(vigilante._aplicar_si_procede())

    assert len(llamadas) == 2


def test_no_hay_un_segundo_camino_de_aplicacion():
    """Se llama al MISMO aplicador que el botón manual.

    Dos caminos acabarían divergiendo, y uno de los dos escribiría anclas que
    el otro rechazaría.
    """
    import inspect

    from app.services import auto_recovery

    fuente = inspect.getsource(auto_recovery)
    assert "WebSeedApplier" in fuente
    # Ni recolector propio, ni validacion propia, ni cursor propio, ni una
    # sola escritura: el vigilante MIRA y delega. Lee `history_status` para
    # contar cuantas conversaciones esperan, y nada mas.
    for prohibido in (
        "RecentSeedCollector(",
        "HistorySeed(",
        "update(",
        "session.add(",
        "persist_cursor",
        "get_valid_history_cursor",
    ):
        assert prohibido not in fuente, f"el vigilante escribe por su cuenta: {prohibido}"


def test_un_rechazo_del_aplicador_no_tumba_el_vigilante(session, monkeypatch):
    from app.web_companion import apply as modulo

    def rechazar(self, **kwargs):
        raise modulo.AplicacionRechazada("ON_DEMAND_NOT_CONFIRMED", "no confirmado")

    monkeypatch.setattr(modulo.WebSeedApplier, "aplicar", rechazar)
    vigilante = AutoRecovery(_runtime(session))
    _esperando(vigilante, 5)

    asyncio.run(vigilante._aplicar_si_procede())  # no lanza

    assert vigilante.estado.motivo_espera == "ON_DEMAND_NOT_CONFIRMED"


def test_un_fallo_inesperado_tampoco(session, monkeypatch):
    from app.web_companion import apply as modulo

    def reventar(self, **kwargs):
        raise RuntimeError("el worker murio")

    monkeypatch.setattr(modulo.WebSeedApplier, "aplicar", reventar)
    vigilante = AutoRecovery(_runtime(session))
    _esperando(vigilante, 5)

    asyncio.run(vigilante._aplicar_si_procede())  # no lanza


# ---------------------------------------------------------------------------
# Reanudación automática
# ---------------------------------------------------------------------------


def test_una_tanda_pausada_se_reanuda_sola(session):
    """El teléfono se despierta cuando el usuario lo usa.

    Pedirle además que vuelva a la aplicación y pulse un botón es pedir dos
    cosas cuando basta con una.
    """
    cola = _Cola(pausada=True)
    vigilante = AutoRecovery(_runtime(session, cola=cola))

    vigilante._reanudar_si_se_puede()

    assert cola.reanudadas == 1
    assert cola.pausada is False


def test_si_todavia_no_se_puede_reanudar_no_se_insiste_en_falso(session):
    cola = _Cola(pausada=True, puede_reanudar=False)
    vigilante = AutoRecovery(_runtime(session, cola=cola))

    vigilante._reanudar_si_se_puede()

    assert cola.pausada is True


def test_una_cola_que_no_esta_pausada_no_se_toca(session):
    cola = _Cola(pausada=False)
    vigilante = AutoRecovery(_runtime(session, cola=cola))
    vigilante._reanudar_si_se_puede()
    assert cola.reanudadas == 0


def test_una_vuelta_hace_las_dos_cosas(session, monkeypatch):
    _con_aplicador(monkeypatch)
    cola = _Cola(pausada=True)
    vigilante = AutoRecovery(_runtime(session, cola=cola))
    _esperando(vigilante, 22)

    asyncio.run(vigilante._una_vuelta())

    assert cola.reanudadas == 1
    assert vigilante.estado.chats_promovidos == 22


# ---------------------------------------------------------------------------
# La fase que ve el usuario
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "motivo,fase",
    [
        ("NOTHING_WAITING", "complete"),
        ("SESSION_NOT_CONNECTED", "waiting_primary"),
        ("INITIAL_SYNC_RUNNING", "initial_sync"),
        ("WEB_COMPANION_NOT_RUNNING", "pairing_web"),
        ("WEB_COMPANION_NOT_READY", "waiting_web"),
        ("WEB_COMPANION_DISABLED", "partial"),
    ],
)
def test_cada_motivo_tiene_su_fase(motivo, fase):
    assert AutoRecovery._fase_por_motivo(motivo) == fase


def test_el_estado_se_puede_contar_sin_datos_tecnicos(session):
    vigilante = AutoRecovery(_runtime(session))
    salida = vigilante.estado.to_json()
    assert set(salida) == {
        "phase",
        "seeds_applied",
        "chats_promoted",
        # Conversaciones que existian en WhatsApp y aqui no: es el numero que
        # dice si el indice esta aportando algo.
        "chats_discovered",
        "waiting_reason",
        "attempts",
        "manual_interventions",
    }


# ---------------------------------------------------------------------------
# El segundo dispositivo es un FALLBACK, no un requisito
# ---------------------------------------------------------------------------


def test_sin_conversaciones_sin_ancla_no_se_arranca_el_segundo_dispositivo():
    """Pedir un segundo codigo cuando no hace falta es molestar por costumbre."""
    import inspect

    from app.core.orchestrator import Orchestrator

    fuente = inspect.getsource(Orchestrator._arrancar_recuperacion_automatica)
    assert "_cuantas_esperan_ancla" in fuente
    # Se comprueba ANTES de arrancarlo.
    assert fuente.index("sin_ancla") < fuente.index("supervisor.start()")


def test_si_no_se_puede_contar_se_ofrece_igual():
    """Mejor ofrecer de mas que dejar conversaciones sin recuperar en silencio."""
    from types import SimpleNamespace

    from app.core.orchestrator import Orchestrator

    orquestador = Orchestrator.__new__(Orchestrator)
    orquestador._database = SimpleNamespace(
        transaction=lambda: (_ for _ in ()).throw(RuntimeError("sin base"))
    )
    assert orquestador._cuantas_esperan_ancla() == 1
