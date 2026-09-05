"""Un usuario no puede leer los datos de otro.

Es la garantia central de esta fase. Todo lo demas —login, Google, guards— es
comodidad; esto es lo que impide que la copia de WhatsApp de una persona
acabe en la pantalla de otra.

POR QUE 404 Y NO 403
--------------------
Un 403 sobre un identificador ajeno confirma que ese identificador existe.
Iterando se averigua cuantos chats tiene otro y cuando los creo. El 404 no
dice nada, y para quien pregunta de buena fe por algo inexistente la respuesta
es la misma.
"""

from __future__ import annotations

import uuid

import pytest

from app.auth.google import SCOPE_DRIVE

CLAVE = "una contrasena larga"


def _correo() -> str:
    return f"iso-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
def dos_usuarios(runtime, session):
    """Dos cuentas completas, cada una con su cuenta de WhatsApp y su chat."""
    from app.models import Chat, GoogleCredential, WhatsAppAccount

    runtime._montar_cuentas()
    hechos = []
    for etiqueta in ("A", "B"):
        inicio = runtime.auth.register(email=_correo(), password=CLAVE)
        session.add(
            GoogleCredential(
                user_id=inicio.user_id,
                google_subject=f"sub-{etiqueta}-{uuid.uuid4().hex[:6]}",
                scope=f"openid email profile {SCOPE_DRIVE}",
                refresh_token_encrypted=b"x",
            )
        )
        cuenta = WhatsAppAccount(
            user_id=inicio.user_id,
            session_status="linked",
            session_storage_key=f"users/{inicio.user_id}",
        )
        session.add(cuenta)
        session.flush()

        chat = Chat(
            jid=f"5730000{etiqueta}00@s.whatsapp.net",
            chat_type="individual",
            name=f"Chat de {etiqueta}",
            whatsapp_account_id=cuenta.id,
        )
        session.add(chat)
        session.flush()
        hechos.append(
            {
                "token": inicio.token,
                "user_id": inicio.user_id,
                "cuenta": cuenta,
                "chat": chat,
            }
        )
    return hechos


@pytest.fixture
def app_de_pruebas(runtime):
    from app.api import create_app

    runtime._montar_cuentas()
    aplicacion = create_app(runtime)
    aplicacion.config.update(TESTING=True)
    return aplicacion


def _cliente_de(aplicacion, runtime, token: str):
    from app.auth.web import CSRF_COOKIE

    cli = aplicacion.test_client()
    cli.set_cookie(runtime.settings.session_cookie_name, token)
    cli.set_cookie(CSRF_COOKIE, "csrf")
    cli.environ_base["HTTP_X_CSRF_TOKEN"] = "csrf"
    return cli


# ---------------------------------------------------------------------------
# Chats
# ---------------------------------------------------------------------------


def test_el_listado_solo_trae_los_chats_propios(app_de_pruebas, runtime, dos_usuarios):
    a, b = dos_usuarios
    cliente_a = _cliente_de(app_de_pruebas, runtime, a["token"])

    jids = {c["jid"] for c in cliente_a.get("/api/v1/chats").get_json()["chats"]}
    assert a["chat"].jid in jids
    assert b["chat"].jid not in jids, "A esta viendo el chat de B"


def test_pedir_el_chat_de_otro_da_404(app_de_pruebas, runtime, dos_usuarios):
    a, b = dos_usuarios
    cliente_a = _cliente_de(app_de_pruebas, runtime, a["token"])

    assert cliente_a.get(f"/api/v1/chats/{b['chat'].id}").status_code == 404


def test_los_mensajes_de_un_chat_ajeno_dan_404(app_de_pruebas, runtime, dos_usuarios):
    a, b = dos_usuarios
    cliente_a = _cliente_de(app_de_pruebas, runtime, a["token"])

    assert (
        cliente_a.get(f"/api/v1/chats/{b['chat'].id}/messages").status_code == 404
    )


def test_el_chat_propio_si_se_ve(app_de_pruebas, runtime, dos_usuarios):
    """La contraparte: el aislamiento no puede bloquear lo legitimo."""
    a, _ = dos_usuarios
    cliente_a = _cliente_de(app_de_pruebas, runtime, a["token"])

    assert cliente_a.get(f"/api/v1/chats/{a['chat'].id}").status_code == 200


def test_revisar_el_historial_de_un_chat_ajeno_da_404(
    app_de_pruebas, runtime, dos_usuarios
):
    a, b = dos_usuarios
    cliente_a = _cliente_de(app_de_pruebas, runtime, a["token"])

    respuesta = cliente_a.post(f"/api/v1/chats/{b['chat'].id}/history/recheck")
    assert respuesta.status_code == 404


# ---------------------------------------------------------------------------
# Multimedia
# ---------------------------------------------------------------------------


@pytest.fixture
def adjunto_de_b(session, dos_usuarios):
    """Un adjunto que pertenece a B, descargado y con archivo."""
    from app.models import MediaFile, Message

    _, b = dos_usuarios
    mensaje = Message(
        chat_id=b["chat"].id,
        chat_jid=b["chat"].jid,
        whatsapp_message_id=f"WAMID{uuid.uuid4().hex[:16].upper()}",
        timestamp=1_760_000_000,
        from_me=False,
        message_type="image",
        source="live",
    )
    session.add(mensaje)
    session.flush()
    adjunto = MediaFile(
        message_id=mensaje.id,
        chat_id=b["chat"].id,
        media_type="image",
        download_status="downloaded",
        local_path="privado/de-b.jpg",
    )
    session.add(adjunto)
    session.flush()
    return adjunto


def test_cambiar_el_id_no_da_la_multimedia_de_otro(
    app_de_pruebas, runtime, dos_usuarios, adjunto_de_b
):
    """Sin esto, iterar ids en la URL vaciaria la galeria del vecino."""
    a, _ = dos_usuarios
    cliente_a = _cliente_de(app_de_pruebas, runtime, a["token"])

    for ruta in (
        f"/api/v1/media/{adjunto_de_b.id}",
        f"/api/v1/media/{adjunto_de_b.id}/file",
        f"/api/v1/media/{adjunto_de_b.id}/thumbnail",
    ):
        assert cliente_a.get(ruta).status_code == 404, ruta


def test_reintentar_la_multimedia_de_otro_da_404(
    app_de_pruebas, runtime, dos_usuarios, adjunto_de_b
):
    a, _ = dos_usuarios
    cliente_a = _cliente_de(app_de_pruebas, runtime, a["token"])
    respuesta = cliente_a.post(f"/api/v1/media/{adjunto_de_b.id}/retry")
    assert respuesta.status_code == 404


def test_recuperar_por_mensaje_ajeno_da_404(
    app_de_pruebas, runtime, dos_usuarios, adjunto_de_b
):
    a, _ = dos_usuarios
    cliente_a = _cliente_de(app_de_pruebas, runtime, a["token"])
    respuesta = cliente_a.post(
        f"/api/v1/messages/{adjunto_de_b.message_id}/media/recover"
    )
    assert respuesta.status_code == 404


def test_la_respuesta_de_404_no_filtra_nada(
    app_de_pruebas, runtime, dos_usuarios, adjunto_de_b
):
    """Ni ruta de disco, ni nombre de archivo, ni el chat al que pertenece."""
    a, b = dos_usuarios
    cliente_a = _cliente_de(app_de_pruebas, runtime, a["token"])

    cuerpo = str(cliente_a.get(f"/api/v1/media/{adjunto_de_b.id}").get_json())
    assert "privado/de-b.jpg" not in cuerpo
    assert b["chat"].jid not in cuerpo


# ---------------------------------------------------------------------------
# Chats sin dueno
# ---------------------------------------------------------------------------


def test_un_chat_sin_dueno_no_es_de_nadie(app_de_pruebas, runtime, dos_usuarios, session):
    """Filas anteriores a multiusuario.

    Darlas por buenas para el primero que entre seria entregarle el historial
    de la cuenta de pruebas. El reset de fase las elimina.
    """
    from app.models import Chat

    huerfano = Chat(jid="99900011122@lid", chat_type="individual", name="Sin dueno")
    session.add(huerfano)
    session.flush()

    a, _ = dos_usuarios
    cliente_a = _cliente_de(app_de_pruebas, runtime, a["token"])

    assert cliente_a.get(f"/api/v1/chats/{huerfano.id}").status_code == 404
    jids = {c["jid"] for c in cliente_a.get("/api/v1/chats").get_json()["chats"]}
    assert huerfano.jid not in jids


def test_un_usuario_sin_cuenta_de_whatsapp_no_ve_ningun_chat(
    app_de_pruebas, runtime, session, dos_usuarios
):
    from app.models import GoogleCredential

    solo = runtime.auth.register(email=_correo(), password=CLAVE)
    session.add(
        GoogleCredential(
            user_id=solo.user_id,
            google_subject=f"sub-{uuid.uuid4().hex[:8]}",
            scope=f"openid email profile {SCOPE_DRIVE}",
            refresh_token_encrypted=b"x",
        )
    )
    session.flush()

    cliente = _cliente_de(app_de_pruebas, runtime, solo.token)
    assert cliente.get("/api/v1/chats").get_json()["chats"] == []


# ---------------------------------------------------------------------------
# La consulta acotada no se puede desactivar por descuido
# ---------------------------------------------------------------------------


def test_una_lista_de_cuentas_vacia_no_deja_verlo_todo(session):
    """Un filtro que "no aplica" seria un filtro que abre la puerta."""
    from app.services import repository as repo

    assert repo.list_chat_summaries(session, accounts=[]) == []


def test_sin_cuentas_el_filtro_de_propiedad_es_imposible():
    from app.auth.ownership import filtro_de_chats

    condicion = str(filtro_de_chats([]))
    assert "IS NULL" in condicion.upper(), "con lista vacia no puede pasar nada"
