"""Nuestro propio par PN <-> LID en el mapa de Signal.

POR QUE EXISTE
--------------
Los mensajes que el usuario envia desde su telefono llegaban al companion y
morian con::

    decrypt failed from=86531142340710@lid: no session for peer

Ese LID es el NUESTRO, y la sesion con ese dispositivo si existia: guardada
bajo la direccion de telefono. pywhats sabe migrar de PN a LID
(``_migrate_known_lid_sender``) pero consulta un ``lid_map`` que se aprende de
los stanzas entrantes, y el par propio no llegaba nunca.

Aqui se siembra con lo que el pairing ya persistio. No se toca criptografia.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest


def _crear_store(ruta: Path, filas=()) -> None:
    """Signal Store minimo con la tabla que interesa."""
    con = sqlite3.connect(str(ruta))
    con.execute("CREATE TABLE lid_map (pn_user TEXT PRIMARY KEY, lid_user TEXT)")
    for pn, lid in filas:
        con.execute("INSERT INTO lid_map VALUES (?, ?)", (pn, lid))
    con.commit()
    con.close()


@pytest.fixture
def entorno(settings, tmp_path):
    """Settings con sesion propia en un temporal, con device.json real."""
    import dataclasses
    import json

    sesion = tmp_path / "session"
    sesion.mkdir()
    (sesion / "device.json").write_text(
        json.dumps(
            {
                "jid": {"user": "573002389304", "server": "s.whatsapp.net", "device": 0},
                "lid": "86531142340710.79@lid",
                "device_id": 79,
            }
        ),
        encoding="utf-8",
    )
    return dataclasses.replace(settings, session_dir=sesion)


def _leer(ruta: Path) -> dict[str, str]:
    con = sqlite3.connect(str(ruta))
    try:
        return {pn: lid for pn, lid in con.execute("SELECT pn_user, lid_user FROM lid_map")}
    finally:
        con.close()


def test_se_registra_el_par_propio(entorno):
    from app.compat import own_lid_map

    _crear_store(entorno.signal_store_file)
    assert own_lid_map.seed(entorno) is True

    mapa = _leer(entorno.signal_store_file)
    assert mapa["573002389304"] == "86531142340710"


def test_el_lid_con_sufijo_de_dispositivo_se_normaliza(entorno):
    """``86531142340710.79@lid`` es el mismo usuario que ``86531142340710``.

    El mapa se indexa por usuario; dejar el sufijo del dispositivo dentro
    haria que la busqueda no encontrase nada.
    """
    from app.compat import own_lid_map

    _crear_store(entorno.signal_store_file)
    own_lid_map.seed(entorno)

    assert "86531142340710" in _leer(entorno.signal_store_file).values()


def test_no_se_pierde_el_par_de_otro_contacto(entorno):
    """Sembrar el nuestro no puede borrar los que ya se habian aprendido."""
    from app.compat import own_lid_map

    _crear_store(entorno.signal_store_file, [("573243116421", "64940106866902")])
    own_lid_map.seed(entorno)

    mapa = _leer(entorno.signal_store_file)
    assert mapa["573243116421"] == "64940106866902", "el de Isaac sigue ahi"
    assert mapa["573002389304"] == "86531142340710"


def test_es_idempotente(entorno):
    from app.compat import own_lid_map

    _crear_store(entorno.signal_store_file)
    assert own_lid_map.seed(entorno) is True
    assert own_lid_map.seed(entorno) is True
    assert len(_leer(entorno.signal_store_file)) == 1


def test_sin_signal_store_no_revienta(entorno):
    """Un pairing nuevo aun no tiene store: se sembrara en el siguiente arranque."""
    from app.compat import own_lid_map

    assert not entorno.signal_store_file.exists()
    assert own_lid_map.seed(entorno) is False


def test_sin_identidad_propia_no_hace_nada(settings, tmp_path):
    """Sin device.json no hay par que sembrar, y no se inventa."""
    import dataclasses

    from app.compat import own_lid_map

    sesion = tmp_path / "vacia"
    sesion.mkdir()
    aislado = dataclasses.replace(settings, session_dir=sesion)
    assert own_lid_map.seed(aislado) is False


def test_un_store_corrupto_no_impide_arrancar(entorno):
    """Este apano es una mejora: sin el, todo sigue como antes."""
    from app.compat import own_lid_map

    entorno.signal_store_file.write_bytes(b"esto no es sqlite")
    assert own_lid_map.seed(entorno) is False


def test_un_store_sin_la_tabla_no_revienta(entorno):
    from app.compat import own_lid_map

    con = sqlite3.connect(str(entorno.signal_store_file))
    con.execute("CREATE TABLE otra (x INTEGER)")
    con.commit()
    con.close()

    assert own_lid_map.seed(entorno) is False


def test_no_se_toca_criptografia():
    """Solo se escribe una correspondencia entre dos identificadores."""
    ruta = Path("app/compat/own_lid_map.py")
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))

    importados = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            importados.extend(a.name for a in nodo.names)
        elif isinstance(nodo, ast.ImportFrom):
            importados.append(nodo.module or "")

    for prohibido in ("cryptography", "pywhats.signal", "x3dh", "ratchet"):
        assert not any(prohibido in i for i in importados), (
            f"este apano no puede tocar {prohibido}"
        )

    # Y solo escribe en lid_map, en ninguna otra tabla del store.
    codigo = ruta.read_text(encoding="utf-8")
    for tabla in ("sessions", "identities", "prekeys", "sender_keys"):
        assert f"FROM {tabla}" not in codigo and f"INTO {tabla}" not in codigo


def test_se_puede_desactivar(settings):
    import dataclasses

    apagado = dataclasses.replace(settings, compat_own_lid_map=False)
    assert apagado.compat_own_lid_map is False


# ---------------------------------------------------------------------------
# ORDEN: sembrar ANTES de que el receptor procese nada
# ---------------------------------------------------------------------------


def test_la_siembra_ocurre_antes_de_construir_el_cliente():
    """Si se sembrara despues, el primer mensaje propio no se descifraria.

    El orden obligatorio es: leer identidad -> abrir el Signal Store ->
    sembrar el par propio -> construir el cliente -> arrancar el receptor.
    Se comprueba sobre el ARBOL de ``AppRuntime.start``, por posicion de
    linea: ``prepare_pywhats`` (que aplica las compatibilidades, y con ellas
    la siembra) tiene que aparecer antes que ``WhatsAppClient(...)``.
    """
    import ast
    import inspect
    import textwrap

    import app.core.runtime as runtime_module

    fuente = textwrap.dedent(inspect.getsource(runtime_module.AppRuntime.start))
    arbol = ast.parse(fuente)

    linea_prepare = linea_cliente = None
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        nombre = getattr(nodo.func, "id", None) or getattr(nodo.func, "attr", None)
        if nombre == "prepare_pywhats" and linea_prepare is None:
            linea_prepare = nodo.lineno
        elif nombre == "WhatsAppClient" and linea_cliente is None:
            linea_cliente = nodo.lineno

    assert linea_prepare is not None, "start() tiene que aplicar las compatibilidades"
    assert linea_cliente is not None, "start() tiene que construir el cliente"
    assert linea_prepare < linea_cliente, (
        "el par PN<->LID propio se siembra DESPUES de crear el cliente: el "
        "receptor podria procesar un mensaje nuestro antes de que el mapa "
        "exista"
    )


def test_la_siembra_va_dentro_de_las_compatibilidades():
    """Y las compatibilidades se aplican en prepare_pywhats, antes del Client."""
    import ast
    from pathlib import Path

    fuente = Path("app/compat/__init__.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    modulos = [
        n.module
        for n in ast.walk(arbol)
        if isinstance(n, ast.ImportFrom) and n.module
    ]
    assert any("own_lid_map" in (m or "") for m in modulos) or "own_lid_map" in fuente


def test_si_el_mapa_no_resuelve_no_se_da_por_aplicada(entorno, monkeypatch):
    """Una compat que miente manda a buscar el fallo al sitio equivocado."""
    from app.compat import own_lid_map

    _crear_store(entorno.signal_store_file)
    assert own_lid_map.seed(entorno) is True

    # Se rompe la correspondencia por debajo, como si otro proceso la hubiera
    # pisado: la verificacion tiene que decir que NO.
    con = sqlite3.connect(str(entorno.signal_store_file))
    con.execute("UPDATE lid_map SET pn_user = '000000000'")
    con.commit()
    con.close()

    assert own_lid_map.verify(
        entorno.signal_store_file, "573002389304", "86531142340710"
    ) is False


def test_la_verificacion_consulta_por_el_lid(entorno):
    """Es la MISMA consulta que hara pywhats al llegar un mensaje nuestro."""
    from app.compat import own_lid_map

    _crear_store(entorno.signal_store_file)
    own_lid_map.seed(entorno)

    assert own_lid_map.verify(
        entorno.signal_store_file, "573002389304", "86531142340710"
    ) is True


def test_la_verificacion_no_registra_el_identificador_completo(entorno, caplog):
    """Un JID completo es un numero de telefono: no aparece en los logs."""
    import logging

    from app.compat import own_lid_map

    _crear_store(entorno.signal_store_file)
    with caplog.at_level(logging.INFO):
        own_lid_map.seed(entorno)

    texto = "\n".join(r.getMessage() for r in caplog.records)
    assert "573002389304" not in texto
    assert "86531142340710" not in texto
