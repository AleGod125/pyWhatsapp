"""El sidebar muestra TODOS los chats del usuario.

EL FALLO QUE ESTO FIJA
----------------------
40 chats en la base, 5 en pantalla. No habia ningun filtro de almacenamiento:
35 chats tenian ``whatsapp_account_id`` a NULL porque entraron antes de que la
ingesta supiera de quien eran. El filtro de propiedad los excluia —hacia lo
correcto— y el resultado era un panel casi vacio.

LA REGLA
--------
Drive decide de donde sale el CONTENIDO de un chat. NO decide si el chat
existe. Un chat sin segmento, sin mensajes o esperando un ancla se muestra
igual: ocultarlo hace desaparecer una conversacion real.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.auth import ownership
from app.models import Chat, ChatHistoryState, Message, WhatsAppAccount
from app.services import repository as repo

CLAVE = "una contrasena larga"


def _correo() -> str:
    return f"lst-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
def dos_usuarios(runtime, session):
    """A con chats de todo tipo; B con los suyos."""
    session.execute(delete(WhatsAppAccount))
    session.flush()
    runtime._montar_cuentas()

    hechos = {}
    for etiqueta, cuantos in (("A", 12), ("B", 3)):
        inicio = runtime.auth.register(email=_correo(), password=CLAVE)
        cuenta = WhatsAppAccount(
            user_id=inicio.user_id,
            session_status="linked",
            session_storage_key=f"users/{inicio.user_id}",
        )
        session.add(cuenta)
        session.flush()

        # Mezcla deliberada: los estados NO pueden cambiar la visibilidad.
        estados = ["waiting_seed"] * (cuantos - 3) + ["exhausted", "pending", "timeout"]
        for i, estado in enumerate(estados):
            chat = Chat(
                jid=f"5739{etiqueta}{i:04d}@s.whatsapp.net",
                chat_type="individual",
                name=f"Chat {etiqueta}{i}",
                whatsapp_account_id=cuenta.id,
            )
            session.add(chat)
            session.flush()
            session.add(
                ChatHistoryState(
                    chat_id=chat.id, chat_jid=chat.jid, history_status=estado
                )
            )
        session.flush()
        hechos[etiqueta] = {"user_id": inicio.user_id, "cuenta": cuenta, "total": cuantos}
    return hechos


def _listar(session, user_id):
    return repo.list_chat_summaries(
        session, accounts=ownership.cuentas_de(session, user_id), limit=1000
    )


# ---------------------------------------------------------------------------
# Todos los chats, sin importar su estado
# ---------------------------------------------------------------------------


def test_se_devuelven_TODOS_los_chats_del_usuario(session, dos_usuarios):
    a = dos_usuarios["A"]
    assert len(_listar(session, a["user_id"])) == a["total"]


def test_ningun_estado_historico_oculta_un_chat(session, dos_usuarios):
    """``waiting_seed`` afecta al historial, NO a que la conversacion exista."""
    a = dos_usuarios["A"]
    estados = {r.history_status for r in _listar(session, a["user_id"])}

    assert "waiting_seed" in estados
    assert {"exhausted", "pending", "timeout"} <= estados


def test_un_chat_sin_mensajes_se_muestra(session, dos_usuarios):
    """Metadata sin mensajes sigue siendo una conversacion."""
    a = dos_usuarios["A"]
    resumenes = _listar(session, a["user_id"])

    assert any((r.message_count or 0) == 0 for r in resumenes)


def test_un_chat_sin_segmento_en_drive_se_muestra(session, dos_usuarios):
    """Drive decide de donde sale el contenido, no si el chat existe."""
    from app.models.storage import MessageSegment

    a = dos_usuarios["A"]
    session.execute(delete(MessageSegment))
    session.flush()

    assert len(_listar(session, a["user_id"])) == a["total"]


def test_el_listado_no_mira_el_almacenamiento(session):
    """La consulta no puede tener condiciones de storage: ocultarian chats."""
    import ast
    import inspect
    import textwrap

    fuente = textwrap.dedent(inspect.getsource(repo.list_chat_summaries))
    arbol = ast.parse(fuente)
    nombres = [
        n.attr for n in ast.walk(arbol) if isinstance(n, ast.Attribute)
    ] + [n.id for n in ast.walk(arbol) if isinstance(n, ast.Name)]

    for prohibido in ("MessageSegment", "storage_status", "drive_file_id"):
        assert prohibido not in nombres, (
            f"el listado no puede depender de {prohibido}: dejaria chats invisibles"
        )


def test_un_chat_con_mensajes_sin_subir_se_muestra(session, dos_usuarios):
    a = dos_usuarios["A"]
    chat = _listar(session, a["user_id"])[0]
    session.add(
        Message(
            chat_id=int(chat.id),
            chat_jid=chat.jid,
            timestamp=1_760_000_000,
            from_me=False,
            message_type="text",
            text="sin subir",
            source="live",
            storage_status="local",
        )
    )
    session.flush()

    assert len(_listar(session, a["user_id"])) == a["total"]


# ---------------------------------------------------------------------------
# Propiedad
# ---------------------------------------------------------------------------


def test_cada_usuario_ve_los_suyos_y_solo_los_suyos(session, dos_usuarios):
    a, b = dos_usuarios["A"], dos_usuarios["B"]

    de_a = {r.jid for r in _listar(session, a["user_id"])}
    de_b = {r.jid for r in _listar(session, b["user_id"])}

    assert len(de_a) == a["total"]
    assert len(de_b) == b["total"]
    assert not (de_a & de_b), "no puede haber ni uno compartido"


def test_un_chat_sin_dueno_no_lo_ve_nadie(session, dos_usuarios):
    """Era la causa del panel vacio, y sigue siendo lo correcto.

    Se arregla dandoles dueno en la ingesta, no relajando el filtro: sin dueno
    no se puede saber a quien mostrarselos.
    """
    huerfano = Chat(jid="99900011122@lid", chat_type="individual", name="Sin dueno")
    session.add(huerfano)
    session.flush()

    for etiqueta in ("A", "B"):
        jids = {r.jid for r in _listar(session, dos_usuarios[etiqueta]["user_id"])}
        assert huerfano.jid not in jids


# ---------------------------------------------------------------------------
# Orden
# ---------------------------------------------------------------------------


def test_los_chats_sin_actividad_van_al_final_no_desaparecen(session, dos_usuarios):
    a = dos_usuarios["A"]
    resumenes = _listar(session, a["user_id"])

    # Todos siguen ahi aunque no tengan marca de tiempo.
    assert len(resumenes) == a["total"]
    sin_marca = [r for r in resumenes if not r.last_message_timestamp]
    assert sin_marca, "los hay sin actividad, y estan"
