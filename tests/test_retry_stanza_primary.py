"""La forma EXACTA del acuse de reintento para una copia del telefono propio.

POR QUE EXISTE ESTE FICHERO
---------------------------
La hipotesis «con material publico en el primer acuse, el telefono reenvia»
quedo refutada por una prueba real::

    20:54:10  no session for peer 865311***@lid
    20:54:10  send_frame len=510          <- acuse CON material
    20:54:10  ack->ok  class=receipt      <- lo acepta el SERVIDOR
    ...                                    <- y del telefono, nada

Se repitio 35 segundos despues con el mismo final. Asi que el material no era
lo unico que faltaba, y a partir de aqui la estructura de la stanza se fija en
una prueba en vez de darse por buena.

LO QUE SE ENCONTRO
------------------
Una asimetria dentro del propio pywhats: al construir un ``<ack>`` copia tres
atributos de enrutado --``participant``, ``recipient`` y ``type``-- pero su
acuse de reintento solo copiaba ``participant``. ``recipient`` es justamente
el que aparece cuando el mensaje va de tu cuenta a tu cuenta, que es la forma
de una copia del telefono principal.

Es la unica diferencia estructural encontrada entre lo que mandabamos y lo que
manda un cliente moderno. Sigue siendo una **hipotesis**: la confirma o la
refuta la siguiente prueba real, no este fichero.

LO QUE NO CAMBIA
----------------
Para un contacto normal --el camino que hoy funciona-- la stanza queda byte a
byte como estaba. La copia es condicional.
"""

from __future__ import annotations

import asyncio

import pytest

from app.compat import retry_observer


class _Transporte:
    def __init__(self):
        self.enviados: list[bytes] = []

    async def send(self, marco):  # noqa: ANN001
        self.enviados.append(marco)


class _Almacen:
    def __init__(self, sesiones=None):
        self._sesiones = sesiones or {}

    def load(self, sid):  # noqa: ANN001
        return self._sesiones.get(sid)


class _Receptor:
    def __init__(self, sesiones=None):
        self._transport = _Transporte()
        self._sessions = _Almacen(sesiones)

        class _Id:
            registration_id = 1111535480

        self._identity = _Id()


class _Nodo:
    """La stanza que llega. `attrs` es lo que se copia al acuse."""

    def __init__(self, wamid: str, **attrs):
        self.attrs = {"id": wamid, "t": "1788700000", **attrs}

    def get_str(self, clave):  # noqa: ANN001
        return str(self.attrs.get(clave, ""))


class _Jid:
    def __init__(self, user="865311142340710", device=0, server="lid"):
        self.user = user
        self.device = device
        self.server = server

    def __str__(self):
        return f"{self.user}.{self.device}@{self.server}"


@pytest.fixture(autouse=True)
def limpio():
    from app.compat import own_retry_trace

    retry_observer.reiniciar()
    own_retry_trace.olvidar_todo()
    yield
    retry_observer.reiniciar()
    own_retry_trace.olvidar_todo()


def _construir(nodo, sender=None, intentos=1, con_claves=False):
    """Manda el acuse y devuelve el Node construido, sin codificar."""
    from pywhats.binary.node import Node

    receptor = _Receptor()
    capturado: dict = {}

    def _encode_espia(nodo_acuse):
        capturado["acuse"] = nodo_acuse
        return b"x" * 10

    import app.compat.retry_observer as mod

    original_encode = None
    try:
        import pywhats.binary.encoder as enc

        original_encode = enc.encode
        enc.encode = _encode_espia
        asyncio.run(
            mod._enviar_con_contador(
                receptor,
                nodo,
                sender or _Jid(),
                intentos,
                con_claves=con_claves,
            )
        )
    finally:
        if original_encode is not None:
            import pywhats.binary.encoder as enc

            enc.encode = original_encode
    assert isinstance(capturado.get("acuse"), Node), "no se construyo el acuse"
    return capturado["acuse"]


def _hijo(nodo, tag):
    for h in nodo.content or []:
        if h.tag == tag:
            return h
    return None


# ---------------------------------------------------------------------------
# La forma obligatoria
# ---------------------------------------------------------------------------


def test_el_acuse_es_un_receipt_de_tipo_retry():
    acuse = _construir(_Nodo("AC009BE9"))
    assert acuse.tag == "receipt"
    assert acuse.attrs["type"] == "retry"


def test_lleva_el_id_ORIGINAL_del_mensaje():
    """Sin el, el emisor no sabe que mensaje reenviar."""
    acuse = _construir(_Nodo("AC009BE91C8070DB06A0F65B0E6ED9EB"))
    assert acuse.attrs["id"] == "AC009BE91C8070DB06A0F65B0E6ED9EB"
    assert _hijo(acuse, "retry").attrs["id"] == "AC009BE91C8070DB06A0F65B0E6ED9EB"


def test_el_nodo_retry_lleva_contador_marca_y_version():
    acuse = _construir(_Nodo("AC009BE9"), intentos=3)
    retry = _hijo(acuse, "retry")
    assert retry is not None
    assert retry.attrs["count"] == "3"
    assert retry.attrs["t"] == "1788700000"
    assert retry.attrs["v"] == "1"


def test_el_contador_empieza_en_UNO_no_en_cero():
    """El primer intento es el 1. Un 0 diria que no ha fallado ninguna vez."""
    acuse = _construir(_Nodo("AC009BE9"), intentos=1)
    assert _hijo(acuse, "retry").attrs["count"] == "1"


def test_la_marca_de_tiempo_sale_de_la_stanza_no_del_reloj():
    """Inventarla seria decirle al emisor que busque en otro momento."""
    acuse = _construir(_Nodo("AC009BE9", t="1700000123"))
    assert _hijo(acuse, "retry").attrs["t"] == "1700000123"


def test_el_registro_va_en_cuatro_bytes_big_endian():
    acuse = _construir(_Nodo("AC009BE9"))
    registro = _hijo(acuse, "registration")
    assert registro is not None
    assert len(registro.content) == 4
    assert int.from_bytes(registro.content, "big") == 1111535480


# ---------------------------------------------------------------------------
# El enrutado: lo que se encontro
# ---------------------------------------------------------------------------


def test_EL_ACUSE_CONSERVA_RECIPIENT():
    """LA HIPOTESIS DE ESTE PARCHE.

    Cuando el mensaje lo escribes tu, la stanza va de tu cuenta a tu cuenta y
    quien dice de que conversacion se trata es `recipient`. Se perdia.
    """
    acuse = _construir(_Nodo("AC009BE9", recipient="206566519222309@lid"))
    assert acuse.attrs.get("recipient") == "206566519222309@lid"


def test_el_acuse_conserva_participant():
    """Ya se copiaba, y se comprueba que sigue haciendolo."""
    acuse = _construir(_Nodo("AC009BE9", participant="573001112233@s.whatsapp.net"))
    assert acuse.attrs.get("participant") == "573001112233@s.whatsapp.net"


def test_se_conservan_LOS_DOS_a_la_vez():
    acuse = _construir(
        _Nodo("AC009BE9", recipient="206566519222309@lid", participant="x@lid")
    )
    assert acuse.attrs.get("recipient") == "206566519222309@lid"
    assert acuse.attrs.get("participant") == "x@lid"


def test_SIN_ESOS_ATRIBUTOS_LA_STANZA_QUEDA_IGUAL_QUE_ANTES():
    """El camino que YA funciona no puede cambiar ni un byte.

    Un contacto normal manda su stanza sin `recipient`, y la copia es
    condicional: no se anade una clave vacia ni un valor inventado.
    """
    acuse = _construir(_Nodo("AC009BE9"))
    assert set(acuse.attrs) == {"id", "type", "to"}


def test_el_acuse_va_al_dispositivo_que_mando_el_mensaje():
    """Con su numero de dispositivo: la copia del telefono es del 0."""
    acuse = _construir(_Nodo("AC009BE9"), sender=_Jid(device=0))
    assert str(acuse.attrs["to"]).endswith("@lid")
    assert ".0@" in str(acuse.attrs["to"])


# ---------------------------------------------------------------------------
# El material publico
# ---------------------------------------------------------------------------


def test_sin_material_no_se_manda_un_bloque_vacio():
    """Un `<keys>` a medias es peor que ninguno: el emisor lo descarta entero."""
    acuse = _construir(_Nodo("AC009BE9"), con_claves=False)
    assert _hijo(acuse, "keys") is None


def test_el_orden_de_los_nodos_es_retry_registration_keys():
    """El orden importa en el arbol binario de WhatsApp."""
    acuse = _construir(_Nodo("AC009BE9"))
    assert [h.tag for h in acuse.content][:2] == ["retry", "registration"]


# ---------------------------------------------------------------------------
# LO QUE NO VIAJA
# ---------------------------------------------------------------------------


def test_NI_UN_BYTE_PRIVADO_EN_LA_STANZA():
    """La prueba que mas importa.

    En el acuse solo va material publico: identificador de registro, claves
    publicas y una firma. Ni privadas, ni estado de ratchet, ni contenido.
    """
    import ast
    import pathlib

    codigo = pathlib.Path("app/compat/retry_observer.py").read_text(encoding="utf-8")
    arbol = ast.parse(codigo)
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
    sin_texto = ast.unparse(arbol).lower()
    for prohibido in (
        "identity_private",
        "private",
        "_sessions.save",
        "_sessions.delete",
        "ratchet",
    ):
        assert prohibido not in sin_texto, f"prohibido: {prohibido}"


def test_el_diagnostico_no_ensena_ninguna_clave():
    """El rastro apunta identificadores y tamanos, nunca material."""
    import ast
    import pathlib

    codigo = pathlib.Path("app/compat/own_retry_trace.py").read_text(encoding="utf-8")
    arbol = ast.parse(codigo)
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
    sin_texto = ast.unparse(arbol).lower()
    # Se prohibe el MATERIAL, no la palabra: del `device_identity` solo se
    # apunta si lo hay o no, y de las claves solo su identificador.
    for prohibido in (
        "identity_public",
        "spk_public",
        "opk_public",
        "identity_private",
        "ciphertext",
        "plaintext",
        "signature",
    ):
        assert prohibido not in sin_texto, f"prohibido: {prohibido}"
    # Y lo que si tiene que estar: los identificadores, que no revelan nada.
    assert "opk_id" in sin_texto
    assert "spk_id" in sin_texto


def test_el_numero_de_telefono_no_se_escribe_entero():
    from app.compat.own_retry_trace import _direccion

    assert _direccion("865311142340710.0@lid") == "865311***@lid"
    assert _direccion(None) == "?"
