"""Que los chats aparezcan segun se extraen, y que tengan nombre.

DOS QUEJAS REALES, MEDIDAS EN LA APLICACION
-------------------------------------------
1. Durante la extraccion el panel se quedaba QUIETO. Para ver lo ya extraido
   habia que recargar con F5. La causa: el backend solo mandaba
   ``history.progress`` con contadores, asi que la pantalla no tenia con que
   pintar una conversacion que no conocia.

2. Grupos y contactos aparecian como "Grupo sin nombre". El asunto de un grupo
   no viaja en el app-state y en los blobs ``ON_DEMAND`` casi nunca viene. Se
   le puede preguntar al servidor --``get_group_info``, que `pywhats` expone
   desde el principio-- y no lo llamaba nadie.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager

import pytest

from app.models import Chat


class _Db:
    """La sesion de la prueba con la forma que esperan los servicios."""

    def __init__(self, session):
        self._session = session

    def transaction(self):
        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


def _grupo(session, cuenta, *, nombre=None) -> Chat:
    fila = Chat(
        jid=f"{uuid.uuid4().int % 10**17}@g.us",
        chat_type="group",
        name=nombre,
        whatsapp_account_id=cuenta.id,
    )
    session.add(fila)
    session.flush()
    return fila


# ---------------------------------------------------------------------------
# Nombres de grupo
# ---------------------------------------------------------------------------


def test_se_listan_los_grupos_SIN_nombre(session, cuenta):
    from app.services.group_names import grupos_sin_nombre

    sin = _grupo(session, cuenta)
    vacio = _grupo(session, cuenta, nombre="")
    con = _grupo(session, cuenta, nombre="Familia")

    pendientes = grupos_sin_nombre(session, cuenta.id)

    assert sin.jid in pendientes
    assert vacio.jid in pendientes, "cadena vacia es tan 'sin nombre' como NULL"
    assert con.jid not in pendientes


def test_no_se_preguntan_los_grupos_de_OTRA_cuenta(session, cuenta):
    """Pedirle al servidor el grupo de otra persona no es cosa de esta sesion."""
    from app.models import User, WhatsAppAccount
    from app.services.group_names import grupos_sin_nombre

    otro_usuario = User(email=f"o-{uuid.uuid4().hex[:8]}@x.com", password_hash="x")
    session.add(otro_usuario)
    session.flush()
    id_otra = uuid.uuid4()
    otra = WhatsAppAccount(
        id=id_otra,
        user_id=otro_usuario.id,
        session_status="linked",
        session_storage_key=f"accounts/{id_otra}",
    )
    session.add(otra)
    session.flush()

    mio = _grupo(session, cuenta)
    ajeno = _grupo(session, otra)

    pendientes = grupos_sin_nombre(session, cuenta.id)

    assert mio.jid in pendientes
    assert ajeno.jid not in pendientes


def test_el_asunto_se_guarda_como_nombre(session, cuenta):
    from app.services.group_names import resolver_nombres_de_grupo

    grupo = _grupo(session, cuenta)

    class _Cliente:
        async def get_group_info(self, jid):
            return type("G", (), {"subject": "Cumpleaños de Marta"})()

    resueltos = asyncio.run(
        resolver_nombres_de_grupo(_Cliente(), _Db(session), account_id=cuenta.id)
    )

    session.expire_all()
    assert resueltos == 1
    assert session.get(Chat, grupo.id).name == "Cumpleaños de Marta"


def test_un_nombre_que_YA_estaba_no_se_pisa(session, cuenta):
    """Si alguien ya lo nombro, ese manda: solo se rellenan huecos."""
    from app.services.group_names import _guardar

    grupo = _grupo(session, cuenta, nombre="El que ya tenia")

    cambio = _guardar(session, grupo.jid, "Otro distinto", cuenta.id)

    session.expire_all()
    assert cambio is False
    assert session.get(Chat, grupo.id).name == "El que ya tenia"


def test_si_el_servidor_no_contesta_el_chat_se_queda_como_estaba(session, cuenta):
    """Mejor "Grupo sin nombre" que un nombre inventado."""
    from app.services.group_names import resolver_nombres_de_grupo

    grupo = _grupo(session, cuenta)

    class _Mudo:
        async def get_group_info(self, jid):
            raise RuntimeError("sin respuesta")

    resueltos = asyncio.run(
        resolver_nombres_de_grupo(_Mudo(), _Db(session), account_id=cuenta.id)
    )

    session.expire_all()
    assert resueltos == 0
    assert session.get(Chat, grupo.id).name is None


def test_un_asunto_vacio_no_cuenta_como_nombre(session, cuenta):
    from app.services.group_names import resolver_nombres_de_grupo

    grupo = _grupo(session, cuenta)

    class _Vacio:
        async def get_group_info(self, jid):
            return type("G", (), {"subject": "   "})()

    asyncio.run(
        resolver_nombres_de_grupo(_Vacio(), _Db(session), account_id=cuenta.id)
    )

    session.expire_all()
    assert session.get(Chat, grupo.id).name is None


def test_el_servicio_no_conoce_la_capa_de_presentacion():
    """Publica el HECHO; la fila para la pantalla la arma el traductor."""
    import inspect

    from app.services import group_names

    fuente = inspect.getsource(group_names)
    assert "app.api" not in fuente, "un servicio no importa adaptadores"
    assert '"chat_renamed"' in fuente


# ---------------------------------------------------------------------------
# Los chats aparecen segun se extraen
# ---------------------------------------------------------------------------


def test_la_ingesta_dice_QUE_chats_son_nuevos(session, cuenta):
    """Sin esto la pantalla no sabe cual soltar y se queda quieta."""
    from app.wa.historial import FullHistorySync, HistoryConversation
    from app.services.history_service import ingest_history_sync

    jid = f"{uuid.uuid4().int % 10**12}@s.whatsapp.net"
    sync = FullHistorySync(
        sync_type="INITIAL_BOOTSTRAP",
        progress=0,
        chunk_order=0,
        conversations=[
            HistoryConversation(
                jid=jid,
                name="Marta",
                messages=[],
                last_message_timestamp=None,
                unread_count=0,
            )
        ],
        pushnames=[],
    )

    primera = ingest_history_sync(session, sync, whatsapp_account_id=cuenta.id)
    assert jid in primera.new_chat_jids, "la primera vez es nuevo"

    segunda = ingest_history_sync(session, sync, whatsapp_account_id=cuenta.id)
    assert jid not in segunda.new_chat_jids, "la segunda ya existia"


def test_el_traductor_emite_un_chat_created_por_conversacion_nueva():
    """Una conversacion, un aviso: es lo que hace que caigan una a una."""
    import inspect

    from app.api import live_events

    fuente = inspect.getsource(live_events._historial_ingerido)
    assert '"chat.created"' in fuente
    assert "new_chat_jids" in fuente


def test_las_filas_del_aviso_se_acotan_por_cuenta():
    """El mismo JID existe en tantas filas como cuentas hablen con el."""
    import inspect

    from app.api import live_events

    fuente = inspect.getsource(live_events._filas_de)
    assert "chat_id_de" in fuente
    assert "account_id=cuenta" in fuente


def test_el_orquestador_recibe_la_cuenta_no_la_adivina():
    """Un UPDATE por JID sin cuenta alcanza la conversacion de otra persona."""
    import inspect

    from app.core.orchestrator import Orchestrator

    firma = inspect.signature(Orchestrator.__init__)
    assert "whatsapp_account_id" in firma.parameters


# ---------------------------------------------------------------------------
# Lo que fallaba en la ejecucion real
# ---------------------------------------------------------------------------


def test_el_SSE_reenvia_lo_reciente_a_quien_llega_tarde():
    """Sin esto habia que recargar con F5, y era exactamente lo que pasaba.

    La extraccion entrega las conversaciones unos segundos despues de
    vincular, cuando el navegador todavia esta montando el panel. Su
    EventSource se conecta despues, y los avisos ya se habian tirado.
    """
    import inspect

    from app.api import routes

    fuente = inspect.getsource(routes.events_stream)
    assert "replay=True" in fuente


def test_el_bus_guarda_eventos_para_reenviarlos():
    """El bus ya sabia hacerlo; nadie se lo pedia."""
    from app.events import EventBus

    bus = EventBus()
    bus.publish("history_ingested", {"chat_jids": ["x@s.whatsapp.net"]})

    with bus.subscribe(replay=True) as tarde:
        assert tarde.get(timeout=0.2) is not None, "lo publicado antes se pierde"

    with bus.subscribe(replay=False) as sordo:
        assert sordo.get(timeout=0.2) is None


def test_los_nombres_NO_esperan_al_historial():
    """Tres minutos de "Contacto sin nombre" con la agenda ya disponible.

        20:58:10  Esperando el History Sync inicial (maximo=180s)
        21:01:10  Preparacion del historial resuelta en 180.3 s
        21:01:11  App-state 'critical_unblock_low': 54 mutaciones

    La agenda vive en el app-state y el historial en sus blobs: no dependen
    una de otra y no tienen por que ir en fila.
    """
    import inspect

    from app.core.orchestrator import Orchestrator

    fuente = inspect.getsource(Orchestrator.post_connect)
    posicion_nombres = fuente.find("_sync_contacts")
    posicion_espera = fuente.find("await self._await_history()")

    assert posicion_nombres != -1 and posicion_espera != -1
    assert posicion_nombres < posicion_espera, (
        "los nombres se lanzan ANTES de ponerse a esperar el historial"
    )
    assert "ensure_future" in fuente or "create_task" in fuente


def test_recablear_no_crea_una_compuerta_nueva(settings, database):
    """Dos instancias: una veia el bootstrap y la otra agotaba los 180 s."""
    from app.core.runtime import AppRuntime

    rt = AppRuntime(settings, owner="prueba", configure_logging=False)
    rt.database = database

    from app.services.history_gate import InitialHistoryGate

    primera = InitialHistoryGate(settle_seconds=1.0)
    rt.gate = primera
    primera.note_history_sync("INITIAL_BOOTSTRAP")

    # Un recableado no puede tirar lo que la compuerta ya sabe.
    import inspect

    fuente = inspect.getsource(AppRuntime._wire_services)
    assert "if self.gate is None:" in fuente

    assert rt.gate is primera
    assert rt.gate.bootstrap_seen is True
