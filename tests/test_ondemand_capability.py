"""``CONFIRMED`` no puede significar "para siempre".

EL FALLO, MEDIDO
----------------
Dos peticiones ON_DEMAND seguidas dieron ACK y ninguna respuesta, con el
transporte sano (sin ``ping failed``, sin ``NotConnected``, sin reconexion).
Y aun asi el arranque decia::

    same_session=True ondemand_capability=CONFIRMED
    Canary omitido: capability ya confirmada para esta sesion

O sea: el sistema daba por buena una capacidad que acababa de incumplirse dos
veces. ``CONFIRMED`` se guardaba una vez y ya no se revisaba.

Ahora hay un tercer estado. Tras dos timeouts REALES seguidos la capacidad
pasa a ``SUSPECT``, el canary vuelve a ejecutarse y una respuesta buena la
devuelve a ``CONFIRMED``. No se borra nada en ningun caso.
"""

from __future__ import annotations

import pytest

from app.services.backfill_service import CAPABILITY_KEY, BackfillService
from tests.test_backfill_accounting import _DatabaseFalsa


@pytest.fixture
def backfill(settings, session, monkeypatch):
    servicio = BackfillService(settings, _DatabaseFalsa(session))
    # Huella fija: la real depende de device.json y aqui no interesa.
    monkeypatch.setattr(servicio, "session_fingerprint", lambda: "huella-prueba")
    return servicio


def _confirmar(backfill, session):
    from app.services import repository as repo

    repo.set_app_state(
        session, CAPABILITY_KEY, {"confirmed": True, "session": "huella-prueba"}
    )
    session.flush()


def test_de_entrada_no_hay_capacidad_confirmada(backfill):
    assert backfill.capability_state() == "UNKNOWN"


def test_una_capacidad_confirmada_se_reconoce(backfill, session):
    _confirmar(backfill, session)
    assert backfill.capability_state() == "CONFIRMED"
    assert backfill.capability_confirmed() is True


def test_un_solo_timeout_no_pone_en_duda_la_capacidad(backfill, session):
    """Un fallo suelto puede ser de ESE chat. No se generaliza."""
    _confirmar(backfill, session)
    backfill._anotar_timeout_real()
    assert backfill.capability_state() == "CONFIRMED"


def test_dos_timeouts_seguidos_la_ponen_en_duda(backfill, session):
    _confirmar(backfill, session)
    backfill._anotar_timeout_real()
    backfill._anotar_timeout_real()
    assert backfill.capability_state() == "SUSPECT"


def test_en_duda_el_canary_vuelve_a_ejecutarse(backfill, session):
    """Era justo lo que faltaba: "Canary omitido" tras dos fallos."""
    _confirmar(backfill, session)
    assert backfill.capability_confirmed() is True

    backfill._anotar_timeout_real()
    backfill._anotar_timeout_real()

    assert backfill.capability_confirmed() is False, (
        "con la capacidad en duda, el canary NO puede omitirse"
    )


def test_poner_en_duda_no_borra_la_confirmacion(backfill, session):
    """No se destruye informacion: se anota la duda encima."""
    from app.services import repository as repo

    _confirmar(backfill, session)
    backfill._anotar_timeout_real()
    backfill._anotar_timeout_real()

    guardado = repo.get_app_state(session, CAPABILITY_KEY)
    assert guardado["confirmed"] is True
    assert guardado["state"] == "SUSPECT"
    assert guardado["session"] == "huella-prueba"


def test_una_respuesta_buena_limpia_la_racha(backfill, session):
    _confirmar(backfill, session)
    backfill._anotar_timeout_real()
    backfill._timeouts_seguidos = 0  # lo que hace una ronda con mensajes nuevos
    backfill._anotar_timeout_real()
    assert backfill.capability_state() == "CONFIRMED"


def test_el_tope_es_de_dos(backfill):
    """Dos es suficiente para descartar un fallo puntual de un solo chat."""
    assert BackfillService.MAX_TIMEOUTS_ANTES_DE_DUDAR == 2


def test_un_corte_de_linea_no_cuenta_como_timeout(backfill, session):
    """Solo los timeouts REALES ponen la capacidad en duda.

    Si la linea se cae, el telefono no tuvo ocasion de contestar y eso no dice
    nada sobre la capacidad.
    """
    import ast
    import inspect
    import textwrap

    fuente = textwrap.dedent(
        inspect.getsource(BackfillService._process_chat_locked)
    )
    arbol = ast.parse(fuente)
    codigo = ast.unparse(arbol)
    rama = codigo.split("_last_transport_lost", 1)[1].split("return", 1)[0]
    assert "_anotar_timeout_real" not in rama


def test_el_estado_de_la_sesion_con_el_telefono_se_puede_consultar(backfill):
    """Antes de cada peticion se registra si hay sesion, y por que direccion.

    Las 73 peticiones que funcionaron salieron con ``enc_type=msg``; las que
    dieron timeout, con ``pkmsg``. Un ``pkmsg`` significa que no habia sesion.
    """
    backfill._client = type(
        "C",
        (),
        {
            "device": type(
                "D",
                (),
                {
                    "jid": type("J", (), {"user": "573002389304", "server": "s.whatsapp.net"}),
                    "lid": "86531142340710.84@lid",
                },
            )
        },
    )()
    estado = backfill.peer_session_state()
    assert set(estado) == {"pn", "lid"}


def test_mirar_la_sesion_no_la_modifica():
    """Solo lectura: se abre el Signal Store en modo ``ro``.

    Se mira el CODIGO, no la prosa: el docstring del metodo explica que hace
    ``migrate_pn_session_to_lid`` y menciona ``delete``, asi que buscarlo como
    texto daria un falso positivo.
    """
    import ast
    import inspect
    import textwrap

    fuente = textwrap.dedent(inspect.getsource(BackfillService.peer_session_state))
    assert "mode=ro" in fuente

    arbol = ast.parse(fuente)
    # Se QUITAN los docstrings del arbol antes de reconstruir el codigo.
    # Reemplazarlos por texto no funciona: ``ast.unparse`` los reindenta y ya
    # no coinciden con el original.
    for nodo in ast.walk(arbol):
        cuerpo = getattr(nodo, "body", None)
        if not isinstance(cuerpo, list) or not cuerpo:
            continue
        primero = cuerpo[0]
        if (
            isinstance(primero, ast.Expr)
            and isinstance(primero.value, ast.Constant)
            and isinstance(primero.value.value, str)
        ):
            cuerpo.pop(0)

    codigo = ast.unparse(arbol)
    for prohibido in ("INSERT", "UPDATE", "DELETE", "migrate_pn_session_to_lid"):
        assert prohibido not in codigo, f"el metodo no puede tocar {prohibido}"
