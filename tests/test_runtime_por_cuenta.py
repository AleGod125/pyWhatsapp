"""El runtime que contesta es el de QUIEN PREGUNTA.

EL FALLO QUE CIERRA
-------------------
Las rutas leian el runtime unico del proceso, asi que con la cuenta de A
conectada el usuario B --recien registrado, sin cuenta ninguna-- recibia::

    POST /session/pair  -> 409 SESSION_ALREADY_CONNECTED   (el estado de A)
    GET  /session/qr    -> el codigo QR de A

Lo segundo es lo grave: el codigo QR es la llave para vincular un dispositivo
a una cuenta. Servirle a alguien el de otra persona es entregarle esa cuenta.

LA CADENA
---------
::

    usuario autenticado  ->  membresia  ->  whatsapp_account  ->  runtime

Nunca "el runtime que haya".
"""

from __future__ import annotations

import uuid

import pytest

from app.core.runtime_registry import RuntimeRegistry


class _Emparejamiento:
    """Un emparejamiento con su propio codigo. Lo justo para distinguirlos."""

    def __init__(self, codigo: str):
        self.available = True
        self.expired = False
        self.generation = 1
        self._codigo = codigo

    def payload(self):
        return self._codigo


class _RuntimeFalso:
    def __init__(self, settings, database, account_id):
        self.settings = settings
        self.database = database
        self.runtime_owner_account_id = account_id
        # El codigo lleva el identificador de su cuenta: si dos runtimes
        # devolvieran el mismo, se veria al instante.
        self.pairing = _Emparejamiento(f"qr-de-{account_id}")
        self.parado = False

    def stop(self):
        self.parado = True


A = uuid.uuid4()
B = uuid.uuid4()


@pytest.fixture
def ajustes(settings, tmp_path):
    import dataclasses

    return dataclasses.replace(settings, session_dir=tmp_path / "session")


@pytest.fixture
def registro(ajustes, database):
    return RuntimeRegistry(ajustes, database, fabrica=_RuntimeFalso)


# ---------------------------------------------------------------------------
# Cada cuenta, su runtime y su codigo
# ---------------------------------------------------------------------------


def test_A_OBTIENE_EL_SUYO_Y_B_EL_SUYO(registro):
    rt_a = registro.get_or_start(A)
    rt_b = registro.get_or_start(B)

    assert rt_a is registro.get(A)
    assert rt_b is registro.get(B)
    assert rt_a is not rt_b


def test_A_NO_OBTIENE_EL_RUNTIME_DE_B(registro):
    """La comprobacion al reves, que es la que de verdad protege."""
    rt_a = registro.get_or_start(A)
    rt_b = registro.get_or_start(B)

    assert registro.get(A) is not rt_b
    assert registro.get(B) is not rt_a


def test_EL_CODIGO_QR_DE_A_NO_ES_EL_DE_B(registro):
    """LA PRUEBA QUE MAS IMPORTA.

    El codigo QR es la llave para vincular un dispositivo a una cuenta.
    Servirle a alguien el de otra persona es entregarle esa cuenta entera.
    """
    qr_a = registro.get_or_start(A).pairing.payload()
    qr_b = registro.get_or_start(B).pairing.payload()

    assert qr_a != qr_b
    assert str(A) in qr_a and str(B) in qr_b


def test_QUE_A_ESTE_CONECTADA_NO_IMPIDE_EMPAREJAR_B(registro):
    """El estado CONNECTED de A pertenece a A, y a nadie mas.

    Antes se leia el estado del runtime unico, asi que con A conectada la
    peticion de B moria en un 409 que hablaba de una sesion que no era suya.
    """
    rt_a = registro.get_or_start(A)
    rt_a.estado = "CONNECTED"

    rt_b = registro.get_or_start(B)

    assert rt_b is not rt_a
    assert getattr(rt_b, "estado", None) is None, (
        "el estado de A no puede alcanzar al runtime de B"
    )
    assert rt_b.pairing.available is True


# ---------------------------------------------------------------------------
# Adoptar el runtime que ya corre
# ---------------------------------------------------------------------------


def test_EL_RUNTIME_QUE_YA_CORRE_SE_ADOPTA_NO_SE_REHACE(registro, ajustes, database):
    """Rehacerlo dejaria a la cuenta viva sin poder abrir su Signal Store.

    Cuando esto se estreno ya habia un runtime en marcha, con su sesion
    abierta y su cerrojo tomado. Construirle otro apuntando a la carpeta nueva
    significaria dos clientes sobre la misma identidad.
    """
    vivo = _RuntimeFalso(ajustes, database, A)

    adoptado = registro.adoptar(A, vivo)

    assert adoptado is vivo
    assert registro.get(A) is vivo
    # Y pedirlo despues devuelve EL MISMO, no uno nuevo.
    assert registro.get_or_start(A) is vivo


def test_adoptar_una_cuenta_no_toca_las_demas(registro, ajustes, database):
    rt_b = registro.get_or_start(B)
    registro.adoptar(A, _RuntimeFalso(ajustes, database, A))

    assert registro.get(B) is rt_b
    assert rt_b.parado is False


def test_adoptar_sin_cuenta_o_sin_runtime_no_hace_nada(registro):
    assert registro.adoptar(None, object()) is None
    assert registro.adoptar(A, None) is None


# ---------------------------------------------------------------------------
# Crear B no toca A
# ---------------------------------------------------------------------------


def test_CREAR_B_NO_MODIFICA_A(registro):
    rt_a = registro.get_or_start(A)
    qr_a = rt_a.pairing.payload()

    registro.get_or_start(B)

    assert registro.get(A) is rt_a
    assert rt_a.pairing.payload() == qr_a, "el codigo de A no se regenera"
    assert rt_a.parado is False


def test_PARAR_B_NO_AFECTA_A(registro):
    rt_a = registro.get_or_start(A)
    registro.get_or_start(B)

    registro.stop(B)

    assert registro.get(A) is rt_a
    assert rt_a.parado is False
    assert rt_a.pairing.available is True


# ---------------------------------------------------------------------------
# El bus de eventos
# ---------------------------------------------------------------------------


def _bus_por_cuenta(ajustes, database):
    from app.events.bus import EventBus

    class _ConBus(_RuntimeFalso):
        def __init__(self, settings, database, account_id):
            super().__init__(settings, database, account_id)
            self.bus = EventBus()

    return RuntimeRegistry(ajustes, database, fabrica=_ConBus)


def test_UN_EVENTO_DE_A_NO_LLEGA_AL_SUSCRIPTOR_DE_B(ajustes, database):
    """LA PRUEBA DEL AISLAMIENTO DE SSE.

    Antes el endpoint escuchaba el bus del runtime base --uno para todo el
    proceso-- y lo que separaba a las personas era un filtro que tapaba los
    eventos ajenos uno a uno. Tapar no es aislar: basta que un evento nuevo no
    entre en la lista para que empiece a llegarle a quien no debe.

    Escuchando el bus de SU cuenta, los eventos de la otra no existen en esa
    conexion.
    """
    registro = _bus_por_cuenta(ajustes, database)
    bus_a = registro.get_or_start(A).bus
    bus_b = registro.get_or_start(B).bus

    with bus_b.subscribe() as de_b, bus_a.subscribe() as de_a:
        bus_a.publish("message.created", {"chat_id": 1})

        assert de_a.get(timeout=0.5) is not None, "A si recibe lo suyo"
        assert de_b.get(timeout=0.2) is None, (
            "un evento de A no puede llegar al cliente de B"
        )


def test_UN_EVENTO_DE_B_NO_LLEGA_AL_SUSCRIPTOR_DE_A(ajustes, database):
    """La comprobacion simetrica, que es la que suele faltar."""
    registro = _bus_por_cuenta(ajustes, database)
    bus_a = registro.get_or_start(A).bus
    bus_b = registro.get_or_start(B).bus

    with bus_a.subscribe() as de_a, bus_b.subscribe() as de_b:
        bus_b.publish("chat.updated", {"chat_id": 7})

        assert de_b.get(timeout=0.5) is not None
        assert de_a.get(timeout=0.2) is None


def test_RECONECTAR_CONSERVA_EL_AISLAMIENTO(ajustes, database):
    """Volver a suscribirse no puede devolver eventos de la otra cuenta."""
    registro = _bus_por_cuenta(ajustes, database)
    bus_a = registro.get_or_start(A).bus
    bus_b = registro.get_or_start(B).bus

    bus_a.publish("message.created", {"chat_id": 1})
    # B se conecta DESPUES, y pide que le repitan lo reciente.
    with bus_b.subscribe(replay=True) as de_b:
        assert de_b.get(timeout=0.2) is None, (
            "ni siquiera al repetir lo reciente puede aparecer lo de A"
        )


def test_CADA_RUNTIME_TIENE_SU_PROPIO_BUS(ajustes, database):
    """El aislamiento de SSE sale de aqui.

    Cada `AppRuntime` construye su bus como atributo suyo, asi que dos
    runtimes distintos ya son dos buses distintos. Lo que faltaba no era el
    bus: era que cada persona se suscribiera al de SU cuenta.
    """
    registro = _bus_por_cuenta(ajustes, database)

    assert registro.get_or_start(A).bus is not registro.get_or_start(B).bus


# ---------------------------------------------------------------------------
# El runtime base NO puede reclamar una cuenta cuya sesion vive en otro sitio
# ---------------------------------------------------------------------------
#
# EL FALLO, MEDIDO EN UN ARRANQUE REAL
# ------------------------------------
# Tras mover la sesion a `session/accounts/<id>/`, el arranque decia::
#
#     [APP] runtime existente adoptado para la cuenta 5d91a4f6
#     [APP] arranque multicuenta: 1 de 1 cuenta(s) vinculada(s)
#
# y acto seguido, al pedir emparejamiento::
#
#     [WA] Sesion no encontrada en ...\session\device.json
#
# El runtime base seguia construido con la carpeta PLANA --vacia ya tras la
# migracion-- y se adoptaba igual. La cuenta quedaba atada a un sitio donde no
# estaba su identidad, el registro ya no le construia la suya, y acababa
# pidiendo un codigo QR teniendo su sesion intacta en disco.


def _base(carpeta, database, cuenta=None):
    """Un runtime base como el que construye el arranque."""
    import types

    return types.SimpleNamespace(
        settings=types.SimpleNamespace(session_dir=carpeta),
        database=database,
        runtime_owner_account_id=cuenta,
    )


def _con_sesion(carpeta):
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / "device.json").write_text('{"jid": {}}', encoding="utf-8")
    return carpeta


def test_EL_BASE_NO_RECLAMA_UNA_CUENTA_QUE_YA_TIENE_SU_CARPETA(tmp_path, database):
    """LA REGLA QUE FALTABA.

    La cuenta guarda su identidad en `accounts/<id>/`; el runtime base apunta
    a la carpeta plana, vacia. Reclamarla lo dejaria buscando donde no hay
    nada.
    """
    from app.api.account_runtime import cuenta_del_runtime_base
    from app.core.session_paths import carpeta_de_cuenta

    plana = tmp_path / "session"
    plana.mkdir(parents=True, exist_ok=True)
    import types

    _con_sesion(carpeta_de_cuenta(types.SimpleNamespace(session_dir=plana), A))

    base = _base(plana, database, cuenta=A)

    assert cuenta_del_runtime_base(base) is None, (
        "la sesion de A vive en su carpeta: el base no puede reclamarla"
    )


def test_el_base_SI_reclama_mientras_la_sesion_sigue_suelta(tmp_path, database):
    """El caso legacy, antes de migrar: la identidad esta en la carpeta plana."""
    from app.api.account_runtime import cuenta_del_runtime_base

    plana = _con_sesion(tmp_path / "session")
    base = _base(plana, database, cuenta=A)

    assert cuenta_del_runtime_base(base) == A


def test_sin_sesion_en_ningun_sitio_se_adopta_el_base(tmp_path, database):
    """Recien instalado no hay identidad en ninguna parte.

    Ahi el base es el unico candidato, y negarselo dejaria al usuario sin
    poder ni empezar a vincular.
    """
    from app.api.account_runtime import cuenta_del_runtime_base

    plana = tmp_path / "session"
    plana.mkdir(parents=True, exist_ok=True)
    base = _base(plana, database, cuenta=A)

    assert cuenta_del_runtime_base(base) == A

