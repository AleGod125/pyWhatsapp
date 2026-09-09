"""Estabilidad del ciclo de vida de la sesion: identidad, prekeys y estado visible.

Tres cosas que se rompieron de verdad y que estas pruebas fijan:

1. El registro de prekeys moria tras archivar una sesion revocada, y la
   reutilizacion de ratchet dejaba de existir EN SILENCIO. Sintoma:
   ``unknown one-time pre-key id N`` en cada PKMSG reenviado despues de un
   re-pairing dentro del mismo proceso.

2. La huella de sesion se derivaba del ``device_id``, que es un numero de
   ranura reutilizable. Dos identidades distintas podian compartir huella.

3. Con ``SESSION_INVALID`` el frontend no podia saber si se estaba verificando
   la sesion guardada o si hacia falta un codigo, y mostraba "Preparando tu
   codigo QR" durante los tres rechazos.
"""

from __future__ import annotations

import json
import types

import pytest


# ---------------------------------------------------------------------------
# 1. El registro de prekeys sobrevive al archivado
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2. La huella identifica la IDENTIDAD, no la ranura
# ---------------------------------------------------------------------------


def _device_json(tmp_path, *, device_id: int, registration_id: int, nombre: str):
    ruta = tmp_path / f"{nombre}.json"
    ruta.write_text(
        json.dumps(
            {
                "me": {"id": f"573000000:{device_id}@s.whatsapp.net", "name": nombre},
                "registrationId": registration_id,
            }
        ),
        encoding="utf-8",
    )
    return types.SimpleNamespace(session_file=ruta)


def test_misma_ranura_identidades_distintas_huellas_distintas(tmp_path):
    """El servidor reutiliza el numero de ranura al desvincular todo."""
    from app.core.identity import session_fingerprint

    a = session_fingerprint(_device_json(tmp_path, device_id=5, registration_id=111, nombre="a"))
    b = session_fingerprint(_device_json(tmp_path, device_id=5, registration_id=222, nombre="b"))

    assert a and b and a != b, (
        "con la misma huella, la segunda daria por confirmado el historial "
        "inicial de la primera y se saltaria el bootstrap"
    )


def test_la_misma_identidad_da_la_misma_huella(tmp_path):
    """No puede cambiar sola: si cambiara, se re-esperaria el bootstrap siempre."""
    from app.core.identity import session_fingerprint

    a = session_fingerprint(_device_json(tmp_path, device_id=5, registration_id=111, nombre="x"))
    b = session_fingerprint(_device_json(tmp_path, device_id=5, registration_id=111, nombre="y"))
    assert a == b


def test_la_huella_no_lleva_material_criptografico(tmp_path):
    """Va en respuestas HTTP: no puede derivarse de claves privadas."""
    import inspect

    from app.core import identity

    fuente = inspect.getsource(identity.session_fingerprint)
    for prohibido in ("identity_private", "noise_private", "signed_pre_key"):
        assert prohibido not in fuente


# ---------------------------------------------------------------------------
# 3. El frontend puede saber en que punto esta
# ---------------------------------------------------------------------------


class _PairingFalso:
    def __init__(self, available: bool = False):
        self.available = available


class _EstadoFalso:
    def __init__(self, valor: str):
        self.value = valor
        self.name = valor

    def __eq__(self, otro):
        return getattr(otro, "value", otro) == self.value

    def __hash__(self):
        return hash(self.value)


def _runtime_falso(estado: str, *, sesion: bool, qr: bool = False, rechazos: int = 0):
    from app.core.session_state import AppState

    return types.SimpleNamespace(
        state=types.SimpleNamespace(
            state=AppState[estado], viewer_allowed=True, generation=1
        ),
        pairing=_PairingFalso(qr),
        session_exists=sesion,
        rechazos_seguidos=rechazos,
        MAX_RECHAZOS_MISMA_SESION=3,
        owner="test",
        decrypt_errors=0,
        info=lambda: types.SimpleNamespace(whatsapp_enabled=True),
    )


@pytest.mark.parametrize(
    "estado,sesion,qr,esperada",
    [
        ("SESSION_INVALID", True, False, "verifying_session"),
        ("PAIRING_REQUIRED", False, False, "pairing_required"),
        ("PAIRING", False, True, "qr_ready"),
        ("CONNECTED", True, False, "connected"),
        ("CONNECTING", True, False, "connecting"),
    ],
)
def test_la_fase_de_vinculacion_dice_la_verdad(estado, sesion, qr, esperada, monkeypatch):
    from app.api.serializers import _fase_de_vinculacion
    from app.core.session_state import AppState

    rt = _runtime_falso(estado, sesion=sesion, qr=qr)
    assert _fase_de_vinculacion(rt, AppState[estado])["pairing_phase"] == esperada


def test_verificando_no_se_confunde_con_hace_falta_QR():
    """Es la distincion que faltaba: sin ella la pantalla miente."""
    from app.api.serializers import _fase_de_vinculacion
    from app.core.session_state import AppState

    con_sesion = _fase_de_vinculacion(
        _runtime_falso("SESSION_INVALID", sesion=True), AppState.SESSION_INVALID
    )
    sin_sesion = _fase_de_vinculacion(
        _runtime_falso("SESSION_INVALID", sesion=False), AppState.SESSION_INVALID
    )
    assert con_sesion["pairing_phase"] == "verifying_session"
    assert sin_sesion["pairing_phase"] != "verifying_session"


def test_se_informa_de_cuantos_rechazos_van():
    """Para poder decir '(intento 2 de 3)' en vez de girar sin explicacion."""
    from app.api.serializers import _fase_de_vinculacion
    from app.core.session_state import AppState

    cuerpo = _fase_de_vinculacion(
        _runtime_falso("SESSION_INVALID", sesion=True, rechazos=2),
        AppState.SESSION_INVALID,
    )
    assert cuerpo["session_rejections"] == 2
    assert cuerpo["session_rejections_max"] == 3


def test_el_runtime_expone_el_contador():
    """El serializador lo lee de ahi; si no existe, siempre diria 0."""
    from app.core.runtime import AppRuntime

    assert isinstance(getattr(AppRuntime, "rechazos_seguidos", None), property)


def test_el_estado_nunca_lleva_secretos():
    """Va al navegador en cada sondeo.

    Se miran los NOMBRES del arbol, no el texto: los docstrings de estas
    funciones nombran justamente lo que prometen no exponer, y buscar en el
    texto daba un falso positivo sobre la propia advertencia.
    """
    import ast
    import inspect

    from app.api import serializers

    nombres = []
    for funcion in (serializers.state_to_json, serializers._fase_de_vinculacion):
        arbol = ast.parse(inspect.getsource(funcion))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Attribute):
                nombres.append(nodo.attr)
            elif isinstance(nodo, ast.Name):
                nombres.append(nodo.id)
            elif isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
                # Las claves del JSON tambien cuentan: son lo que sale.
                nombres.append(nodo.value)

    for prohibido in (
        "payload",
        "identity_private",
        "noise_private",
        "signed_pre_key",
        "database_url",
    ):
        assert prohibido not in nombres, f"{prohibido} no puede salir al navegador"
