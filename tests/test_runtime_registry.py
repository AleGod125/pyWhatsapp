"""Un runtime por cuenta. Nunca uno global.

EL FALLO QUE CIERRA, MEDIDO EN EL CODIGO
----------------------------------------
Habia UN runtime en todo el proceso y cada endpoint leia su estado::

    rt = runtime()                       # el unico que hay
    estado = rt.state.state.value        # el de A
    if estado == "CONNECTED": ...        # -> 409 para B
    if rt.pairing.available: ...         # -> el codigo QR de A

Con la cuenta de A conectada, el usuario B --sin cuenta ninguna-- o no podia
vincular, o se le ofrecia un codigo que no era suyo.

LA REGLA
--------
El runtime se busca por ``whatsapp_account_id``. Jamas por usuario, jamas "el
que haya". Dos personas que comparten una cuenta comparten runtime; dos
cuentas distintas no comparten nada.
"""

from __future__ import annotations

import threading
import uuid

import pytest

from app.core.runtime_registry import RuntimeRegistry
from app.core.session_paths import (
    MigracionAmbigua,
    ajustes_de_cuenta,
    carpeta_de_cuenta,
    hay_sesion_en,
    migrar_sesion_plana,
)


class _RuntimeFalso:
    """Lo justo para poder distinguir dos y ver si se paran."""

    def __init__(self, settings, database, account_id):
        self.settings = settings
        self.database = database
        self.runtime_owner_account_id = account_id
        self.parado = False

    def stop(self):
        self.parado = True


@pytest.fixture
def ajustes(settings, tmp_path):
    """Ajustes con la sesion en un temporal: no se toca la sesion viva."""
    import dataclasses

    return dataclasses.replace(settings, session_dir=tmp_path / "session")


@pytest.fixture
def registro(ajustes, database):
    return RuntimeRegistry(ajustes, database, fabrica=_RuntimeFalso)


A = uuid.uuid4()
B = uuid.uuid4()


# ---------------------------------------------------------------------------
# Dos cuentas, dos runtimes
# ---------------------------------------------------------------------------


def test_CADA_CUENTA_TIENE_EL_SUYO(registro):
    """LA REGLA. Antes habia uno solo y el de A contestaba por B."""
    rt_a = registro.get_or_start(A)
    rt_b = registro.get_or_start(B)

    assert rt_a is not rt_b
    assert rt_a.runtime_owner_account_id == A
    assert rt_b.runtime_owner_account_id == B


def test_LA_CARPETA_DE_SESION_NO_SE_COMPARTE(registro):
    """Sin esto la segunda cuenta escribiria encima de la identidad de la primera.

    No es que se mezclen datos: es que una de las dos dejaria de existir.
    """
    rt_a = registro.get_or_start(A)
    rt_b = registro.get_or_start(B)

    assert rt_a.settings.session_dir != rt_b.settings.session_dir
    assert str(A) in str(rt_a.settings.session_dir)
    assert str(B) in str(rt_b.settings.session_dir)


def test_pedir_dos_veces_devuelve_EL_MISMO(registro):
    """Un refresco o un doble clic no pueden abrir dos clientes de WhatsApp."""
    assert registro.get_or_start(A) is registro.get_or_start(A)


def test_EL_ARRANQUE_ES_A_PRUEBA_DE_CONCURRENCIA(ajustes, database):
    """Dos peticiones a la vez obtienen el mismo runtime, no dos.

    Dos clientes sobre la misma identidad se pelean por el Signal Store.
    """
    creados = []

    def _lento(s, d, account_id):
        import time

        time.sleep(0.02)  # ensancha la ventana de carrera a proposito
        rt = _RuntimeFalso(s, d, account_id)
        creados.append(rt)
        return rt

    registro = RuntimeRegistry(ajustes, database, fabrica=_lento)
    obtenidos = []
    hilos = [
        threading.Thread(target=lambda: obtenidos.append(registro.get_or_start(A)))
        for _ in range(8)
    ]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    assert len(creados) == 1, "solo se puede crear UN runtime por cuenta"
    assert len({id(o) for o in obtenidos}) == 1


def test_los_cerrojos_son_POR_CUENTA(registro):
    """Con un cerrojo global, emparejar B dejaria esperando a A."""
    assert registro.candado_de(A) is not registro.candado_de(B)
    assert registro.candado_de(A) is registro.candado_de(A)


# ---------------------------------------------------------------------------
# Aislamiento de fallos y de ciclo de vida
# ---------------------------------------------------------------------------


def test_SI_UNA_CUENTA_FALLA_LA_OTRA_SIGUE(ajustes, database):
    """Un proceso no puede caerse entero porque una cuenta diera problemas."""

    def _revienta_para_b(s, d, account_id):
        if account_id == B:
            raise RuntimeError("esta cuenta da problemas")
        return _RuntimeFalso(s, d, account_id)

    registro = RuntimeRegistry(ajustes, database, fabrica=_revienta_para_b)

    rt_a = registro.get_or_start(A)
    rt_b = registro.get_or_start(B)

    assert rt_a is not None, "A no puede caerse por lo que le pase a B"
    assert rt_b is None


def test_PARAR_UNA_NO_TOCA_LA_OTRA(registro):
    rt_a = registro.get_or_start(A)
    rt_b = registro.get_or_start(B)

    assert registro.stop(B) is True

    assert rt_b.parado is True
    assert rt_a.parado is False
    assert registro.get(A) is rt_a
    assert registro.get(B) is None


def test_parar_lo_que_no_existe_no_es_un_error(registro):
    assert registro.stop(uuid.uuid4()) is False


def test_sin_cuenta_no_se_levanta_nada(registro):
    assert registro.get_or_start(None) is None
    assert registro.get(None) is None


def test_pararlos_todos(registro):
    registro.get_or_start(A)
    registro.get_or_start(B)
    assert registro.stop_all() == 2
    assert registro.all() == {}


# ---------------------------------------------------------------------------
# Las rutas de sesion
# ---------------------------------------------------------------------------


def test_la_carpeta_lleva_el_identificador_NO_el_correo(ajustes):
    """El disco no es sitio para un dato personal, y ademas el correo cambia."""
    carpeta = carpeta_de_cuenta(ajustes, A)
    assert carpeta.name == str(A)
    assert "@" not in str(carpeta)


def test_los_ajustes_de_cuenta_apuntan_a_su_carpeta(ajustes):
    propios = ajustes_de_cuenta(ajustes, A)
    assert propios.session_dir == carpeta_de_cuenta(ajustes, A)
    assert propios.session_dir.is_dir()
    # Y lo demas no cambia: son los mismos ajustes con otra carpeta.
    assert propios.database_url == ajustes.database_url


# ---------------------------------------------------------------------------
# Migrar la sesion que YA existe
# ---------------------------------------------------------------------------


@pytest.fixture
def sesion_plana(settings, tmp_path):
    """Una sesion suelta en `session/`, como la que hay hoy en produccion."""
    import dataclasses

    base = tmp_path / "session"
    base.mkdir(parents=True, exist_ok=True)
    (base / "device.json").write_text('{"identidad": "de mentira"}', encoding="utf-8")
    (base / "device.json.signal.db").write_bytes(b"SQLite format 3\x00")
    (base / "device.json.signal.db-wal").write_bytes(b"wal")
    (base / "compat_prekey.db").write_bytes(b"SQLite format 3\x00")
    return dataclasses.replace(settings, session_dir=base)


def test_LA_SESION_ACTUAL_SE_MUEVE_ENTERA(sesion_plana):
    """Se mueve, NO se regenera.

    Volver a escanear un codigo QR seria perder el Signal Store, y con el todo
    el historial que solo se puede pedir con esa identidad.
    """
    destino = migrar_sesion_plana(sesion_plana, [A])

    assert destino == carpeta_de_cuenta(sesion_plana, A)
    assert hay_sesion_en(destino)
    for nombre in ("device.json", "device.json.signal.db", "compat_prekey.db"):
        assert (destino / nombre).exists(), nombre
    # El acompanante de SQLite viaja con su base: sin el se pierde lo ultimo
    # escrito, que es justo lo que hace falta.
    assert (destino / "device.json.signal.db-wal").exists()
    # Y no queda nada suelto en el sitio antiguo.
    assert not hay_sesion_en(sesion_plana.session_dir)


def test_migrar_dos_veces_no_hace_nada_la_segunda(sesion_plana):
    """Idempotente: arrancar el servicio otra vez no puede romper nada."""
    primera = migrar_sesion_plana(sesion_plana, [A])
    segunda = migrar_sesion_plana(sesion_plana, [A])

    assert primera is not None
    assert segunda is None


def test_SIN_SESION_PLANA_NO_HAY_NADA_QUE_MIGRAR(settings, tmp_path):
    import dataclasses

    vacia = dataclasses.replace(settings, session_dir=tmp_path / "sin-nada")
    (tmp_path / "sin-nada").mkdir(parents=True, exist_ok=True)
    assert migrar_sesion_plana(vacia, [A]) is None


def test_CON_DOS_CUENTAS_NO_SE_ADIVINA(sesion_plana):
    """La prueba que mas importa de la migracion.

    Atribuir una identidad de WhatsApp a la cuenta equivocada le entrega a
    alguien la conversacion de otro. Antes de eso, se para.
    """
    with pytest.raises(MigracionAmbigua):
        migrar_sesion_plana(sesion_plana, [A, B])

    # Y no se ha movido nada: la sesion sigue donde estaba.
    assert hay_sesion_en(sesion_plana.session_dir)


def test_sin_ninguna_cuenta_tampoco(sesion_plana):
    with pytest.raises(MigracionAmbigua):
        migrar_sesion_plana(sesion_plana, [])
    assert hay_sesion_en(sesion_plana.session_dir)


def test_NO_SE_SOBRESCRIBE_UNA_IDENTIDAD_EXISTENTE(sesion_plana):
    """El destino ya tiene una sesion: podria ser la buena. No se pisa."""
    destino = carpeta_de_cuenta(sesion_plana, A)
    destino.mkdir(parents=True, exist_ok=True)
    (destino / "device.json").write_text('{"identidad": "la que ya estaba"}', encoding="utf-8")

    with pytest.raises(MigracionAmbigua):
        migrar_sesion_plana(sesion_plana, [A])

    assert (destino / "device.json").read_text(encoding="utf-8") == (
        '{"identidad": "la que ya estaba"}'
    )


# ---------------------------------------------------------------------------
# El arranque multicuenta
# ---------------------------------------------------------------------------


def _cuenta_en_bd(session, estado="linked"):
    """Una cuenta de WhatsApp real, con su usuario."""
    from app.models import User, WhatsAppAccount

    usuario = User(
        email=f"reg-{uuid.uuid4().hex[:10]}@example.com", password_hash="x"
    )
    session.add(usuario)
    session.flush()
    fila = WhatsAppAccount(
        user_id=usuario.id,
        session_status=estado,
        session_storage_key=f"accounts/{uuid.uuid4().hex}",
    )
    session.add(fila)
    session.flush()
    return fila


@pytest.fixture
def db_de_sesion(session):
    """Una base que reutiliza la transaccion de la prueba."""

    class _Db:
        def transaction(self):
            from contextlib import contextmanager

            @contextmanager
            def scope():
                yield session
                session.flush()

            return scope()

    return _Db()


def test_UNA_CUENTA_VINCULADA_UN_RUNTIME(ajustes, db_de_sesion, session):
    from sqlalchemy import delete

    from app.models import WhatsAppAccount

    session.execute(delete(WhatsAppAccount))
    session.flush()
    cuenta = _cuenta_en_bd(session)

    registro = RuntimeRegistry(ajustes, db_de_sesion, fabrica=_RuntimeFalso)
    levantados = registro.levantar_las_vinculadas()

    assert len(levantados) == 1
    assert registro.get(cuenta.id) is levantados[0]


def test_DOS_CUENTAS_VINCULADAS_DOS_RUNTIMES(ajustes, db_de_sesion, session):
    from sqlalchemy import delete

    from app.models import WhatsAppAccount

    session.execute(delete(WhatsAppAccount))
    session.flush()
    a = _cuenta_en_bd(session)
    b = _cuenta_en_bd(session)

    registro = RuntimeRegistry(ajustes, db_de_sesion, fabrica=_RuntimeFalso)
    registro.levantar_las_vinculadas()

    rt_a, rt_b = registro.get(a.id), registro.get(b.id)
    assert rt_a is not None and rt_b is not None
    assert rt_a is not rt_b
    assert rt_a.settings.session_dir != rt_b.settings.session_dir


def test_ARRANCAR_DOS_VECES_NO_DUPLICA(ajustes, db_de_sesion, session):
    """Idempotente. Un runtime de mas seria un segundo cliente sobre la misma
    identidad, y el segundo no podria ni abrir el Signal Store."""
    from sqlalchemy import delete

    from app.models import WhatsAppAccount

    session.execute(delete(WhatsAppAccount))
    session.flush()
    cuenta = _cuenta_en_bd(session)

    registro = RuntimeRegistry(ajustes, db_de_sesion, fabrica=_RuntimeFalso)
    primera = registro.levantar_las_vinculadas()
    segunda = registro.levantar_las_vinculadas()

    assert primera[0] is segunda[0]
    assert registro.get(cuenta.id) is primera[0]
    assert len(registro.all()) == 1


def test_UNA_CUENTA_ROTA_NO_IMPIDE_LEVANTAR_LAS_DEMAS(
    ajustes, db_de_sesion, session
):
    from sqlalchemy import delete

    from app.models import WhatsAppAccount

    session.execute(delete(WhatsAppAccount))
    session.flush()
    buena = _cuenta_en_bd(session)
    rota = _cuenta_en_bd(session)

    def _falla_para_la_rota(s, d, account_id):
        if str(account_id) == str(rota.id):
            raise RuntimeError("esta cuenta no arranca")
        return _RuntimeFalso(s, d, account_id)

    registro = RuntimeRegistry(ajustes, db_de_sesion, fabrica=_falla_para_la_rota)
    levantados = registro.levantar_las_vinculadas()

    assert len(levantados) == 1
    assert registro.get(buena.id) is not None
    assert registro.get(rota.id) is None


def test_las_cuentas_SIN_vincular_no_se_levantan(ajustes, db_de_sesion, session):
    """Una cuenta que nunca se vinculo no tiene sesion que recuperar."""
    from sqlalchemy import delete

    from app.models import WhatsAppAccount

    session.execute(delete(WhatsAppAccount))
    session.flush()
    _cuenta_en_bd(session, estado="never_linked")

    registro = RuntimeRegistry(ajustes, db_de_sesion, fabrica=_RuntimeFalso)
    assert registro.levantar_las_vinculadas() == []


def test_sin_base_de_datos_no_se_levanta_nada(ajustes):
    registro = RuntimeRegistry(ajustes, None, fabrica=_RuntimeFalso)
    assert registro.levantar_las_vinculadas() == []


# ---------------------------------------------------------------------------
# Escala: la arquitectura no depende de "una cuenta"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cuantas", [10, 50])
def test_N_CUENTAS_N_RUNTIMES_SIN_COLISIONES(ajustes, database, cuantas):
    """No hace falta abrir 50 sesiones reales para demostrar el punto.

    Lo que se prueba es que el registro indexa por cuenta y no arrastra
    ninguna suposicion de "la unica que hay": con 10 o con 50, cada
    identificador tiene su runtime, su carpeta y su cuenta declarada.
    """
    registro = RuntimeRegistry(ajustes, database, fabrica=_RuntimeFalso)
    cuentas = [uuid.uuid4() for _ in range(cuantas)]

    runtimes = [registro.get_or_start(c) for c in cuentas]

    assert all(rt is not None for rt in runtimes)
    # Ni un runtime repetido.
    assert len({id(rt) for rt in runtimes}) == cuantas
    # Ni una carpeta repetida.
    assert len({str(rt.settings.session_dir) for rt in runtimes}) == cuantas
    # Y cada uno sabe de quien es.
    for cuenta, rt in zip(cuentas, runtimes):
        assert rt.runtime_owner_account_id == cuenta
        assert registro.get(cuenta) is rt
    assert len(registro.all()) == cuantas

