"""El equipo no es de nadie: cada usuario vincula lo suyo.

EL FALLO QUE FIJA ESTE ARCHIVO
------------------------------
Medido en la aplicacion real, con dos usuarios y un solo ordenador::

    A vincula su WhatsApp        -> conectado, todo bien
    B entra y pide su codigo QR  -> "No pudimos preparar el codigo"
                                    "Este dispositivo tiene una vinculacion
                                     de WhatsApp en marcha de otro usuario."

El primero que vinculaba se quedaba con la maquina. Las reglas del producto
son de PERSONA, no de equipo:

* una cuenta de Google sostiene como mucho UNA cuenta de WhatsApp;
* una cuenta de WhatsApp puede estar en varias cuentas de Google, siempre de
  forma explicita;
* dos cuentas distintas no comparten NADA: ni identidad, ni Signal Store, ni
  carpeta, ni runtime.
"""

from __future__ import annotations

import inspect
import pathlib
import uuid
from contextlib import contextmanager

import pytest

from app.models import User, WhatsAppAccount


class _DatabaseDeSesion:
    """Reutiliza la sesion del test: nada se escribe fuera de la transaccion."""

    def __init__(self, session):
        self._session = session

    def transaction(self):
        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


def _usuario(session) -> User:
    fila = User(email=f"u-{uuid.uuid4().hex[:10]}@example.com", password_hash="x")
    session.add(fila)
    session.flush()
    return fila


def _servicio(session, settings):
    from app.auth.whatsapp_accounts import WhatsAppAccountService

    return WhatsAppAccountService(_DatabaseDeSesion(session), settings)


# ---------------------------------------------------------------------------
# A. Cada cuenta, su carpeta
# ---------------------------------------------------------------------------


def test_la_carpeta_se_nombra_por_la_CUENTA_y_no_por_el_usuario(session, settings):
    """Con la clave por usuario, una cuenta compartida tendria DOS carpetas.

    Y dos carpetas para una identidad significa dos Signal Store para un solo
    dispositivo: el segundo no descifra nada.
    """
    servicio = _servicio(session, settings)
    cuenta = servicio.asegurar_cuenta(_usuario(session).id)

    assert cuenta.session_storage_key == f"accounts/{cuenta.id}"


def test_la_clave_coincide_con_la_ruta_que_se_usa_de_verdad(session, settings):
    """La base y el disco tienen que decir lo mismo.

    `carpeta_de_cuenta` es quien construye la ruta real. Si la etiqueta que
    guarda la base no concuerda, el arranque no sabe de quien es cada carpeta
    -- y ese desajuste ya provoco que se leyera `session/device.json` para una
    cuenta cuya sesion vivia en otro sitio.
    """
    from app.core.session_paths import carpeta_de_cuenta

    servicio = _servicio(session, settings)
    cuenta = servicio.asegurar_cuenta(_usuario(session).id)

    esperada = carpeta_de_cuenta(settings, cuenta.id)
    assert str(esperada).replace("\\", "/").endswith(cuenta.session_storage_key)


def test_dos_usuarios_no_comparten_carpeta(session, settings):
    servicio = _servicio(session, settings)
    a = servicio.asegurar_cuenta(_usuario(session).id)
    b = servicio.asegurar_cuenta(_usuario(session).id)

    assert a.id != b.id
    assert a.session_storage_key != b.session_storage_key


def test_pedirla_dos_veces_no_crea_dos_cuentas(session, settings):
    servicio = _servicio(session, settings)
    yo = _usuario(session).id

    assert servicio.asegurar_cuenta(yo).id == servicio.asegurar_cuenta(yo).id


# ---------------------------------------------------------------------------
# B. Con dos vinculaciones NO se adivina
# ---------------------------------------------------------------------------


def test_con_dos_cuentas_vinculadas_no_se_atribuye_la_sesion_a_nadie(
    session, settings
):
    """El peor fallo posible, y no daba ningun error.

    ``dueno_actual`` devolvia ``.first()`` sin ordenar. Con A y B vinculados,
    PostgreSQL entrega la que quiera; al arrancar, el runtime que sostenia la
    sesion de A podia preguntar aqui y recibir a B, y entonces marcaba la
    cuenta de B como vinculada usando la identidad de A.
    """
    servicio = _servicio(session, settings)
    for _ in range(2):
        cuenta = servicio.asegurar_cuenta(_usuario(session).id)
        cuenta_viva = session.get(WhatsAppAccount, cuenta.id)
        cuenta_viva.session_status = "linked"
    session.flush()

    assert servicio.dueno_actual() is None, (
        "con dos vinculadas no hay respuesta correcta, asi que no se da ninguna"
    )


def test_con_UNA_vinculada_si_se_sabe_de_quien_es(session, settings):
    servicio = _servicio(session, settings)
    yo = _usuario(session).id
    cuenta = servicio.asegurar_cuenta(yo)
    session.get(WhatsAppAccount, cuenta.id).session_status = "linked"
    session.flush()

    assert servicio.dueno_actual() == yo


# ---------------------------------------------------------------------------
# C. Ningun guarda de dispositivo en el camino del emparejamiento
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ruta", ["session_pair", "session_qr", "session_qr_image"]
)
def test_el_emparejamiento_no_pregunta_de_quien_es_la_maquina(ruta):
    """Ni un 409 por haber vinculado otro antes.

    `_conflicto_de_sesion()` preguntaba de quien era el runtime del PROCESO y
    quien tenia una cuenta vinculada en TODA la base. Las dos son preguntas de
    equipo, y con cualquiera de ellas el segundo usuario no podia vincular.
    """
    from app.api import routes

    # Solo el CODIGO: los comentarios nombran el fallo a proposito, para que
    # nadie lo reintroduzca sin leer por que se quito.
    codigo = chr(10).join(
        linea
        for linea in inspect.getsource(getattr(routes, ruta)).splitlines()
        if not linea.lstrip().startswith("#")
    )
    assert "_conflicto_de_sesion" not in codigo
    assert "ACCOUNT_RUNTIME_IN_USE" not in codigo


def test_el_guarda_de_dispositivo_ya_no_existe():
    """Y no vuelve por la puerta de atras en una ruta nueva."""
    from app.api import routes

    assert not hasattr(routes, "_conflicto_de_sesion")


def test_el_qr_sale_del_runtime_de_quien_pregunta():
    """Servir el de otro es entregarle la llave de una cuenta ajena."""
    from app.api import routes

    for ruta in (routes.session_qr, routes.session_qr_image):
        assert "runtime_de_mi_cuenta" in inspect.getsource(ruta)


# ---------------------------------------------------------------------------
# D. Un runtime por cuenta, y ARRANCADO
# ---------------------------------------------------------------------------


def _fabrica_real() -> str:
    """El codigo de `_crear_runtime` tal y como esta escrito.

    Se lee del FICHERO y no del atributo del modulo: la suite sustituye la
    fabrica por una que no arranca nada (ver el autouse de `conftest`), asi
    que `inspect.getsource` devolveria la de mentira y esta prueba no
    comprobaria nada.
    """
    from app.core import runtime_registry

    fuente = pathlib.Path(runtime_registry.__file__).read_text(encoding="utf-8")
    inicio = fuente.index("def _crear_runtime(")
    return fuente[inicio:]


def test_la_fabrica_arranca_el_runtime_que_crea():
    """Sin arrancarlo no hay cliente, y sin cliente no hay codigo QR.

    Se devolvia el objeto sin llamar a `start()`. `iniciar_vinculacion` no
    encontraba cliente que arrancar, no se generaba ningun QR, y la pantalla
    se quedaba en "Codigo no disponible". Solo parecia funcionar porque la
    unica cuenta era la del runtime adoptado, que arranca `service.py`.
    """
    assert "runtime.start(" in _fabrica_real()


def test_el_runtime_de_una_cuenta_sabe_de_que_usuario_es():
    """El almacenamiento y la sincronizacion atribuyen por `runtime_owner_user_id`."""
    assert "runtime_owner_user_id" in _fabrica_real()


def test_sin_registro_no_se_entrega_el_runtime_de_otro(monkeypatch):
    """El respaldo devolvia "el de siempre", que con dos cuentas es el ajeno."""
    from app.api import account_runtime

    fuente = inspect.getsource(account_runtime.runtime_de_mi_cuenta)
    assert "_es_el_runtime_de(base, cuenta)" in fuente


# ---------------------------------------------------------------------------
# E. La sesion suelta se atribuye por IDENTIDAD, no por descarte
# ---------------------------------------------------------------------------
#
# El agujero, con su final: instalacion con la sesion de A todavia suelta en
# `session/`; B vincula; al siguiente arranque hay DOS cuentas. Ni la clave de
# almacenamiento coincidia con la carpeta plana ni valia el "hay una sola", asi
# que la sesion no se atribuia a nadie, el registro le construia a A un runtime
# apuntando a su carpeta --vacia-- y a A se le pedia un codigo QR teniendo su
# identidad intacta en disco.


def _escribir_identidad(carpeta, *, usuario: str, lid: str) -> None:
    """Un ``device.json`` con lo justo para identificarlo. Sin claves."""
    import json

    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / "device.json").write_text(
        json.dumps(
            {"jid": {"user": usuario, "server": "s.whatsapp.net", "device": 0},
             "lid": lid}
        ),
        encoding="utf-8",
    )


class _BaseFalso:
    """Un runtime base: solo su carpeta de sesion y su base de datos."""

    def __init__(self, carpeta, database):
        import types

        self.settings = types.SimpleNamespace(session_dir=carpeta)
        self.database = database


def test_con_dos_cuentas_la_identidad_dice_de_quien_es_la_sesion_suelta(
    session, tmp_path
):
    from app.api.account_runtime import cuenta_del_runtime_base
    from app.models import User, WhatsAppAccount

    plana = tmp_path / "session"
    _escribir_identidad(plana, usuario="573001112233", lid="111122223333@lid")

    ids = []
    for pn, lid in (
        ("573001112233@s.whatsapp.net", "111122223333@lid"),   # la de la sesion
        ("573009998877@s.whatsapp.net", "444455556666@lid"),   # la del otro
    ):
        usuario = User(email=f"e-{uuid.uuid4().hex[:8]}@x.com", password_hash="x")
        session.add(usuario)
        session.flush()
        fila = WhatsAppAccount(
            id=uuid.uuid4(),
            user_id=usuario.id,
            session_status="linked",
            session_storage_key=f"accounts/{uuid.uuid4()}",
            wa_pn=pn,
            wa_lid=lid,
        )
        session.add(fila)
        session.flush()
        ids.append(fila.id)

    class _Db:
        def transaction(self):
            @contextmanager
            def scope():
                yield session

            return scope()

    assert cuenta_del_runtime_base(_BaseFalso(plana, _Db())) == ids[0], (
        "la sesion es de quien coincide con device.json, no de la primera fila"
    )


def test_si_la_identidad_no_coincide_con_nadie_no_se_atribuye(session, tmp_path):
    """Mejor quedarse sin dato que darle a alguien la conversacion de otro."""
    from app.api.account_runtime import cuenta_del_runtime_base
    from app.models import User, WhatsAppAccount

    plana = tmp_path / "session"
    _escribir_identidad(plana, usuario="573000000000", lid="000000000000@lid")

    for _ in range(2):
        usuario = User(email=f"e-{uuid.uuid4().hex[:8]}@x.com", password_hash="x")
        session.add(usuario)
        session.flush()
        session.add(
            WhatsAppAccount(
                id=uuid.uuid4(),
                user_id=usuario.id,
                session_status="linked",
                session_storage_key=f"accounts/{uuid.uuid4()}",
                wa_pn=f"5730099{uuid.uuid4().int % 10000:04d}@s.whatsapp.net",
                wa_lid=f"{uuid.uuid4().int % 10**12}@lid",
            )
        )
        session.flush()

    class _Db:
        def transaction(self):
            @contextmanager
            def scope():
                yield session

            return scope()

    assert cuenta_del_runtime_base(_BaseFalso(plana, _Db())) is None


def test_una_clave_antigua_se_normaliza_Y_SE_GUARDA(session, settings):
    """El `expunge` descartaba el cambio y la UPDATE no llegaba a emitirse.

    Se midio en la instalacion real: la cuenta seguia con `users/<usuario>`
    despues de pasar por `asegurar_cuenta`, asi que la base y el disco
    seguian discrepando.
    """
    from app.models import WhatsAppAccount

    usuario = _usuario(session)
    id_cuenta = uuid.uuid4()
    session.add(
        WhatsAppAccount(
            id=id_cuenta,
            user_id=usuario.id,
            session_status="linked",
            session_storage_key=f"users/{usuario.id}",  # el formato antiguo
        )
    )
    session.flush()

    _servicio(session, settings).asegurar_cuenta(usuario.id)

    session.expire_all()  # se relee de la base, no del mapa de identidad
    guardada = session.get(WhatsAppAccount, id_cuenta)
    assert guardada.session_storage_key == f"accounts/{id_cuenta}"
