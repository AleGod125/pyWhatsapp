"""Anclas de historial: de donde salen y cuando despiertan un chat.

LA REGLA QUE NO SE ROMPE
------------------------
Nunca se fabrica un identificador ni una marca de tiempo. Un ancla inventada
recibe confirmacion del servidor y despues silencio, y eso es lo mas caro de
diagnosticar que tiene este proyecto. Casi todas estas pruebas comprueban
RECHAZOS.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from app.history.seed_collector import (
    RecentSeedCollector,
    SeedCandidate,
    desde_mensaje_vivo,
    fuente_de_sync,
    validar,
)
from app.models import Chat, ChatHistoryState, HistorySeed, WhatsAppAccount

CLAVE = "una contrasena larga"
ID_REAL = "3A1F8BDD4678EB6DE395"


def _correo() -> str:
    return f"pe-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
def escenario(runtime, session):
    """Un chat esperando ancla, con dueno."""
    session.execute(delete(WhatsAppAccount))
    session.flush()
    runtime._montar_cuentas()

    inicio = runtime.auth.register(email=_correo(), password=CLAVE)
    cuenta = WhatsAppAccount(
        user_id=inicio.user_id,
        session_status="linked",
        session_storage_key=f"users/{inicio.user_id}",
    )
    session.add(cuenta)
    session.flush()

    chat = Chat(
        jid=f"5735{uuid.uuid4().hex[:8]}@s.whatsapp.net",
        chat_type="individual",
        whatsapp_account_id=cuenta.id,
    )
    session.add(chat)
    session.flush()
    session.add(
        ChatHistoryState(
            chat_id=chat.id, chat_jid=chat.jid, history_status="waiting_seed"
        )
    )
    session.flush()

    class _Cola:
        def __init__(self):
            self.encolados = []

        def enqueue(self, jids):
            self.encolados.extend(jids)

    cola = _Cola()
    colector = RecentSeedCollector(
        runtime.database,
        user_id=inicio.user_id,
        account_id=cuenta.id,
        seed_queue=cola,
    )
    return {
        "colector": colector,
        "chat": chat,
        "cola": cola,
        "user_id": inicio.user_id,
        "cuenta": cuenta,
    }


def _candidato(chat, **cambios):
    base = dict(
        chat_jid=chat.jid,
        wa_msg_id=ID_REAL,
        timestamp=1_760_000_000,
        from_me=False,
        source="live",
        message_type="text",
    )
    base.update(cambios)
    return SeedCandidate(**base)


# ---------------------------------------------------------------------------
# Validacion: casi todo se rechaza
# ---------------------------------------------------------------------------


def test_un_ancla_real_se_acepta(escenario):
    assert validar(_candidato(escenario["chat"])) is None


@pytest.mark.parametrize(
    "cambio,motivo",
    [
        ({"wa_msg_id": None}, "sin identificador"),
        ({"wa_msg_id": ""}, "sin identificador"),
        ({"wa_msg_id": "opaque-1"}, "forma inesperada"),
        ({"wa_msg_id": "1760000000"}, "forma inesperada"),
        ({"timestamp": 0}, "sin marca"),
        ({"timestamp": None}, "sin marca"),
        ({"timestamp": 1_760_000_000_000}, "milisegundos"),
        ({"chat_jid": ""}, "sin chat"),
        ({"chat_jid": "x@broadcast"}, "no es una conversacion"),
        ({"chat_jid": "x@newsletter"}, "no es una conversacion"),
        ({"message_type": "protocol"}, "sin valor de ancla"),
    ],
)
def test_lo_que_no_sirve_se_rechaza(escenario, cambio, motivo):
    resultado = validar(_candidato(escenario["chat"], **cambio))
    assert resultado is not None and motivo in resultado


def test_una_marca_en_milisegundos_NO_se_convierte(escenario):
    """Dividir por mil es adivinar la unidad.

    Equivocarse produce un cursor que el servidor confirma y nunca responde.
    """
    motivo = validar(_candidato(escenario["chat"], timestamp=1_760_000_000_000))
    assert "milisegundos" in (motivo or "")


@pytest.mark.parametrize("tipo", ["image", "video", "audio", "document", "sticker"])
def test_un_mensaje_con_contenido_SI_sirve_de_ancla(escenario, tipo):
    """Lo que descarta un ancla es ser senalizacion, no llevar multimedia."""
    assert validar(_candidato(escenario["chat"], message_type=tipo)) is None


# ---------------------------------------------------------------------------
# Despertar
# ---------------------------------------------------------------------------


def test_un_ancla_valida_despierta_el_chat(escenario, session):
    resultado = escenario["colector"].observe(_candidato(escenario["chat"]))
    session.flush()

    assert resultado.aceptada and resultado.desperto
    estado = session.execute(
        select(ChatHistoryState).where(
            ChatHistoryState.chat_id == escenario["chat"].id
        )
    ).scalar_one()
    assert estado.history_status == "pending"
    assert estado.oldest_message_id == ID_REAL
    assert estado.oldest_message_timestamp == 1_760_000_000


def test_despertar_encola_en_el_motor_de_siempre(escenario, session):
    """No hay un segundo extractor: se le entrega trabajo al que ya funciona."""
    escenario["colector"].observe(_candidato(escenario["chat"]))
    assert escenario["cola"].encolados == [escenario["chat"].jid]


def test_no_hace_falta_pulsar_nada(escenario):
    """La promocion ocurre al observar, sin frontend ni reinicio."""
    import inspect

    fuente = inspect.getsource(RecentSeedCollector.observe)
    assert "promote_waiting_chat" in fuente


def test_un_ancla_rechazada_no_despierta_nada(escenario, session):
    escenario["colector"].observe(
        _candidato(escenario["chat"], wa_msg_id="inventado")
    )
    session.flush()

    estado = session.execute(
        select(ChatHistoryState).where(
            ChatHistoryState.chat_id == escenario["chat"].id
        )
    ).scalar_one()
    assert estado.history_status == "waiting_seed"


def test_un_chat_que_ya_excava_no_se_toca(escenario, session):
    from sqlalchemy import update

    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.chat_id == escenario["chat"].id)
        .values(history_status="exhausted")
    )
    session.flush()

    resultado = escenario["colector"].observe(_candidato(escenario["chat"]))
    assert resultado.aceptada and not resultado.desperto


# ---------------------------------------------------------------------------
# Duplicados y eleccion del cursor
# ---------------------------------------------------------------------------


def test_la_misma_ancla_por_dos_caminos_es_una_sola(escenario, session):
    """Llega en vivo y tambien en un blob: es la misma."""
    from sqlalchemy import func

    escenario["colector"].observe(_candidato(escenario["chat"], source="live"))
    escenario["colector"].observe(
        _candidato(escenario["chat"], source="initial_bootstrap")
    )
    session.flush()

    total = session.execute(
        select(func.count())
        .select_from(HistorySeed)
        .where(HistorySeed.chat_id == escenario["chat"].id)
    ).scalar()
    assert total == 1
    assert escenario["colector"].metricas.duplicadas == 1


def test_se_elige_el_ancla_mas_ANTIGUA(escenario, session):
    """Se excava hacia atras.

    Empezar por la mas antigua alcanza lo que queda antes de ella; partir de
    la mas reciente obligaria a recorrer otra vez lo que ya se tiene.
    """
    for i, ts in enumerate((1_760_000_300, 1_760_000_100, 1_760_000_200)):
        escenario["colector"].observe(
            _candidato(
                escenario["chat"],
                wa_msg_id=f"3A1F8BDD4678EB6DE{i:03d}",
                timestamp=ts,
            )
        )
    session.flush()

    elegida = RecentSeedCollector.oldest_valid_cursor(session, escenario["chat"].id)
    assert elegida.timestamp == 1_760_000_100


# ---------------------------------------------------------------------------
# Alias y grupos
# ---------------------------------------------------------------------------


def test_un_ancla_por_LID_encuentra_el_chat_existente(escenario, session):
    """Telefono y LID son el mismo contacto: no puede crearse otro chat."""
    from app.models import Contact

    lid = "99988877766@lid"
    session.add(Contact(jid=escenario["chat"].jid, lid=lid))
    session.flush()

    resultado = escenario["colector"].observe(
        _candidato(escenario["chat"], chat_jid=lid)
    )
    assert resultado.chat_id == escenario["chat"].id


def test_un_ancla_de_un_chat_desconocido_se_rechaza(escenario):
    """No se inventa la conversacion a la que pertenece."""
    resultado = escenario["colector"].observe(
        _candidato(escenario["chat"], chat_jid="00000000000@s.whatsapp.net")
    )
    assert not resultado.aceptada


def test_el_ancla_de_un_grupo_es_del_grupo(escenario, session):
    """El participante no es una conversacion."""
    grupo = Chat(
        jid="120363000000000000@g.us",
        chat_type="group",
        whatsapp_account_id=escenario["cuenta"].id,
    )
    session.add(grupo)
    session.flush()

    resultado = escenario["colector"].observe(
        _candidato(escenario["chat"], chat_jid=grupo.jid)
    )
    assert resultado.chat_id == grupo.id


# ---------------------------------------------------------------------------
# Sin dueno
# ---------------------------------------------------------------------------


def test_sin_dueno_no_se_anota_nada(runtime):
    colector = RecentSeedCollector(runtime.database)
    resultado = colector.observe(
        SeedCandidate("x@s.whatsapp.net", ID_REAL, 1_760_000_000)
    )
    assert not resultado.aceptada and "dueno" in resultado.motivo


# ---------------------------------------------------------------------------
# Fuentes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tipo,esperado",
    [
        ("INITIAL_BOOTSTRAP", "initial_bootstrap"),
        ("RECENT", "recent_history"),
        ("FULL", "full_history"),
        ("ON_DEMAND", "on_demand"),
    ],
)
def test_cada_tipo_de_sync_tiene_su_fuente(tipo, esperado):
    assert fuente_de_sync(tipo) == esperado


def test_un_mensaje_vivo_se_traduce_a_candidato():
    import types

    mensaje = types.SimpleNamespace(
        id=ID_REAL,
        chat_jid="5730@s.whatsapp.net",
        timestamp=1_760_000_000,
        from_me=False,
        message_type="text",
    )
    candidato = desde_mensaje_vivo(mensaje)
    assert candidato.wa_msg_id == ID_REAL and candidato.source == "live"


def test_las_metricas_dicen_de_donde_vino_cada_ancla(escenario):
    """Sin esto, "hemos anadido fuentes" no lo comprueba nadie."""
    escenario["colector"].observe(_candidato(escenario["chat"], source="offline"))
    assert escenario["colector"].metricas.por_fuente.get("offline") == 1
