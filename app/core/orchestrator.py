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
    ) -> None:
        self._settings = settings
        self._database = database
        self._client = client
        self._publish = publish or (lambda _name, _payload: None)

        self.status = RuntimeStatus()
        self.maintenance: Any = None
        # ``post_connect`` corre tambien al reconectar. La primera vez no hay
        # nada que reconciliar (se acaba de arrancar); las siguientes si.
        self._reconexion = False
        self.media: Any = None
        self.backfill: Any = None
        self.gate: Any = None

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

        signal_file = getattr(self._settings, "signal_store_file", None)
        report.signal_store = bool(signal_file is not None and signal_file.exists())

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

    async def post_connect(self, pywhats_client: Any) -> None:
        """Secuencia completa tras el ``<success>`` del servidor.

        Se ejecuta tambien despues de RECONECTAR, y eso importa: mientras el
        socket estuvo muerto no llego ni un evento, asi que lo que ocurriera
        en ese rato no se puede dar por recibido. Al volver se reconcilia.
        """
        self.status.connection = "Conectado"
        self.status.connected = True
        self._emit_status()

        # 0) El par PN<->LID propio, antes que nada. En una vinculacion nueva
        #    la siembra del arranque no pudo hacerse (no habia ni device.json
        #    ni Signal Store), y sin ella los mensajes que el usuario envia
        #    desde el telefono llegan y no se descifran.
        sembrar = getattr(self, "_on_own_identity_ready", None)
        if sembrar is not None:
            try:
                sembrar("post_connect")
            except Exception:  # noqa: BLE001 - no puede tumbar la conexion
                log.exception("Fallo registrando el par PN<->LID propio")

        # 1) Identidad propia, leida del dispositivo VIVO. Un device.json de
        #    una vinculacion anterior daria una identidad caducada y el
        #    backfill se pediria historial a si mismo.
        if self.backfill is not None:
            self.backfill._client = pywhats_client
            self.backfill.refresh_own_identity()
            self._fingerprint = self.backfill.session_fingerprint()

        # 2) Preparacion del historial. Si esta sesion ya recibio su
        #    bootstrap, no se esperan 180 segundos a algo que no va a llegar.
        await self._await_history()

        # 3) Nombres de la agenda y correspondencia telefono <-> LID.
        await self._sync_contacts(pywhats_client)

        # 4) Reconciliar con lo que acabe de entrar.
        self.run_maintenance()

        # 4 ter) Veredicto del app-state. Para cuando llega aqui ya paso su
        #        sincronizacion, asi que el recuento esta completo. Es SOLO
        #        una medicion: no siembra, no encola y no pide historial.
        try:
            from app.compat import appstate_seeds

            appstate_seeds.log_summary()
        except Exception:  # noqa: BLE001 - un resumen no puede cortar nada
            log.debug("No se pudo resumir la busqueda de anclas en app-state")

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
        await self._run_backfill(pywhats_client)

        # 7) Mantenimiento periodico mientras la aplicacion siga abierta.
        self._start_maintenance_loop()

        # 8) WATCHING. NO se termina nada: el receptor, el worker de
        #    multimedia, el bus y el SSE siguen vivos esperando cambios. Este
        #    es el estado normal del servicio, no el final del trabajo.
        publicar_estado = getattr(self, "_set_sync_state", None)
        if publicar_estado is not None:
            publicar_estado("WATCHING")

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
