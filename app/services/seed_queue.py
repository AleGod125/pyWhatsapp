"""Cuando un chat dormido consigue su ancla, excavarlo. Solo a el.

EL PROBLEMA, MEDIDO
-------------------
"Juan Andrés" llevaba en ``waiting_seed`` sin un solo mensaje. Llegaron tres
mensajes reales el 2 de septiembre, la semilla aparecio y el estado paso a
``pending``... y ahi se quedo. En el telefono ese chat tiene historial
anterior, pero nadie lo pidio: el backfill solo mira los chats al arrancar y
en el ciclo manual, asi que la semilla se quedaba esperando horas a que
alguien lanzara una sincronizacion.

QUE HACE ESTA COLA
------------------
Al aparecer una semilla, encola ESE chat y lo excava en cuanto puede::

    mensaje nuevo -> semilla -> encolar -> (espera corta) -> ON_DEMAND de ESE chat

Tres cosas que importan y son deliberadas:

* **No es un backfill global.** Se pide historial del chat que acaba de
  despertar, no de los cuarenta. Un backfill entero por cada mensaje que
  llega seria bombardear el telefono del usuario, que es quien atiende las
  peticiones ``ON_DEMAND``.

* **No bloquea la recepcion.** El manejador en vivo persiste, encola y
  termina. La excavacion ocurre despues, en la tarea de la cola.

* **Agrupa.** Si en la misma conversacion entran cinco mensajes seguidos, se
  excava UNA vez. Por eso hay una espera corta antes de empezar: da tiempo a
  que la rafaga termine.

LO QUE NO HACE
--------------
No inventa cursores. Solo excava chats que YA tienen un ancla real, que es lo
unico que ``HISTORY_SYNC_ON_DEMAND`` acepta. Un chat sin ancla no se encola,
ni se marca de otra manera, ni se le pide nada al servidor.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select

from app.core.logging_setup import get_logger
from app.history.cursor import get_valid_history_cursor
from app.models import Chat, ChatHistoryState

log = get_logger("BACKFILL")

# Espera antes de empezar a excavar un chat recien despertado. Agrupa las
# rafagas: cinco mensajes seguidos en la misma conversacion son UNA excavacion.
DEBOUNCE_SECONDS = 3.0

# Tope de rondas por chat en esta via. Mas bajo que el del backfill completo a
# proposito: esto corre mientras el usuario esta usando la aplicacion y no
# puede monopolizar la unica ranura de peticiones.
MAX_ROUNDS = 40


class SeedBackfillQueue:
    """Excava los chats que acaban de conseguir su primera ancla."""

    def __init__(self, database: Any, backfill: Any, loop_provider: Any = None) -> None:
        """``loop_provider`` se consulta CADA VEZ, no se guarda el bucle.

        El bucle del cliente no existe todavia cuando se cablea todo: se crea
        cuando arranca su hilo. Guardar aqui el valor de ese momento dejaria
        la cola con ``None`` para siempre y ningun chat se excavaria nunca.
        """
        self._database = database
        self._backfill = backfill
        self._loop_provider = loop_provider
        self._pendientes: set[str] = set()
        self._tarea: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # Solo para las pruebas y el diagnostico: cuantos chats se han
        # excavado por esta via desde que arranco el proceso.
        self.excavados = 0
        # -- Pausa por telefono que no responde ------------------------------
        #
        # El telefono puede quedarse dormido. Cuando pasa, la peticion sale,
        # el servidor la confirma y no llega nada -- exactamente igual que un
        # fallo de protocolo. La diferencia es que aqui no hay nada que
        # arreglar: hace falta que el usuario abra WhatsApp.
        #
        # Lo que NO se hace es seguir: quemar los 22 chats de la tanda contra
        # un telefono dormido los deja a todos en timeout con espera de
        # reintento, y el usuario ve 22 fallos donde habia un solo problema.
        # La cola se para y los que quedan siguen en ella.
        self.pausada = False
        self.motivo_pausa: str | None = None
        self._timeouts_seguidos = 0
        #: A quien avisar de que la recuperacion se paro o siguio.
        self.publish: Any = None

    # -- Entrada -------------------------------------------------------------

    def enqueue(self, chat_jids: Any) -> list[str]:
        """Encola los chats indicados. NO bloquea, NO espera, NO lanza.

        Devuelve los que se han encolado de verdad, para poder comprobarlo.
        Se llama desde el hilo del receptor, asi que aqui no puede ocurrir
        nada lento ni nada que pueda fallar.
        """
        nuevos = [j for j in dict.fromkeys(chat_jids or ()) if j]
        if not nuevos:
            return []

        antes = set(self._pendientes)
        self._pendientes.update(nuevos)
        encolados = [j for j in nuevos if j not in antes]
        if encolados:
            log.info(
                "%d chat(s) con semilla nueva entran en la cola de excavacion",
                len(encolados),
            )
        self._despertar()
        return encolados

    def _despertar(self) -> None:
        """Arranca la tarea de vaciado si no hay una en marcha."""
        loop = self._loop_provider() if callable(self._loop_provider) else self._loop_provider
        if loop is None or not self._pendientes or self.pausada:
            return
        if self._tarea is not None and not self._tarea.done():
            return
        try:
            self._tarea = asyncio.run_coroutine_threadsafe(  # type: ignore[assignment]
                self._vaciar(), loop
            )
        except Exception:  # noqa: BLE001 - encolar no puede tumbar la recepcion
            log.debug("No se pudo programar la excavacion; se hara en el ciclo normal")

    # -- Vaciado -------------------------------------------------------------

    async def _vaciar(self) -> None:
        """Excava los chats encolados, de UNO EN UNO.

        Nunca en paralelo: el telefono atiende una peticion cada vez, y dos
        respuestas cruzadas no se pueden atribuir. Este bucle es la garantia,
        y el candado global del backfill es la red por debajo.
        """
        async with self._lock:
            await asyncio.sleep(DEBOUNCE_SECONDS)

            while self._pendientes and not self.pausada:
                chat_jid = self._pendientes.pop()
                try:
                    await self._excavar(chat_jid)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - un chat no arrastra al resto
                    log.exception("Fallo excavando %s tras aparecer su semilla", _corto(chat_jid))

                motivo = self._motivo_para_pausar()
                if motivo is not None:
                    # El chat que se acaba de sacar ya se intento; los que
                    # quedan siguen en la cola tal cual, para cuando vuelva.
                    self._pausar(motivo)

    # -- Pausa y reanudacion -------------------------------------------------

    #: Timeouts ON_DEMAND seguidos antes de dar por dormido al telefono.
    MAX_TIMEOUTS_ANTES_DE_PAUSAR = 2

    def _motivo_para_pausar(self) -> str | None:
        """Por que habria que parar la tanda, o ``None`` para seguir.

        Dos motivos, y ninguno es "este chat fallo":

        * el transporte se cayo -- el telefono no tuvo ocasion de contestar;
        * dos peticiones seguidas agotaron su espera con el transporte sano,
          que es lo que se ve cuando el telefono esta dormido.

        Un timeout suelto NO para nada: puede ser de ese chat, y para eso ya
        esta la espera de reintento de siempre.
        """
        if getattr(self._backfill, "_last_transport_lost", False):
            return "transporte"
        seguidos = int(getattr(self._backfill, "_timeouts_seguidos", 0) or 0)
        if seguidos >= self.MAX_TIMEOUTS_ANTES_DE_PAUSAR:
            return "telefono"
        return None

    def _pausar(self, motivo: str) -> None:
        self.pausada = True
        self.motivo_pausa = motivo
        log.warning(
            "[BACKFILL] recuperacion pausada: %s. Quedan %d chat(s) en la cola; "
            "no se pierde nada y se retoma al continuar.",
            "el transporte se corto"
            if motivo == "transporte"
            else "el telefono no responde",
            len(self._pendientes),
        )
        self._avisar(
            "history.waiting_for_phone",
            {"reason": motivo, "pending": len(self._pendientes)},
        )

    def reanudar(self) -> bool:
        """Continua la tanda parada. ``False`` si todavia no se puede.

        Se exige que la sesion este viva y que ON_DEMAND siga confirmado: si
        no, reanudar solo repetiria el mismo fallo con otro chat.
        """
        if not self.pausada:
            return True
        if getattr(self._backfill, "_client", None) is None:
            return False
        try:
            if self._backfill.capability_state() != "CONFIRMED":
                return False
        except Exception:  # noqa: BLE001 - no poder mirarlo no autoriza seguir
            return False

        self.pausada = False
        self.motivo_pausa = None
        self._timeouts_seguidos = 0
        log.info(
            "[BACKFILL] recuperacion reanudada: quedan %d chat(s)",
            len(self._pendientes),
        )
        self._avisar("history.recovery_resumed", {"pending": len(self._pendientes)})
        self._despertar()
        return True

    def estado(self) -> dict[str, Any]:
        """Como va la tanda, para que el panel pueda decirlo."""
        return {
            "pending": len(self._pendientes),
            "paused": self.pausada,
            "pause_reason": self.motivo_pausa,
            "waiting_for_phone": self.pausada and self.motivo_pausa == "telefono",
            "dug": self.excavados,
        }

    def _avisar(self, nombre: str, datos: dict[str, Any]) -> None:
        if self.publish is None:
            return
        try:
            self.publish(nombre, datos)
        except Exception:  # noqa: BLE001 - avisar no puede cortar nada
            log.debug("No se pudo publicar %s", nombre)

    async def _excavar(self, chat_jid: str) -> None:
        """Pide historial de UN chat, si de verdad tiene con que pedirlo."""
        cliente = getattr(self._backfill, "_client", None)
        if cliente is None:
            log.debug("Sin cliente conectado; %s se excavara en el ciclo normal", _corto(chat_jid))
            return

        with self._database.transaction() as session:
            fila = session.execute(
                select(Chat.id, ChatHistoryState.history_status)
                .outerjoin(ChatHistoryState, ChatHistoryState.chat_jid == Chat.jid)
                .where(Chat.jid == chat_jid)
            ).first()
            if fila is None:
                return
            chat_id, estado = fila

            # Se vuelve a comprobar el ancla AQUI, no se confia en lo que
            # dijera quien encolo: entre una cosa y otra pudo pasar cualquier
            # cosa, y pedir sin ancla es lo que produce el ACK vacio. Con la
            # MISMA funcion que usa el motor, para que no puedan discrepar.
            cursor = get_valid_history_cursor(
                session, chat_id=chat_id, chat_jid=chat_jid
            )

        if cursor is None:
            log.debug("%s sigue sin ancla real; no se pide nada", _corto(chat_jid))
            return
        if estado in ("exhausted", "fetching"):
            # Ya esta agotado, o ya lo esta excavando el backfill normal.
            return

        log.info(
            "%s desperto: se le pide historial ahora, sin esperar al ciclo",
            _corto(chat_jid),
        )
        await self._backfill._process_chat(chat_id, chat_jid, MAX_ROUNDS)
        self.excavados += 1


def _corto(jid: str) -> str:
    user, _, server = jid.partition("@")
    return f"{user[:6]}***@{server}" if server else f"{user[:6]}***"
