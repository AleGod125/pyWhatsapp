"""Para entrar al panel hacen falta las DOS cosas. Y la verdad la dice el vivo.

EL FALLO, MEDIDO
----------------
Había dos fuentes de verdad que se contradecían:

    el guard  ->  /onboarding/status  ->  columna `session_status`
    el panel  ->  /session            ->  estado VIVO del runtime

La columna sobrevive a una sesión revocada. El usuario desvinculó desde el
teléfono, llegaron los 401, y hasta que se archivara la sesión la fila seguía
diciendo ``linked``. Resultado: el onboarding contestaba ``dashboard``, el
guard dejaba entrar, y el panel enseñaba un cartel de «vuelve a vincular»
dentro de una pantalla que no podía funcionar.

LO QUE PROTEGEN ESTAS PRUEBAS
-----------------------------
* que un estado sin vinculación mande a ``pairing`` aunque la fila diga otra
  cosa;
* que una desconexión pasajera **no** lo haga — el socket se cae solo, y
  mandar al código QR por eso es pedir que se rehaga algo que no está roto;
* que no se le quite la vinculación a un usuario por la sesión de otro.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.auth_routes import SIN_VINCULACION, _sin_vinculacion_utilizable


def _runtime(estado: str, dueno=None):
    return SimpleNamespace(
        state=SimpleNamespace(state=SimpleNamespace(value=estado)),
        runtime_owner_user_id=dueno,
    )


YO = "usuario-1"
OTRO = "usuario-2"


@pytest.mark.parametrize("estado", sorted(SIN_VINCULACION))
def test_sin_vinculacion_no_se_puede_decir_que_la_hay(estado):
    """LA REGLA. Da igual lo que diga la fila."""
    usuario = SimpleNamespace(id=YO)
    assert _sin_vinculacion_utilizable(_runtime(estado, YO), usuario) is True


@pytest.mark.parametrize("estado", ["CONNECTED", "DISCONNECTED", "RECONNECTING"])
def test_una_desconexion_pasajera_NO_desvincula(estado):
    """El socket se cae constantemente: red, suspension, cambio de wifi.

    Mandar al codigo QR por eso seria pedirle al usuario que rehaga algo que
    no esta roto, y por eso `disconnected` sigue contando como vinculada.
    """
    usuario = SimpleNamespace(id=YO)
    assert _sin_vinculacion_utilizable(_runtime(estado, YO), usuario) is False


def test_la_sesion_de_OTRO_no_me_quita_la_mia():
    """Nunca se le quita la vinculacion a alguien por lo que le pase a otro."""
    usuario = SimpleNamespace(id=YO)
    assert _sin_vinculacion_utilizable(_runtime("SESSION_INVALID", OTRO), usuario) is False


def test_sin_dueno_todavia_el_estado_si_aplica():
    """El runtime arranco y no ha vinculado a nadie: no hay sesion de nadie."""
    usuario = SimpleNamespace(id=YO)
    assert _sin_vinculacion_utilizable(_runtime("NO_SESSION", None), usuario) is True


def test_un_estado_ilegible_no_concluye_nada():
    """Sin poder leer el estado no se toma ninguna decision."""
    usuario = SimpleNamespace(id=YO)
    roto = SimpleNamespace(state=None, runtime_owner_user_id=YO)
    assert _sin_vinculacion_utilizable(roto, usuario) is False


def test_reconnecting_no_esta_en_la_lista():
    """Explicito porque es el que mas facil se cuela por error."""
    assert "RECONNECTING" not in SIN_VINCULACION
    assert "DISCONNECTED" not in SIN_VINCULACION
    assert "CONNECTED" not in SIN_VINCULACION


def test_los_estados_que_si_exigen_escanear():
    """Los unicos en los que de verdad hay que ir al codigo QR."""
    assert SIN_VINCULACION == {
        "NO_SESSION",
        "PAIRING",
        "PAIRING_REQUIRED",
        "QR_READY",
        "SESSION_INVALID",
    }
