"""Los mensajes que el usuario escribe desde su propio teléfono.

EL FALLO, MEDIDO SOBRE UNA VINCULACION LIMPIA
---------------------------------------------
Un mensaje escrito desde el teléfono llega al companion dirigido a **nuestro
propio LID**::

    receiver: decrypt failed id=AC6065... from=8653...@lid type=msg:
              no session for peer 8653...@lid

Y la sesión con ese aparato SÍ existe: está guardada bajo la otra dirección,
la del número::

    sessions:  573002389304:0@s.whatsapp.net    <- nuestro telefono

Es el mismo aparato con dos identificadores. ``pywhats`` sabe resolverlo
—``_migrate_known_lid_sender`` consulta el ``lid_map``— pero ese mapa se
aprende de los atributos de los stanzas entrantes, y el par propio no llega
nunca por ahí. Hay que sembrarlo.

POR QUE NO SE SEMBRABA
----------------------
Dos motivos encadenados, y hacían falta los dos para que fallara:

1. **La rama que sembraba era código muerto.** Había dos ramas
   ``elif nombre == "paired"`` en la misma cadena ``if/elif``; la primera
   ganaba siempre, así que la segunda —la que sembraba— no corría nunca.

2. **Y aunque hubiera corrido, habría sido pronto.** El ``pair-success`` no
   trae el LID: lo trae el ``<success>`` posterior. En el pairing limpio de
   las 15:45 la siembra falló a las 15:45:17 y el LID llegó a las 15:45:18.

Estas pruebas fijan las dos cosas.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sqlite3

import pytest

from app.compat import own_lid_map

MI_PN = "573002389304"
MI_LID = "86531142340710"


# ---------------------------------------------------------------------------
# Un Signal Store de mentira, con la misma forma que el real
# ---------------------------------------------------------------------------


def _store(tmp_path: pathlib.Path, *, con_lid_map: bool = True) -> pathlib.Path:
    ruta = tmp_path / "device.json.signal.db"
    conexion = sqlite3.connect(ruta)
    conexion.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, state BLOB)")
    if con_lid_map:
        conexion.execute("CREATE TABLE lid_map (pn_user TEXT, lid_user TEXT)")
    conexion.commit()
    conexion.close()
    return ruta


class _Ajustes:
    def __init__(self, tmp_path: pathlib.Path, *, lid_en_disco: str | None):
        self.session_file = tmp_path / "device.json"
        cuerpo: dict = {"jid": {"user": MI_PN, "server": "s.whatsapp.net"}}
        if lid_en_disco is not None:
            cuerpo["lid"] = lid_en_disco
        self.session_file.write_text(json.dumps(cuerpo), encoding="utf-8")
        self.signal_store_file = _store(tmp_path)
        self.compat_own_lid_map = True


def _pares(store: pathlib.Path) -> list[tuple[str, str]]:
    conexion = sqlite3.connect(store)
    try:
        return list(conexion.execute("SELECT pn_user, lid_user FROM lid_map"))
    finally:
        conexion.close()


# ---------------------------------------------------------------------------
# La siembra
# ---------------------------------------------------------------------------


def test_siembra_el_par_propio_cuando_el_disco_lo_tiene(tmp_path):
    ajustes = _Ajustes(tmp_path, lid_en_disco=f"{MI_LID}.6@lid")
    assert own_lid_map.seed(ajustes) is True
    assert _pares(ajustes.signal_store_file) == [(MI_PN, MI_LID)]


def test_el_sufijo_de_dispositivo_no_entra_en_el_mapa(tmp_path):
    """El mapa va por usuario. ``.6@lid`` es NUESTRO device, no parte del id."""
    ajustes = _Ajustes(tmp_path, lid_en_disco=f"{MI_LID}.6@lid")
    own_lid_map.seed(ajustes)
    (_, lid_guardado), = _pares(ajustes.signal_store_file)
    assert lid_guardado == MI_LID


def test_sin_lid_en_disco_no_se_siembra_nada(tmp_path):
    """EL FALLO. Es el estado exacto del pairing limpio a las 15:45:17."""
    ajustes = _Ajustes(tmp_path, lid_en_disco=None)
    assert own_lid_map.seed(ajustes) is False
    assert _pares(ajustes.signal_store_file) == []


def test_el_lid_del_dispositivo_vivo_rescata_la_siembra(tmp_path):
    """EL ARREGLO.

    El ``<success>`` trae el LID un segundo antes de que llegue al disco. El
    dispositivo vivo ya lo sabe, asi que se le pregunta a el.
    """
    ajustes = _Ajustes(tmp_path, lid_en_disco=None)
    assert own_lid_map.seed(ajustes, lid_hint=f"{MI_LID}.6@lid") is True
    assert _pares(ajustes.signal_store_file) == [(MI_PN, MI_LID)]


def test_el_indicio_no_pisa_lo_que_ya_hay_en_disco(tmp_path):
    """El indicio RELLENA un hueco; nunca sustituye a lo persistido."""
    ajustes = _Ajustes(tmp_path, lid_en_disco=f"{MI_LID}.6@lid")
    own_lid_map.seed(ajustes, lid_hint="99999999999999@lid")
    assert _pares(ajustes.signal_store_file) == [(MI_PN, MI_LID)]


def test_un_indicio_vacio_no_inventa_nada(tmp_path):
    ajustes = _Ajustes(tmp_path, lid_en_disco=None)
    assert own_lid_map.seed(ajustes, lid_hint=None) is False
    assert own_lid_map.seed(ajustes, lid_hint="") is False
    assert _pares(ajustes.signal_store_file) == []


def test_sembrar_dos_veces_deja_una_sola_fila(tmp_path):
    """Se llama en cada ``<success>``, incluido cada reconexion."""
    ajustes = _Ajustes(tmp_path, lid_en_disco=f"{MI_LID}.6@lid")
    assert own_lid_map.seed(ajustes) is True
    assert own_lid_map.seed(ajustes) is True
    assert _pares(ajustes.signal_store_file) == [(MI_PN, MI_LID)]


def test_sin_signal_store_no_revienta(tmp_path):
    """Un pairing nuevo llega aqui antes de que exista el store."""
    ajustes = _Ajustes(tmp_path, lid_en_disco=f"{MI_LID}.6@lid")
    ajustes.signal_store_file.unlink()
    assert own_lid_map.seed(ajustes) is False


def test_la_verificacion_exige_que_resuelva_de_verdad(tmp_path):
    """Escribir la fila no basta: lo que importa es que LID -> PN resuelva.

    Es lo que hara ``_migrate_known_lid_sender`` cuando llegue el mensaje.
    """
    ajustes = _Ajustes(tmp_path, lid_en_disco=f"{MI_LID}.6@lid")
    own_lid_map.seed(ajustes)
    assert own_lid_map.verify(ajustes.signal_store_file, MI_PN, MI_LID) is True
    assert own_lid_map.verify(ajustes.signal_store_file, MI_PN, "00000000") is False


# ---------------------------------------------------------------------------
# El despachador: que la siembra corra, y en el momento correcto
# ---------------------------------------------------------------------------


def _ramas_del_despachador() -> dict[str, list[str]]:
    """Qué eventos maneja ``_observar_evento`` y qué llama cada rama.

    Se lee el AST y no el texto: un comentario que mencione ``paired`` no
    puede hacer pasar ni fallar esta prueba.
    """
    fuente = pathlib.Path("app/core/runtime.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    despachador = next(
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.FunctionDef) and nodo.name == "_observar_evento"
    )

    ramas: dict[str, list[str]] = {}
    pendientes = [n for n in despachador.body if isinstance(n, ast.If)]
    while pendientes:
        rama = pendientes.pop(0)
        prueba = rama.test
        nombre = None
        if (
            isinstance(prueba, ast.Compare)
            and isinstance(prueba.comparators[0], ast.Constant)
            and isinstance(prueba.comparators[0].value, str)
        ):
            nombre = prueba.comparators[0].value
        if nombre is not None:
            llamadas = [
                n.func.attr
                for n in ast.walk(ast.Module(body=rama.body, type_ignores=[]))
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            ]
            ramas.setdefault(nombre, []).extend(llamadas)
        pendientes.extend(n for n in rama.orelse if isinstance(n, ast.If))
    return ramas


def test_ningun_evento_tiene_dos_ramas():
    """LA CAUSA DEL FALLO.

    Habia dos ``elif nombre == "paired"``. La cadena entra por la primera y la
    segunda no se ejecuta jamas, asi que la siembra del par propio era codigo
    muerto y nadie lo notaba: no hay error, simplemente no pasa nada.
    """
    fuente = pathlib.Path("app/core/runtime.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    despachador = next(
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.FunctionDef) and nodo.name == "_observar_evento"
    )

    vistos: list[str] = []
    pendientes = [n for n in despachador.body if isinstance(n, ast.If)]
    while pendientes:
        rama = pendientes.pop(0)
        prueba = rama.test
        if (
            isinstance(prueba, ast.Compare)
            and isinstance(prueba.comparators[0], ast.Constant)
            and isinstance(prueba.comparators[0].value, str)
        ):
            vistos.append(prueba.comparators[0].value)
        pendientes.extend(n for n in rama.orelse if isinstance(n, ast.If))

    repetidos = {n for n in vistos if vistos.count(n) > 1}
    assert not repetidos, f"ramas duplicadas e inalcanzables: {sorted(repetidos)}"


def test_el_par_propio_se_siembra_con_el_success():
    """Y no antes.

    ``pair-success`` cierra la vinculacion pero NO trae el LID; lo trae el
    ``<success>``. Sembrar en ``paired`` es sembrar un segundo demasiado
    pronto, que es exactamente lo que se midio.
    """
    ramas = _ramas_del_despachador()
    assert "_sembrar_lid_propio" in ramas.get("session_valid", [])
    assert "_sembrar_lid_propio" not in ramas.get("paired", [])


# ---------------------------------------------------------------------------
# Nada de atajos criptograficos (§77)
# ---------------------------------------------------------------------------


def test_la_siembra_no_toca_ni_una_sesion(tmp_path):
    """Escribe una correspondencia entre identificadores. Nada mas.

    No crea sesiones, no copia ratchets, no deriva claves y no salta ninguna
    verificacion. Si esta prueba falla, alguien convirtio un apano de
    direcciones en un atajo de criptografia.
    """
    ajustes = _Ajustes(tmp_path, lid_en_disco=f"{MI_LID}.6@lid")
    conexion = sqlite3.connect(ajustes.signal_store_file)
    conexion.execute(
        "INSERT INTO sessions VALUES (?, ?)", (f"{MI_PN}:0@s.whatsapp.net", b"RATCHET")
    )
    conexion.commit()
    conexion.close()

    own_lid_map.seed(ajustes)

    conexion = sqlite3.connect(ajustes.signal_store_file)
    try:
        sesiones = list(conexion.execute("SELECT session_id, state FROM sessions"))
    finally:
        conexion.close()
    # La sesion del LID NO se crea aqui. Eso lo hace pywhats migrando, o el
    # propio pkmsg; inventarla seria fabricar estado criptografico.
    assert sesiones == [(f"{MI_PN}:0@s.whatsapp.net", b"RATCHET")]


def test_el_modulo_no_escribe_en_tablas_de_criptografia():
    """Guardia de codigo: solo puede tocar ``lid_map``."""
    fuente = pathlib.Path("app/compat/own_lid_map.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    # Se quitan los docstrings: explican el fallo citando otras tablas.
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.FunctionDef, ast.ClassDef, ast.Module)):
            cuerpo = getattr(nodo, "body", [])
            if (
                cuerpo
                and isinstance(cuerpo[0], ast.Expr)
                and isinstance(cuerpo[0].value, ast.Constant)
                and isinstance(cuerpo[0].value.value, str)
            ):
                cuerpo[0].value.value = ""

    sql = " ".join(
        n.value.lower()
        for n in ast.walk(arbol)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )
    for prohibida in ("insert into sessions", "update sessions", "delete from sessions",
                      "insert into identities", "update identities",
                      "insert into prekeys", "delete from prekeys"):
        assert prohibida not in sql, f"escritura prohibida: {prohibida}"


@pytest.mark.parametrize("crudo, esperado", [
    (f"{MI_LID}.6@lid", MI_LID),
    (f"{MI_LID}@lid", MI_LID),
    (f"{MI_LID}:0@lid", MI_LID),
    (MI_LID, MI_LID),
    ("", None),
    (None, None),
])
def test_el_usuario_se_extrae_igual_de_todas_las_formas(crudo, esperado):
    assert own_lid_map._user(crudo) == esperado
