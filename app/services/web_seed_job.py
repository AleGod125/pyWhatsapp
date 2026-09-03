"""Piloto de recuperacion por semilla auxiliar. Une el proveedor con el motor.

QUE HACE
--------
1. resuelve los alias del chat (telefono y LID son el mismo contacto);
2. lanza el proveedor auxiliar y espera UNA clave de mensaje;
3. la valida;
4. la entrega al MISMO camino que usa una semilla llegada en vivo.

D1 Y D2 SE MIDEN POR SEPARADO
-----------------------------
Conseguir la semilla y conseguir excavar con ella son dos resultados
distintos, y hoy sabemos que pueden divergir: hay chats con semillas reales
cuyo ``ON_DEMAND`` da ACK y ningun historial. Mezclarlos haria que un fallo
de excavacion se leyera como "la semilla no sirve", que es falso.

LA SEMILLA NO SE GASTA
----------------------
Si el transporte no esta usable, NO se lanza la peticion y el chat queda
``pending`` con su cursor guardado. Obtener una semilla auxiliar cuesta
vincular un dispositivo: no puede tirarse por un socket caido.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

from app.core.logging_setup import get_logger
from app.services.web_seed_provider import ETIQUETA, SeedJob, WebSeedProvider

log = get_logger("WA")


class WebSeedJobRunner:
    """Ejecuta pilotos, uno cada vez, y guarda su estado para consultarlo."""

    def __init__(self, settings: Any, database: Any) -> None:
        self._settings = settings
        self._database = database
        self._provider = WebSeedProvider(settings)
        self._jobs: dict[str, SeedJob] = {}
        self._lock = threading.Lock()
        self._corriendo = False

    @property
    def provider(self) -> WebSeedProvider:
        return self._provider

    def get(self, job_id: str) -> SeedJob | None:
        return self._jobs.get(job_id)

    @property
    def busy(self) -> bool:
        return self._corriendo

    # -- Lanzamiento ---------------------------------------------------------

    def start(self, chat_id: int, runtime: Any) -> SeedJob:
        """Arranca el piloto en su propio hilo. No bloquea la peticion HTTP."""
        if self._corriendo:
            raise RuntimeError("ya hay una busqueda de semilla en marcha")

        trabajo = SeedJob(job_id=uuid.uuid4().hex[:12], chat_id=chat_id)
        self._jobs[trabajo.job_id] = trabajo
        self._corriendo = True

        hilo = threading.Thread(
            target=self._ejecutar,
            args=(trabajo, runtime),
            name=f"web-seed-{trabajo.job_id}",
            daemon=True,
        )
        hilo.start()
        return trabajo

    def _ejecutar(self, trabajo: SeedJob, runtime: Any) -> None:
        try:
            self._pilotar(trabajo, runtime)
        except Exception as exc:  # noqa: BLE001 - un piloto no tumba el servicio
            log.exception("%s El piloto fallo", ETIQUETA)
            trabajo.state = "failed"
            trabajo.error = str(exc)[:300]
        finally:
            import time

            trabajo.finished_at = time.time()
            self._corriendo = False

    def _pilotar(self, trabajo: SeedJob, runtime: Any) -> None:
        aliases, chat_jid = self._aliases(trabajo.chat_id)
        if not chat_jid:
            trabajo.state = "failed"
            trabajo.error = "chat no encontrado"
            return

        trabajo.state = "connecting"

        def anotar(estado: str, datos: dict[str, Any]) -> None:
            trabajo.state = estado
            if "qr" in datos:
                trabajo.qr = datos["qr"]
            if "seed" in datos:
                trabajo.seed = datos["seed"]
            if "error" in datos:
                trabajo.error = datos["error"]

        semilla = self._provider.run(aliases, on_event=anotar)
        if semilla is None:
            trabajo.state = "failed"
            trabajo.error = trabajo.error or "no se encontro ninguna clave utilizable"
            return

        trabajo.seed = semilla
        trabajo.state = "seed_validated"

        # -- D1 termina aqui. Lo que sigue es D2. --
        trabajo.candidate_emitted = self._entregar(chat_jid, semilla)
        if not trabajo.candidate_emitted:
            trabajo.state = "failed"
            trabajo.error = "la semilla no se pudo registrar como cursor"
            return

        usable = self._transporte_usable(runtime)
        trabajo.transport_available = usable
        if not usable:
            # La semilla YA esta guardada como cursor: el chat se excavara en
            # cuanto haya transporte. No se gasta pidiendo sobre un socket
            # muerto, que es como se perdio tiempo en rondas anteriores.
            log.warning(
                "%s Semilla guardada, pero el transporte no esta usable: el "
                "chat queda pendiente y se excavara despues",
                ETIQUETA,
            )
            trabajo.state = "completed"
            return

        trabajo.backfill_enqueued = self._encolar(chat_jid, runtime)
        trabajo.state = "completed"

    # -- Piezas --------------------------------------------------------------

    def _aliases(self, chat_id: int) -> tuple[set[str], str | None]:
        """Todos los identificadores conocidos del contacto de ese chat."""
        from sqlalchemy import select

        from app.models import Chat

        with self._database.transaction() as sesion:
            jid = sesion.execute(
                select(Chat.jid).where(Chat.id == chat_id)
            ).scalar_one_or_none()
            if jid is None:
                return set(), None

            from app.services.history_recheck import HistoryRecheck

            revision = HistoryRecheck(self._database, self._settings)
            alias = set(revision.aliases_de(sesion, jid))
        return alias, jid

    def _entregar(self, chat_jid: str, semilla: Any) -> bool:
        """Guarda la clave como cursor del chat. NO escribe ningun mensaje.

        Es el unico efecto de todo el Plan D sobre la base: una fila de
        ``chat_history_state`` con el ancla y el estado a ``pending``. Ni un
        mensaje ni un adjunto salen del dispositivo auxiliar.
        """
        from sqlalchemy import update

        from app.models import ChatHistoryState

        try:
            with self._database.transaction() as sesion:
                actualizadas = sesion.execute(
                    update(ChatHistoryState)
                    .where(ChatHistoryState.chat_jid == chat_jid)
                    .values(
                        history_status="pending",
                        oldest_message_id=semilla.message_id,
                        oldest_message_timestamp=semilla.timestamp,
                        consecutive_no_progress=0,
                        last_error=None,
                    )
                ).rowcount
        except Exception:  # noqa: BLE001
            log.exception("%s No se pudo registrar la semilla", ETIQUETA)
            return False

        if not actualizadas:
            log.warning("%s El chat no tenia fila de estado historico", ETIQUETA)
            return False

        log.info(
            "%s Semilla registrada como cursor: el chat pasa a 'pending'", ETIQUETA
        )
        return True

    def _transporte_usable(self, runtime: Any) -> bool:
        """Si de verdad se puede hablar con WhatsApp ahora mismo.

        No basta con ``state=CONNECTED``: se midio un socket muerto con la
        maquina de estados diciendo que todo iba bien. Aqui se mira el
        transporte, que es lo que de verdad tiene que estar vivo.
        """
        try:
            cliente = getattr(runtime.client, "_client", None)
            if cliente is None:
                return False
            transporte = getattr(cliente, "_transport", None) or getattr(
                cliente, "transport", None
            )
            if transporte is None:
                # Sin poder mirarlo, se cree a la maquina de estados.
                return bool(getattr(runtime.state, "state", None))
            conectado = getattr(transporte, "connected", None)
            if conectado is None:
                conectado = getattr(transporte, "is_connected", None)
            if callable(conectado):
                conectado = conectado()
            return bool(conectado) if conectado is not None else True
        except Exception:  # noqa: BLE001
            return False

    def _encolar(self, chat_jid: str, runtime: Any) -> bool:
        """Entrega el chat al MISMO camino que una semilla llegada en vivo."""
        cola = getattr(runtime, "seed_queue", None)
        if cola is None:
            log.warning("%s No hay cola de excavacion disponible", ETIQUETA)
            return False
        encolados = cola.enqueue([chat_jid])
        return bool(encolados)
