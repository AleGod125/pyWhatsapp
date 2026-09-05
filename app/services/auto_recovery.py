"""Que la recuperación ocurra sola, sin herramientas de diagnóstico.

QUE AUTOMATIZA
--------------
Hasta ahora, una conversación sin ancla sólo salía de ``waiting_seed`` si
alguien pulsaba «Probar cobertura Web» y después «Usar referencias Web». Eso
funcionaba, pero era un flujo de laboratorio: el usuario tenía que saber que
esos botones existían y en qué orden.

Aquí se hace lo mismo, con las MISMAS piezas, cuando se dan las condiciones::

    hay conversaciones esperando ancla
    + el Web Companion está listo
    + ON_DEMAND está confirmado
    -> sondear -> validar -> aplicar -> encolar

No hay un segundo camino. Se llama a ``WebSeedApplier``, que a su vez llama a
``RecentSeedCollector``, que es el recolector de siempre. Si algún día cambia
la validación, cambia para los dos.

LO QUE NO HACE
--------------
No inventa condiciones nuevas. Si ON_DEMAND no está confirmado, ESPERA: aplicar
22 referencias con el motor mudo produce 22 esperas agotadas y después no hay
forma de saber si el ancla era mala o si el teléfono estaba dormido. Y si el
Web Companion no está listo tampoco es un error: es una vía que ahora no está
disponible, y se vuelve a mirar más tarde.

POR QUE UN BUCLE Y NO UN EVENTO
-------------------------------
Las condiciones se cumplen en momentos distintos y en hilos distintos: el
bootstrap termina en el bucle del cliente, el Web Companion avisa desde el
proceso de Node, y la capacidad se confirma cuando el teléfono contesta una
petición. Esperar a que las tres coincidan en un evento concreto es frágil;
mirar cada poco es aburrido y funciona.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("PLAN_E")

#: Cada cuánto se vuelven a mirar las condiciones.
INTERVALO_SEGUNDOS = 30.0

#: Cuánto se espera, como mucho, a que el Web Companion se vincule antes de
#: dejar de intentarlo en este arranque. Un QR sin escanear no se escanea solo.
ESPERA_MAXIMA_WEB = 900.0


@dataclass
class EstadoDeRecuperacion:
    """Lo que el panel necesita para contar qué está pasando."""

    #: La máquina de estados del onboarding, en runtime. Sin migración: esto
    #: describe un momento, no un dato que haya que conservar.
    fase: str = "not_started"
    #: Lo que ya se aplicó por la vía automática.
    seeds_aplicadas: int = 0
    chats_promovidos: int = 0
    #: Conversaciones que existian en WhatsApp y aqui no.
    chats_descubiertos: int = 0
    #: Por qué no se está aplicando ahora, si es que no.
    motivo_espera: str | None = None
    intentos: int = 0
    ultimo_intento: float | None = None
    #: Cuántas veces hizo falta que una persona hiciera algo. En una prueba
    #: limpia ideal son dos: los dos códigos QR.
    intervenciones_manuales: int = 0
    momentos: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "phase": self.fase,
            "seeds_applied": self.seeds_aplicadas,
            "chats_promoted": self.chats_promovidos,
            "chats_discovered": self.chats_descubiertos,
            "waiting_reason": self.motivo_espera,
            "attempts": self.intentos,
            "manual_interventions": self.intervenciones_manuales,
        }


class AutoRecovery:
    """Vigila las condiciones y dispara la recuperación cuando se dan."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self.estado = EstadoDeRecuperacion()
        self._tarea: asyncio.Task | None = None
        self._parar = False
        self._arranque = time.monotonic()
        #: Conversaciones que ya se intentaron aplicar sin éxito, para no
        #: repetir el sondeo cada treinta segundos sobre lo mismo.
        self._ultima_huella: tuple[int, str] | None = None
        #: Rondas seguidas que no consiguieron ni una referencia.
        self._rondas_secas = 0
        #: Cuando quedo lista la sesion Web. La ventana se cuenta desde ahi,
        #: no desde que arranco el proceso.
        self._web_lista_desde: float | None = None
        #: Cuantas esperaban la ultima vez que se dijo. Para no repetir la
        #: misma linea cada treinta segundos mientras nada cambia.
        self._ultimo_aviso_de_espera: int | None = None

    # -- Ciclo de vida -------------------------------------------------------

    def start(self, loop: Any) -> bool:
        """Arranca el vigilante en el bucle del cliente. Idempotente."""
        if self._tarea is not None and not self._tarea.done():
            return True
        if loop is None or loop.is_closed():
            return False
        self._parar = False
        try:
            self._tarea = asyncio.run_coroutine_threadsafe(self._vigilar(), loop)  # type: ignore[assignment]
        except Exception:  # noqa: BLE001 - no poder vigilar no tumba nada
            log.debug("No se pudo arrancar la recuperacion automatica")
            return False
        log.info(
            "Recuperacion automatica en marcha: se revisara cada %.0fs",
            INTERVALO_SEGUNDOS,
        )
        return True

    def stop(self) -> None:
        self._parar = True

    async def _vigilar(self) -> None:
        while not self._parar:
            try:
                await self._una_vuelta()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - una vuelta mala no para el resto
                log.exception("Fallo en la revision automatica de recuperacion")
            await asyncio.sleep(INTERVALO_SEGUNDOS)

    # -- Una vuelta ----------------------------------------------------------

    async def _una_vuelta(self) -> None:
        # 1) Si la tanda se paró porque el teléfono no respondía, mirar si ya
        #    se puede continuar. Reanudar es barato y no pide nada al
        #    teléfono: sólo levanta la pausa si la sesión está viva.
        self._reanudar_si_se_puede()

        # 2) Si la capacidad quedó en duda, volver a probarla cuando toque.
        #    Sin esto un solo timeout —el teléfono apagado un minuto— dejaba
        #    la recuperación parada hasta que alguien reiniciara el servicio.
        await self._reintentar_capacidad()

        # 3) Y si queda alguien esperando ancla, ver si la vía Web puede dársela.
        await self._aplicar_si_procede()

        # 4) Y lo que ya tiene con qué seguir, que siga. Sin botón.
        self._continuar_lo_que_puede_seguir()

    def _continuar_lo_que_puede_seguir(self) -> None:
        """Vuelve a encolar las conversaciones que pueden dar más historial.

        POR QUE HACE FALTA
        ------------------
        Se midió: conversaciones que se quedaban a medias y sólo avanzaban
        cuando el usuario pulsaba «Recuperar historial completo». Que el
        trabajo continúe no puede depender de que alguien mire la pantalla.

        La decisión NO se toma aquí. La toma :mod:`app.history.decision`, que
        es una tabla determinista y se puede leer entera. Aquí se pregunta y
        se encola: el vigilante mira y delega, como con las referencias.

        No reabre nada terminal: un chat agotado necesita evidencia nueva —una
        referencia que antes no existía—, no que pase el tiempo.
        """
        from app.history.decision import chats_que_pueden_seguir

        cola = getattr(self._runtime, "seed_queue", None)
        backfill = getattr(self._runtime, "backfill", None)
        if cola is None or backfill is None:
            return
        if getattr(backfill, "busy", False):
            # El teléfono atiende de una en una: encolar ahora sólo adelanta
            # trabajo que va a esperar igual.
            return

        try:
            seguir, motivos = chats_que_pueden_seguir(
                self._runtime.database, capacidad=backfill.capability_state()
            )
        except Exception:  # noqa: BLE001 - decidir no puede tumbar el vigilante
            log.exception("No se pudo decidir que conversaciones siguen")
            return

        if not seguir:
            return
        encolados = cola.enqueue(seguir)
        if encolados:
            log.info(
                "[AUTO] %d conversacion(es) siguen recuperando historial solas %s",
                len(encolados),
                motivos,
            )

    async def _reintentar_capacidad(self) -> None:
        """Vuelve a probar ON_DEMAND cuando venza la espera.

        La regla de seguridad no cambia: sigue haciendo falta una respuesta
        real y correlacionada. Lo que cambia es que la prueba se repite sola,
        con espera creciente, en vez de quedarse esperando a que alguien
        reinicie el servicio.
        """
        from app.core.primary import primary_ready

        if not primary_ready(self._runtime):
            return
        backfill = getattr(self._runtime, "backfill", None)
        if backfill is None or not backfill.toca_reintentar_canary():
            return

        cliente = getattr(getattr(self._runtime, "client", None), "_client", None)
        if cliente is None:
            return

        antes = backfill.capability_state()
        try:
            await backfill.run_canary(cliente)
        except Exception:  # noqa: BLE001 - una prueba no puede tumbar el vigilante
            log.exception("El reintento de la prueba de capacidad fallo")
            return

        if backfill.capability_state() == "CONFIRMED" and antes != "CONFIRMED":
            # A partir de aqui la vuelta normal ya aplica lo que estuviera
            # esperando. No hace falta boton ni reinicio.
            log.info("[CAPABILITY] auto_recovery reanudado")
            self.estado.momentos.setdefault("capability_confirmed_at", time.time())

    def _reanudar_si_se_puede(self) -> None:
        """Continúa una recuperación pausada, sin que nadie pulse nada.

        El teléfono se despierta solo cuando el usuario lo usa, y esperar a
        que además vuelva a la aplicación y pulse «Reintentar» es pedirle dos
        cosas cuando basta con una.
        """
        cola = getattr(self._runtime, "seed_queue", None)
        if cola is None or not getattr(cola, "pausada", False):
            return
        if cola.reanudar():
            log.info("[BACKFILL] recuperacion reanudada automaticamente")

    async def _aplicar_si_procede(self) -> None:
        motivo = self._por_que_no()
        self.estado.motivo_espera = motivo
        if motivo is not None:
            self.estado.fase = self._fase_por_motivo(motivo)
            return

        if not self._toca_sondear():
            return

        self.estado.fase = "recovering_history"
        self.estado.intentos += 1
        self.estado.ultimo_intento = time.time()

        # PRIMERO el indice completo: que conversaciones existen y cual es el
        # ultimo mensaje real de cada una. Antes se preguntaba solo por las que
        # ya estaban en la base esperando ancla, asi que lo que la sesion
        # principal nunca descubrio no aparecia por ningun lado.
        await self._refrescar_indice()

        from app.web_companion.apply import AplicacionRechazada, WebSeedApplier

        try:
            resultado = await asyncio.to_thread(WebSeedApplier(self._runtime).aplicar)
        except AplicacionRechazada as exc:
            # No es un fallo: es una condición que ahora no se cumple.
            log.info("Recuperacion automatica en espera: %s", exc)
            self.estado.motivo_espera = exc.code
            self.estado.fase = self._fase_por_motivo(exc.code)
            return
        except Exception:  # noqa: BLE001 - una via opcional no tumba nada
            log.exception("La recuperacion automatica fallo")
            return

        self._anotar_ronda(resultado.promovidos)
        self.estado.seeds_aplicadas += resultado.insertadas
        self.estado.chats_promovidos += resultado.promovidos
        if resultado.promovidos:
            log.info(
                "[AUTO] %d conversacion(es) recuperan su historial sin que nadie "
                "haya pulsado nada",
                resultado.promovidos,
            )
            self.estado.momentos.setdefault("web_applied_at", time.time())

    async def _refrescar_indice(self) -> None:
        """Actualiza el inventario de conversaciones desde WhatsApp Web.

        Es la parte de descubrimiento: crea las que faltaban, refresca nombres
        y entrega anclas. Si falla, no pasa nada grave -- la aplicacion sigue
        con lo que ya tenia y la aplicacion de referencias se intenta igual.
        """
        from app.web_companion.inventory import (
            InventarioNoDisponible,
            WebInventoryService,
        )

        try:
            resultado = await asyncio.to_thread(
                WebInventoryService(self._runtime).refrescar
            )
        except InventarioNoDisponible as exc:
            log.debug("Indice Web no disponible: %s", exc)
            return
        except Exception:  # noqa: BLE001 - indexar no puede tumbar el vigilante
            log.exception("El indice de WhatsApp Web fallo")
            return

        self.estado.chats_descubiertos += resultado.creados
        self.estado.seeds_aplicadas += resultado.validos
        self.estado.chats_promovidos += resultado.promovidos

    # -- Ventana de hidratacion ----------------------------------------------
    #
    # MEDIDO, no supuesto. En una sesion de WhatsApp Web recien vinculada:
    #
    #     t+108s   32 conversaciones visibles, 6 con mensajes materializados
    #     t+138s   26 visibles, 0 con mensajes
    #     t+16min   0 visibles (la pagina se estaba recargando sola)
    #
    # Y en una sesion vinculada el dia anterior: 22 de 25. La diferencia no
    # esta en el codigo: WhatsApp Web va materializando los mensajes poco a
    # poco, y una foto tomada a los dos minutos es una foto de casi nada.
    #
    # Antes se sondeaba una vez y, si el numero de conversaciones esperando no
    # cambiaba, no se volvia a intentar. Eso convertia el primer sondeo -- el
    # peor de todos -- en el definitivo.

    #: Cuanto se sigue intentando desde que la sesion Web quedo lista.
    VENTANA_SEGUNDOS = 30 * 60.0

    #: Rondas seguidas sin conseguir ni una referencia antes de parar. No se
    #: para al primer cero: la hidratacion va a saltos.
    RONDAS_SIN_MEJORA = 6

    def _toca_sondear(self) -> bool:
        """Si vale la pena volver a preguntarle a WhatsApp Web.

        Se insiste mientras la ventana siga abierta Y no se lleven demasiadas
        rondas seguidas sin conseguir nada. Cuando algo cambia -- llegan
        conversaciones nuevas, o la anterior dio resultado -- se reinicia la
        cuenta: significa que todavia esta hidratandose.
        """
        huella = self._huella()
        if huella != self._ultima_huella:
            # Cambio el panorama: merece la pena mirar otra vez.
            self._ultima_huella = huella
            self._rondas_secas = 0
            return True
        if self._rondas_secas >= self.RONDAS_SIN_MEJORA:
            return False
        return time.monotonic() - self._inicio_ventana() < self.VENTANA_SEGUNDOS

    def _inicio_ventana(self) -> float:
        """Desde cuando se cuenta la ventana: desde que la sesion Web esta lista."""
        if self._web_lista_desde is None:
            self._web_lista_desde = time.monotonic()
        return self._web_lista_desde

    def _anotar_ronda(self, promovidos: int) -> None:
        """Una ronda mas. Si no aporto nada, cuenta para el limite."""
        self._ultima_huella = self._huella()
        if promovidos > 0:
            self._rondas_secas = 0
            log.info(
                "[WEB] hidratacion: +%d referencia(s) a los %.0fs de estar lista",
                promovidos,
                time.monotonic() - self._inicio_ventana(),
            )
        else:
            self._rondas_secas += 1
            if self._rondas_secas == self.RONDAS_SIN_MEJORA:
                log.info(
                    "[WEB] hidratacion agotada: %d rondas seguidas sin ninguna "
                    "referencia nueva. Se deja de insistir.",
                    self._rondas_secas,
                )

    # -- Condiciones ---------------------------------------------------------

    def _por_que_no(self) -> str | None:
        """El motivo por el que ahora no se aplica, o ``None`` si se puede."""
        if self._esperando_ancla() == 0:
            return "NOTHING_WAITING"

        # La conexion principal manda. Y no basta con que haya un cliente:
        # un emparejamiento a medias deja objeto pero no identidad, y con eso
        # no se puede pedir nada ni descifrar nada.
        from app.core.primary import primary_ready

        if not primary_ready(self._runtime):
            return "PRIMARY_NOT_READY"

        # El bootstrap inicial y la vía Web piden lo mismo al mismo teléfono.
        # Correr a la vez sólo consigue que las dos vayan peor.
        puerta = getattr(self._runtime, "gate", None)
        if puerta is not None and not self._historial_inicial_listo(puerta):
            return "INITIAL_SYNC_RUNNING"

        supervisor = getattr(self._runtime, "web_companion", None)
        if supervisor is None or not getattr(supervisor, "habilitado", False):
            return "WEB_COMPANION_DISABLED"
        if not getattr(supervisor, "vivo", False):
            return "WEB_COMPANION_NOT_RUNNING"
        if not supervisor.snapshot().get("web_client_ready"):
            return "WEB_COMPANION_NOT_READY"
        # A partir de aqui la sesion Web esta lista: es cuando empieza a
        # contar la ventana de hidratacion.
        self._inicio_ventana()

        backfill = getattr(self._runtime, "backfill", None)
        if backfill is None:
            return "SESSION_NOT_CONNECTED"
        if backfill.capability_state() != "CONFIRMED":
            # Se espera, no se fuerza. La capacidad la confirma una respuesta
            # real correlacionada, nunca un ACK.
            #
            # Y lo que la via Web ya encontro NO se pierde: no se ha aplicado
            # todavia, se vuelve a proponer en cada vuelta y se aplicara entera
            # en cuanto la capacidad se confirme. Se dice cuantas son para que
            # no parezca que el trabajo se tiro.
            esperando = self._esperando_ancla()
            # Una vez por cambio, no cada treinta segundos: esto se consulta
            # en cada vuelta y repetir la misma linea entierra el resto.
            if esperando and esperando != self._ultimo_aviso_de_espera:
                self._ultimo_aviso_de_espera = esperando
                log.info(
                    "[CAPABILITY] %d conversacion(es) esperan a que ON_DEMAND "
                    "vuelva a responder; sus referencias no se pierden",
                    esperando,
                )
            return "ON_DEMAND_NOT_CONFIRMED"
        return None

    @staticmethod
    def _historial_inicial_listo(puerta: Any) -> bool:
        """Si el bootstrap ya se asento, o si nunca llego a empezar.

        ``InitialHistoryGate.settled()`` solo es cierto DESPUES de haber visto
        un ``INITIAL_BOOTSTRAP``. En una sesion ya sincronizada ese bootstrap
        no vuelve a llegar nunca, asi que esperar a que se asiente seria
        esperar para siempre: si no se vio ninguno, no hay nada con lo que
        competir.
        """
        try:
            if not getattr(puerta, "bootstrap_seen", False):
                return True
            return bool(puerta.settled())
        except Exception:  # noqa: BLE001 - no poder preguntarlo no bloquea
            return True

    def _esperando_ancla(self) -> int:
        from sqlalchemy import func, select

        from app.models import SEEDLESS_STATUSES, ChatHistoryState

        try:
            with self._runtime.database.transaction() as sesion:
                return int(
                    sesion.execute(
                        select(func.count())
                        .select_from(ChatHistoryState)
                        .where(ChatHistoryState.history_status.in_(SEEDLESS_STATUSES))
                    ).scalar()
                    or 0
                )
        except Exception:  # noqa: BLE001 - contar no puede tumbar el vigilante
            return 0

    def _huella(self) -> tuple[int, str]:
        """Qué había la última vez que se intentó.

        Si no ha cambiado el número de conversaciones esperando ni la sesión,
        volver a sondear daría el mismo resultado.
        """
        backfill = getattr(self._runtime, "backfill", None)
        sesion = ""
        if backfill is not None:
            try:
                sesion = str(backfill.session_fingerprint() or "")
            except Exception:  # noqa: BLE001
                sesion = ""
        return (self._esperando_ancla(), sesion)

    @staticmethod
    def _fase_por_motivo(motivo: str) -> str:
        return {
            "NOTHING_WAITING": "complete",
            "SESSION_NOT_CONNECTED": "waiting_primary",
            # Sin conexion principal no hay nada que hacer, y sobre todo no
            # hay que pedir el segundo codigo: el que falta es el primero.
            "PRIMARY_NOT_READY": "pairing_primary",
            "INITIAL_SYNC_RUNNING": "initial_sync",
            "WEB_COMPANION_DISABLED": "partial",
            "WEB_COMPANION_NOT_RUNNING": "pairing_web",
            "WEB_COMPANION_NOT_READY": "waiting_web",
            "ON_DEMAND_NOT_CONFIRMED": "initial_sync",
        }.get(motivo, "recovering_history")
