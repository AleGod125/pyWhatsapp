"""Una sesion que el telefono desvinculo, y a donde va el usuario despues.

EL BUCLE QUE SE MIDIO, EN EL LOG DEL USUARIO
--------------------------------------------
::

    [APP] Cerrojo adquirido (account:229a9753)      <- cuenta NUEVA, never_linked
    [WA]  Estado: STARTING -> CHECKING_SESSION (device.json presente)
    [401] rechazo 1/3 huella=032034d0
          Login rechazado ({'reason': 'loggedOut'})
    [WA]  Estado: CHECKING_SESSION -> SESSION_INVALID
    [WA]  Estado: SESSION_INVALID -> PAIRING (vinculacion solicitada)
    [APP] [401] reintento con la sesion guardada (aun no se descarta)
    [WA]  Estado: PAIRING -> CONNECTING (reintento con la sesion guardada)
    [401] ... y vuelta a empezar

El usuario pedia un codigo QR y no se generaba NINGUNO. ``restart_pairing``
encontraba el ``device.json`` muerto y volvia a hacer login con el en vez de
pedir codigo; el servidor lo rechazaba; se pedia otra vez. La pantalla se
quedaba en "Conexion confirmada. Abriendo tus conversaciones..." para siempre.

LAS TRES COSAS QUE FALLABAN
---------------------------
1. ``loggedOut`` se contaba como un 401 cualquiera y habia que esperar a tres
   rechazos. Pero ``loggedOut`` no es ambiguo: es el telefono diciendo que
   quito este dispositivo, y no cambia de opinion al tercer intento.
2. La columna ``session_status`` se quedaba en ``linked`` para siempre. El
   selector pintaba el punto verde de "conectada" sobre una cuenta
   desvinculada.
3. El onboarding juzgaba al usuario ENTERO por su cuenta activa. Agregar una
   cuenta nueva --que nace ``never_linked`` y se vuelve la activa-- lo sacaba
   del panel, que es justo donde esta el selector para volver a la buena.
"""

from __future__ import annotations

import uuid

import pytest


# ---------------------------------------------------------------------------
# 1. `loggedOut` es definitivo a la primera
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "motivo",
    [
        {"reason": "loggedOut"},
        "loggedOut",
        "stream:error code=401 conflict type=device_removed",
        {"reason": "device_removed"},
    ],
)
def test_el_telefono_la_quito_no_es_ambiguo(motivo):
    """No hay nada que reintentar: la vinculacion ya no existe."""
    from app.core.runtime import AppRuntime

    assert AppRuntime._es_desvinculacion(motivo) is True


@pytest.mark.parametrize("motivo", [401, "401", {"reason": "restartRequired"}, None])
def test_un_401_cualquiera_SI_se_reintenta(motivo):
    """Un corte de red no puede tirar una vinculacion buena.

    Es el peor incidente que ha tenido este proyecto: 74 logins y 61 codigos
    QR en segundos, y 99 carpetas de sesion vacias. Por eso los motivos que no
    se reconocen siguen contando hasta tres.
    """
    from app.core.runtime import AppRuntime

    assert AppRuntime._es_desvinculacion(motivo) is False


# ---------------------------------------------------------------------------
# 2. La base se entera de la revocacion
# ---------------------------------------------------------------------------


def test_revoked_NO_cuenta_como_vinculada():
    """Si contara, el selector seguiria pintandola como utilizable."""
    from app.models.accounts import LINKED_STATUSES

    assert "revoked" not in LINKED_STATUSES
    # Y `disconnected` SI cuenta: una caida de red no es una desvinculacion.
    assert "disconnected" in LINKED_STATUSES


def test_marcar_estado_no_ADIVINA_con_varias_cuentas(session, runtime):
    """Anotar sobre la cuenta equivocada es peor que no anotar.

    El respaldo "la mas antigua" viene de cuando solo podia haber una cuenta.
    Con dos, marcar `revoked` a ciegas deja la buena como muerta y la muerta
    como viva, sin nada en el log que lo delate.
    """
    from app.models import User, WhatsAppAccount

    usuario = User(
        email=f"dos-{uuid.uuid4().hex[:10]}@example.com", password_hash="x"
    )
    session.add(usuario)
    session.flush()
    creadas = []
    for _ in range(2):
        cid = uuid.uuid4()
        session.add(
            WhatsAppAccount(
                id=cid,
                user_id=usuario.id,
                session_status="linked",
                session_storage_key=f"accounts/{cid}",
            )
        )
        session.flush()
        creadas.append(cid)

    runtime.whatsapp_accounts.marcar_estado(usuario.id, "revoked")

    session.expire_all()
    estados = {session.get(WhatsAppAccount, c).session_status for c in creadas}
    assert estados == {"linked"}, "marco una cuenta a ciegas"


def test_con_UNA_sola_cuenta_si_se_anota(session, runtime):
    """Ahi no hay nada que adivinar, y las llamadas antiguas siguen valiendo."""
    from app.models import User, WhatsAppAccount

    usuario = User(
        email=f"una-{uuid.uuid4().hex[:10]}@example.com", password_hash="x"
    )
    session.add(usuario)
    session.flush()
    cid = uuid.uuid4()
    session.add(
        WhatsAppAccount(
            id=cid,
            user_id=usuario.id,
            session_status="linked",
            session_storage_key=f"accounts/{cid}",
        )
    )
    session.flush()

    runtime.whatsapp_accounts.marcar_estado(usuario.id, "revoked")

    session.expire_all()
    assert session.get(WhatsAppAccount, cid).session_status == "revoked"


def test_con_account_id_se_anota_la_que_toca(session, runtime):
    from app.models import User, WhatsAppAccount

    usuario = User(
        email=f"pin-{uuid.uuid4().hex[:10]}@example.com", password_hash="x"
    )
    session.add(usuario)
    session.flush()
    creadas = []
    for _ in range(2):
        cid = uuid.uuid4()
        session.add(
            WhatsAppAccount(
                id=cid,
                user_id=usuario.id,
                session_status="linked",
                session_storage_key=f"accounts/{cid}",
            )
        )
        session.flush()
        creadas.append(cid)

    runtime.whatsapp_accounts.marcar_estado(
        usuario.id, "revoked", account_id=creadas[1]
    )

    session.expire_all()
    assert session.get(WhatsAppAccount, creadas[0]).session_status == "linked"
    assert session.get(WhatsAppAccount, creadas[1]).session_status == "revoked"


# ---------------------------------------------------------------------------
# 3. A donde va el usuario: el bucle pairing <-> dashboard
# ---------------------------------------------------------------------------


def _cuenta(session, usuario_id, estado: str):
    from app.models import WhatsAppAccount

    cid = uuid.uuid4()
    session.add(
        WhatsAppAccount(
            id=cid,
            user_id=usuario_id,
            session_status=estado,
            session_storage_key=f"accounts/{cid}",
        )
    )
    session.flush()
    return cid


def test_una_cuenta_nueva_NO_saca_del_panel(cliente, session, cuenta_del_cliente):
    """El caso exacto de la captura.

    Agregar una cuenta la crea `never_linked` y la vuelve la activa. Juzgando
    al usuario por ella, el onboarding contestaba "pairing" y el guard lo
    echaba del panel -- donde esta el selector con el que volveria a la que si
    funciona. Y el pairing, leyendo el runtime, decia "conexion confirmada" y
    navegaba al panel otra vez: un bucle sin salida.
    """
    from app.auth.memberships import activar_cuenta

    nueva = _cuenta(session, cuenta_del_cliente.user_id, "never_linked")
    activar_cuenta(session, user_id=cuenta_del_cliente.user_id, account_id=nueva)
    session.flush()

    cuerpo = cliente.get("/api/v1/onboarding/status").get_json()

    assert cuerpo["whatsapp_linked"] is True
    assert cuerpo["next_step"] == "dashboard"


def test_una_cuenta_revocada_tampoco_si_queda_otra(
    cliente, session, cuenta_del_cliente
):
    """Cambiar de cuenta se hace desde el panel, asi que hay que dejarle entrar."""
    from app.auth.memberships import activar_cuenta

    muerta = _cuenta(session, cuenta_del_cliente.user_id, "revoked")
    activar_cuenta(session, user_id=cuenta_del_cliente.user_id, account_id=muerta)
    session.flush()

    cuerpo = cliente.get("/api/v1/onboarding/status").get_json()

    assert cuerpo["next_step"] == "dashboard"


def test_sin_NINGUNA_utilizable_si_manda_a_vincular(cliente, session):
    """Lo contrario tambien tiene que seguir funcionando.

    Si todas estan revocadas no hay nada que enseñar en el panel, y dejarle
    entrar seria dejarle en una pantalla que no puede funcionar.
    """
    from sqlalchemy import update

    from app.models import WhatsAppAccount

    antes = cliente.get("/api/v1/onboarding/status").get_json()
    usuario_id = antes["user"]["id"]

    session.execute(
        update(WhatsAppAccount)
        .where(WhatsAppAccount.user_id == uuid.UUID(usuario_id))
        .values(session_status="revoked")
    )
    session.flush()

    cuerpo = cliente.get("/api/v1/onboarding/status").get_json()

    assert cuerpo["whatsapp_linked"] is False
    assert cuerpo["next_step"] == "pairing"


# ---------------------------------------------------------------------------
# NUNCA se sella una cuenta a ciegas
# ---------------------------------------------------------------------------
#
# LO QUE PASO, MEDIDO EN LA INSTALACION DEL USUARIO
# -------------------------------------------------
# Sus cuatro cuentas acabaron con el numero, el LID y el nombre de OTRO
# telefono::
#
#     creds de 69900813  ->  573002389304   (su numero real)
#     wa_pn en la base   ->  573008927374   (el del otro telefono)
#
# `marcar_vinculada` tenia el mismo respaldo que `marcar_estado`: sin
# `account_id`, coger "la mas antigua". Con una sola cuenta acierta siempre;
# con cuatro, sella a la equivocada.
#
# Y `wa_pn` no es decorativo: marca el "chat contigo mismo" y es lo que
# permite atribuir una sesion suelta a su cuenta. Cambiado, la aplicacion cree
# que una cuenta es otra.


def _cuentas_de(session, cuantas: int):
    from app.models import User, WhatsAppAccount

    usuario = User(
        email=f"sello-{uuid.uuid4().hex[:10]}@example.com", password_hash="x"
    )
    session.add(usuario)
    session.flush()
    creadas = []
    for _ in range(cuantas):
        cid = uuid.uuid4()
        session.add(
            WhatsAppAccount(
                id=cid,
                user_id=usuario.id,
                session_status="never_linked",
                session_storage_key=f"accounts/{cid}",
            )
        )
        session.flush()
        creadas.append(cid)
    return usuario, creadas


def test_con_VARIAS_cuentas_no_se_sella_ninguna(session, runtime):
    """Antes sellaba la mas antigua con el numero de otro telefono."""
    from app.models import WhatsAppAccount

    usuario, creadas = _cuentas_de(session, 3)

    runtime.whatsapp_accounts.marcar_vinculada(
        usuario.id,
        pn="573008927374@s.whatsapp.net",
        lid="85539055239413@lid",
        profile_name="Dora Niebles",
    )

    session.expire_all()
    for cid in creadas:
        fila = session.get(WhatsAppAccount, cid)
        assert fila.wa_pn is None, "sello una cuenta a ciegas con otro numero"
        assert fila.session_status == "never_linked"
        assert fila.display_name is None


def test_con_UNA_sola_si_se_sella(session, runtime):
    """Ahi no hay nada que adivinar: las llamadas antiguas siguen valiendo."""
    from app.models import WhatsAppAccount

    usuario, (cid,) = _cuentas_de(session, 1)

    runtime.whatsapp_accounts.marcar_vinculada(
        usuario.id,
        pn="573002389304@s.whatsapp.net",
        lid="86531142340710@lid",
        profile_name="Ale",
    )

    session.expire_all()
    fila = session.get(WhatsAppAccount, cid)
    assert fila.wa_pn == "573002389304@s.whatsapp.net"
    assert fila.phone_number == "573002389304"
    assert fila.session_status == "linked"


def test_con_account_id_se_sella_la_que_toca(session, runtime):
    from app.models import WhatsAppAccount

    usuario, creadas = _cuentas_de(session, 3)

    runtime.whatsapp_accounts.marcar_vinculada(
        usuario.id,
        pn="573008927374@s.whatsapp.net",
        lid="85539055239413@lid",
        account_id=creadas[2],
        profile_name="Dora Niebles",
    )

    session.expire_all()
    assert session.get(WhatsAppAccount, creadas[2]).wa_pn == "573008927374@s.whatsapp.net"
    assert session.get(WhatsAppAccount, creadas[0]).wa_pn is None
    assert session.get(WhatsAppAccount, creadas[1]).wa_pn is None


def test_no_se_sella_la_cuenta_de_OTRO_usuario(session, runtime, cuenta):
    """Un identificador equivocado no puede tocar la cuenta de otra persona."""
    from app.models import WhatsAppAccount

    usuario, _creadas = _cuentas_de(session, 1)
    antes = session.get(WhatsAppAccount, cuenta.id).wa_pn

    runtime.whatsapp_accounts.marcar_vinculada(
        usuario.id,
        pn="573008927374@s.whatsapp.net",
        lid=None,
        account_id=cuenta.id,  # ajena
    )

    session.expire_all()
    assert session.get(WhatsAppAccount, cuenta.id).wa_pn == antes
