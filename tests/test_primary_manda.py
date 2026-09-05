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
            signal_store_file=SimpleNamespace(exists=lambda: signal)
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
    runtime.settings.signal_store_file.parent.mkdir(parents=True, exist_ok=True)
    runtime.settings.signal_store_file.write_text("{}", encoding="utf-8")
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
        "web": {"enabled": True, "ready": True, "qr_available": False},
        "recuperacion": {},
        "resumen": {"waiting_seed": 0, "pending": 0, "fetching": 0, "timeout": 0},
        "cola": None,
        "motivo_principal": None,
    }
    argumentos.update(kwargs)
    return _fase_de_onboarding(
        argumentos["principal_listo"],
        argumentos["web"],
        argumentos["recuperacion"],
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
    """La puerta no puede haberse comido las fases de siempre."""
    assert _fase(web={"enabled": True, "ready": False, "qr_available": True}) == (
        "pairing_web"
    )
    assert _fase(cola={"pending": 4, "waiting_for_phone": False}) == (
        "recovering_history"
    )
    assert _fase() == "complete"


# ---------------------------------------------------------------------------
# 3. El código QR del segundo dispositivo
# ---------------------------------------------------------------------------


def test_sin_principal_el_QR_del_segundo_NO_se_genera(
    cliente, companion_encendido, principal_lista
):
    principal_lista.state.set(AppState.NO_SESSION)

    respuesta = cliente.get("/api/v1/web-companion/qr/image")

    assert respuesta.status_code == 409
    cuerpo = respuesta.get_json()
    assert cuerpo["error"]["code"] == "PRIMARY_NOT_READY"
    assert cuerpo["primary_reason"] == NOT_CONNECTED


def test_tampoco_se_entrega_un_QR_ANTERIOR(
    cliente, companion_encendido, principal_lista
):
    """El código guardado de antes de que cayera la principal es el peor caso.

    Es un código que se puede escanear y que llevaría a vincular el
    dispositivo que no toca. Se comprueba que estando la principal lista SÍ se
    sirve, para que la prueba no pase por no haber código.
    """
    companion_encendido._procesar(
        {"event": "state", "state": "qr_required", "qr": "2@abcdefghijklmnop,x,y"}
    )
    assert cliente.get("/api/v1/web-companion/qr/image").status_code == 200

    principal_lista.state.set(AppState.NO_SESSION)

    respuesta = cliente.get("/api/v1/web-companion/qr/image")
    assert respuesta.status_code == 409
    assert respuesta.get_json()["error"]["code"] == "PRIMARY_NOT_READY"


def test_el_estado_dice_que_esta_suspendido_y_no_anuncia_codigo(
    cliente, companion_encendido, principal_lista
):
    """Suspender no es destruir: si ya estaba vinculado lo sigue estando."""
    companion_encendido._procesar(
        {"event": "state", "state": "qr_required", "qr": "2@abcdefghijklmnop,x,y"}
    )
    principal_lista.state.set(AppState.NO_SESSION)

    cuerpo = cliente.get("/api/v1/web-companion/status").get_json()

    assert cuerpo["blocked_by_primary"] is True
    assert cuerpo["primary_reason"] == NOT_CONNECTED
    assert cuerpo["can_start"] is False
    assert cuerpo["qr_available"] is False


def test_con_la_principal_lista_el_estado_no_esta_bloqueado(
    cliente, companion_encendido, principal_lista
):
    cuerpo = cliente.get("/api/v1/web-companion/status").get_json()

    assert cuerpo["blocked_by_primary"] is False
    assert cuerpo["primary_reason"] is None


# ---------------------------------------------------------------------------
# 4. Las rutas que arrancan, sondean o escriben
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ruta",
    [
        "/api/v1/web-companion/start",
        "/api/v1/web-companion/inventory",
        "/api/v1/web-companion/probe",
        "/api/v1/web-companion/seeds/apply",
        "/api/v1/web-companion/inventory/preview",
        "/api/v1/web-companion/inventory/refresh",
    ],
)
def test_sin_principal_ninguna_ruta_del_segundo_dispositivo_avanza(
    cliente, companion_encendido, principal_lista, ruta
):
    """Todas, no solo la del código.

    Sondear y aplicar sin conexión principal cambia estados de recuperación de
    una cuenta que ahora mismo no se puede excavar: deja el trabajo pareciendo
    hecho cuando no lo está.
    """
    principal_lista.state.set(AppState.NO_SESSION)

    respuesta = cliente.post(ruta)

    assert respuesta.status_code == 409
    assert respuesta.get_json()["error"]["code"] == "PRIMARY_NOT_READY"


def test_el_arranque_manual_no_levanta_el_worker_sin_principal(
    cliente, companion_encendido, principal_lista, monkeypatch
):
    """No basta con contestar 409: el worker no puede haberse levantado."""
    arranques = []
    monkeypatch.setattr(
        type(companion_encendido),
        "start",
        lambda self: arranques.append(1) or True,
    )
    principal_lista.state.set(AppState.NO_SESSION)

    cliente.post("/api/v1/web-companion/start")

    assert arranques == []


# ---------------------------------------------------------------------------
# 5. El supervisor no se reinicia solo
# ---------------------------------------------------------------------------


def test_el_supervisor_pregunta_antes_de_reintentar(settings):
    """La causa exacta del fallo medido.

    ``_reintentar`` terminaba en ``self.start()`` sin mirar nada, con espera
    creciente. Cada arranque sin sesión guardada publica un código nuevo, así
    que seguía apareciendo código mucho después de caerse la principal.
    """
    from app.web_companion.supervisor import WebCompanionSupervisor

    supervisor = WebCompanionSupervisor(settings)
    supervisor.runtime = _falso_runtime(estado=AppState.NO_SESSION)

    assert supervisor.permitido() is False

    supervisor.runtime = _falso_runtime()
    assert supervisor.permitido() is True


def test_sin_runtime_cableado_el_supervisor_sigue_usable(settings):
    """Las herramientas y las pruebas lo usan suelto: no se rompe."""
    from app.web_companion.supervisor import WebCompanionSupervisor

    assert WebCompanionSupervisor(settings).permitido() is True


def test_el_reintento_bloqueado_deja_constancia_y_no_arranca(settings, monkeypatch):
    from app.web_companion import supervisor as modulo

    supervisor = modulo.WebCompanionSupervisor(settings)
    supervisor.runtime = _falso_runtime(estado=AppState.NO_SESSION)
    arranques = []
    monkeypatch.setattr(
        modulo.WebCompanionSupervisor,
        "start",
        lambda self: arranques.append(1) or True,
    )
    supervisor._reintentar()

    assert arranques == []
    assert supervisor.snapshot()["state"] == "blocked_by_primary"


# ---------------------------------------------------------------------------
# 5 bis. El arranque del servicio: la causa exacta del fallo medido
# ---------------------------------------------------------------------------


def test_arrancar_el_servicio_NO_levanta_el_segundo_dispositivo(monkeypatch, settings):
    """La causa raiz, y la unica que explica el log observado.

    ``service.py`` levantaba el worker en un hilo nada mas arrancar, sin mirar
    la sesion. Y no podia salir bien: al arrancar, la conexion principal nunca
    esta lista todavia -- se abre despues y en otro hilo. De ahi salieron
    ``[WEB] worker iniciado`` y ``[WEB] QR requerido`` con ``NO_SESSION``.
    """
    import service

    arranques = []
    hilos = []
    companion = SimpleNamespace(
        habilitado=True,
        comprobar_entorno=lambda: (True, "ok"),
        start=lambda: arranques.append(1),
    )
    monkeypatch.setattr(
        service.threading,
        "Thread",
        lambda *a, **k: hilos.append(k.get("name")) or SimpleNamespace(start=lambda: None),
    )

    service._informar_del_web_companion(SimpleNamespace(web_companion=companion))

    assert arranques == [], "el arranque del servicio no puede levantar el worker"
    assert hilos == [], "ni siquiera en segundo plano"


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


def test_el_orquestador_no_arranca_el_segundo_dispositivo_sin_principal():
    """Entre el ``<success>`` y este punto la sesión se puede haber caído."""
    import ast
    import inspect

    from app.core.orchestrator import Orchestrator

    fuente = inspect.getsource(Orchestrator._arrancar_recuperacion_automatica)
    arbol = ast.parse(fuente.lstrip())
    llamadas = {
        n.func.id
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "primary_ready" in llamadas, (
        "el arranque automático tiene que pasar por la misma puerta"
    )


# ---------------------------------------------------------------------------
# 7. El índice
# ---------------------------------------------------------------------------


def test_el_indice_no_reconcilia_nada_sin_principal(session):
    """Descubrir conversaciones sin principal crea filas que nadie va a excavar."""
    from app.web_companion.inventory import InventarioNoDisponible, WebInventoryService

    rt = _falso_runtime(estado=AppState.NO_SESSION)
    rt.database = None
    rt.web_companion = SimpleNamespace(
        habilitado=True,
        vivo=True,
        snapshot=lambda: {"web_client_ready": True},
        enviar=lambda *a, **k: pytest.fail("no se puede haber pedido el índice"),
    )

    with pytest.raises(InventarioNoDisponible) as fallo:
        WebInventoryService(rt).refrescar()

    assert fallo.value.code == "PRIMARY_NOT_READY"


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
