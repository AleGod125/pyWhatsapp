"""Anclas en el app-state: la unica fuente que quedaba por mirar.

POR QUE
-------
33 chats llegaron como pura metadata. Se auditaron los 29 campos que trae de
verdad una ``Conversation`` del bootstrap (no los 5 que modela pywhats) y para
un chat sin mensajes solo hay identificadores del contacto, una categoria y
contadores. Ni un identificador de mensaje.

El app-state es otro canal, y varias de sus acciones llevan claves de mensaje
reales. pywhats no modela esas acciones, pero protobuf conserva los campos
que no conoce.

QUE FIJAN ESTAS PRUEBAS
-----------------------
Que la busqueda no invente nada, que la pertenencia al chat se DEMUESTRE con
el ``remote_jid`` de la propia clave, y que en modo observacion no se escriba
una sola fila ni se pida nada al servidor.
"""

from __future__ import annotations

import pytest

from app.compat import appstate_seeds

CONTACTO = "34600111222@s.whatsapp.net"
OTRO = "34600999888@s.whatsapp.net"
GRUPO = "120363111222333444@g.us"
ID_REAL = "3A1F8BDD4678EB6DE395"


def _message_key(remote_jid: str, message_id: str, *, from_me: bool = False) -> bytes:
    """Un ``MessageKey`` de verdad, serializado con el descriptor instalado."""
    from pywhats.proto import MessageKey

    clave = MessageKey()
    clave.remote_jid = remote_jid
    clave.id = message_id
    clave.from_me = from_me
    return clave.SerializeToString()


def _accion_con_clave(clave: bytes, *, campo: int = 8):
    """``SyncActionValue`` con un ``MessageKey`` en un campo no modelado.

    El campo 8 es el que ocupa ``markChatAsReadAction`` en el protocolo real.
    pywhats no lo modela, que es justo la situacion que se quiere reproducir:
    el dato llega, protobuf lo conserva, y hay que encontrarlo sin nombre.
    """
    from pywhats.proto import SyncActionValue

    accion = SyncActionValue()
    accion.timestamp = 1_788_400_000
    crudo = bytearray(accion.SerializeToString())

    # messageRange { messages { key { ... } } }: tres envoltorios, como en el
    # protocolo real. Se construyen a mano SOLO para la prueba.
    key_env = bytes([0x0A, len(clave)]) + clave
    msg_env = bytes([0x0A, len(key_env)]) + key_env
    rango = bytes([(campo << 3) | 2, len(msg_env)]) + msg_env
    crudo.extend(rango)

    recuperada = SyncActionValue()
    recuperada.ParseFromString(bytes(crudo))
    return recuperada


class _Mutacion:
    def __init__(self, indice, accion):
        self.index = indice
        self.action = accion
        self.operation = 0


@pytest.fixture(autouse=True)
def limpio():
    appstate_seeds.reset()
    yield
    appstate_seeds.reset()


# ---------------------------------------------------------------------------
# Deteccion estructural
# ---------------------------------------------------------------------------


def test_los_campos_salen_del_descriptor_instalado():
    """No se adivina ningun numero."""
    from pywhats.proto import MessageKey

    campos = {f.number: f.name for f in MessageKey.DESCRIPTOR.fields}
    assert campos[appstate_seeds.CAMPO_REMOTE_JID] == "remote_jid"
    assert campos[appstate_seeds.CAMPO_FROM_ME] == "from_me"
    assert campos[appstate_seeds.CAMPO_ID] == "id"
    assert campos[appstate_seeds.CAMPO_PARTICIPANT] == "participant"


def test_se_reconoce_un_message_key():
    candidato = appstate_seeds.as_message_key(_message_key(CONTACTO, ID_REAL))
    assert candidato is not None
    assert candidato.chat_jid == CONTACTO
    assert candidato.message_id == ID_REAL


def test_una_estructura_que_no_es_message_key_se_descarta():
    """Otro submensaje con campos fuera del molde no cuela."""
    from pywhats.proto import SyncActionValue

    otro = SyncActionValue()
    otro.timestamp = 1_788_400_000
    assert appstate_seeds.as_message_key(otro.SerializeToString()) is None


def test_sin_remote_jid_no_se_puede_demostrar_el_chat():
    """Sin el, la pertenencia seria una suposicion."""
    from pywhats.proto import MessageKey

    clave = MessageKey()
    clave.id = ID_REAL
    assert appstate_seeds.as_message_key(clave.SerializeToString()) is None


def test_sin_id_no_hay_ancla():
    from pywhats.proto import MessageKey

    clave = MessageKey()
    clave.remote_jid = CONTACTO
    assert appstate_seeds.as_message_key(clave.SerializeToString()) is None


# ---------------------------------------------------------------------------
# Validacion
# ---------------------------------------------------------------------------


def test_una_clave_real_se_acepta():
    aceptados = appstate_seeds.inspect(
        _Mutacion(["markChatAsRead", CONTACTO], _accion_con_clave(_message_key(CONTACTO, ID_REAL))),
        collection="regular",
    )
    assert len(aceptados) == 1
    assert aceptados[0].chat_jid == CONTACTO
    assert aceptados[0].collection == "regular"
    assert aceptados[0].index_type == "markChatAsRead"


def test_un_id_sintetico_se_rechaza():
    """El filtro es el MISMO que decide si el backfill puede anclarse."""
    for falso in ("opaque-123", "sintetico-1", "", "1788400000"):
        appstate_seeds.inspect(
            _Mutacion(["markChatAsRead", CONTACTO],
                      _accion_con_clave(_message_key(CONTACTO, falso)))
        )
    assert appstate_seeds.report().real_candidates == 0


def test_una_clave_de_otro_chat_no_ancla_este():
    """Anclar un chat con el mensaje de otro corromperia el historial."""
    appstate_seeds.inspect(
        _Mutacion(["markChatAsRead", CONTACTO], _accion_con_clave(_message_key(OTRO, ID_REAL)))
    )
    informe = appstate_seeds.report()
    assert informe.real_candidates == 0
    assert informe.rejected_wrong_chat == 1


def test_el_mismo_contacto_por_lid_y_por_telefono_es_el_mismo_chat():
    """Un contacto aparece con los dos identificadores; no son chats distintos."""
    appstate_seeds.inspect(
        _Mutacion(["markChatAsRead", "64940106866902@lid"],
                  _accion_con_clave(_message_key("64940106866902@lid", ID_REAL)))
    )
    assert appstate_seeds.report().real_candidates == 1


def test_un_estado_no_ancla_una_conversacion():
    """Los estados no son una conversacion y su historial no se pide igual."""
    appstate_seeds.inspect(
        _Mutacion(["markChatAsRead", "status@broadcast"],
                  _accion_con_clave(_message_key("status@broadcast", ID_REAL)))
    )
    informe = appstate_seeds.report()
    assert informe.real_candidates == 0
    assert informe.rejected_broadcast == 1


def test_un_grupo_se_ancla_por_el_jid_del_grupo():
    aceptados = appstate_seeds.inspect(
        _Mutacion(["markChatAsRead", GRUPO], _accion_con_clave(_message_key(GRUPO, ID_REAL)))
    )
    assert len(aceptados) == 1
    assert aceptados[0].chat_jid == GRUPO


def test_la_misma_clave_dos_veces_se_deduplica():
    for _ in range(3):
        appstate_seeds.inspect(
            _Mutacion(["markChatAsRead", CONTACTO],
                      _accion_con_clave(_message_key(CONTACTO, ID_REAL)))
        )
    informe = appstate_seeds.report()
    assert informe.real_candidates == 1
    assert informe.duplicates == 2


def test_una_mutacion_sin_clave_no_aporta_nada():
    from pywhats.proto import SyncActionValue

    vacia = SyncActionValue()
    vacia.timestamp = 1_788_400_000
    assert appstate_seeds.inspect(_Mutacion(["mute", CONTACTO], vacia)) == []
    assert appstate_seeds.report().mutations_with_message_key == 0


def test_una_mutacion_ilegible_no_revienta():
    assert appstate_seeds.inspect(_Mutacion(["x", "y@lid"], object())) == []
    assert appstate_seeds.inspect(None) == []


# ---------------------------------------------------------------------------
# Modo observacion: no toca nada
# ---------------------------------------------------------------------------


def test_no_escribe_en_la_base_ni_pide_historial():
    """Primero medir. Decidir viene despues, y con datos."""
    import ast
    import inspect as _inspect

    arbol = ast.parse(_inspect.getsource(appstate_seeds))
    nombres = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Attribute):
            nombres.append(nodo.attr)
        elif isinstance(nodo, ast.Name):
            nombres.append(nodo.id)

    for prohibido in (
        "execute", "commit", "add", "transaction", "flush",
        "BackfillService", "SeedRecovery", "enqueue", "_process_chat",
    ):
        assert prohibido not in nombres, (
            f"en modo observacion no puede aparecer {prohibido}"
        )


def test_el_envoltorio_no_altera_el_evento(settings):
    """Se observa lo que pywhats ya autentico y se devuelve lo mismo."""
    import pywhats.appstate.events as events_module

    original = events_module.app_state_mutation_to_event
    try:
        appstate_seeds.apply(settings)
        envuelto = events_module.app_state_mutation_to_event
        assert envuelto is not original

        from pywhats.proto import SyncActionValue

        accion = SyncActionValue()
        accion.timestamp = 1_788_400_000
        accion.mute_action.muted = True
        muestra = _Mutacion(["mute", CONTACTO], accion)

        assert envuelto(muestra) == original(muestra)
    finally:
        events_module.app_state_mutation_to_event = original


def test_no_se_registra_el_identificador_completo(caplog):
    """Se registra una huella corta, no el identificador."""
    import logging

    with caplog.at_level(logging.INFO):
        appstate_seeds.inspect(
            _Mutacion(["markChatAsRead", CONTACTO],
                      _accion_con_clave(_message_key(CONTACTO, ID_REAL))),
            collection="regular",
        )

    texto = "\n".join(r.getMessage() for r in caplog.records)
    assert ID_REAL not in texto
    assert "id_fp=" in texto


def test_el_informe_cuenta_lo_que_hay():
    appstate_seeds.inspect(
        _Mutacion(["markChatAsRead", CONTACTO],
                  _accion_con_clave(_message_key(CONTACTO, ID_REAL)))
    )
    appstate_seeds.inspect(
        _Mutacion(["markChatAsRead", OTRO],
                  _accion_con_clave(_message_key(OTRO, "3A99AABBCCDDEEFF0011")))
    )

    informe = appstate_seeds.report()
    assert informe.mutations_scanned == 2
    assert informe.mutations_with_message_key == 2
    assert informe.real_candidates == 2
    assert informe.unique_chats == 2
    assert "candidatos=2" in informe.resumen()


def test_el_veredicto_se_registra_al_terminar_el_arranque():
    """El recuento tiene que llegar a la terminal, no quedarse en memoria."""
    import inspect as _inspect

    from app.core.orchestrator import Orchestrator

    fuente = _inspect.getsource(Orchestrator.post_connect)
    assert "appstate_seeds" in fuente
    assert "log_summary" in fuente


def test_el_resumen_calla_si_no_hubo_mutaciones(caplog):
    import logging

    with caplog.at_level(logging.INFO):
        appstate_seeds.log_summary()
    assert not [r for r in caplog.records if "app-state" in r.getMessage()]


def test_esta_apagada_por_defecto():
    """Se midio contra la cuenta real y no aparecio ni una clave de mensaje.

    No se retira porque repetir la medicion tiene valor, pero no puede formar
    parte del arranque normal: seria arquitectura sobre un resultado negativo.

    Se comprueba el valor de FABRICA (``.env.example``, que va al repositorio)
    y el de la configuracion sin variables: el ``.env`` local puede tenerla
    encendida a proposito mientras se esta midiendo.
    """
    from pathlib import Path

    ejemplo = Path(".env.example").read_text(encoding="utf-8")
    assert "COMPAT_APPSTATE_SEEDS=false" in ejemplo

    import os

    from app.core.config import _bool

    guardado = os.environ.pop("COMPAT_APPSTATE_SEEDS", None)
    try:
        assert _bool("COMPAT_APPSTATE_SEEDS", False) is False
    finally:
        if guardado is not None:
            os.environ["COMPAT_APPSTATE_SEEDS"] = guardado


def test_no_se_aplica_si_esta_apagada(settings, tmp_path):
    import dataclasses

    from app.compat import apply_all

    aislado = dataclasses.replace(
        settings,
        session_dir=tmp_path / "s",
        compat_appstate_seeds=False,
        compat_windows_store=False,
        compat_pairing_515=False,
        compat_prekey_replay=False,
        compat_history_messages=False,
        compat_wa_version=False,
        pairing_full_sync=False,
    )
    (tmp_path / "s").mkdir(parents=True, exist_ok=True)
    assert "appstate_seeds" not in apply_all(aislado)
