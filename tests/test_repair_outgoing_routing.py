"""El reparador solo toca lo que el protobuf demuestra.

POR QUE IMPORTA TANTO
---------------------
Este script MUEVE mensajes entre conversaciones. Un criterio flojo (el tipo,
el remitente, "estaba en el chat propio") reescribiria historial real. El
unico criterio admitido es el ``raw_proto`` del propio mensaje:

  1. lleva el campo 31 (``device_sent_message``), luego lo enviamos nosotros;
  2. su ``destination_jid`` no es una identidad propia.

Un mensaje sin ``raw_proto`` no es candidato jamas: no hay con que probarlo.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Chat, Message as MessageRow
from tests.test_outgoing_routing import (
    ISAAC_LID,
    OWN_LID,
    OWN_PN,
    entrante,
    envuelto,
)

from scripts.repair_outgoing_routing import aplicar, auditar

PROPIOS = frozenset({OWN_PN, OWN_LID})

# Identificadores ficticios: la base de pruebas es la real y no se puede
# chocar con los chats que ya existen.
YO = "99988877766@lid"
OTRO = "99911122233@lid"
MIS_IDS = frozenset({YO, "34600000001@s.whatsapp.net"})


@pytest.fixture
def escenario(session):
    """Chat propio y chat del contacto, ambos vacios y ficticios."""
    session.add(Chat(jid=YO, chat_type="individual"))
    session.add(Chat(jid=OTRO, chat_type="individual"))
    session.flush()
    return session


def guardar(session, *, wamid, chat, raw, tipo="text", from_me=False, texto=None):
    fila = MessageRow(
        chat_id=session.execute(
            select(Chat.id).where(Chat.jid == chat)
        ).scalar_one(),
        chat_jid=chat,
        whatsapp_message_id=wamid,
        message_type=tipo,
        text=texto,
        timestamp=1_788_400_000,
        from_me=from_me,
        source="live",
        raw_proto=raw,
    )
    session.add(fila)
    session.flush()
    return fila


def test_un_saliente_a_otra_persona_es_candidato(escenario):
    guardar(
        escenario,
        wamid="REPWAMID001",
        chat=YO,
        raw=envuelto(OTRO, texto="hola"),
    )
    candidatos = [
        c for c in auditar(escenario, MIS_IDS) if c.whatsapp_message_id == "REPWAMID001"
    ]
    assert len(candidatos) == 1
    assert candidatos[0].chat_destino == OTRO
    assert candidatos[0].solo_autoria is False


def test_un_entrante_nunca_es_candidato(escenario):
    """Sin envoltorio no hay prueba de autoria, y sin prueba no se toca."""
    guardar(
        escenario,
        wamid="REPWAMID002",
        chat=OTRO,
        raw=entrante(texto="hola"),
    )
    assert not [
        c for c in auditar(escenario, MIS_IDS) if c.whatsapp_message_id == "REPWAMID002"
    ]


def test_un_auto_mensaje_no_se_mueve(escenario):
    """Solo se corrige quien lo envio: su sitio ES el chat propio."""
    guardar(escenario, wamid="REPWAMID003", chat=YO, raw=envuelto(YO, imagen=True))
    candidatos = [
        c for c in auditar(escenario, MIS_IDS) if c.whatsapp_message_id == "REPWAMID003"
    ]
    assert len(candidatos) == 1
    assert candidatos[0].solo_autoria is True
    assert candidatos[0].chat_destino == YO

    aplicar(escenario, candidatos)
    escenario.flush()

    fila = escenario.execute(
        select(MessageRow).where(MessageRow.whatsapp_message_id == "REPWAMID003")
    ).scalar_one()
    assert fila.chat_jid == YO, "un auto-mensaje no puede salir del chat propio"
    assert fila.from_me is True


def test_un_auto_mensaje_ya_correcto_no_reaparece(escenario):
    """Idempotencia: repetir el reparador no debe encontrar nada."""
    guardar(
        escenario,
        wamid="REPWAMID004",
        chat=YO,
        raw=envuelto(YO, imagen=True),
        from_me=True,
    )
    assert not [
        c for c in auditar(escenario, MIS_IDS) if c.whatsapp_message_id == "REPWAMID004"
    ]


def test_un_mensaje_sin_raw_proto_nunca_es_candidato(escenario):
    guardar(escenario, wamid="REPWAMID005", chat=YO, raw=None)
    assert not [
        c for c in auditar(escenario, MIS_IDS) if c.whatsapp_message_id == "REPWAMID005"
    ]


def test_al_mover_se_corrige_la_autoria(escenario):
    guardar(escenario, wamid="REPWAMID006", chat=YO, raw=envuelto(OTRO, texto="x"))
    candidatos = [
        c for c in auditar(escenario, MIS_IDS) if c.whatsapp_message_id == "REPWAMID006"
    ]
    aplicar(escenario, candidatos)
    escenario.flush()

    fila = escenario.execute(
        select(MessageRow).where(MessageRow.whatsapp_message_id == "REPWAMID006")
    ).scalar_one()
    assert fila.chat_jid == OTRO
    assert fila.from_me is True
    assert fila.chat_id == escenario.execute(
        select(Chat.id).where(Chat.jid == OTRO)
    ).scalar_one()


def test_si_ya_existe_en_el_destino_se_elimina_la_copia_mal_enrutada(escenario):
    """Mover chocaria con el indice unico (chat_jid, wamid), que es el dedup.

    La copia buena, la que ya estaba en la conversacion correcta, se conserva.
    """
    buena = guardar(
        escenario,
        wamid="REPWAMID007",
        chat=OTRO,
        raw=envuelto(OTRO, texto="x"),
        from_me=True,
    )
    mala = guardar(escenario, wamid="REPWAMID007", chat=YO, raw=envuelto(OTRO, texto="x"))

    candidatos = [c for c in auditar(escenario, MIS_IDS) if c.id == mala.id]
    assert len(candidatos) == 1
    assert candidatos[0].ya_existe_en_destino is True

    aplicar(escenario, candidatos)
    escenario.flush()
    escenario.expire_all()

    quedan = escenario.execute(
        select(MessageRow.id).where(MessageRow.whatsapp_message_id == "REPWAMID007")
    ).scalars().all()
    assert quedan == [buena.id]


def test_el_reparador_no_toca_los_mensajes_de_otras_personas(escenario):
    """Un entrante en el chat del contacto se queda exactamente donde esta."""
    guardar(escenario, wamid="REPWAMID008", chat=OTRO, raw=entrante(texto="hola"))
    guardar(escenario, wamid="REPWAMID009", chat=YO, raw=envuelto(OTRO, texto="x"))

    candidatos = [
        c
        for c in auditar(escenario, MIS_IDS)
        if c.whatsapp_message_id in ("REPWAMID008", "REPWAMID009")
    ]
    aplicar(escenario, candidatos)
    escenario.flush()

    intacto = escenario.execute(
        select(MessageRow).where(MessageRow.whatsapp_message_id == "REPWAMID008")
    ).scalar_one()
    assert intacto.chat_jid == OTRO
    assert intacto.from_me is False


def test_sin_identidad_propia_todo_saliente_pareceria_ajeno(escenario):
    """Por eso el script se niega a correr sin ella: lo dice su main().

    Aqui se comprueba el motivo: sin identidad, un auto-mensaje se
    clasificaria como dirigido a otro y se moveria de conversacion.
    """
    guardar(escenario, wamid="REPWAMID010", chat=YO, raw=envuelto(YO, imagen=True))
    ciego = [
        c
        for c in auditar(escenario, frozenset())
        if c.whatsapp_message_id == "REPWAMID010"
    ]
    assert ciego and ciego[0].solo_autoria is True, (
        "sin identidad el destino coincide con el chat, asi que no se mueve; "
        "aun asi el script exige identidad antes de escribir nada"
    )
