"""Las tres secciones del panel: normal, archivados y restringidos.

DE DONDE SALE LA FORMA
----------------------
De WhatsApp Web, que es lo que el usuario ya sabe usar:

* la lista que se ve al abrir NO incluye ni archivados ni restringidos --el
  sentido de las dos cosas es justamente no estar ahi;
* "Archivados" es una entrada aparte con su contador;
* "Chats bloqueados" es otra, mas cerrada todavia.

LO QUE NO PUEDE PASAR
---------------------
Que un chat restringido se cuele en la lista normal. Ni por un parametro mal
escrito en la URL, ni porque llegue un mensaje, ni por estar tambien
archivado. Es la unica de las tres secciones donde el fallo tiene consecuencia
para el usuario: alguien mirando la pantalla veria una conversacion que
deliberadamente se habia escondido.
"""

from __future__ import annotations

import uuid

import pytest

from app.models import Chat
from app.services import repository as repo


@pytest.fixture
def tres_chats(session, cuenta):
    """Uno normal, uno archivado y uno restringido. Todos con un mensaje."""
    from app.models import Message

    creados = {}
    estados = {
        "normal": {"archived": False, "locked": False},
        "archivado": {"archived": True, "locked": False},
        "restringido": {"archived": False, "locked": True},
        # El caso que decide a que seccion pertenece: las dos cosas a la vez.
        "ambos": {"archived": True, "locked": True},
    }
    for etiqueta, estado in estados.items():
        jid = f"57{uuid.uuid4().hex[:9]}@s.whatsapp.net"
        chat_id = repo.upsert_chat(
            session,
            jid=jid,
            name=f"Chat {etiqueta}",
            whatsapp_account_id=cuenta.id,
            pinned_at=None,
            mute_until=None,
            **estado,
        )
        session.add(
            Message(
                chat_id=chat_id,
                chat_jid=jid,
                whatsapp_message_id=f"M-{uuid.uuid4().hex[:10]}",
                message_type="text",
                text="hola",
                timestamp=1,
            )
        )
        session.flush()
        creados[etiqueta] = (chat_id, jid)
    return creados


def _nombres(resumenes):
    return {r.display_name for r in resumenes}


# ---------------------------------------------------------------------------
# Cada seccion trae lo suyo
# ---------------------------------------------------------------------------


def test_la_lista_normal_deja_fuera_archivados_y_restringidos(session, cuenta, tres_chats):
    vistos = repo.list_chat_summaries(session, accounts=[cuenta.id])

    assert _nombres(vistos) == {"Chat normal"}


def test_los_archivados_son_su_propia_seccion(session, cuenta, tres_chats):
    vistos = repo.list_chat_summaries(
        session, accounts=[cuenta.id], vista="archivados"
    )

    assert _nombres(vistos) == {"Chat archivado"}


def test_un_chat_archivado_Y_restringido_va_a_restringidos(session, cuenta, tres_chats):
    """De las dos, manda la mas cerrada.

    Si apareciera en archivados, bastaria con abrir esa seccion --que no pide
    codigo-- para ver una conversacion restringida.
    """
    archivados = repo.list_chat_summaries(
        session, accounts=[cuenta.id], vista="archivados"
    )
    restringidos = repo.list_chat_summaries(
        session, accounts=[cuenta.id], vista="restringidos"
    )

    assert "Chat ambos" not in _nombres(archivados)
    assert _nombres(restringidos) == {"Chat restringido", "Chat ambos"}


def test_una_vista_inventada_cae_en_la_normal(session, cuenta, tres_chats):
    """Lo que llega de la URL no puede acabar ensenando lo escondido."""
    vistos = repo.list_chat_summaries(
        session, accounts=[cuenta.id], vista="../restringidos"
    )

    assert _nombres(vistos) == {"Chat normal"}


# ---------------------------------------------------------------------------
# El orden: los fijados arriba
# ---------------------------------------------------------------------------


def test_los_fijados_van_primero(session, cuenta):
    """Aunque su ultimo mensaje sea mas viejo. Es lo que significa fijar."""
    from app.models import Message

    for etiqueta, momento, fijado in (
        ("reciente", 2000, None),
        ("viejo pero fijado", 1000, 1725900000),
    ):
        jid = f"57{uuid.uuid4().hex[:9]}@s.whatsapp.net"
        chat_id = repo.upsert_chat(
            session,
            jid=jid,
            name=etiqueta,
            whatsapp_account_id=cuenta.id,
            last_message_timestamp=momento,
            archived=False,
            locked=False,
            pinned_at=fijado,
            mute_until=None,
        )
        session.add(
            Message(
                chat_id=chat_id,
                chat_jid=jid,
                whatsapp_message_id=f"M-{uuid.uuid4().hex[:10]}",
                message_type="text",
                text="hola",
                timestamp=momento,
            )
        )
        session.flush()

    vistos = repo.list_chat_summaries(session, accounts=[cuenta.id])

    assert [r.display_name for r in vistos][0] == "viejo pero fijado"


def test_entre_dos_fijados_manda_cuando_se_fijaron(session, cuenta):
    from app.models import Message

    for etiqueta, fijado in (("fijado antes", 1725900000), ("fijado despues", 1726900000)):
        jid = f"57{uuid.uuid4().hex[:9]}@s.whatsapp.net"
        chat_id = repo.upsert_chat(
            session,
            jid=jid,
            name=etiqueta,
            whatsapp_account_id=cuenta.id,
            last_message_timestamp=1000,
            archived=False,
            locked=False,
            pinned_at=fijado,
            mute_until=None,
        )
        session.add(
            Message(
                chat_id=chat_id,
                chat_jid=jid,
                whatsapp_message_id=f"M-{uuid.uuid4().hex[:10]}",
                message_type="text",
                text="hola",
                timestamp=1000,
            )
        )
        session.flush()

    vistos = repo.list_chat_summaries(session, accounts=[cuenta.id])

    assert [r.display_name for r in vistos] == ["fijado despues", "fijado antes"]


# ---------------------------------------------------------------------------
# Por HTTP
# ---------------------------------------------------------------------------


def _pedir(cliente, **params):
    consulta = "&".join(f"{k}={v}" for k, v in params.items())
    return cliente.get(f"/api/v1/chats?{consulta}" if consulta else "/api/v1/chats")


def _chats(cliente, **params):
    respuesta = _pedir(cliente, **params)
    assert respuesta.status_code == 200, respuesta.get_data(as_text=True)
    return respuesta.get_json()


def _poner_codigo(cliente, codigo="135790"):
    """Pone el codigo local. Ponerlo deja el pestillo abierto."""
    respuesta = cliente.post("/api/v1/chat-lock", json={"codigo": codigo})
    assert respuesta.status_code == 200, respuesta.get_data(as_text=True)
    return respuesta.get_json()


@pytest.fixture
def chats_del_cliente(session, cuenta_del_cliente):
    from app.models import Message

    creados = {}
    for etiqueta, estado in (
        ("normal", {"archived": False, "locked": False}),
        ("archivado", {"archived": True, "locked": False}),
        ("restringido", {"archived": False, "locked": True}),
    ):
        jid = f"57{uuid.uuid4().hex[:9]}@s.whatsapp.net"
        chat_id = repo.upsert_chat(
            session,
            jid=jid,
            name=f"Chat {etiqueta}",
            whatsapp_account_id=cuenta_del_cliente.id,
            pinned_at=None,
            mute_until=None,
            **estado,
        )
        session.add(
            Message(
                chat_id=chat_id,
                chat_jid=jid,
                whatsapp_message_id=f"M-{uuid.uuid4().hex[:10]}",
                message_type="text",
                text="hola",
                timestamp=1,
            )
        )
        session.flush()
        creados[etiqueta] = chat_id
    return creados


def test_por_defecto_la_api_devuelve_la_lista_normal(cliente, chats_del_cliente):
    cuerpo = _chats(cliente)

    nombres = {c["display_name"] for c in cuerpo["chats"]}
    assert nombres == {"Chat normal"}
    assert cuerpo["vista"] == "normal"


def test_la_api_sabe_cuantos_hay_en_las_otras_secciones(cliente, chats_del_cliente):
    """Para pintar "Archivados 12" sin pedir la seccion entera solo por contar."""
    cuerpo = _chats(cliente)

    assert cuerpo["secciones"] == {"archivados": 1, "restringidos": 1}


def test_los_archivados_no_piden_codigo(cliente, chats_del_cliente):
    """Archivar es ordenar, no esconder. WhatsApp tampoco lo pide."""
    archivados = _chats(cliente, vista="archivados")

    assert {c["display_name"] for c in archivados["chats"]} == {"Chat archivado"}


def test_los_restringidos_estan_CERRADOS_por_defecto(cliente, chats_del_cliente):
    """Taparlo solo en la pantalla no es taparlo: la URL se escribe a mano."""
    respuesta = _pedir(cliente, vista="restringidos")

    assert respuesta.status_code == 423
    assert "Chat restringido" not in respuesta.get_data(as_text=True)


def test_con_el_codigo_puesto_se_abren(cliente, chats_del_cliente):
    _poner_codigo(cliente)

    restringidos = _chats(cliente, vista="restringidos")

    assert {c["display_name"] for c in restringidos["chats"]} == {"Chat restringido"}


def test_al_cerrar_vuelven_a_taparse(cliente, chats_del_cliente):
    _poner_codigo(cliente)
    assert cliente.post("/api/v1/chat-lock/cerrar").status_code == 200

    assert _pedir(cliente, vista="restringidos").status_code == 423


def test_una_vista_inventada_en_la_URL_no_destapa_nada(cliente, chats_del_cliente):
    cuerpo = _chats(cliente, vista="restringidos%00")

    assert {c["display_name"] for c in cuerpo["chats"]} == {"Chat normal"}
    assert cuerpo["vista"] == "normal"


def test_la_fila_dice_su_estado(cliente, chats_del_cliente):
    """El frontend necesita saberlo para pintar la chincheta y el altavoz."""
    fila = _chats(cliente, vista="archivados")["chats"][0]

    assert fila["archived"] is True
    assert fila["locked"] is False
    assert fila["pinned"] is False
    assert fila["muted"] is False
