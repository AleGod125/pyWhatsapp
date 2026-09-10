"""Un usuario nuevo NO hereda el WhatsApp de otro.

EL FALLO, TAL Y COMO SE VIO
---------------------------
Usuario A tenia su WhatsApp vinculado y el servidor conectado. Usuario B se
registraba, entraba, y la pantalla le decia **"Cuenta vinculada"** -- la de A.

La causa de fondo: el acceso se deducia del estado GLOBAL del proceso ("hay un
WhatsApp conectado") en vez de mirar que tiene ESA persona.

LA REGLA
--------
El acceso no es una propiedad del proceso. Es una fila en
``user_whatsapp_memberships``: o esta, o no esta. Da igual cuantas cuentas haya
conectadas en el servidor.

Estas pruebas usan rutas HTTP reales con dos usuarios, no helpers sueltos: el
fallo estaba justamente en el camino que recorre el navegador.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, update

from app.auth import memberships
from app.models import UserWhatsAppMembership, WhatsAppAccount

CLAVE = "Contrasena-De-Prueba-1"


def _correo() -> str:
    return f"mu-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
def anonimo(runtime):
    """Cliente SIN cookie: lo que ve alguien que todavia no ha entrado."""
    from app.api import create_app

    runtime._montar_cuentas()
    aplicacion = create_app(runtime)
    aplicacion.config.update(TESTING=True)
    return aplicacion.test_client()


@pytest.fixture
def dos_usuarios(runtime, session):
    """A con cuenta de WhatsApp y Drive; B recien registrado, sin nada."""
    from tests.conftest import _conceder_drive, _vincular_whatsapp

    a = runtime.auth.register(email=_correo(), password=CLAVE)
    _conceder_drive(session, a.user_id)
    cuenta_a = _vincular_whatsapp(session, a.user_id)
    memberships.conceder(session, user_id=a.user_id, account_id=cuenta_a.id)

    b = runtime.auth.register(email=_correo(), password=CLAVE)
    _conceder_drive(session, b.user_id)
    session.flush()
    return {"a": a, "b": b, "cuenta_a": cuenta_a, "session": session}


# ---------------------------------------------------------------------------
# La regla, en la base
# ---------------------------------------------------------------------------


def test_UN_USUARIO_NUEVO_NO_TIENE_CUENTA(dos_usuarios, cuenta):
    """LA REGLA. Aunque exista otra cuenta vinculada en el mismo servidor."""
    sesion = dos_usuarios["session"]

    assert memberships.cuenta_de(sesion, dos_usuarios["b"].user_id) is None
    assert memberships.id_de_cuenta_de(sesion, dos_usuarios["b"].user_id) is None


def test_el_usuario_con_membresia_si_encuentra_la_suya(dos_usuarios):
    sesion = dos_usuarios["session"]
    cuenta = memberships.cuenta_de(sesion, dos_usuarios["a"].user_id)
    assert cuenta is not None
    assert cuenta.id == dos_usuarios["cuenta_a"].id


def test_B_NO_TIENE_ACCESO_A_LA_CUENTA_DE_A(dos_usuarios):
    """La comprobacion que va en toda ruta que reciba un id desde el navegador.

    Un parametro del cliente dice a que se QUIERE entrar, no a que se puede.
    """
    sesion = dos_usuarios["session"]
    cuenta_a = dos_usuarios["cuenta_a"].id

    assert memberships.tiene_acceso(sesion, dos_usuarios["a"].user_id, cuenta_a) is True
    assert memberships.tiene_acceso(sesion, dos_usuarios["b"].user_id, cuenta_a) is False


def test_sin_usuario_o_sin_cuenta_no_se_concede_nada(dos_usuarios):
    sesion = dos_usuarios["session"]
    assert memberships.tiene_acceso(sesion, None, dos_usuarios["cuenta_a"].id) is False
    assert memberships.tiene_acceso(sesion, dos_usuarios["a"].user_id, None) is False


# ---------------------------------------------------------------------------
# Una persona, VARIAS cuentas -- pero solo una activa
# ---------------------------------------------------------------------------


def _otra_cuenta(sesion, user_id):
    fila = WhatsAppAccount(
        user_id=user_id,
        session_status="never_linked",
        session_storage_key=f"accounts/{uuid.uuid4()}",
    )
    sesion.add(fila)
    sesion.flush()
    return fila


def test_una_persona_SI_puede_tener_dos_cuentas(dos_usuarios):
    """El WhatsApp personal y el del trabajo son cosas distintas.

    Aqui habia un `UNIQUE(user_id)` en la membresia -- "una cuenta por
    persona" -- que era correcto mientras no habia forma de elegir cuenta en
    la interfaz. Ahora la hay, asi que la regla cambio.
    """
    sesion = dos_usuarios["session"]
    yo = dos_usuarios["a"].user_id
    otra = _otra_cuenta(sesion, yo)

    memberships.conceder(sesion, user_id=yo, account_id=otra.id)

    mias = {str(c.id) for c in memberships.cuentas_de_usuario(sesion, yo)}
    assert str(otra.id) in mias
    assert len(mias) >= 2


def test_SOLO_UNA_puede_estar_activa(dos_usuarios):
    """Lo garantiza la base con un indice unico parcial, no el codigo.

    Dos peticiones a la vez dejarian dos activas, y la siguiente lectura
    elegiria una al azar: el usuario veria un WhatsApp u otro sin tocar nada.
    """
    sesion = dos_usuarios["session"]
    yo = dos_usuarios["a"].user_id
    primera = dos_usuarios["cuenta_a"].id
    otra = _otra_cuenta(sesion, yo)

    memberships.conceder(sesion, user_id=yo, account_id=primera, activar=True)
    memberships.conceder(sesion, user_id=yo, account_id=otra.id, activar=True)

    activa = memberships.cuenta_activa_de(sesion, yo)
    assert activa is not None and str(activa.id) == str(otra.id)

    from app.models import UserWhatsAppMembership
    from sqlalchemy import func, select

    cuantas = sesion.execute(
        select(func.count())
        .select_from(UserWhatsAppMembership)
        .where(
            UserWhatsAppMembership.user_id == yo,
            UserWhatsAppMembership.is_active.is_(True),
        )
    ).scalar()
    assert cuantas == 1, "quedaron dos cuentas activas a la vez"


def test_activar_una_cuenta_AJENA_no_hace_nada(dos_usuarios):
    """El identificador llega del navegador: no se cree por si solo.

    Sin esta comprobacion, cambiar un parametro dejaria a alguien mirando la
    copia de seguridad de otra persona.
    """
    sesion = dos_usuarios["session"]
    # Una cuenta de B, a la que A no tiene ningun acceso.
    ajena = _otra_cuenta(sesion, dos_usuarios["b"].user_id)
    memberships.conceder(
        sesion, user_id=dos_usuarios["b"].user_id, account_id=ajena.id
    )

    hecho = memberships.activar_cuenta(
        sesion, user_id=dos_usuarios["a"].user_id, account_id=ajena.id
    )

    assert hecho is False
    activa = memberships.cuenta_activa_de(sesion, dos_usuarios["a"].user_id)
    assert activa is None or str(activa.id) != str(ajena.id)


def test_conceder_lo_mismo_dos_veces_no_es_un_error(dos_usuarios):
    """Idempotente: repetir la misma asociacion no duplica ni revienta."""
    sesion = dos_usuarios["session"]
    primera = memberships.conceder(
        sesion,
        user_id=dos_usuarios["a"].user_id,
        account_id=dos_usuarios["cuenta_a"].id,
    )
    segunda = memberships.conceder(
        sesion,
        user_id=dos_usuarios["a"].user_id,
        account_id=dos_usuarios["cuenta_a"].id,
    )
    assert primera.id == segunda.id


# ---------------------------------------------------------------------------
# Varias personas, una cuenta: la puerta que esta tabla abre
# ---------------------------------------------------------------------------


def test_VARIAS_PERSONAS_PUEDEN_COMPARTIR_UNA_CUENTA(dos_usuarios, cuenta):
    """Socios, parejas, equipos. Una sola sesion de WhatsApp para las dos.

    Es el motivo de que NO exista `UNIQUE(whatsapp_account_id)`: ponerlo
    cerraria justo la puerta que esta tabla existe para abrir.
    """
    sesion = dos_usuarios["session"]
    cuenta_a = dos_usuarios["cuenta_a"].id

    memberships.conceder(
        sesion,
        user_id=dos_usuarios["b"].user_id,
        account_id=cuenta_a,
        role=memberships.MIEMBRO,
    )

    miembros = memberships.miembros_de(sesion, cuenta_a)
    assert set(miembros) == {dos_usuarios["a"].user_id, dos_usuarios["b"].user_id}
    # Y los dos apuntan a la MISMA cuenta: una sesion, un Signal Store.
    assert memberships.id_de_cuenta_de(sesion, dos_usuarios["b"].user_id) == cuenta_a
    assert memberships.id_de_cuenta_de(sesion, dos_usuarios["a"].user_id) == cuenta_a


def test_los_roles_se_distinguen(dos_usuarios):
    sesion = dos_usuarios["session"]
    memberships.conceder(
        sesion,
        user_id=dos_usuarios["b"].user_id,
        account_id=dos_usuarios["cuenta_a"].id,
        role=memberships.MIEMBRO,
    )
    filas = {
        f.user_id: f.role
        for f in sesion.execute(select(UserWhatsAppMembership)).scalars().all()
    }
    assert filas[dos_usuarios["a"].user_id] == memberships.DUENO
    assert filas[dos_usuarios["b"].user_id] == memberships.MIEMBRO


# ---------------------------------------------------------------------------
# LO QUE NO SE HACE: compartir en silencio
# ---------------------------------------------------------------------------


def test_COMPARTIR_NUNCA_OCURRE_SOLO(dos_usuarios, cuenta):
    """La prueba que mas importa de este modelo.

    No hay ni una via por la que aparezca una membresia sin que alguien la
    conceda. Reconocer un numero no puede dar acceso: seria entregar el
    historial de otra persona a quien tenga su movil un minuto.
    """
    sesion = dos_usuarios["session"]

    # Se mira la cuenta de A todo lo que se quiera: B sigue sin nada.
    memberships.cuenta_de(sesion, dos_usuarios["a"].user_id)
    memberships.miembros_de(sesion, dos_usuarios["cuenta_a"].id)
    memberships.tiene_acceso(
        sesion, dos_usuarios["b"].user_id, dos_usuarios["cuenta_a"].id
    )

    assert memberships.cuenta_de(sesion, dos_usuarios["b"].user_id) is None


def test_el_modulo_no_concede_nada_por_su_cuenta():
    """Guardia de codigo: nadie mas puede crear una membresia.

    `conceder` es la via normal. `activar_cuenta` tambien escribe, y esta
    permitido a proposito: crea la fila que le FALTA a una cuenta creada antes
    de que existieran las membresias, y esa cuenta ya era de ese usuario por
    `whatsapp_accounts.user_id`. No concede acceso nuevo -- anota uno que ya
    existia. Sin eso, quien tenga una de esas no podria seleccionarla nunca.

    Cualquier otra funcion que empiece a escribir aqui hace saltar esto, que
    es justo lo que se quiere: conceder acceso tiene que ser una decision
    visible, no un efecto secundario.
    """
    import ast
    import pathlib

    PERMITIDAS = {"conceder", "activar_cuenta"}

    arbol = ast.parse(
        pathlib.Path("app/auth/memberships.py").read_text(encoding="utf-8")
    )
    escriben = {
        n.name
        for n in ast.walk(arbol)
        if isinstance(n, ast.FunctionDef)
        and any(
            isinstance(x, ast.Attribute) and x.attr in ("add", "merge")
            for x in ast.walk(n)
        )
    }
    assert escriben <= PERMITIDAS, f"escriben sin permiso: {escriben - PERMITIDAS}"


# ---------------------------------------------------------------------------
# El camino que recorre el navegador
# ---------------------------------------------------------------------------
#
# El fallo estaba aqui, no en un helper: por eso estas pruebas van por HTTP.


def _entrar(cliente, correo):
    return cliente.post("/api/v1/auth/login", json={"email": correo, "password": CLAVE})


def test_B_VE_QUE_TIENE_QUE_VINCULAR_AUNQUE_A_ESTE_CONECTADO(
    anonimo, runtime, session
):
    """EL FALLO EXACTO. B entraba y le decian "Cuenta vinculada" -- la de A."""
    from tests.conftest import _conceder_drive, _vincular_whatsapp

    correo_a = _correo()
    a = runtime.auth.register(email=correo_a, password=CLAVE)
    _conceder_drive(session, a.user_id)
    cuenta_a = _vincular_whatsapp(session, a.user_id)
    memberships.conceder(session, user_id=a.user_id, account_id=cuenta_a.id)

    correo_b = _correo()
    b = runtime.auth.register(email=correo_b, password=CLAVE)
    _conceder_drive(session, b.user_id)
    session.flush()

    assert _entrar(anonimo, correo_b).status_code == 200

    estado = anonimo.get("/api/v1/onboarding/status").get_json()
    assert estado["whatsapp_linked"] is False, "B no tiene WhatsApp"
    assert estado["next_step"] == "pairing"

    sesion = anonimo.get("/api/v1/session").get_json()
    assert sesion.get("linked") is not True
    assert sesion.get("connected") is not True
    assert sesion.get("owned_by_another_user") is True


def test_A_SI_ENTRA_AL_PANEL(anonimo, runtime, session, cuenta):
    """Y el que si tiene su cuenta no se ve afectado por el arreglo."""
    from tests.conftest import _conceder_drive, _vincular_whatsapp

    correo_a = _correo()
    a = runtime.auth.register(email=correo_a, password=CLAVE)
    _conceder_drive(session, a.user_id)
    cuenta_a = _vincular_whatsapp(session, a.user_id)
    memberships.conceder(session, user_id=a.user_id, account_id=cuenta_a.id)
    session.flush()

    assert _entrar(anonimo, correo_a).status_code == 200

    estado = anonimo.get("/api/v1/onboarding/status").get_json()
    assert estado["whatsapp_linked"] is True
    assert estado["next_step"] == "dashboard"


def test_una_cuenta_compartida_deja_entrar_a_los_dos(anonimo, runtime, session):
    """B recibe acceso EXPLICITO a la cuenta de A: entonces si entra."""
    from tests.conftest import _conceder_drive, _vincular_whatsapp

    a = runtime.auth.register(email=_correo(), password=CLAVE)
    _conceder_drive(session, a.user_id)
    cuenta_a = _vincular_whatsapp(session, a.user_id)
    memberships.conceder(session, user_id=a.user_id, account_id=cuenta_a.id)

    correo_b = _correo()
    b = runtime.auth.register(email=correo_b, password=CLAVE)
    _conceder_drive(session, b.user_id)
    memberships.conceder(
        session,
        user_id=b.user_id,
        account_id=cuenta_a.id,
        role=memberships.MIEMBRO,
    )
    session.flush()

    assert _entrar(anonimo, correo_b).status_code == 200
    estado = anonimo.get("/api/v1/onboarding/status").get_json()
    assert estado["whatsapp_linked"] is True
    assert estado["next_step"] == "dashboard"


# ---------------------------------------------------------------------------
# Aislamiento de datos: el mismo contacto en dos cuentas
# ---------------------------------------------------------------------------
#
# Cuatro restricciones globales lo impedian o lo cruzaban:
#
#   chats               UNIQUE(jid)                  -> la segunda cuenta no
#                                                       podia ni insertar
#   contacts            UNIQUE(jid), sin cuenta      -> el nombre de A en la
#                                                       agenda de B
#   chat_history_state  UNIQUE(chat_jid)             -> el progreso se cruzaba
#   messages            UNIQUE(chat_jid, wa_msg_id)  -> el mensaje de B se
#                                                       descartaba EN SILENCIO


@pytest.fixture
def dos_cuentas(runtime, session):
    """Dos cuentas de WhatsApp distintas, cada una de su usuario.

    Se crean a mano y no con `_vincular_whatsapp`: ese ayudante BORRA las
    cuentas de los demas usuarios --tiene sentido en el mundo de una sola
    cuenta, donde `dueno_actual()` devuelve la primera que encuentre-- y aqui
    hacen falta las dos vivas a la vez.
    """
    a = runtime.auth.register(email=_correo(), password=CLAVE)
    b = runtime.auth.register(email=_correo(), password=CLAVE)
    cuentas = []
    for usuario in (a, b):
        cuenta = WhatsAppAccount(
            user_id=usuario.user_id,
            session_status="linked",
            session_storage_key=f"accounts/{uuid.uuid4().hex}",
        )
        session.add(cuenta)
        cuentas.append(cuenta)
    session.flush()
    return {"session": session, "x": cuentas[0], "y": cuentas[1]}


JID = "111222333@s.whatsapp.net"


def _chat(sesion, cuenta, jid=JID, nombre=None):
    from app.models import Chat

    fila = Chat(
        jid=jid, chat_type="individual", name=nombre, whatsapp_account_id=cuenta.id
    )
    sesion.add(fila)
    sesion.flush()
    return fila


def test_EL_MISMO_JID_PUEDE_EXISTIR_EN_DOS_CUENTAS(dos_cuentas):
    """LA REGLA. Antes la segunda insercion reventaba contra UNIQUE(jid)."""
    sesion = dos_cuentas["session"]

    en_x = _chat(sesion, dos_cuentas["x"], nombre="Mama")
    en_y = _chat(sesion, dos_cuentas["y"], nombre="Cliente")

    assert en_x.id != en_y.id
    assert en_x.jid == en_y.jid == JID
    assert en_x.whatsapp_account_id != en_y.whatsapp_account_id


def test_dentro_de_UNA_cuenta_el_jid_sigue_siendo_unico(dos_cuentas):
    """El aislamiento no puede costar duplicados dentro de la misma cuenta."""
    import sqlalchemy.exc

    sesion = dos_cuentas["session"]
    _chat(sesion, dos_cuentas["x"])
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        _chat(sesion, dos_cuentas["x"])


def test_CADA_UNA_LE_PONE_SU_NOMBRE_AL_MISMO_NUMERO(dos_cuentas):
    """La agenda es privada: A guarda "Mama" y B no lo ve."""
    from app.models import Contact

    sesion = dos_cuentas["session"]
    sesion.add(
        Contact(
            jid=JID, display_name="Mama", whatsapp_account_id=dos_cuentas["x"].id
        )
    )
    sesion.add(
        Contact(
            jid=JID, display_name="Cliente", whatsapp_account_id=dos_cuentas["y"].id
        )
    )
    sesion.flush()

    nombres = {
        c.whatsapp_account_id: c.display_name
        for c in sesion.execute(select(Contact).where(Contact.jid == JID))
        .scalars()
        .all()
    }
    assert nombres[dos_cuentas["x"].id] == "Mama"
    assert nombres[dos_cuentas["y"].id] == "Cliente"


def test_EL_MISMO_MENSAJE_DE_GRUPO_LLEGA_A_LAS_DOS(dos_cuentas):
    """El fallo mas silencioso de todos.

    Un mensaje de grupo lleva el MISMO identificador de WhatsApp para todos
    los que lo reciben. Con la unicidad por `chat_jid`, el que le llegaba a la
    segunda persona se descartaba como duplicado del de la primera: sin error,
    sin aviso, sin mensaje.
    """
    from app.models import Message

    sesion = dos_cuentas["session"]
    en_x = _chat(sesion, dos_cuentas["x"])
    en_y = _chat(sesion, dos_cuentas["y"])
    wamid = "3EB0" + uuid.uuid4().hex[:16].upper()

    for chat, texto in ((en_x, "A SECRET"), (en_y, "B SECRET")):
        sesion.add(
            Message(
                chat_id=chat.id,
                chat_jid=chat.jid,
                whatsapp_message_id=wamid,
                timestamp=1_788_700_000,
                from_me=False,
                message_type="text",
                text=texto,
                source="live",
            )
        )
    sesion.flush()

    guardados = {
        m.chat_id: m.text
        for m in sesion.execute(
            select(Message).where(Message.whatsapp_message_id == wamid)
        )
        .scalars()
        .all()
    }
    assert guardados[en_x.id] == "A SECRET"
    assert guardados[en_y.id] == "B SECRET"


def test_dentro_de_una_conversacion_no_se_duplica(dos_cuentas):
    """Y el dedupe que SI hace falta sigue funcionando."""
    import sqlalchemy.exc

    from app.models import Message

    sesion = dos_cuentas["session"]
    chat = _chat(sesion, dos_cuentas["x"])
    wamid = "3EB0" + uuid.uuid4().hex[:16].upper()

    def _poner():
        sesion.add(
            Message(
                chat_id=chat.id,
                chat_jid=chat.jid,
                whatsapp_message_id=wamid,
                timestamp=1_788_700_000,
                from_me=False,
                message_type="text",
                source="live",
            )
        )
        sesion.flush()

    _poner()
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        _poner()


def test_EL_PROGRESO_DE_HISTORIAL_NO_SE_CRUZA(dos_cuentas):
    """Misma conversacion, progreso distinto. Antes solo cabia una fila."""
    from app.models import ChatHistoryState

    sesion = dos_cuentas["session"]
    en_x = _chat(sesion, dos_cuentas["x"])
    en_y = _chat(sesion, dos_cuentas["y"])

    sesion.add(
        ChatHistoryState(
            chat_id=en_x.id, chat_jid=JID, history_status="waiting_seed"
        )
    )
    sesion.add(
        ChatHistoryState(chat_id=en_y.id, chat_jid=JID, history_status="exhausted")
    )
    sesion.flush()

    estados = {
        h.chat_id: h.history_status
        for h in sesion.execute(
            select(ChatHistoryState).where(ChatHistoryState.chat_jid == JID)
        )
        .scalars()
        .all()
    }
    assert estados[en_x.id] == "waiting_seed"
    assert estados[en_y.id] == "exhausted"


# ---------------------------------------------------------------------------
# Las consultas privadas, con el caso ambiguo PROVOCADO
# ---------------------------------------------------------------------------
#
# Ahora que el mismo jid puede existir en dos cuentas, cualquier consulta que
# busque solo por `chat_jid` y espere UNA fila es insegura. Estas pruebas crean
# el caso a proposito y comprueban que las lecturas del motor no cruzan.


@pytest.fixture
def mismo_jid_en_dos_cuentas(dos_cuentas):
    """La misma conversacion, una en cada cuenta, con su propio estado."""
    from app.models import ChatHistoryState

    sesion = dos_cuentas["session"]
    en_x = _chat(sesion, dos_cuentas["x"], nombre="Chat A")
    en_y = _chat(sesion, dos_cuentas["y"], nombre="Chat B")
    sesion.add(
        ChatHistoryState(chat_id=en_x.id, chat_jid=JID, history_status="fetching")
    )
    sesion.add(
        ChatHistoryState(chat_id=en_y.id, chat_jid=JID, history_status="waiting_seed")
    )
    sesion.flush()
    return {**dos_cuentas, "en_x": en_x, "en_y": en_y}


def test_EL_CURSOR_SE_LEE_POR_CHAT_NO_POR_JID(mismo_jid_en_dos_cuentas):
    """La lectura del ancla no puede cruzar cuentas.

    `get_valid_history_cursor` recibe `chat_id`, y por eso resuelve LA fila.
    Sin el, con dos chats del mismo jid, o devolveria la de otra persona o
    reventaria por encontrar dos.
    """
    from app.history.cursor import get_valid_history_cursor
    from app.models import Message

    sesion = mismo_jid_en_dos_cuentas["session"]
    en_x = mismo_jid_en_dos_cuentas["en_x"]
    en_y = mismo_jid_en_dos_cuentas["en_y"]

    # Solo la conversacion de X tiene un mensaje con identificador real.
    sesion.add(
        Message(
            chat_id=en_x.id,
            chat_jid=JID,
            whatsapp_message_id="3A1F8BDD4678EB6DE395",
            timestamp=1_788_700_000,
            from_me=False,
            message_type="text",
            source="live",
        )
    )
    sesion.flush()

    de_x = get_valid_history_cursor(sesion, chat_id=en_x.id, chat_jid=JID)
    de_y = get_valid_history_cursor(sesion, chat_id=en_y.id, chat_jid=JID)

    assert de_x is not None and de_x.wa_msg_id == "3A1F8BDD4678EB6DE395"
    assert de_y is None, "Y no tiene ancla propia y no puede heredar la de X"


def test_ESCRIBIR_EL_CURSOR_DE_UNA_NO_TOCA_LA_OTRA(mismo_jid_en_dos_cuentas):
    """LA PRUEBA QUE MAS IMPORTA de este bloque.

    Es el fallo que se corrigio en los 13 UPDATE: por `chat_jid` habrian
    quedado escritas LAS DOS filas, sin error y sin aviso.
    """
    from app.history.cursor import CursorInfo, persist_cursor
    from app.models import ChatHistoryState

    sesion = mismo_jid_en_dos_cuentas["session"]
    en_x = mismo_jid_en_dos_cuentas["en_x"]
    en_y = mismo_jid_en_dos_cuentas["en_y"]

    persist_cursor(
        sesion,
        JID,
        CursorInfo("3A1F8BDD4678EB6DE395", 1_788_700_000, False, source="seed"),
        chat_id=en_x.id,
    )
    sesion.flush()

    estados = {
        h.chat_id: h
        for h in sesion.execute(
            select(ChatHistoryState).where(ChatHistoryState.chat_jid == JID)
        )
        .scalars()
        .all()
    }
    assert estados[en_x.id].oldest_message_id == "3A1F8BDD4678EB6DE395"
    assert estados[en_y.id].oldest_message_id is None, "la de Y no se toca"


def test_LIMPIAR_REINTENTOS_SOLO_AFECTA_A_SU_CHAT(mismo_jid_en_dos_cuentas):
    from app.history.cursor import limpiar_reintentos
    from app.models import ChatHistoryState

    sesion = mismo_jid_en_dos_cuentas["session"]
    en_x = mismo_jid_en_dos_cuentas["en_x"]
    en_y = mismo_jid_en_dos_cuentas["en_y"]

    for chat in (en_x, en_y):
        sesion.execute(
            update(ChatHistoryState)
            .where(ChatHistoryState.chat_id == chat.id)
            .values(attempt_count=3)
        )
    sesion.flush()

    limpiar_reintentos(sesion, JID, chat_id=en_x.id)
    sesion.flush()

    intentos = {
        h.chat_id: h.attempt_count
        for h in sesion.execute(
            select(ChatHistoryState).where(ChatHistoryState.chat_jid == JID)
        )
        .scalars()
        .all()
    }
    assert intentos[en_x.id] == 0
    assert intentos[en_y.id] == 3, "el contador de Y se queda como estaba"


def test_EL_ESTADO_DE_HISTORIAL_NO_SE_CRUZA(mismo_jid_en_dos_cuentas):
    """Misma conversacion, progresos distintos, cada uno el suyo."""
    from app.models import ChatHistoryState

    sesion = mismo_jid_en_dos_cuentas["session"]
    estados = {
        h.chat_id: h.history_status
        for h in sesion.execute(
            select(ChatHistoryState).where(ChatHistoryState.chat_jid == JID)
        )
        .scalars()
        .all()
    }
    assert estados[mismo_jid_en_dos_cuentas["en_x"].id] == "fetching"
    assert estados[mismo_jid_en_dos_cuentas["en_y"].id] == "waiting_seed"


def test_LOS_CONTEOS_DE_UNA_CUENTA_NO_INCLUYEN_LOS_DE_LA_OTRA(
    mismo_jid_en_dos_cuentas, settings
):
    """El resumen que ve cada persona cuenta solo lo suyo."""
    from app.history.resumen import resumen_de_estado

    sesion = mismo_jid_en_dos_cuentas["session"]

    class _Db:
        def transaction(self):
            from contextlib import contextmanager

            @contextmanager
            def scope():
                yield sesion

            return scope()

    de_x = resumen_de_estado(_Db(), account_id=mismo_jid_en_dos_cuentas["x"].id)
    de_y = resumen_de_estado(_Db(), account_id=mismo_jid_en_dos_cuentas["y"].id)

    assert de_x.chats_total == 1
    assert de_y.chats_total == 1
    assert de_x.fetching == 1 and de_x.waiting_seed == 0
    assert de_y.waiting_seed == 1 and de_y.fetching == 0

