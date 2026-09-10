"""Un telefono, una cuenta. Y una cuenta solo recibe lo de su telefono.

LO QUE VIO EL USUARIO
---------------------
Vinculo dos moviles. En el selector aparecia UNO --"+573002389304"-- y al
refrescar salian los chats de los dos mezclados, con los contactos comunes
fusionados en una sola conversacion.

Lo que habia debajo::

    fila 36e15dde   session/baileys/creds.json           -> Dora Niebles
                    wa_pn / wa_lid en la base             -> los de Ale     (!)
                    328 chats

    fila 95531d07   session/accounts/95531d07/creds.json  -> Ale
                    session_status                         -> never_linked  (!)
                    0 chats

O sea: la cuenta visible era la sesion de Dora con la etiqueta de Ale, y la de
Ale no se sello nunca --por eso no aparecia y no tenia mensajes--. Y catorce
segundos despues de vincular el segundo movil entraron 195 conversaciones en
la fila de la primera.

Como la unicidad es ``(whatsapp_account_id, jid)``, un contacto que los dos
tengan cae en LA MISMA fila: de ahi que se vieran fusionados.

LAS TRES REGLAS QUE LO CIERRAN
------------------------------
1. una cuenta solo se sella con el telefono que dicen SUS credenciales;
2. un telefono no puede quedar vinculado en dos cuentas;
3. un socket no escribe historial en una cuenta que no es la suya.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager

import pytest

from app.core.session_paths import carpeta_de_cuenta


@pytest.fixture
def ajustes(settings, tmp_path):
    """Carpeta de sesion propia: estas pruebas escriben credenciales."""
    import dataclasses

    return dataclasses.replace(settings, session_dir=tmp_path / "session")


class _Base:
    def __init__(self, session):
        self._session = session

    def transaction(self):
        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


def _creds(ajustes, account_id, telefono, nombre=None):
    destino = carpeta_de_cuenta(ajustes, account_id) / "baileys"
    destino.mkdir(parents=True, exist_ok=True)
    yo = {"id": f"{telefono}:17@s.whatsapp.net"}
    if nombre:
        yo["name"] = nombre
    (destino / "creds.json").write_text(
        json.dumps({"me": yo, "registered": True}), encoding="utf-8"
    )


def _servicio(ajustes, session):
    from app.auth.whatsapp_accounts import WhatsAppAccountService

    return WhatsAppAccountService(_Base(session), ajustes)


def _otra(session, user_id, **kw):
    from app.models import WhatsAppAccount

    ident = uuid.uuid4()
    fila = WhatsAppAccount(
        id=ident,
        user_id=user_id,
        session_status=kw.pop("session_status", "never_linked"),
        session_storage_key=f"accounts/{ident}",
        **kw,
    )
    session.add(fila)
    session.flush()
    return fila


# ---------------------------------------------------------------------------
# 1. Solo se sella con las credenciales propias
# ---------------------------------------------------------------------------


def _creds_en_la_carpeta_base(ajustes, telefono, nombre=None):
    """Donde escribe el runtime BASE: `session/baileys/`, sin identificador."""
    from pathlib import Path

    destino = Path(ajustes.session_dir) / "baileys"
    destino.mkdir(parents=True, exist_ok=True)
    yo = {"id": f"{telefono}:17@s.whatsapp.net"}
    if nombre:
        yo["name"] = nombre
    (destino / "creds.json").write_text(
        json.dumps({"me": yo, "registered": True}), encoding="utf-8"
    )


def test_no_se_sella_una_cuenta_con_el_numero_de_otra_sesion(ajustes, session, cuenta):
    """El fallo exacto, y por que costo verlo.

    La fila 36e15dde acabo con el numero Y el LID de Ale teniendo la sesion de
    Dora. Sus credenciales no estaban en `session/accounts/<id>/` sino en
    `session/baileys/` --la carpeta del runtime base, que no lleva el
    identificador en la ruta-- asi que al mirar solo la primera ruta parecia
    una cuenta sin credenciales y se dejaba escribir cualquier numero encima.

    Mirando tambien la carpeta base, se resuelve a Dora y el numero ajeno se
    rechaza.
    """
    cuenta.session_status = "never_linked"
    cuenta.wa_pn = None
    cuenta.display_name = None
    session.flush()
    _creds_en_la_carpeta_base(ajustes, "573008927374", "Dora Niebles")

    sellada = _servicio(ajustes, session).marcar_vinculada(
        cuenta.user_id,
        pn="573002389304@s.whatsapp.net",   # el de Ale
        lid="86531142340710@lid",           # y su LID
        account_id=cuenta.id,
    )

    session.expire_all()
    assert sellada, "la cuenta tiene sesion: sellarla es lo correcto"
    assert cuenta.phone_number == "573008927374", (
        "se le puso el numero de OTRA sesion; el selector enseñaria una "
        "persona con la etiqueta de otra"
    )
    assert cuenta.wa_lid != "86531142340710@lid", "y tambien el LID ajeno"
    assert cuenta.display_name == "Dora Niebles"


def test_sin_ningun_creds_se_sella_igual_pero_se_avisa(ajustes, session, cuenta):
    """Negarse aqui seria peor que el problema.

    Dejaria la cuenta sin sellar --invisible en el selector y sin runtime--
    que es exactamente el sintoma que se esta arreglando. Y hay un instante
    legitimo en que el fichero todavia no esta escrito.

    Lo que cierra el fallo no es esta rama: es mirar TAMBIEN la carpeta base.
    """
    cuenta.session_status = "never_linked"
    session.flush()

    sellada = _servicio(ajustes, session).marcar_vinculada(
        cuenta.user_id,
        pn="573002389304@s.whatsapp.net",
        lid=None,
        account_id=cuenta.id,
    )

    session.expire_all()
    assert sellada
    assert cuenta.phone_number == "573002389304"


def test_con_sus_credenciales_SI_se_sella(ajustes, session, cuenta):
    """La otra mitad: lo normal tiene que seguir funcionando."""
    cuenta.session_status = "never_linked"
    cuenta.display_name = None
    session.flush()
    _creds(ajustes, cuenta.id, "573008927374", "Dora Niebles")

    sellada = _servicio(ajustes, session).marcar_vinculada(
        cuenta.user_id,
        pn="573008927374@s.whatsapp.net",
        lid=None,
        account_id=cuenta.id,
    )

    session.expire_all()
    assert sellada
    assert cuenta.phone_number == "573008927374"
    assert cuenta.display_name == "Dora Niebles"
    assert cuenta.session_status == "linked"


def test_si_el_numero_que_llega_no_cuadra_MANDA_el_fichero(ajustes, session, cuenta):
    """Llega el numero de Ale para una cuenta cuya sesion es de Dora."""
    _creds(ajustes, cuenta.id, "573008927374", "Dora Niebles")

    _servicio(ajustes, session).marcar_vinculada(
        cuenta.user_id,
        pn="573002389304@s.whatsapp.net",
        lid=None,
        account_id=cuenta.id,
    )

    session.expire_all()
    assert cuenta.phone_number == "573008927374"


# ---------------------------------------------------------------------------
# 2. Un telefono no se vincula dos veces
# ---------------------------------------------------------------------------


def test_el_mismo_telefono_en_una_segunda_cuenta_se_rechaza(ajustes, session, cuenta):
    """Peticion del usuario: que no se pueda agregar dos veces la misma cuenta.

    Y no es teorico: paso, y dejo 132 conversaciones guardadas por duplicado
    con los mismos identificadores de mensaje.
    """
    _creds(ajustes, cuenta.id, "573008927374", "Dora Niebles")
    _servicio(ajustes, session).marcar_vinculada(
        cuenta.user_id, pn=None, lid=None, account_id=cuenta.id
    )

    # Se escanea EL MISMO telefono en una cuenta nueva.
    gemela = _otra(session, cuenta.user_id)
    _creds(ajustes, gemela.id, "573008927374", "Dora Niebles")

    sellada = _servicio(ajustes, session).marcar_vinculada(
        cuenta.user_id, pn=None, lid=None, account_id=gemela.id
    )

    session.expire_all()
    assert not sellada
    assert gemela.session_status == "error", (
        "la cuenta duplicada quedo utilizable: se le levantaria runtime y "
        "traeria otra copia de las mismas conversaciones"
    )
    assert cuenta.session_status == "linked", "la original no se toca"


def test_otro_telefono_distinto_si_se_admite(ajustes, session, cuenta):
    """Dos moviles de verdad es el caso que el programa existe para cubrir."""
    _creds(ajustes, cuenta.id, "573008927374", "Dora Niebles")
    _servicio(ajustes, session).marcar_vinculada(
        cuenta.user_id, pn=None, lid=None, account_id=cuenta.id
    )

    segunda = _otra(session, cuenta.user_id)
    _creds(ajustes, segunda.id, "573002389304", "Ale")

    sellada = _servicio(ajustes, session).marcar_vinculada(
        cuenta.user_id, pn=None, lid=None, account_id=segunda.id
    )

    session.expire_all()
    assert sellada
    assert segunda.session_status == "linked"
    assert segunda.phone_number == "573002389304"
    assert cuenta.phone_number == "573008927374"


def test_el_duplicado_se_mide_por_CREDENCIALES_no_por_la_etiqueta(
    ajustes, session, cuenta
):
    """`phone_number` es justo el campo que puede estar mal.

    Compararse con el dejaria pasar el duplicado precisamente cuando hay una
    etiqueta equivocada de por medio, que es como empezo todo.
    """
    _creds(ajustes, cuenta.id, "573008927374")
    cuenta.phone_number = "000000000000"  # etiqueta equivocada
    cuenta.session_status = "linked"
    session.flush()

    gemela = _otra(session, cuenta.user_id)
    _creds(ajustes, gemela.id, "573008927374")

    sellada = _servicio(ajustes, session).marcar_vinculada(
        cuenta.user_id, pn=None, lid=None, account_id=gemela.id
    )

    assert not sellada, "el duplicado se colo por mirar la etiqueta"


# ---------------------------------------------------------------------------
# 3. Un socket no escribe en la cuenta de otro
# ---------------------------------------------------------------------------


def test_un_socket_no_ingiere_en_una_cuenta_ajena(ajustes, monkeypatch):
    """La ultima linea de defensa, y la que faltaba.

    Aunque el dueno del runtime apunte a la cuenta equivocada, el historial no
    se escribe: la identidad del socket y las credenciales de la cuenta destino
    salen las dos de WhatsApp, y si no coinciden alguien se equivoco.
    """
    from app.core.runtime import AppRuntime

    ajena = uuid.uuid4()
    _creds(ajustes, ajena, "573008927374", "Dora Niebles")

    rt = object.__new__(AppRuntime)
    rt.settings = ajustes
    rt.runtime_owner_account_id = ajena
    # Este socket es el de Ale.
    monkeypatch.setattr(
        AppRuntime,
        "_identificadores_propios",
        lambda self: ("573002389304@s.whatsapp.net", None),
    )

    assert rt._la_cuenta_es_de_esta_sesion() is False


def test_su_propia_cuenta_si_pasa(ajustes, monkeypatch):
    from app.core.runtime import AppRuntime

    mia = uuid.uuid4()
    _creds(ajustes, mia, "573008927374", "Dora Niebles")

    rt = object.__new__(AppRuntime)
    rt.settings = ajustes
    rt.runtime_owner_account_id = mia
    monkeypatch.setattr(
        AppRuntime,
        "_identificadores_propios",
        lambda self: ("573008927374:17@s.whatsapp.net", None),
    )

    assert rt._la_cuenta_es_de_esta_sesion() is True


@pytest.mark.parametrize("falta", ["sin_cuenta", "sin_creds", "sin_identidad"])
def test_sin_datos_suficientes_NO_se_bloquea(ajustes, monkeypatch, falta):
    """Durante el arranque falta informacion, y ahi bloquear perderia historial.

    Lo que se corta es el caso claro: dos telefonos distintos. La duda no se
    resuelve tirando mensajes.
    """
    from app.core.runtime import AppRuntime

    ident = uuid.uuid4()
    rt = object.__new__(AppRuntime)
    rt.settings = ajustes
    rt.runtime_owner_account_id = None if falta == "sin_cuenta" else ident
    if falta == "sin_identidad":
        _creds(ajustes, ident, "573008927374")
    monkeypatch.setattr(
        AppRuntime,
        "_identificadores_propios",
        lambda self: (None, None)
        if falta == "sin_identidad"
        else ("573008927374@s.whatsapp.net", None),
    )

    assert rt._la_cuenta_es_de_esta_sesion() is True


# ---------------------------------------------------------------------------
# 4. Identidad INVERTIDA: la fila ya estaba registrada a otro numero
# ---------------------------------------------------------------------------
#
# El caso que no ve la comprobacion de arriba, y el que se observo en
# produccion. Se escanea el QR de Dora en el flujo de una fila registrada como
# Ale: Baileys escribe las credenciales de Dora en ESA carpeta, asi que el
# fichero y el socket coinciden --los dos dicen Dora-- y nada salta.
#
# La fila de Ale se convertia en Dora en silencio, con los chats de Ale
# colgando de una etiqueta que decia Dora. En Drive se veia como
# "WhatsApp Ale ✨🏍️" con las conversaciones de Dora dentro.


def test_una_fila_registrada_NO_cambia_de_identidad(ajustes, session, cuenta):
    """Sus chats son del numero registrado. Reetiquetarla los pone bajo otro."""
    cuenta.wa_pn = "573002389304@s.whatsapp.net"  # registrada como Ale
    cuenta.phone_number = "573002389304"
    cuenta.display_name = "Ale✨🏍️"
    cuenta.session_status = "linked"
    session.flush()

    # Y ahora en SU carpeta aparecen las credenciales de Dora.
    _creds(ajustes, cuenta.id, "573008927374", "Dora Niebles")

    sellada = _servicio(ajustes, session).marcar_vinculada(
        cuenta.user_id, pn=None, lid=None, account_id=cuenta.id
    )

    session.expire_all()
    # La fila de Ale conserva SU identidad: sus chats son suyos.
    assert cuenta.phone_number == "573002389304", "la fila cambio de dueno"
    assert cuenta.display_name == "Ale✨🏍️", (
        "en Drive esto seria 'WhatsApp Ale' con los chats de Dora dentro"
    )
    # Y su sesion ya no vale: se la acaban de sobrescribir.
    assert cuenta.session_status == "disconnected"

    # Pero NO se rechaza sin mas: se sella la cuenta de Dora, que es de quien
    # es el telefono que se escaneo. Rechazar y ya dejaria la sesion conectada
    # sin poder escribir en ninguna parte.
    assert sellada, "no se creo cuenta para el telefono que se escaneo"
    assert str(sellada) != str(cuenta.id)

    from app.models import WhatsAppAccount

    nueva = session.get(WhatsAppAccount, sellada)
    assert nueva.phone_number == "573008927374"
    assert nueva.display_name == "Dora Niebles"
    assert nueva.session_status == "linked"


def test_una_fila_SIN_registrar_si_admite_su_primera_identidad(
    ajustes, session, cuenta
):
    """La otra mitad: una cuenta nueva tiene que poder sellarse."""
    cuenta.wa_pn = None
    cuenta.phone_number = None
    cuenta.display_name = None
    cuenta.session_status = "never_linked"
    session.flush()
    _creds(ajustes, cuenta.id, "573008927374", "Dora Niebles")

    sellada = _servicio(ajustes, session).marcar_vinculada(
        cuenta.user_id, pn=None, lid=None, account_id=cuenta.id
    )

    session.expire_all()
    assert sellada
    assert cuenta.phone_number == "573008927374"
    assert cuenta.display_name == "Dora Niebles"


def test_reconectar_el_MISMO_telefono_no_se_confunde_con_una_inversion(
    ajustes, session, cuenta
):
    """Volver a vincular el mismo movil es normal y tiene que seguir yendo."""
    cuenta.wa_pn = "573008927374:17@s.whatsapp.net"
    cuenta.phone_number = "573008927374"
    cuenta.session_status = "disconnected"
    session.flush()
    # Otro dispositivo del MISMO numero: cambia el sufijo, no el telefono.
    _creds(ajustes, cuenta.id, "573008927374", "Dora Niebles")

    sellada = _servicio(ajustes, session).marcar_vinculada(
        cuenta.user_id, pn=None, lid=None, account_id=cuenta.id
    )

    assert sellada, "se confundio un reenganche con una inversion"
    assert str(sellada) == str(cuenta.id), "y ademas cambio de cuenta"


def test_el_socket_no_escribe_bajo_una_cuenta_de_otro_numero(ajustes, monkeypatch):
    """El otro extremo: aunque el sellado se rechace, tampoco se ingiere.

    Sin esto, la fila conservaria su etiqueta correcta pero seguiria
    recibiendo las conversaciones del telefono equivocado.
    """
    from app.core.runtime import AppRuntime

    ident = uuid.uuid4()
    # La carpeta tiene las credenciales de Dora...
    _creds(ajustes, ident, "573008927374", "Dora Niebles")

    class _BaseFalsa:
        def transaction(self):
            @contextmanager
            def scope():
                yield _SesionFalsa()

            return scope()

    class _SesionFalsa:
        def get(self, _modelo, _id):
            # ...pero la cuenta esta REGISTRADA como Ale.
            class _Fila:
                wa_pn = "573002389304@s.whatsapp.net"

            return _Fila()

    rt = object.__new__(AppRuntime)
    rt.settings = ajustes
    rt.database = _BaseFalsa()
    rt.runtime_owner_account_id = ident
    monkeypatch.setattr(
        AppRuntime,
        "_identificadores_propios",
        lambda self: ("573008927374@s.whatsapp.net", None),
    )

    assert rt._la_cuenta_es_de_esta_sesion() is False, (
        "el socket de Dora habria escrito en la cuenta de Ale"
    )


def test_se_REUTILIZA_la_cuenta_que_ya_era_de_ese_telefono(ajustes, session, cuenta):
    """Si ese numero ya tenia fila --revocada, por ejemplo-- se recupera.

    Crear otra la duplicaria y dejaria sus chats anteriores huerfanos en una
    fila que ya nadie mira.
    """
    cuenta.wa_pn = "573002389304@s.whatsapp.net"
    cuenta.phone_number = "573002389304"
    cuenta.display_name = "Ale✨🏍️"
    cuenta.session_status = "linked"
    session.flush()

    # Dora ya tenia su fila, revocada de una vez anterior.
    suya = _otra(session, cuenta.user_id, session_status="revoked")
    suya.wa_pn = "573008927374@s.whatsapp.net"
    suya.phone_number = "573008927374"
    session.flush()

    _creds(ajustes, cuenta.id, "573008927374", "Dora Niebles")

    sellada = _servicio(ajustes, session).marcar_vinculada(
        cuenta.user_id, pn=None, lid=None, account_id=cuenta.id
    )

    session.expire_all()
    assert str(sellada) == str(suya.id), "se creo una cuenta nueva pudiendo reusar"
    assert suya.session_status == "linked", "la suya sigue revocada"


def test_el_runtime_se_traspasa_a_la_cuenta_correcta():
    """Sellar otra cuenta no basta si el runtime sigue apuntando a la vieja.

    Sin el traspaso, todo lo que entre por este socket iria a la cuenta
    anterior -- justo lo que la deteccion venia a evitar.
    """
    import inspect

    from app.core.runtime import AppRuntime

    fuente = inspect.getsource(AppRuntime._persistir_vinculacion)

    assert "_traspasar_a" in fuente
    assert "str(sellada) != str(self.runtime_owner_account_id)" in fuente


def test_el_traspaso_para_el_cliente_ANTES_de_mover():
    """Baileys tiene esos ficheros abiertos.

    Moverlos en caliente falla en Windows y, peor, puede dejar la identidad
    partida entre dos carpetas: una sesion que no descifra nada.
    """
    import inspect

    from app.core.runtime import AppRuntime

    fuente = inspect.getsource(AppRuntime._traspasar_a)

    assert fuente.index("self.client.stop()") < fuente.index("shutil.move")
    assert fuente.index("shutil.move") < fuente.index("self.client.start()")


def test_si_no_se_puede_mover_NO_se_reasigna():
    """Peor que no traspasar es un runtime apuntando a una carpeta que no es."""
    import inspect

    from app.core.runtime import AppRuntime

    fuente = inspect.getsource(AppRuntime._traspasar_a)
    tras_el_fallo = fuente[fuente.index("No se pudo mover la sesion") :]

    assert "return" in tras_el_fallo.split("self.runtime_owner_account_id")[0], (
        "se reasigna el runtime aunque la sesion se haya quedado en la otra "
        "carpeta"
    )
