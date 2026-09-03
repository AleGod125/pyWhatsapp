"""El producto funciona entero con UN solo QR. Baileys queda fuera.

POR QUE IMPORTA
---------------
La sesion auxiliar vincula un SEGUNDO dispositivo a la cuenta del usuario, con
su propio codigo. Para el uso normal no hace falta, y pedir dos QR convierte
una app de copia local en algo que parece pedir permisos de mas.

Asi que la ruta normal tiene que funcionar sin ella, y estas pruebas lo fijan:
que este apagada por defecto, que nada la arranque sola, y que la revision de
historiales pendientes no la necesite ni la importe.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.models import Chat, ChatHistoryState
from app.services.pending_recheck import PendingRecheckService

FANTASMA = "99977766655@lid"


class _DatabaseDeSesion:
    def __init__(self, session):
        self._session = session

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


class _RuntimeFalso:
    def __init__(self):
        self.encolados: list[str] = []
        self.seed_queue = self

    def enqueue(self, jids):
        self.encolados.extend(jids)
        return list(jids)


@pytest.fixture
def fantasma(session):
    chat = Chat(jid=FANTASMA, chat_type="individual", name="Sin ancla")
    session.add(chat)
    session.flush()
    session.add(
        ChatHistoryState(
            chat_id=chat.id, chat_jid=FANTASMA, history_status="waiting_seed"
        )
    )
    session.flush()
    return chat


@pytest.fixture
def revision(settings, session):
    return PendingRecheckService(settings, _DatabaseDeSesion(session))


# ---------------------------------------------------------------------------
# Apagado por defecto
# ---------------------------------------------------------------------------


def test_la_sesion_auxiliar_viene_apagada(settings):
    """Un segundo QR no puede ser el estado por defecto del producto."""
    assert settings.web_bootstrap_enabled is False


def test_se_puede_encender_cuando_haga_falta(monkeypatch):
    """No se borro: se aparto. Sigue disponible para el futuro."""
    from app.core.config import load_settings

    monkeypatch.setenv("WEB_BOOTSTRAP_ENABLED", "true")
    assert load_settings().web_bootstrap_enabled is True


def test_nada_arranca_baileys_solo():
    """El arranque no puede levantar un proceso Node ni pedir un QR auxiliar."""
    for archivo in ("app/core/runtime.py", "app/core/orchestrator.py", "service.py"):
        fuente = Path(archivo).read_text(encoding="utf-8")
        for prohibido in ("web_seed_provider", "WebSeedProvider", "HistoryRecoveryService"):
            assert prohibido not in fuente, (
                f"{archivo} no puede tocar {prohibido}: el arranque no vincula "
                f"un segundo dispositivo"
            )


def test_el_producto_no_depende_de_app_experimental():
    """La frontera del paquete, no solo de tres archivos.

    ``app/experimental`` existe para que lo experimental se vea desde el arbol
    de carpetas. Si el runtime, la sincronizacion o los servicios normales lo
    importaran, volveria a estar mezclado sin que se notara.
    """
    import ast

    paquetes = ("app/core", "app/services", "app/models", "app/events", "app/compat")
    culpables = []
    for paquete in paquetes:
        for ruta in Path(paquete).rglob("*.py"):
            arbol = ast.parse(ruta.read_text(encoding="utf-8"))
            for nodo in ast.walk(arbol):
                if isinstance(nodo, ast.ImportFrom) and (nodo.module or "").startswith(
                    "app.experimental"
                ):
                    culpables.append(f"{ruta}: {nodo.module}")
                elif isinstance(nodo, ast.Import):
                    culpables += [
                        f"{ruta}: {a.name}"
                        for a in nodo.names
                        if a.name.startswith("app.experimental")
                    ]

    assert not culpables, "el producto no puede depender de lo experimental: " + str(
        culpables
    )


def test_lo_experimental_solo_se_carga_bajo_demanda():
    """Se importa DENTRO de la funcion, no en la cabecera del modulo.

    Un import en cabecera cargaria el modulo en cada arranque aunque el flag
    este apagado, y bastaria un fallo suyo para tumbar la API entera.
    """
    import ast

    arbol = ast.parse(Path("app/api/routes.py").read_text(encoding="utf-8"))
    for nodo in arbol.body:  # solo el nivel superior
        if isinstance(nodo, ast.ImportFrom):
            assert not (nodo.module or "").startswith("app.experimental")


def test_las_rutas_apagadas_siguen_registradas():
    """Y es deliberado.

    Una ruta que no existe devuelve 404 SIN cabeceras CORS, y el navegador lo
    reporta como error de CORS: manda a diagnosticar el sitio equivocado. Ya
    paso una vez. Registrada, el frontend recibe un motivo legible.
    """
    import types

    from app.api.app_factory import create_app
    from app.core.config import load_settings

    app = create_app(runtime=types.SimpleNamespace(settings=load_settings()))
    reglas = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/v1/history/web-bootstrap/recover-pending" in reglas
    assert "/api/v1/history/recheck-pending" in reglas


def test_la_puerta_explica_la_alternativa():
    """Un 'desactivado' a secas deja al frontend sin saber que usar."""
    fuente = Path("app/api/routes.py").read_text(encoding="utf-8")
    assert "WEB_BOOTSTRAP_DISABLED" in fuente
    assert "recheck-pending" in fuente


# ---------------------------------------------------------------------------
# La revision local no depende de la auxiliar
# ---------------------------------------------------------------------------


def test_la_revision_no_importa_nada_de_baileys():
    """La frontera. Si esto se rompe, el producto vuelve a necesitar dos QR."""
    arbol = ast.parse(Path("app/services/pending_recheck.py").read_text(encoding="utf-8"))
    importados = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module:
            importados.append(nodo.module)
        elif isinstance(nodo, ast.Import):
            importados.extend(a.name for a in nodo.names)

    for prohibido in ("web_seed_provider", "history_recovery", "subprocess"):
        assert not any(prohibido in m for m in importados), (
            f"la revision local no puede depender de {prohibido}"
        )


def test_se_revisan_los_que_esperan_ancla(revision, session, fantasma):
    jids = {c["chat_jid"] for c in revision._pendientes()}
    assert FANTASMA in jids


def test_un_chat_ya_excavado_no_se_revisa(revision, session, fantasma):
    session.execute(
        ChatHistoryState.__table__.update()
        .where(ChatHistoryState.chat_jid == FANTASMA)
        .values(history_status="exhausted")
    )
    session.flush()
    assert FANTASMA not in {c["chat_jid"] for c in revision._pendientes()}


def test_lo_que_despierta_va_al_motor_de_siempre(revision):
    """Sin cola no se pierde el aviso, y con cola se encola. Nada mas."""
    runtime = _RuntimeFalso()
    revision._encolar(runtime, [FANTASMA])
    assert runtime.encolados == [FANTASMA]

    class SinCola:
        seed_queue = None

    revision._encolar(SinCola(), [FANTASMA])  # no lanza


def test_sin_pendientes_termina_en_el_acto(revision, session):
    session.execute(
        ChatHistoryState.__table__.update().values(history_status="exhausted")
    )
    session.flush()

    trabajo = revision.start(_RuntimeFalso())
    assert trabajo.state == "completed"
    assert trabajo.total == 0
    assert revision.busy is False


def test_solo_una_revision_a_la_vez(revision, session, fantasma):
    revision._activo = "otra"
    with pytest.raises(RuntimeError, match="ya hay una revision"):
        revision.start(_RuntimeFalso())


# ---------------------------------------------------------------------------
# La extraccion automatica al abrir el panel
# ---------------------------------------------------------------------------


def test_la_automatica_no_repite_si_acaba_de_correr(revision, session, fantasma):
    """Un F5 repetido reinterpretaria los mismos blobs sin que cambie nada."""
    import time as reloj

    from app.services.pending_recheck import RecheckJob

    revision._ultimo = RecheckJob(job_id="previo", finished_at=reloj.time())

    trabajo = revision.start(_RuntimeFalso(), auto=True)
    assert trabajo.skipped is True
    assert trabajo.state == "completed", "omitir no es fallar"
    assert revision.busy is False


def test_el_boton_manual_no_respeta_la_espera(revision, session, fantasma):
    """Si el usuario lo pulsa, es que quiere mirar YA.

    Se vacia la lista de pendientes a proposito: arrancar el trabajo de verdad
    lanzaria un hilo que usaria la sesion de prueba en paralelo y la dejaria en
    transaccion abierta. Lo que se comprueba aqui es la DECISION, no el trabajo.
    """
    import time as reloj

    from app.services.pending_recheck import RecheckJob

    revision._ultimo = RecheckJob(job_id="previo", finished_at=reloj.time())
    assert revision._espera_restante() > 0, "la espera esta activa"

    revision._pendientes = lambda: []
    trabajo = revision.start(_RuntimeFalso())

    assert trabajo.skipped is False, "el boton manual nunca se omite"


def test_la_automatica_se_engancha_a_la_que_ya_corre(revision, session, fantasma):
    """No puede fallar por algo que el usuario no pidio."""
    from app.services.pending_recheck import RecheckJob

    enMarcha = RecheckJob(job_id="enmarcha", state="running", total=5)
    revision._trabajos["enmarcha"] = enMarcha
    revision._activo = "enmarcha"

    assert revision.start(_RuntimeFalso(), auto=True) is enMarcha


def test_sin_espera_configurada_la_automatica_corre_siempre(settings):
    """0 la desactiva, para quien quiera revisar en cada carga.

    Sin fixtures de base a proposito: esto solo mide el calculo de la espera, y
    pedir una sesion aqui chocaba con la transaccion abierta de otra prueba.
    """
    import dataclasses
    import time as reloj

    from app.services.pending_recheck import RecheckJob

    sin_espera = dataclasses.replace(settings, auto_recheck_cooldown_seconds=0.0)
    servicio = PendingRecheckService(sin_espera, database=None)
    servicio._ultimo = RecheckJob(job_id="previo", finished_at=reloj.time())

    assert servicio._espera_restante() == 0.0


def test_la_espera_se_agota(revision):
    import time as reloj

    from app.services.pending_recheck import RecheckJob

    revision._ultimo = RecheckJob(
        job_id="viejo", finished_at=reloj.time() - 10_000
    )
    assert revision._espera_restante() == 0.0


# ---------------------------------------------------------------------------
# Lo que ve el frontend
# ---------------------------------------------------------------------------


def test_el_progreso_trae_lo_que_el_frontend_necesita():
    from app.services.pending_recheck import ChatProgress, RecheckJob

    cuerpo = RecheckJob(
        job_id="abc",
        total=30,
        state="running",
        processed=11,
        recovered=4,
        still_waiting=6,
        errors=1,
        current=ChatProgress(13, "ubernel", "rechecking"),
    ).to_json()

    for clave in ("job_id", "state", "total", "processed", "recovered", "still_waiting", "errors"):
        assert clave in cuerpo
    assert cuerpo["current_chat"] == {"id": 13, "name": "ubernel", "state": "rechecking"}


def test_los_eventos_llegan_al_frontend():
    from app.api.routes import EVENT_NAMES

    for evento in (
        "history.recheck.started",
        "history.recheck.progress",
        "history.recheck.completed",
        "history.backfill.started",
        "history.backfill.completed",
    ):
        assert evento in EVENT_NAMES, f"falta {evento} en la traduccion a SSE"


# ---------------------------------------------------------------------------
# El resumen medido
# ---------------------------------------------------------------------------


def test_el_resumen_cuenta_lo_que_hay(session, fantasma):
    from app.services.product_report import collect

    informe = collect(session)
    assert informe.chats_total >= 1
    assert informe.waiting_seed >= 1
    assert "waiting_seed" in informe.to_json()["by_status"]


def test_el_resumen_no_promete_de_mas():
    """Nunca 'backup completo'. Solo lo que WhatsApp entrego."""
    fuente = Path("app/services/product_report.py").read_text(encoding="utf-8")
    assert "recuperable que WhatsApp ha proporcionado" in fuente
    assert "100%" not in fuente.split('"""')[2] if '"""' in fuente else True


def test_el_resumen_nunca_tumba_el_backfill(settings):
    """Es observacion: si falla, se calla y el backfill sigue."""
    from app.services.product_report import log_summary

    class BaseRota:
        def transaction(self):
            raise RuntimeError("la base no esta")

    assert log_summary(BaseRota()) is None


# ---------------------------------------------------------------------------
# El reset
# ---------------------------------------------------------------------------


def test_el_reset_no_borra_sin_que_se_lo_pidan():
    fuente = Path("tools/reset_product_test.py").read_text(encoding="utf-8")
    assert "--aplicar" in fuente
    assert 'respuesta != "BORRAR"' in fuente


def test_el_reset_no_separa_identidad_y_signal_store():
    """Separarlas fabrico la identidad mezclada. No puede volver a pasar."""
    arbol = ast.parse(Path("tools/reset_product_test.py").read_text(encoding="utf-8"))
    funcion = next(
        n for n in ast.walk(arbol)
        if isinstance(n, ast.FunctionDef) and n.name == "borrar_sesion"
    )
    cuerpo = ast.unparse(funcion)
    assert "session_file" in cuerpo and "signal_store_file" in cuerpo, (
        "las dos van juntas o no va ninguna"
    )


def test_el_reset_conserva_migraciones_y_diagnosticos():
    fuente = Path("tools/reset_product_test.py").read_text(encoding="utf-8")
    assert "alembic_version" in fuente
    for jamas in ("DROP TABLE", "DROP DATABASE", "DROP SCHEMA"):
        assert jamas not in fuente.upper(), f"el reset no puede hacer {jamas}"


def test_el_reset_para_si_el_servicio_esta_vivo():
    """Borrar con el Signal Store abierto se salta el archivo en silencio."""
    fuente = Path("tools/reset_product_test.py").read_text(encoding="utf-8")
    assert "servicio_en_marcha" in fuente
    assert "ABORTADO" in fuente


def test_el_reset_no_pone_la_contrasena_en_argv():
    """Cualquiera que liste procesos veria la URL entera de PostgreSQL."""
    import sys

    sys.path.insert(0, ".")
    from tools.reset_product_test import _entorno_pg, copia_de_seguridad  # noqa: F401

    entorno = _entorno_pg("postgresql://ana:cl%40ve@localhost:5432/pywa")
    assert entorno["PGPASSWORD"] == "cl@ve", "la contrasena se decodifica"
    assert entorno["PGUSER"] == "ana"
    assert entorno["PGDATABASE"] == "pywa"

    # Y el comando NO la lleva.
    arbol = ast.parse(Path("tools/reset_product_test.py").read_text(encoding="utf-8"))
    funcion = next(
        n for n in ast.walk(arbol)
        if isinstance(n, ast.FunctionDef) and n.name == "copia_de_seguridad"
    )
    llamada = ast.unparse(funcion)
    assert "'pg_dump', '--no-owner', '--file'" in llamada.replace('"', "'")
    assert "database_url" not in llamada.split("subprocess.run")[1].split(")")[0]
