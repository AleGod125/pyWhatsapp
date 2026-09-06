"""La sesión de nuestro propio teléfono no se mueve. Se midió por qué.

LA CADENA QUE SE ROMPE AQUI
---------------------------
Sobre la sesión real, tras sembrar el par PN↔LID propio::

    16:35:51  enc=msg    sesion_por_pn=True   sesion_por_lid=False
    16:37:56  copia propia guardada, todo bien
    16:38:06  llega una copia propia dirigida al LID -> migracion
    16:38:07  enc=pkmsg  sesion_por_pn=FALSE  sesion_por_lid=True
    16:38:08  enc=msg    sesion_por_pn=True   sesion_por_lid=True
    16:38:10  mac check failed ... y ni una copia propia mas

La migración mueve la sesión del número al LID y **borra el número**. Esa es
justo la dirección a la que van nuestras peticiones de historial, así que la
siguiente saluda de nuevo, el teléfono rehace su ratchet, y la copia siguiente
llega cifrada con uno que nuestro registro LID ya no tiene.

Para un contacto normal la migración es correcta: no le escribimos por su
número. Con el nuestro sí, y ahí está la diferencia.

LO QUE PROTEGEN ESTAS PRUEBAS
-----------------------------
* que la abstención sea SOLO para nosotros, y siga migrando a los demás;
* que no se copie, mezcle ni borre nada de Signal por el camino;
* que el sufijo de dispositivo no despiste a la comparación.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.compat import self_session_guard as guarda

MI_PN = "573002389304"
MI_LID = "86531142340710"


class _JID:
    """Lo justo de un JID de pywhats para esta decisión."""

    def __init__(self, user: str, server: str, device: int = 0):
        self.user = user
        self.server = server
        self.device = device


@pytest.fixture(autouse=True)
def identidad_propia():
    """Se fija la identidad y se restaura: el módulo tiene estado global."""
    antes = (guarda._own_pn_user, guarda._own_lid_user)
    guarda._own_pn_user, guarda._own_lid_user = MI_PN, MI_LID
    guarda.METRICAS["migraciones_evitadas"] = 0
    yield
    guarda._own_pn_user, guarda._own_lid_user = antes


# ---------------------------------------------------------------------------
# Quiénes somos
# ---------------------------------------------------------------------------


def test_nos_reconocemos_por_las_dos_direcciones():
    assert guarda.es_dispositivo_propio(_JID(MI_LID, "lid"))
    assert guarda.es_dispositivo_propio(_JID(MI_PN, "s.whatsapp.net"))


def test_el_sufijo_de_dispositivo_no_despista():
    """El ``<success>`` trae ``8653....6@lid``; la dirección Signal es ``:0``.

    Si la comparación se hiciera sobre la cadena entera, el caso que importa
    —una copia que llega del dispositivo 0— no se reconoceria como nuestra.
    """
    for crudo in (MI_LID, f"{MI_LID}.6", f"{MI_LID}:0"):
        assert guarda.es_dispositivo_propio(_JID(crudo, "lid")), crudo


def test_un_tercero_no_es_nuestro():
    assert not guarda.es_dispositivo_propio(_JID("64940106866902", "lid"))
    assert not guarda.es_dispositivo_propio(_JID("573243116421", "s.whatsapp.net"))


def test_un_remitente_sin_usuario_no_se_confunde_con_nosotros():
    assert not guarda.es_dispositivo_propio(_JID("", "lid"))
    assert not guarda.es_dispositivo_propio(object())


def test_sin_identidad_conocida_no_se_reconoce_a_nadie():
    guarda._own_pn_user = guarda._own_lid_user = None
    assert not guarda.es_dispositivo_propio(_JID(MI_LID, "lid"))


# ---------------------------------------------------------------------------
# La abstención
# ---------------------------------------------------------------------------


class _ReceptorFalso:
    """Un receptor con la firma que importa, para ver a quién se migra."""

    def __init__(self) -> None:
        self.migrados: list[str] = []

    def _migrate_known_lid_sender(self, sender) -> None:  # noqa: ANN001
        self.migrados.append(f"{sender.user}@{sender.server}")


def _envolver():
    """Aplica la MISMA envoltura que instala ``apply``, sin tocar pywhats."""
    original = _ReceptorFalso._migrate_known_lid_sender

    def envuelto(self, sender):  # noqa: ANN001, ANN202
        if guarda.es_dispositivo_propio(sender):
            guarda.METRICAS["migraciones_evitadas"] += 1
            return
        original(self, sender)

    return envuelto


def test_a_nosotros_no_se_nos_migra():
    """LA REGLA. Migrarnos borra la direccion que usa ON_DEMAND."""
    receptor = _ReceptorFalso()
    envuelto = _envolver()
    envuelto(receptor, _JID(MI_LID, "lid"))
    assert receptor.migrados == []
    assert guarda.METRICAS["migraciones_evitadas"] == 1


def test_a_los_demas_se_les_sigue_migrando():
    """La migracion es correcta para un tercero: no le escribimos por su numero."""
    receptor = _ReceptorFalso()
    envuelto = _envolver()
    envuelto(receptor, _JID("64940106866902", "lid"))
    assert receptor.migrados == ["64940106866902@lid"]
    assert guarda.METRICAS["migraciones_evitadas"] == 0


def test_la_abstencion_no_devuelve_nada_ni_lanza():
    """No puede cortar la recepcion: solo se abstiene de un movimiento."""
    receptor = _ReceptorFalso()
    envuelto = _envolver()
    assert envuelto(receptor, _JID(MI_LID, "lid")) is None


# ---------------------------------------------------------------------------
# Nada de atajos criptograficos (§84)
# ---------------------------------------------------------------------------


def _sin_docstrings(ruta: str) -> ast.Module:
    """El AST sin textos: los comentarios explican el fallo citando tablas."""
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


def test_la_guarda_no_toca_nada_de_signal():
    """Sólo se ABSTIENE. No copia, no borra, no deriva, no fusiona."""
    codigo = ast.unparse(_sin_docstrings("app/compat/self_session_guard.py")).lower()
    for prohibido in (
        "sessions.save",
        "sessions.delete",
        "sessions.load",
        "identity_store.save",
        "identity_store.delete",
        "migrate_pn_session_to_lid",
        "x3dh",
        "ratchet",
        "verify_mac",
        "serialize_state",
    ):
        assert prohibido not in codigo, f"la guarda no puede usar {prohibido}"


def test_la_guarda_no_abre_el_signal_store():
    """Ni siquiera lo lee. Su decision sale del remitente y de la identidad."""
    codigo = ast.unparse(_sin_docstrings("app/compat/self_session_guard.py")).lower()
    assert "sqlite3" not in codigo
    assert "signal_store_file" not in codigo


def test_solo_envuelve_la_migracion():
    """No puede colarse un envoltorio sobre el descifrado.

    Envolver `_decrypt_enc` desde aqui seria ponerse en el camino de la
    verificacion del MAC, que es lo unico que no se negocia.
    """
    arbol = _sin_docstrings("app/compat/self_session_guard.py")
    asignados = {
        nodo.args[1].value
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Name)
        and nodo.func.id == "setattr"
        and len(nodo.args) >= 2
        and isinstance(nodo.args[1], ast.Constant)
    }
    parcheados = {
        nodo.targets[0].attr
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Assign)
        and isinstance(nodo.targets[0], ast.Attribute)
    }
    assert "_decrypt_enc" not in parcheados
    assert parcheados <= {"_migrate_known_lid_sender"}
    # El marcador de idempotencia, y nada mas.
    assert asignados <= {guarda._MARKER}


# ---------------------------------------------------------------------------
# Instalacion
# ---------------------------------------------------------------------------


class _Ajustes:
    def __init__(self, tmp_path, *, con_lid=True):
        import json

        self.session_file = tmp_path / "device.json"
        cuerpo = {"jid": {"user": MI_PN, "server": "s.whatsapp.net"}}
        if con_lid:
            cuerpo["lid"] = f"{MI_LID}.6@lid"
        self.session_file.write_text(json.dumps(cuerpo), encoding="utf-8")


def test_sin_identidad_completa_no_se_instala(tmp_path):
    """Una guarda que no sabe a quien proteger no se pone."""
    assert guarda.apply(_Ajustes(tmp_path, con_lid=False)) is False


def test_instalar_dos_veces_no_apila_envoltorios(tmp_path):
    import pywhats.messaging.receiver as receiver_module

    antes = receiver_module.Receiver._migrate_known_lid_sender
    try:
        assert guarda.apply(_Ajustes(tmp_path)) is True
        primera = receiver_module.Receiver._migrate_known_lid_sender
        assert guarda.apply(_Ajustes(tmp_path)) is True
        assert receiver_module.Receiver._migrate_known_lid_sender is primera
    finally:
        receiver_module.Receiver._migrate_known_lid_sender = antes


def test_instalada_deja_pasar_a_los_demas_y_nos_salta_a_nosotros(tmp_path):
    """La prueba de extremo a extremo sobre el receptor de verdad."""
    import pywhats.messaging.receiver as receiver_module

    antes = receiver_module.Receiver._migrate_known_lid_sender
    vistos: list[str] = []
    receiver_module.Receiver._migrate_known_lid_sender = (
        lambda self, sender: vistos.append(f"{sender.user}@{sender.server}")
    )
    try:
        assert guarda.apply(_Ajustes(tmp_path)) is True
        receptor = object()
        receiver_module.Receiver._migrate_known_lid_sender(
            receptor, _JID(MI_LID, "lid")
        )
        receiver_module.Receiver._migrate_known_lid_sender(
            receptor, _JID("64940106866902", "lid")
        )
        assert vistos == ["64940106866902@lid"]
    finally:
        receiver_module.Receiver._migrate_known_lid_sender = antes
