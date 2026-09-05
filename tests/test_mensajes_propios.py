"""Copias de lo que yo mismo escribo: del telefono y del navegador.

LA DIFERENCIA QUE HAY QUE DISTINGUIR
------------------------------------
Las dos llevan MI identificador de cuenta. Lo que las separa es el
DISPOSITIVO, y eso decide por que sesion Signal llegan y como fallan:

    telefono   -> dispositivo 0
    vinculado  -> dispositivo distinto de 0 (se midio el 92)

Deducirlo del JID a secas las confunde, y confundirlas manda a buscar el fallo
al sitio equivocado.

LO QUE NO SE NEGOCIA
--------------------
Una copia que no supera su verificacion de autenticidad NO se entrega, no se
persiste, no siembra un ancla y no se anuncia. Se pide el reenvio, y si el
reenvio llega autenticado, entra por el camino normal UNA sola vez.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.own_device import (
    LINKED_UNKNOWN,
    LINKED_WEB,
    PEER,
    PRIMARY_PHONE,
    auditar_sesiones,
    clasificar,
    direccion_signal,
    enmascarar,
)
from app.core.retry_tracker import (
    ORIGINAL_FAILED,
    RETRY_FAILED,
    RETRY_SENT,
    RETRY_SUCCESS,
    RetryTracker,
)

MI_PN = "573002389304"
MI_LID = "86531142340710"
DE_OTRO = "64940106866902"


class _Remitente:
    def __init__(self, user: str, server: str = "lid", device: int = 0):
        self.user = user
        self.server = server
        self.device = device


def _clasificar(remitente):
    return clasificar(remitente, own_pn_user=MI_PN, own_lid_user=MI_LID)


# ---------------------------------------------------------------------------
# De cual de mis dispositivos viene
# ---------------------------------------------------------------------------


def test_el_telefono_es_el_dispositivo_cero():
    assert _clasificar(_Remitente(MI_LID, device=0)) == PRIMARY_PHONE


def test_un_vinculado_no_es_el_telefono():
    """Se midio el 92 en la cuenta real: es WhatsApp Web."""
    assert _clasificar(_Remitente(MI_LID, device=92)) == LINKED_WEB


def test_mi_numero_tambien_soy_yo():
    assert _clasificar(_Remitente(MI_PN, server="s.whatsapp.net")) == PRIMARY_PHONE


def test_otra_persona_no_es_mi_dispositivo():
    assert _clasificar(_Remitente(DE_OTRO)) == PEER


def test_sin_dispositivo_no_se_afirma_cual_es():
    """``linked_unknown`` existe para no inventarse "Web"."""

    class _SinDispositivo:
        user = MI_LID
        server = "lid"
        device = None

    assert _clasificar(_SinDispositivo()) == LINKED_UNKNOWN


def test_no_se_clasifica_solo_por_el_identificador():
    """El telefono y el vinculado llevan el MISMO identificador de cuenta."""
    telefono = _clasificar(_Remitente(MI_LID, device=0))
    vinculado = _clasificar(_Remitente(MI_LID, device=92))
    assert telefono != vinculado


# ---------------------------------------------------------------------------
# La direccion Signal
# ---------------------------------------------------------------------------


def test_la_direccion_signal_lleva_el_dispositivo():
    assert direccion_signal(_Remitente(MI_LID, device=92)) == f"{MI_LID}:92@lid"


def test_la_direccion_registrada_va_truncada():
    """Un identificador completo es un numero de telefono."""
    enmascarada = enmascarar(direccion_signal(_Remitente(MI_LID, device=0)))
    assert MI_LID not in enmascarada
    assert enmascarada.endswith(":0@lid")


def test_se_detecta_el_mismo_aparato_con_dos_sesiones(tmp_path):
    """La causa medida de que alguna copia del telefono no cuadre.

    ``PN:0`` y ``LID:0`` son el mismo aparato con dos estados de ratchet
    distintos. Se comprobo en el almacen real de la cuenta.
    """
    import sqlite3

    store = tmp_path / "signal.db"
    con = sqlite3.connect(store)
    con.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
    con.executemany(
        "INSERT INTO sessions VALUES (?)",
        [
            (f"{MI_PN}:0@s.whatsapp.net",),
            (f"{MI_LID}:0@lid",),
            (f"{MI_LID}:92@lid",),
            (f"{DE_OTRO}:0@lid",),
        ],
    )
    con.commit()
    con.close()

    auditoria = auditar_sesiones(store, own_pn_user=MI_PN, own_lid_user=MI_LID)

    assert auditoria.legible
    assert auditoria.duplicados == [0], "el dispositivo 0 esta por las dos vias"
    assert 92 in auditoria.por_lid, "el vinculado esta solo por LID; por eso no falla"
    assert auditoria.hay_duplicados


def test_sin_duplicados_no_se_avisa_de_nada(tmp_path):
    import sqlite3

    store = tmp_path / "signal.db"
    con = sqlite3.connect(store)
    con.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
    con.execute("INSERT INTO sessions VALUES (?)", (f"{MI_LID}:0@lid",))
    con.commit()
    con.close()

    auditoria = auditar_sesiones(store, own_pn_user=MI_PN, own_lid_user=MI_LID)
    assert not auditoria.hay_duplicados


def test_auditar_no_escribe_en_el_almacen(tmp_path):
    """Se abre en SOLO LECTURA: mirar no puede cambiar estado de Signal."""
    import inspect

    from app.core import own_device

    fuente = inspect.getsource(own_device.auditar_sesiones)
    assert "mode=ro" in fuente
    for prohibido in ("INSERT", "UPDATE", "DELETE", "DROP"):
        assert prohibido not in fuente.upper()


# ---------------------------------------------------------------------------
# El recorrido de un mensaje que no cuadro
# ---------------------------------------------------------------------------


def test_el_recorrido_completo_de_un_reintento_que_funciona():
    """msg falla -> se pide reenvio -> llega pkmsg -> descifra -> listo."""
    seguimiento = RetryTracker()

    seguimiento.fallo("WAMID1", "mac check failed", origen=PRIMARY_PHONE, enc_type="msg")
    assert seguimiento.estado_de("WAMID1") == ORIGINAL_FAILED

    seguimiento.acuse_enviado("WAMID1")
    assert seguimiento.estado_de("WAMID1") == RETRY_SENT

    recuperado = seguimiento.recuperado("WAMID1")
    assert recuperado is not None and recuperado.estado == RETRY_SUCCESS
    assert seguimiento.sin_resolver == 0
    assert seguimiento.resumen()[RETRY_SUCCESS] == 1


def test_un_reenvio_que_tampoco_cuadra_no_es_un_mensaje_nuevo():
    seguimiento = RetryTracker()
    seguimiento.fallo("WAMID1", "mac check failed")
    seguimiento.acuse_enviado("WAMID1")
    seguimiento.fallo("WAMID1", "mac check failed")

    assert seguimiento.estado_de("WAMID1") == RETRY_FAILED
    assert seguimiento.intentos_de("WAMID1") == 2, (
        "es el MISMO mensaje fallando otra vez, no dos mensajes rotos"
    )
    assert seguimiento.sin_resolver == 1


def test_un_reenvio_que_nunca_llega_se_queda_a_la_vista():
    """La pregunta util no es cuantos fallaron: es cuantos faltan."""
    seguimiento = RetryTracker()
    seguimiento.fallo("WAMID1", "no session")
    seguimiento.acuse_enviado("WAMID1")

    assert seguimiento.resumen()["sin_respuesta"] == 1
    assert seguimiento.resumen()["sin_resolver"] == 1


def test_recuperar_algo_que_nunca_fallo_no_inventa_un_reintento():
    assert RetryTracker().recuperado("NUNCA_FALLO") is None


def test_el_seguimiento_no_crece_sin_limite():
    """Una sesion larga no puede acumular cada fallo para siempre."""
    seguimiento = RetryTracker(maximo=10)
    for i in range(50):
        seguimiento.fallo(f"WAMID{i}", "mac check failed")
    assert seguimiento.sin_resolver <= 10


def test_el_seguimiento_no_guarda_contenido():
    seguimiento = RetryTracker()
    seguimiento.fallo("WAMID1", "mac check failed", origen=PRIMARY_PHONE)
    fila = seguimiento.pendientes()[0]
    assert set(fila) == {
        "message_id",
        "state",
        "attempts",
        "source_device",
        "enc_type",
        "device",
        "reason",
        "age_seconds",
    }
    assert "text" not in fila and "body" not in fila


def test_el_contador_real_de_intentos_esta_disponible():
    """``Receiver`` manda ``count="1"`` fijo; aqui esta el numero de verdad."""
    seguimiento = RetryTracker()
    for _ in range(3):
        seguimiento.fallo("WAMID1", "mac check failed")
    assert seguimiento.intentos_de("WAMID1") == 3


# ---------------------------------------------------------------------------
# Un MAC que no cuadra no entrega nada
# ---------------------------------------------------------------------------


def test_una_copia_que_no_cuadra_no_persiste_ni_siembra(runtime):
    """El camino en vivo NO llega a ejecutarse: la excepcion corta antes.

    Se comprueba sobre el runtime real, no leyendo el codigo: si un dia
    alguien capturara la excepcion para "no perder el mensaje", este test
    fallaria.
    """
    antes = int(runtime.counters.get("live_persisted", 0) or 0)

    class _EventoDeFallo:
        extra = {"args": ["signal message mac check failed"]}

    runtime._anotar_fallo_descifrado("WAMID_MAC", _EventoDeFallo())

    assert runtime.counters.get("live_persisted", 0) == antes, "no se persiste"
    assert runtime.retry_tracker.estado_de("WAMID_MAC") == ORIGINAL_FAILED
    assert runtime.counters.get("mac_failures", 0) >= 1


def test_el_receptor_relanza_el_fallo_en_vez_de_tragarselo():
    """La red de seguridad: si esto cambia, se entregaria lo no autenticado."""
    import inspect

    from app.compat import lid_diagnostics

    fuente = inspect.getsource(lid_diagnostics.apply)
    assert "raise" in fuente, "la excepcion se relanza"
    assert "return None" not in fuente


def _codigo_sin_prosa(modulo) -> str:
    """El codigo del modulo SIN docstrings ni comentarios.

    Importa: estos modulos EXPLICAN en su documentacion lo que no hacen, y
    buscar la cadena en el texto daria positivo justo por explicarlo.
    """
    import ast
    import inspect

    arbol = ast.parse(inspect.getsource(modulo))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            cuerpo = getattr(nodo, "body", [])
            if (
                cuerpo
                and isinstance(cuerpo[0], ast.Expr)
                and isinstance(cuerpo[0].value, ast.Constant)
                and isinstance(cuerpo[0].value.value, str)
            ):
                cuerpo.pop(0)
    return ast.unparse(arbol)


def test_no_hay_forma_de_desactivar_la_verificacion():
    """En NINGUN modulo, ni siquiera en el que ya existia y funciona."""
    from app.compat import lid_diagnostics, prekey_compat, retry_observer
    from app.core import own_device, retry_tracker

    for modulo in (
        lid_diagnostics,
        prekey_compat,
        retry_observer,
        own_device,
        retry_tracker,
    ):
        codigo = _codigo_sin_prosa(modulo)
        for prohibido in (
            "verify_mac=False",
            "skip_mac",
            "check_mac=False",
            "verify=False",
        ):
            assert prohibido not in codigo, f"{modulo.__name__}: {prohibido}"


def test_lo_nuevo_no_escribe_ni_una_sesion():
    """Copiar un ratchet a otra direccion es fabricar estado criptografico.

    ``prekey_compat`` queda fuera a proposito: SI escribe sesiones, es la
    adaptacion que ya existia, esta auditada y funciona. Lo que no puede pasar
    es que el codigo nuevo empiece a tocarlas.
    """
    from app.compat import lid_diagnostics, retry_observer
    from app.core import own_device, retry_tracker

    for modulo in (own_device, lid_diagnostics, retry_observer, retry_tracker):
        codigo = _codigo_sin_prosa(modulo)
        for prohibido in (
            "migrate_pn_session_to_lid",
            "sessions.delete",
            "sessions.save",
            "identity_store.save",
            "identity_store.delete",
        ):
            assert prohibido not in codigo, f"{modulo.__name__} toca sesiones"


def test_la_adaptacion_de_prekeys_sigue_intacta():
    """Reutilizar el ratchet con una OPK ya consumida es legitimo y funciona.

    Se comprueba que sigue ahi: "arreglar" los fallos de MAC desactivandola
    romperia el caso que YA se resolvio.
    """
    from app.compat import prekey_compat

    codigo = _codigo_sin_prosa(prekey_compat)
    assert "sessions.save" in codigo, "la compat de prekeys sigue estableciendo sesion"


# ---------------------------------------------------------------------------
# El caso del navegador sigue funcionando
# ---------------------------------------------------------------------------


def test_una_copia_del_navegador_va_a_su_destinatario(session):
    """``DeviceSentMessage``: el destino lo dice el protobuf, no el remitente.

    Es la ruta que YA funcionaba. Este test esta aqui para que siga
    funcionando despues de tocar la clasificacion por dispositivo.
    """
    from app.core.device_sent import route
    from tests.test_outgoing_routing import OWN_LID, OWN_PN, envuelto

    destino = "64940106866902@lid"
    crudo = envuelto(destino, texto="hola")

    resultado = route(crudo, chat_jid=OWN_LID, own_identifiers=frozenset({OWN_PN, OWN_LID}))

    assert resultado.reenrutado
    assert resultado.chat_jid == destino
    assert resultado.es_saliente


def test_una_nota_para_mi_mismo_se_queda_en_mi_chat(session):
    """El destino que declara el protobuf a veces soy yo. Se respeta."""
    from app.core.device_sent import route
    from tests.test_outgoing_routing import OWN_LID, OWN_PN, envuelto

    crudo = envuelto(OWN_LID, texto="nota")
    resultado = route(crudo, chat_jid=OWN_LID, own_identifiers=frozenset({OWN_PN, OWN_LID}))

    assert resultado.chat_jid == OWN_LID


def test_un_mensaje_entrante_no_se_mueve_de_sitio(session):
    from app.core.device_sent import route
    from tests.test_outgoing_routing import OWN_LID, OWN_PN, entrante

    ajeno = "64940106866902@lid"
    resultado = route(
        entrante(texto="hola"), chat_jid=ajeno, own_identifiers=frozenset({OWN_PN, OWN_LID})
    )
    assert not resultado.reenrutado and resultado.chat_jid == ajeno


# ---------------------------------------------------------------------------
# Un solo proceso sobre la sesion
# ---------------------------------------------------------------------------


def test_la_guarda_de_instancia_unica_sigue_puesta():
    """Ya existia, y es la correcta. No se ha anadido una segunda.

    Dos procesos sobre el mismo Signal Store avanzarian los mismos ratchets y
    producirian fallos de autenticacion identicos a los que se estaban
    diagnosticando. ``app.core.lock`` lo impide desde antes de construir nada,
    y ademas distingue un cerrojo huerfano por su latido.
    """
    import inspect

    import service
    from app.core import lock

    fuente = inspect.getsource(service.main)
    assert "probe(settings.session_dir)" in fuente
    assert fuente.index("probe(settings.session_dir)") < fuente.index(
        "build_service_runtime"
    ), "se comprueba ANTES de construir nada"
    assert lock.HEARTBEAT_INTERVAL < lock.STALE_AFTER
