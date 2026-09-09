"""Los dos proveedores de WhatsApp, y la costura que no se puede mover.

QUE SE PRUEBA AQUI
------------------
La migracion a Baileys se sostiene sobre una promesa: que desde la ingesta
hacia arriba nada se entera de que libreria esta hablando con WhatsApp. Estas
pruebas son la unica forma de que esa promesa no dependa de la buena memoria.

Lo dificil no es que Baileys funcione: es que emita EXACTAMENTE lo mismo. Un
nombre de evento distinto o un campo que cambia de sitio no da error -- da
mensajes que faltan, y se descubre semanas despues.
"""

from __future__ import annotations

import base64
import dataclasses
import uuid
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# El interruptor
# ---------------------------------------------------------------------------


def test_la_fabrica_solo_construye_baileys(settings):
    """Un unico proveedor, sin condicional.

    Hubo un interruptor ``WA_PROVIDER`` mientras convivian pywhats y Baileys.
    Se retiro entero: quedarse con la rama muerta significaba mantener viva
    una libreria Pre-Alpha con criptografia sin auditar.
    """
    import queue

    from app.wa import crear_cliente_de_whatsapp
    from app.wa.baileys_client import BaileysClient

    assert isinstance(crear_cliente_de_whatsapp(settings, queue.Queue()), BaileysClient)


def test_ya_no_queda_interruptor_de_proveedor():
    """Ni la variable, ni la bandera, ni las siete COMPAT_* de pywhats."""
    from app.core import config

    assert not hasattr(config, "_proveedor_de_whatsapp")
    assert not hasattr(config, "PROVEEDORES")
    ajustes = config.load_settings()
    assert not hasattr(ajustes, "wa_provider")
    assert [a for a in dir(ajustes) if a.startswith("compat_")] == []


def test_los_dos_sitios_donde_se_crea_el_cliente_pasan_por_la_fabrica():
    """Que uno se quedara en pywhats y el otro en Baileys solo se notaria a
    mitad de una re-vinculacion, con la sesion ya abierta."""
    import inspect

    from app.core import runtime

    fuente = inspect.getsource(runtime)
    assert fuente.count("crear_cliente_de_whatsapp") >= 2
    # Y ya no se instancia a mano en ningun sitio.
    assert "self.client = WhatsAppClient(" not in fuente


# ---------------------------------------------------------------------------
# El contrato de eventos
# ---------------------------------------------------------------------------


def test_el_worker_emite_TODOS_los_eventos_del_contrato():
    """La lista de nombres es el contrato de verdad.

    Los sinks y el traductor de SSE buscan por nombre: si el worker dejara de
    emitir uno, la ingesta se rompe sin que salte ningun error. Antes esto se
    comprobaba contra `PYWHATS_EVENTS`; ahora, contra quien de verdad los
    produce.
    """
    from app.wa.port import EVENTOS

    fuente = Path("wa_baileys/worker.js").read_text(encoding="utf-8")
    # `message_edit` y `message_revoke` salen con el nombre en una variable,
    # decidido por `clasificarActualizacion`; los demas van literales.
    traductor = Path("wa_baileys/traducir.js").read_text(encoding="utf-8")
    faltan = [
        nombre
        for nombre in EVENTOS
        if f"evento('{nombre}'" not in fuente
        and f'evento("{nombre}"' not in fuente
        and f"return '{nombre}'" not in traductor
    ]
    assert not faltan, f"el worker no emite: {faltan}"


#: Todo lo que ESCRIBE en WhatsApp. Ninguno se usa hoy: verificado con grep
#: sobre `app/` entero. De las 20 corutinas publicas del cliente de pywhats el
#: proyecto usa 6, y ninguna manda nada.
ENVIO_PROHIBIDO = (
    "send_text",
    "send_image",
    "send_video",
    "send_audio",
    "send_document",
    "send_sticker",
    "send_reaction",
    "send_group_text",
    "edit_message",
    "revoke_message",
    "mark_read",
    "send_presence",
    "subscribe_presence",
    "send_chat_presence",
)


def test_el_puerto_NO_declara_nada_de_envio():
    """Es una copia de seguridad: solo lee.

    Lo que no esta en el puerto no se puede llamar desde arriba, y eso quita a
    la vez superficie de error y el motivo mas comun de bloqueo de cuenta.

    Se miran los MIEMBROS declarados, no el texto: la documentacion del puerto
    nombra estos metodos justamente para decir que no estan, y buscar la
    cadena a secas convertia esa explicacion en un fallo.
    """
    from app.wa import port

    declarados = set()
    for clase in (port.ClienteDeWhatsApp, port.SesionRemota):
        declarados |= {n for n in vars(clase) if not n.startswith("__")}

    for prohibido in ENVIO_PROHIBIDO:
        assert prohibido not in declarados, f"el puerto no puede ofrecer {prohibido}"


def test_la_sesion_de_baileys_tampoco_ofrece_envio():
    """Lo que reciben los servicios en `post_connect`."""
    from app.wa.baileys_client import SesionBaileys

    declarados = {n for n in vars(SesionBaileys) if not n.startswith("__")}
    for prohibido in ENVIO_PROHIBIDO:
        assert prohibido not in declarados


def test_el_worker_de_node_no_LLAMA_a_ninguna_funcion_de_envio():
    """La misma garantia, del otro lado de la tuberia.

    Se busca la LLAMADA (`sock.sendMessage(`), no la palabra: el encabezado
    del worker menciona esos nombres para dejar dicho que no estan.
    """
    fuente = Path("wa_baileys/worker.js").read_text(encoding="utf-8")
    for prohibido in (
        "sendMessage",
        "readMessages",
        "sendPresenceUpdate",
        "sendReceipt",
        "sendReadReceipt",
    ):
        assert f"sock.{prohibido}(" not in fuente, f"el worker no puede llamar a {prohibido}"


# ---------------------------------------------------------------------------
# Aislamiento multiusuario: lo que NO se puede romper
# ---------------------------------------------------------------------------


def test_cada_cuenta_tiene_su_carpeta_de_sesion_de_baileys(settings, tmp_path):
    """Dos cuentas no pueden compartir credenciales ni almacen de Signal.

    El aislamiento viene del mecanismo que ya existe: cada `AppRuntime` recibe
    unos `Settings` con el `session_dir` de SU cuenta. Baileys cuelga de ahi,
    asi que no hace falta nada nuevo -- pero si se comprueba, porque romperlo
    seria darle la sesion de una persona a otra.
    """
    import queue

    from app.core.session_paths import ajustes_de_cuenta
    from app.wa.baileys_client import BaileysClient

    base = dataclasses.replace(settings, session_dir=tmp_path)
    una, otra = uuid.uuid4(), uuid.uuid4()

    cliente_a = BaileysClient(ajustes_de_cuenta(base, una), queue.Queue())
    cliente_b = BaileysClient(ajustes_de_cuenta(base, otra), queue.Queue())

    assert cliente_a.carpeta_de_sesion != cliente_b.carpeta_de_sesion
    assert str(una) in str(cliente_a.carpeta_de_sesion)
    assert str(otra) in str(cliente_b.carpeta_de_sesion)


def test_la_carpeta_de_sesion_es_UNA_unidad(settings, tmp_path):
    """Credenciales y almacen de Signal viven juntos, y se borran juntos.

    Medio borrado deja un dispositivo NUEVO usando ratchets VIEJOS: el sintoma
    es `unknown one-time pre-key id` y peticiones de historial que reciben ACK
    y despues nada. Es el fallo que mas costo diagnosticar.
    """
    import inspect
    import queue

    from app.wa.baileys_client import BaileysClient

    cliente = BaileysClient(dataclasses.replace(settings, session_dir=tmp_path), queue.Queue())
    # Una sola carpeta, no dos rutas sueltas que alguien pueda borrar por
    # separado.
    assert cliente.carpeta_de_sesion == tmp_path / "baileys"

    fuente = inspect.getsource(BaileysClient.carpeta_de_sesion.fget)
    assert "indivisible" in fuente.lower() or "unidad" in fuente.lower()


# ---------------------------------------------------------------------------
# La traduccion: que la ingesta reciba lo de siempre
# ---------------------------------------------------------------------------


def _cliente(settings, tmp_path):
    import queue

    from app.wa.baileys_client import BaileysClient

    return BaileysClient(dataclasses.replace(settings, session_dir=tmp_path), queue.Queue())


def test_el_historial_llega_con_la_forma_que_espera_la_ingesta(settings, tmp_path):
    """`ingest_history_sync` no cambia: recibe lo mismo que con pywhats."""
    cliente = _cliente(settings, tmp_path)

    lote = cliente._carga_de(
        "history_sync",
        {
            "sync_type": "ON_DEMAND",
            "chunk_order": 0,
            "progress": 100,
            "conversations": [
                {
                    "jid": "a@s.whatsapp.net",
                    "name": "Marta",
                    "last_message_timestamp": 1700000000,
                    "unread_count": 2,
                    "messages": [[base64.b64encode(b"crudo").decode(), 0]],
                    "end_of_history_type": 1,
                    "end_of_history": True,
                }
            ],
            "pushnames": [["a@s.whatsapp.net", "Marta"]],
        },
    )

    assert lote.sync_type == "ON_DEMAND"
    assert lote.message_count == 1
    conv = lote.conversations[0]
    assert conv.jid == "a@s.whatsapp.net"
    assert conv.messages == [(b"crudo", 0)]
    # El marcador de fin es lo que sostiene todo el estado `exhausted`.
    assert conv.end_of_history_type == 1
    assert conv.end_of_history is True
    assert lote.pushnames == [("a@s.whatsapp.net", "Marta")]


def test_un_mensaje_del_historial_ilegible_no_tira_el_lote(settings, tmp_path):
    cliente = _cliente(settings, tmp_path)

    lote = cliente._carga_de(
        "history_sync",
        {
            "sync_type": "FULL",
            "conversations": [
                {
                    "jid": "a@s.whatsapp.net",
                    "messages": [["no-es-base64-valido!!!", 0], [base64.b64encode(b"ok").decode(), 1]],
                }
            ],
        },
    )

    assert lote.conversations[0].messages == [(b"ok", 1)]


def test_el_JID_tiene_la_misma_forma_que_el_de_pywhats(settings, tmp_path):
    """El resto del codigo lee `.user` y `.server`. No puede notar el cambio."""
    from app.wa.baileys_client import jid_desde

    jid = jid_desde("573001234567@s.whatsapp.net")
    assert jid.user == "573001234567"
    assert jid.server == "s.whatsapp.net"
    assert jid.device == 0

    # Y `jid_to_string` de live_service tiene que seguir sabiendo leerlo.
    from app.services.live_service import jid_to_string

    assert jid_to_string(jid) == "573001234567@s.whatsapp.net"


def test_un_lid_NO_se_convierte_en_telefono(settings, tmp_path):
    from app.services.live_service import jid_to_string
    from app.wa.baileys_client import jid_desde

    assert jid_to_string(jid_desde("86531142340710@lid")) == "86531142340710@lid"


def test_el_mensaje_en_vivo_deja_el_protobuf_donde_live_service_lo_busca(
    settings, tmp_path
):
    """Sin esto no hay `raw_proto`: ni clasificacion por protobuf ni adjunto.

    Con pywhats hacia falta un parche para capturarlo (`protocol_flag`). Con
    Baileys llega de serie, pero el hueco es el mismo para que `live_service`
    no cambie ni una linea.
    """
    from app.wa.crudo import last_raw_message

    cliente = _cliente(settings, tmp_path)
    cliente._carga_de(
        "message",
        {
            "id": "ABC",
            "chat": "a@s.whatsapp.net",
            "sender": "a@s.whatsapp.net",
            "from_me": False,
            "timestamp": 1700000000,
            "raw_proto": base64.b64encode(b"\x0a\x00").decode(),
        },
    )

    assert last_raw_message() == b"\x0a\x00"


def test_el_mensaje_en_vivo_NO_trae_adjunto_propio(settings, tmp_path):
    """El adjunto lo detecta NUESTRO parser desde el protobuf.

    Un solo camino en vez de dos que hay que mantener de acuerdo: el mismo que
    ya usaban los mensajes salientes.
    """
    cliente = _cliente(settings, tmp_path)
    mensaje = cliente._carga_de(
        "message",
        {"id": "A", "chat": "a@s.whatsapp.net", "timestamp": 1, "raw_proto": None},
    )

    assert mensaje.media is None
    assert mensaje.id == "A"


def test_el_qr_se_entrega_como_CADENA_no_como_diccionario(settings, tmp_path):
    """`note_qr` y `render_qr` reciben esto tal cual y lo dibujan.

    Con el diccionario entero dentro, la imagen sale --es un QR valido-- pero
    codifica `{'qr': '2@...'}` y el telefono no la reconoce. Un fallo mudo:
    la pantalla muestra un codigo que no vincula.
    """
    cliente = _cliente(settings, tmp_path)
    carga = cliente._carga_de("qr", {"qr": "2@abc,B,C,D"})
    assert carga == "2@abc,B,C,D"
    assert isinstance(carga, str)


def test_un_qr_sin_carga_no_revienta(settings, tmp_path):
    cliente = _cliente(settings, tmp_path)
    assert cliente._carga_de("qr", {}) is None
    assert cliente._carga_de("qr", None) is None


def test_los_eventos_pasan_por_el_sink_antes_de_publicarse(settings, tmp_path):
    """Es como se persiste en PostgreSQL sin pasar por la cola de la pantalla.

    El orden importa: si se publicara antes, la pantalla podria pedir un chat
    que todavia no existe.
    """
    cliente = _cliente(settings, tmp_path)
    vistos = []
    cliente.sinks["message"] = lambda carga: vistos.append(carga)

    cliente._reenviar(
        {
            "event": "client",
            "name": "message",
            "payload": {"id": "A", "chat": "a@s.whatsapp.net", "timestamp": 1},
            "extra": {},
        }
    )

    assert len(vistos) == 1
    encolado = cliente._events.get_nowait()
    assert encolado.name == "message"


def test_un_sink_que_falla_no_para_la_recepcion(settings, tmp_path):
    cliente = _cliente(settings, tmp_path)

    def _revienta(_carga):
        raise RuntimeError("la base no responde")

    cliente.sinks["message"] = _revienta
    cliente._reenviar(
        {"name": "message", "payload": {"id": "A", "chat": "a@s.whatsapp.net", "timestamp": 1}}
    )

    # El evento se publica igual: el receptor manda.
    assert cliente._events.get_nowait().name == "message"


# ---------------------------------------------------------------------------
# La trampa que no desaparece
# ---------------------------------------------------------------------------


def test_el_worker_protege_contra_el_ancla_en_milisegundos():
    """El campo se llama `oldestMsgTimestampMS` y el telefono espera SEGUNDOS.

    Multiplicar por 1000 pone el ancla ~56.000 anos en el futuro: la stanza se
    acepta, llega el ACK y no vuelve ninguna respuesta. Es el fallo que mas
    costo encontrar, y no desaparece por cambiar de libreria.
    """
    fuente = Path("wa_baileys/worker.js").read_text(encoding="utf-8")
    assert "4102444800" in fuente, "falta la guarda contra milisegundos"
    assert "SEGUNDOS" in fuente


def test_el_worker_archiva_el_lote_antes_de_interpretarlo():
    """Es lo que salvo 5920 mensajes cuando la base se vacio."""
    fuente = Path("wa_baileys/worker.js").read_text(encoding="utf-8")
    assert "archivarLote" in fuente


def test_el_worker_distingue_logged_out_del_resto():
    """La diferencia entre reconectar solo y pedir un QR nuevo."""
    fuente = Path("wa_baileys/worker.js").read_text(encoding="utf-8")
    assert "DisconnectReason.loggedOut" in fuente
    assert "logged_out" in fuente


def test_el_worker_NO_se_anuncia_como_escritorio():
    """Anunciarse como escritorio hace que WhatsApp rechace el registro. (C-14)

    Baileys cambia `webInfo.webSubPlatform` de WEB_BROWSER a DARWIN o WIN32
    cuando `syncFullHistory` esta puesto Y el sistema del navegador aparece en
    su `PLATFORM_MAP` --que solo tiene 'Mac OS' y 'Windows'--. El servidor
    cierra el socket con 1011 nada mas leer ese registro, asi que el QR no
    llega a generarse NUNCA y la pantalla se queda cargando sin un error.

    Se midio: DARWIN y WIN32 fallan; WEB_BROWSER da QR en 0,4 s.
    """
    fuente = Path("wa_baileys/worker.js").read_text(encoding="utf-8")
    activos = [
        linea
        for linea in fuente.splitlines()
        if "Browsers." in linea and not linea.lstrip().startswith("*")
    ]
    assert activos, "el worker tiene que elegir un navegador"
    for linea in activos:
        assert "macOS" not in linea and "windows" not in linea, (
            f"{linea.strip()!r} activa la subplataforma que el servidor rechaza"
        )


def test_el_worker_SIGUE_pidiendo_el_historial_completo():
    """Renunciar al perfil de escritorio no puede costar el historial.

    Lo que de verdad lo pide es `requireFullSync`, y Baileys lo saca de
    `syncFullHistory` hacia `deviceProps` sin mirar el navegador
    (`Utils/validate-connection.js:72`).
    """
    fuente = Path("wa_baileys/worker.js").read_text(encoding="utf-8")
    assert "syncFullHistory: true" in fuente


# ---------------------------------------------------------------------------
# post_connect: enchufar la sesion a los consumidores de siempre
# ---------------------------------------------------------------------------


class _WorkerFalso:
    """Un `BaileysClient` al que se le sustituye la tuberia por respuestas."""

    def __init__(self, respuestas=None):
        self.pedidos = []
        self._respuestas = respuestas or {}
        self.carpeta_temporal = Path(".")

    def pedir(self, cmd, datos, espera=30.0):
        self.pedidos.append((cmd, datos))
        return self._respuestas.get(cmd, {})

    @property
    def device(self):
        return None


def test_el_cliente_expone_la_sesion_como_client(settings, tmp_path):
    """``_client`` es contrato de hecho: SEIS sitios lo leen.

    El backfill, los bordes perdidos, la auto-recuperacion, el diagnostico y
    la comprobacion de principal hacen todos
    ``getattr(runtime.client, "_client", None)``. Al migrar se quedo sin
    definir y todos vieron ``None``.

    El sintoma fue una sola linea de log --"Sin cliente de pywhats; se omite
    el backfill"-- y la excavacion no mandaba NI UNA peticion de historial. El
    boton respondia, la fase corria, y no hacia nada.
    """
    cliente = _cliente(settings, tmp_path)
    sesion = cliente._client

    assert sesion is not None, "sin esto la excavacion se salta entera"
    for nombre in ("device", "_sender", "_app_state_syncer"):
        assert hasattr(sesion, nombre), nombre


def test_la_sesion_es_SIEMPRE_la_misma(settings, tmp_path):
    """`backfill._client` se guarda: dos fachadas serian dos verdades."""
    cliente = _cliente(settings, tmp_path)
    assert cliente._client is cliente._client


def test_el_backfill_no_se_salta_por_falta_de_cliente():
    """La fase tiene que mirar `_client`, y `_client` tiene que existir.

    Se comprueban las dos mitades juntas a proposito: cada una por separado
    parece correcta y el fallo solo aparece al juntarlas.
    """
    import inspect

    from app.services.sync_job import SyncJob
    from app.wa.baileys_client import BaileysClient

    fuente = inspect.getsource(SyncJob._fase_backfill)
    assert '"_client"' in fuente, "la fase resuelve la sesion por ese nombre"
    assert isinstance(
        inspect.getattr_static(BaileysClient, "_client"), property
    ), "y el proveedor tiene que ofrecerlo"


def test_la_sesion_ofrece_los_MISMOS_nombres_que_pywhats():
    """`orchestrator` y los servicios leen `_sender` y `_app_state_syncer`.

    Eran privados de pywhats, y por eso se replican con el mismo nombre: es lo
    que permite que `post_connect`, el backfill, los contactos y el multimedia
    funcionen sin tocar una linea.
    """
    from app.wa.baileys_client import SesionBaileys

    sesion = SesionBaileys(_WorkerFalso())
    assert hasattr(sesion, "_sender")
    assert hasattr(sesion._sender, "send_message")
    assert hasattr(sesion._sender, "_fetch_devices")
    assert hasattr(sesion, "_app_state_syncer")
    assert hasattr(sesion._app_state_syncer, "fetch")


def test_la_peticion_de_historial_se_construye_en_PYTHON_y_solo_se_traduce():
    """El ancla en segundos, el `accountLid` y la tanda siguen siendo nuestros.

    `build_on_demand_message` lleva dentro todas las lecciones que costaron
    dias. La fachada solo lee sus campos y los manda al worker.
    """
    import asyncio

    from app.services.backfill_service import build_on_demand_message
    from app.wa.baileys_client import SesionBaileys

    mensaje = build_on_demand_message(
        chat_jid="a@s.whatsapp.net",
        oldest_message_id="WAMID1",
        oldest_from_me=False,
        oldest_timestamp=1_700_000_000,
        count=500,
        account_lid="99887766@lid",
    )

    worker = _WorkerFalso({"pedir_historial": {"request_id": "R1"}})
    sesion = SesionBaileys(worker)
    enviado = asyncio.run(sesion._sender.send_message("destino", mensaje))

    cmd, datos = worker.pedidos[0]
    assert cmd == "pedir_historial"
    assert datos["chat_jid"] == "a@s.whatsapp.net"
    assert datos["message_id"] == "WAMID1"
    # LA TANDA ENTERA. `fetchMessageHistory` no tiene tope en el codigo: mete
    # el count tal cual en el PDO (Socket/messages-recv.js:707).
    assert datos["count"] == 500
    # EN SEGUNDOS, pese a que el campo se llame `...MS`.
    assert datos["timestamp"] == 1_700_000_000
    assert datos["account_lid"] == "99887766@lid"
    assert enviado.id == "R1"


def test_por_el_emisor_NO_puede_salir_un_mensaje_de_chat():
    """La unica cosa que se envia es una peticion a nuestro propio telefono."""
    import asyncio

    from app.wa.baileys_client import SesionBaileys

    sesion = SesionBaileys(_WorkerFalso())

    class _CualquierOtro:
        pass

    with pytest.raises(RuntimeError, match="solo lectura"):
        asyncio.run(sesion._sender.send_message("a@s.whatsapp.net", _CualquierOtro()))


def test_el_usync_devuelve_un_DICCIONARIO_como_espera_el_consumidor():
    """`resolve_lids_via_usync` lo recorre con `.items()`.

    Devolver una lista habria hecho que el bucle no encontrara nada y los LIDs
    se quedaran sin resolver -- en silencio, que es lo peor.
    """
    import asyncio

    from app.wa.baileys_client import SesionBaileys

    worker = _WorkerFalso(
        {
            "resolver_lids": {
                "resultados": [
                    {
                        "jid": "573001234567@s.whatsapp.net",
                        "lid": "99887766@lid",
                        "existe": True,
                    },
                    {"jid": "573009999999@s.whatsapp.net", "lid": None, "existe": False},
                ]
            }
        }
    )
    sesion = SesionBaileys(worker)
    salida = asyncio.run(sesion._sender._fetch_devices(["573001234567@s.whatsapp.net"]))

    assert isinstance(salida, dict)
    ((jid, entrada),) = salida.items()
    assert jid.user == "573001234567"
    assert entrada.lid.user == "99887766"


def test_el_appstate_pide_las_colecciones_que_traen_nombres():
    import asyncio

    from app.services.contacts_service import CONTACT_COLLECTIONS
    from app.wa.baileys_client import SesionBaileys

    worker = _WorkerFalso({"resync_appstate": {"colecciones": ["critical_unblock_low"]}})
    sesion = SesionBaileys(worker)
    asyncio.run(sesion._app_state_syncer.fetch("critical_unblock_low", full_sync=True))

    cmd, datos = worker.pedidos[0]
    assert cmd == "resync_appstate"
    assert datos["colecciones"] == ["critical_unblock_low"]
    assert datos["completo"] is True
    assert "critical_unblock_low" in CONTACT_COLLECTIONS


def test_el_cliente_expone_el_loop_que_busca_el_sync_job():
    """`sync_job.start()` hace `run_coroutine_threadsafe(..., client._loop)`.

    El nombre empieza por guion bajo pero es contrato de hecho: sin el, pulsar
    "excavar" no lanza nada y no da error.
    """
    import inspect

    from app.services.sync_job import SyncJob
    from app.wa.baileys_client import BaileysClient

    assert "_loop" in inspect.getsource(SyncJob.start)
    assert "_loop" in inspect.getsource(BaileysClient.__init__)


def test_post_connect_se_lanza_al_conectar_y_UNA_sola_vez(settings, tmp_path):
    """Y con el loop de verdad: sin loop no se puede programar la corrutina."""
    import threading

    cliente = _cliente(settings, tmp_path)
    cliente._arrancar_loop()
    llamadas = threading.Semaphore(0)

    async def _falso(sesion):
        llamadas.release()

    cliente.post_connect = _falso
    try:
        cliente._reenviar({"name": "connected", "payload": {}})
        cliente._reenviar({"name": "connected", "payload": {}})

        assert cliente._post_connect_lanzado is True
        assert llamadas.acquire(timeout=5.0), "post_connect no llego a correr"
        # La segunda conexion NO vuelve a lanzarlo.
        assert llamadas.acquire(timeout=0.5) is False
    finally:
        bucle = cliente._loop
        if bucle is not None:
            bucle.call_soon_threadsafe(bucle.stop)


def test_sin_event_loop_post_connect_no_se_da_por_lanzado(settings, tmp_path):
    """Decir que corrio cuando no pudo seria peor que no correr."""
    cliente = _cliente(settings, tmp_path)

    async def _falso(sesion):
        return None

    cliente.post_connect = _falso
    cliente._reenviar({"name": "connected", "payload": {}})

    assert cliente._post_connect_lanzado is False


def test_al_reconectar_post_connect_vuelve_a_correr(settings, tmp_path):
    """Mientras el socket estuvo muerto no llego ni un evento: nada de ese
    rato se puede dar por recibido."""
    cliente = _cliente(settings, tmp_path)

    async def _falso(sesion):
        return None

    cliente.post_connect = _falso
    cliente._arrancar_loop()
    try:
        cliente._reenviar({"name": "connected", "payload": {}})
        assert cliente._post_connect_lanzado is True

        cliente._reenviar({"name": "disconnected", "payload": {}})
        assert cliente._post_connect_lanzado is False
    finally:
        bucle = cliente._loop
        if bucle is not None:
            bucle.call_soon_threadsafe(bucle.stop)


def test_el_par_lid_pasa_por_el_sink_no_solo_por_la_cola(settings, tmp_path):
    """Publicarlo a secas lo dejaria en la cola de la pantalla, que no hace
    nada con el. Quien lo escribe en `contacts.lid` es el sink."""
    cliente = _cliente(settings, tmp_path)
    vistos = []
    cliente.sinks["lid_pair"] = lambda carga: vistos.append(carga)

    cliente._guardar_par_lid("99887766@lid", "573001234567@s.whatsapp.net")

    assert len(vistos) == 1


def test_un_par_incompleto_no_se_anota(settings, tmp_path):
    cliente = _cliente(settings, tmp_path)
    vistos = []
    cliente.sinks["lid_pair"] = lambda carga: vistos.append(carga)

    cliente._guardar_par_lid("99887766@lid", None)
    cliente._guardar_par_lid(None, "573001234567@s.whatsapp.net")

    assert vistos == []


def test_guardar_par_lid_solo_rellena_huecos(session, cuenta):
    """No pisa un LID ya conocido, y no toca nada mas que esa columna."""
    import uuid as _uuid
    from contextlib import contextmanager

    from app.models import Contact
    from app.services.contacts_service import guardar_par_lid

    class _Db:
        def transaction(self):
            @contextmanager
            def scope():
                yield session
                session.flush()

            return scope()

    pn = f"{_uuid.uuid4().int % 10**12}@s.whatsapp.net"
    session.add(Contact(jid=pn, whatsapp_account_id=cuenta.id, push_name="Marta"))
    session.flush()

    assert guardar_par_lid(_Db(), "111@lid", pn) is True
    session.expire_all()
    fila = session.execute(Contact.__table__.select().where(Contact.jid == pn)).one()
    assert fila.lid == "111@lid"
    assert fila.push_name == "Marta", "no puede tocar el nombre"

    # Ya tiene LID: no se pisa.
    assert guardar_par_lid(_Db(), "222@lid", pn) is False


def test_guardar_par_lid_rechaza_lo_que_no_es_un_par():
    """Un PN en el hueco del LID corromperia la columna."""
    from app.services.contacts_service import guardar_par_lid

    assert guardar_par_lid(None, "573001234567@s.whatsapp.net", "111@lid") is False
    assert guardar_par_lid(None, "", "") is False


# ---------------------------------------------------------------------------
# G1: la stanza cruda
# ---------------------------------------------------------------------------


def test_el_worker_manda_la_peticion_por_la_via_peer():
    """`sendPeerDataOperationMessage` envia al propio telefono con
    `category: 'peer'`. Sin ese atributo el servidor confirma y descarta."""
    fuente = Path("wa_baileys/worker.js").read_text(encoding="utf-8")
    assert "sendPeerDataOperationMessage" in fuente
    assert "HISTORY_SYNC_ON_DEMAND" in fuente
    # Y ya NO por el atajo, que no deja anadir `accountLid`.
    assert "sock.fetchMessageHistory(" not in fuente


def test_el_worker_ya_no_se_impone_un_tope_de_50():
    """El tope de 50 era de la documentacion, no del codigo.

    `fetchMessageHistory` mete el `count` tal cual en `onDemandMsgCount`
    (Socket/messages-recv.js:707). El recorte lo estaba haciendo este worker.
    """
    fuente = Path("wa_baileys/worker.js").read_text(encoding="utf-8")
    assert "TOPE_DE_BAILEYS" not in fuente
    assert "Math.min(TANDA_PEDIDA" not in fuente
