"""El codigo que tapa los chats restringidos.

LO QUE SE INVESTIGO ANTES DE ESCRIBIR ESTO
------------------------------------------
La pregunta del usuario era si se puede abrir un chat restringido con EL MISMO
codigo secreto que puso en WhatsApp, como hace WhatsApp Web. No se puede, y la
razon esta medida:

1. ``chatLockSettings.secretCode`` llega como BYTES OPACOS. WhatsApp no manda
   el codigo, manda un derivado.
2. Baileys ni llega a eso: ``lockChatAction`` es la unica accion de app-state
   que no procesa.
3. Y sobre todo, los mensajes de un chat restringido NO estan cifrados con
   ese codigo: llegan y se guardan como los de cualquier otro. El bloqueo de
   WhatsApp es un control de acceso de su interfaz, no una capa de cifrado.

Asi que lo que hay es un codigo LOCAL de esta aplicacion, que tapa la seccion
igual que WhatsApp Web tapa la suya. La pantalla lo dice con esas palabras.

LO QUE ESTAS PRUEBAS PROTEGEN
-----------------------------
Que el pestillo valga en el SERVIDOR. Una seccion tapada solo en el frontend
no esta tapada: basta con escribir la URL. Y como el listado se puede saltar
conociendo el identificador del chat, se comprueba tambien al abrirlo y al
pedir sus mensajes.
"""

from __future__ import annotations

import uuid

import pytest

from app.services import repository as repo

CODIGO = "135790"
OTRO = "246801"


@pytest.fixture
def chat_restringido(session, cuenta_del_cliente):
    from app.models import Message

    jid = f"57{uuid.uuid4().hex[:9]}@s.whatsapp.net"
    chat_id = repo.upsert_chat(
        session,
        jid=jid,
        name="Conversacion restringida",
        whatsapp_account_id=cuenta_del_cliente.id,
        archived=False,
        locked=True,
        pinned_at=None,
        mute_until=None,
    )
    session.add(
        Message(
            chat_id=chat_id,
            chat_jid=jid,
            whatsapp_message_id=f"M-{uuid.uuid4().hex[:10]}",
            message_type="text",
            text="un secreto",
            timestamp=1,
        )
    )
    session.flush()
    return chat_id


# ---------------------------------------------------------------------------
# Poner el codigo
# ---------------------------------------------------------------------------


def test_al_principio_no_hay_codigo(cliente):
    cuerpo = cliente.get("/api/v1/chat-lock").get_json()

    assert cuerpo["configurado"] is False
    assert cuerpo["abierto"] is False


def test_se_puede_poner_uno_de_seis_digitos(cliente):
    """Los mismos seis que usa WhatsApp. Pedirle una contrasena de cuenta
    seria ofrecerle otra cosa distinta con el mismo nombre."""
    respuesta = cliente.post("/api/v1/chat-lock", json={"codigo": CODIGO})

    assert respuesta.status_code == 200
    assert cliente.get("/api/v1/chat-lock").get_json()["configurado"] is True


def test_uno_demasiado_corto_no_vale(cliente):
    respuesta = cliente.post("/api/v1/chat-lock", json={"codigo": "12"})

    assert respuesta.status_code == 400
    assert cliente.get("/api/v1/chat-lock").get_json()["configurado"] is False


def test_ponerlo_lo_deja_abierto(cliente):
    """Acaba de demostrar que lo sabe; volver a pedirselo solo molesta."""
    cliente.post("/api/v1/chat-lock", json={"codigo": CODIGO})

    assert cliente.get("/api/v1/chat-lock").get_json()["abierto"] is True


def test_para_CAMBIARLO_hay_que_saber_el_anterior(cliente):
    """Sin esto, quien se siente delante de una sesion abierta lo sustituye
    por el suyo y el pestillo no ha servido de nada."""
    cliente.post("/api/v1/chat-lock", json={"codigo": CODIGO})

    respuesta = cliente.post(
        "/api/v1/chat-lock", json={"codigo": OTRO, "codigo_actual": "000000"}
    )

    assert respuesta.status_code == 403
    # Y el de antes sigue valiendo.
    cliente.post("/api/v1/chat-lock/cerrar")
    assert (
        cliente.post("/api/v1/chat-lock/abrir", json={"codigo": CODIGO}).status_code
        == 200
    )


def test_con_el_anterior_si_se_cambia(cliente):
    cliente.post("/api/v1/chat-lock", json={"codigo": CODIGO})

    respuesta = cliente.post(
        "/api/v1/chat-lock", json={"codigo": OTRO, "codigo_actual": CODIGO}
    )

    assert respuesta.status_code == 200
    cliente.post("/api/v1/chat-lock/cerrar")
    assert (
        cliente.post("/api/v1/chat-lock/abrir", json={"codigo": OTRO}).status_code
        == 200
    )


# ---------------------------------------------------------------------------
# Abrir y cerrar
# ---------------------------------------------------------------------------


def test_abrir_con_el_codigo_correcto(cliente):
    cliente.post("/api/v1/chat-lock", json={"codigo": CODIGO})
    cliente.post("/api/v1/chat-lock/cerrar")

    respuesta = cliente.post("/api/v1/chat-lock/abrir", json={"codigo": CODIGO})

    assert respuesta.status_code == 200
    assert cliente.get("/api/v1/chat-lock").get_json()["abierto"] is True


def test_abrir_con_uno_equivocado_no_abre(cliente):
    cliente.post("/api/v1/chat-lock", json={"codigo": CODIGO})
    cliente.post("/api/v1/chat-lock/cerrar")

    respuesta = cliente.post("/api/v1/chat-lock/abrir", json={"codigo": "000000"})

    assert respuesta.status_code == 403
    assert cliente.get("/api/v1/chat-lock").get_json()["abierto"] is False


def test_el_error_no_dice_nada_de_mas(cliente):
    """Cualquier detalle --por poco fallo, cuantos intentos quedan-- es pista."""
    cliente.post("/api/v1/chat-lock", json={"codigo": CODIGO})
    cliente.post("/api/v1/chat-lock/cerrar")

    cuerpo = cliente.post(
        "/api/v1/chat-lock/abrir", json={"codigo": "000000"}
    ).get_json()

    assert cuerpo == {"error": "codigo incorrecto"}


def test_abrir_sin_haber_puesto_ninguno(cliente):
    respuesta = cliente.post("/api/v1/chat-lock/abrir", json={"codigo": CODIGO})

    assert respuesta.status_code == 409


def test_el_hash_no_sale_nunca(cliente):
    """Quien pregunta solo necesita saber que pantalla pintar."""
    cliente.post("/api/v1/chat-lock", json={"codigo": CODIGO})

    texto = cliente.get("/api/v1/chat-lock").get_data(as_text=True)

    assert "hash" not in texto
    assert "argon" not in texto.lower()
    assert CODIGO not in texto


# ---------------------------------------------------------------------------
# El pestillo vale en el SERVIDOR
# ---------------------------------------------------------------------------


def test_un_chat_restringido_no_se_abre_por_su_identificador(cliente, chat_restringido):
    """Ocultarlo del listado no basta: el identificador se puede escribir."""
    respuesta = cliente.get(f"/api/v1/chats/{chat_restringido}")

    assert respuesta.status_code == 423


def test_sus_mensajes_tampoco(cliente, chat_restringido):
    """Es el que de verdad importa: aqui esta el contenido."""
    respuesta = cliente.get(f"/api/v1/chats/{chat_restringido}/messages")

    assert respuesta.status_code == 423
    assert "un secreto" not in respuesta.get_data(as_text=True)


def test_con_el_pestillo_abierto_si(cliente, chat_restringido):
    cliente.post("/api/v1/chat-lock", json={"codigo": CODIGO})

    detalle = cliente.get(f"/api/v1/chats/{chat_restringido}")
    mensajes = cliente.get(f"/api/v1/chats/{chat_restringido}/messages")

    assert detalle.status_code == 200
    assert mensajes.status_code == 200
    assert "un secreto" in mensajes.get_data(as_text=True)


def test_al_cerrar_se_vuelve_a_tapar(cliente, chat_restringido):
    cliente.post("/api/v1/chat-lock", json={"codigo": CODIGO})
    cliente.post("/api/v1/chat-lock/cerrar")

    assert cliente.get(f"/api/v1/chats/{chat_restringido}/messages").status_code == 423


def test_un_chat_NORMAL_no_pide_nada(cliente, session, cuenta_del_cliente):
    """El pestillo solo tapa lo restringido. Todo lo demas sigue igual."""
    jid = f"57{uuid.uuid4().hex[:9]}@s.whatsapp.net"
    chat_id = repo.upsert_chat(
        session,
        jid=jid,
        name="Conversacion normal",
        whatsapp_account_id=cuenta_del_cliente.id,
        archived=False,
        locked=False,
        pinned_at=None,
        mute_until=None,
    )
    session.flush()

    assert cliente.get(f"/api/v1/chats/{chat_id}").status_code == 200
    assert cliente.get(f"/api/v1/chats/{chat_id}/messages").status_code == 200


@pytest.fixture
def anonimo(runtime):
    """Cliente SIN cookie. Es lo que ve alguien que no ha entrado."""
    from app.api import create_app

    runtime._montar_cuentas()
    aplicacion = create_app(runtime)
    aplicacion.config.update(TESTING=True)
    return aplicacion.test_client()


def test_un_anonimo_no_toca_el_codigo(anonimo):
    """El pestillo es de un usuario, y hay que ser ese usuario."""
    assert anonimo.get("/api/v1/chat-lock").status_code == 401
    assert anonimo.post("/api/v1/chat-lock", json={"codigo": CODIGO}).status_code in (
        401,
        403,
    )
