"""Dos WhatsApp del MISMO usuario no se mezclan.

EL RIESGO, QUE ERA REAL
-----------------------
El esquema admite varias cuentas por usuario desde hace tiempo --``chats`` y
``contacts`` llevan ``whatsapp_account_id``, y el registro levanta un runtime
por cuenta--, pero los listados se acotaban por USUARIO::

    cuentas = ownership.cuentas_de(sesion, usuario_actual().id)   # <- TODAS

Con una sola cuenta daba igual. Con dos, el panel juntaba en una misma lista
los chats del WhatsApp personal y los del de trabajo, sin que nada lo
delatara: no hay columna en la fila que diga de cual es cada conversacion, asi
que el usuario no tenia forma de notarlo.

LA DISTINCION QUE HAY QUE MANTENER
----------------------------------
No es lo mismo lo que alguien PUEDE tocar que lo que se le ENSEÑA:

* autorizacion (``chat_es_de``): todas sus cuentas. Ese chat es suyo.
* contexto (los listados): UNA. Es el WhatsApp que esta mirando.

Mezclar las dos ideas es lo que produjo el fallo, y por eso aqui se prueban
por separado.
"""

from __future__ import annotations

import uuid

import pytest

from app.auth import ownership
from app.services import repository as repo


@pytest.fixture
def dos_cuentas(session):
    """Un usuario con DOS WhatsApp vinculados, cada uno con su chat."""
    from app.models import Chat, Message, User, WhatsAppAccount

    usuario = User(
        email=f"dos-{uuid.uuid4().hex[:10]}@example.com", password_hash="x"
    )
    session.add(usuario)
    session.flush()

    creadas = []
    for etiqueta in ("personal", "trabajo"):
        cuenta_id = uuid.uuid4()
        session.add(
            WhatsAppAccount(
                id=cuenta_id,
                user_id=usuario.id,
                session_status="linked",
                session_storage_key=f"accounts/{cuenta_id}",
            )
        )
        session.flush()
        chat = Chat(
            jid=f"{etiqueta}@lid",
            chat_type="individual",
            name=f"Chat {etiqueta}",
            whatsapp_account_id=cuenta_id,
        )
        session.add(chat)
        session.flush()
        session.add(
            Message(
                chat_id=chat.id,
                chat_jid=chat.jid,
                whatsapp_message_id=f"M-{etiqueta}",
                message_type="text",
                text="hola",
                timestamp=1,
            )
        )
        session.flush()
        creadas.append((cuenta_id, chat))

    return usuario, creadas


# ---------------------------------------------------------------------------
# El contexto: UNA cuenta
# ---------------------------------------------------------------------------


def test_cada_cuenta_ve_SOLO_sus_chats(session, dos_cuentas):
    """El fallo que este cambio cierra."""
    _usuario, creadas = dos_cuentas
    (id_personal, chat_personal), (id_trabajo, chat_trabajo) = creadas

    solo_personal = repo.list_chat_summaries(session, accounts=[id_personal])
    solo_trabajo = repo.list_chat_summaries(session, accounts=[id_trabajo])

    assert {c.jid for c in solo_personal} == {chat_personal.jid}
    assert {c.jid for c in solo_trabajo} == {chat_trabajo.jid}


def test_acotar_por_USUARIO_es_lo_que_los_mezclaba(session, dos_cuentas):
    """Se deja fijado el comportamiento que NO queremos en los listados.

    `cuentas_de` sigue existiendo y sigue devolviendo las dos: es la lista de
    autorizacion. Lo que no puede volver es que un LISTADO la use.
    """
    usuario, _creadas = dos_cuentas

    todas = ownership.cuentas_de(session, usuario.id)
    assert len(todas) == 2

    mezclados = repo.list_chat_summaries(session, accounts=todas)
    assert len(mezclados) == 2, (
        "acotar por usuario junta los dos WhatsApp; por eso los listados "
        "usan cuenta_visible_de y no cuentas_de"
    )


def test_sin_cuenta_pedida_se_elige_SIEMPRE_la_misma(session, dos_cuentas):
    """Sin `ORDER BY`, "la primera" cambiaba entre dos peticiones.

    PostgreSQL puede devolver las filas en cualquier orden. El usuario habria
    visto un WhatsApp u otro al recargar, sin tocar nada.
    """
    usuario, _creadas = dos_cuentas

    elegidas = {
        str(ownership.cuenta_visible_de(session, usuario.id)) for _ in range(5)
    }
    assert len(elegidas) == 1


# ---------------------------------------------------------------------------
# Lo que pide el navegador se COMPRUEBA
# ---------------------------------------------------------------------------


def test_se_puede_pedir_otra_cuenta_PROPIA(session, dos_cuentas):
    usuario, creadas = dos_cuentas
    (_id_personal, _), (id_trabajo, _) = creadas

    elegida = ownership.cuenta_visible_de(session, usuario.id, str(id_trabajo))
    assert str(elegida) == str(id_trabajo)


def test_pedir_una_cuenta_AJENA_no_la_entrega(session, dos_cuentas, cuenta):
    """Cambiar un parametro de la URL no puede leer la copia de otro.

    Se cae en la propia en vez de responder 403: un 403 confirmaria que esa
    cuenta existe, y con eso se puede ir tanteando identificadores.
    """
    usuario, creadas = dos_cuentas
    mias = {str(c) for c, _ in creadas}

    elegida = ownership.cuenta_visible_de(session, usuario.id, str(cuenta.id))

    assert str(elegida) != str(cuenta.id), "entrego una cuenta ajena"
    assert str(elegida) in mias


def test_un_identificador_inventado_no_rompe_nada(session, dos_cuentas):
    usuario, creadas = dos_cuentas
    elegida = ownership.cuenta_visible_de(session, usuario.id, "no-es-un-uuid")
    assert str(elegida) in {str(c) for c, _ in creadas}


def test_sin_ninguna_cuenta_no_se_inventa_una(session):
    """`None` significa "hay que vincular", nunca "toma la de al lado"."""
    from app.models import User

    usuario = User(
        email=f"sin-{uuid.uuid4().hex[:10]}@example.com", password_hash="x"
    )
    session.add(usuario)
    session.flush()

    assert ownership.cuenta_visible_de(session, usuario.id) is None


# ---------------------------------------------------------------------------
# La autorizacion NO se estrecha
# ---------------------------------------------------------------------------


def test_un_chat_de_la_otra_cuenta_SIGUE_siendo_suyo(session, dos_cuentas):
    """Estrechar esto habria roto abrir un chat tras cambiar de cuenta.

    Lo que se ve es de una cuenta; lo que se puede tocar es de todas las
    suyas. Si la autorizacion se acotara igual que el listado, pedir un chat
    justo despues de cambiar de WhatsApp daria 404 sobre algo propio.
    """
    usuario, creadas = dos_cuentas
    for _cuenta_id, chat in creadas:
        assert ownership.chat_es_de(session, chat.id, usuario.id) is True


def test_el_chat_de_OTRO_usuario_no_es_suyo(session, dos_cuentas, cuenta):
    from app.models import Chat

    usuario, _creadas = dos_cuentas
    ajeno = Chat(
        jid="ajeno@lid",
        chat_type="individual",
        whatsapp_account_id=cuenta.id,
    )
    session.add(ajeno)
    session.flush()

    assert ownership.chat_es_de(session, ajeno.id, usuario.id) is False

# ---------------------------------------------------------------------------
# El motor de historial no toca las conversaciones de la otra cuenta
# ---------------------------------------------------------------------------


def test_el_backfill_solo_coge_los_chats_de_SU_cuenta(session, dos_cuentas):
    """No se veia con una sola cuenta: "todos" y "los suyos" eran lo mismo.

    Con dos, el motor de una cuenta cogia las conversaciones de la otra y le
    pedia su historial a un telefono que no las conoce: ACK y despues nada.
    Medido en el log real: 336 candidatos para una cuenta con 131 chats.
    """
    from app.services.backfill_service import BackfillService

    _usuario, creadas = dos_cuentas
    (id_personal, chat_personal), (_id_trabajo, chat_trabajo) = creadas

    servicio = object.__new__(BackfillService)
    servicio._database = _BaseDeLaPrueba(session)
    servicio.whatsapp_account_id = id_personal
    servicio._own_jids = set()

    vistos = {jid for _chat_id, jid in servicio.chats_to_process(limit=100)}

    assert chat_personal.jid in vistos
    assert chat_trabajo.jid not in vistos, "cogio un chat de la otra cuenta"


def test_la_cuenta_de_servicio_de_whatsapp_no_se_excava(session, dos_cuentas):
    """Pedirle historial gasta una peticion y una espera para no traer nada."""
    from app.services.backfill_service import BackfillService

    servicio = object.__new__(BackfillService)
    servicio._own_jids = set()

    assert servicio.is_backfill_candidate("0@s.whatsapp.net") is False
    assert servicio.is_backfill_candidate("status@broadcast") is False
    assert servicio.is_backfill_candidate("573001234567@s.whatsapp.net") is True


class _BaseDeLaPrueba:
    """Reutiliza la sesion transaccional en vez de abrir otra."""

    def __init__(self, session):
        self._session = session

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


# ---------------------------------------------------------------------------
# LA COLISION DE IDENTIFICADORES ENTRE CUENTAS: por que NO puede ocurrir
# ---------------------------------------------------------------------------
#
# La preocupacion, planteada asi: "el mismo contacto genera el mismo chat ID en
# el WhatsApp personal y en el del trabajo, y un mensaje `AC3F...` de una cuenta
# puede descartarse como duplicado del `AC3F...` de la otra, en silencio".
#
# Seria cierto si la clave fuera el JID de WhatsApp. No lo es. El esquema usa
# una clave SUBROGADA::
#
#     chats     UNIQUE (whatsapp_account_id, jid)     -> `chats.id` es serial
#     messages  UNIQUE (chat_id, whatsapp_message_id)
#
# El mismo contacto en dos cuentas produce dos filas de `chats` con `id`
# distinto, asi que el mismo wamid cae en `chat_id` distintos y convive. La
# particion viaja por `chat_id`, y por eso no hace falta repetir
# `whatsapp_account_id` en `messages`: seria una columna mas que mantener para
# una separacion que ya esta garantizada.
#
# Estas pruebas lo ejercitan con datos, no con el esquema: si alguien cambiara
# la clave de deduplicacion al JID, aqui se veria.


def test_el_MISMO_wamid_en_dos_cuentas_no_se_descarta(session, dos_cuentas):
    """El descarte silencioso que se temia. Se comprueba que no ocurre."""
    from app.models import Message

    _usuario, creadas = dos_cuentas
    (_id_a, chat_a), (_id_b, chat_b) = creadas
    WAMID = "AC3F0000000000000000000000000000"

    for chat in (chat_a, chat_b):
        session.add(
            Message(
                chat_id=chat.id,
                chat_jid=chat.jid,
                whatsapp_message_id=WAMID,
                message_type="text",
                text=f"soy de {chat.name}",
                timestamp=1,
            )
        )
    session.flush()

    filas = (
        session.query(Message).filter(Message.whatsapp_message_id == WAMID).all()
    )
    assert len(filas) == 2, "un mensaje se perdio por colisionar con el de otra cuenta"
    assert {f.chat_id for f in filas} == {chat_a.id, chat_b.id}
    assert {f.text for f in filas} == {"soy de Chat personal", "soy de Chat trabajo"}


def test_el_MISMO_contacto_da_DOS_conversaciones(session, cuenta):
    """Mismo numero, dos cuentas: dos chats, dos identificadores."""
    import uuid as _uuid

    from app.models import User, WhatsAppAccount
    from app.services import repository as repo

    usuario = User(
        email=f"mismo-{_uuid.uuid4().hex[:8]}@example.com", password_hash="x"
    )
    session.add(usuario)
    session.flush()
    cuentas = []
    for _ in range(2):
        cid = _uuid.uuid4()
        session.add(
            WhatsAppAccount(
                id=cid,
                user_id=usuario.id,
                session_status="linked",
                session_storage_key=f"accounts/{cid}",
            )
        )
        session.flush()
        cuentas.append(cid)

    # El MISMO numero de telefono, vinculado desde las dos cuentas.
    jid = "573001234567@s.whatsapp.net"
    a = repo.upsert_chat(
        session, jid=jid, name="Mejor amigo Juan", whatsapp_account_id=cuentas[0]
    )
    b = repo.upsert_chat(
        session, jid=jid, name="Juan Perez Proveedor", whatsapp_account_id=cuentas[1]
    )
    session.flush()

    assert a != b, "el mismo contacto colapso en una sola conversacion"
    # Y cada cuenta lo llama como lo tiene guardado ELLA.
    from app.models import Chat

    assert session.get(Chat, a).name == "Mejor amigo Juan"
    assert session.get(Chat, b).name == "Juan Perez Proveedor"


# ---------------------------------------------------------------------------
# CAMBIAR DE CUENTA CAMBIA LO QUE SE PUEDE LEER
# ---------------------------------------------------------------------------
#
# EL FALLO, TAL Y COMO SE VEIA
# ----------------------------
# El usuario cambiaba de cuenta y seguia viendo la misma conversacion. La
# lista SI se vaciaba y se recargaba con los chats de la cuenta nueva, pero el
# identificador seguia en la URL (`/dashboard/925`) y el servidor lo servia
# igual: `chat_es_de` mira TODAS las cuentas del usuario.
#
# Esa comprobacion es correcta para lo que hace --contestar "es suyo"-- y no se
# toca. Lo que faltaba es la otra pregunta: "¿es de la cuenta que esta
# mirando?". Sin ella, tener dos WhatsApp separados no separaba nada en cuanto
# se conservaba un identificador.


def _cliente_de(app_cliente, cuenta_id):
    """El mismo cliente, diciendo que mira ESA cuenta."""
    app_cliente.environ_base["HTTP_X_WHATSAPP_ACCOUNT"] = str(cuenta_id)
    return app_cliente


def test_un_chat_de_OTRA_cuenta_propia_no_se_sirve(cliente, session, cuenta_del_cliente):
    """Es suyo, pero no es de la cuenta que tiene abierta.

    Se fija CUAL esta activa antes de mirar. Sin fijarlo, la prueba pasaba
    sola y fallaba dentro de la suite: lo que decide que se ve es la cuenta
    activa, y cual lo sea al llegar aqui depende de lo que haya corrido antes.
    Eso es un fallo de la prueba, no del producto -- pero una prueba que
    depende del orden no mide nada.
    """
    import uuid as _uuid

    from app.auth.memberships import activar_cuenta
    from app.models import WhatsAppAccount
    from app.services import repository as repo

    activar_cuenta(
        session,
        user_id=cuenta_del_cliente.user_id,
        account_id=cuenta_del_cliente.id,
    )
    session.flush()

    otra_id = _uuid.uuid4()
    session.add(
        WhatsAppAccount(
            id=otra_id,
            user_id=cuenta_del_cliente.user_id,
            session_status="linked",
            session_storage_key=f"accounts/{otra_id}",
        )
    )
    session.flush()
    ajeno = repo.upsert_chat(
        session,
        jid="573001234567@s.whatsapp.net",
        name="De la otra cuenta",
        whatsapp_account_id=otra_id,
    )
    session.flush()

    # Mirando la cuenta de siempre, el chat de la otra no aparece.
    detalle = cliente.get(f"/api/v1/chats/{ajeno}")
    mensajes = cliente.get(f"/api/v1/chats/{ajeno}/messages")

    assert detalle.status_code == 404
    assert mensajes.status_code == 404
    assert "De la otra cuenta" not in detalle.get_data(as_text=True)


def test_al_cambiar_de_cuenta_SI_se_sirve(cliente, session, cuenta_del_cliente):
    """La otra mitad: cambiando de cuenta, ese chat se abre con normalidad.

    Si esto se rompiera, el aislamiento habria pasado de escaso a excesivo y
    el usuario no podria leer su propia conversacion desde ninguna parte.
    """
    import uuid as _uuid

    from app.auth.memberships import activar_cuenta
    from app.models import WhatsAppAccount
    from app.services import repository as repo

    otra_id = _uuid.uuid4()
    session.add(
        WhatsAppAccount(
            id=otra_id,
            user_id=cuenta_del_cliente.user_id,
            session_status="linked",
            session_storage_key=f"accounts/{otra_id}",
        )
    )
    session.flush()
    suyo = repo.upsert_chat(
        session,
        jid="573001234567@s.whatsapp.net",
        name="De la otra cuenta",
        whatsapp_account_id=otra_id,
    )
    activar_cuenta(session, user_id=cuenta_del_cliente.user_id, account_id=otra_id)
    session.flush()

    assert cliente.get(f"/api/v1/chats/{suyo}").status_code == 200


def test_la_propiedad_NO_se_estrecha(session, dos_cuentas):
    """`chat_es_de` sigue contestando por TODAS sus cuentas.

    Son dos preguntas distintas y las dos hacen falta: lo que se puede tocar
    es de todas las suyas; lo que se ENSEÑA es de la que esta mirando.
    Estrechar la primera romperia el cambio de cuenta.
    """
    usuario, creadas = dos_cuentas
    for _cuenta_id, chat in creadas:
        assert ownership.chat_es_de(session, chat.id, usuario.id) is True
