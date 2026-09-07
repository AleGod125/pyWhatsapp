"""Sin sesion, el acuse lleva el material publico DESDE EL PRIMER INTENTO.

EL BLOQUEO, MEDIDO EN EL REGISTRO LOCAL
---------------------------------------
Los mensajes escritos desde el telefono propio llegan aqui como copia y no se
descifran nunca::

    [OWN_LIVE] enc=msg address=LID lid_fp=- decrypt=fallo
               (no session for peer 865311***@lid)
    receiver: sent retry receipt id=ACF10E... to=865311***@lid
    send_frame len=100

**303 acuses a nuestro propio LID. Los 303 de 100 bytes.** Ni uno con
material: el bloque de claves ocupa unos 500. El telefono los confirma con un
ack y no reenvia nada, porque sin nuestras claves publicas no tiene con que
rehacer el saludo.

POR QUE NO SALIA
----------------
El material se adjuntaba a partir del SEGUNDO intento --lo que hacen Baileys y
whatsmeow-- y eso da por hecho que habra un segundo intento. Para la copia del
propio telefono no lo hay: se manda UNA vez, no la descifra nadie, y como no
vuelve a mandarse el contador se queda clavado en 1. Para siempre.

LA REGLA NUEVA, Y SU LIMITE
---------------------------
Esperar tenia sentido para no regalar una clave de un solo uso con cada
mensaje que llegue desordenado. Eso vale cuando HAY sesion: el mensaje puede
venir fuera de orden. Sin sesion no hay nada con lo que estar desordenado.

Asi que la excepcion es exactamente esa, y ninguna otra. Un MAC que no cuadra
significa que la sesion existe y esta desincronizada: ese caso sigue la regla
de siempre, y el mensaje sigue sin aceptarse.
"""

from __future__ import annotations

import asyncio

import pytest

from app.compat import retry_observer


class _Transporte:
    def __init__(self):
        self.enviados = []

    async def send(self, marco):  # noqa: ANN001
        self.enviados.append(marco)


class _Almacen:
    """El almacen de sesiones, con lo justo para poder preguntarle."""

    def __init__(self, sesiones=None, revienta: bool = False):
        self._sesiones = sesiones or {}
        self._revienta = revienta

    def load(self, sid):  # noqa: ANN001
        if self._revienta:
            raise RuntimeError("el almacen no se deja leer")
        return self._sesiones.get(sid)


class _Receptor:
    def __init__(self, sesiones=None, revienta: bool = False):
        self._transport = _Transporte()
        self._sessions = _Almacen(sesiones, revienta)

        class _Id:
            registration_id = 123456

        self._identity = _Id()


class _Nodo:
    def __init__(self, wamid: str):
        self.attrs = {"id": wamid, "t": "1760000000"}

    def get_str(self, clave):  # noqa: ANN001
        return self.attrs.get(clave, "")


class _Jid:
    """Un remitente con la forma que espera ``session_id``."""

    def __init__(self, user="865311142340710", device=0, server="lid"):
        self.user = user
        self.device = device
        self.server = server


def _sid(jid) -> str:  # noqa: ANN001
    from pywhats.messaging.addressing import session_id

    return session_id(jid)


@pytest.fixture
def observador():
    retry_observer.reiniciar()
    yield retry_observer
    retry_observer.reiniciar()


# ---------------------------------------------------------------------------
# La lectura: ¿hay sesion o no?
# ---------------------------------------------------------------------------


def test_sin_registro_de_sesion_se_sabe_que_no_la_hay():
    """Es el caso medido: ``lid_fp=-``, ningun registro."""
    assert retry_observer._sin_sesion_con(_Receptor(), _Jid()) is True


def test_con_sesion_guardada_se_sabe_que_si_la_hay():
    jid = _Jid()
    receptor = _Receptor({_sid(jid): b"estado-de-sesion"})
    assert retry_observer._sin_sesion_con(receptor, jid) is False


def test_si_el_almacen_no_se_deja_leer_se_mantiene_la_regla_de_siempre():
    """Ante la duda, la norma antigua.

    Adjuntar material de mas gasta una clave de un solo uso; no adjuntarlo
    cuando hacia falta solo repite el acuse simple en el intento siguiente.
    """
    assert retry_observer._sin_sesion_con(_Receptor(revienta=True), _Jid()) is False


def test_mirar_el_almacen_no_lo_modifica():
    """Es una lectura. No crea, no borra, no migra."""
    jid = _Jid()
    sesiones = {_sid(jid): b"estado-de-sesion"}
    receptor = _Receptor(dict(sesiones))
    retry_observer._sin_sesion_con(receptor, jid)
    assert receptor._sessions._sesiones == sesiones


# ---------------------------------------------------------------------------
# La consecuencia: cuando se adjunta el material
# ---------------------------------------------------------------------------


def _acusar(observador, receptor, jid, wamid="WAMID-1"):
    """Manda un acuse y devuelve con que ``con_claves`` se construyo."""
    import pywhats.messaging.receiver as receiver_module

    antes = receiver_module.Receiver._send_retry_receipt
    vistos = []

    async def _base(self, node, *, sender):  # noqa: ANN001, ANN202
        vistos.append(None)

    async def _con_contador(self, node, sender, intentos, *, con_claves=False):  # noqa: ANN001, ANN202
        vistos.append({"intentos": intentos, "con_claves": con_claves})
        return True

    receiver_module.Receiver._send_retry_receipt = _base
    original_contador = observador._enviar_con_contador
    observador._enviar_con_contador = _con_contador
    try:
        assert observador.apply(tracker=None) is True
        asyncio.run(
            receiver_module.Receiver._send_retry_receipt(
                receptor, _Nodo(wamid), sender=jid
            )
        )
    finally:
        observador._enviar_con_contador = original_contador
        receiver_module.Receiver._send_retry_receipt = antes
    return vistos[0] if vistos and vistos[0] is not None else None


def test_SIN_SESION_EL_PRIMER_ACUSE_YA_LLEVA_MATERIAL(observador):
    """LA REGLA NUEVA. Es lo que desatasca los 303 acuses vacios."""
    enviado = _acusar(observador, _Receptor(), _Jid())

    assert enviado is not None, "tenia que construirse el acuse con contador"
    assert enviado["intentos"] == 1
    assert enviado["con_claves"] is True


def test_CON_SESION_EL_PRIMER_ACUSE_SIGUE_SIENDO_EL_SIMPLE(observador):
    """El limite de la excepcion.

    Con sesion, un mensaje que no descifra puede ser sencillamente uno que
    llego fuera de orden. Regalar una clave de un solo uso por eso es lo que
    la regla de los dos intentos evita, y se conserva.
    """
    jid = _Jid()
    receptor = _Receptor({_sid(jid): b"estado-de-sesion"})

    enviado = _acusar(observador, receptor, jid, wamid="WAMID-CON-SESION")

    assert enviado is None, "no debia adjuntarse material en el primer intento"


def test_un_fallo_de_MAC_no_es_una_sesion_que_falte(observador):
    """§17 y §26: un MAC que no cuadra NO abre ninguna puerta.

    Que el MAC falle significa que la sesion existe y esta desincronizada. El
    acuse sigue la regla de siempre y --lo que de verdad importa-- el mensaje
    sigue sin descifrarse y sin guardarse: aqui no se acepta nada por
    insistencia.
    """
    jid = _Jid()
    receptor = _Receptor({_sid(jid): b"sesion-desincronizada"})

    assert retry_observer._sin_sesion_con(receptor, jid) is False
    assert _acusar(observador, receptor, jid, wamid="WAMID-MAC") is None


# ---------------------------------------------------------------------------
# Lo que este cambio NO hace
# ---------------------------------------------------------------------------


def test_el_cambio_no_toca_ni_una_sesion():
    """Guardia de codigo: aqui no se crea, ni se borra, ni se migra nada."""
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
    # Se prohiben ESCRITURAS, no la palabra "identity": el acuse lee el
    # `registration_id` publico para armar el nodo <registration>, que es
    # justo lo que el protocolo pide y no revela nada.
    for prohibido in (
        "_sessions.delete",
        "_sessions.store",
        "_sessions.save",
        "_sessions.put",
        "_identities.store",
        "_identities.save",
        "_identities.delete",
        "migrate_pn_session",
        "private",
    ):
        assert prohibido not in sin_texto, f"prohibido: {prohibido}"
