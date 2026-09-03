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
from app.models import Chat, ChatHistoryState
from app.services import repository as repo

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
        if loop is None or not self._pendientes:
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
        """Excava los chats encolados, de uno en uno."""
        async with self._lock:
            await asyncio.sleep(DEBOUNCE_SECONDS)

            while self._pendientes:
                chat_jid = self._pendientes.pop()
                try:
                    await self._excavar(chat_jid)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - un chat no arrastra al resto
                    log.exception("Fallo excavando %s tras aparecer su semilla", _corto(chat_jid))

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
            # cosa, y pedir sin ancla es lo que produce el ACK vacio.
            cursor = repo.get_oldest_valid_history_cursor(session, chat_jid)

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
