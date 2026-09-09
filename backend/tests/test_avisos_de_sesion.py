"""Quien entra, quien sale, y de quien era la sesion que se cerro.

POR QUE
-------
El log decia esto al entrar alguien::

    [AUTH] Credenciales de Google guardadas (drive=si)
    [AUTH] [AUTH] cookie de sesion emitida (secure=False samesite=Lax)

Ni nombre ni identificador. Con dos personas usando la misma maquina no habia
forma de saber quien habia entrado. Y al cerrar sesion no se escribia nada en
absoluto: el usuario desaparecia y el log se quedaba mudo.

UNA SESION SE CIERRA DE TRES MANERAS
------------------------------------
* la cierra quien la abrio (``logout``);
* la revoca otro sitio (cambio de credencial);
* caduca sola.

Las tres tienen que verse. Las dos ultimas no pasan por ``logout``, asi que se
detectan en la siguiente peticion -- con el frontend sondeando, en segundos.
"""

from __future__ import annotations

import logging
import uuid

import pytest

CLAVE = "una contrasena bastante larga"


def _correo() -> str:
    return f"avisos-{uuid.uuid4().hex[:10]}@example.com"


# ---------------------------------------------------------------------------
# Como se nombra a una persona
# ---------------------------------------------------------------------------


def test_se_nombra_con_el_nombre_Y_el_identificador():
    """El nombre para leerlo; el id para cruzarlo con la base y los runtimes."""
    from types import SimpleNamespace

    from app.auth.service import quien_es

    texto = quien_es(
        SimpleNamespace(
            display_name="Alejandra", email="ale@example.com", id="abcdef12-3456"
        )
    )
    assert "Alejandra" in texto
    assert "abcdef12" in texto


def test_sin_nombre_se_usa_el_correo_RECORTADO():
    """Identifica sin escribir la direccion entera, como el resto del modulo."""
    from types import SimpleNamespace

    from app.auth.service import quien_es

    texto = quien_es(
        SimpleNamespace(display_name=None, email="alejandro@example.com", id="ff00")
    )
    assert "al***@example.com" in texto
    assert "alejandro@example.com" not in texto


def test_sin_usuario_no_se_inventa_uno():
    from app.auth.service import quien_es

    assert quien_es(None) == "desconocido"


# ---------------------------------------------------------------------------
# Entrar
# ---------------------------------------------------------------------------


def test_al_entrar_el_log_dice_QUIEN(runtime, session, caplog):
    correo = _correo()
    with caplog.at_level(logging.INFO, logger="app.auth"):
        inicio = runtime.auth.register(email=correo, password=CLAVE)

    lineas = [r.getMessage() for r in caplog.records if "Sesion iniciada" in r.getMessage()]
    assert lineas, "entrar tiene que dejar constancia"
    assert str(inicio.user_id)[:8] in lineas[0]


def test_entrar_con_contrasena_tambien_lo_dice(runtime, session, caplog):
    """`register`, `login` y Google pasan todos por el mismo sitio."""
    correo = _correo()
    runtime.auth.register(email=correo, password=CLAVE)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.auth"):
        inicio = runtime.auth.login(email=correo, password=CLAVE)

    lineas = [r.getMessage() for r in caplog.records if "Sesion iniciada" in r.getMessage()]
    assert lineas
    assert str(inicio.user_id)[:8] in lineas[0]


def test_el_aviso_de_entrada_no_lleva_la_contrasena(runtime, session, caplog):
    correo = _correo()
    with caplog.at_level(logging.INFO, logger="app.auth"):
        runtime.auth.register(email=correo, password=CLAVE)

    todo = " ".join(r.getMessage() for r in caplog.records)
    assert CLAVE not in todo


# ---------------------------------------------------------------------------
# Salir
# ---------------------------------------------------------------------------


def test_al_cerrar_sesion_el_log_dice_QUIEN(runtime, session, caplog):
    correo = _correo()
    inicio = runtime.auth.register(email=correo, password=CLAVE)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.auth"):
        assert runtime.auth.revocar(inicio.token) is True

    lineas = [r.getMessage() for r in caplog.records if "Sesion cerrada" in r.getMessage()]
    assert lineas, "salir tiene que dejar constancia"
    assert str(inicio.user_id)[:8] in lineas[0]


def test_cerrar_una_sesion_que_no_existe_no_escribe_nada(runtime, session, caplog):
    """Sin esto, un doble clic en "cerrar sesion" escribiria dos veces."""
    with caplog.at_level(logging.INFO, logger="app.auth"):
        assert runtime.auth.revocar("token-que-no-existe") is False

    assert not [r for r in caplog.records if "Sesion cerrada" in r.getMessage()]


def test_cerrarlas_todas_dice_cuantas_y_de_quien(runtime, session, caplog):
    correo = _correo()
    inicio = runtime.auth.register(email=correo, password=CLAVE)
    runtime.auth.login(email=correo, password=CLAVE)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.auth"):
        cerradas = runtime.auth.revocar_todas(inicio.user_id)

    assert cerradas >= 2
    lineas = [r.getMessage() for r in caplog.records if "de golpe" in r.getMessage()]
    assert lineas and str(inicio.user_id)[:8] in lineas[0]


# ---------------------------------------------------------------------------
# La que se cierra SOLA
# ---------------------------------------------------------------------------


def test_una_sesion_revocada_se_avisa_al_detectarla(runtime, session, caplog):
    """No pasa por `logout`, asi que sin esto el log se quedaba mudo."""
    correo = _correo()
    inicio = runtime.auth.register(email=correo, password=CLAVE)
    runtime.auth.revocar(inicio.token)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.auth"):
        assert runtime.auth.resolver(inicio.token) is None

    lineas = [r.getMessage() for r in caplog.records if "revocada" in r.getMessage()]
    assert lineas and str(inicio.user_id)[:8] in lineas[0]


def test_una_sesion_caducada_se_avisa_al_detectarla(runtime, session, caplog):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.auth.crypto import hash_de_token
    from app.models import UserSession

    correo = _correo()
    inicio = runtime.auth.register(email=correo, password=CLAVE)

    fila = session.execute(
        select(UserSession).where(
            UserSession.token_hash == hash_de_token(inicio.token)
        )
    ).scalar_one()
    fila.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    session.flush()

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.auth"):
        assert runtime.auth.resolver(inicio.token) is None

    lineas = [r.getMessage() for r in caplog.records if "caducada" in r.getMessage()]
    assert lineas and str(inicio.user_id)[:8] in lineas[0]


def test_una_cookie_de_otra_instalacion_no_nombra_a_nadie(runtime, session, caplog):
    """No hay a quien nombrar, y no se inventa."""
    with caplog.at_level(logging.INFO, logger="app.auth"):
        assert runtime.auth.resolver("cookie-de-otro-sitio") is None

    lineas = [r.getMessage() for r in caplog.records if "no reconocida" in r.getMessage()]
    assert lineas and "id=" not in lineas[0]


def test_los_rechazos_se_agrupan(runtime, session, caplog):
    """`resolver` corre en CADA peticion: sin agrupar, una linea por sondeo."""
    correo = _correo()
    inicio = runtime.auth.register(email=correo, password=CLAVE)
    runtime.auth.revocar(inicio.token)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.auth"):
        for _ in range(25):
            runtime.auth.resolver(inicio.token)

    lineas = [r for r in caplog.records if "revocada" in r.getMessage()]
    assert len(lineas) == 1, f"veinticinco sondeos, una linea; hubo {len(lineas)}"


# ---------------------------------------------------------------------------
# La sesion de WhatsApp
# ---------------------------------------------------------------------------


def test_el_runtime_dice_de_quien_es(runtime):
    """"La sesion se cerro" sin decir cual no sirve con varias cuentas."""
    runtime.runtime_owner_account_id = "aaaabbbb-cccc-dddd"
    runtime.runtime_owner_user_id = "11112222-3333"

    texto = runtime.de_quien_soy()
    assert "aaaabbbb" in texto and "11112222" in texto


def test_un_runtime_sin_dueno_lo_dice_en_vez_de_inventarlo(runtime):
    runtime.runtime_owner_account_id = None
    runtime.runtime_owner_user_id = None

    assert runtime.de_quien_soy() == "sin dueno todavia"


@pytest.mark.parametrize("evento", ["transport_lost", "disconnected"])
def test_el_cierre_de_la_sesion_de_whatsapp_se_avisa(evento):
    """Y con la cuenta, para poder saber a quien le paso."""
    import inspect

    from app.core.runtime import AppRuntime

    fuente = inspect.getsource(AppRuntime._observar_evento)
    bloque = fuente.split(f'nombre == "{evento}"')[1][:900]
    assert "log.warning" in bloque, evento
    assert "de_quien_soy" in bloque, evento


def test_el_socket_muerto_se_detecta_en_segundos():
    """"Unos pocos segundos": el primer reintento, no minutos.

    Antes lo media el latido del vigilante de pywhats. Ahora la reconexion la
    lleva el worker: la espera empieza en 1 s y crece hasta un tope de 60,
    porque un socket que no levanta no levanta mejor por intentarlo cien
    veces seguidas.
    """
    import re
    from pathlib import Path

    fuente = Path("wa_baileys/worker.js").read_text(encoding="utf-8")
    formula = re.search(r"const espera = Math\.min\((\d+), (\d+) \* 2", fuente)
    assert formula, "no se encontro la espera entre reintentos"
    tope, base = int(formula.group(1)), int(formula.group(2))
    assert base <= 1000, "el primer reintento tiene que ser de segundos"
    assert tope <= 60000, "esperar mas de un minuto es abandonar"
