"""Pedir historial completo al vincular.

POR QUE EXISTE
--------------
pywhats 0.2.0 registra el companion con ``requires_full_sync=False`` y sin
``history_sync_config``, y por eso WhatsApp solo siembra un adelanto: de 40
chats, 32 llegaron sin un solo mensaje. Se verifico comparando los blobs
CRUDOS contra lo persistido (no hay ni un chat con crudo>0 y persistido=0),
asi que no era un fallo del parser.

Estas pruebas comprueban que se anade la configuracion SIN tocar lo que
pywhats eligio a proposito, y que nada de esto roza la criptografia.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.fixture
def props_originales():
    """Devuelve el DeviceProps sin parchear, y restaura al terminar."""
    import pywhats.pairing as pairing_module

    original = pairing_module._device_props
    yield original
    pairing_module._device_props = original


def _parse(datos: bytes):
    from pywhats.proto import DeviceProps

    dp = DeviceProps()
    dp.ParseFromString(datos)
    return dp


def test_sin_el_parche_no_se_pide_historial_completo(props_originales):
    """La linea base: es exactamente el problema que se corrige."""
    dp = _parse(props_originales("prueba"))
    assert dp.requires_full_sync is False
    assert dp.HasField("history_sync_config") is False


def test_el_parche_pide_historial_completo(settings, props_originales):
    from app.compat import history_config

    history_config.apply(settings)
    import pywhats.pairing as pairing_module

    dp = _parse(pairing_module._device_props("prueba"))
    assert dp.requires_full_sync is True
    assert dp.HasField("history_sync_config") is True

    configuracion = dp.history_sync_config
    assert configuracion.full_sync_days_limit == settings.pairing_full_sync_days
    assert configuracion.full_sync_size_mb_limit == settings.pairing_full_sync_size_mb
    assert configuracion.storage_quota_mb == settings.pairing_storage_quota_mb


def test_se_conserva_lo_que_pywhats_puso_a_proposito(settings, props_originales):
    """El os/platform/version imitan a un navegador PARA QUE el servidor no
    rechace el pairing. Sustituir el payload en vez de ampliarlo lo romperia.
    """
    antes = _parse(props_originales("prueba"))

    from app.compat import history_config

    history_config.apply(settings)
    import pywhats.pairing as pairing_module

    despues = _parse(pairing_module._device_props("prueba"))

    assert despues.os == antes.os == "Mac OS"
    assert despues.platform_type == antes.platform_type
    assert despues.version == antes.version


def test_el_parche_es_idempotente(settings, props_originales):
    from app.compat import history_config

    import pywhats.pairing as pairing_module

    history_config.apply(settings)
    una_vez = pairing_module._device_props("prueba")
    history_config.apply(settings)
    dos_veces = pairing_module._device_props("prueba")

    assert una_vez == dos_veces


def test_el_payload_inicial_no_va_en_linea(settings, props_originales):
    """El historial se descarga como blob, que es lo que sabemos procesar.

    En linea dentro de la notificacion, un historial grande no cabria.
    """
    from app.compat import history_config

    history_config.apply(settings)
    import pywhats.pairing as pairing_module

    dp = _parse(pairing_module._device_props("prueba"))
    assert dp.history_sync_config.inline_initial_payload_in_notification is False


def test_los_campos_existen_en_el_protobuf_instalado():
    """No se inventa ningun campo: salen del descriptor del paquete."""
    from pywhats.proto import DeviceProps

    campos = {f.name for f in DeviceProps.DESCRIPTOR.fields}
    assert {"requires_full_sync", "history_sync_config"} <= campos

    sync = {f.name for f in DeviceProps.HistorySyncConfig.DESCRIPTOR.fields}
    assert {
        "full_sync_days_limit",
        "full_sync_size_mb_limit",
        "storage_quota_mb",
        "inline_initial_payload_in_notification",
    } <= sync


def test_no_se_toca_criptografia_ni_el_flujo_de_pairing():
    """El parche solo anade metadatos al registro. Nada mas."""
    modulo = Path("app/compat/history_config.py").read_text(encoding="utf-8")
    arbol = ast.parse(modulo)

    importados = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            importados.extend(a.name for a in nodo.names)
        elif isinstance(nodo, ast.ImportFrom):
            importados.append(nodo.module or "")

    for prohibido in ("cryptography", "hashlib", "hmac", "os.urandom"):
        assert not any(prohibido in i for i in importados), (
            f"el parche no puede tocar criptografia ({prohibido})"
        )
    # Y no reimplementa el pairing: solo envuelve _device_props.
    assert "_device_props" in modulo

    # Se mira el CODIGO, no la prosa: el modulo menciona 'pair-device-sign' y
    # el '515' justamente en la explicacion de que NO los toca, y buscarlos
    # como cadena daria un falso positivo.
    codigo = ast.unparse(arbol)
    # ``ast.unparse`` conserva los docstrings: se quitan uno a uno.
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(nodo)
            if doc:
                codigo = codigo.replace(doc, "")

    for prohibido in ("pair-device-sign", "adv_secret", "noise_private"):
        assert prohibido not in codigo, (
            f"el parche no puede tocar {prohibido}"
        )


def test_se_puede_desactivar(settings, tmp_path):
    """``PAIRING_FULL_SYNC=false`` vuelve al comportamiento anterior.

    Se desactivan TAMBIEN las demas compatibilidades y se apunta la sesion a
    un temporal: ``apply_all`` con los settings de produccion aplicaria todos
    los parches, y uno de ellos abre el registro de prekeys dentro de
    ``session/``. Una prueba no puede tocar eso.
    """
    import dataclasses

    from app.compat import apply_all

    aislado = dataclasses.replace(
        settings,
        pairing_full_sync=False,
        session_dir=tmp_path,
        compat_windows_store=False,
        compat_pairing_515=False,
        compat_prekey_replay=False,
        compat_history_messages=False,
        compat_wa_version=False,
    )
    assert "full_history" not in apply_all(aislado)

    encendido = dataclasses.replace(aislado, pairing_full_sync=True)
    assert "full_history" in apply_all(encendido)


def test_solo_afecta_a_una_vinculacion_nueva():
    """Queda escrito: con sesion establecida esto no hace nada."""
    modulo = Path("app/compat/history_config.py").read_text(encoding="utf-8")
    assert "vinculacion" in modulo.lower()
    assert "nueva" in modulo.lower()
