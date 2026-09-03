"""``AppRuntime``: todo lo que la aplicacion ES, sin ninguna interfaz.

QUE RESUELVE
------------
Antes se cableaban a mano configuracion, base de datos, cliente de WhatsApp,
ingesta, backfill, multimedia y mantenimiento. Con un segundo adaptador (la API
Flask) ese cableado tendria que existir dos veces, y dos copias de un cableado
divergen: se arregla un fallo en una y la otra se queda con el.

Aqui vive UNA vez. Los adaptadores solo lo encienden y escuchan.

    AppRuntime ── Tkinter  (app/gui)
              └── Flask    (app/api)

LO QUE NO SABE
--------------
Ni interfaz, ni Flask, ni HTTP. No importa nada de
``app.api``. Se comunica publicando en el bus de eventos.

DOS MODOS
---------
``start()``      abre la sesion de WhatsApp. Exige el cerrojo.
``start_local()`` solo base de datos, para leer la copia sin tocar la sesion.

El cerrojo es lo que impide que la ventana y la API abran el companion a la
vez y corrompan el estado del protocolo.

ARRANQUE NO BLOQUEANTE
----------------------
``start()`` no espera a que WhatsApp conteste. Lanza la conexion en su hilo y
vuelve enseguida, para que Flask pueda responder ``CONNECTING`` en vez de
quedarse tres minutos sin escuchar en el puerto.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings, load_settings
from app.core.database import Database
from app.core.identity import own_identity, own_jid, session_fingerprint
from app.core.lock import SessionLock, SessionLockedError
from app.core.logging_setup import get_logger, setup_logging
from app.core.pairing import PairingManager
from app.core.session_state import AppState, NEEDS_PAIRING, SessionState
from app.events import EventBus

log = get_logger("APP")


@dataclass
class RuntimeInfo:
    """Resumen del runtime, para ``/health`` y para los logs."""

    owner: str
    connected: bool
    state: str
    database: bool
    session_file: bool
    whatsapp_enabled: bool


class AppRuntime:
    """La aplicacion sin interfaz. La comparten la ventana y la API."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        owner: str = "app",
        configure_logging: bool = True,
    ) -> None:
        self.settings = settings or load_settings()
        self.owner = owner
        if configure_logging:
            setup_logging(
                self.settings.log_level,
                log_file=self.settings.diagnostics_dir / "app.log",
            )
        self.settings.ensure_directories()

        self.bus = EventBus()
        self.state = SessionState()
        self.database: Database | None = None
        self.client: Any = None
        self.orchestrator: Any = None
        self.backfill: Any = None
        self.gate: Any = None
        self.sync_job: Any = None
        # Vigila la aparicion de la PRIMERA ancla de un chat que no la tenia.
        self.seed_recovery: Any = None
        # Y excava el chat que acaba de despertar, solo a el.
        self.seed_queue: Any = None
        # Ciclo de vida del QR. Se crea siempre, aunque la instancia arranque
        # en modo solo lectura: asi ``/session/qr`` responde "no disponible"
        # en vez de reventar.
        self.pairing = PairingManager(
            ttl_seconds=self.settings.pairing_qr_ttl,
            on_renew=self.restart_pairing,
            publish=self.bus.publish,
        )

        self._lock = SessionLock(self.settings.session_dir, owner=owner)
        self._started = False
        self._whatsapp = False
        self._stopping = threading.Event()
        self._observer: threading.Thread | None = None
        # Freno de los reintentos de vinculacion. Sin el, un 401 en bucle
        # produce decenas de intentos por segundo contra los servidores de
        # WhatsApp: se midieron 74 en unos segundos. Eso no es un detalle de
        # eficiencia, es golpear un servicio ajeno.
        self._ultimo_reintento = 0.0
        self._reintentos_seguidos = 0
        # Rechazos seguidos del MISMO device.json. Se cuenta por huella para
        # no confundir "esta sesion no vale" con "hubo un 401 suelto tras
        # vincular otra vez".
        self._huella_rechazada: str | None = None
        self._rechazos_seguidos = 0
        # Mensajes que no se pudieron descifrar. Se cuentan para poder
        # medirlos; NO se tocan aqui: son cosa de Signal y del reintento por
        # receipt, que sigue exactamente como estaba.
        self.decrypt_errors = 0
        # Contadores por ETAPA del pipeline en vivo. Sirven para saber
        # exactamente DONDE muere un mensaje: si el receptor lo vio pero no se
        # persistio, el fallo esta en live_service; si se persistio pero no
        # salio por SSE, esta en la traduccion. Sin esto solo se puede
        # adivinar.
        self.counters: dict[str, int] = {
            "receiver_messages_seen": 0,
            "receiver_decrypt_errors": 0,
            "live_handle_called": 0,
            "live_persisted": 0,
            "live_duplicates": 0,
            # Por DIRECCION. Los salientes se guardaban en el chat propio y el
            # total subia igual, asi que la asimetria no se veia. Separadas,
            # un cero en "outgoing" salta a la vista.
            "live_incoming_seen": 0,
            "live_outgoing_seen": 0,
            "live_incoming_persisted": 0,
            "live_outgoing_persisted": 0,
            "live_outgoing_rerouted": 0,
            "live_self_messages": 0,
            "sse_message_created": 0,
            "sse_chat_updated": 0,
            "sse_media_updated": 0,
            "stale_callbacks_ignored": 0,
            # Descifrado: se distingue lo que se recupera de lo que no.
            # Un fallo suelto que el reintento por receipt resuelve NO es lo
            # mismo que un mensaje perdido, y contarlos juntos no dejaba ver
            # cual de los dos estaba pasando.
            "decrypt_recovered": 0,
            "decrypt_unrecovered": 0,
            "sender_key_missing": 0,
            "mac_failures": 0,
            "no_session": 0,
            # Esperas de historial cortadas por una caida del transporte. NO
            # son timeouts: el telefono no tuvo ocasion de contestar.
            "transport_aborted_requests": 0,
        }
        # wamid -> motivo, de los que fallaron y aun no se han recuperado.
        # Acotado: un fallo antiguo no interesa y retenerlos todos seria una
        # fuga de memoria en una sesion larga.
        self._decrypt_pendientes: dict[str, str] = {}
        # Estado del ciclo de sincronizacion. WATCHING = conectado y a la
        # espera de cambios; es el estado NORMAL, no el final de nada.
        self.sync_state = "IDLE"

        # El estado de la maquina se publica en el bus para que cualquier
        # adaptador se entere sin conocer a los demas.
        self.state.on_change(self._publicar_estado)

    # -- Identidad y configuracion ------------------------------------------

    @property
    def session_exists(self) -> bool:
        return self.settings.session_file.exists()

    @property
    def own_jid(self) -> str | None:
        return own_jid(self.settings)

    @property
    def fingerprint(self) -> str | None:
        return session_fingerprint(self.settings)

    @property
    def rechazos_seguidos(self) -> int:
        """Rechazos 401 seguidos de la sesion guardada. Para poder informar.

        Al tercero se archiva y se pide un codigo nuevo; mientras tanto el
        frontend puede decir por donde va en vez de girar sin explicacion.
        """
        return self._rechazos_seguidos

    def info(self) -> RuntimeInfo:
        return RuntimeInfo(
            owner=self.owner,
            connected=self.state.state is AppState.CONNECTED,
            state=self.state.state.value,
            database=self.database is not None,
            session_file=self.session_exists,
            whatsapp_enabled=self._whatsapp,
        )

    # -- Arranque ------------------------------------------------------------

    def start_local(self) -> "AppRuntime":
        """Solo PostgreSQL. NO abre la sesion ni pide el cerrojo.

        Es lo que usa ``service.py --local`` y lo que permite leer la copia
        mientras otro proceso tiene la sesion: dos lectores de la base no
        estorban, dos duenos de la sesion si.
        """
        if self.database is None:
            self.database = Database(self.settings)
            self.database.connect()
            # Sin adjetivos: este metodo lo llaman los DOS caminos. Decir aqui
            # "modo local" era falso con ``py service.py``, que sigue y abre la
            # sesion un instante despues. Quien sabe en que modo esta es
            # ``start()``, y lo dice el.
            log.info("PostgreSQL listo")
        self._started = True
        return self

    def start(self, *, connect: bool = True) -> "AppRuntime":
        """Arranque completo: base, compatibilidades, sesion y trabajos.

        Vuelve EN CUANTO la conexion esta lanzada, no cuando esta establecida:
        quien llama (Flask) tiene que poder atender peticiones mientras tanto.

        :raises SessionLockedError: si otro proceso ya tiene la sesion.
        """
        self.start_local()
        self._lock.acquire()
        self._whatsapp = True
        log.info("Runtime WhatsApp habilitado")

        from app.whatsapp_client import WhatsAppClient, prepare_pywhats

        resumen = prepare_pywhats(self.settings)
        version = resumen.get("wa_version")
        if version is not None:
            log.info(
                "Version de WhatsApp Web: %s (%s)",
                ".".join(map(str, version)),
                resumen.get("wa_version_origin"),
            )

        # El bus hace de cola del cliente: expone put_nowait, asi que la capa
        # de protocolo no se entera de que ahora hay varios consumidores.
        self.client = WhatsAppClient(self.settings, self.bus)
        self._wire_session_signal()
        self._wire_services()

        # El observador traduce eventos del cliente a estado de sesion y de
        # QR. Arranca ANTES de conectar para no perderse el primer evento.
        self._start_event_observer()

        if connect:
            self._marcar_estado_inicial()
            # AUTO-PAIRING: si hace falta vincular, se dice ya, sin esperar a
            # que nadie pulse nada. El QR llegara por el canal normal y el
            # vigilante se encargara de renovarlo.
            if self.needs_pairing():
                log.info("Se requiere vinculacion: iniciando automaticamente")
                self.state.set(AppState.PAIRING, reason="vinculacion automatica")
                self.pairing.start_watchdog()
            self.client.start()
        return self

    def _marcar_estado_inicial(self) -> None:
        if self.session_exists:
            # Que exista device.json NO significa que la sesion valga: el
            # servidor puede rechazarla con un 401. Hasta su <success>, esto
            # es "comprobando".
            self.state.set(AppState.CHECKING_SESSION, reason="device.json presente")
        else:
            # Sin device.json no hay nada que comprobar: hace falta vincular,
            # y el QR se pedira solo. Nadie tiene que pulsar nada.
            self.state.set(AppState.NO_SESSION, reason="no hay device.json")

    def _wire_session_signal(self) -> None:
        """El ``<success>`` del servidor es la unica confirmacion del login."""
        from app.core.session_state import install_success_hook, set_success_callback

        install_success_hook()
        set_success_callback(lambda: self.bus.publish("session_valid", None))

    def _observar_evento(self, event: Any) -> None:
        """Traduce eventos del cliente a estado de sesion y de vinculacion.

        Corre en un hilo propio que consume el bus. Se hace asi, y no con un
        sink, porque estos eventos no persisten nada: solo cambian estado, y
        varios de ellos (``connected``, ``logged_out``) no tienen sink.
        """
        nombre = getattr(event, "name", "")
        carga = getattr(event, "payload", None)

        # Generacion del intento al que pertenece este evento. Si el cliente
        # que lo produjo ya fue sustituido, el evento es historia.
        generacion = getattr(event, "extra", {}).get("generation")
        if generacion is not None and not self.pairing.is_current(generacion):
            self.bump("stale_callbacks_ignored")
            log.info(
                "[SESSION] stale %s ignored generation=%s current=%s",
                nombre, generacion, self.pairing.connection_generation,
            )
            return

        if nombre == "qr" and carga:
            if self.pairing.committed:
                # Ya se escaneo: lo que falta es la conexion. Un QR tardio no
                # puede devolver la pantalla a "escanea esto".
                log.debug("QR posterior al pair-success ignorado")
                return
            self.pairing.note_qr(carga)
            if self.state.state is not AppState.QR_READY:
                self.state.set(AppState.QR_READY, reason="QR disponible")
        elif nombre == "paired":
            # pair-success: la vinculacion queda CERRADA. Se para el vigilante
            # y no se generan mas QR. Lo que viene (515, reconnect, <success>)
            # es cosa de la conexion.
            self.pairing.commit()
            self.state.set(AppState.CONNECTING, reason="dispositivo vinculado")
        elif nombre == "connected":
            # Provisional: el handshake termino, pero el servidor todavia
            # puede rechazar el login con un <failure reason="401">.
            if self.state.state not in (AppState.CONNECTED,):
                self.state.set(AppState.CONNECTING, reason="handshake completado")
        elif nombre == "session_valid":
            self._reiniciar_freno()
            self.pairing.note_linked()
            self.state.set(AppState.CONNECTED, reason="<success> del servidor")
        elif nombre == "logged_out":
            self._sesion_rechazada(carga)
        elif nombre == "paired":
            # Vinculacion NUEVA. Hasta este instante no existian ni
            # ``device.json`` ni el Signal Store, asi que la siembra del
            # arranque no pudo hacer nada: se hace AHORA, que es cuando hay
            # identidad y hay store.
            self._sembrar_lid_propio("pairing nuevo")
        elif nombre == "transport_lost":
            # Se cayo la linea. Las peticiones de historial en vuelo esperan
            # algo imposible: se las despierta ya, conservando su cursor.
            if self.backfill is not None:
                try:
                    cortadas = self.backfill.abort_pending("conexion perdida")
                    if cortadas:
                        self.bump("transport_aborted_requests", cortadas)
                except Exception:  # noqa: BLE001
                    log.exception("No se pudieron cortar las esperas en vuelo")
        elif nombre == "reconnecting":
            # El socket murio y se esta levantando otra vez. NO es CONNECTED:
            # mientras dure, no entra ni un mensaje, y el frontend tiene que
            # poder decirlo.
            intento = (carga or {}).get("attempt") if isinstance(carga, dict) else None
            self.state.set(
                AppState.RECONNECTING,
                reason=f"reconectando (intento {intento})" if intento else "reconectando",
            )
            self.set_sync_state("RECONNECTING")
        elif nombre == "reconnected":
            self.state.set(AppState.CONNECTED, reason="reconectado")
            self.set_sync_state("WATCHING")
        elif nombre == "disconnected":
            # Perder la conexion NO invalida la sesion: al reconectar se
            # vuelve a procesar todo. Se distingue de SESSION_INVALID, que si
            # es terminal.
            if self.state.state is AppState.CONNECTED:
                self.state.set(AppState.DISCONNECTED, reason="conexion perdida")
        elif nombre == "decrypt_error":
            # Se CUENTAN, no se tocan. "mac check failed", "no sender-key" y
            # "skmsg iteration older than chain iteration" son cosa de Signal
            # y del reintento por receipt que ya existe; mezclarlos con el
            # pipeline de mensajes solo enturbiaria ambos. Aqui solo se deja
            # constancia para poder medirlos.
            self._anotar_fallo_descifrado(carga, event)
            log.warning(
                "Mensaje no descifrado (%d en esta sesion); el reintento por "
                "receipt sigue su curso",
                self.decrypt_errors,
            )
        elif nombre in ("client_error", "client_stopped"):
            self._fin_del_cliente(carga)

    # Rechazos seguidos del MISMO device.json antes de parar. Tres es
    # suficiente para descartar un fallo puntual de red y bastante poco para
    # no insistir contra un servidor ajeno.
    MAX_RECHAZOS_MISMA_SESION = 3

    def _sesion_rechazada(self, motivo: Any) -> None:
        """El servidor rechazo el login (401).

        Un 401 suelto NO destruye nada: puede venir de un corte de red o de un
        rechazo temporal, y tirar por eso una vinculacion buena es lo que
        produjo el peor incidente de este proyecto (74 logins y 61 QR en
        segundos, 99 carpetas vacias). Los dos primeros solo se cuentan.

        Pero al TERCER rechazo seguido de la misma sesion ya no hay
        ambiguedad: el servidor esta diciendo, tres veces, que esa vinculacion
        no existe. Entonces si se archiva, porque no hacerlo deja algo peor
        que un archivado de mas: un estado zombi.

        Se midio: sin archivar, ``restart_pairing`` crea un cliente nuevo que
        encuentra el ``device.json`` muerto y hace login otra vez en vez de
        pedir un QR (``whatsapp_client._main``: ``if existed: connect()``). El
        resultado era ``state=PAIRING``, ``qr_available=false`` y
        ``session_file_present=true`` para siempre, con el frontend girando en
        "Generando codigo seguro...".

        Archivar NO es borrar: la sesion se guarda en ``diagnostics/``. Y no
        puede entrar en bucle, porque al desaparecer el ``device.json`` cambia
        la huella y la cuenta se reinicia sobre una vinculacion nueva.
        """
        huella = self.fingerprint
        if huella and huella == self._huella_rechazada:
            self._rechazos_seguidos += 1
        else:
            self._huella_rechazada = huella
            self._rechazos_seguidos = 1

        log.info(
            "[401] rechazo %d/%d huella=%s sesion_guardada=%s",
            self._rechazos_seguidos,
            self.MAX_RECHAZOS_MISMA_SESION,
            (huella or "?")[:8],
            self.session_exists,
        )
        self.pairing.note_unlinked()

        if self._rechazos_seguidos >= self.MAX_RECHAZOS_MISMA_SESION:
            self._descartar_sesion_revocada(motivo)
            return

        log.warning(
            "Login rechazado (%s), intento %d de %d con esta sesion",
            motivo,
            self._rechazos_seguidos,
            self.MAX_RECHAZOS_MISMA_SESION,
        )
        self.state.set(AppState.SESSION_INVALID, reason=f"login rechazado {motivo}")
        self.pairing.next_generation()

    def _descartar_sesion_revocada(self, motivo: Any) -> None:
        """La sesion esta muerta segun el servidor: archivar y vincular de nuevo.

        Es el unico caso en que se archiva sin que el usuario lo pida, y hace
        falta un motivo fuerte: tres rechazos seguidos de la MISMA sesion. No
        es una suposicion nuestra, es lo que el servidor ha contestado tres
        veces.

        Si no se pudiera archivar, NO se anuncia una vinculacion que no va a
        ocurrir: se deja ERROR con un mensaje que dice que hacer. Un estado
        intermedio eterno es peor que un error claro.
        """
        from app.whatsapp_client import archive_session

        log.warning(
            "El servidor rechazo la misma sesion %d veces (%s): esa vinculacion "
            "ya no existe. Se archiva y se pide un codigo nuevo.",
            self._rechazos_seguidos,
            motivo,
        )

        try:
            archivada = archive_session(self.settings, reason=f"revoked-{motivo}")
        except Exception:  # noqa: BLE001 - archivar no puede abortar esto
            log.exception("No se pudo archivar la sesion revocada")
            archivada = None

        # Una sesion son DOS cosas, y las dos tienen que irse juntas.
        #
        # ``device.json`` lleva la identidad (claves de Noise, identity
        # keypair, registration_id) y ``device.json.signal.db`` lleva el
        # estado Signal construido BAJO esa identidad: sesiones, prekeys,
        # sender keys, claves de app-state. Dejar uno sin el otro produce una
        # mezcla que no es una sesion de nadie.
        #
        # Se midio: al archivar tras un 401, el store quedaba bloqueado y se
        # saltaba. El pairing siguiente creaba un device.json nuevo
        # (registration_id 572666329) sobre un store del anterior
        # (registration_id 1403204623), con 14 sesiones y 8 sender keys
        # heredadas. Sintoma visible: "unknown one-time pre-key id 66" en cada
        # mensaje entrante.
        restos = [
            ruta
            for ruta in (self.settings.session_file, self.settings.signal_store_file)
            if ruta.exists()
        ]
        if restos:
            log.error(
                "No se pudo apartar la sesion revocada entera; siguen ahi: %s. "
                "NO se vincula de nuevo: un device.json nuevo sobre un Signal "
                "Store viejo produce una identidad mezclada que no descifra "
                "nada. Cierra el servicio y arrancalo con --fresh, o mueve esa "
                "carpeta a mano. PostgreSQL no se ha tocado.",
                ", ".join(r.name for r in restos),
            )
            self.state.set(
                AppState.ERROR,
                reason="la sesion revocada no se pudo apartar entera",
            )
            return

        if archivada is not None:
            log.info("Sesion revocada archivada en %s", archivada.name)

        # Vinculacion nueva: la cuenta de rechazos era de la sesion anterior y
        # los reintentos tambien. Empezar con ellos gastados haria que el
        # pairing nuevo naciera sin margen.
        self._huella_rechazada = None
        self._rechazos_seguidos = 0
        self._reiniciar_freno()

        self.pairing.invalidate()
        self.state.set(AppState.PAIRING_REQUIRED, reason="sesion revocada por el servidor")
        self.restart_pairing()

    # Espera minima entre reintentos de vinculacion, en segundos. Crece con
    # cada intento seguido: 5, 10, 20, 40... hasta el tope.
    REINTENTO_BASE = 5.0
    REINTENTO_MAXIMO = 120.0
    # Tras esto se para y se pide intervencion, en vez de insistir sin fin.
    REINTENTOS_MAXIMOS = 8

    def _puede_reintentar(self) -> bool:
        """Freno de los reintentos de vinculacion.

        Sin esto, un 401 repetido produce un bucle cerrado: se midieron 74
        intentos de login y 61 QR en cuestion de segundos, todos contra los
        servidores de WhatsApp. Ademas de inutil, es abusivo, y arriesga que
        la cuenta acabe limitada.

        Espera creciente y tope de intentos. Al agotarse NO se sigue: se deja
        el estado en ERROR y se dice que hace falta intervenir.
        """
        import time as _time

        if self._reintentos_seguidos >= self.REINTENTOS_MAXIMOS:
            if self.state.state is not AppState.ERROR:
                log.error(
                    "Vinculacion abandonada tras %d intentos seguidos. "
                    "Revisa que el dispositivo no siga vinculado en el telefono "
                    "y reinicia el servicio cuando quieras volver a intentarlo.",
                    self._reintentos_seguidos,
                )
                self.state.set(
                    AppState.ERROR, reason="demasiados intentos de vinculacion"
                )
            return False

        espera = min(
            self.REINTENTO_BASE * (2 ** self._reintentos_seguidos),
            self.REINTENTO_MAXIMO,
        )
        transcurrido = _time.monotonic() - self._ultimo_reintento
        if self._ultimo_reintento and transcurrido < espera:
            log.debug(
                "Reintento de vinculacion frenado: faltan %.0fs",
                espera - transcurrido,
            )
            return False

        self._ultimo_reintento = _time.monotonic()
        self._reintentos_seguidos += 1
        if self._reintentos_seguidos > 1:
            log.info(
                "Reintento de vinculacion %d/%d (proxima espera %.0fs)",
                self._reintentos_seguidos,
                self.REINTENTOS_MAXIMOS,
                min(self.REINTENTO_BASE * (2 ** self._reintentos_seguidos),
                    self.REINTENTO_MAXIMO),
            )
        return True

    def _reiniciar_freno(self) -> None:
        """Una conexion buena borra la cuenta de intentos."""
        self._reintentos_seguidos = 0
        self._ultimo_reintento = 0.0
        # Y tambien la de rechazos: si esta sesion acaba de conectar, los 401
        # anteriores eran de otra cosa.
        self._huella_rechazada = None
        self._rechazos_seguidos = 0

    def _fin_del_cliente(self, motivo: Any) -> None:
        """El cliente termino. Decide si eso es un error o un reintento.

        Se midio en una prueba limpia: al agotarse los intentos de pairing el
        estado pasaba a ERROR y ahi se quedaba 4 minutos y 40 segundos, hasta
        que vencia el TTL del QR y el vigilante renovaba. En esa ventana
        ``/session/qr`` seguia diciendo ``available: true`` con un codigo YA
        MUERTO: quien lo escaneara no conseguia nada, y el frontend mostraba
        un error de un sistema que iba a recuperarse solo.

        Ahora, si todavia hace falta vincular:

        * el QR se invalida EN EL ACTO, porque su flujo ya no existe;
        * se delega en ``restart_pairing``, que decide el estado segun haya o
          no una sesion guardada;
        * se reintenta sin esperar al TTL.

        ERROR queda para lo que de verdad no se va a arreglar solo.
        """
        if self._stopping.is_set():
            return

        if self.pairing.committed:
            # Se escaneo y el flujo termino: lo que sigue es la reconexion tras
            # el 515, no una vinculacion nueva.
            log.info("El cliente termino tras el pair-success; esperando reconexion")
            return

        vinculando = self.state.state in NEEDS_PAIRING or self.state.state in (
            AppState.PAIRING,
            AppState.QR_READY,
        )
        if not vinculando:
            self.state.set(AppState.ERROR, reason=str(motivo)[:120])
            return

        log.warning(
            "La vinculacion termino sin exito (%s); se reintenta",
            str(motivo)[:80],
        )
        # El QR muere con su flujo: seguir ofreciendolo seria mentir.
        self.pairing.invalidate()

        # El freno y el estado los decide ``restart_pairing``, y SOLO el.
        #
        # Antes se consumia el freno aqui y ademas se anunciaba PAIRING, y las
        # dos cosas estaban mal. ``restart_pairing`` vuelve a comprobar el
        # freno; como acababa de gastarse, encontraba la espera sin cumplir y
        # se iba sin arrancar ningun cliente. Resultado medido: tras el PRIMER
        # 401 no habia segundo intento, ni tercero, ni archivado, ni QR. El
        # estado se quedaba en PAIRING con qr_available=false para siempre.
        #
        # Y el estado tampoco puede fijarse aqui: si el device.json sigue
        # estando, lo que viene es un login, no un codigo nuevo.
        self.pairing.renew()

    def _start_event_observer(self) -> None:
        """Hilo que traduce eventos del bus a estado. Uno solo por runtime."""
        if getattr(self, "_observer", None) is not None:
            return

        suscripcion = self.bus.subscribe()

        def bucle() -> None:
            while not self._stopping.is_set():
                evento = suscripcion.get(timeout=1.0)
                if evento is None:
                    continue
                try:
                    self._observar_evento(evento)
                except Exception:  # noqa: BLE001 - observar no puede tumbar nada
                    log.exception("Fallo procesando un evento de sesion")
            suscripcion.close()

        self._observer = threading.Thread(
            target=bucle, name="session-observer", daemon=True
        )
        self._observer.start()

    # -- Vinculacion ---------------------------------------------------------

    def needs_pairing(self) -> bool:
        return self.state.state in NEEDS_PAIRING

    def restart_pairing(self) -> None:
        """Vuelve a lanzar la vinculacion con un cliente nuevo.

        Se construye un cliente NUEVO en vez de reutilizar el anterior: tras
        agotar sus intentos, el que habia ya tiene su ciclo de vida cerrado
        (``_stopping`` puesto, event loop terminado) y reactivarlo seria
        pelearse con su estado interno.

        NO toca el cerrojo: la sesion sigue siendo de este proceso.
        """
        if not self._whatsapp or self._stopping.is_set():
            return
        if self.state.state is AppState.CONNECTED:
            log.debug("Ya conectado; no se reinicia la vinculacion")
            return
        if self.pairing.committed:
            # Ya hubo pair-success: reiniciar aqui pediria un SEGUNDO escaneo.
            log.debug("Vinculacion ya cerrada; no se reinicia")
            return
        if not self._puede_reintentar():
            return

        generacion = self.pairing.next_generation()
        log.info(
            "Reiniciando la vinculacion para pedir un QR nuevo (generacion %d)",
            generacion,
        )
        anterior = self.client
        if anterior is not None:
            try:
                anterior.stop(timeout=5.0)
            except Exception:  # noqa: BLE001 - el que viene es el que importa
                log.debug("El cliente anterior no cerro limpiamente")

        from app.whatsapp_client import WhatsAppClient

        self.client = WhatsAppClient(self.settings, self.bus)
        self._wire_services()

        # PAIRING solo si de verdad va a haber un QR.
        #
        # ``WhatsAppClient._main`` decide por la existencia del device.json:
        # si esta, hace login directo y NO emite ningun QR. Anunciar PAIRING
        # en ese caso deja al frontend esperando un codigo que no llegara
        # nunca, que es exactamente el estado zombi que se midio:
        # state=PAIRING, qr_available=false, session_file_present=true.
        if self.session_exists:
            log.info(
                "[401] reintento con la sesion guardada (aun no se descarta): "
                "si vuelve a rechazarse, al tercero se archiva"
            )
            self.state.set(
                AppState.CONNECTING, reason="reintento con la sesion guardada"
            )
        else:
            self.state.set(AppState.PAIRING, reason="vinculacion reiniciada")
        self.client.start()

    def _wire_services(self) -> None:
        """Ingesta, mensajes en vivo, contactos, backfill y orquestador."""
        assert self.database is not None and self.client is not None

        from app.core.orchestrator import Orchestrator
        from app.services.backfill_service import BackfillService
        from app.services.contacts_service import ContactService
        from app.services.history_gate import InitialHistoryGate, initial_history_confirmed
        from app.services.live_service import LiveMessageService

        propio = self.own_jid

        # -- History Sync: persistir cada blob en cuanto llega --
        self._wire_history_ingestion(propio)

        # -- Mensajes en vivo --
        # Se envuelve el sink para que un mensaje nuevo pueda SEMBRAR un chat
        # que estaba sin ancla. Es lo que permite que una conversacion vieja
        # vuelva a ser excavable en cuanto alguien escribe en ella.
        # El LID propio se pasa para poder distinguir un auto-mensaje (nota
        # para uno mismo, que SI va al chat personal) de un mensaje
        # saliente dirigido a otra persona.
        vivo = LiveMessageService(
            self.database, own_jid=propio, own_lid=own_identity(self.settings)[1]
        )

        def recibir(mensaje: Any) -> Any:
            self.bump("receiver_messages_seen")
            self.bump("live_handle_called")
            identificador = getattr(mensaje, "id", None)
            log.info("[LIVE] ingress id=%s", identificador)
            # Si este mensaje habia fallado antes, el reintento lo salvo.
            self._marcar_recuperado(identificador)

            resultado = vivo.handle(mensaje)
            if resultado is None:
                # Puede ser un evento interno filtrado, o un fallo ya
                # registrado. En ambos casos NO hay mensaje que anunciar.
                return None

            # Se copian los contadores del servicio, que es quien sabe la
            # direccion real: la decide el protobuf, no el evento.
            for interno, externo in (
                ("incoming_seen", "live_incoming_seen"),
                ("outgoing_seen", "live_outgoing_seen"),
                ("incoming_stored", "live_incoming_persisted"),
                ("outgoing_stored", "live_outgoing_persisted"),
                ("outgoing_rerouted", "live_outgoing_rerouted"),
                ("self_messages", "live_self_messages"),
            ):
                self.counters[externo] = getattr(vivo.stats, interno, 0)

            if resultado.get("new"):
                self.bump("live_persisted")
                log.info(
                    "[LIVE] persisted id=%s db_id=%s",
                    identificador, resultado.get("message_id"),
                )
                self._sembrar([resultado.get("chat_jid")])
            else:
                self.bump("live_duplicates")
            return resultado

        self.client.sinks["message"] = recibir

        # -- Nombres de la agenda --
        contactos = ContactService(self.database)
        self.client.sinks["contact"] = contactos.handle_contact
        self.client.sinks["pushname"] = contactos.handle_pushname

        # -- Barrera del historial inicial --
        # Si esta sesion YA recibio su bootstrap no hay nada que esperar: el
        # servidor no lo reenvia, y esperarlo costaba 180 s en cada arranque.
        huella = self.fingerprint
        ya_confirmado = initial_history_confirmed(self.database, huella)
        self.gate = InitialHistoryGate(
            settle_seconds=self.settings.history_settle_seconds,
            already_confirmed=ya_confirmado,
        )
        if ya_confirmado:
            log.info("Historial inicial ya confirmado para esta sesion")

        # -- Backfill historico --
        self.backfill = BackfillService(self.settings, self.database)
        pn, lid = own_identity(self.settings)
        self.backfill.set_own_identity(pn, lid)
        log.info("Identidad propia: pn=%s lid=%s", bool(pn), bool(lid))

        # -- Ciclo de sincronizacion manual (el boton del frontend) --
        # Se crea una sola vez: su estado tiene que sobrevivir a la peticion
        # HTTP para que /sync/status pueda contar como va.
        if self.seed_recovery is None:
            from app.services.seed_recovery import SeedRecovery

            self.seed_recovery = SeedRecovery(self.database)

        if self.seed_queue is None:
            from app.services.seed_queue import SeedBackfillQueue

            # El bucle del cliente es donde vive el backfill: la excavacion
            # tiene que correr ahi, no en el hilo del receptor.
            cliente = self.client
            self.seed_queue = SeedBackfillQueue(
                self.database,
                self.backfill,
                lambda: getattr(cliente, "_loop", None),
            )

        if self.sync_job is None:
            from app.services.sync_job import SyncJob

            self.sync_job = SyncJob(
                self.settings, self.database, publish=self.bus.publish
            )

        # -- Orquestador: salud, mantenimiento y trabajo de fondo --
        self.orchestrator = Orchestrator(
            self.settings, self.database, self.client, publish=self.bus.publish
        )
        self.orchestrator.backfill = self.backfill
        self.orchestrator.gate = self.gate
        # El orquestador avisa del estado del ciclo sin conocer al runtime.
        self.orchestrator._set_sync_state = self.set_sync_state
        # Los chats que la reconciliacion despierte se excavan enseguida, no
        # en el siguiente ciclo.
        self.orchestrator._on_seeds_recovered = self._encolar_despertados
        self.orchestrator._on_own_identity_ready = self._sembrar_lid_propio
        self.orchestrator.prepare()
        self.client.post_connect = self.orchestrator.post_connect
        self.client.on_shutdown = self._stop_workers

    def _wire_history_ingestion(self, own_jid_value: str | None) -> None:
        """Persiste cada History Sync en cuanto llega.

        Corre en el hilo del cliente, no en el de ninguna interfaz: escribir en
        PostgreSQL no puede congelar la ventana ni bloquear una peticion HTTP.
        """
        from app.compat import history_compat
        from app.services.history_service import ingest_history_sync

        sync_log = get_logger("SYNC")
        database = self.database
        assert database is not None
        signal_db = self.settings.signal_store_file

        def ingest(full: Any) -> None:
            # Se avisa ANTES de persistir: al backfill le basta saber que su
            # chat aparecio en un blob para dejar de esperar.
            if self.gate is not None:
                self.gate.note_history_sync(full.sync_type)
            if self.backfill is not None:
                self.backfill.notify_history(full)
            try:
                with database.transaction() as session:
                    resultado = ingest_history_sync(
                        session, full, own_jid=own_jid_value, signal_db=signal_db
                    )
            except Exception as exc:  # noqa: BLE001 - el blob ya esta en disco
                sync_log.exception("Fallo al persistir el History Sync: %s", exc)
                sync_log.warning(
                    "El blob sigue en data/history/; se puede reintentar con "
                    "'py scripts/ingest_blobs.py' sin volver a pedir nada al servidor"
                )
                return

            # Cuantos mensajes metio ESTE blob. Es lo que el backfill usa
            # para saber que trajo su peticion, en vez de restar el numero de
            # filas antes y despues: con el receptor corriendo a la vez, esa
            # resta apuntaba los mensajes en vivo como si fueran historial.
            if self.backfill is not None:
                try:
                    self.backfill.note_history_ingest(
                        getattr(resultado, "messages_inserted", 0),
                        sum(
                            len(getattr(c, "messages", ()) or ())
                            for c in getattr(full, "conversations", ())
                        ),
                    )
                except Exception:  # noqa: BLE001 - contabilizar no puede cortar
                    sync_log.debug("No se pudo anotar el conteo del blob")

            # Un History Sync puede traer el PRIMER mensaje de un chat que
            # estaba sin ancla. Ese mensaje la crea, y el chat pasa a poder
            # excavarse sin esperar a ningun reinicio.
            self._sembrar([c.jid for c in getattr(full, "conversations", [])])
            self.bus.publish("history_ingested", str(resultado))

        history_compat.set_callback(ingest)

    # Cuantos fallos de descifrado se recuerdan a la vez.
    MAX_DECRYPT_PENDIENTES = 200

    def _anotar_fallo_descifrado(self, wamid: Any, event: Any) -> None:
        """Registra un fallo de descifrado. NO toca Signal.

        Se clasifica por el motivo que da pywhats para poder decir QUE tipo de
        fallo es, no solo cuantos. El reintento por receipt sigue exactamente
        como estaba: aqui solo se mide.

        Nunca se registra el contenido, solo el identificador y el motivo
        tecnico.
        """
        self.decrypt_errors += 1
        self.bump("receiver_decrypt_errors")

        extras = getattr(event, "extra", {}) or {}
        argumentos = extras.get("args") or []
        motivo = str(argumentos[0]) if argumentos else ""
        bajo = motivo.lower()
        if "sender-key" in bajo or "sender key" in bajo:
            self.bump("sender_key_missing")
        elif "mac" in bajo:
            self.bump("mac_failures")
        elif "no session" in bajo:
            self.bump("no_session")

        if wamid:
            clave = str(wamid)
            if clave not in self._decrypt_pendientes:
                self.bump("decrypt_unrecovered")
            self._decrypt_pendientes[clave] = motivo[:120]
            if len(self._decrypt_pendientes) > self.MAX_DECRYPT_PENDIENTES:
                self._decrypt_pendientes.pop(next(iter(self._decrypt_pendientes)))
        log.warning(
            "[LIVE] decrypt failed id=%s motivo=%s (%d en esta sesion); "
            "el reintento por receipt sigue su curso",
            wamid, motivo[:60] or "?", self.decrypt_errors,
        )

    def _marcar_recuperado(self, wamid: Any) -> None:
        """El reintento funciono: ese mensaje ya no esta perdido."""
        if not wamid:
            return
        clave = str(wamid)
        if self._decrypt_pendientes.pop(clave, None) is not None:
            self.bump("decrypt_recovered")
            self.bump("decrypt_unrecovered", -1)
            log.info("[LIVE] decrypt recovered id=%s (tras reintento)", clave)

    def bump(self, nombre: str, cantidad: int = 1) -> None:
        """Sube un contador de etapa. Nunca lanza."""
        try:
            self.counters[nombre] = self.counters.get(nombre, 0) + cantidad
        except Exception:  # noqa: BLE001 - un contador no rompe nada
            pass

    def set_sync_state(self, estado: str) -> None:
        """Cambia el estado del ciclo y lo publica.

        WATCHING no es "terminado": es el estado normal de un servicio
        conectado esperando cambios. Terminar el runtime al acabar el backfill
        seria justo lo contrario de lo que queremos.
        """
        if self.sync_state == estado:
            return
        self.sync_state = estado
        if estado == "WATCHING":
            log.info("[SYNC] WATCHING - esperando cambios de WhatsApp")
        else:
            log.info("[SYNC] %s", estado)
        self.bus.publish("sync_state_changed", {"sync_state": estado})

    def _sembrar_lid_propio(self, motivo: str) -> None:
        """Registra nuestro par PN<->LID en el mapa de Signal, ahora.

        La siembra del arranque (``prepare_pywhats`` -> ``apply_all``) corre
        ANTES de crear el cliente, y en una vinculacion nueva eso es demasiado
        pronto: ni ``device.json`` ni el Signal Store existen todavia, asi que
        no hay nada que sembrar ni donde. Se midio: tras un pairing limpio el
        mapa tenia UNA fila, de un contacto, y la nuestra no estaba; los
        mensajes propios volvian a fallar con "no session for peer".

        Aqui ya hay identidad y hay store, asi que esta vez si escribe. Es
        idempotente: si ya estaba, no hace nada.
        """
        if not self.settings.compat_own_lid_map:
            return
        try:
            from app.compat import own_lid_map

            if own_lid_map.seed(self.settings):
                log.info("Par PN<->LID propio registrado (%s)", motivo)
            else:
                log.warning(
                    "No se pudo registrar el par PN<->LID propio (%s): los "
                    "mensajes que envies desde el telefono podrian no "
                    "descifrarse",
                    motivo,
                )
        except Exception:  # noqa: BLE001 - sembrar no puede tumbar la sesion
            log.exception("Fallo registrando el par PN<->LID propio")

    def _sembrar(self, chat_jids: Any) -> None:
        """Un mensaje nuevo puede desbloquear un chat sin ancla.

        Nunca dispara un backfill global: marca el chat como excavable y lo
        encola para pedir SU historial. Excavar los cuarenta chats cada vez
        que llega un mensaje seria bombardear el telefono del usuario, que es
        quien atiende las peticiones ``ON_DEMAND``.

        Se midio por que hace falta encolar: "Juan Andrés" consiguio su
        semilla, paso a ``pending`` y ahi se quedo, porque el backfill solo
        mira los chats al arrancar y en el ciclo manual.
        """
        if self.seed_recovery is None or not chat_jids:
            return
        try:
            informe = self.seed_recovery.seed_from_messages(chat_jids)
        except Exception:  # noqa: BLE001 - sembrar no puede tumbar la ingesta
            log.exception("Fallo buscando semillas nuevas")
            return
        if not informe.sembrados:
            return

        self.bus.publish("chats_seeded", {"chats": informe.chats})
        # Evento interno: "este chat ya tiene con que pedir historial".
        self.bus.publish("history.seed_available", {"chats": informe.chats})

        cola = self.seed_queue
        if cola is not None:
            # Encolar y seguir. La excavacion NO puede hacer esperar al
            # receptor: se hace en la tarea de la cola.
            cola.enqueue(informe.chats)

    def _encolar_despertados(self, chat_jids: Any) -> None:
        """Chats que ya tenian ancla y seguian marcados como dormidos.

        Es la recuperacion de los que despertaron mientras la siembra
        automatica no funcionaba: no hace falta pedirle al usuario que vuelva
        a escribir en ellos.
        """
        if not chat_jids:
            return
        self.bus.publish("history.seed_available", {"chats": list(chat_jids)})
        cola = self.seed_queue
        if cola is not None:
            cola.enqueue(chat_jids)

    # -- Eventos -------------------------------------------------------------

    def _publicar_estado(self, cambio: Any) -> None:
        self.bus.publish(
            "session_state_changed",
            {
                "previous": cambio.previous.value,
                "current": cambio.current.value,
                "generation": cambio.generation,
                "reason": cambio.reason,
            },
        )

    def subscribe(self, *, replay: bool = False) -> Any:
        """Cola propia de eventos. La usan la ventana y cada cliente SSE."""
        return self.bus.subscribe(replay=replay)

    # -- Parada --------------------------------------------------------------

    def _stop_workers(self) -> None:
        """Detiene el trabajo de fondo. SOLO senaliza.

        Cerrar el cliente desde fuera de su event loop es lo que producia el
        'Task was destroyed but it is pending' del cierre; esa parte la hace
        el propio cliente en su hilo.
        """
        if self.orchestrator is not None:
            self.orchestrator.stop()
        elif self.backfill is not None:
            self.backfill.stop()

    def stop(self) -> None:
        """Cierre ordenado. Es idempotente y NUNCA borra datos."""
        if self._stopping.is_set():
            return
        self._stopping.set()

        self.pairing.stop()
        if self.client is not None:
            try:
                self.client.stop()
            except Exception:  # noqa: BLE001 - cerrar no puede fallar hacia fuera
                log.exception("Fallo al cerrar el cliente de WhatsApp")
        self._lock.release()
        if self.database is not None:
            self.database.dispose()
            self.database = None
        log.info("Runtime detenido")

    def __enter__(self) -> "AppRuntime":
        return self

    def __exit__(self, *_excinfo: Any) -> None:
        self.stop()


def build_runtime(
    *, owner: str, settings: Settings | None = None, configure_logging: bool = True
) -> AppRuntime:
    """Fabrica unica del runtime. La usa ``service.py``."""
    return AppRuntime(settings, owner=owner, configure_logging=configure_logging)


def build_service_runtime(
    settings: Settings | None = None, *, configure_logging: bool = True
) -> AppRuntime:
    """El runtime EXACTO que monta ``service.py``, base de datos incluida.

    Existe para que las pruebas ejerciten el mismo cableado que el producto y
    no una version montada a mano. Una correccion no vale porque funcione con
    un runtime construido de otra forma: el entrypoint de referencia es
    ``service.py``, y este es su camino.

    NO abre la sesion de WhatsApp ni pide el cerrojo: eso lo hace
    ``start()``, en su hilo, y depende de que la sesion este libre.
    """
    runtime = build_runtime(
        owner="service.py", settings=settings, configure_logging=configure_logging
    )
    runtime.start_local()
    return runtime


def build_service_app(runtime: AppRuntime) -> Any:
    """La aplicacion Flask tal y como la sirve ``service.py``."""
    from app.api import create_app

    return create_app(runtime)


__all__ = [
    "AppRuntime",
    "RuntimeInfo",
    "SessionLockedError",
    "build_runtime",
    "build_service_app",
    "build_service_runtime",
]
