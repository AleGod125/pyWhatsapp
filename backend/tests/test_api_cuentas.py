"""La API de cuentas de WhatsApp: listar, anadir, renombrar y cambiar.

POR QUE HACIA FALTA
-------------------
El esquema admitia varias cuentas por usuario desde hace tiempo, y el registro
de runtimes ya levantaba una sesion por cada una. Lo que no habia era forma de
DECIRLE al backend de cual se habla: ninguna ruta aceptaba un identificador de
cuenta, y los resolutores devolvian "la del usuario" con un ``.first()``.

Sin eso, anadir una segunda cuenta solo producia estados que nadie podia
resolver -- que es exactamente lo que decia el codigo que se evitaba mientras
no existiera esta interfaz.

LO QUE SE COMPRUEBA AQUI
------------------------
Sobre todo lo que NO puede pasar: que un identificador venido del navegador
entregue la cuenta de otra persona, y que dos cuentas queden activas a la vez.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def dos_mias(cliente, session, cuenta_del_cliente):
    """La cuenta que ya tenia el cliente, mas otra anadida por la API."""
    respuesta = cliente.post("/api/v1/accounts", json={"display_name": "Trabajo"})
    assert respuesta.status_code == 201, respuesta.get_json()
    nueva = respuesta.get_json()["account"]
    return str(cuenta_del_cliente.id), nueva["id"]


# ---------------------------------------------------------------------------
# Listar
# ---------------------------------------------------------------------------


def test_el_listado_trae_las_cuentas_del_usuario(cliente, cuenta_del_cliente):
    cuerpo = cliente.get("/api/v1/accounts").get_json()

    assert cuerpo["count"] >= 1
    ids = {c["id"] for c in cuerpo["accounts"]}
    assert str(cuenta_del_cliente.id) in ids


def test_siempre_hay_UNA_activa(cliente, cuenta_del_cliente):
    """Sin ninguna marcada, el frontend se quedaria sin contexto y sin lista."""
    cuerpo = cliente.get("/api/v1/accounts").get_json()

    assert cuerpo["active_id"] is not None
    activas = [c for c in cuerpo["accounts"] if c["active"]]
    assert len(activas) == 1


def test_el_listado_NO_expone_material_de_sesion(cliente, cuenta_del_cliente):
    """Ni la clave de almacenamiento: dice donde vive la carpeta de claves."""
    cuerpo = cliente.get("/api/v1/accounts").get_json()

    for una in cuerpo["accounts"]:
        for prohibido in ("session_storage_key", "user_id", "wa_lid"):
            assert prohibido not in una, prohibido


# ---------------------------------------------------------------------------
# Anadir
# ---------------------------------------------------------------------------


def test_anadir_una_cuenta_no_toca_la_que_ya_habia(cliente, dos_mias):
    """La primera sigue vinculada, con su sesion y su historial intactos."""
    primera, segunda = dos_mias
    assert primera != segunda

    cuerpo = cliente.get("/api/v1/accounts").get_json()
    por_id = {c["id"]: c for c in cuerpo["accounts"]}

    assert primera in por_id
    assert por_id[primera]["linked"] is True, "la primera dejo de estar vinculada"
    # La recien creada NO sale todavia: no tiene sesion ni un solo chat, asi
    # que no hay nada que elegir en ella. Aparece cuando se vincule. Ver
    # `test_una_cuenta_a_medio_crear_no_ensucia_el_selector`.
    assert segunda not in por_id


def test_la_nueva_NO_se_activa_sola(cliente, dos_mias):
    """Cambiar el contexto a una cuenta sin sesion deja la pantalla vacia.

    Se activa cuando termine de vincularse, no al crearla: mientras el usuario
    escanea tiene que poder seguir viendo lo que ya tenia.
    """
    primera, segunda = dos_mias
    cuerpo = cliente.get("/api/v1/accounts").get_json()

    assert cuerpo["active_id"] == primera


def test_anadir_dos_veces_crea_DOS(cliente, cuenta_del_cliente):
    """`asegurar_cuenta` devolveria siempre la misma; esto tiene que crear."""
    a = cliente.post("/api/v1/accounts", json={}).get_json()["account"]["id"]
    b = cliente.post("/api/v1/accounts", json={}).get_json()["account"]["id"]

    assert a != b


# ---------------------------------------------------------------------------
# Renombrar
# ---------------------------------------------------------------------------


def test_renombrar_cambia_solo_el_nombre(cliente, dos_mias):
    _primera, segunda = dos_mias

    respuesta = cliente.patch(
        f"/api/v1/accounts/{segunda}", json={"display_name": "WhatsApp de Pedro"}
    )

    assert respuesta.status_code == 200
    assert respuesta.get_json()["account"]["display_name"] == "WhatsApp de Pedro"


def test_un_nombre_vacio_devuelve_la_cuenta_a_su_nombre_de_perfil(cliente, dos_mias):
    _primera, segunda = dos_mias

    respuesta = cliente.patch(
        f"/api/v1/accounts/{segunda}", json={"display_name": ""}
    )

    # Se comprueba en la respuesta del PATCH, no en el listado: una cuenta a
    # medio crear ya no sale ahi, y lo que se mide aqui es el nombre.
    assert respuesta.status_code == 200
    # Sin nombre propio y sin numero todavia, no se inventa nada.
    assert respuesta.get_json()["account"]["display_name"] is None


def test_no_se_puede_renombrar_una_cuenta_AJENA(cliente, cuenta):
    """`cuenta` es de otro usuario. Se responde 404, no 403.

    Un 403 confirmaria que ese identificador existe, y con eso se pueden ir
    tanteando las cuentas de los demas.
    """
    respuesta = cliente.patch(
        f"/api/v1/accounts/{cuenta.id}", json={"display_name": "mia"}
    )

    assert respuesta.status_code == 404


# ---------------------------------------------------------------------------
# Cambiar de cuenta
# ---------------------------------------------------------------------------


def test_activar_cambia_el_contexto(cliente, session, dos_mias):
    _primera, segunda = dos_mias

    # La cuenta tiene que ser ELEGIBLE para poder quedarse activa: una sin
    # sesion y sin chats no sale en el selector, y dejar el contexto ahi
    # dejaria al usuario mirando una lista vacia sin forma de volver.
    _con_un_chat(session, segunda)

    respuesta = cliente.post(f"/api/v1/accounts/{segunda}/activate")

    assert respuesta.status_code == 200
    cuerpo = cliente.get("/api/v1/accounts").get_json()
    assert cuerpo["active_id"] == segunda
    activas = [c for c in cuerpo["accounts"] if c["active"]]
    assert len(activas) == 1, "quedaron dos activas"


def test_activar_una_cuenta_AJENA_da_404(cliente, cuenta):
    respuesta = cliente.post(f"/api/v1/accounts/{cuenta.id}/activate")
    assert respuesta.status_code == 404


def test_un_identificador_inventado_da_404(cliente, cuenta_del_cliente):
    import uuid

    respuesta = cliente.post(f"/api/v1/accounts/{uuid.uuid4()}/activate")
    assert respuesta.status_code == 404


# ---------------------------------------------------------------------------
# Y lo que se ve despues de cambiar
# ---------------------------------------------------------------------------


def test_al_cambiar_de_cuenta_los_chats_cambian(cliente, session, dos_mias):
    """El punto entero de esto: no mezclar.

    Se crea un chat en cada cuenta y se comprueba que el listado ensena UNO,
    el de la cuenta activa -- nunca los dos.
    """
    from app.models import Chat, Message

    primera, segunda = dos_mias
    for jid, account_id in (("uno@lid", primera), ("dos@lid", segunda)):
        chat = Chat(
            jid=jid, chat_type="individual", whatsapp_account_id=account_id
        )
        session.add(chat)
        session.flush()
        session.add(
            Message(
                chat_id=chat.id,
                chat_jid=jid,
                whatsapp_message_id=f"M-{jid}",
                message_type="text",
                text="hola",
                timestamp=1,
            )
        )
    session.flush()

    vistos = {c["jid"] for c in cliente.get("/api/v1/chats").get_json()["chats"]}
    assert vistos == {"uno@lid"}, "se colo un chat de la otra cuenta"

    cliente.post(f"/api/v1/accounts/{segunda}/activate")
    vistos = {c["jid"] for c in cliente.get("/api/v1/chats").get_json()["chats"]}
    assert vistos == {"dos@lid"}


def test_se_puede_pedir_otra_cuenta_por_parametro(cliente, session, dos_mias):
    """`?account_id=` para que una pestaña pueda mirar otra sin cambiar la activa."""
    from app.models import Chat, Message

    _primera, segunda = dos_mias
    chat = Chat(jid="dos@lid", chat_type="individual", whatsapp_account_id=segunda)
    session.add(chat)
    session.flush()
    session.add(
        Message(
            chat_id=chat.id,
            chat_jid="dos@lid",
            whatsapp_message_id="M-dos",
            message_type="text",
            text="hola",
            timestamp=1,
        )
    )
    session.flush()

    cuerpo = cliente.get(f"/api/v1/chats?account_id={segunda}").get_json()
    assert {c["jid"] for c in cuerpo["chats"]} == {"dos@lid"}

    # Y la activa NO ha cambiado por mirar.
    assert cliente.get("/api/v1/accounts").get_json()["active_id"] != segunda


def test_pedir_la_cuenta_de_OTRO_no_entrega_sus_chats(cliente, session, cuenta):
    """La comprobacion que impide leer la copia ajena cambiando la URL."""
    from app.models import Chat, Message

    chat = Chat(
        jid="ajeno@lid", chat_type="individual", whatsapp_account_id=cuenta.id
    )
    session.add(chat)
    session.flush()
    session.add(
        Message(
            chat_id=chat.id,
            chat_jid="ajeno@lid",
            whatsapp_message_id="M-ajeno",
            message_type="text",
            text="secreto",
            timestamp=1,
        )
    )
    session.flush()

    cuerpo = cliente.get(f"/api/v1/chats?account_id={cuenta.id}").get_json()
    assert "ajeno@lid" not in {c["jid"] for c in cuerpo["chats"]}


# ---------------------------------------------------------------------------
# EL SELECTOR SOLO ENSEÑA LO QUE SE PUEDE ELEGIR
# ---------------------------------------------------------------------------
#
# LO QUE SE VEIA, EN UNA INSTALACION REAL
# ---------------------------------------
# Cuatro cuentas y tres inservibles::
#
#     9d6a0cfb  revoked        0 chats   <- y era la ACTIVA
#     69900813  linked       327 chats
#     e11fbe69  revoked        3 chats
#     b864a6ce  never_linked   0 chats
#
# Tres decian "hay que vincular" y ninguna llevaba a ninguna parte. Peor: la
# activa era una revocada y vacia, asi que el panel pedia los chats de una
# cuenta sin nada y la lista salia en blanco con 327 conversaciones al lado.
#
# LA REGLA NO ES "VINCULADA O FUERA"
# ----------------------------------
# `e11fbe69` esta revocada y tiene tres conversaciones guardadas. Desvincular
# un telefono no borra lo que ya se copio, y esconderla dejaria al usuario sin
# ninguna forma de leer su propio historial. Lo que sobra es lo que no tiene
# NADA detras.


def _con_un_chat(session, cuenta_id):
    """Le da a esa cuenta una conversacion, para que sea elegible."""
    import uuid as _uuid

    from app.services import repository as repo

    chat_id = repo.upsert_chat(
        session,
        jid=f"57{_uuid.uuid4().hex[:9]}@s.whatsapp.net",
        whatsapp_account_id=_uuid.UUID(str(cuenta_id)),
    )
    session.flush()
    return chat_id


def _ids_del_selector(cliente):
    return {c["id"] for c in cliente.get("/api/v1/accounts").get_json()["accounts"]}


def _marcar(session, cuenta_id, estado):
    from sqlalchemy import update

    from app.models import WhatsAppAccount

    session.execute(
        update(WhatsAppAccount)
        .where(WhatsAppAccount.id == cuenta_id)
        .values(session_status=estado)
    )
    session.flush()


def test_una_cuenta_a_medio_crear_no_ensucia_el_selector(cliente, dos_mias):
    """Se creo para vincular y nunca se escaneo: no es una opcion, es ruido."""
    _primera, segunda = dos_mias

    assert segunda not in _ids_del_selector(cliente)


def test_una_REVOCADA_CON_historial_si_aparece(cliente, session, dos_mias):
    """Su telefono ya no esta, pero sus conversaciones si. Es lo que se lee."""
    _primera, segunda = dos_mias
    _con_un_chat(session, segunda)
    _marcar(session, segunda, "revoked")

    assert segunda in _ids_del_selector(cliente), (
        "se escondio un historial que el usuario todavia puede leer"
    )


def test_una_REVOCADA_y_VACIA_no_aparece(cliente, session, dos_mias):
    _primera, segunda = dos_mias
    _marcar(session, segunda, "revoked")

    assert segunda not in _ids_del_selector(cliente)


def test_la_activa_SIEMPRE_es_una_de_las_que_se_ven(cliente, session, dos_mias):
    """El caso de la instalacion real, y el que dejaba sin salida.

    Con el contexto en una cuenta que no aparece, el selector no puede marcar
    ninguna y el panel pide los chats de una cuenta vacia. El usuario ve una
    lista en blanco y no tiene con que cambiar.
    """
    primera, segunda = dos_mias
    _con_un_chat(session, segunda)
    cliente.post(f"/api/v1/accounts/{segunda}/activate")
    # Y ahora esa deja de poder ensenarse.
    _marcar(session, segunda, "revoked")
    from app.models import Chat

    session.query(Chat).filter(Chat.whatsapp_account_id == segunda).delete()
    session.flush()

    cuerpo = cliente.get("/api/v1/accounts").get_json()

    ids = {c["id"] for c in cuerpo["accounts"]}
    assert cuerpo["active_id"] in ids, "el contexto quedo en una cuenta invisible"
    assert cuerpo["active_id"] == primera
