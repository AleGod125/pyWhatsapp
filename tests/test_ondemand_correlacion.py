"""Donde se rompe la cadena peticion -> ACK -> HistorySync -> correlacion.

QUE SE MIDIO
------------
El 2026-09-03 salieron 73 peticiones ON_DEMAND y todas respondieron, con
latencias de un segundo. El 2026-09-04 salieron cuatro y ninguna respondio.
El transporte estaba sano en los dos casos: stanza enviada, ACK recibido.

La unica diferencia estructural entre unas y otras, en el log:

    2026-09-03  Peer shape=bare enc_count=1 enc_type=msg     -> respuesta
    2026-09-04  Peer shape=bare enc_count=1 enc_type=pkmsg   -> timeout

``pkmsg`` es un ``PreKeySignalMessage``: abre una sesion Signal NUEVA y lleva
dentro nuestra clave de identidad. El telefono no puede aceptar esa clave sin
la ``ADVSignedDeviceIdentity`` que demuestra que este companion es suyo, y esa
firma viaja en ``<device-identity>`` -- un nodo que la adaptacion peer estaba
quitando SIEMPRE, tambien cuando hacia falta.

Y en el mismo tramo de log se vio otra cosa: dos peticiones ON_DEMAND en vuelo
a la vez, con dos segundos de diferencia, porque la cola de despertados entra
por un camino que no pasa por la bandera de ocupado.

Estas pruebas fijan las dos correcciones y el resto de la correlacion.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.backfill_service import BackfillService, CAPABILITY_KEY
from tests.test_backfill_accounting import _DatabaseFalsa


# ---------------------------------------------------------------------------
# La stanza peer
# ---------------------------------------------------------------------------


def _reestructurar(enc_type: str, con_identidad: bool = True):
    """Una stanza peer REAL, y devuelve ``(nodo, sender)``.

    Se usa el constructor de pywhats con la adaptacion ya instalada, no una
    copia: una prueba que reimplemente la logica seguiria pasando el dia que
    la produccion cambiara, que es justo lo que no se quiere de esta.
    """
    from pywhats.binary import Node
    from pywhats.messaging.sender import Sender

    import app.compat.peer_message as peer

    peer.apply()  # idempotente

    enc = Node(tag="enc", attrs={"v": "2", "type": enc_type}, content=b"cifrado")
    participante = Node(tag="to", attrs={"jid": "destino"}, content=[enc])

    sender = SimpleNamespace(
        _adv_signed_device_identity=b"adv-firmada" if con_identidad else None,
        _config=SimpleNamespace(message_type="text", enc_version="2"),
    )
    setattr(sender, peer._FLAG, True)

    nodo = Sender._build_message_node(
        sender, message_id="AB12", to="yo@s.whatsapp.net", participants=[participante]
    )
    return nodo, sender


def _tags(nodo) -> list[str]:
    return [hijo.tag for hijo in nodo.content]


def test_un_msg_sale_desnudo_como_siempre():
    """La forma que funciono 73 veces no se toca."""
    nodo, _ = _reestructurar("msg")
    assert _tags(nodo) == ["enc"]
    assert nodo.attrs["category"] == "peer"
    assert nodo.attrs["type"] == "text"


def test_un_pkmsg_conserva_la_device_identity():
    """Sin la firma, el telefono no puede validar una sesion nueva."""
    nodo, _ = _reestructurar("pkmsg")
    assert _tags(nodo) == ["enc", "device-identity"]


def test_nunca_quedan_participants_ni_en_uno_ni_en_otro():
    """Una operacion peer va a UN dispositivo: no hay fanout que anunciar."""
    for tipo in ("msg", "pkmsg"):
        nodo, _ = _reestructurar(tipo)
        assert "participants" not in _tags(nodo)


def test_un_pkmsg_sin_firma_disponible_no_revienta():
    """Se emite igual y queda avisado; romper aqui seria peor."""
    nodo, _ = _reestructurar("pkmsg", con_identidad=False)
    assert _tags(nodo) == ["enc"]


def test_un_envio_normal_no_se_toca():
    """El parche solo actua dentro de ``peer_mode``."""
    from pywhats.binary import Node
    from pywhats.messaging.sender import Sender

    import app.compat.peer_message as peer

    peer.apply()
    enc = Node(tag="enc", attrs={"v": "2", "type": "pkmsg"}, content=b"c")
    sender = SimpleNamespace(
        _adv_signed_device_identity=b"adv",
        _config=SimpleNamespace(message_type="text", enc_version="2"),
    )
    nodo = Sender._build_message_node(
        sender,
        message_id="AB12",
        to="alguien@s.whatsapp.net",
        participants=[Node(tag="to", attrs={"jid": "x"}, content=[enc])],
    )
    assert "category" not in nodo.attrs
    assert _tags(nodo) == ["participants", "device-identity"]


def test_una_stanza_peer_con_varios_enc_se_rechaza():
    """Mas de un ``<enc>`` significa que el destino se abrio en fanout."""
    from pywhats.binary import Node
    from pywhats.messaging.sender import Sender

    import app.compat.peer_message as peer

    peer.apply()
    sender = SimpleNamespace(
        _adv_signed_device_identity=b"adv",
        _config=SimpleNamespace(message_type="text", enc_version="2"),
    )
    setattr(sender, peer._FLAG, True)
    participantes = [
        Node(
            tag="to",
            attrs={"jid": f"d{i}"},
            content=[Node(tag="enc", attrs={"v": "2", "type": "msg"}, content=b"c")],
        )
        for i in range(2)
    ]
    with pytest.raises(peer.PeerShapeError):
        Sender._build_message_node(
            sender, message_id="AB12", to="yo", participants=participantes
        )


def test_el_enc_type_queda_anotado_para_el_diagnostico():
    """Un timeout con ``pkmsg`` y uno con ``msg`` no significan lo mismo."""
    import app.compat.peer_message as peer

    _, sender = _reestructurar("pkmsg")
    assert peer.ultimo_enc_type(sender) == "pkmsg"
    _, sender = _reestructurar("msg")
    assert peer.ultimo_enc_type(sender) == "msg"


# ---------------------------------------------------------------------------
# Correlacion de la respuesta
# ---------------------------------------------------------------------------


def _sync(sync_type="ON_DEMAND", jid="1@lid", mensajes=3, sesion=None, fin=None):
    conversacion = SimpleNamespace(
        jid=jid, messages=[object()] * mensajes, end_of_history_type=fin
    )
    return SimpleNamespace(
        sync_type=sync_type, conversations=[conversacion], peer_session_id=sesion
    )


@pytest.fixture
def servicio(settings, session):
    return BackfillService(settings, _DatabaseFalsa(session))


def _esperar(servicio, chat_jid="1@lid", request_id=None):
    from app.services.backfill_service import _Pending

    pendiente = _Pending(chat_jid=chat_jid, request_id=request_id)
    servicio._pending[chat_jid] = pendiente
    return pendiente


def test_una_respuesta_on_demand_despierta_su_espera(servicio):
    pendiente = _esperar(servicio)
    servicio.notify_history(_sync())
    assert pendiente.event.is_set()
    assert pendiente.messages == 3
    assert pendiente.correlacion == "chat_jid"


def test_un_bootstrap_NO_despierta_una_espera_on_demand(servicio):
    """Historial que venia solo no puede pasar por respuesta a una peticion.

    Durante una espera puede entrar un ``INITIAL_BOOTSTRAP`` con el mismo
    chat. Contarlo como respuesta convierte "el telefono no contesto" en
    "el protocolo funciona".
    """
    pendiente = _esperar(servicio)
    servicio.notify_history(_sync(sync_type="INITIAL_BOOTSTRAP"))
    assert not pendiente.event.is_set()
    assert pendiente.messages == 0


def test_ni_un_recent_ni_un_full(servicio):
    for tipo in ("RECENT", "FULL", "NON_BLOCKING_DATA", "PUSH_NAME"):
        pendiente = _esperar(servicio, chat_jid=f"{tipo}@lid")
        servicio.notify_history(_sync(sync_type=tipo, jid=f"{tipo}@lid"))
        assert not pendiente.event.is_set(), tipo


def test_el_identificador_de_sesion_correlaciona_aunque_cambie_el_jid(servicio):
    """El campo 12 es exacto; el JID es lo que queda cuando no viene.

    El telefono puede devolver la conversacion con su otra direccion (LID
    donde nosotros usamos telefono). Con el identificador de la peticion la
    correlacion no depende de eso.
    """
    pendiente = _esperar(servicio, chat_jid="1@lid", request_id="STANZA-1")
    servicio.notify_history(_sync(jid="otra@s.whatsapp.net", sesion="STANZA-1"))
    assert pendiente.event.is_set()
    assert pendiente.correlacion == "session_id"


def test_un_identificador_de_otra_peticion_no_despierta_a_nadie(servicio):
    pendiente = _esperar(servicio, chat_jid="1@lid", request_id="STANZA-1")
    servicio.notify_history(_sync(jid="otra@lid", sesion="STANZA-DISTINTA"))
    assert not pendiente.event.is_set()


def test_una_respuesta_sin_nadie_esperando_se_avisa(servicio, caplog):
    """No se descarta en silencio: dice que el fallo es de correlacion."""
    import logging

    with caplog.at_level(logging.WARNING):
        servicio.notify_history(_sync(sesion="STANZA-7"))
    assert servicio.respuestas_sin_waiter == 1
    assert "sin waiter correlacionable" in caplog.text


def test_el_aviso_de_respuesta_sin_waiter_esta_limitado(servicio):
    for _ in range(20):
        servicio.notify_history(_sync())
    assert servicio.respuestas_sin_waiter == 1


def test_se_mide_la_latencia_de_la_respuesta(servicio):
    pendiente = _esperar(servicio)
    servicio.notify_history(_sync())
    assert pendiente.latency is not None
    assert pendiente.latency >= 0


def test_el_fin_de_historial_llega_al_que_espera(servicio):
    pendiente = _esperar(servicio)
    servicio.notify_history(_sync(fin=1))
    assert pendiente.end_of_history_type == 1


# ---------------------------------------------------------------------------
# El waiter se registra ANTES de enviar
# ---------------------------------------------------------------------------


def test_el_waiter_se_registra_antes_del_envio():
    """Las respuestas que funcionaron llegaron en ~1 s.

    Registrar el waiter despues del envio abre una ventana en la que una
    respuesta rapida no encuentra a nadie y la espera agota sus 45 s.
    """
    import ast
    import inspect
    import textwrap

    fuente = textwrap.dedent(
        inspect.getsource(BackfillService._request_once_locked)
    )
    codigo = ast.unparse(ast.parse(fuente))
    registro = codigo.index("self._pending[chat_jid] = pending")
    envio = codigo.index("send_message")
    assert registro < envio, "el waiter tiene que existir antes de enviar"


def test_una_respuesta_inmediata_no_se_pierde(servicio):
    """La respuesta llega antes de que nadie empiece a esperar."""

    async def escenario():
        pendiente = _esperar(servicio)
        servicio.notify_history(_sync())  # responde YA
        await asyncio.wait_for(pendiente.event.wait(), timeout=1)
        return pendiente.messages

    assert asyncio.run(escenario()) == 3


def test_una_respuesta_dos_segundos_despues_del_ack_tambien_resuelve(servicio):
    async def escenario():
        pendiente = _esperar(servicio)

        async def tarde():
            await asyncio.sleep(0.05)
            servicio.notify_history(_sync())

        asyncio.get_running_loop().create_task(tarde())
        await asyncio.wait_for(pendiente.event.wait(), timeout=2)
        return pendiente.messages

    assert asyncio.run(escenario()) == 3


# ---------------------------------------------------------------------------
# Una peticion cada vez, venga de donde venga
# ---------------------------------------------------------------------------


def test_dos_peticiones_no_pueden_solaparse(servicio, monkeypatch):
    """Se midio lo contrario: dos en vuelo con dos segundos de diferencia.

    Una salio por "desperto: se le pide historial ahora" (la cola de
    despertados, que llama a ``_process_chat`` directamente) y otra por el
    ciclo automatico, que si pasa por ``run()``. ``_busy`` solo protegia el
    segundo camino.
    """
    solapadas = []
    vivas = 0

    async def falso_request(self, chat_id, chat_jid, cursor):
        nonlocal vivas
        vivas += 1
        solapadas.append(vivas)
        await asyncio.sleep(0.02)
        vivas -= 1
        return True

    monkeypatch.setattr(
        BackfillService, "_request_once_locked", falso_request, raising=True
    )

    async def escenario():
        await asyncio.gather(
            servicio._request_once(1, "a@lid", None),
            servicio._request_once(2, "b@lid", None),
            servicio._request_once(3, "c@lid", None),
        )

    asyncio.run(escenario())
    assert max(solapadas) == 1, "solo puede haber UNA peticion ON_DEMAND en vuelo"


def test_el_candado_es_el_mismo_para_todos_los_caminos(servicio):
    assert servicio._lock_ondemand() is servicio._lock_ondemand()


# ---------------------------------------------------------------------------
# El destino, con el Web Companion vinculado
# ---------------------------------------------------------------------------


def _cliente(user="573002389304", server="s.whatsapp.net", device=75):
    return SimpleNamespace(
        device=SimpleNamespace(
            jid=SimpleNamespace(user=user, server=server, device=device),
            lid="86531142340710@lid",
        )
    )


def test_el_destino_es_el_telefono_principal(servicio):
    servicio._client = _cliente()
    destino = servicio._target_jid()
    assert destino.device == 0
    assert destino.server == "s.whatsapp.net"
    assert destino.user == "573002389304"


def test_un_web_companion_vinculado_no_cambia_el_destino(servicio):
    """Se vinculo un dispositivo nuevo (el 94) el mismo dia de los timeouts.

    El destino no se resuelve por usync: se construye desde nuestro propio JID
    poniendo ``device=0``. Que existan el 92 y el 94 no puede desviarlo.
    """
    for numero in (75, 92, 94):
        servicio._client = _cliente(device=numero)
        assert servicio._target_jid().device == 0


def test_el_destino_nunca_es_el_jid_del_chat(servicio):
    servicio._client = _cliente()
    destino = servicio._target_jid()
    assert not str(destino.user).endswith("lid")


# ---------------------------------------------------------------------------
# La sospecha tiene que poder levantarse
# ---------------------------------------------------------------------------


@pytest.fixture
def con_huella(servicio, monkeypatch):
    monkeypatch.setattr(servicio, "session_fingerprint", lambda: "huella-prueba")
    return servicio


def test_de_sospechosa_a_confirmada_tras_una_respuesta(con_huella, session):
    """SUSPECT era una puerta de un solo sentido.

    ``_confirm_capability`` solo escribia cuando NO habia registro para la
    sesion. Con ``{"confirmed": True, "state": "SUSPECT"}`` ya guardado, un
    canary que funcionara despues no borraba el ``state``, y la capacidad
    seguia SUSPECT hasta desvincular.
    """
    from app.services import repository as repo

    repo.set_app_state(
        session, CAPABILITY_KEY, {"confirmed": True, "session": "huella-prueba"}
    )
    session.flush()
    con_huella._anotar_timeout_real()
    con_huella._anotar_timeout_real()
    assert con_huella.capability_state() == "SUSPECT"

    con_huella._confirm_capability()
    assert con_huella.capability_state() == "CONFIRMED"


def test_confirmar_borra_la_racha_de_timeouts(con_huella):
    con_huella._anotar_timeout_real()
    con_huella._confirm_capability()
    assert con_huella._timeouts_seguidos == 0


def test_un_cursor_malo_no_deja_la_capacidad_inservible(con_huella, session):
    """Dos timeouts la ponen en duda, no en imposible.

    ``SUSPECT`` significa "vuelve a probarlo con un canary", y ese canary
    puede usar OTRO chat. Nunca se llega a un estado del que no se salga.
    """
    from app.services import repository as repo

    repo.set_app_state(
        session, CAPABILITY_KEY, {"confirmed": True, "session": "huella-prueba"}
    )
    session.flush()
    for _ in range(10):
        con_huella._anotar_timeout_real()

    assert con_huella.capability_state() == "SUSPECT"
    guardado = repo.get_app_state(session, CAPABILITY_KEY)
    assert guardado["confirmed"] is True, "no se borra lo que ya se demostro"

    con_huella._confirm_capability()
    assert con_huella.capability_state() == "CONFIRMED"


def test_la_espera_de_reintento_sigue_siendo_la_de_siempre():
    from app.history.cursor import RETRY_BACKOFF_SECONDS

    assert RETRY_BACKOFF_SECONDS == (60, 300, 900, 3600)


# ---------------------------------------------------------------------------
# El aviso, antes del parser
# ---------------------------------------------------------------------------


def test_los_campos_8_y_12_del_aviso_se_leen():
    """pywhats modela hasta el 8; el 12 sigue ahi como campo desconocido."""
    from app.compat.history_compat import leer_notificacion
    from app.models.proto import OnDemandNotification

    crudo = OnDemandNotification()
    crudo.originalMessageID = "STANZA-1"
    crudo.peerDataRequestSessionID = "SESION-1"

    from pywhats.proto import HistorySyncNotification

    aviso = HistorySyncNotification()
    aviso.ParseFromString(crudo.SerializeToString())
    aviso.sync_type = 6  # ON_DEMAND
    aviso.chunk_order = 0

    datos = leer_notificacion(aviso)
    assert datos["sync_type"] == "ON_DEMAND"
    assert datos["original_message_id"] == "STANZA-1"
    assert datos["peer_session_id"] == "SESION-1"


def test_un_aviso_sin_identificador_no_revienta():
    from app.compat.history_compat import leer_notificacion
    from pywhats.proto import HistorySyncNotification

    aviso = HistorySyncNotification()
    aviso.sync_type = 0
    datos = leer_notificacion(aviso)
    assert datos["peer_session_id"] is None
    assert datos["sync_type"] == "INITIAL_BOOTSTRAP"


def test_el_identificador_del_aviso_llega_al_blob(monkeypatch):
    """El aviso y el blob son dos cosas distintas y viajan por separado.

    Si el identificador no se guarda al recibir el aviso, se pierde antes de
    llegar al parser y la unica correlacion posible vuelve a ser el JID.
    """
    import zlib

    from app.compat import history_compat
    from pywhats.proto import HistorySync as HistorySyncProto

    proto = HistorySyncProto()
    proto.sync_type = 6
    crudo = proto.SerializeToString()

    history_compat._notificacion.set(
        {"original_message_id": "STANZA-9", "peer_session_id": "SESION-9"}
    )
    try:
        completo = history_compat.parse_full(crudo)
    finally:
        history_compat._notificacion.set(None)

    assert completo.original_message_id == "STANZA-9"
    assert completo.peer_session_id == "SESION-9"
    del zlib
