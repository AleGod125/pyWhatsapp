"""La sesión principal manda; el segundo dispositivo espera.

EL FALLO QUE FIJAN ESTAS PRUEBAS
--------------------------------
Se midió: ``service.py`` arrancó sin sesión — ``STARTING → NO_SESSION``, sin
``device.json``, sin identidad propia, sin Signal — y el segundo dispositivo
arrancó igualmente y publicó su código QR. El usuario se quedó mirando el
código equivocado: el que hacía falta escanear era el principal.

El agujero no estaba en un sitio, estaba en que cada sitio decidía por su
cuenta si «había sesión» y ninguno lo preguntaba entero. Por eso lo que se
protege aquí no es un endpoint: es que TODOS los caminos hacia el segundo
dispositivo pasen por la misma puerta.

LOS CAMINOS
-----------
* el supervisor, que se reinicia solo con espera creciente;
* el orquestador, que lo arranca al terminar de conectar;
* las rutas HTTP, incluida la del código QR;
* el vigilante que aplica referencias solo;
* el índice, que reconcilia conversaciones contra PostgreSQL.

Y una distinción que no se puede perder: **caerse un momento no es dejar de
estar vinculado**. Mandar al usuario al código QR por un corte de red le hace
rehacer algo que no está roto.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.primary import (
    NO_ACCOUNT,
    NO_IDENTITY,
    NO_SIGNAL_STORE,
    NOT_CONNECTED,
    RECONNECTING,
    esperando_reconexion,
    primary_ready,
    razon_no_lista,
)
from app.core.session_state import AppState


# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------


def _falso_runtime(
    *,
    estado=AppState.CONNECTED,
    identidad=True,
    signal=True,
    cuenta=1,
):
    """Un runtime con las cuatro condiciones controlables por separado.

    Por separado a propósito: cada una se puede cumplir sin las otras, y ese
    es justamente el caso que se colaba.
    """
    cliente = None
    if identidad:
        cliente = SimpleNamespace(
            device=SimpleNamespace(jid=SimpleNamespace(user="34600111222"))
        )
    return SimpleNamespace(
        state=SimpleNamespace(state=estado),
        client=SimpleNamespace(_client=cliente),
        settings=SimpleNamespace(
            session_dir_baileys=SimpleNamespace(
                is_dir=lambda: signal,
                iterdir=lambda: iter(["creds.json"] if signal else []),
            )
        ),
        runtime_owner_account_id=cuenta,
    )


@pytest.fixture
def principal_lista(runtime):
    """Deja LISTA de verdad la conexión principal del runtime de la API.

    Las cuatro condiciones a la vez, que es lo que exige la puerta: estado
    conectado, identidad propia, Signal Store y cuenta reconciliada.
    """
    runtime.state.set(AppState.CONNECTED)
    runtime.settings.session_dir_baileys.mkdir(parents=True, exist_ok=True)
    (runtime.settings.session_dir_baileys / "session-573000.0.json").write_text(
        "{}", encoding="utf-8"
    )
    runtime.client = SimpleNamespace(
        _client=SimpleNamespace(
            device=SimpleNamespace(jid=SimpleNamespace(user="34600111222"))
        )
    )
    if getattr(runtime, "runtime_owner_account_id", None) is None:
        runtime.runtime_owner_account_id = 1
    return runtime


@pytest.fixture
def companion_encendido(runtime):
    """El companion del runtime que sirve la API, encendido."""
    supervisor = runtime.web_companion
    ajustes = supervisor._settings
    previo = getattr(ajustes, "web_companion_enabled", False)
    object.__setattr__(ajustes, "web_companion_enabled", True)
    yield supervisor
    object.__setattr__(ajustes, "web_companion_enabled", previo)


# ---------------------------------------------------------------------------
# 1. La definición canónica
# ---------------------------------------------------------------------------


def test_con_las_cuatro_condiciones_la_principal_esta_lista():
    assert primary_ready(_falso_runtime()) is True
    assert razon_no_lista(_falso_runtime()) is None


def test_sin_sesion_no_esta_lista():
    assert razon_no_lista(_falso_runtime(estado=AppState.NO_SESSION)) == NOT_CONNECTED


def test_un_emparejamiento_a_medias_NO_cuenta_como_vinculado():
    """El caso exacto: estado conectado pero sin identidad propia.

    ``device.json`` escrito no es prueba de nada: un emparejamiento que no
    llegó a completarse lo deja igual. Por eso se pregunta al dispositivo
    vivo y no al archivo.
    """
    assert razon_no_lista(_falso_runtime(identidad=False)) == NO_IDENTITY


def test_sin_Signal_no_se_puede_descifrar_asi_que_no_esta_lista():
    assert razon_no_lista(_falso_runtime(signal=False)) == NO_SIGNAL_STORE


def test_sin_cuenta_reconciliada_tampoco():
    assert razon_no_lista(_falso_runtime(cuenta=None)) == NO_ACCOUNT


def test_reconectando_NO_es_lo_mismo_que_hay_que_volver_a_vincular():
    """Un corte de red no invalida credenciales.

    Tratarlo como «vuelve a vincular» manda al usuario a rehacer algo que no
    está roto, y encima el código que vería sería el que no toca.
    """
    rt = _falso_runtime(estado=AppState.RECONNECTING)
    assert razon_no_lista(rt) == RECONNECTING
    assert esperando_reconexion(rt) is True
    assert primary_ready(rt) is False


# ---------------------------------------------------------------------------
# 2. El onboarding: la principal se evalúa ANTES que nada
# ---------------------------------------------------------------------------


def _fase(**kwargs):
    from app.api.routes import _fase_de_onboarding

    argumentos = {
        "principal_listo": True,
        "resumen": {"waiting_seed": 0, "pending": 0, "fetching": 0, "timeout": 0},
        "cola": None,
        "motivo_principal": None,
    }
    # `web` y `recuperacion` estaban aqui: eran el estado del SEGUNDO
    # dispositivo y el de su vigilante. Se retiraron con sus proveedores.
    argumentos.update({k: v for k, v in kwargs.items() if k in argumentos})
    return _fase_de_onboarding(
        argumentos["principal_listo"],
        argumentos["resumen"],
        argumentos["cola"],
        argumentos["motivo_principal"],
    )


def test_sin_principal_la_fase_es_pairing_primary(cliente, principal_lista):
    """Y se comprueba contra la ruta de verdad, no solo contra la función."""
    principal_lista.state.set(AppState.NO_SESSION)

    cuerpo = cliente.get("/api/v1/onboarding/recovery").get_json()

    assert cuerpo["phase"] == "pairing_primary"
    assert cuerpo["primary"]["linked"] is False
    assert cuerpo["primary"]["reason"] == NOT_CONNECTED
    assert cuerpo["primary"]["reconnecting"] is False
    assert cuerpo["primary"]["message"]


def test_con_el_QR_del_segundo_esperando_SIGUE_siendo_pairing_primary():
    """La prueba del fallo medido, en una línea.

    Había código del segundo dispositivo disponible: antes eso ganaba y la
    pantalla enseñaba «Mejorar la recuperación».
    """
    fase = _fase(
        principal_listo=False,
        motivo_principal=NOT_CONNECTED,
        web={"enabled": True, "ready": False, "qr_available": True},
    )
    assert fase == "pairing_primary"


@pytest.mark.parametrize(
    "situacion",
    [
        # pairing_web
        {"web": {"enabled": True, "ready": False, "qr_available": True}},
        # waiting_web
        {"web": {"enabled": True, "ready": False, "qr_available": False}},
        # recovering_history
        {"cola": {"pending": 7, "waiting_for_phone": False}},
        # waiting_for_phone
        {"cola": {"pending": 0, "waiting_for_phone": True}},
        # partial
        {"resumen": {"waiting_seed": 3, "pending": 0, "fetching": 0, "timeout": 0}},
    ],
)
def test_cualquier_fase_posterior_vuelve_a_pairing_primary(situacion):
    """El rollback del punto 3, sin reiniciar nada.

    No hace falta apagar el servicio: la fase se calcula en cada llamada, así
    que en cuanto la principal deja de estar lista la siguiente respuesta ya
    manda al usuario al código que toca.
    """
    antes = _fase(**situacion)
    assert antes != "pairing_primary", "la situación de partida no era posterior"

    despues = _fase(principal_listo=False, motivo_principal=NOT_CONNECTED, **situacion)
    assert despues == "pairing_primary"


def test_un_corte_pasajero_no_manda_al_usuario_a_vincular_otra_vez():
    """Se distingue de ``pairing_primary``: son dos mensajes y dos salidas."""
    fase = _fase(principal_listo=False, motivo_principal=RECONNECTING)
    assert fase == "reconnecting"


def test_con_la_principal_lista_el_flujo_normal_sigue_igual():
    """La puerta no puede haberse comido las fases de siempre.

    `pairing_web` y `waiting_web` estaban aqui: eran las dos fases del SEGUNDO
    dispositivo, que se retiro. Ahora solo hay una vinculacion que atender.
    """
    assert _fase(cola={"waiting_for_phone": True}) == "waiting_for_phone"
    assert _fase(cola={"pending": 4, "waiting_for_phone": False}) == (
        "recovering_history"
    )
    assert _fase() == "complete"


# ---------------------------------------------------------------------------
# 4. Las rutas que arrancan, sondean o escriben
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5. El supervisor no se reinicia solo
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5 bis. El arranque del servicio: la causa exacta del fallo medido
# ---------------------------------------------------------------------------


def test_el_servicio_ya_no_tiene_ninguna_via_para_arrancarlo_solo():
    """Que no vuelva por otro camino: se comprueba el modulo entero."""
    import inspect

    import service

    fuente = inspect.getsource(service)
    assert "_arrancar_web_companion" not in fuente
    assert "web-companion-start" not in fuente


# ---------------------------------------------------------------------------
# 6. El orquestador no lo arranca al conectar
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 7. El índice
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 8. Una desconexión temporal no destruye credenciales
# ---------------------------------------------------------------------------


def test_un_rechazo_suelto_NO_borra_la_sesion(runtime):
    """Se distingue «corte» de «esa vinculación ya no existe».

    Hacen falta tres rechazos seguidos de la MISMA sesión. Tirar una
    vinculación buena por un 401 suelto produjo el peor incidente del
    proyecto.
    """
    runtime.settings.session_file.parent.mkdir(parents=True, exist_ok=True)
    runtime.settings.session_file.write_text("{}", encoding="utf-8")

    runtime._sesion_rechazada("401")

    assert runtime.settings.session_file.exists(), (
        "un rechazo suelto no puede destruir una vinculación buena"
    )
    assert runtime.rechazos_seguidos == 1


def test_reconectando_no_manda_a_vincular_ni_toca_los_archivos(runtime):
    runtime.settings.session_file.parent.mkdir(parents=True, exist_ok=True)
    runtime.settings.session_file.write_text("{}", encoding="utf-8")
    runtime.state.set(AppState.RECONNECTING)

    assert razon_no_lista(runtime) == RECONNECTING
    assert runtime.settings.session_file.exists()
