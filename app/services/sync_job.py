"""El boton "buscar novedades": lo que se puede completar, y lo que no.

QUE ES Y QUE NO ES
------------------
NO es lo que hace llegar los mensajes nuevos: eso ocurre solo, siempre que la
sesion este conectada, por el receptor en vivo.

Y NO es una "sincronizacion total". Eso era lo enganoso de la version
anterior: reconciliaba, revalidaba y excavaba, pero las tres cosas trabajan
sobre conversaciones que YA tienen un ancla. Con 27 esperando una referencia
de WhatsApp, pulsar el boton no podia cambiar nada de ellas por definicion, y
aun asi terminaba diciendo "sincronizacion completada".

QUE HACE AHORA
--------------
1. reconciliar lo derivado (contadores, cursores, previas, alias)
2. BUSCAR ANCLAS NUEVAS -- blobs sin escanear y alias recien aprendidos
3. revalidar que conversaciones pueden pedir historial AHORA
4. pedir historial SOLO de las que tienen ancla y su espera ha vencido
5. recoger la multimedia pendiente
6. asegurar que lo nuevo tiene su trabajo de subida a Drive
7. contar el resultado y decirlo sin adornos

El paso 2 es el que faltaba: es el unico que puede despertar una conversacion
dormida. Y aun asi puede no despertar ninguna, y entonces se dice.

LO QUE NO HACE, A PROPOSITO
---------------------------
* No pide historial de una conversacion sin ancla. No hay forma protocolar de
  hacerlo, y fabricar un cursor produce un ACK y despues silencio.
* No resetea la espera de reintento porque el usuario haya pulsado el boton.
  Si el telefono no contesto hace diez segundos, no contesta mejor por
  insistir; esos chats se informan como ``retry_pending``.

NO EJECUTA SCRIPTS
------------------
Ni ``subprocess``, ni ``os.system``, ni ``probe_chat.py``. Llama a los mismos
servicios que usa el arranque: ``MaintenanceService``, ``BackfillService`` y
``MediaService``. Lanzar un script seria arrancar un segundo proceso que
pelearia por el cerrojo de la sesion con el que ya la tiene.

NO BLOQUEA LA PETICION HTTP
---------------------------
El ciclo puede durar minutos. ``start()`` vuelve enseguida y el trabajo sigue
en el event loop del cliente de WhatsApp, que es donde tiene que estar: el
backfill es asincrono y necesita ESE loop, no uno nuevo.

UNA SOLA A LA VEZ
-----------------
Dos ciclos simultaneos se pisarian los cursores y duplicarian peticiones al
telefono. El estado lo impide y la API lo traduce a un 409.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from app.core.logging_setup import get_logger

log = get_logger("SYNC")

# Estados del ciclo.
IDLE = "idle"
RUNNING = "running"
COMPLETE = "complete"
ERROR = "error"

# Fases, en el orden en que ocurren.
PHASES = (
    "reconcile",   # recalcular lo derivado; nada destructivo
    "seeds",       # buscar anclas nuevas (blobs sin escanear, alias nuevos)
    "web",         # referencias de WhatsApp Web para las que siguen sin ancla
    "revalidate",  # que chats pueden pedir historial AHORA
    "backfill",    # pedir historial SOLO de los que tienen ancla
    "media",       # recoger adjuntos pendientes
    "storage",     # asegurar que lo nuevo tiene su trabajo de subida
    "finalize",
)


@dataclass
class SyncState:
    """Lo que se puede contar de un ciclo. Todo son hechos, no estimaciones."""

    state: str = IDLE
    job_id: str | None = None
    phase: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    chats_total: int = 0
    chats_processed: int = 0
    messages_new: int = 0
    media_pending: int = 0
    last_error: str | None = None
    # Lo que reconcilio la pasada segura, para poder decir que cambio.
    reconciled: str | None = None
    # EL RESULTADO, contado por lo que de verdad paso con cada chat.
    #
    # "Sincronizacion terminada" a secas escondia treinta conversaciones que
    # siguen sin una linea de historial. Un chat que espera semilla NO esta
    # sincronizado, no esta agotado, y no puede desaparecer del resumen.
    synced: int = 0
    waiting_seed: int = 0
    timeouts: int = 0
    errors: int = 0
    pending: int = 0
    fetching: int = 0

    # -- Lo que este ciclo pudo hacer, y lo que no --------------------------
    #
    # "Sincronizacion completada" a secas no distingue "no habia nada nuevo"
    # de "hay 27 conversaciones que no se pueden ni intentar". Son cosas
    # distintas y el usuario merece saber cual es.
    with_cursor: int = 0
    retried: int = 0
    #: Chats con ancla que NO se tocaron porque su espera no habia vencido.
    retry_pending: int = 0
    recovered_messages: int = 0
    new_seeds: int = 0
    #: Conversaciones que llevan varias pasadas sin aparecer. NO se borran:
    #: es una anotacion, no una decision.
    stale_candidates: int = 0
    drive_pending: int = 0
    #: Conversaciones que despertaron gracias a una referencia de WhatsApp Web.
    web_promoted: int = 0
    #: ``"incremental"`` o ``"full"``. Lo que el usuario pidio, para que el
    #: resumen no prometa una revision completa cuando fue la rapida.
    mode: str = "incremental"
    #: Chats en espera de reintento a los que esta revision SI llego, porque
    #: la accion profunda adelanta esa espera una vez.
    retries_reopened: int = 0

    # -- Lo que se puede MEDIR del boton -----------------------------------
    #
    # `new_seeds` cuenta FILAS de ancla insertadas, y resulto enganoso: en una
    # pasada real marco 3410 mientras el numero de conversaciones que pasaron
    # a poder pedir historial era CERO. No habia contradiccion --las 3410 eran
    # anclas de la excavacion de ocho conversaciones que YA funcionaban-- pero
    # el numero grande sugeria un avance que no existia.
    #
    # Lo que responde a "¿ha servido de algo pulsar?" es este bloque: cuantas
    # esperaban antes, cuantas esperan despues, y cuantas cambiaron de estado.
    waiting_before: int = 0
    waiting_after: int = 0
    #: Conversaciones que pasaron de esperar a poder pedir su historial.
    promoted: int = 0
    #: Conversaciones nuevas descubiertas en esta pasada.
    new_chats: int = 0
    #: Conversaciones que entraron a la cola de excavacion.
    backfill_started: int = 0
    #: Blobs releidos de principio a fin en esta pasada (0 = solo los nuevos).
    blobs_rescanned: int = 0
    #: Conversaciones que habia al empezar, para poder restar al final.
    chats_al_empezar: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "job_id": self.job_id,
            "phase": self.phase,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "chats_total": self.chats_total,
            "chats_processed": self.chats_processed,
            "messages_new": self.messages_new,
            "mode": self.mode,
            "web_promoted": self.web_promoted,
            "retries_reopened": self.retries_reopened,
            "media_pending": self.media_pending,
            # El desglose va SIEMPRE, tambien cuando el ciclo termina bien.
            "result": {
                "synced": self.synced,
                "waiting_seed": self.waiting_seed,
                "timeouts": self.timeouts,
                "errors": self.errors,
                "pending": self.pending,
            },
            # El resumen honesto del ciclo: que se pudo hacer y que no.
            "summary": {
                "chats_total": self.chats_total,
                "with_cursor": self.with_cursor,
                "waiting_seed": self.waiting_seed,
                "pending": self.pending,
                "fetching": self.fetching,
                "timeout": self.timeouts,
                "exhausted": self.synced,
                "errors": self.errors,
                "retried": self.retried,
                "retry_pending": self.retry_pending,
                "recovered_messages": self.recovered_messages,
                "new_seeds": self.new_seeds,
                "stale_candidates": self.stale_candidates,
                "drive_pending": self.drive_pending,
            },
            # Lo que de verdad hizo el boton, para poder medirlo. Va aparte
            # del resumen de estado porque responde a otra pregunta: no "como
            # esta la cuenta" sino "que cambio por haber pulsado".
            "recovery": {
                "waiting_before": self.waiting_before,
                "seeds_found": self.new_seeds,
                "promoted": self.promoted,
                "waiting_after": self.waiting_after,
                "new_chats": self.new_chats,
                "messages_added": self.recovered_messages,
                "backfill_started": self.backfill_started,
                "blobs_rescanned": self.blobs_rescanned,
            },
            "last_error": self.last_error,
            "reconciled": self.reconciled,
        }


class SyncAlreadyRunningError(RuntimeError):
    """Ya hay un ciclo en marcha."""


class SyncUnavailableError(RuntimeError):
    """No se puede sincronizar: sin WhatsApp, o sin conexion."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class SyncJob:
    """Ejecuta el ciclo manual y lleva su estado.

    Vive en el runtime, no en la peticion: el estado tiene que sobrevivir a la
    respuesta HTTP para que ``GET /sync/status`` pueda contar como va.
    """

    def __init__(
        self,
        settings: Any,
        database: Any,
        *,
        publish: Callable[..., None] | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._publish = publish or (lambda *_a, **_k: None)
        self._lock = threading.Lock()
        self.state = SyncState()

    # -- Consulta ------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.state.state == RUNNING

    def snapshot(self) -> dict[str, Any]:
        return self.state.to_json()

    # -- Arranque ------------------------------------------------------------

    def start(self, runtime: Any, *, profundo: bool = False) -> str:
        """Lanza el ciclo. Devuelve el ``job_id``.

        :raises SyncUnavailableError: en modo local, o sin conexion.
        :raises SyncAlreadyRunningError: si ya hay uno en marcha.
        """
        self._comprobar_disponible(runtime)

        # El backfill AUTOMATICO tambien cuenta. Se midio el fallo: mientras
        # el del arranque excavaba, un POST /sync/run lanzo un segundo
        # backfill sobre el mismo chat. El telefono atiende las peticiones
        # ON_DEMAND de una en una, y dos respuestas cruzadas ya no se pueden
        # atribuir a la peticion que las pidio.
        backfill = getattr(runtime, "backfill", None)
        if backfill is not None and getattr(backfill, "busy", False):
            raise SyncAlreadyRunningError(
                "Ya hay una excavacion de historial en marcha; espera a que "
                "termine antes de lanzar otra."
            )

        with self._lock:
            if self.running:
                raise SyncAlreadyRunningError(
                    "Ya hay una sincronizacion en curso."
                )
            job_id = uuid.uuid4().hex[:12]
            self.state = SyncState(
                state=RUNNING,
                job_id=job_id,
                phase=PHASES[0],
                started_at=_ahora(),
                mode="full" if profundo else "incremental",
            )

        if profundo:
            # La revision completa adelanta UNA vez la espera de reintento de
            # los chats que la estan cumpliendo. Es lo que el usuario acaba de
            # pedir explicitamente: "vuelve a intentarlo todo ahora".
            #
            # Solo eso. No se borra historial, ni anclas, ni cursores, y NO se
            # reabre ningun chat que el telefono haya dado por terminado: para
            # eso hace falta evidencia nueva, no un boton.
            self.state.retries_reopened = self._adelantar_reintentos()

        self._emitir()
        log.info(
            "Sincronizacion manual iniciada (job=%s modo=%s)",
            job_id,
            self.state.mode,
        )

        # El ciclo va al event loop del cliente: el backfill es asincrono y
        # necesita ESE loop, el mismo que mantiene viva la sesion.
        loop = getattr(runtime.client, "_loop", None)
        if loop is None or loop.is_closed():
            self._terminar_con_error("el cliente de WhatsApp no tiene event loop")
            raise SyncUnavailableError(
                "SESSION_NOT_CONNECTED",
                "La sesion de WhatsApp no esta lista para sincronizar.",
            )
        asyncio.run_coroutine_threadsafe(self._ciclo(runtime), loop)
        return job_id

    def _adelantar_reintentos(self) -> int:
        """Deja vencer YA la espera de los chats que estaban esperando turno.

        Es lo unico que la revision completa hace de mas, y es reversible por
        naturaleza: si el chat vuelve a fallar, se le pone otra espera. No
        toca mensajes, ni anclas, ni cursores, ni conversaciones agotadas.
        """
        from sqlalchemy import update

        from app.models import ChatHistoryState

        try:
            with self._database.transaction() as sesion:
                return int(
                    sesion.execute(
                        update(ChatHistoryState)
                        .where(
                            ChatHistoryState.next_retry_at.isnot(None),
                            ChatHistoryState.history_status.notin_(
                                ("exhausted", "no_valid_cursor")
                            ),
                        )
                        .values(next_retry_at=None)
                    ).rowcount
                    or 0
                )
        except Exception:  # noqa: BLE001 - no poder adelantar no cancela el ciclo
            log.exception("No se pudo adelantar la espera de reintento")
            return 0

    @staticmethod
    def _comprobar_disponible(runtime: Any) -> None:
        from app.core.session_state import AppState

        if not runtime.info().whatsapp_enabled:
            raise SyncUnavailableError(
                "WHATSAPP_DISABLED",
                "El backend esta en modo local y no puede sincronizar WhatsApp.",
            )
        if runtime.state.state is not AppState.CONNECTED:
            # Sin conexion NO se arranca el backfill: pedirle historial al
            # telefono sin sesion solo produce timeouts y ensucia el estado de
            # los chats.
            raise SyncUnavailableError(
                "SESSION_NOT_CONNECTED",
                "WhatsApp no esta conectado; no se puede sincronizar.",
            )

    # -- El ciclo ------------------------------------------------------------

    async def _ciclo(self, runtime: Any) -> None:
        try:
            await self._fase_reconciliar()
            await self._fase_semillas(runtime)
            await self._fase_web(runtime)
            await self._fase_revalidar(runtime)
            await self._fase_backfill(runtime)
            await self._fase_bordes_perdidos(runtime)
            await self._fase_media(runtime)
            await self._fase_almacenamiento(runtime)
            await self._fase_final(runtime)
        except asyncio.CancelledError:
            self._terminar_con_error("sincronizacion cancelada")
            raise
        except Exception as exc:  # noqa: BLE001 - el fallo se reporta, no se traga
            log.exception("La sincronizacion manual fallo")
            self._terminar_con_error(str(exc)[:300])

    async def _fase_reconciliar(self) -> None:
        """Reconciliacion SEGURA. Nunca borra nada."""
        self._fase("reconcile")
        from app.services.maintenance_service import MaintenanceService

        informe = await asyncio.to_thread(
            MaintenanceService(self._database, self._settings).run_all
        )
        self.state.reconciled = str(informe)
        self._emitir()

    async def _fase_semillas(self, runtime: Any) -> None:
        """Buscar anclas NUEVAS. Sin esto el ciclo no puede despertar a nadie.

        Es la parte que faltaba y la que hacia enganoso el boton: reconciliar
        y excavar solo trabaja sobre chats que YA tenian ancla, asi que
        pulsarlo con 27 conversaciones esperando no podia cambiar nada de
        ellas por definicion.

        Aqui se mira si hay blobs sin escanear y si han aparecido alias
        nuevos. Es barato cuando no hay nada: comparar huellas no abre ningun
        archivo.
        """
        self._fase("seeds")

        # El punto de partida se LEE de la base antes de tocar nada. Sin esto
        # no se puede decir si el boton sirvio: "27 esperan" al final no
        # significa nada si no se sabe cuantas esperaban al principio.
        cuenta = getattr(runtime, "runtime_owner_account_id", None)
        await asyncio.to_thread(
            self._refrescar_conteos, getattr(runtime, "backfill", None), account_id=cuenta
        )
        self.state.waiting_before = self.state.waiting_seed
        self.state.chats_al_empezar = self.state.chats_total

        colector = getattr(runtime, "seed_collector", None)
        if colector is None or not getattr(colector, "listo", False):
            return

        antes = int(getattr(colector.metricas, "validas", 0) or 0)
        despertados_antes = int(getattr(colector.metricas, "despertados", 0) or 0)
        try:
            await asyncio.to_thread(self._buscar_semillas, runtime, colector)
        except Exception:  # noqa: BLE001 - buscar anclas no puede tumbar el ciclo
            log.exception("Fallo buscando anclas nuevas")
        self.state.new_seeds = max(
            0, int(getattr(colector.metricas, "validas", 0) or 0) - antes
        )
        self.state.promoted = max(
            0, int(getattr(colector.metricas, "despertados", 0) or 0) - despertados_antes
        )
        self._emitir()

    def _buscar_semillas(self, runtime: Any, colector: Any) -> None:
        """Escanea SOLO los blobs que no se hayan escaneado ya."""
        from app.history.blob_scanner import BlobSeedScanner

        escaner = BlobSeedScanner(
            self._database,
            self._settings,
            account_id=getattr(runtime, "runtime_owner_account_id", None),
        )

        # RELEER TODO cuando queda alguien esperando.
        #
        # Saltarse los blobs ya vistos parecia gratis y no lo era. Un blob se
        # marca como escaneado aunque sus anclas no se hayan podido atribuir a
        # nadie, y eso pasa constantemente al principio: el `INITIAL_BOOTSTRAP`
        # llega ANTES de que existan las filas de conversacion, asi que sus
        # anclas se rechazan por "no se pudo resolver el chat" y el blob queda
        # marcado para siempre.
        #
        # Se midio en la base local: la conversacion 170686312136883@lid
        # llevaba 120 anclas VALIDAS dentro de blobs ya marcados, y seguia en
        # `waiting_seed` sin forma de salir.
        #
        # Y el ahorro no existia: releer los 192 blobs enteros cuesta 0,4
        # segundos, contra 0,1 de comparar huellas. Por una decima de segundo
        # se estaban perdiendo anclas de forma permanente.
        #
        # Cuando no espera nadie se mantiene el camino barato: no hay nada que
        # rescatar y la pasada no tiene por que hacer trabajo.
        rescan = self.state.waiting_before > 0
        if rescan or escaner.hay_blobs_nuevos():
            candidatos, informe = escaner.escanear(solo_nuevos=not rescan, marcar=True)
            if rescan:
                self.state.blobs_rescanned = informe.blobs_nuevos
                log.info(
                    "[PLAN_E] %d conversacion(es) esperando: se releen los %d "
                    "blobs enteros (%d referencias dentro)",
                    self.state.waiting_before,
                    informe.blobs_nuevos,
                    informe.candidatos,
                )
            colector.observe_many(candidatos)
        else:
            log.debug("Ningun blob sin escanear; no hay anclas nuevas que buscar")

        # Y despues, lo que YA estaba guardado y no se estaba usando.
        #
        # Una conversacion puede tener doscientos mensajes reales --traidos por
        # el historial inicial, por excavacion o en vivo-- y aun asi figurar sin
        # ancla propia, porque nadie promovio ninguno de ellos a referencia. Se
        # midio sobre la base real: 23 conversaciones en ese estado.
        #
        # No es una fuente nueva de informacion: es una que ya teniamos. Y
        # sirve para que una conversacion, una vez que tiene mensajes reales, no
        # vuelva a depender del segundo dispositivo nunca mas.
        self._resolver_anclas_propias(runtime, colector)

        # Y por ultimo se vuelve a preguntar por CADA conversacion que espera.
        self._repescar_esperando(colector)

    def _repescar_esperando(self, colector: Any) -> None:
        """Vuelve a evaluar todas las conversaciones que esperan referencia.

        Que una conversacion no tuviera ancla la ultima vez no dice nada sobre
        si la tiene ahora: puede haber llegado un mensaje en vivo, puede
        haberse resuelto un alias entre telefono y LID, o puede que la
        referencia estuviera guardada desde el principio y nadie la mirara.

        No inventa nada. Usa la MISMA funcion que el motor de extraccion para
        elegir ancla, asi que si no hay una referencia real la conversacion se
        queda esperando, que es lo correcto.
        """
        from sqlalchemy import select

        from app.models import Chat, ChatHistoryState

        try:
            with self._database.transaction() as sesion:
                esperando = [
                    fila[0]
                    for fila in sesion.execute(
                        select(ChatHistoryState.chat_id)
                        .join(Chat, Chat.id == ChatHistoryState.chat_id)
                        .where(ChatHistoryState.history_status == "waiting_seed")
                    ).all()
                    if fila[0] is not None
                ]
        except Exception:  # noqa: BLE001 - la repesca es una mejora, no un requisito
            log.exception("No se pudieron listar las conversaciones que esperan")
            return

        if not esperando:
            return

        rescatadas = 0
        for chat_id in esperando:
            try:
                if colector.promote_waiting_chat(chat_id):
                    rescatadas += 1
            except Exception:  # noqa: BLE001 - una conversacion no para al resto
                log.debug("No se pudo reevaluar el chat %s", chat_id, exc_info=True)

        log.info(
            "[PLAN_E] repesca: %d de %d conversacion(es) que esperaban ya "
            "tienen una referencia real con la que pedir historial",
            rescatadas,
            len(esperando),
        )

    def _resolver_anclas_propias(self, runtime: Any, colector: Any) -> None:
        """Promueve a referencia los mensajes reales ya guardados. Nunca lanza."""
        from app.discovery.primary_seed_resolver import PrimarySeedResolver

        try:
            resolutor = PrimarySeedResolver(
                self._database,
                account_id=getattr(runtime, "runtime_owner_account_id", None),
            )
            resolutor.resolver(colector)
        except Exception:  # noqa: BLE001 - una mejora opcional no corta el ciclo
            log.exception("Fallo resolviendo anclas propias")

    async def _fase_web(self, runtime: Any) -> None:
        """Si quedan conversaciones esperando ancla, preguntarle a WhatsApp Web.

        Este es el paso que faltaba para que el boton hiciera el recorrido
        entero. Reconciliar y excavar solo trabajan sobre chats que YA tienen
        ancla; buscar en los blobs solo encuentra lo que WhatsApp ya entrego.
        Una conversacion sin ninguna referencia no podia salir de ahi por mas
        veces que se pulsara.

        Se aplica el MISMO aplicador de la accion manual, con sus mismas
        condiciones: sin ``ON_DEMAND`` confirmado no escribe nada. Y si el Web
        Companion no esta listo, o esta apagado, la fase no es un error: es
        que esa via no esta disponible ahora.
        """
        self._fase("web")
        if self.state.waiting_seed <= 0:
            return
        supervisor = getattr(runtime, "web_companion", None)
        if supervisor is None or not getattr(supervisor, "habilitado", False):
            return

        from app.web_companion.apply import AplicacionRechazada, WebSeedApplier

        try:
            resultado = await asyncio.to_thread(WebSeedApplier(runtime).aplicar)
        except AplicacionRechazada as exc:
            # No es un fallo del ciclo: es una via que hoy no se puede usar.
            log.info("Referencias Web no disponibles: %s", exc)
            return
        except Exception:  # noqa: BLE001 - una via opcional no tumba el ciclo
            log.exception("Fallo aplicando las referencias de WhatsApp Web")
            return

        self.state.new_seeds += resultado.insertadas
        self.state.web_promoted = resultado.promovidos
        if resultado.promovidos:
            log.info(
                "[WEB_SEEDS] %d conversacion(es) pasan a poder pedir su historial",
                resultado.promovidos,
            )
        self._emitir()

    async def _fase_revalidar(self, runtime: Any) -> None:
        """Vuelve a mirar que chats pueden pedir historial."""
        self._fase("revalidate")
        backfill = runtime.backfill
        if backfill is None:
            return
        await asyncio.to_thread(backfill.revalidate_for_new_session)
        self._refrescar_conteos(
            backfill, account_id=getattr(runtime, "runtime_owner_account_id", None)
        )
        self._emitir()

    def _refrescar_conteos(
        self, backfill: Any = None, *, account_id: Any = None
    ) -> None:
        """Relee TODOS los conteos de la base. Una sola fuente.

        Antes cada fase se apanaba con lo que tuviera a mano, y la de
        excavacion acababa leyendo un ``waiting_seed`` que todavia no se habia
        calculado: por eso decia "0 espera(n)" con 26 esperando.
        """
        from app.history.resumen import resumen_de_estado

        try:
            resumen = resumen_de_estado(
                self._database, account_id=account_id, backfill=backfill
            )
        except Exception:  # noqa: BLE001 - contar no puede parar el ciclo
            log.debug("No se pudieron releer los conteos", exc_info=True)
            return
        self.state.chats_total = resumen.chats_total
        self.state.with_cursor = resumen.with_cursor
        self.state.waiting_seed = resumen.waiting_seed
        self.state.pending = resumen.pending
        self.state.fetching = resumen.fetching
        self.state.timeouts = resumen.timeout
        self.state.synced = resumen.exhausted
        self.state.errors = resumen.errors
        self.state.retry_pending = resumen.retry_pending

    async def _fase_backfill(self, runtime: Any) -> None:
        """Historial ON_DEMAND. NO se toca el wire protocol."""
        self._fase("backfill")
        backfill = runtime.backfill
        if backfill is None or runtime.client is None:
            return

        antes = int(getattr(backfill.stats, "messages_new", 0) or 0)
        peticiones_antes = int(getattr(backfill.stats, "requests_sent", 0) or 0)
        cliente = getattr(runtime.client, "_client", None)
        if cliente is None:
            log.warning("Sin cliente de pywhats; se omite el backfill")
            return

        if not self.state.with_cursor:
            # Ni una conversacion con ancla: no hay NADA que pedir. Arrancar
            # el motor para que recorra la lista y no envie una sola peticion
            # solo sirve para que el resumen parezca que hizo algo.
            #
            # Los dos numeros salen de la MISMA lectura, hecha en la fase
            # anterior. Antes este mensaje leia un contador que se rellenaba
            # DESPUES, y por eso decia "0 espera(n)" con 26 esperando.
            log.info(
                "No hay conversaciones con una referencia valida para pedir "
                "historial. %d sigue(n) esperando una referencia de WhatsApp.",
                self.state.waiting_seed,
            )
            return

        await backfill.run(cliente)

        despues = int(getattr(backfill.stats, "messages_new", 0) or 0)
        self.state.messages_new = max(0, despues - antes)
        self.state.recovered_messages = self.state.messages_new
        self.state.retried = max(
            0, int(getattr(backfill.stats, "requests_sent", 0) or 0) - peticiones_antes
        )
        self.state.chats_processed = int(
            getattr(backfill.stats, "chats_processed", 0) or 0
        )
        self.state.backfill_started = self.state.chats_processed
        self._emitir()
        self._publish("backfill_progress", self.snapshot())

    async def _fase_almacenamiento(self, runtime: Any) -> None:
        """Que lo recien traido tenga su trabajo de subida, y contar lo que falta.

        No sube nada aqui ni toca el formato: de eso se encarga el worker de
        siempre. Esto solo se asegura de que no queda un mensaje nuevo sin
        encolar y deja el numero en el resumen.
        """
        self._fase("storage")
        almacen = getattr(runtime, "storage", None)
        usuario = getattr(runtime, "runtime_owner_user_id", None)
        cuenta = getattr(runtime, "runtime_owner_account_id", None)
        if almacen is None or not getattr(almacen, "habilitado", False):
            return
        if usuario is None or cuenta is None:
            return
        try:
            if callable(getattr(almacen, "encolar_pendientes", None)):
                await asyncio.to_thread(
                    almacen.encolar_pendientes, user_id=usuario, account_id=cuenta
                )
            self.state.drive_pending = await asyncio.to_thread(self._sin_subir)
        except Exception:  # noqa: BLE001 - el almacenamiento no corta el ciclo
            log.debug("No se pudo revisar el estado de subida", exc_info=True)
        self._emitir()

    def _sin_subir(self) -> int:
        """Mensajes que todavia no estan confirmados en Drive.

        ``ready`` es el estado final del pipeline: el segmento esta cerrado,
        cifrado y subido. Todo lo demas —``local``, ``pending``,
        ``uploading``, ``failed``— sigue pendiente de alguna manera.
        """
        from sqlalchemy import func, select

        from app.models import Message

        try:
            with self._database.transaction() as session:
                return int(
                    session.execute(
                        select(func.count())
                        .select_from(Message)
                        .where(Message.storage_status != "ready")
                    ).scalar()
                    or 0
                )
        except Exception:  # noqa: BLE001
            return 0

    async def _fase_bordes_perdidos(self, runtime: Any) -> None:
        """Cierra los agujeros de los mensajes en vivo indescifrables.

        Va DESPUES de la excavacion normal y usa otro motor: aquella baja
        desde el ancla mas antigua, y lo que falta aqui esta por arriba. No
        reabre ningun ``exhausted``.
        """
        backfill = getattr(runtime, "backfill", None)
        cliente = getattr(getattr(runtime, "client", None), "_client", None)
        if backfill is None or cliente is None:
            return
        try:
            from app.services.missed_live_filler import MissedLiveFiller

            resumen = await MissedLiveFiller(self._database, backfill).cerrar_pendientes(
                cliente
            )
        except Exception:  # noqa: BLE001 - una via de rescate no tumba el ciclo
            log.exception("Fallo cerrando los bordes de mensajes perdidos")
            return
        self.state.recovered_messages += int(resumen.get("mensajes", 0) or 0)

    async def _fase_media(self, runtime: Any) -> None:
        """Recoge lo que quede pendiente de descargar."""
        self._fase("media")
        media = runtime.orchestrator.media if runtime.orchestrator else None
        if media is None:
            return
        if media.pending_count():
            await media.run()
        self.state.media_pending = media.pending_count()
        self._emitir()

    def _anotar_ausencias(self, runtime: Any) -> None:
        """Deja constancia de que conversaciones aparecieron en esta pasada.

        NO borra ninguna. Una foto de WhatsApp puede venir incompleta --se
        midieron 41 y 39 conversaciones en dos arranques de la misma cuenta--
        y actuar sobre una sola ausencia convertiria un hueco temporal en una
        perdida de historial.

        Aqui solo se anota. Hacen falta varias ausencias seguidas para llamar
        dudosa a una conversacion, y volver a aparecer lo deshace.
        """
        from app.services.ghost_chats import anotar_snapshot

        vistos = getattr(self, "_vistos_en_la_pasada", None)
        if not vistos:
            # SIN FOTO NO SE ANOTA NADA, y es deliberado.
            #
            # Hoy la unica fuente de una lista COMPLETA de conversaciones es
            # el inventario del segundo dispositivo (apagado por defecto) o un
            # `INITIAL_BOOTSTRAP`. Derivarla de la propia base seria circular:
            # todas las conversaciones apareceran siempre, y no se anotaria ni
            # una ausencia jamas.
            #
            # Quien produzca una foto de verdad la deja en
            # `_vistos_en_la_pasada` antes de la fase final. Mientras no la
            # haya, esto no hace nada, que es lo correcto: castigar a las
            # conversaciones porque la pasada no trajo lista es justo el error
            # que esta capa evita.
            log.debug("[SYNC] sin foto de conversaciones: no se anotan ausencias")
            return
        try:
            with self._database.transaction() as sesion:
                resultado = anotar_snapshot(
                    sesion,
                    vistos,
                    account_id=getattr(runtime, "runtime_owner_account_id", None),
                )
            with self._lock:
                self.state.stale_candidates = resultado.dudosas_totales
        except Exception:  # noqa: BLE001 - anotar no puede tumbar el ciclo
            log.exception("No se pudieron anotar las ausencias de la pasada")

    async def _fase_final(self, runtime: Any) -> None:
        """Segunda reconciliacion: lo recien traido cambia contadores."""
        self._fase("finalize")
        await asyncio.to_thread(self._anotar_ausencias, runtime)
        from app.services.maintenance_service import MaintenanceService

        await asyncio.to_thread(
            MaintenanceService(self._database, self._settings).run_all
        )

        # El recuento se hace AL FINAL y sobre la base, no sobre lo que el
        # backfill creyo hacer: si un chat sigue esperando semilla, tiene que
        # aparecer en el resumen, no desaparecer detras de un "terminado".
        # El recuento se hace AL FINAL y sobre la base, con la MISMA funcion
        # que usan las demas fases: si un chat cambio de estado por otra via
        # —una semilla en vivo, por ejemplo— el resumen refleja el final.
        await asyncio.to_thread(
            self._refrescar_conteos,
            getattr(runtime, "backfill", None),
            account_id=getattr(runtime, "runtime_owner_account_id", None),
        )
        # El cierre del recuento: cuantas siguen esperando DESPUES de todo lo
        # que esta pasada pudo hacer. Junto con `waiting_before` es lo unico
        # que contesta si pulsar sirvio de algo.
        self.state.waiting_after = self.state.waiting_seed
        self.state.new_chats = max(
            0, self.state.chats_total - (self.state.chats_al_empezar or 0)
        )

        with self._lock:
            self.state.state = COMPLETE
            self.state.phase = None
            self.state.finished_at = _ahora()

        # UNA linea con lo que de verdad paso. Es lo que se lee para saber si
        # el ciclo sirvio de algo.
        # `anclas_nuevas` cuenta FILAS de ancla insertadas, no conversaciones
        # rescatadas: en una pasada real marco 3410 mientras `con_ancla` era 0,
        # porque las 3410 salieron de excavar ocho conversaciones que ya
        # funcionaban. Por eso al lado va SIEMPRE `promovidas`, que es la que
        # responde a si el ciclo desatasco algo.
        log.info(
            "complete chats=%d con_ancla=%d esperando=%d (antes %d) "
            "promovidas=%d reintentos=%d "
            "recuperados=%d anclas_nuevas=%d drive_pendiente=%d",
            self.state.chats_total,
            self.state.with_cursor,
            self.state.waiting_seed,
            self.state.waiting_before,
            self.state.promoted,
            self.state.retried,
            self.state.recovered_messages,
            self.state.new_seeds,
            self.state.drive_pending,
        )
        if self.state.retry_pending:
            log.info(
                "%d conversacion(es) tienen ancla pero su espera de reintento "
                "no ha vencido; NO se les ha pedido nada.",
                self.state.retry_pending,
            )
        if self.state.waiting_seed:
            log.info(
                "%d conversacion(es) siguen SIN historial: WhatsApp aun no ha "
                "dado una referencia con la que pedirlo. NO estan vacias, NO "
                "estan sincronizadas, y esto NO es un error.",
                self.state.waiting_seed,
            )
        self._emitir()

    # -- Utilidades ----------------------------------------------------------

    def _fase(self, nombre: str) -> None:
        self.state.phase = nombre
        log.info("Sincronizacion: fase %s", nombre)
        self._emitir()

    def _terminar_con_error(self, mensaje: str) -> None:
        with self._lock:
            self.state.state = ERROR
            self.state.phase = None
            self.state.last_error = mensaje
            self.state.finished_at = _ahora()
        self._emitir()

    def _emitir(self) -> None:
        self._publish("sync_progress", self.snapshot())


def _ahora() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()
