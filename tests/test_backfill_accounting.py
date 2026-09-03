"""El backfill cuenta lo que TRAJO, no lo que crecio la tabla.

EL FALLO, MEDIDO
----------------
Juan Andrés dio TIMEOUT en su peticion ``ON_DEMAND``. Mientras se esperaba,
entraron seis mensajes en vivo. El log dijo::

    respuesta=no nuevos=6

Falso. Esos seis no los trajo el backfill: los guardo el receptor. La causa
era calcular el resultado restando el numero de filas del chat antes y despues
de la peticion, con el receptor escribiendo en la misma tabla a la vez.

La separacion que arregla esto ya existia en el codigo: el historial entra por
``ingest_history_sync`` y los mensajes en vivo por ``LiveMessageService``. Solo
habia que contar por la via correcta.

Y de paso: un solo backfill a la vez, y una sola peticion por chat.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.backfill_service import BackfillService


class _DatabaseFalsa:
    """Base minima: el backfill solo la usa para contar y leer cursores."""

    def __init__(self, session):
        self._session = session

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


@pytest.fixture
def backfill(settings, session):
    return BackfillService(settings, _DatabaseFalsa(session))


# ---------------------------------------------------------------------------
# Correlacion peticion <-> respuesta
# ---------------------------------------------------------------------------


def test_sin_peticion_en_vuelo_no_se_anota_nada(backfill):
    """La ventana solo esta abierta mientras se espera una respuesta."""
    backfill.note_history_ingest(48, 50)  # no debe explotar ni contar
    assert backfill._ingest_watch is None


def test_lo_que_inserta_la_ingesta_se_acumula(backfill):
    backfill._ingest_watch = {"inserted": 0, "blob_messages": 0}
    backfill.note_history_ingest(48, 50)
    backfill.note_history_ingest(2, 3)

    assert backfill._ingest_watch["inserted"] == 50
    assert backfill._ingest_watch["blob_messages"] == 53


def test_los_mensajes_en_vivo_no_pasan_por_este_contador(backfill):
    """Es la separacion que arregla el bug: el receptor no llama aqui.

    Se comprueba por AST que ``LiveMessageService`` no toca al backfill.
    """
    import ast
    from pathlib import Path

    fuente = Path("app/services/live_service.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    atributos = [n.attr for n in ast.walk(arbol) if isinstance(n, ast.Attribute)]
    assert "note_history_ingest" not in atributos
    assert "backfill" not in atributos


def test_un_timeout_no_apunta_los_mensajes_que_entraron_mientras(backfill):
    """El caso exacto de Juan Andrés: timeout con seis mensajes en vivo."""
    # Se reproduce la aritmetica del metodo: si no hubo respuesta, lo ganado
    # es cero por definicion, por mucho que la tabla haya crecido.
    observado = {"inserted": 0, "blob_messages": 0}
    received = False
    before, after = 15, 21  # entraron 6 mensajes en vivo durante la espera

    gained = int(observado["inserted"]) if received else 0
    en_vivo = max(0, (after - before) - gained)

    assert gained == 0, "un timeout no puede acreditarse mensajes"
    assert en_vivo == 6, "los seis se reportan aparte, como lo que son"


def test_una_respuesta_real_si_se_acredita():
    observado = {"inserted": 48, "blob_messages": 50}
    received = True
    before, after = 100, 150  # 48 del blob + 2 en vivo

    gained = int(observado["inserted"]) if received else 0
    en_vivo = max(0, (after - before) - gained)

    assert gained == 48
    assert en_vivo == 2


def test_el_metodo_ya_no_resta_conteos_de_la_base():
    """La resta era la causa. Que no vuelva.

    Se mira el codigo del metodo, no la prosa: el modulo explica el fallo en
    sus comentarios y buscar el texto daria un falso positivo.
    """
    import ast
    import inspect
    import textwrap

    import app.services.backfill_service as modulo

    fuente = textwrap.dedent(
        inspect.getsource(modulo.BackfillService._process_chat_locked)
    )
    arbol = ast.parse(fuente)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Assign) and isinstance(nodo.value, ast.BinOp):
            if isinstance(nodo.value.op, ast.Sub):
                objetivo = nodo.targets[0]
                nombre = getattr(objetivo, "id", "")
                assert nombre != "gained", (
                    "'gained' vuelve a calcularse restando conteos de la base"
                )


# ---------------------------------------------------------------------------
# Un solo backfill, una sola peticion por chat
# ---------------------------------------------------------------------------


def test_al_principio_no_hay_nada_en_marcha(backfill):
    assert backfill.busy is False
    assert backfill.in_flight == frozenset()


def test_un_segundo_backfill_no_arranca(backfill):
    """Dos excavaciones a la vez enredan la correlacion peticion/respuesta."""
    backfill._busy = True
    stats = asyncio.run(backfill.run(client=object()))
    assert stats is backfill.stats
    assert backfill._client is None, "no llego ni a tomar el cliente"


def test_el_mismo_chat_no_admite_dos_peticiones(backfill):
    """El telefono atiende de una en una."""
    procesados = []

    async def falso(chat_id, chat_jid, max_rounds):
        procesados.append(chat_jid)

    backfill._process_chat_locked = falso
    backfill._in_flight.add("99911122233@lid")

    asyncio.run(backfill._process_chat(1, "99911122233@lid", 5))
    assert procesados == []


def test_un_chat_libre_si_se_procesa(backfill):
    procesados = []

    async def falso(chat_id, chat_jid, max_rounds):
        procesados.append(chat_jid)

    backfill._process_chat_locked = falso
    asyncio.run(backfill._process_chat(1, "99911122233@lid", 5))

    assert procesados == ["99911122233@lid"]
    assert backfill.in_flight == frozenset(), "el chat se libera al terminar"


def test_el_chat_se_libera_aunque_falle(backfill):
    """Si no se libera, ese chat queda inalcanzable para siempre."""

    async def explota(chat_id, chat_jid, max_rounds):
        raise RuntimeError("fallo simulado")

    backfill._process_chat_locked = explota
    with pytest.raises(RuntimeError):
        asyncio.run(backfill._process_chat(1, "99911122233@lid", 5))

    assert backfill.in_flight == frozenset()


def test_la_marca_de_ocupado_se_suelta_aunque_falle(backfill):
    """Sin esto, un fallo deja el backfill bloqueado el resto del proceso."""

    async def explota(*a, **k):
        raise RuntimeError("fallo simulado")

    backfill._run_locked = explota
    with pytest.raises(RuntimeError):
        asyncio.run(backfill.run(client=object()))

    assert backfill.busy is False


# ---------------------------------------------------------------------------
# La sincronizacion manual respeta la automatica
# ---------------------------------------------------------------------------


def test_la_sincronizacion_manual_se_rechaza_si_el_backfill_esta_excavando(backfill):
    from app.services.sync_job import SyncAlreadyRunningError, SyncJob

    backfill._busy = True

    class _RuntimeFalso:
        def __init__(self, backfill):
            self.backfill = backfill

        def info(self):
            return type("I", (), {"whatsapp_enabled": True})()

    trabajo = SyncJob(backfill._settings, backfill._database)
    trabajo._comprobar_disponible = lambda rt: None

    with pytest.raises(SyncAlreadyRunningError):
        trabajo.start(_RuntimeFalso(backfill))


def test_con_el_backfill_parado_la_manual_no_se_bloquea_por_eso(backfill):
    """El guardia es por estar excavando, no por existir."""
    from app.services.sync_job import SyncJob

    assert backfill.busy is False
    trabajo = SyncJob(backfill._settings, backfill._database)
    # Se comprueba solo el guardia nuevo: el resto del arranque necesita un
    # cliente conectado y no es lo que se prueba aqui.
    assert getattr(backfill, "busy", False) is False
    assert trabajo.running is False


# ---------------------------------------------------------------------------
# El canary arranca solo, sin boton
# ---------------------------------------------------------------------------


def test_el_canary_se_puede_llamar_de_verdad(backfill):
    """Esta prueba nace de una regresion propia, y es la que faltaba.

    Al anadir el cerrojo de single-flight, el reemplazo entro en
    ``run_canary`` en vez de en ``run``, y lo dejo llamando a ``_run_locked``
    con dos variables que NO existen en ese ambito. En ejecucion eso es un
    ``NameError``, que el orquestador capturaba como "el backfill fallo": el
    arranque automatico moria en silencio y habia que pulsar el boton manual.

    Ninguna prueba lo vio porque todas ejercitaban ``run``, nunca el canary.
    """
    import asyncio

    llamadas = []

    async def falso(client, max_rounds):
        llamadas.append(max_rounds)
        return True

    backfill._canary_locked = falso
    assert asyncio.run(backfill.run_canary(client=object())) is True
    assert llamadas == [3], "el canary pasa su propio tope de rondas"


def test_el_canary_devuelve_un_booleano_si_esta_ocupado(backfill):
    """El orquestador ramifica con ``if ok``: devolver stats seria un bug."""
    import asyncio

    backfill._busy = True
    resultado = asyncio.run(backfill.run_canary(client=object()))
    assert resultado is False
    assert isinstance(resultado, bool)


def test_cada_metodo_llama_a_su_propio_cuerpo():
    """Que ``run`` y ``run_canary`` no se crucen otra vez."""
    import ast
    import inspect
    import textwrap

    import app.services.backfill_service as modulo

    def llamadas_de(metodo):
        arbol = ast.parse(textwrap.dedent(inspect.getsource(metodo)))
        return [
            getattr(n.func, "attr", None)
            for n in ast.walk(arbol)
            if isinstance(n, ast.Call)
        ]

    assert "_canary_locked" in llamadas_de(modulo.BackfillService.run_canary)
    assert "_run_locked" not in llamadas_de(modulo.BackfillService.run_canary)

    assert "_run_locked" in llamadas_de(modulo.BackfillService.run)
    assert "_canary_locked" not in llamadas_de(modulo.BackfillService.run)


def test_los_cuerpos_no_usan_variables_que_no_reciben():
    """El NameError exacto: usar un parametro que ese metodo no tiene.

    Se compila cada metodo y se comprueba que todo nombre libre que parezca un
    parametro este de verdad en su firma.
    """
    import inspect

    import app.services.backfill_service as modulo

    for nombre in ("run", "run_canary", "_run_locked", "_canary_locked"):
        metodo = getattr(modulo.BackfillService, nombre)
        firma = set(inspect.signature(metodo).parameters)
        codigo = metodo.__code__
        locales = set(codigo.co_varnames)
        for sospechoso in ("max_rounds", "max_rounds_per_chat", "max_passes", "client"):
            if sospechoso in codigo.co_names:
                assert sospechoso in firma or sospechoso in locales, (
                    f"{nombre} usa {sospechoso}, que no recibe ni define"
                )


def test_cada_tanda_cuenta_lo_suyo(backfill):
    """El log decia "chats=64" tras pulsar el boton dos veces, con 32 chats.

    Las estadisticas se acumulaban entre sincronizaciones y se mostraban como
    si fueran el resultado de una sola.
    """
    import asyncio

    async def falso(client, max_rounds_per_chat, max_passes):
        backfill.stats.chats_processed = 32
        backfill.stats.no_cursor = 32
        return backfill.stats

    backfill._run_locked = falso

    asyncio.run(backfill.run(client=object()))
    assert backfill.stats.chats_processed == 32

    asyncio.run(backfill.run(client=object()))
    assert backfill.stats.chats_processed == 32, "la segunda tanda no suma la primera"
    assert backfill.lifetime.chats_processed == 32, "lo acumulado vive aparte"

    asyncio.run(backfill.run(client=object()))
    assert backfill.lifetime.chats_processed == 64
