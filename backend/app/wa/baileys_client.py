"""La capa de WhatsApp sobre Baileys, vista desde Python.

QUE ES
------
Un ``ClienteDeWhatsApp`` --el mismo contrato que ``WhatsAppClient``-- que en
vez de hablar el protocolo desde Python lanza un proceso hijo de Node y le
habla JSON Lines por las tuberias. Emite los MISMOS ``ClientEvent``, asi que
la ingesta, los cursores, Drive, la API y las pruebas no cambian.

La forma esta copiada de ``app/web_companion/supervisor.py``, que lleva tiempo
en produccion haciendo exactamente esto: sin puertos, sin segundo servidor
HTTP, ``stdout`` solo protocolo y ``stderr`` al log.

LA DECISION QUE HACE QUIRURGICA LA MIGRACION
--------------------------------------------
Del worker no llega un mensaje "a la manera de Baileys": llega el
``WebMessageInfo`` en crudo. Aqui se pasa por ``parse_web_message_info``, que
es el MISMO normalizador que usa el historial. Es decir: hay un solo sitio en
todo el proyecto donde se decide que es un mensaje, y sigue siendo nuestro.

Normalizar en JavaScript habria creado un segundo criterio que tarde o
temprano discrepa del de Python -- y esas discrepancias no dan error, dan
mensajes que faltan.

AISLAMIENTO POR CUENTA
----------------------
Gratis, y por el mismo mecanismo que ya existe: cada ``AppRuntime`` recibe unos
``Settings`` cuyo ``session_dir`` es la carpeta de SU cuenta
(``app/core/session_paths.py``). La sesion de Baileys cuelga de ahi, asi que
dos cuentas no comparten ni credenciales ni almacen de Signal.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.logging_setup import get_logger
from app.wa.historial import FullHistorySync, HistoryConversation
from app.wa.tipos import (
    ClientEvent,
    Identidad,
    JID,
    MediaInfo,
    jid_desde,
)

log = get_logger("WA")

#: Cuanto se espera una respuesta del worker por defecto.
#:
#: Las peticiones de historial NO usan esto: su plazo es el del proyecto (180 s,
#: medido -- las respuestas tardan ~90 s) y lo gobierna el backfill.
ESPERA_RPC = 30.0


# ---------------------------------------------------------------------------
# Objetos con la forma que espera el resto del sistema
# ---------------------------------------------------------------------------


@dataclass
class MensajeEnVivo:
    """La carga del evento ``message``, con los nombres de siempre.

    ``media`` va SIEMPRE a ``None`` a proposito: el adjunto lo detecta nuestro
    parser desde ``raw_proto``, que es el camino que ya usaban los mensajes
    salientes y el que registra ``_register_parsed_media``. Un solo camino en
    vez de dos que hay que mantener de acuerdo.
    """

    id: str | None
    chat: JID | None
    sender: JID | None
    text: str | None
    timestamp: int
    from_me: bool = False
    media: Any = None
    quoted: Any = None
    push_name: str | None = None


# `ConversacionDelHistorial` y `HistorialCompleto` vivian aqui, como copia
# letra por letra de `HistoryConversation`/`FullHistorySync`
# (``app/wa/historial.py``). Se unificaron: el parseo del JSON de Baileys usa
# ahora las MISMAS clases que el protobuf de antes, porque hace falta desde
# dos sitios que no pueden discrepar -- la llegada en vivo y la recuperacion
# de blobs archivados (``blob_reingest``). Ver ``historial.parse_full_json``.


# ---------------------------------------------------------------------------
# La sesion que reciben los servicios en post_connect
# ---------------------------------------------------------------------------


class _EmisorBaileys:
    """La fachada de ``client._sender``, para que el backfill no cambie.

    POR QUE ESTO EN VEZ DE REESCRIBIR EL BACKFILL
    ---------------------------------------------
    ``backfill_service`` construye la peticion de historial con
    ``build_on_demand_message()`` --el ancla en SEGUNDOS, el ``accountLid``, el
    tamano de tanda-- y la manda con ``sender.send_message(destino, mensaje)``.
    Ahi estan metidas todas las lecciones que costaron dias.

    Reescribir eso para Baileys habria sido volver a aprenderlas. Asi que la
    peticion se sigue construyendo en Python, EXACTAMENTE igual, y aqui solo se
    traduce la llamada: se leen los campos del protobuf y se mandan al worker,
    que los pone en una stanza ``category="peer"``.
    """

    def __init__(self, cliente: "BaileysClient") -> None:
        self._cliente = cliente

    async def send_message(self, destino: Any, mensaje: Any) -> Any:
        """La peticion de historial, por la via de Baileys.

        Solo acepta peticiones de historial. Cualquier otro mensaje se
        rechaza: esto es una copia de seguridad y no manda nada a nadie.
        """
        import asyncio

        peticion = _peticion_de_historial(mensaje)
        if peticion is None:
            raise RuntimeError(
                "este proveedor solo envia peticiones de historial: "
                "el producto es de solo lectura"
            )

        datos = await asyncio.to_thread(
            self._cliente.pedir, "pedir_historial", peticion, 60.0
        )
        # El backfill lee `.id` de lo que devuelve el envio para correlacionar.
        return type("Enviado", (), {"id": datos.get("request_id")})()

    async def _fetch_devices(self, usuarios: Any) -> dict[Any, Any]:
        """usync: resuelve numeros y, de paso, su LID. (C-31)

        pywhats ya traia el LID en la respuesta y no lo guardaba en ningun
        sitio. Aqui se devuelve con la misma forma que espera
        ``resolve_lids_via_usync``: objetos con ``.jid`` y ``.lid``.
        """
        import asyncio

        numeros = [str(u) for u in (usuarios or [])]
        if not numeros:
            return {}
        datos = await asyncio.to_thread(
            self._cliente.pedir, "resolver_lids", {"numeros": numeros}, 60.0
        )
        # UN DICCIONARIO {JID: entrada}, no una lista: es la forma que recorre
        # `resolve_lids_via_usync` con `.items()`. Devolver una lista habria
        # hecho que el bucle no encontrara nada y los LIDs se quedaran sin
        # resolver -- en silencio, que es lo peor de todo.
        salida: dict[Any, Any] = {}
        for fila in datos.get("resultados") or []:
            if not fila.get("existe"):
                continue
            jid = jid_desde(fila.get("jid"))
            lid = jid_desde(fila.get("lid"))
            if jid is None or lid is None:
                continue
            salida[jid] = _Objeto({}, lid=lid)
        return salida


class _AppStateBaileys:
    """La fachada de ``client._app_state_syncer``. (C-30)

    ``fetch_contact_names`` pide las colecciones que traen nombres. Con
    pywhats habia que pedirlas a mano porque solo sincronizaba de forma
    reactiva; con Baileys hace falta igual, porque el barrido inicial no
    garantiza estas dos.
    """

    def __init__(self, cliente: "BaileysClient") -> None:
        self._cliente = cliente

    async def fetch(self, collection: str, *, full_sync: bool = False) -> list[Any]:
        import asyncio

        datos = await asyncio.to_thread(
            self._cliente.pedir,
            "resync_appstate",
            {"colecciones": [collection], "completo": bool(full_sync)},
            120.0,
        )
        # Se devuelve una lista para que el contador de mutaciones del
        # llamante siga teniendo sentido; Baileys no dice cuantas fueron, asi
        # que se informa lo que se sabe: que la coleccion se pidio.
        return list(datos.get("colecciones") or [])


def _peticion_de_historial(mensaje: Any) -> dict[str, Any] | None:
    """Saca los campos de la peticion ON_DEMAND del protobuf que armo Python.

    HAY QUE RE-PARSEAR, Y ESA ES LA PARTE QUE NO SE VE
    --------------------------------------------------
    ``build_on_demand_message`` arma la peticion con NUESTRO descriptor
    (``app/models/proto``) y la reparsea con el ``Message`` de pywhats, porque
    es el tipo que aquella libreria sabe enviar. Pero el proto de pywhats **no
    define** ``peerDataOperationRequestMessage``: protobuf conserva los campos
    que no conoce como bytes opacos, asi que leerlos con ``getattr`` devuelve
    siempre nada.

    Por eso se vuelve a parsear con el descriptor que SI los declara. Los
    bytes son los mismos --el viaje de ida y vuelta es identico byte a byte--
    y asi la peticion se sigue construyendo en un solo sitio.

    Devuelve ``None`` si el mensaje no es una peticion de historial, y eso es
    lo que impide que por aqui salga cualquier otra cosa.
    """
    try:
        crudo = mensaje.SerializeToString()
    except Exception:  # noqa: BLE001 - lo que no es un protobuf no se envia
        return None

    try:
        from app.models.proto import OnDemandMessage

        interpretado = OnDemandMessage()
        interpretado.ParseFromString(crudo)
    except Exception:  # noqa: BLE001
        return None

    peticion = (
        interpretado.protocolMessage.peerDataOperationRequestMessage
        .historySyncOnDemandRequest
    )
    if not peticion.chatJID:
        return None

    return {
        "chat_jid": peticion.chatJID,
        "message_id": peticion.oldestMsgID,
        "from_me": bool(peticion.oldestMsgFromMe),
        # EN SEGUNDOS. El campo se llama `...MS` y aun asi el telefono espera
        # segundos; se pasa tal cual lo dejo `build_on_demand_message`, que es
        # donde vive esa leccion.
        "timestamp": int(peticion.oldestMsgTimestampMS or 0),
        "count": int(peticion.onDemandMsgCount or 0),
        "account_lid": peticion.accountLid or None,
    }


class SesionBaileys:
    """Lo que hoy es el ``pywhats.Client`` crudo, hablando con el worker.

    Solo expone lo que el proyecto usa de verdad, y con los MISMOS nombres --
    incluidos los que en pywhats eran privados (``_sender``,
    ``_app_state_syncer``). No es capricho: asi ``orchestrator``,
    ``backfill_service``, ``contacts_service`` y ``media_service`` funcionan
    sin tocar una linea.

    De envio solo hay una cosa, y es lectura disfrazada: la peticion de
    historial, que es un mensaje a NUESTRO propio telefono pidiendole datos.
    No se puede mandar nada a nadie mas.
    """

    def __init__(self, cliente: "BaileysClient") -> None:
        self._cliente = cliente
        self._sender = _EmisorBaileys(cliente)
        self._app_state_syncer = _AppStateBaileys(cliente)

    @property
    def device(self) -> Identidad:
        return self._cliente.device

    async def get_group_info(self, jid: Any) -> Any:
        """El asunto del grupo. El ritmo (5 y 0,4 s) lo pone quien llama."""
        import asyncio

        texto = jid if isinstance(jid, str) else str(jid)
        datos = await asyncio.to_thread(
            self._cliente.pedir, "info_de_grupo", {"jid": texto}
        )
        return type("GroupInfo", (), {"subject": datos.get("subject")})()

    async def download_media(self, info: Any) -> bytes:
        """Descarga un adjunto. El binario NO viaja por el canal.

        Node lo escribe a disco y devuelve la ruta; aqui se lee y se borra. Un
        adjunto de 30 MB dentro de una linea JSON seria una linea de 40 MB.
        """
        import asyncio

        crudo = getattr(info, "raw_proto", None)
        if not crudo:
            raise ValueError("descarga sin el protobuf del mensaje")
        destino = self._cliente.carpeta_temporal / f"{uuid.uuid4().hex}.bin"
        try:
            await asyncio.to_thread(
                self._cliente.pedir,
                "descargar_media",
                {
                    "raw_proto": base64.b64encode(bytes(crudo)).decode("ascii"),
                    "destino": str(destino),
                },
                180.0,
            )
            return destino.read_bytes()
        finally:
            try:
                destino.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - limpiar no puede fallar el flujo
                log.debug("No se pudo borrar el temporal de descarga")


# ---------------------------------------------------------------------------
# El cliente
# ---------------------------------------------------------------------------


class BaileysClient:
    """Arranca el worker de Node y traduce lo que dice a ``ClientEvent``."""

    def __init__(self, settings: Any, events: Any) -> None:
        self._settings = settings
        self._events = events
        self._proceso: subprocess.Popen[str] | None = None
        self._candado = threading.Lock()
        self._parando = threading.Event()
        self._identidad = Identidad()
        #: Respuestas de las ordenes en vuelo, por identificador.
        self._respuestas: dict[str, queue.Queue] = {}

        #: EL EVENT LOOP DEL CLIENTE.
        #:
        #: Con pywhats lo creaba el hilo del cliente; aqui el protocolo vive
        #: en Node, asi que hace falta uno igualmente. No es un detalle
        #: interno: `sync_job.start()` hace
        #: `run_coroutine_threadsafe(..., runtime.client._loop)`, y el
        #: backfill y el worker de multimedia necesitan ESE loop para no
        #: bloquear la recepcion. El nombre `_loop` es parte del contrato de
        #: hecho, aunque empiece por guion bajo.
        self._loop: Any = None
        self._hilo_loop: threading.Thread | None = None
        #: `post_connect` corre una vez por conexion, no una por evento.
        self._post_connect_lanzado = False

        # El mismo contrato que `WhatsAppClient`.
        self.post_connect: Any = None
        self.sinks: dict[str, Any] = {}
        self.on_shutdown: Any = None
        #: La sesion que ven los servicios. Se crea una sola vez y se
        #: reutiliza: `backfill._client` la guarda, y dos fachadas distintas
        #: sobre el mismo worker serian dos cosas que hay que mantener de
        #: acuerdo sin ninguna ventaja.
        self._sesion: Any = None

    @property
    def _client(self) -> Any:
        """La sesion viva, con el nombre que usa el resto del proyecto.

        NO ES DECORACION, y costo caro descubrirlo. Con pywhats esto era el
        ``Client`` crudo, y SEIS sitios lo leen con
        ``getattr(runtime.client, "_client", None)``: el backfill, los bordes
        perdidos, la auto-recuperacion, el diagnostico y la comprobacion de
        principal.

        Al migrar se quedo sin definir y todos vieron ``None``. El unico
        sintoma era una linea en el log --"Sin cliente de pywhats; se omite el
        backfill"-- y la excavacion no pedia NI UNA peticion de historial. El
        boton funcionaba, la fase corria, y no hacia nada.

        Devuelve la MISMA fachada que recibe ``post_connect``, con
        ``device``, ``_sender`` y ``_app_state_syncer``.
        """
        if self._sesion is None:
            self._sesion = SesionBaileys(self)
        return self._sesion

    # -- Rutas ---------------------------------------------------------------

    @property
    def raiz_del_worker(self) -> Path:
        return Path(__file__).resolve().parents[2] / "wa_baileys"

    @property
    def carpeta_de_sesion(self) -> Path:
        """LA UNIDAD INDIVISIBLE: credenciales y almacen de Signal juntos.

        Cuelga de ``session_dir``, que ya es la carpeta de ESTA cuenta, asi
        que el aislamiento entre cuentas viene puesto.

        Borrar media carpeta deja un dispositivo NUEVO usando ratchets VIEJOS:
        el sintoma es ``unknown one-time pre-key id`` y peticiones de historial
        que reciben ACK y despues nada. Se borra entera o no se borra.
        """
        return Path(self._settings.session_dir) / "baileys"

    @property
    def carpeta_temporal(self) -> Path:
        destino = Path(self._settings.data_dir) / "tmp"
        destino.mkdir(parents=True, exist_ok=True)
        return destino

    # -- Estado --------------------------------------------------------------

    @property
    def session_exists(self) -> bool:
        """``True`` si hay credenciales reutilizables de Baileys."""
        return (self.carpeta_de_sesion / "creds.json").is_file()

    @property
    def device(self) -> Identidad:
        return self._identidad

    @property
    def vivo(self) -> bool:
        return self._proceso is not None and self._proceso.poll() is None

    # -- Arranque ------------------------------------------------------------

    def start(self) -> None:
        """Lanza el worker. No bloquea."""
        with self._candado:
            if self.vivo:
                return
            self.carpeta_de_sesion.mkdir(parents=True, exist_ok=True)
            try:
                self._proceso = subprocess.Popen(  # noqa: S603 - orden construida aqui
                    ["node", "worker.js"],
                    cwd=str(self.raiz_del_worker),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=self._entorno(),
                )
            except Exception as exc:  # noqa: BLE001 - se dice y no se tumba nada
                log.error("No se pudo lanzar el worker de Baileys: %s", exc)
                raise

        self._parando.clear()
        self._arrancar_loop()
        for objetivo, nombre in (
            (self._leer_salida, "baileys-out"),
            (self._leer_errores, "baileys-err"),
        ):
            threading.Thread(target=objetivo, name=nombre, daemon=True).start()
        log.info("[WA] worker de Baileys en marcha (pid=%s)", self._proceso.pid)

    def _arrancar_loop(self) -> None:
        """Un event loop propio, en su hilo, para los servicios de fondo."""
        import asyncio

        if self._loop is not None:
            return
        listo = threading.Event()

        def _correr() -> None:
            bucle = asyncio.new_event_loop()
            asyncio.set_event_loop(bucle)
            self._loop = bucle
            listo.set()
            try:
                bucle.run_forever()
            finally:
                bucle.close()

        self._hilo_loop = threading.Thread(target=_correr, name="baileys-loop", daemon=True)
        self._hilo_loop.start()
        # Se espera a que exista: quien conecte justo despues va a programar
        # `post_connect` sobre el, y sin loop la corrutina se perderia.
        listo.wait(timeout=5.0)

    def _entorno(self) -> dict[str, str]:
        entorno = dict(os.environ)
        entorno["WA_BAILEYS_SESSION_DIR"] = str(self.carpeta_de_sesion)
        # Los lotes de historial se archivan CRUDOS antes de interpretarlos.
        # Es lo que salvo 5920 mensajes cuando la base se vacio.
        entorno["WA_BAILEYS_HISTORY_DIR"] = str(Path(self._settings.data_dir) / "history_baileys")
        entorno["WA_BAILEYS_ONDEMAND_COUNT"] = str(
            getattr(self._settings, "history_on_demand_count", 50)
        )
        version = self._version_a_anunciar()
        if version:
            entorno["WA_BAILEYS_VERSION"] = version
        return entorno

    def _version_a_anunciar(self) -> str | None:
        """La version de WhatsApp Web, resuelta en vivo. Nunca bloquea.

        Nuestro resolutor lee ``client_revision`` del ``sw.js`` de verdad y
        encuentra versiones MAS NUEVAS que ``fetchLatestBaileysVersion()``
        (2.3000.1047094411 frente a 2.3000.1043857760). Por eso se conserva.

        Pero no es critico y no puede serlo: se midio que el servidor se
        comporta igual con una y con otra. Si la red falla, se sigue con la
        version compilada en la libreria en vez de dejar al usuario sin
        vincular por un ``sw.js`` que no respondio.
        """
        fijada = (os.environ.get("WA_BAILEYS_VERSION") or "").strip()
        if fijada:
            return fijada
        try:
            from app.wa.version import resolve

            version, origen = resolve(
                Path(self._settings.wa_version_cache),
                timeout=getattr(self._settings, "wa_version_fetch_timeout", 10.0),
            )
            log.debug("[WA] version de WhatsApp Web %s (%s)", version, origen)
            return ".".join(map(str, version))
        except Exception:  # noqa: BLE001 - la version no puede impedir vincular
            log.debug("[WA] no se pudo resolver la version; se usa la de la libreria")
            return None

    # -- Lectura de las tuberias ---------------------------------------------

    def _leer_salida(self) -> None:
        """``stdout``: protocolo. Una linea, un evento."""
        proceso = self._proceso
        if proceso is None or proceso.stdout is None:
            return
        for linea in proceso.stdout:
            texto = linea.strip()
            if not texto:
                continue
            try:
                mensaje = json.loads(texto)
            except json.JSONDecodeError:
                # Algo escribio texto suelto en stdout. Se anota y se sigue: el
                # canal se recupera en la siguiente linea.
                log.debug("[WA] linea no interpretable del worker")
                continue
            try:
                self._procesar(mensaje)
            except Exception:  # noqa: BLE001 - un evento roto no para el canal
                log.exception("[WA] fallo procesando un evento del worker")

    def _leer_errores(self) -> None:
        """``stderr``: lo legible. Va al log del proyecto."""
        proceso = self._proceso
        if proceso is None or proceso.stderr is None:
            return
        for linea in proceso.stderr:
            texto = linea.strip()
            if texto:
                log.debug("%s", texto[:300])

    def _procesar(self, mensaje: dict[str, Any]) -> None:
        tipo = mensaje.get("event")

        if tipo == "client":
            self._reenviar(mensaje)
        elif tipo == "respuesta":
            self._resolver(mensaje)
        elif tipo == "device":
            self._identidad = Identidad(
                jid=jid_desde(mensaje.get("jid")),
                lid=mensaje.get("lid"),
                device_id=str(mensaje.get("device_id") or ""),
                registration_id=str(mensaje.get("registration_id") or ""),
            )
        elif tipo == "lid_par":
            self._guardar_par_lid(mensaje.get("lid"), mensaje.get("pn"))
        elif tipo == "fatal":
            log.error(
                "[WA] el worker de Baileys no puede arrancar (%s): %s",
                mensaje.get("code"),
                mensaje.get("message"),
            )

    # -- Traduccion a ClientEvent --------------------------------------------

    def _reenviar(self, mensaje: dict[str, Any]) -> None:
        """Un evento del worker -> el mismo ``ClientEvent`` de siempre."""
        nombre = mensaje.get("name") or ""
        carga = self._carga_de(nombre, mensaje.get("payload"))
        extra = mensaje.get("extra") or {}

        # Donde quedo archivado el blob. El worker lo guarda en disco ANTES de
        # interpretarlo y dice donde; sin esto la ruta se perdia y el aviso de
        # la ingesta no podia decir de que fichero venia -- justo el dato que
        # hizo falta para recuperar 5920 mensajes cuando la base se vacio.
        if nombre == "history_sync" and carga is not None and extra.get("archivo"):
            try:
                carga.blob_path = Path(str(extra["archivo"]))
            except Exception:  # noqa: BLE001 - una ruta rara no tira el lote
                pass

        sink = self.sinks.get(nombre)
        if sink is not None:
            try:
                resultado = sink(carga)
                if resultado is not None:
                    self._publicar(f"{nombre}_stored", resultado)
            except Exception:  # noqa: BLE001 - el receptor manda
                log.exception("El sink de %r fallo; se sigue escuchando", nombre)

        self._publicar(nombre, carga, **extra)

        # La secuencia completa de arranque va DESPUES de publicar `connected`:
        # asi la pantalla ya sabe que hay sesion mientras se sincroniza.
        if nombre == "connected":
            self._lanzar_post_connect()
        elif nombre in ("disconnected", "logged_out"):
            # Al reconectar se vuelve a ejecutar: mientras el socket estuvo
            # muerto no llego ni un evento, asi que nada de ese rato se puede
            # dar por recibido.
            self._post_connect_lanzado = False

    def _lanzar_post_connect(self) -> None:
        """Enchufa la sesion a los servicios de siempre. Nunca lanza."""
        import asyncio

        if self.post_connect is None or self._post_connect_lanzado:
            return
        if self._loop is None:
            log.warning("[WA] no hay event loop: post_connect no se puede lanzar")
            return
        self._post_connect_lanzado = True

        async def _correr() -> None:
            try:
                await self.post_connect(self._client)
            except Exception:  # noqa: BLE001 - la sesion sigue viva aunque falle
                log.exception("[WA] post_connect fallo")

        asyncio.run_coroutine_threadsafe(_correr(), self._loop)

    def _carga_de(self, nombre: str, crudo: Any) -> Any:
        """Convierte la carga del canal en el objeto que espera cada consumidor."""
        if nombre == "qr":
            # La CADENA pelada, no el diccionario que la envuelve.
            #
            # `PairingManager.note_qr` y `render_qr` reciben esto tal cual y lo
            # meten en el generador de imagenes. Con un diccionario dentro, el
            # QR se dibuja igual --pero codificando `{'qr': '2@...'}`-- y el
            # telefono no lo reconoce: un codigo perfectamente valido que no
            # vincula nada.
            if isinstance(crudo, dict):
                return crudo.get("qr")
            return crudo
        if nombre == "message":
            return self._mensaje(crudo)
        if nombre == "history_sync":
            return self._historial(crudo)
        if nombre in ("contact", "pushname", "mute", "pin", "archive"):
            return _Objeto(crudo or {}, jid=jid_desde((crudo or {}).get("jid")))
        if nombre in ("receipt", "reaction", "message_edit", "message_revoke"):
            return _Objeto(crudo or {})
        return crudo

    def _mensaje(self, crudo: dict[str, Any] | None) -> MensajeEnVivo | None:
        """El evento ``message``, normalizado por NUESTRO parser.

        El texto y el adjunto salen del protobuf, no de lo que diga Baileys:
        asi el mensaje en vivo y el del historial pasan por el mismo sitio.
        """
        if not crudo:
            return None
        bruto = crudo.get("raw_proto")
        texto = None
        datos = None
        if bruto:
            try:
                datos = base64.b64decode(bruto, validate=True)
            except Exception:  # noqa: BLE001 - sin bytes se pierde clasificacion, no el mensaje
                datos = None
        if datos:
            # El mismo hueco del que lee `live_service`. Sin esto no habria
            # `raw_proto` para clasificar ni para detectar el adjunto.
            _registrar_crudo(datos)
            from app.core.message_parser import parse_web_message_info

            analizado = parse_web_message_info(datos)
            if analizado is not None:
                texto = analizado.text
        else:
            _registrar_crudo(None)

        return MensajeEnVivo(
            id=crudo.get("id"),
            chat=jid_desde(crudo.get("chat")),
            sender=jid_desde(crudo.get("sender")),
            text=texto,
            timestamp=int(crudo.get("timestamp") or 0),
            from_me=bool(crudo.get("from_me")),
            push_name=crudo.get("push_name"),
        )

    def _historial(self, crudo: dict[str, Any] | None) -> FullHistorySync | None:
        """El lote de historial con la forma que espera ``ingest_history_sync``.

        El parseo vive en ``app.wa.historial.parse_full_json``: es el mismo
        que usa la recuperacion de blobs archivados, y las dos rutas tienen
        que entender el JSON exactamente igual.
        """
        if not crudo:
            return None
        from app.wa.historial import parse_full_json

        return parse_full_json(crudo)

    def _guardar_par_lid(self, lid: Any, pn: Any) -> None:
        """El par PN<->LID que viene en los mensajes. (C-23)

        Se publica como evento para que lo recoja quien corresponda. NO se
        escribe aqui en la base: este objeto no sabe de que cuenta es, y
        adivinarlo seria escribir el contacto de otra persona.
        """
        if not lid or not pn:
            return
        # Por `_reenviar` y no por `_publicar`: asi pasa por el sink, que es
        # quien lo escribe en `contacts.lid`. Publicarlo a secas lo habria
        # dejado en la cola de la pantalla, que no hace nada con el.
        self._reenviar({"name": "lid_pair", "payload": {"lid": str(lid), "pn": str(pn)}})

    def _publicar(self, nombre: str, carga: Any, **extra: Any) -> None:
        try:
            self._events.put_nowait(ClientEvent(name=nombre, payload=carga, extra=extra))
        except Exception:  # noqa: BLE001 - una cola llena no tumba el receptor
            log.warning("[WA] no se pudo encolar el evento %s", nombre)

    # -- Ordenes hacia el worker ---------------------------------------------

    def pedir(self, cmd: str, datos: dict[str, Any], espera: float = ESPERA_RPC) -> dict[str, Any]:
        """Manda una orden y espera su respuesta. Sincrono, para hilos."""
        proceso = self._proceso
        if proceso is None or proceso.stdin is None or not self.vivo:
            raise RuntimeError("el worker de Baileys no esta en marcha")

        identificador = uuid.uuid4().hex[:12]
        buzon: queue.Queue = queue.Queue(maxsize=1)
        self._respuestas[identificador] = buzon
        try:
            linea = json.dumps({"id": identificador, "cmd": cmd, **datos}) + "\n"
            with self._candado:
                proceso.stdin.write(linea)
                proceso.stdin.flush()
            try:
                respuesta = buzon.get(timeout=espera)
            except queue.Empty:
                raise TimeoutError(f"el worker no contesto a {cmd} en {espera}s") from None
        finally:
            self._respuestas.pop(identificador, None)

        if not respuesta.get("ok"):
            raise RuntimeError(respuesta.get("error") or f"{cmd} fallo")
        return respuesta.get("data") or {}

    def _resolver(self, mensaje: dict[str, Any]) -> None:
        buzon = self._respuestas.get(str(mensaje.get("id")))
        if buzon is None:
            return
        try:
            buzon.put_nowait(mensaje)
        except queue.Full:  # pragma: no cover - el buzon es de uno solo
            log.debug("[WA] respuesta duplicada para %s", mensaje.get("id"))

    # -- Parada --------------------------------------------------------------

    def stop(self, timeout: float = 10.0) -> None:
        """Cierra el worker. Nunca lanza."""
        self._parando.set()
        if self.on_shutdown is not None:
            try:
                self.on_shutdown()
            except Exception:  # noqa: BLE001 - avisar no puede impedir cerrar
                log.debug("El aviso de cierre fallo")

        proceso = self._proceso
        if proceso is None:
            return
        try:
            if proceso.stdin is not None:
                proceso.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        limite = time.monotonic() + timeout
        while proceso.poll() is None and time.monotonic() < limite:
            time.sleep(0.1)
        if proceso.poll() is None:
            proceso.kill()
        self._proceso = None

        bucle = self._loop
        if bucle is not None:
            try:
                bucle.call_soon_threadsafe(bucle.stop)
            except Exception:  # noqa: BLE001 - cerrar no puede fallar el cierre
                log.debug("El event loop no acepto la parada")
            self._loop = None

        log.info("[WA] worker de Baileys detenido")


class _Objeto:
    """Un diccionario con acceso por atributo.

    Los consumidores leen ``carga.jid``, ``carga.name``... Se envuelve en vez
    de crear un dataclass por evento porque estas cargas son pequenas y su
    forma la fija el worker, que es donde estan probadas.
    """

    def __init__(self, datos: dict[str, Any], **extra: Any) -> None:
        self.__dict__.update(datos)
        self.__dict__.update({k: v for k, v in extra.items() if v is not None})

    def __repr__(self) -> str:  # pragma: no cover - comodidad al depurar
        return f"_Objeto({self.__dict__})"


def _registrar_crudo(datos: bytes | None) -> None:
    """Deja el protobuf donde ``live_service`` va a buscarlo.

    ``last_raw_message()`` vive en ``app/compat/protocol_flag.py`` porque con
    pywhats hacia falta un parche para capturarlo. Con Baileys llega de serie,
    pero el hueco es el mismo: asi ``live_service`` no cambia ni una linea.
    """
    try:
        from app.wa.crudo import registrar_crudo

        registrar_crudo(datos)
    except Exception:  # noqa: BLE001 - sin esto se pierde clasificacion, no el mensaje
        log.debug("No se pudo registrar el protobuf crudo")
