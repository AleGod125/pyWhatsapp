"""El acuse de reintento moderno: con qué rehacer el saludo, y nada más.

LO QUE SE MIDIO
---------------
Con la sesión LID ya retirada, la copia propia deja de fallar el MAC y falla
por «no hay sesión», que es el estado correcto. El acuse sale::

    17:47:53.861  no session for peer <mi LID>   ->  sent retry receipt
    17:47:54.114  no session (MISMO WAMID)       ->  sent retry receipt

Y el teléfono no reenvía nada como ``pkmsg``. Coherente: el acuse que
mandábamos era el mínimo —``<retry>`` y ``<registration>``— y no llevaba con
qué rehacer el saludo.

Dos cosas que arreglar, y las dos están aquí:

1. **dos acuses del mismo mensaje en 253 ms.** Uno por mensaje.
2. **el acuse no lleva material.** Desde el segundo intento, ``<keys>``.

LO QUE PROTEGEN ESTAS PRUEBAS
-----------------------------
Sobre todo lo segundo, campo por campo: que viaje sólo material público, con
la forma verificada contra ``pywhats/messaging/prekey.py`` de este mismo
repositorio, y que **no viaje ni un byte privado**.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.compat import retry_keys

IDENTIDAD_PUBLICA = bytes(range(32))
SPK_PUBLICA = bytes(range(32, 64))
SPK_FIRMA = bytes(range(64, 128))
OPK_PUBLICA = bytes(range(128, 160))
ADV = b"ADV-DEVICE-IDENTITY-FIRMADA"

#: Lo que NUNCA puede salir. Se busca literalmente en el cable.
IDENTIDAD_PRIVADA = b"PRIVADA-IDENTIDAD-NO-DEBE-SALIR-"
SPK_PRIVADA = b"PRIVADA-PREFIRMADA-NO-DEBE-SALIR"
OPK_PRIVADA = b"PRIVADA-UNSOLOUSO-NO-DEBE-SALIR."


def _material(**cambios) -> retry_keys.MaterialPublico:
    base = dict(
        registration_id=123456,
        identity_public=IDENTIDAD_PUBLICA,
        spk_id=7,
        spk_public=SPK_PUBLICA,
        spk_signature=SPK_FIRMA,
        opk_id=42,
        opk_public=OPK_PUBLICA,
        device_identity=ADV,
    )
    base.update(cambios)
    return retry_keys.MaterialPublico(**base)


def _hijos(nodo) -> dict:
    """Los nodos hijos por etiqueta. Sirve para leer la forma sin adivinar."""
    salida: dict[str, list] = {}
    for hijo in nodo.content or []:
        salida.setdefault(hijo.tag, []).append(hijo)
    return salida


# ---------------------------------------------------------------------------
# La forma del bloque
# ---------------------------------------------------------------------------


def test_el_bloque_tiene_la_forma_de_prekey_to_node():
    bloque = retry_keys.bloque_de_claves(_material())
    assert bloque.tag == "keys"
    hijos = _hijos(bloque)
    assert set(hijos) == {"type", "identity", "key", "skey", "device-identity"}


def test_el_byte_de_curva_va_SOLO_en_su_nodo():
    """EL ERROR FACIL DE ESTE BLOQUE.

    El 0x05 no se pega a los valores: va en su propio `<type>`. Sale asi del
    nodo de subida de este mismo repositorio, verificado, no supuesto.
    """
    hijos = _hijos(retry_keys.bloque_de_claves(_material()))
    assert hijos["type"][0].content == b"\x05"
    # Y los valores van CRUDOS, de 32 bytes, sin prefijo.
    assert hijos["identity"][0].content == IDENTIDAD_PUBLICA
    assert len(hijos["identity"][0].content) == 32
    valor = _hijos(hijos["skey"][0])["value"][0].content
    assert valor == SPK_PUBLICA and valor[0] != 0x05


def test_los_identificadores_van_en_tres_bytes():
    """Los bajos de un id de cuatro, como ``preKeyToNode``."""
    hijos = _hijos(retry_keys.bloque_de_claves(_material()))
    assert _hijos(hijos["key"][0])["id"][0].content == b"\x00\x00\x2a"  # 42
    assert _hijos(hijos["skey"][0])["id"][0].content == b"\x00\x00\x07"


def test_la_prefirmada_lleva_su_firma():
    skey = _hijos(retry_keys.bloque_de_claves(_material()))["skey"][0]
    firma = _hijos(skey)["signature"][0].content
    assert firma == SPK_FIRMA
    assert len(firma) == 64


def test_sin_clave_de_un_solo_uso_el_bloque_sigue_valiendo():
    """Es opcional: la prefirmada basta para el saludo."""
    hijos = _hijos(retry_keys.bloque_de_claves(_material(opk_id=None, opk_public=None)))
    assert "key" not in hijos
    assert "skey" in hijos and "identity" in hijos


def test_sin_identidad_de_dispositivo_no_se_inventa():
    hijos = _hijos(retry_keys.bloque_de_claves(_material(device_identity=None)))
    assert "device-identity" not in hijos


# ---------------------------------------------------------------------------
# Lo que NO puede salir (§9)
# ---------------------------------------------------------------------------


def _todo_el_contenido(nodo) -> bytes:
    trozos = []
    if isinstance(nodo.content, (bytes, bytearray)):
        trozos.append(bytes(nodo.content))
    elif nodo.content:
        for hijo in nodo.content:
            trozos.append(_todo_el_contenido(hijo))
    return b"".join(trozos)


def test_NINGUNA_clave_privada_llega_al_cable():
    """LA REGLA QUE NO SE NEGOCIA."""
    bloque = retry_keys.bloque_de_claves(_material())
    cable = _todo_el_contenido(bloque)
    for privada in (IDENTIDAD_PRIVADA, SPK_PRIVADA, OPK_PRIVADA):
        assert privada not in cable


def test_el_material_completo_exige_lo_imprescindible():
    assert _material().completo() is True
    assert _material(identity_public=b"").completo() is False
    assert _material(spk_public=b"").completo() is False
    assert _material(spk_signature=b"").completo() is False
    assert _material(registration_id=0).completo() is False


def test_sin_material_completo_no_se_construye_nada(monkeypatch, tmp_path):
    """Un bloque a medias es peor que ninguno: se devuelve None."""

    class _Ajustes:
        session_file = tmp_path / "no-existe.json"
        signal_store_file = tmp_path / "no-existe.db"

    assert retry_keys.material_de(_Ajustes()) is None


# ---------------------------------------------------------------------------
# Cuándo se adjunta (§13)
# ---------------------------------------------------------------------------


def test_se_adjunta_desde_el_segundo_intento():
    """Lo que hacen Baileys y whatsmeow: el primero va simple."""
    assert retry_keys.INTENTO_DESDE_EL_QUE_SE_ADJUNTA == 2


# ---------------------------------------------------------------------------
# La clave de un solo uso sale del almacén, no se genera
# ---------------------------------------------------------------------------


def test_la_clave_de_un_solo_uso_sale_del_almacen(tmp_path):
    import sqlite3

    ruta = tmp_path / "signal.db"
    conexion = sqlite3.connect(ruta)
    conexion.execute("CREATE TABLE prekeys (key_id INTEGER, private BLOB, public BLOB)")
    conexion.execute("INSERT INTO prekeys VALUES (?,?,?)", (9, OPK_PRIVADA, OPK_PUBLICA))
    conexion.commit()
    conexion.close()

    encontrada = retry_keys._una_clave_de_un_solo_uso(ruta)
    assert encontrada == (9, OPK_PUBLICA)


def test_sin_claves_de_un_solo_uso_no_se_inventa_ninguna(tmp_path):
    import sqlite3

    ruta = tmp_path / "signal.db"
    conexion = sqlite3.connect(ruta)
    conexion.execute("CREATE TABLE prekeys (key_id INTEGER, private BLOB, public BLOB)")
    conexion.commit()
    conexion.close()
    assert retry_keys._una_clave_de_un_solo_uso(ruta) is None


# ---------------------------------------------------------------------------
# Un acuse por mensaje (§5, §6)
# ---------------------------------------------------------------------------


class _Transporte:
    def __init__(self):
        self.enviados = []

    async def send(self, marco):  # noqa: ANN001
        self.enviados.append(marco)


class _Receptor:
    def __init__(self):
        self._transport = _Transporte()

        class _Id:
            registration_id = 123456

        self._identity = _Id()


class _Nodo:
    def __init__(self, wamid: str):
        self.attrs = {"id": wamid, "t": "1760000000"}

    def get_str(self, clave):  # noqa: ANN001
        return self.attrs.get(clave, "")


@pytest.fixture
def observador():
    from app.compat import retry_observer

    retry_observer.reiniciar()
    yield retry_observer
    retry_observer.reiniciar()


def test_dos_acuses_seguidos_del_mismo_mensaje_son_uno(observador):
    """Se midio: el mismo WAMID recibio dos en 253 ms."""
    import asyncio

    import pywhats.messaging.receiver as receiver_module

    antes = receiver_module.Receiver._send_retry_receipt
    llamadas = []

    async def _base(self, node, *, sender):  # noqa: ANN001, ANN202
        llamadas.append(node.get_str("id"))

    receiver_module.Receiver._send_retry_receipt = _base
    try:
        assert observador.apply(tracker=None) is True
        receptor = _Receptor()
        nodo = _Nodo("WAMID-REPETIDO")

        async def _dos_veces():
            await receiver_module.Receiver._send_retry_receipt(
                receptor, nodo, sender="x@lid"
            )
            await receiver_module.Receiver._send_retry_receipt(
                receptor, nodo, sender="x@lid"
            )

        asyncio.run(_dos_veces())
        assert len(llamadas) == 1
    finally:
        receiver_module.Receiver._send_retry_receipt = antes


def test_mensajes_distintos_reciben_su_propio_acuse(observador):
    import asyncio

    import pywhats.messaging.receiver as receiver_module

    antes = receiver_module.Receiver._send_retry_receipt
    llamadas = []

    async def _base(self, node, *, sender):  # noqa: ANN001, ANN202
        llamadas.append(node.get_str("id"))

    receiver_module.Receiver._send_retry_receipt = _base
    try:
        assert observador.apply(tracker=None) is True
        receptor = _Receptor()

        async def _dos_mensajes():
            for wamid in ("UNO", "OTRO"):
                await receiver_module.Receiver._send_retry_receipt(
                    receptor, _Nodo(wamid), sender="x@lid"
                )

        asyncio.run(_dos_mensajes())
        assert llamadas == ["UNO", "OTRO"]
    finally:
        receiver_module.Receiver._send_retry_receipt = antes


# ---------------------------------------------------------------------------
# Guardias de código (§24)
# ---------------------------------------------------------------------------


def _sin_docstrings(ruta: str) -> ast.Module:
    arbol = ast.parse(pathlib.Path(ruta).read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        cuerpo = getattr(nodo, "body", None)
        if (
            isinstance(cuerpo, list)
            and cuerpo
            and isinstance(cuerpo[0], ast.Expr)
            and isinstance(cuerpo[0].value, ast.Constant)
            and isinstance(cuerpo[0].value.value, str)
        ):
            cuerpo[0].value.value = ""
    return arbol


def test_el_modulo_no_lee_ni_una_clave_privada():
    """Ni identidad, ni prefirmada, ni de un solo uso. Sólo las públicas."""
    codigo = ast.unparse(_sin_docstrings("app/compat/retry_keys.py"))
    for prohibido in (
        "identity_private",
        "signed_pre_key_private",
        "private",
    ):
        # `private` aparece en la consulta SQL como NOMBRE DE COLUMNA que NO
        # se selecciona; se comprueba que no se lee en ningun sitio.
        assert f".{prohibido}" not in codigo, f"prohibido leer {prohibido}"
    assert "SELECT key_id, public FROM prekeys" in codigo


def test_el_modulo_no_genera_material_criptografico():
    """No fabrica claves ni identificadores: usa los que ya existen."""
    codigo = ast.unparse(_sin_docstrings("app/compat/retry_keys.py"))
    for prohibido in ("generate_pre_key", "urandom", "secrets", "random", "x3dh"):
        assert prohibido not in codigo, f"prohibido: {prohibido}"


def test_el_modulo_no_escribe_en_el_signal_store():
    """Sólo lee, y en modo lectura explicito."""
    codigo = ast.unparse(_sin_docstrings("app/compat/retry_keys.py"))
    assert "mode=ro" in codigo
    for prohibido in ("INSERT", "UPDATE", "DELETE", "DROP", ".save(", ".delete("):
        assert prohibido not in codigo, f"prohibido: {prohibido}"
