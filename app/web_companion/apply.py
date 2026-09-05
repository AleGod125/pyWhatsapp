"""Convertir las referencias que ve WhatsApp Web en anclas de verdad.

QUE HACE, Y QUE NO
------------------
El sondeo mide y no toca nada. Esto es lo contrario, y por eso es una accion
aparte: entra por su propio endpoint y por su propio boton. "Probar cobertura
Web" sigue siendo de solo lectura, y ninguna medicion puede acabar mutando la
base por accidente.

DOS FASES, SEPARADAS A PROPOSITO
--------------------------------
::

    FASE A  candidatos Web -> validacion -> history_seeds -> cursor -> pending
    FASE B  encolar los promovidos en la excavacion

Si la fase B se pausa porque el telefono deja de responder, las anclas de la
fase A siguen guardadas. Nada se deshace.

NO HAY UN SEGUNDO EXTRACTOR
---------------------------
Node propone; Python decide. Y quien decide es ``RecentSeedCollector``, el
MISMO recolector que atiende las anclas que llegan en vivo o en un blob: la
misma validacion, la misma deduplicacion, la misma eleccion de cursor y la
misma transicion de estado. Una segunda implementacion acabaria eligiendo un
ancla distinta de la que despues se pide, que es un fallo que este proyecto ya
ha pagado una vez.

POR QUE SE VUELVE A SONDEAR
---------------------------
Los candidatos se miden justo antes de aplicarlos, no se aceptan los de hace
un rato. Entre una medicion y una escritura el usuario puede haber recibido
mensajes, un chat puede haber despertado solo y otro puede haber dejado de
estar visible. Aplicar sobre una foto vieja escribiria anclas de un estado que
ya no existe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.core.logging_setup import get_logger
from app.models import Chat

log = get_logger("WEB")


class AplicacionRechazada(RuntimeError):
    """No se dan las condiciones para escribir nada. Nada se ha tocado."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


@dataclass
class ResultadoDeAplicacion:
    """Lo que se hizo. Cada numero cuenta una cosa distinta."""

    #: Los que propuso Node y pasaron nuestras reglas.
    candidatos: int = 0
    validados: int = 0
    #: Anclas nuevas escritas.
    insertadas: int = 0
    #: Anclas que ya estaban: el apply es idempotente.
    ya_estaban: int = 0
    #: Chats que pasaron de esperar ancla a poder pedir historial.
    promovidos: int = 0
    rechazados: int = 0
    motivos: dict[str, int] = field(default_factory=dict)
    #: Los que siguen esperando porque Web no tiene mensajes suyos.
    sin_referencia_web: int = 0
    capacidad: str = "UNKNOWN"
    encolados: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "candidates": self.candidatos,
            "validated": self.validados,
            "inserted": self.insertadas,
            "already_present": self.ya_estaban,
            "promoted_to_pending": self.promovidos,
            "rejected": self.rechazados,
            "rejection_reasons": dict(self.motivos),
            "still_waiting_without_seed": self.sin_referencia_web,
            "on_demand_capability": self.capacidad,
            "enqueued": len(self.encolados),
        }


class WebSeedApplier:
    """Aplica las referencias de WhatsApp Web. Accion explicita."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    # -- Condiciones previas -------------------------------------------------

    def comprobar(self) -> str:
        """Devuelve el estado de la capacidad, o explica por que no se puede.

        Se comprueba ANTES de escribir nada. Promover 22 conversaciones a
        ``pending`` cuando el motor no esta respondiendo las convertiria en 22
        timeouts con espera de reintento, y no habria forma de distinguir "el
        ancla era mala" de "el telefono no contesto".
        """
        supervisor = getattr(self._runtime, "web_companion", None)
        if supervisor is None or not supervisor.habilitado:
            raise AplicacionRechazada(
                "WEB_COMPANION_DISABLED", "El Web Companion esta apagado."
            )
        if not supervisor.vivo:
            raise AplicacionRechazada(
                "WEB_COMPANION_NOT_RUNNING",
                "El Web Companion no esta en marcha.",
                state=supervisor.snapshot().get("state"),
            )
        instantanea = supervisor.snapshot()
        if not instantanea.get("web_client_ready"):
            raise AplicacionRechazada(
                "WEB_COMPANION_NOT_READY",
                "El Web Companion todavia no ha terminado de conectar.",
                state=instantanea.get("state"),
            )

        backfill = getattr(self._runtime, "backfill", None)
        if backfill is None:
            raise AplicacionRechazada(
                "SESSION_NOT_CONNECTED",
                "Hace falta la sesion de WhatsApp conectada.",
            )
        capacidad = backfill.capability_state()
        if capacidad != "CONFIRMED":
            raise AplicacionRechazada(
                "ON_DEMAND_NOT_CONFIRMED",
                (
                    "ON_DEMAND no esta confirmado en esta sesion "
                    f"(capacidad={capacidad}). No se aplican referencias: sin "
                    "el motor respondiendo, promoverlas solo produciria "
                    "esperas agotadas."
                ),
                capability=capacidad,
            )
        return capacidad

    # -- Aplicacion ----------------------------------------------------------

    def aplicar(self, *, timeout: float = 300.0) -> ResultadoDeAplicacion:
        """Mide de nuevo, escribe las anclas y encola lo que se promueva."""
        capacidad = self.comprobar()

        from app.web_companion.probe import WebCompanionProbe

        cuenta = getattr(self._runtime, "runtime_owner_account_id", None)
        sondeador = WebCompanionProbe(self._runtime.database, self._runtime.web_companion)
        medido = sondeador.medir(cuenta, timeout=timeout)
        if isinstance(medido, dict):
            raise AplicacionRechazada(
                "WEB_COMPANION_PROBE_FAILED",
                str(medido.get("error") or "el sondeo no pudo completarse"),
                state=medido.get("state"),
            )

        resultado = ResultadoDeAplicacion(
            candidatos=medido.candidates,
            validados=medido.seed_usable,
            sin_referencia_web=medido.sin_seed,
            capacidad=capacidad,
        )
        if not medido.aceptados:
            log.info(
                "[WEB_SEEDS] candidates=%d valid=0: no hay ninguna referencia "
                "que aplicar",
                medido.candidates,
            )
            return resultado

        # -- FASE A: escribir las anclas ------------------------------------
        recolector = self._recolector()
        chats_promovidos: list[int] = []
        for candidato in medido.aceptados:
            # El MISMO recolector de siempre. Valida otra vez -- ya lo hizo el
            # sondeo, pero entre medir y escribir cambia el mundo --, resuelve
            # el chat por alias, deduplica y elige el cursor con la funcion
            # canonica.
            salida = recolector.observe(candidato)
            if not salida.aceptada:
                resultado.rechazados += 1
                resultado.motivos[salida.motivo] = (
                    resultado.motivos.get(salida.motivo, 0) + 1
                )
                continue
            if salida.motivo == "ya conocida":
                resultado.ya_estaban += 1
            else:
                resultado.insertadas += 1
            if salida.desperto and salida.chat_id is not None:
                resultado.promovidos += 1
                chats_promovidos.append(salida.chat_id)

        log.info(
            "[WEB_SEEDS] candidates=%d valid=%d inserted=%d already=%d "
            "promoted=%d rejected=%d waiting_without_seed=%d",
            resultado.candidatos,
            resultado.validados,
            resultado.insertadas,
            resultado.ya_estaban,
            resultado.promovidos,
            resultado.rechazados,
            resultado.sin_referencia_web,
        )

        # -- FASE B: encolar -------------------------------------------------
        # Aparte, y despues. Si esto falla o se pausa, las anclas de arriba ya
        # estan guardadas y no se pierden.
        resultado.encolados = self._encolar(chats_promovidos)
        return resultado

    # -- Piezas --------------------------------------------------------------

    def _recolector(self) -> Any:
        """El recolector de siempre, SIN cola.

        Se le quita la cola a proposito: encolar es la fase B y se hace
        explicitamente al final, con la lista completa. Asi el numero de
        promovidos se puede informar antes de que empiece a excavar nada.
        """
        from app.history.seed_collector import RecentSeedCollector

        existente = getattr(self._runtime, "seed_collector", None)
        return RecentSeedCollector(
            self._runtime.database,
            user_id=getattr(existente, "user_id", None)
            or getattr(self._runtime, "runtime_owner_user_id", None),
            account_id=getattr(existente, "account_id", None)
            or getattr(self._runtime, "runtime_owner_account_id", None),
            seed_queue=None,
        )

    def _encolar(self, chat_ids: list[int]) -> list[str]:
        """Entrega los promovidos a la cola. De uno en uno los excava ella."""
        if not chat_ids:
            return []
        cola = getattr(self._runtime, "seed_queue", None)
        if cola is None:
            log.debug("Sin cola de excavacion; se recogeran en el ciclo normal")
            return []

        with self._runtime.database.transaction() as sesion:
            jids = [
                j
                for j in sesion.execute(
                    select(Chat.jid).where(Chat.id.in_(chat_ids))
                ).scalars()
                if j
            ]
        if not jids:
            return []

        log.info("[BACKFILL] lote Web iniciado chats=%d", len(jids))
        try:
            return cola.enqueue(jids)
        except Exception:  # noqa: BLE001 - encolar no puede deshacer las anclas
            log.exception("No se pudo encolar el lote Web; las anclas siguen guardadas")
            return []
