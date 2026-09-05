"""El experimento: mismo chat, ancla de hoy contra ancla que ya funciono.

POR QUE ESTE EXPERIMENTO
------------------------
Se probo una peticion ON_DEMAND con TODO lo comprobable en local correcto:
cursor valido, marca en segundos, ``from_me`` correcto, destino el telefono
propio en el dispositivo 0, forma valida campo a campo, ``enc_type=msg`` y
sesion Signal establecida. Salio, obtuvo ACK y no llego respuesta.

Cuando todo lo que se puede mirar esta bien, lo unico que queda es medir. Y la
medicion que separa las dos hipotesis que quedan es esta: repetir, sobre el
MISMO chat, una peticion cuyo ancla ya obtuvo respuesta, y compararla con el
ancla que el motor usaria hoy.

    responde el known-good y no el actual  -> el problema es el ancla
    responden los dos                      -> ON_DEMAND esta sano
    no responde ninguno                    -> cambio la sesion o el servidor

DE DONDE SALE EL KNOWN-GOOD
---------------------------
``history_requests`` guarda cada peticion emitida con su ancla y su resultado.
No hay que reconstruir nada ni fiarse del log: hay 88 filas con
``status='received'``, y para la elegida el log ademas confirma
``endOfHistoryTransferType=0`` y 50 mensajes.

LA GARANTIA
-----------
El camino de diagnostico usa el MISMO constructor, el MISMO destino, el MISMO
waiter y la MISMA correlacion que la excavacion normal -- un camino especial
podria funcionar aqui y fallar alli, y entonces no habria medido nada -- pero
no escribe cursor, ni estado, ni intentos, ni mensajes.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.services.backfill_service import BackfillService, CAPABILITY_KEY
from tests.test_backfill_accounting import _DatabaseFalsa


# ---------------------------------------------------------------------------
# Andamio
# ---------------------------------------------------------------------------


class _SenderFalso:
    """Registra lo que se le manda y no habla con nadie."""

    def __init__(self, tarda: float = 0.0):
        self.enviados: list[tuple] = []
        self._tarda = tarda

    async def send_message(self, destino, mensaje):
        if self._tarda:
            await asyncio.sleep(self._tarda)
        self.enviados.append((destino, mensaje))
        return SimpleNamespace(id=f"STANZA-{len(self.enviados)}")


def _cliente(sender=None):
    return SimpleNamespace(
        _sender=sender or _SenderFalso(),
        device=SimpleNamespace(
            jid=SimpleNamespace(user="573002389304", server="s.whatsapp.net", device=75),
            lid="86531142340710@lid",
        ),
    )


def _servicio_con(settings, session, monkeypatch, espera: float):
    import dataclasses

    ajustes = dataclasses.replace(settings, history_request_timeout=espera)
    s = BackfillService(ajustes, _DatabaseFalsa(session))
    monkeypatch.setattr(s, "session_fingerprint", lambda: "huella-prueba")
    monkeypatch.setattr(s, "peer_session_state", lambda: {"pn": True, "lid": True})
    return s


@pytest.fixture
def servicio(settings, session, monkeypatch):
    """Para las pruebas que comprueban el TIMEOUT.

    45 s por cada una dejaria la suite en varios minutos de espera. Lo que se
    comprueba aqui es el camino, no cuanto aguanta la espera.
    """
    return _servicio_con(settings, session, monkeypatch, 0.05)


@pytest.fixture
def servicio_paciente(settings, session, monkeypatch):
    """Para las que comprueban la RESPUESTA.

    Con la espera de 0,05 s la respuesta simulada llegaba justo en el limite y
    la prueba fallaba solo cuando la suite entera corria a la vez: no por el
    codigo, sino por la maquina. Aqui el margen es amplio a proposito -- estas
    pruebas terminan en cuanto contesta, asi que no cuesta nada.
    """
    return _servicio_con(settings, session, monkeypatch, 5.0)





def _pedir(servicio, cliente, **extra):
    datos = {
        "chat_jid": "206566000000000@lid",
        "message_id": "AC" + "0" * 30,
        "timestamp": 1788458511,
        "from_me": False,
    }
    datos.update(extra)
    return asyncio.run(servicio.request_diagnostico(cliente, **datos))


def _responder(servicio, chat_jid="206566000000000@lid", mensajes=50, fin=0):
    """Lo que hace la ingesta cuando llega el blob."""
    conversacion = SimpleNamespace(
        jid=chat_jid, messages=[object()] * mensajes, end_of_history_type=fin
    )
    servicio.notify_history(
        SimpleNamespace(
            sync_type="ON_DEMAND", conversations=[conversacion], peer_session_id=None
        )
    )


# ---------------------------------------------------------------------------
# Una sola peticion, y solo si se pide
# ---------------------------------------------------------------------------


def test_manda_exactamente_una_peticion(servicio):
    sender = _SenderFalso()
    _pedir(servicio, _cliente(sender))
    assert len(sender.enviados) == 1


def test_el_timeout_no_reintenta(servicio):
    """Una prueba que reintenta sola deja de ser una prueba."""
    sender = _SenderFalso()
    medido = _pedir(servicio, _cliente(sender))
    assert len(sender.enviados) == 1
    assert medido["history_response"] is False


def test_usa_el_ancla_que_se_le_da_sin_recalcularla(servicio):
    """La variante known-good pierde todo el sentido si se recalcula.

    ``get_valid_history_cursor`` devuelve el ancla MAS ANTIGUA conocida hoy,
    que para un chat agotado es justo la que dio FINAL. Si el diagnostico la
    recalculara, la peticion historica no se estaria repitiendo.
    """
    from app.models.proto import OnDemandMessage

    sender = _SenderFalso()
    _pedir(servicio, _cliente(sender), timestamp=1786334153, message_id="BB" + "1" * 30)

    _, mensaje = sender.enviados[0]
    leido = OnDemandMessage()
    leido.ParseFromString(mensaje.SerializeToString())
    peticion = leido.protocolMessage.peerDataOperationRequestMessage.historySyncOnDemandRequest
    assert peticion.oldestMsgID == "BB" + "1" * 30
    assert peticion.oldestMsgTimestampMS == 1786334153


def test_la_forma_de_la_peticion_es_la_de_siempre(servicio):
    """Lo unico que cambia entre las dos variantes es el ancla."""
    from app.models.proto import HISTORY_SYNC_ON_DEMAND, OnDemandMessage

    sender = _SenderFalso()
    _pedir(servicio, _cliente(sender))
    destino, mensaje = sender.enviados[0]

    assert destino.device == 0
    assert destino.server == "s.whatsapp.net"

    leido = OnDemandMessage()
    leido.ParseFromString(mensaje.SerializeToString())
    operacion = leido.protocolMessage.peerDataOperationRequestMessage
    peticion = operacion.historySyncOnDemandRequest
    assert leido.protocolMessage.type == 16
    assert operacion.peerDataOperationRequestType == HISTORY_SYNC_ON_DEMAND
    assert peticion.onDemandMsgCount == 50
    assert len(str(peticion.oldestMsgTimestampMS)) == 10
    assert not mensaje.HasField("device_sent_message")


def test_from_me_viaja_tal_cual(servicio):
    from app.models.proto import OnDemandMessage

    for valor in (True, False):
        sender = _SenderFalso()
        _pedir(servicio, _cliente(sender), from_me=valor)
        leido = OnDemandMessage()
        leido.ParseFromString(sender.enviados[0][1].SerializeToString())
        peticion = (
            leido.protocolMessage.peerDataOperationRequestMessage.historySyncOnDemandRequest
        )
        assert peticion.oldestMsgFromMe is valor


# ---------------------------------------------------------------------------
# Sin mutaciones
# ---------------------------------------------------------------------------


def test_el_diagnostico_no_escribe_en_history_requests(servicio, session):
    from sqlalchemy import func, select

    from app.models import HistoryRequest

    antes = session.execute(select(func.count()).select_from(HistoryRequest)).scalar()
    _pedir(servicio, _cliente())
    session.flush()
    despues = session.execute(select(func.count()).select_from(HistoryRequest)).scalar()
    assert antes == despues


def test_el_diagnostico_no_toca_el_estado_del_chat(servicio, session):
    """Ni cursor, ni history_status, ni intentos, ni waiting_seed."""
    from sqlalchemy import select

    from app.models import Chat, ChatHistoryState

    chat = Chat(jid="206566000000000@lid", chat_type="individual")
    session.add(chat)
    session.flush()
    session.add(
        ChatHistoryState(
            chat_id=chat.id,
            chat_jid=chat.jid,
            history_status="exhausted",
            oldest_message_id="VIEJO",
            oldest_message_timestamp=111,
            attempt_count=3,
        )
    )
    session.flush()

    _pedir(servicio, _cliente())
    session.flush()
    session.expire_all()

    guardado = session.execute(
        select(ChatHistoryState).where(
            ChatHistoryState.chat_jid == "206566000000000@lid"
        )
    ).scalar_one()
    assert guardado.history_status == "exhausted"
    assert guardado.oldest_message_id == "VIEJO"
    assert guardado.oldest_message_timestamp == 111
    assert guardado.attempt_count == 3


def test_lo_declara_en_su_propio_resultado(servicio):
    medido = _pedir(servicio, _cliente())
    assert medido["cursor_written"] is False
    assert medido["history_status_changed"] is False
    assert medido["attempts_incremented"] is False
    assert medido["messages_persisted"] is False


def test_durante_la_prueba_los_blobs_on_demand_no_se_persisten(servicio):
    """La bandera solo esta viva mientras dura la peticion."""
    visto = []

    class _Espia(_SenderFalso):
        async def send_message(self, destino, mensaje):
            visto.append(servicio.diagnostico_sin_persistir)
            return await super().send_message(destino, mensaje)

    assert servicio.diagnostico_sin_persistir is False
    _pedir(servicio, _cliente(_Espia()))
    assert visto == [True]
    assert servicio.diagnostico_sin_persistir is False


def test_la_bandera_se_baja_aunque_el_envio_falle(servicio):
    class _Roto(_SenderFalso):
        async def send_message(self, destino, mensaje):
            raise RuntimeError("socket cerrado")

    medido = _pedir(servicio, _cliente(_Roto()))
    assert "error" in medido
    assert servicio.diagnostico_sin_persistir is False


# ---------------------------------------------------------------------------
# Exclusividad
# ---------------------------------------------------------------------------


def test_no_arranca_si_hay_una_excavacion_en_marcha(servicio):
    servicio._busy = True
    medido = _pedir(servicio, _cliente())
    assert medido["error"] == "busy"


def test_no_arranca_si_hay_una_peticion_en_vuelo(servicio):
    servicio._in_flight.add("otro@lid")
    medido = _pedir(servicio, _cliente())
    assert medido["error"] == "busy"


def test_mientras_corre_bloquea_el_ciclo_y_el_canary(servicio):
    """``run()`` y ``run_canary()`` miran ``_busy``, asi que se retiran."""
    estados = []

    class _Espia(_SenderFalso):
        async def send_message(self, destino, mensaje):
            estados.append(servicio._busy)
            return await super().send_message(destino, mensaje)

    _pedir(servicio, _cliente(_Espia()))
    assert estados == [True]
    assert servicio._busy is False


def test_al_terminar_suelta_todo(servicio):
    _pedir(servicio, _cliente())
    assert servicio._busy is False
    assert servicio._in_flight == frozenset()
    assert servicio._pending == {}


def test_la_cola_de_despertados_espera_su_turno(servicio, monkeypatch):
    """Comparten el mismo candado, asi que no pueden solaparse."""
    vivas = 0
    maximo = 0

    async def falso(self, chat_id, chat_jid, cursor):
        nonlocal vivas, maximo
        vivas += 1
        maximo = max(maximo, vivas)
        await asyncio.sleep(0.02)
        vivas -= 1
        return True

    monkeypatch.setattr(BackfillService, "_request_once_locked", falso, raising=True)

    async def escenario():
        # El diagnostico usa el mismo lock que ``_request_once``.
        async with servicio._lock_ondemand():
            await asyncio.sleep(0.01)
        await asyncio.gather(
            servicio._request_once(1, "a@lid", None),
            servicio._request_once(2, "b@lid", None),
        )

    asyncio.run(escenario())
    assert maximo == 1


# ---------------------------------------------------------------------------
# Que se mide, y que confirma la capacidad
# ---------------------------------------------------------------------------


def _con_respuesta(servicio, cliente, mensajes=50, fin=0):
    """Envia y responde desde otra tarea, como haria la ingesta."""

    async def escenario():
        async def contestar():
            await asyncio.sleep(0.02)
            _responder(servicio, mensajes=mensajes, fin=fin)

        asyncio.get_running_loop().create_task(contestar())
        return await servicio.request_diagnostico(
            cliente,
            chat_jid="206566000000000@lid",
            message_id="AC" + "0" * 30,
            timestamp=1788458511,
            from_me=False,
        )

    return asyncio.run(escenario())


def test_una_respuesta_real_deja_todo_lo_que_hay_que_medir(servicio_paciente):
    medido = _con_respuesta(servicio_paciente, _cliente())
    assert medido["request_sent"] is True
    assert medido["ack"] is True
    assert medido["history_response"] is True
    assert medido["messages"] == 50
    assert medido["result"] == "MORE"
    assert medido["latency_seconds"] is not None
    assert medido["correlation"] == "chat_jid"
    assert medido["destination_primary"] is True
    assert medido["device"] == 0
    assert medido["count"] == 50


def test_el_fin_de_historial_se_traduce(servicio_paciente):
    assert (
        _con_respuesta(servicio_paciente, _cliente(), mensajes=0, fin=1)["result"]
        == "FINAL"
    )
    assert _con_respuesta(servicio_paciente, _cliente(), fin=2)["result"] == "MORE"


def test_un_ack_NO_confirma_la_capacidad(servicio, session):
    """El ACK lo emite el servidor al aceptar la stanza. No dice nada mas."""
    from app.services import repository as repo

    repo.set_app_state(
        session,
        CAPABILITY_KEY,
        {"confirmed": True, "session": "huella-prueba", "state": "SUSPECT"},
    )
    session.flush()

    medido = _pedir(servicio, _cliente())
    assert medido["ack"] is True
    assert medido["history_response"] is False
    assert medido["capability_after"] == "SUSPECT"


def test_una_respuesta_real_SI_levanta_la_sospecha(servicio_paciente, session):
    from app.services import repository as repo

    repo.set_app_state(
        session,
        CAPABILITY_KEY,
        {"confirmed": True, "session": "huella-prueba", "state": "SUSPECT"},
    )
    session.flush()

    medido = _con_respuesta(servicio_paciente, _cliente())
    assert medido["capability_before"] == "SUSPECT"
    assert medido["capability_after"] == "CONFIRMED"


def test_un_timeout_no_borra_nada(servicio, session):
    from app.services import repository as repo

    repo.set_app_state(
        session,
        CAPABILITY_KEY,
        {"confirmed": True, "session": "huella-prueba", "state": "SUSPECT"},
    )
    session.flush()
    _pedir(servicio, _cliente())
    guardado = repo.get_app_state(session, CAPABILITY_KEY)
    assert guardado["confirmed"] is True
    assert guardado["state"] == "SUSPECT"


def test_los_avisos_del_experimento_se_distinguen(servicio, caplog):
    with caplog.at_level(logging.INFO):
        _pedir(servicio, _cliente())
    assert "[ON_DEMAND_TEST]" in caplog.text


# ---------------------------------------------------------------------------
# Privacidad de los logs
# ---------------------------------------------------------------------------


def _redactar(mensaje: str, *args) -> str:
    from app.core.logging_setup import SecretRedactionFilter

    registro = logging.LogRecord(
        "pywhats.messaging.receiver", logging.INFO, "x", 1, mensaje, args, None
    )
    SecretRedactionFilter().filter(registro)
    return registro.getMessage()


def test_el_volcado_hexadecimal_del_cuerpo_no_llega_al_log():
    """pywhats registra en INFO el protobuf DESCIFRADO entero.

    ``receiver: empty text id=... len=... hex=...`` sale cuando su
    ``_extract_text`` no reconoce la variante del mensaje. Esa cadena lleva el
    texto, las URL de multimedia y el resto de la metadata en claro. Se
    contaron 220 apariciones en un solo archivo de log.
    """
    # Un protobuf con texto real dentro.
    cuerpo = b"\n\x0bhola mundo\x12\x04algo" * 4
    salida = _redactar(
        "receiver: empty text id=%s len=%d hex=%s", "AC01", len(cuerpo), cuerpo.hex()
    )
    assert "hex=" in salida, "la cabecera se conserva: dice QUE campo llego"
    assert "redactados" in salida
    assert cuerpo.hex() not in salida
    assert "hola mundo".encode().hex() not in salida


def test_se_conserva_lo_justo_para_saber_que_campo_era():
    from app.core.logging_setup import HEX_VISIBLE

    cuerpo = bytes(range(60))
    salida = _redactar("hex=%s", cuerpo.hex())
    assert cuerpo.hex()[:HEX_VISIBLE] in salida
    assert cuerpo.hex()[HEX_VISIBLE:] not in salida


def test_una_url_de_multimedia_no_llega_al_log():
    salida = _redactar(
        "descargando https://mmg.whatsapp.net/v/t62.7118-24/1234567?ccb=11-4&oh=abc listo"
    )
    assert "mmg.whatsapp.net" not in salida
    assert "https://***" in salida
    assert salida.endswith("listo"), "solo se tapa la URL, no la linea entera"


def test_un_hex_corto_no_se_toca():
    """Un identificador corto no es un volcado; taparlo solo estorbaria."""
    assert _redactar("hex=deadbeef") == "hex=deadbeef"


def test_las_lineas_normales_pasan_intactas():
    for linea in (
        "receiver: ack->ok id=AE167C9B79748FC6 class=message",
        "history sync: ON_DEMAND chunk=0 progress=100 convos=1 msgs=50",
        "[ON_DEMAND_TEST] respuesta latency=1.2s result=MORE mensajes=50",
    ):
        assert _redactar(linea) == linea


def test_la_redaccion_no_cambia_el_comportamiento_del_receptor():
    """Solo se filtra el log. El receptor sigue haciendo lo mismo.

    Se comprueba que NO se ha tocado el paquete: la linea sigue emitiendose
    desde pywhats y lo que cambia es lo que se escribe, no lo que se decide.
    """
    import inspect

    from pywhats.messaging import receiver

    fuente = inspect.getsource(receiver)
    assert "receiver: empty text id=%s len=%d hex=%s" in fuente, (
        "si esto cambia, revisa que el filtro siga cubriendo el caso"
    )
