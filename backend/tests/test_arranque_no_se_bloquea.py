"""Arrancar dos cuentas no lanza dos reconciliaciones de la base entera.

LO QUE PASO
-----------
La API dejo de arrancar. No fallaba: se quedaba parada, sin llegar nunca a
"API escuchando". Volcando las pilas del proceso colgado, los dos hilos
estaban en la MISMA linea::

    hilo del runtime base   service.py -> runtime.start -> _wire_services
                            -> orchestrator.prepare -> run_maintenance
                            -> maintenance_service.reconcile_stuck_fetching
                            -> psycopg wait_select        <- esperando

    hilo principal          levantar_las_demas -> get_or_start -> _crear_runtime
                            -> runtime.start -> ... -> reconcile_stuck_fetching
                            -> psycopg wait_select        <- esperando

`MaintenanceService.run_all` reconcilia la base ENTERA -- no esta acotado por
cuenta. Pero lo llamaba `prepare()`, y `prepare()` lo llama CADA
`AppRuntime.start()`, o sea uno por cuenta vinculada. Con dos cuentas, dos
hilos ejecutando el mismo ``UPDATE ... WHERE chat_jid IN (...)`` sobre las
mismas filas, cada uno esperando a que el otro suelte.

Con una cuenta no se veia. Aparecio al vincular la segunda.

LO QUE SE COMPRUEBA
-------------------
1. arrancar N runtimes reconcilia UNA vez, no N;
2. y aunque se fuerce, dos reconciliaciones no se solapan nunca.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.core.orchestrator import Orchestrator, reiniciar_mantenimiento


class _MantenimientoEspia:
    """Cuenta llamadas y delata solapamientos.

    Tarda un poco a proposito: sin esa espera, dos hilos "concurrentes"
    pueden entrar y salir sin llegar a coincidir nunca, y la prueba pasaria
    aunque no hubiera candado.
    """

    def __init__(self, tarda: float = 0.05):
        self.llamadas = 0
        self.a_la_vez = 0
        self.solapamientos = 0
        self._tarda = tarda
        self._contador = threading.Lock()

    def run_all(self):
        with self._contador:
            self.llamadas += 1
            self.a_la_vez += 1
            if self.a_la_vez > 1:
                self.solapamientos += 1
        try:
            time.sleep(self._tarda)
        finally:
            with self._contador:
                self.a_la_vez -= 1
        return _Informe()


class _Informe:
    changed = False
    seeds_recovered_chats: list[str] = []


def _orquestador(espia) -> Orchestrator:
    """Un orquestador con el mantenimiento ya puesto, sin tocar la base."""
    orq = object.__new__(Orchestrator)
    orq.maintenance = espia
    orq._database = None
    orq._settings = None
    return orq


@pytest.fixture(autouse=True)
def _bandera_limpia():
    reiniciar_mantenimiento()
    yield
    reiniciar_mantenimiento()


# ---------------------------------------------------------------------------
# 1. El arranque reconcilia una sola vez
# ---------------------------------------------------------------------------


def test_cinco_runtimes_reconcilian_UNA_vez():
    """Reconciliar la base entera cinco veces seguidas es cinco veces lo mismo."""
    espia = _MantenimientoEspia()

    for _ in range(5):
        _orquestador(espia).run_maintenance()

    assert espia.llamadas == 1, (
        "cada runtime volvio a reconciliar la base entera; con dos cuentas eso "
        "es lo que colgaba el arranque"
    )


def test_el_que_se_salta_devuelve_nulo_y_no_revienta():
    """Quien lea el informe tiene que aguantarlo, no explotar."""
    espia = _MantenimientoEspia()

    primero = _orquestador(espia).run_maintenance()
    segundo = _orquestador(espia).run_maintenance()

    assert primero is not None
    assert segundo is None


def test_dos_arranques_A_LA_VEZ_no_se_pisan():
    """El caso real: dos hilos, uno por cuenta, arrancando juntos."""
    espia = _MantenimientoEspia()
    hilos = [
        threading.Thread(target=lambda: _orquestador(espia).run_maintenance())
        for _ in range(4)
    ]

    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=10)

    assert not any(h.is_alive() for h in hilos), "algun arranque se quedo colgado"
    assert espia.llamadas == 1
    assert espia.solapamientos == 0


# ---------------------------------------------------------------------------
# 2. Forzar sigue funcionando, pero tampoco se solapa
# ---------------------------------------------------------------------------


def test_forzar_SI_vuelve_a_reconciliar():
    """Tras ingerir historial hay algo nuevo: saltarselo dejaria la base a medias."""
    espia = _MantenimientoEspia()
    orq = _orquestador(espia)

    orq.run_maintenance()
    orq.run_maintenance(forzar=True)
    orq.run_maintenance(forzar=True)

    assert espia.llamadas == 3


def test_ni_forzando_corren_dos_a_la_vez():
    """Forzar quiere decir "vuelve a hacerlo", no "hazlo a la vez que otro".

    Dos cuentas con el mismo intervalo de mantenimiento acaban coincidiendo,
    y ahi vuelve el bloqueo original aunque el arranque ya este arreglado.
    """
    espia = _MantenimientoEspia()
    hilos = [
        threading.Thread(
            target=lambda: _orquestador(espia).run_maintenance(forzar=True)
        )
        for _ in range(4)
    ]

    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=10)

    assert not any(h.is_alive() for h in hilos)
    assert espia.llamadas == 4, "forzadas, tienen que correr todas"
    assert espia.solapamientos == 0, (
        "dos reconciliaciones de la base entera a la vez: es exactamente lo "
        "que bloqueaba el arranque"
    )


# ---------------------------------------------------------------------------
# 3. Y las llamadas del codigo dicen lo que quieren decir
# ---------------------------------------------------------------------------


def test_prepare_NO_fuerza():
    """`prepare()` es el que corre una vez por runtime: es la avalancha."""
    import inspect

    fuente = inspect.getsource(Orchestrator.prepare)

    assert "run_maintenance()" in fuente
    assert "forzar" not in fuente, (
        "si `prepare` fuerza, cada cuenta vuelve a reconciliar la base entera "
        "al arrancar y el bloqueo vuelve"
    )


def test_las_que_deben_repetirse_fuerzan():
    """Tras historial, tras reconectar, tras backfill y el bucle periodico.

    Si alguna se quedara sin `forzar`, dejaria de reconciliar en cuanto el
    arranque hubiera puesto la bandera -- y el sintoma seria contadores y
    anclas que no se actualizan nunca, sin ningun error en el log.
    """
    import inspect

    import app.core.orchestrator as modulo

    fuente = inspect.getsource(modulo)
    # `self.run_maintenance` y no `run_maintenance(`: una de las llamadas se
    # hace en otro hilo --`to_thread(self.run_maintenance, forzar=True)`-- y
    # ahi el nombre va suelto, sin parentesis detras.
    llamadas = [
        linea.strip()
        for linea in fuente.splitlines()
        if "self.run_maintenance" in linea and not linea.strip().startswith("#")
    ]

    assert len(llamadas) == 5, f"cambio el numero de llamadas: {llamadas}"

    forzadas = [l for l in llamadas if "forzar=True" in l]
    assert len(forzadas) == 4, (
        f"se esperaban 4 llamadas forzadas, hay {len(forzadas)}: {llamadas}"
    )


# ---------------------------------------------------------------------------
# 4. Y lo que despierta la reconciliacion se queda en SU cuenta
# ---------------------------------------------------------------------------
#
# Consecuencia directa de lo anterior. `run_all` reconcilia la base ENTERA
# --`maintenance_service.py` no nombra `whatsapp_account_id` ni una vez-- asi
# que la lista de chats despertados mezcla cuentas. Antes la recibian todos
# los runtimes; ahora la recibe UNO. En los dos casos, encolar un chat ajeno
# es pedirle a este WhatsApp el historial de otra persona.


@pytest.fixture
def runtime_con_cola(runtime, session, cuenta):
    """Un runtime de `cuenta`, con la cola y el bus espiados."""

    class _Cola:
        def __init__(self):
            self.encolados = []

        def enqueue(self, jids):
            self.encolados.extend(jids)

    class _Bus:
        def __init__(self):
            self.publicados = []

        def publish(self, evento, datos):
            self.publicados.append((evento, datos))

    runtime.runtime_owner_account_id = cuenta.id
    runtime.seed_queue = _Cola()
    runtime.bus = _Bus()
    return runtime


def _chat_de(session, cuenta_id, jid):
    from app.models import Chat

    chat = Chat(jid=jid, chat_type="individual", whatsapp_account_id=cuenta_id)
    session.add(chat)
    session.flush()
    return chat


def _otra_cuenta(session, del_mismo_usuario):
    """Una segunda cuenta de WhatsApp, como la crea el fixture `cuenta`.

    Del MISMO usuario a proposito: es el caso del que se partio --dos telefonos
    bajo un solo correo de Google-- y el que hace que separar por usuario no
    baste.
    """
    import uuid as _uuid

    from app.models import WhatsAppAccount

    ident = _uuid.uuid4()
    fila = WhatsAppAccount(
        id=ident,
        user_id=del_mismo_usuario,
        session_status="linked",
        session_storage_key=f"accounts/{ident}",
    )
    session.add(fila)
    session.flush()
    return fila


def _jid() -> str:
    import uuid as _uuid

    return f"57{_uuid.uuid4().hex[:9]}@s.whatsapp.net"


def test_un_chat_de_OTRA_cuenta_no_se_encola(runtime_con_cola, session, cuenta):
    """El caso que importa: excavarlo seria traerse la conversacion de otro."""
    otra = _otra_cuenta(session, cuenta.user_id)
    ajeno = _jid()
    _chat_de(session, otra.id, ajeno)

    runtime_con_cola._encolar_despertados([ajeno])

    assert runtime_con_cola.seed_queue.encolados == []
    assert runtime_con_cola.bus.publicados == [], (
        "ni siquiera se anuncia: el frontend lo enseñaria como propio"
    )


def test_los_MIOS_si_se_encolan(runtime_con_cola, session, cuenta):
    """La otra mitad: despertar un chat propio tiene que seguir funcionando."""
    mio = _jid()
    _chat_de(session, cuenta.id, mio)

    runtime_con_cola._encolar_despertados([mio])

    assert runtime_con_cola.seed_queue.encolados == [mio]


def test_de_una_lista_mezclada_solo_pasan_los_propios(
    runtime_con_cola, session, cuenta
):
    """Es lo que devuelve de verdad una reconciliacion de la base entera."""
    otra = _otra_cuenta(session, cuenta.user_id)
    mio_a, mio_b, ajeno = _jid(), _jid(), _jid()
    _chat_de(session, cuenta.id, mio_a)
    _chat_de(session, cuenta.id, mio_b)
    _chat_de(session, otra.id, ajeno)

    runtime_con_cola._encolar_despertados([mio_a, ajeno, mio_b])

    assert runtime_con_cola.seed_queue.encolados == [mio_a, mio_b], (
        "se cuela un chat ajeno, o se pierde el orden por antiguedad"
    )


def test_sin_dueno_conocido_no_se_encola_nada(runtime_con_cola, session, cuenta):
    """Al arrancar, el dueno puede no saberse todavia.

    No se pierde nada: el mantenimiento periodico vuelve a pasar cuando ya se
    sabe. Adivinar, en cambio, es como se ingirieron 6613 mensajes ajenos.
    """
    mio = _jid()
    _chat_de(session, cuenta.id, mio)
    runtime_con_cola.runtime_owner_account_id = None

    runtime_con_cola._encolar_despertados([mio])

    assert runtime_con_cola.seed_queue.encolados == []


def test_un_chat_que_no_existe_no_se_encola(runtime_con_cola):
    """Sin fila no hay cuenta a la que atribuirlo."""
    runtime_con_cola._encolar_despertados(["fantasma@s.whatsapp.net"])

    assert runtime_con_cola.seed_queue.encolados == []
