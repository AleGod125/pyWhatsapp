"""Un ratchet LID propio que no abre nada se retira. La del número, jamás.

LO QUE SE MIDIO
---------------
Con la guarda de sesión propia ya puesta (17:25:15), las copias del teléfono
seguían fallando el MAC, y el registro LID no se movía::

    86531142340710:0@lid    529 B   fp=21003e28f9cc
    86531142340710:0@lid    529 B   fp=21003e28f9cc     <- minutos después

No podía moverse: ``ratchet_decrypt`` lanza antes de guardar. Era un registro
heredado de la migración de las 16:38, anterior a la guarda — que impide que
vuelva a pasar, pero no cura el que ya estaba roto.

El acuse de reintento sí salía. El teléfono no reenviaba: desde su lado la
sesión estaba bien, y no tiene forma de saber que la nuestra no.

LO QUE PROTEGEN ESTAS PRUEBAS
-----------------------------
* que se retire SOLO el registro LID, y sólo tras evidencia repetida;
* que la sesión por número **nunca** se toque — borrarla fue el fallo anterior;
* que un mensaje que no pasa el MAC no se entregue igualmente;
* que no se entre en un bucle de retirar y reintentar.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.compat import own_lid_recovery as recuperacion
from app.compat import self_session_guard as guarda

MI_PN = "573002389304"
MI_LID = "86531142340710"


class _JID:
    def __init__(self, user: str, server: str, device: int = 0):
        self.user = user
        self.server = server
        self.device = device


class _Almacen:
    """El almacén de sesiones, con lo justo: cargar, guardar y borrar."""

    def __init__(self, inicial: dict[str, bytes] | None = None):
        self.datos = dict(inicial or {})
        self.borrados: list[str] = []

    def load(self, sid: str):
        return self.datos.get(sid)

    def save(self, sid: str, estado) -> None:
        self.datos[sid] = estado

    def delete(self, sid: str) -> None:
        self.borrados.append(sid)
        self.datos.pop(sid, None)


class _Receptor:
    def __init__(self, sesiones: _Almacen):
        self._sessions = sesiones


PN_SID = f"{MI_PN}:0@s.whatsapp.net"
LID_SID = f"{MI_LID}:0@lid"


@pytest.fixture(autouse=True)
def identidad():
    antes = (guarda._own_pn_user, guarda._own_lid_user)
    guarda._own_pn_user, guarda._own_lid_user = MI_PN, MI_LID
    recuperacion.reiniciar()
    recuperacion.METRICAS.__init__()  # type: ignore[misc]
    yield
    guarda._own_pn_user, guarda._own_lid_user = antes
    recuperacion.reiniciar()


def _almacen() -> _Almacen:
    return _Almacen({PN_SID: b"RATCHET-PN-VIVO", LID_SID: b"RATCHET-LID-RANCIO"})


def _fallar_mac(veces: int, almacen: _Almacen, *, sid: str = LID_SID) -> None:
    """Simula `veces` fallos de MAC contra el registro que haya ahora."""
    receptor = _Receptor(almacen)
    for _ in range(veces):
        estado = almacen.load(sid)
        recuperacion._quiza_retirar(receptor, sid, recuperacion._huella(estado))


# ---------------------------------------------------------------------------
# La retirada
# ---------------------------------------------------------------------------


def test_un_solo_fallo_no_retira_nada():
    """Un mensaje fuera de orden no justifica rehacer el saludo."""
    almacen = _almacen()
    _fallar_mac(1, almacen)
    assert almacen.borrados == []
    assert LID_SID in almacen.datos


def test_dos_fallos_contra_el_mismo_registro_lo_retiran():
    almacen = _almacen()
    _fallar_mac(2, almacen)
    assert almacen.borrados == [LID_SID]
    assert LID_SID not in almacen.datos


def test_LA_SESION_POR_NUMERO_NO_SE_TOCA_NUNCA():
    """LA REGLA QUE MAS IMPORTA.

    Borrar la sesion por numero fue exactamente la causa del fallo anterior:
    es la que usa la excavacion para pedirle historial al telefono.
    """
    almacen = _almacen()
    _fallar_mac(10, almacen)
    assert PN_SID not in almacen.borrados
    assert almacen.datos[PN_SID] == b"RATCHET-PN-VIVO"


def test_no_se_retira_dos_veces_el_mismo_registro():
    almacen = _almacen()
    _fallar_mac(6, almacen)
    assert almacen.borrados == [LID_SID]


def test_un_registro_nuevo_empieza_la_cuenta_de_cero():
    """Otro ratchet merece sus propios intentos, no hereda los fallos."""
    almacen = _almacen()
    _fallar_mac(1, almacen)

    almacen.datos[LID_SID] = b"RATCHET-LID-NUEVO"
    _fallar_mac(1, almacen)  # primero contra el nuevo: no basta
    assert almacen.borrados == []

    _fallar_mac(1, almacen)  # segundo contra el nuevo: ahora si
    assert almacen.borrados == [LID_SID]


def test_hay_un_tope_de_retiradas_para_no_entrar_en_bucle():
    """Si el problema no es este, retirar sin fin solo haria ruido."""
    for _ in range(recuperacion.MAXIMO_DE_RETIRADAS + 3):
        almacen = _almacen()
        _fallar_mac(2, almacen)
        recuperacion._cuentas.clear()  # como si fuera otro registro cada vez
    assert (
        recuperacion.METRICAS.own_lid_sesiones_retiradas
        == recuperacion.MAXIMO_DE_RETIRADAS
    )


def test_si_el_borrado_falla_no_se_propaga():
    class _Roto(_Almacen):
        def delete(self, sid):  # noqa: ANN001
            raise RuntimeError("disco ocupado")

    almacen = _Roto({LID_SID: b"X"})
    _fallar_mac(2, almacen)  # no lanza
    assert recuperacion.METRICAS.own_lid_sesiones_retiradas == 0


# ---------------------------------------------------------------------------
# El camino completo, sobre el receptor de verdad (§20, §25)
# ---------------------------------------------------------------------------


class _Ajustes:
    def __init__(self, tmp_path):
        import json

        self.session_file = tmp_path / "device.json"
        self.session_file.write_text(
            json.dumps(
                {
                    "jid": {"user": MI_PN, "server": "s.whatsapp.net"},
                    "lid": f"{MI_LID}.6@lid",
                }
            ),
            encoding="utf-8",
        )


@pytest.fixture
def receptor_parcheado(tmp_path):
    """Instala la capa sobre el receptor real y la retira al terminar."""
    import pywhats.messaging.receiver as receiver_module

    antes = receiver_module.Receiver._decrypt_enc
    antes_migrar = receiver_module.Receiver._migrate_known_lid_sender
    estado = {"fallo": "mac check failed", "llamadas": []}

    def _base(self, sender, enc_type, ciphertext):  # noqa: ANN001, ANN202
        estado["llamadas"].append((sender.server, enc_type))
        if estado["fallo"]:
            raise ValueError(estado["fallo"])
        return b"texto-claro"

    receiver_module.Receiver._decrypt_enc = _base
    try:
        assert recuperacion.apply(_Ajustes(tmp_path)) is True
        yield receiver_module.Receiver, estado
    finally:
        receiver_module.Receiver._decrypt_enc = antes
        receiver_module.Receiver._migrate_known_lid_sender = antes_migrar


def test_el_mensaje_que_falla_el_MAC_no_se_entrega(receptor_parcheado):
    """No se negocia: sin autenticar, no pasa."""
    Receptor, _ = receptor_parcheado
    receptor = _Receptor(_almacen())
    with pytest.raises(ValueError, match="mac check failed"):
        Receptor._decrypt_enc(receptor, _JID(MI_LID, "lid"), "msg", b"cif")


def test_dos_copias_propias_fallidas_retiran_el_registro(receptor_parcheado):
    Receptor, _ = receptor_parcheado
    almacen = _almacen()
    receptor = _Receptor(almacen)
    for _ in range(2):
        with pytest.raises(ValueError):
            Receptor._decrypt_enc(receptor, _JID(MI_LID, "lid"), "msg", b"cif")

    assert almacen.borrados == [LID_SID]
    assert almacen.datos[PN_SID] == b"RATCHET-PN-VIVO"
    assert recuperacion.METRICAS.own_msg_mac_fallido == 2


def test_un_fallo_por_el_NUMERO_no_retira_el_LID(receptor_parcheado):
    """Sólo el camino LID puede retirar el registro LID."""
    Receptor, _ = receptor_parcheado
    almacen = _almacen()
    receptor = _Receptor(almacen)
    for _ in range(4):
        with pytest.raises(ValueError):
            Receptor._decrypt_enc(
                receptor, _JID(MI_PN, "s.whatsapp.net"), "msg", b"cif"
            )
    assert almacen.borrados == []


def test_un_tercero_ni_se_cuenta_ni_se_toca(receptor_parcheado):
    Receptor, _ = receptor_parcheado
    almacen = _almacen()
    receptor = _Receptor(almacen)
    for _ in range(4):
        with pytest.raises(ValueError):
            Receptor._decrypt_enc(receptor, _JID("64940106866902", "lid"), "msg", b"c")
    assert almacen.borrados == []
    assert recuperacion.METRICAS.own_msg_recibidos == 0


def test_un_pkmsg_que_descifra_cuenta_como_sesion_nueva(receptor_parcheado):
    """El final del camino: el reenvio establece la sesion y el mensaje entra."""
    Receptor, estado = receptor_parcheado
    almacen = _Almacen({PN_SID: b"RATCHET-PN-VIVO"})  # sin LID: ya se retiro
    receptor = _Receptor(almacen)

    estado["fallo"] = None
    assert Receptor._decrypt_enc(
        receptor, _JID(MI_LID, "lid"), "pkmsg", b"cif"
    ) == b"texto-claro"

    assert recuperacion.METRICAS.own_pkmsg_entrantes == 1
    assert recuperacion.METRICAS.own_lid_sesiones_creadas == 1
    assert recuperacion.METRICAS.own_msg_descifrados == 1


def test_coexistencia_tras_recuperar(receptor_parcheado):
    """§26: por número y por LID a la vez, y las dos correctas."""
    Receptor, estado = receptor_parcheado
    almacen = _almacen()
    receptor = _Receptor(almacen)

    for _ in range(2):
        with pytest.raises(ValueError):
            Receptor._decrypt_enc(receptor, _JID(MI_LID, "lid"), "msg", b"cif")
    assert LID_SID not in almacen.datos

    # Llega el reenvio y el camino original guarda la sesion nueva.
    estado["fallo"] = None

    def _con_guardado(self, sender, enc_type, ciphertext):  # noqa: ANN001, ANN202
        from pywhats.messaging.addressing import session_id

        self._sessions.save(session_id(sender), b"RATCHET-LID-NUEVO")
        return b"texto-claro"

    import pywhats.messaging.receiver as receiver_module

    envuelto = receiver_module.Receiver._decrypt_enc
    receiver_module.Receiver.__dict__  # noqa: B018 - solo para claridad
    # Se sustituye la BASE, conservando la envoltura instalada.
    recuperacion.reiniciar()
    receiver_module.Receiver._decrypt_enc = _con_guardado
    recuperacion.apply(_Ajustes(pathlib.Path(str(almacen.datos and "."))))
    try:
        Receptor._decrypt_enc(receptor, _JID(MI_LID, "lid"), "pkmsg", b"cif")
    finally:
        receiver_module.Receiver._decrypt_enc = envuelto

    assert almacen.datos[PN_SID] == b"RATCHET-PN-VIVO"
    assert almacen.datos[LID_SID] == b"RATCHET-LID-NUEVO"


# ---------------------------------------------------------------------------
# Guardias de código (§9)
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


def test_no_se_copia_ni_se_fusiona_ningun_ratchet():
    """Retirar no es mover. No hay un solo `save` de sesion en esta capa."""
    codigo = ast.unparse(_sin_docstrings("app/compat/own_lid_recovery.py")).lower()
    for prohibido in (
        "_sessions.save",
        "sessions.save",
        "identity_store.save",
        "migrate_pn_session_to_lid",
        "x3dh",
        "ratchet_init",
        "verify_mac",
    ):
        assert prohibido not in codigo, f"prohibido: {prohibido}"


def test_lo_unico_que_se_borra_es_una_sesion():
    """Ni identidades, ni prekeys, ni el mapa. Un registro de sesion y ya."""
    codigo = ast.unparse(_sin_docstrings("app/compat/own_lid_recovery.py")).lower()
    assert "_sessions.delete" in codigo
    for prohibido in ("identity_store.delete", "prekey", "lid_map"):
        assert prohibido not in codigo, f"prohibido borrar/tocar: {prohibido}"


def test_el_texto_claro_nunca_se_devuelve_tras_un_fallo():
    """No puede haber un `return` que se trague la excepcion y siga.

    Se comprueba que todo `except` de la envoltura termina en `raise`.
    """
    arbol = _sin_docstrings("app/compat/own_lid_recovery.py")
    funcion = next(
        n
        for n in ast.walk(arbol)
        if isinstance(n, ast.FunctionDef) and n.name == "_decrypt_enc"
    )
    manejadores = [n for n in ast.walk(funcion) if isinstance(n, ast.ExceptHandler)]
    assert manejadores, "la envoltura tiene que clasificar el fallo"

    # El invariante: ningun manejador puede DEVOLVER. Da igual cuantos haya
    # --alguno solo protege una lectura de estado--; lo que no puede pasar es
    # que uno de ellos entregue un texto claro que no supero su verificacion.
    for manejador in manejadores:
        devuelve = [
            n
            for n in ast.walk(ast.Module(body=manejador.body, type_ignores=[]))
            if isinstance(n, ast.Return) and n.value is not None
        ]
        assert not devuelve, "un except que devuelve entregaria algo sin autenticar"

    # Y el que envuelve al descifrado original tiene que relanzar.
    assert any(
        isinstance(n, ast.Raise)
        for m in manejadores
        for n in ast.walk(ast.Module(body=m.body, type_ignores=[]))
    ), "el fallo del descifrado tiene que relanzarse"
