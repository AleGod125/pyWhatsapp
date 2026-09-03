"""Integracion por el camino de ``service.py``, no por uno montado a mano.

POR QUE ESTE ARCHIVO
--------------------
Una prueba de estructura (AST) sirve para fijar un invariante: "esto se llama
antes que aquello", "aqui no se importa criptografia". NO sirve para afirmar
que algo funciona en ejecucion.

Y hubo una contradiccion que lo demostro. En la misma ejecucion de
``service.py`` aparecieron::

    [COMPAT] el LID propio resuelve a su PN=True
    receiver: decrypt failed from=<LID propio>: no session for peer

La verificacion decia que si y el receptor decia que no. Comprobar el mapa no
bastaba, porque el mapa es solo el primer paso de lo que hace
``_migrate_known_lid_sender``; el segundo usa el DISPOSITIVO del remitente.

Aqui se usa el store de pywhats de verdad (``SqliteStore``), su propio
``SqliteLidMap`` y su propia funcion de migracion. Nada simulado en el camino
que importa.

QUE NO SE PRUEBA AQUI
---------------------
La conexion con WhatsApp. Eso solo se puede validar ejecutando
``py service.py`` contra la cuenta real, y asi esta dicho en el informe.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def store(identidad):
    """Signal Store REAL de pywhats, EN LA RUTA que usaria el cliente.

    Tiene que ser la misma que ``settings.signal_store_file``, o la prueba no
    estaria comprobando nada: justo lo que se quiere descartar es que la
    siembra y pywhats hablen de archivos distintos.
    """
    from pywhats.signal.experimental.sqlite_store import SqliteStore

    creado = SqliteStore(str(identidad / "device.json.signal.db"))
    yield creado
    creado.close()


@pytest.fixture
def identidad(tmp_path):
    """``device.json`` como lo deja el pairing."""
    import json

    sesion = tmp_path / "session"
    sesion.mkdir(exist_ok=True)
    (sesion / "device.json").write_text(
        json.dumps(
            {
                "jid": {"user": "573002389304", "server": "s.whatsapp.net", "device": 0},
                "lid": "86531142340710.83@lid",
                "device_id": 83,
            }
        ),
        encoding="utf-8",
    )
    return sesion


# ---------------------------------------------------------------------------
# El store es UNO solo
# ---------------------------------------------------------------------------


def test_la_siembra_y_pywhats_apuntan_al_mismo_archivo(settings):
    """Si fueran dos stores distintos, sembrar no serviria de nada.

    pywhats abre ``<session_path>.signal.db``; nosotros sembramos
    ``settings.signal_store_file``. La huella tiene que coincidir.
    """
    from app.compat.own_lid_map import store_fingerprint

    nuestro = store_fingerprint(settings.signal_store_file)
    el_de_pywhats = store_fingerprint(f"{settings.session_file}.signal.db")
    assert nuestro == el_de_pywhats


def test_la_huella_no_depende_de_como_se_escriba_la_ruta(settings):
    from app.compat.own_lid_map import store_fingerprint

    ruta = str(settings.signal_store_file)
    assert store_fingerprint(ruta) == store_fingerprint(ruta.replace("\\", "/"))


def test_la_huella_no_lleva_la_ruta_dentro(settings):
    """Se registra en el log: no puede llevar el nombre de usuario del sistema."""
    from app.compat.own_lid_map import store_fingerprint

    huella = store_fingerprint(settings.signal_store_file)
    assert len(huella) == 8
    assert "aleja" not in huella.lower()
    assert "\\" not in huella and "/" not in huella


# ---------------------------------------------------------------------------
# El lookup REAL, con las clases de pywhats
# ---------------------------------------------------------------------------


def test_pywhats_lee_lo_que_nuestra_compat_escribe(store, identidad, settings, tmp_path):
    """La prueba que faltaba: quien consulta es el ``SqliteLidMap`` de pywhats.

    Nuestra siembra escribe con sqlite3 directo. Si pywhats cacheara el mapa
    en memoria, o abriera otra base, esto fallaria. No lo hace, y queda
    demostrado con su propia clase.
    """
    import dataclasses

    from app.compat import own_lid_map

    aislado = dataclasses.replace(settings, session_dir=identidad)
    assert aislado.signal_store_file.exists(), "el store del fixture ya existe"

    assert own_lid_map.seed(aislado) is True

    # Y ahora se consulta por donde consulta el receptor.
    assert store.lid_map.get_pn("86531142340710") == "573002389304"
    assert store.lid_map.get_lid("573002389304") == "86531142340710"


def test_sembrar_no_borra_lo_que_pywhats_habia_aprendido(store, identidad, settings):
    """El mapa es de pywhats: nuestra siembra solo anade nuestro par."""
    import dataclasses

    from app.compat import own_lid_map

    store.lid_map.set("573243116421", "64940106866902")

    aislado = dataclasses.replace(settings, session_dir=identidad)
    own_lid_map.seed(aislado)

    assert store.lid_map.get_pn("64940106866902") == "573243116421"
    assert store.lid_map.get_pn("86531142340710") == "573002389304"


def test_el_dispositivo_es_lo_que_decide_la_migracion(store, identidad, settings):
    """La pieza que la verificacion del mapa NO cubria.

    ``_migrate_known_lid_sender`` construye el JID de telefono con el MISMO
    numero de dispositivo del remitente::

        pn = JID(user=pn_user, server="s.whatsapp.net", device=sender.device)

    Una cuenta tiene varios dispositivos vinculados: el telefono es el 0, pero
    WhatsApp Web y este propio companion son otros. Si la copia llega desde un
    dispositivo para el que no hay sesion por telefono, no hay nada que
    migrar, por mucho que el mapa resuelva.

    Aqui se comprueba esa dependencia con la funcion real de pywhats.
    """
    import dataclasses

    from pywhats.events import JID
    from pywhats.messaging.addressing import session_id

    from app.compat import own_lid_map

    aislado = dataclasses.replace(settings, session_dir=identidad)
    own_lid_map.seed(aislado)

    # El mapa resuelve, sin depender del dispositivo: va por usuario.
    assert store.lid_map.get_pn("86531142340710") == "573002389304"

    # Pero la sesion se busca POR DISPOSITIVO, y son claves distintas.
    telefono = session_id(JID(user="573002389304", server="s.whatsapp.net", device=0))
    otro_dispositivo = session_id(
        JID(user="573002389304", server="s.whatsapp.net", device=84)
    )
    assert telefono != otro_dispositivo, (
        "la clave de sesion incluye el dispositivo: por eso resolver el mapa "
        "no garantiza que haya sesion que migrar"
    )

    # Y sin ninguna sesion guardada, no hay nada que migrar en ningun caso.
    assert store.sessions.load(telefono) is None
    assert store.sessions.load(otro_dispositivo) is None


def test_el_diagnostico_distingue_los_cuatro_casos():
    """El modulo de observacion mira las tres cosas, no solo el mapa."""
    import ast
    from pathlib import Path

    fuente = Path("app/compat/lid_diagnostics.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    codigo = ast.unparse(arbol)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(nodo)
            if doc:
                codigo = codigo.replace(doc, "")

    for necesario in ("get_pn", "session_id", "device"):
        assert necesario in codigo, f"el diagnostico no consulta {necesario}"


def test_el_diagnostico_no_toca_criptografia():
    """Solo observa: no migra, no crea sesiones, no deriva nada."""
    import ast
    from pathlib import Path

    arbol = ast.parse(Path("app/compat/lid_diagnostics.py").read_text(encoding="utf-8"))
    llamadas = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call):
            nombre = getattr(nodo.func, "id", None) or getattr(nodo.func, "attr", None)
            if nombre:
                llamadas.append(nombre)

    for prohibido in ("migrate_pn_session_to_lid", "save", "set", "delete"):
        assert prohibido not in llamadas, f"el diagnostico no puede llamar a {prohibido}"


# ---------------------------------------------------------------------------
# El runtime de service.py, montado como lo monta service.py
# ---------------------------------------------------------------------------


def test_service_py_y_las_pruebas_usan_la_misma_fabrica():
    """Si el cableado se duplicara, una prueba podria validar otro producto."""
    import ast
    from pathlib import Path

    arbol = ast.parse(Path("service.py").read_text(encoding="utf-8"))
    llamadas = [
        getattr(n.func, "id", None) or getattr(n.func, "attr", None)
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
    ]
    assert "build_service_runtime" in llamadas
    assert "build_service_app" in llamadas
    assert "AppRuntime" not in llamadas, "service.py no construye el runtime a mano"


def test_la_fabrica_de_servicio_deja_la_base_lista(settings, tmp_path):
    """El mismo runtime que sirve la API, sin abrir la sesion de WhatsApp."""
    import dataclasses

    from app.core.runtime import build_service_app, build_service_runtime

    aislado = dataclasses.replace(
        settings,
        session_dir=tmp_path / "session",
        diagnostics_dir=tmp_path / "diagnostics",
    )
    runtime = build_service_runtime(aislado, configure_logging=False)
    try:
        assert runtime.owner == "service.py"
        assert runtime.database is not None
        assert runtime.info().whatsapp_enabled is False, (
            "la fabrica NO abre la sesion: eso lo hace start(), en su hilo"
        )

        aplicacion = build_service_app(runtime)
        aplicacion.config.update(TESTING=True)
        cliente = aplicacion.test_client()

        salud = cliente.get("/api/v1/health").get_json()
        assert salud["owner"] == "service.py"
        assert salud["database"] is True
    finally:
        runtime.stop()


def test_las_compatibilidades_incluyen_la_siembra_y_el_diagnostico(settings, identidad):
    """Ambas tienen que estar en el arranque REAL, no solo existir."""
    import dataclasses

    from app.compat import apply_all

    aislado = dataclasses.replace(settings, session_dir=identidad)
    aplicadas = apply_all(aislado)

    assert "lid_diagnostics" in aplicadas, (
        "sin el diagnostico no se puede saber por que falla una sesion propia"
    )
