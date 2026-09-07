"""Lo que no se pudo descifrar se recupera por el historial, no por insistir.

EL CASO
-------
89 mensajes llegaron cifrados con una clave de un solo uso ya consumida. Sin
su parte privada el X3DH no se puede completar: no falta codigo, falta un
secreto que ya no existe. Reenviarlos mil veces da el mismo resultado.

Pero el mensaje EXISTE, y el historial de WhatsApp lo tiene. Asi que el
agujero se cierra pidiendolo por ahi --firmado, por el camino de siempre-- en
vez de intentar forzar el cifrado.

LOS DOS FRENTES, QUE NO SON UNO
-------------------------------
``ON_DEMAND`` de siempre excava HACIA ATRAS desde el ancla mas antigua. La
conversacion medida esta ``exhausted``: el servidor ya dijo que por abajo no
queda nada, y volver a preguntarle no puede traer nada. Lo que falta esta
ARRIBA, en el borde reciente, y para eso hay otro motor.

LO QUE PROTEGEN ESTAS PRUEBAS
-----------------------------
Sobre todo dos cosas: que un agujero se anote en la conversacion CORRECTA, y
que nada de esto acepte un mensaje sin autenticar.
"""

from __future__ import annotations

import pytest

from app.services import missed_live
from app.services.missed_live import AgujerosEnVivo, chat_del_remitente, sin_remedio

OPK = "unknown one-time pre-key id 17"
CONTACTO = "206566519222309.44@lid"
CHAT = "206566519222309@lid"


@pytest.fixture
def agujeros():
    return AgujerosEnVivo()


# ---------------------------------------------------------------------------
# A que conversacion pertenece lo perdido
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "remitente",
    ["206566519222309.44@lid", "206566519222309:44@lid", "206566519222309@lid"],
)
def test_los_dispositivos_de_un_contacto_son_UNA_conversacion(remitente):
    """LA REGLA. Los dispositivos 0, 14 y 44 son la misma persona."""
    assert chat_del_remitente(remitente) == CHAT


def test_un_numero_de_telefono_conserva_su_servidor():
    assert chat_del_remitente("573002389304.3@s.whatsapp.net") == (
        "573002389304@s.whatsapp.net"
    )


def test_EN_UN_GRUPO_NO_SE_ADIVINA():
    """El remitente es un participante; la conversacion es el grupo.

    Anotar el agujero en la conversacion equivocada seria pedir el historial
    de quien no perdio nada. Antes que acertar por casualidad, no se contesta.
    """
    assert chat_del_remitente("120363429372692312@g.us") is None


@pytest.mark.parametrize("basura", ["", None, "   ", "sin-arroba", "@lid"])
def test_lo_que_no_se_entiende_no_se_anota(basura):
    assert chat_del_remitente(basura) is None


# ---------------------------------------------------------------------------
# Que fallos dejan agujero
# ---------------------------------------------------------------------------


def test_una_clave_de_un_solo_uso_consumida_deja_agujero():
    """No hay reenvio que lo arregle: la privada ya no existe."""
    assert sin_remedio(OPK) is True


@pytest.mark.parametrize(
    "motivo",
    [
        "signal message mac check failed",
        "no session for peer 865311@lid",
        "bad mac",
        "",
    ],
)
def test_LO_QUE_SI_PUEDE_ARREGLARSE_SOLO_NO_CUENTA(motivo):
    """El limite, y es importante.

    Un MAC que no cuadra o una sesion que falta se resuelven por su via --el
    acuse de reintento con material publico-- y el reenvio SI puede salir
    bien. Anotarlos como agujero pediria historial sin motivo en cada bache.
    """
    assert sin_remedio(motivo) is False


def test_solo_lo_sin_remedio_se_anota(agujeros):
    assert agujeros.anotar(CONTACTO, "huella-1", "mac check failed") is None
    assert agujeros.anotar(CONTACTO, "huella-1", OPK) == CHAT
    assert agujeros.resumen() == {"chats": 1, "messages": 1}


# ---------------------------------------------------------------------------
# El recuento
# ---------------------------------------------------------------------------


def test_el_mismo_mensaje_reenviado_se_cuenta_una_vez(agujeros):
    """El emisor insiste con la misma clave: es UN mensaje perdido, no siete."""
    for _ in range(7):
        agujeros.anotar(CONTACTO, "huella-1", OPK)
    assert agujeros.resumen()["messages"] == 1


def test_mensajes_distintos_se_cuentan_aparte(agujeros):
    for n in range(3):
        agujeros.anotar(CONTACTO, f"huella-{n}", OPK)
    assert agujeros.resumen() == {"chats": 1, "messages": 3}


def test_los_dispositivos_del_mismo_contacto_suman_al_mismo_agujero(agujeros):
    agujeros.anotar("206566519222309.44@lid", "a", OPK)
    agujeros.anotar("206566519222309.14@lid", "b", OPK)
    assert agujeros.resumen() == {"chats": 1, "messages": 2}


def test_cerrar_lo_olvida(agujeros):
    agujeros.anotar(CONTACTO, "a", OPK)
    agujeros.cerrado(CHAT)
    assert agujeros.pendientes() == []


def test_UN_AGUJERO_NO_SE_OLVIDA_SI_NO_SE_CERRO(agujeros):
    """La anotacion es la unica prueba de que ahi falta un mensaje.

    Perderla sin haber traido nada seria dar por recuperado lo que no se
    recupero.
    """
    agujeros.anotar(CONTACTO, "a", OPK)
    agujeros.intento(CHAT)
    agujeros.intento(CHAT)
    assert len(agujeros.pendientes()) == 1


def test_se_cuentan_los_intentos_para_no_insistir_para_siempre(agujeros):
    agujeros.anotar(CONTACTO, "a", OPK)
    assert agujeros.intento(CHAT) == 1
    assert agujeros.intento(CHAT) == 2


def test_de_una_conversacion_sin_agujero_no_se_cuenta_nada(agujeros):
    assert agujeros.intento("nadie@lid") == 0


def test_lo_viejo_caduca():
    """Pasado un dia, o se cerro o ya no esta en el borde reciente."""
    viejos = AgujerosEnVivo(vigencia=-1.0)
    viejos.anotar(CONTACTO, "a", OPK)
    assert viejos.pendientes() == []


def test_el_registro_no_crece_sin_fin(agujeros):
    for n in range(missed_live.MAXIMO_DE_CHATS + 40):
        agujeros.anotar(f"{n}@lid", "a", OPK)
    assert len(agujeros.pendientes()) <= missed_live.MAXIMO_DE_CHATS


# ---------------------------------------------------------------------------
# LO QUE NO SE HACE
# ---------------------------------------------------------------------------


def test_no_se_guarda_ni_un_byte_de_contenido(agujeros):
    """No se descifro: no hay contenido que guardar, y no se inventa ninguno."""
    agujeros.anotar(CONTACTO, "huella-1", OPK)
    guardado = agujeros.pendientes()[0]
    assert not hasattr(guardado, "text")
    assert guardado.perdidos == {"huella-1"}


def test_NADA_DE_ESTO_ACEPTA_UN_MENSAJE_SIN_AUTENTICAR():
    """La prueba que mas importa.

    Cerrar el agujero es pedir el mensaje por el historial, que llega firmado
    y por el camino de siempre. En ningun punto se relaja una comprobacion ni
    se guarda lo que no se pudo descifrar.
    """
    import ast
    import pathlib

    for fichero in (
        "app/services/missed_live.py",
        "app/services/missed_live_filler.py",
    ):
        arbol = ast.parse(pathlib.Path(fichero).read_text(encoding="utf-8"))
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
        codigo = ast.unparse(arbol).lower()
        for prohibido in (
            "verify_mac",
            "ratchet_decrypt",
            "identity_private",
            "_sessions",
            "history_status",
            "persist_cursor",
        ):
            assert prohibido not in codigo, f"{fichero}: prohibido {prohibido}"
