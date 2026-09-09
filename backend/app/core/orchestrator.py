"""Orquestador: el ciclo completo de la aplicacion, sin intervencion manual.

QUE CAMBIA
----------
Antes las piezas se cableaban a mano y el usuario tenia que acordarse
de ejecutar ``inspect_db.py``, ``repair_db.py`` o ``probe_chat.py`` para que
la copia estuviera al dia. Esas herramientas siguen existiendo para
diagnostico, pero el mantenimiento normal ocurre aqui solo.

EL FLUJO
---------------------
    START
      -> migraciones            (las verifica service.py antes de llegar aqui)
      -> salud de la base       health_check()
      -> mantenimiento seguro   MaintenanceService.run_all()
      -> cargar sesion / conectar
      -> resolver identidad propia
      -> preparacion del historial   (sin esperar 180 s si ya se recibio)
      -> ingerir el sync pendiente
      -> reconciliar el estado historico
      -> arrancar el worker de multimedia
      -> arrancar el planificador de backfill
      -> habilitar la GUI
      -> procesar eventos en vivo
      -> mantenimiento periodico en segundo plano
      -> cierre limpio

LO QUE NO HACE
--------------
NADA destructivo. No borra mensajes, ni multimedia, ni la sesion, ni
``raw_proto``. Todo lo que ejecuta automaticamente esta en la lista blanca de
``repair_db.py`` sigue siendo la unica via para lo demas y
sigue exigiendo autorizacion explicita.

LA GUI NO ESPERA
-----------------------------
El visor se habilita en cuanto el servidor confirma la sesion. El historial y
la multimedia siguen llegando por detras y la barra de estado lo cuenta. Nunca
hay tres minutos de pantalla congelada.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.core.logging_setup import get_logger

log = get_logger("APP")


# ---------------------------------------------------------------------------
# Estado que se ensena en la barra inferior
# ---------------------------------------------------------------------------


@dataclass
class RuntimeStatus:
    """Lo que la barra de estado necesita saber. Sin jerga tecnica."""

    connection: str = "Iniciando"
    connected: bool = False
    history: str = "en espera"
    history_done: bool = False
    media_pending: int = 0
    media_done: bool = True
    backfill: str = "en espera"
    backfill_done: bool = False

    def summary(self) -> str:
        partes = [self.connection]
        partes.append(f"Historial: {self.history}")
        if self.media_pending:
            partes.append(f"Multimedia: {self.media_pending} pendientes")
        else:
            partes.append("Multimedia: al dia")
        partes.append(f"Backfill: {self.backfill}")
        return "   ·   ".join(partes)


@dataclass
class HealthReport:
    """Comprobacion LIGERA del arranque.

    Ligera de verdad: ni un ``COUNT(*)`` sobre ``messages``. Un escaneo
    profundo se pide a mano con ``inspect_db.py --deep``.
    """

    database: bool = False
    server_version: str | None = None
    session: bool = False
    identity: bool = False
    signal_store: bool = False
    media_dirs: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.database and self.media_dirs

    def __str__(self) -> str:
        def marca(valor: bool) -> str:
            return "OK" if valor else "NO"

        return (
            f"DB={marca(self.database)} sesion={marca(self.session)} "
            f"identidad={marca(self.identity)} signal={marca(self.signal_store)} "
            f"media={marca(self.media_dirs)}"
        )


class Orchestrator:
    """Coordina arranque, trabajo de fondo y cierre.

    No conoce Tkinter. Se comunica con la interfaz publicando eventos por el
    cliente, igual que el resto de servicios, de modo que puede ejecutarse
    tambien sin GUI.
    """

    def __init__(
        self,
        settings: Any,
        database: Any,
        client: Any,
        *,
        publish: Callable[[str, Any], None] | None = None,
        whatsapp_account_id: Any = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._client = client
        self._publish = publish or (lambda _name, _payload: None)
        #: De QUE cuenta es este orquestador.
        #:
        #: Se recibe, no se adivina. Todo lo que escriba --nombres de grupo,
        #: por ejemplo-- tiene que ir acotado: sin la cuenta, un UPDATE por
        #: JID alcanza la conversacion de la otra persona.
        self.whatsapp_account_id = whatsapp_account_id

        self.status = RuntimeStatus()
        self.maintenance: Any = None
        # ``post_connect`` corre tambien al reconectar. La primera vez no hay
        # nada que reconciliar (se acaba de arrancar); las siguientes si.
        self._reconexion = False
        self.media: Any = None
        self.backfill: Any = None
        self.gate: Any = None
        # Solo para el resumen del arranque. Lo cablea el runtime.
        self.seed_collector: Any = None
        # Devuelve la linea de resumen del receptor. Lo cablea el runtime.
        self._live_summary: Any = None

        self._tasks: list[asyncio.Task[Any]] = []
        self._stopping = False
        self._fingerprint: str | None = None

    # -- Fase sincrona: antes de conectar ------------------------------------

    def health_check(self) -> HealthReport:
        """Comprobacion rapida de que el entorno esta en pie."""
        report = HealthReport()

        try:
            salud = self._database.health()
            report.database = True
            report.server_version = str(salud.get("server_version", ""))
        except Exception as exc:  # noqa: BLE001
            report.problems.append(f"PostgreSQL: {exc}")

        session_file = getattr(self._settings, "session_file", None)
        report.session = bool(session_file is not None and session_file.exists())

        # El almacen de Signal ya no es un fichero SQLite: Baileys lo guarda
        # como ficheros sueltos junto a las credenciales. Hay almacen cuando
        # esa carpeta tiene algo mas que el propio `creds.json`.
        carpeta = getattr(self._settings, "session_dir_baileys", None)
        report.signal_store = bool(
            carpeta is not None
            and carpeta.is_dir()
            and any(p.name != "creds.json" for p in carpeta.iterdir())
        )

        try:
            from app.core.identity import own_identity

            own_pn, own_lid = own_identity(self._settings)
            report.identity = bool(own_pn or own_lid)
        except Exception:  # noqa: BLE001 - sin identidad se sigue arrancando
            report.problems.append("no se pudo resolver la identidad propia")

        try:
            self._settings.ensure_directories()
            report.media_dirs = self._settings.media_dir.is_dir()
        except Exception as exc:  # noqa: BLE001
            report.problems.append(f"carpetas de multimedia: {exc}")

        log.info("Health check: %s", report)
        for problema in report.problems:
            log.warning("Health check: %s", problema)
        return report

    def run_maintenance(self) -> Any:
        """Reconciliacion segura. NUNCA destructiva."""
        from app.services.maintenance_service import MaintenanceService

        if self.maintenance is None:
            self.maintenance = MaintenanceService(self._database, self._settings)
        informe = self.maintenance.run_all()

        # Un chat puede haber despertado aqui: tenia una semilla real y seguia
        # marcado como dormido. Se avisa para que se le pida SU historial sin
        # esperar al siguiente ciclo. Sin esto, el mantenimiento periodico lo
        # dejaba en 'pending' y ahi se quedaba.
        despertados = getattr(informe, "seeds_recovered_chats", None)
        avisar = getattr(self, "_on_seeds_recovered", None)
        if despertados and avisar is not None:
            try:
                avisar(list(despertados))
            except Exception:  # noqa: BLE001 - avisar no puede tumbar el ciclo
                log.exception("Fallo avisando de los chats despertados")
        return informe

    def prepare(self) -> HealthReport:
        """Todo lo que se puede hacer antes de hablar con WhatsApp."""
        report = self.health_check()
        if report.database:
            self.run_maintenance()
        return report

    # -- Fase asincrona: despues de conectar ---------------------------------

    #: Cuanto se espera, tras recuperar una sesion guardada, antes de probar
    #: ON_DEMAND.
    #:
    #: POR QUE HACE FALTA
    #: ------------------
    #: Una vinculacion NUEVA espera el ``INITIAL_BOOTSTRAP``, y esa espera —de
    #: segundos a minutos— hace de asentamiento sin que nadie la pensara. Una
    #: sesion RECUPERADA no espera nada: ``_await_history`` vuelve en el acto
    #: porque el bootstrap ya se confirmo en su dia. Medido en la sesion real:
    #: ``<success>`` a las 00:32:38.443 y la peticion de prueba a las
    #: 00:32:40.489. Dos segundos.
    #:
    #: En esos dos segundos el servidor todavia estaba mandando
    #: ``ib: dirty type=account_sync`` y acababa de terminar el vaciado de lo
    #: pendiente. Pedir historial ahi es pedirlo a una sesion que aun se esta
    #: colocando.
    #:
    #: No es un `sleep` a ciegas: primero se espera a las señales que SI
    #: existen —identidad, Signal, cuenta, app-state, vaciado— y esto es solo
    #: el margen que queda despues.
    GRACIA_TRAS_RECUPERAR_SESION = 20.0

    def _registrar_par_propio(self, sesion: Any) -> None:
        """Anota nuestro PN<->LID en `contacts`. Nunca lanza.

        El LID propio NO llega en el ``pair-success``: llega en el
        ``<success>``, un segundo despues. Por eso se lee del dispositivo ya
        conectado y no del disco, que va un paso por detras.
        """
        try:
            from app.services.contacts_service import guardar_par_lid

            dispositivo = getattr(sesion, "device", None)
            jid = getattr(dispositivo, "jid", None)
            lid = getattr(dispositivo, "lid", None)
            if jid is None or not lid:
                log.debug("Todavia no hay par PN<->LID propio que registrar")
                return
            if guardar_par_lid(self._database, str(lid), str(jid)):
                log.info("Par PN<->LID propio registrado")
        except Exception:  # noqa: BLE001 - no puede tumbar la conexion
            log.exception("Fallo registrando el par PN<->LID propio")

    async def post_connect(self, pywhats_client: Any) -> None:
        """Secuencia completa tras el ``<success>`` del servidor.

        Se ejecuta tambien despues de RECONECTAR, y eso importa: mientras el
        socket estuvo muerto no llego ni un evento, asi que lo que ocurriera
        en ese rato no se puede dar por recibido. Al volver se reconcilia.
        """
        # Desde aqui se cuenta el asentamiento. Es el momento del
        # ``<success>``, no el del arranque del proceso.
        self._conectado_en = time.monotonic()
        self.status.connection = "Conectado"
        self.status.connected = True
        self._emit_status()

        # 0) NUESTRO propio par PN<->LID, antes que nada.
        #
        #    Aqui se sembraba el `lid_map` interno de pywhats, porque aquella
        #    libreria buscaba la sesion por la direccion literal y los
        #    mensajes que el usuario escribia desde SU TELEFONO llegaban
        #    dirigidos a nuestro propio LID, no encontraban sesion --que
        #    existia, pero guardada bajo el numero-- y morian con "no session
        #    for peer". Baileys resuelve esa equivalencia por dentro, asi que
        #    ya no hay nada que parchear.
        #
        #    Lo que SI sigue haciendo falta es la fila en `contacts`: de ella
        #    salen los nombres del panel, y nuestra propia conversacion es una
        #    mas. El par no hay que adivinarlo, lo trae el dispositivo vivo.
        self._registrar_par_propio(pywhats_client)

        # 1) Identidad propia, leida del dispositivo VIVO. Un device.json de
        #    una vinculacion anterior daria una identidad caducada y el
        #    backfill se pediria historial a si mismo.
        if self.backfill is not None:
            self.backfill._client = pywhats_client
            self.backfill.refresh_own_identity()
            self._fingerprint = self.backfill.session_fingerprint()

        # 2 y 3) EN PARALELO, y el orden importaba mucho.
        #
        # Los nombres iban DETRAS de la espera del historial. Se midio en un
        # arranque real: la compuerta agoto sus 180 segundos y hasta entonces
        # el panel enseno "Contacto sin nombre" en todas las conversaciones.
        #
        #     20:58:10  Esperando el History Sync inicial (maximo=180s)
        #     21:01:10  Preparacion del historial resuelta en 180.3 s
        #     21:01:11  App-state 'critical_unblock_low': 54 mutaciones  <- nombres
        #
        # Tres minutos de nombres que ya estaban disponibles. Y no dependen
        # una de otra: la agenda vive en el app-state y el historial en sus
        # blobs. Se piden a la vez y cada una llega cuando llega.
        nombres = asyncio.ensure_future(self._sync_contacts(pywhats_client))
        try:
            await self._await_history()
        finally:
            # Se espera igualmente: lo que venga despues cuenta con la agenda
            # puesta. Pero ya ha corrido en paralelo, asi que no cuesta nada.
            await nombres

        # 4) Reconciliar con lo que acabe de entrar.
        self.run_maintenance()

        # 4 quater) Una linea con la situacion del historial. Es lo primero
        #           que se mira al arrancar, y antes habia que deducirlo de
        #           lineas sueltas repartidas por toda la consola.
        self._resumen_de_anclas()

        # 4 bis) Reconciliacion LIGERA si esto es una reconexion.
        #
        # Los mensajes que llegaron con el socket muerto no produjeron ningun
        # evento: darlos por recibidos seria dar por buena una copia con un
        # hueco. Se mira lo reciente, NO se excavan los cuarenta chats: un
        # backfill completo por cada caida de WiFi seria abusivo con el
        # telefono, que es quien atiende las peticiones.
        if self._reconexion:
            await self._reconciliar_tras_reconectar()

        # 5) Worker permanente de multimedia. Va por su cuenta: ni la GUI ni
        #    el backfill esperan por el.
        self._start_media_worker(pywhats_client)

        # 6) Backfill historico. Es el unico que depende del telefono.
        await self._esperar_a_que_asiente()
        await self._run_backfill(pywhats_client)

        # 6 bis) La segunda vinculacion y la recuperacion automatica.
        #
        # Van DESPUES del backfill a proposito: el motor de siempre ya trabajo
        # con lo que habia, y lo que quede esperando ancla es justo lo que la
        # via Web puede resolver. Arrancarlo antes solo pondria a Chromium a
        # competir por el mismo telefono.

        # 7) Mantenimiento periodico mientras la aplicacion siga abierta.
        self._start_maintenance_loop()

        # 8) WATCHING. NO se termina nada: el receptor, el worker de
        #    multimedia, el bus y el SSE siguen vivos esperando cambios. Este
        #    es el estado normal del servicio, no el final del trabajo.
        publicar_estado = getattr(self, "_set_sync_state", None)
        if publicar_estado is not None:
            publicar_estado("WATCHING")

    def _resumen_de_anclas(self) -> None:
        """``waiting=27 exhausted=10 timeout=2 pending=1``, y ya esta.

        Nunca lanza: un resumen no puede cortar el arranque.
        """
        colector = getattr(self, "seed_collector", None)
        if colector is None:
            return
        try:
            datos = colector.resumen()
        except Exception:  # noqa: BLE001 - observar no puede tumbar nada
            log.debug("No se pudo resumir el estado de las anclas")
            return
        get_logger("PLAN_E").info(
            "waiting=%d exhausted=%d timeout=%d pending=%d anclas=%d "
            "conversaciones_con_ancla=%d",
            datos.get("waiting_seed", 0),
            datos.get("exhausted", 0),
            datos.get("timeout", 0),
            datos.get("pending", 0),
            datos.get("seeds_total", 0),
            datos.get("chats_with_seed", 0),
        )

    def _resumen_live(self) -> None:
        """``[LIVE] recibidos=20 propios=8 ...``. Nunca lanza."""
        resumen = getattr(self, "_live_summary", None)
        if resumen is None:
            return
        try:
            get_logger("LIVE").info("%s", resumen())
        except Exception:  # noqa: BLE001 - un resumen no puede cortar el ciclo
            log.debug("No se pudo resumir la recepcion en vivo")

    async def _reconciliar_tras_reconectar(self) -> None:
        """Recupera lo que entro mientras no habia socket. Solo lo reciente.

        No pide historial antiguo: eso es el backfill y va por su lado. Aqui
        solo se vuelve a mirar el estado de los chats que tienen actividad
        reciente, y se despiertan los que hayan conseguido un ancla.
        """
        import asyncio

        log.info("[LIVE] reconexion: reconciliando lo reciente")
        try:
            informe = await asyncio.to_thread(self.run_maintenance)
            log.info("[LIVE] reconciliacion tras reconectar: %s", informe)
        except Exception:  # noqa: BLE001 - reconciliar no puede tumbar la sesion
            log.exception("La reconciliacion tras reconectar fallo; la sesion sigue")

    async def _await_history(self) -> None:
        if self.gate is None:
            return
        from app.services.history_gate import confirm_initial_history

        self.status.history = "sincronizando"
        self._emit_status()
        self._publish("waiting_initial_history", None)

        comenzo = time.monotonic()
        await self.gate.wait()
        espera = time.monotonic() - comenzo

        if self.gate.bootstrap_seen:
            confirm_initial_history(self._database, self._fingerprint, self.gate.chunks)
        log.info("Preparacion del historial resuelta en %.1f s", espera)

        self.status.history = "sincronizado"
        self.status.history_done = True
        self._emit_status()
        self._publish("initial_history_ready", self.gate.chunks)

    async def _sync_contacts(self, pywhats_client: Any) -> None:
        from app.services.contacts_service import fetch_contact_names, resolve_lids_via_usync

        try:
            await fetch_contact_names(pywhats_client)
            await resolve_lids_via_usync(pywhats_client, self._database)
            self._publish("contacts_synced", None)
        except Exception:  # noqa: BLE001 - sin nombres se sigue funcionando
            log.debug("No se pudieron sincronizar los nombres de contacto")

        # Y los GRUPOS, que van por otro camino.
        #
        # El asunto de un grupo no viaja en el app-state ni siempre en el blob
        # de historial. Se le pregunta al servidor, que es la unica fuente que
        # lo tiene siempre. Va aparte y con su propio try: que un grupo no
        # conteste no puede costar los nombres de contacto que ya se
        # resolvieron arriba.
        try:
            from app.services.group_names import resolver_nombres_de_grupo

            await resolver_nombres_de_grupo(
                pywhats_client,
                self._database,
                account_id=self.whatsapp_account_id,
                publish=self._publish,
            )
        except Exception:  # noqa: BLE001 - un grupo sin nombre no para nada
            log.debug("No se pudieron resolver los nombres de grupo")

    def _start_media_worker(self, pywhats_client: Any) -> None:
        from app.services.media_service import MediaService

        def adjunto_listo(detalle: dict[str, Any]) -> None:
            # Un evento POR ADJUNTO: quien escuche sabe exactamente que
            # burbuja repintar, sin recargar la conversacion entera.
            self._publish("media_ready", detalle)

        self.media = MediaService(
            self._settings,
            self._database,
            pywhats_client,
            on_media_ready=adjunto_listo,
        )
        self.status.media_pending = self.media.pending_count()
        self.status.media_done = self.status.media_pending == 0
        self._emit_status()

        def progreso(stats: Any, nuevos: int) -> None:
            self.status.media_pending = self.media.pending_count()
            self.status.media_done = self.status.media_pending == 0
            self._emit_status()
            self._publish("media_downloaded", f"{nuevos} nuevos · {stats}")

        self._spawn(
            self.media.run_forever(
                interval=self._settings.media_worker_interval, on_progress=progreso
            ),
            name="media-worker",
        )

    async def _esperar_a_que_asiente(self) -> None:
        """El margen que le falta a una sesion recuperada. Solo a esa.

        QUE SIGNIFICA "ASENTADA"
        ------------------------
        Cuando se llega aqui ya se cumplio todo lo que se puede comprobar:

        * ``<success>`` del servidor y estado CONECTADO;
        * identidad propia leida del dispositivo VIVO;
        * Signal Store en su sitio y cuenta reconciliada;
        * el historial inicial resuelto —esperado o ya confirmado—;
        * app-state sincronizado (se espera de verdad: ``_sync_contacts``
          hace ``await`` de cada coleccion);
        * la reconciliacion de lo que entro con el socket muerto.

        Lo unico que NO se puede comprobar con una señal es lo que hace el
        telefono por su cuenta al reaparecer un companion. Para eso, y solo
        para eso, queda este margen.

        A una vinculacion NUEVA no se le aplica: ya espero su bootstrap.
        """
        if self.gate is not None and getattr(self.gate, "bootstrap_seen", False):
            return

        desde = getattr(self, "_conectado_en", None)
        if desde is None:
            return
        restante = self.GRACIA_TRAS_RECUPERAR_SESION - (time.monotonic() - desde)
        if restante <= 0:
            return

        log.info(
            "[CAPABILITY] sesion recuperada; se deja asentar %.0fs antes de "
            "probar ON_DEMAND",
            restante,
        )
        await asyncio.sleep(restante)

    async def _run_backfill(self, pywhats_client: Any) -> None:
        if self.backfill is None:
            return
        self.status.backfill = "excavando"
        self._emit_status()
        try:
            if self.backfill.capability_confirmed():
                log.info("Canary omitido: capability ya confirmada para esta sesion")
                ok = True
            else:
                ok = await self.backfill.run_canary(pywhats_client)
                if not ok and self.backfill.last_canary_reason == "sin_candidato":
                    # El canary es un DIAGNOSTICO, no un permiso. Si no llego
                    # a elegir objetivo no ha probado nada, y tratarlo como
                    # "ON_DEMAND no funciona" dejaba sin excavar a chats que
                    # si tenian ancla. Lo unico que bloquea la excavacion es
                    # una prueba que se hizo y no obtuvo respuesta.
                    log.info(
                        "El canary no encontro objetivo, asi que no probo nada. "
                        "Se continua con la excavacion normal."
                    )
                    ok = True
            if ok and self._settings.backfill_all_after_canary:
                await self.backfill.run(pywhats_client)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - el backfill no puede tumbar la app
            log.exception("El backfill fallo; la copia local sigue intacta")
            self.status.backfill = "interrumpido"
            self._emit_status()
            return

        self.status.backfill = "terminado"
        self.status.backfill_done = True
        self._emit_status()
        self._publish("backfill_done", str(self.backfill.stats))

        # Lo que acaba de llegar cambia contadores, cursores y previas.
        self.run_maintenance()
        self._publish("history_ingested", "backfill completado")

        # Y se deja el recuento medido en el log, para poder juzgar una prueba
        # desde cero sin tener que consultar la base a mano.
        from app.services.product_report import log_summary

        informe = log_summary(self._database)
        self._publish(
            "history.backfill.completed",
            informe.to_json() if informe is not None else {},
        )

    def _start_maintenance_loop(self) -> None:
        intervalo = self._settings.maintenance_interval_seconds
        if intervalo <= 0:
            return

        async def bucle() -> None:
            try:
                while True:
                    await asyncio.sleep(intervalo)
                    informe = self.run_maintenance()
                    if informe.changed:
                        self._publish("maintenance_done", str(informe))
                    # Una linea con lo que ha pasado por el receptor desde el
                    # arranque. Sustituye a una linea por mensaje.
                    self._resumen_live()
            except asyncio.CancelledError:
                raise

        self._spawn(bucle(), name="maintenance-loop")

    # -- Utilidades ----------------------------------------------------------

    def _spawn(self, coro: Any, *, name: str) -> None:
        tarea = asyncio.create_task(coro, name=name)
        self._tasks.append(tarea)

    def _emit_status(self) -> None:
        self._publish("status", self.status)

    def stop(self) -> None:
        """Cancela el trabajo de fondo. No cierra la base ni la sesion.

        Solo SENALIZA. Cerrar el cliente desde fuera de su propio event loop
        fue lo que producia el ``Task was destroyed but it is pending`` del
        cierre; esa responsabilidad es de quien es dueno del loop.
        """
        if self._stopping:
            return
        self._stopping = True
        for tarea in self._tasks:
            if not tarea.done():
                tarea.cancel()
        if self.backfill is not None:
            self.backfill.stop()
        log.info("Trabajos de fondo detenidos")
