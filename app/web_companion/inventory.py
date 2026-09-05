"""WhatsApp Web dice qué conversaciones existen; Python decide qué son.

EL CAMBIO DE ARQUITECTURA
------------------------
Hasta ahora el universo de conversaciones lo definía la sesión principal, y a
WhatsApp Web se le preguntaba sólo por las que ya estaban en la base esperando
ancla. Eso tenía dos agujeros medidos:

* lo que la sesión principal nunca descubrió no aparecía por ningún lado;
* de las que sí conocía, sólo se miraba lo que ya estuviera materializado.

Ahora el reparto es otro. Web indexa: qué conversaciones hay y cuál es el
último mensaje real de cada una. La sesión principal excava: `ON_DEMAND`,
bloques de cincuenta, `MORE`, `FINAL`. Cada una hace lo que hace bien.

QUIEN DECIDE
------------
Web **propone**. No sabe de quién es la cuenta, ni de alias entre teléfono y
LID, ni de qué conversaciones existen aquí. Este servicio reconcilia contra
PostgreSQL, que sigue siendo la fuente de verdad, y las anclas pasan por
`RecentSeedCollector` — el mismo recolector de siempre. No hay un segundo
sistema de anclas.

LO QUE NO HACE
--------------
No pide historial. No toca `ON_DEMAND`. No excava. Cuando una conversación
consigue ancla, entra en la cola de siempre y el motor de siempre se encarga.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.core.logging_setup import get_logger
from app.history.seed_collector import SeedCandidate
from app.models import Chat, ChatHistoryState

log = get_logger("WEB")

#: De dónde salió la conversación. Se guarda para poder distinguir después
#: qué aportó cada vía sin tener que deducirlo.
ORIGEN = "web_discovery"


@dataclass
class ResultadoDeInventario:
    """Lo que se encontró y lo que se hizo con ello."""

    # -- Lo que ve Web -------------------------------------------------------
    web_total: int = 0
    web_individual: int = 0
    web_grupos: int = 0
    con_ultimo_mensaje: int = 0
    con_memoria: int = 0
    con_fetch: int = 0
    sin_candidato: int = 0

    # -- La red: cuantas veces se pidio UN mensaje y que salio ---------------
    fetch_intentados: int = 0
    fetch_con_mensaje: int = 0
    fetch_vacios: int = 0
    fetch_fallidos: int = 0
    fetch_omitidos: int = 0

    #: Conversaciones de las que se consiguio ver ALGUN mensaje. No es lo
    #: mismo que tener referencia: si esto sube y las referencias no, el
    #: problema esta en el filtro, no en WhatsApp. Con una sola cifra las dos
    #: cosas eran indistinguibles.
    mensajes_encontrados: int = 0
    #: Mensajes que se vieron y aun asi no servian como referencia.
    descartados_por_filtro: int = 0
    #: Por que se quedo sin referencia cada una que se quedo sin ella.
    motivos_sin_referencia: dict[str, int] = field(default_factory=dict)
    #: Lo que Node descarto antes de proponerlo.
    rechazos_de_node: dict[str, int] = field(default_factory=dict)

    # -- La reconciliación ---------------------------------------------------
    ya_existian: int = 0
    creados: int = 0
    actualizados: int = 0
    #: Web propuso un identificador que aquí resuelve a otra conversación.
    alias_resueltos: int = 0

    # -- Las anclas ----------------------------------------------------------
    candidatos: int = 0
    validos: int = 0
    rechazados: int = 0
    promovidos: int = 0
    motivos: dict[str, int] = field(default_factory=dict)
    encolados: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        cobertura = (
            round(self.validos / self.web_total * 100) if self.web_total else 0
        )
        return {
            "web_inventory_total": self.web_total,
            "web_individual_chats": self.web_individual,
            "web_groups": self.web_grupos,
            "web_last_message_available": self.con_ultimo_mensaje,
            "web_store_memory": self.con_memoria,
            "web_fetch1_recovered": self.con_fetch,
            "web_chats_without_last_message": self.sin_candidato,
            "web_existing_chats": self.ya_existian,
            "web_inventory_new": self.creados,
            "web_chats_updated": self.actualizados,
            "web_alias_resolved": self.alias_resueltos,
            "web_seed_candidates": self.candidatos,
            "web_seed_valid": self.validos,
            "web_seed_rejected": self.rechazados,
            "web_seed_coverage_percent": cobertura,
            "chats_promoted": self.promovidos,
            "rejection_reasons": dict(self.motivos),
            "enqueued": len(self.encolados),
            # -- La red, por separado -------------------------------------
            "fetch1_attempted": self.fetch_intentados,
            "fetch1_success": self.fetch_con_mensaje,
            "fetch1_empty": self.fetch_vacios,
            "fetch1_error": self.fetch_fallidos,
            "fetch1_skipped": self.fetch_omitidos,
            # -- "vi un mensaje" no es "tengo una referencia" ---------------
            "messages_found": self.mensajes_encontrados,
            "valid_seeds": self.validos,
            "seed_invalid": self.descartados_por_filtro,
            "no_seed_reasons": dict(self.motivos_sin_referencia),
            "node_rejections": dict(self.rechazos_de_node),
        }


class WebInventoryService:
    """Pide el índice, lo reconcilia y entrega las anclas al motor."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    # -- Entrada -------------------------------------------------------------

    def refrescar(self, *, timeout: float = 300.0) -> ResultadoDeInventario:
        """Una pasada completa: índice, reconciliación, anclas y cola."""
        # La conexion principal manda. Descubrir conversaciones sin ella
        # crearia filas y cambiaria estados de una cuenta que ahora mismo no
        # se puede excavar, y encima haria pensar que el trabajo avanza.
        from app.core.primary import mensaje_para_el_usuario, razon_no_lista

        motivo = razon_no_lista(self._runtime)
        if motivo is not None:
            raise InventarioNoDisponible(
                "PRIMARY_NOT_READY", mensaje_para_el_usuario(motivo)
            )

        supervisor = getattr(self._runtime, "web_companion", None)
        if supervisor is None or not getattr(supervisor, "habilitado", False):
            raise InventarioNoDisponible(
                "WEB_COMPANION_DISABLED", "El indice de WhatsApp Web esta apagado."
            )
        if not getattr(supervisor, "vivo", False):
            raise InventarioNoDisponible(
                "WEB_COMPANION_NOT_RUNNING", "El indice de WhatsApp Web no esta en marcha."
            )
        if not supervisor.snapshot().get("web_client_ready"):
            raise InventarioNoDisponible(
                "WEB_COMPANION_NOT_READY",
                "El indice de WhatsApp Web todavia no ha terminado de conectar.",
            )

        # Node no sabe cuales esperan referencia, cuales ya tienen cursor ni
        # cuales conoce esta base. Sin eso la cuota de red se gasta en
        # conversaciones que no desbloquean nada, y se malgasta en las que ya
        # estan resueltas.
        reparto = self._reparto()
        respuesta = supervisor.enviar(
            {
                "cmd": "web_inventory",
                "priority_chat_jids": reparto["prioritarios"],
                "skip_chat_jids": reparto["omitir"],
                "known_chat_jids": reparto["conocidos"],
                "debug": log.isEnabledFor(logging.DEBUG),
            },
            timeout=timeout,
        )
        if respuesta.get("error"):
            raise InventarioNoDisponible(
                "WEB_INVENTORY_FAILED", str(respuesta.get("error"))
            )
        return self._procesar(respuesta)

    # -- El trabajo ----------------------------------------------------------

    def _procesar(self, respuesta: dict[str, Any]) -> ResultadoDeInventario:
        metricas = respuesta.get("metrics") or {}
        resultado = ResultadoDeInventario(
            web_total=int(metricas.get("total", 0) or 0),
            web_individual=int(metricas.get("individual", 0) or 0),
            web_grupos=int(metricas.get("group", 0) or 0),
            con_ultimo_mensaje=int(metricas.get("con_last_message", 0) or 0),
            con_memoria=int(metricas.get("con_memoria", 0) or 0),
            con_fetch=int(metricas.get("con_fetch", 0) or 0),
            sin_candidato=int(metricas.get("sin_candidato", 0) or 0),
            fetch_intentados=int(metricas.get("fetch1_attempted", 0) or 0),
            fetch_con_mensaje=int(metricas.get("fetch1_success", 0) or 0),
            fetch_vacios=int(metricas.get("fetch1_empty", 0) or 0),
            fetch_fallidos=int(metricas.get("fetch1_error", 0) or 0),
            fetch_omitidos=int(metricas.get("fetch1_skipped", 0) or 0),
            mensajes_encontrados=int(metricas.get("messages_found", 0) or 0),
            descartados_por_filtro=int(metricas.get("seed_invalid", 0) or 0),
            rechazos_de_node=dict(respuesta.get("rejections") or {}),
        )

        # El detalle por conversacion va redactado desde Node y solo cuando se
        # pide: un JID completo es un numero de telefono.
        for fila in respuesta.get("per_chat") or []:
            log.debug(
                "[WEB_INDEX] chat=%s via=%s resultado=%s motivo=%s",
                fila.get("chat"),
                fila.get("source"),
                fila.get("result"),
                fila.get("reason"),
            )

        cuenta = getattr(self._runtime, "runtime_owner_account_id", None)
        recolector = self._recolector()
        promovidos: list[int] = []

        for fila in respuesta.get("chats") or []:
            jid = str(fila.get("chat_jid") or "")
            if not jid:
                continue

            chat_id, era_nuevo, alias = self._asegurar_chat(jid, fila, cuenta)
            if chat_id is None:
                continue
            if era_nuevo:
                resultado.creados += 1
                # Ya esta en PostgreSQL: la transaccion de `_asegurar_chat`
                # cerro antes de volver. Primero la base, despues el aviso;
                # nunca al reves, o la pantalla ensenaria una conversacion que
                # no existe.
                self._avisar_de_la_conversacion(chat_id, nueva=True)
            else:
                resultado.ya_existian += 1
                if fila.get("name") or fila.get("last_activity"):
                    self._avisar_de_la_conversacion(chat_id, nueva=False)
            if alias:
                resultado.alias_resueltos += 1

            crudo = fila.get("candidate")
            if not crudo:
                motivo = str(fila.get("no_seed_reason") or "WEB_NO_CANDIDATE")
                resultado.motivos_sin_referencia[motivo] = (
                    resultado.motivos_sin_referencia.get(motivo, 0) + 1
                )
                continue

            resultado.candidatos += 1
            candidato = SeedCandidate(
                chat_jid=str(crudo.get("chat_jid") or jid),
                wa_msg_id=crudo.get("wa_msg_id"),
                timestamp=crudo.get("timestamp"),
                from_me=bool(crudo.get("from_me")),
                # El origen dice por que via llego, para poder medirlo despues.
                source=str(crudo.get("source") or ORIGEN),
                message_type=crudo.get("message_type"),
            )

            # El recolector de SIEMPRE. Valida, deduplica, elige el cursor con
            # la funcion canonica y promueve. Aqui no se decide nada de eso.
            salida = recolector.observe(candidato)
            if not salida.aceptada:
                resultado.rechazados += 1
                resultado.motivos[salida.motivo] = (
                    resultado.motivos.get(salida.motivo, 0) + 1
                )
                continue
            resultado.validos += 1
            if salida.desperto and salida.chat_id is not None:
                resultado.promovidos += 1
                promovidos.append(salida.chat_id)

        resultado.encolados = self._encolar(promovidos)

        # El resumen de la tanda. Antes solo lo publicaba la ruta HTTP, asi
        # que cuando el indice lo lanzaba el vigilante automatico —que es el
        # caso normal— la pantalla no se enteraba de nada y habia que recargar
        # a mano. Era la causa del F5.
        bus = getattr(self._runtime, "bus", None)
        if bus is not None:
            try:
                bus.publish("web_inventory_done", resultado.to_json())
            except Exception:  # noqa: BLE001 - avisar no puede cortar nada
                log.debug("No se pudo publicar el resumen del indice")

        # Por metodo, no un total. "seeds=0" no dice si WhatsApp no tiene
        # mensajes, si no se llego a preguntar o si el filtro los rechazo, y
        # esas tres cosas se arreglan en sitios distintos.
        log.info(
            "[WEB_INDEX] chats=%d origen=%s last=%d store=%d "
            "fetch1_intentados=%d fetch1_con_mensaje=%d "
            "mensajes=%d seeds=%d cobertura=%d%% sin_referencia=%d "
            "nuevos=%d promovidos=%d",
            resultado.web_total,
            respuesta.get("source") or "?",
            resultado.con_ultimo_mensaje,
            resultado.con_memoria,
            resultado.fetch_intentados,
            resultado.fetch_con_mensaje,
            resultado.mensajes_encontrados,
            resultado.validos,
            resultado.to_json()["web_seed_coverage_percent"],
            resultado.sin_candidato,
            resultado.creados,
            resultado.promovidos,
        )
        if resultado.rechazos_de_node:
            log.info("[WEB_INDEX] descartes: %s", resultado.rechazos_de_node)
        if resultado.motivos_sin_referencia:
            log.info(
                "[WEB_INDEX] sin referencia por: %s",
                resultado.motivos_sin_referencia,
            )
        return resultado

    def _reparto(self) -> dict[str, list[str]]:
        """Que conversaciones son prioritarias, cuales se omiten y cuales hay.

        * **prioritarias**: las que esperan referencia. Son las que bloquean el
          producto, asi que la cuota de red se gasta ahi primero.
        * **omitir**: las que ya tienen un cursor valido. Pedirles un mensaje
          no aporta nada y consume una peticion que le hace falta a otra.
          Tambien las agotadas: su historial ya termino, y una referencia
          nueva no lo reabre.
        * **conocidas**: para que Node sepa cuales ve solo el, y pueda
          ponerlas por delante del resto.
        """
        from app.models import SEEDLESS_STATUSES
        from app.history.cursor import get_valid_history_cursor

        prioritarios: list[str] = []
        omitir: list[str] = []
        conocidos: list[str] = []
        cuenta = getattr(self._runtime, "runtime_owner_account_id", None)
        try:
            with self._runtime.database.transaction() as sesion:
                consulta = select(Chat.jid, ChatHistoryState.history_status).join(
                    ChatHistoryState,
                    ChatHistoryState.chat_jid == Chat.jid,
                    isouter=True,
                )
                if cuenta is not None:
                    consulta = consulta.where(Chat.whatsapp_account_id == cuenta)
                for jid, estado in sesion.execute(consulta).all():
                    if not jid:
                        continue
                    conocidos.append(jid)
                    if estado == "exhausted":
                        omitir.append(jid)
                        continue
                    if estado in SEEDLESS_STATUSES:
                        prioritarios.append(jid)
                        continue
                    # Ya se puede excavar: no hace falta otra referencia.
                    try:
                        if get_valid_history_cursor(sesion, chat_jid=jid) is not None:
                            omitir.append(jid)
                    except Exception:  # noqa: BLE001 - no saberlo no omite
                        pass
        except Exception:  # noqa: BLE001 - sin reparto se indexa igual
            log.debug("No se pudo calcular el reparto del indice", exc_info=True)

        return {
            "prioritarios": prioritarios,
            "omitir": omitir,
            "conocidos": conocidos,
        }

    # -- Piezas --------------------------------------------------------------

    def _asegurar_chat(
        self, jid: str, fila: dict[str, Any], cuenta: Any
    ) -> tuple[int | None, bool, bool]:
        """El chat de esta base para ese identificador. Lo crea si no está.

        Devuelve ``(chat_id, era_nuevo, hubo_alias)``.

        Un contacto aparece por teléfono y por LID y es la MISMA conversación.
        Se usa el resolutor de siempre: crear una segunda dejaría el historial
        partido en dos mitades que nunca se juntan.
        """
        from app.services.chat_alias import canonical_chat_jid

        with self._runtime.database.transaction() as sesion:
            canonico = canonical_chat_jid(sesion, jid) or jid
            hubo_alias = canonico != jid

            chat_id = sesion.execute(
                select(Chat.id).where(Chat.jid == canonico)
            ).scalar_one_or_none()
            if chat_id is not None:
                self._actualizar(sesion, chat_id, fila)
                return chat_id, False, hubo_alias

            # Nuevo. Antes se descartaba por no haberlo visto la sesion
            # principal, y asi se perdian conversaciones que existen de verdad.
            chat = Chat(
                jid=canonico,
                chat_type="group" if fila.get("is_group") else "individual",
                name=fila.get("name") or None,
                whatsapp_account_id=cuenta,
                last_message_timestamp=fila.get("last_activity") or None,
            )
            sesion.add(chat)
            sesion.flush()

            # Nace esperando ancla. Nunca "sincronizado" por el mero hecho de
            # existir: eso es justo la mentira que costo dos fases descubrir.
            sesion.add(
                ChatHistoryState(
                    chat_id=chat.id,
                    chat_jid=canonico,
                    history_status="waiting_seed",
                )
            )
            sesion.flush()
            return chat.id, True, hubo_alias

    @staticmethod
    def _actualizar(sesion: Any, chat_id: int, fila: dict[str, Any]) -> None:
        """Refresca lo que Web sabe mejor: el nombre y la última actividad.

        No toca el estado del historial. Que Web vea actividad reciente no
        significa que haya que volver a excavar una conversación que el
        teléfono ya dio por terminada.
        """
        chat = sesion.execute(select(Chat).where(Chat.id == chat_id)).scalar_one_or_none()
        if chat is None:
            return
        nombre = fila.get("name")
        if nombre and not chat.name:
            chat.name = nombre
        actividad = fila.get("last_activity")
        if actividad and (
            chat.last_message_timestamp is None
            or actividad > chat.last_message_timestamp
        ):
            chat.last_message_timestamp = int(actividad)

    def _avisar_de_la_conversacion(self, chat_id: int, *, nueva: bool) -> None:
        """Manda la fila COMPLETA para que la pantalla no tenga que pedirla.

        POR QUE LA FILA ENTERA
        ----------------------
        El aviso escueto —«ha cambiado algo»— obliga al frontend a pedir la
        lista otra vez, y descubrir cincuenta conversaciones serian cincuenta
        peticiones. Con la fila dentro, la pantalla la inserta o la actualiza
        en el sitio y no pregunta nada.

        Es el mismo contrato que ya usa un mensaje nuevo (``chat.updated`` con
        su ``chat`` dentro), asi que el frontend no aprende nada nuevo.

        Avisar NUNCA puede cortar la indexacion: si esto falla, la
        conversacion ya esta guardada y la proxima carga la ensenara.
        """
        bus = getattr(self._runtime, "bus", None)
        if bus is None:
            return
        try:
            from app.api.serializers import chat_to_json
            from app.services import repository as repo

            with self._runtime.database.transaction() as sesion:
                resumen = repo.chat_summary(sesion, chat_id)
            if resumen is None:
                return
            bus.publish(
                "web_chat_created" if nueva else "web_chat_updated",
                {"chat_id": chat_id, "chat": chat_to_json(resumen)},
            )
        except Exception:  # noqa: BLE001 - un aviso no puede tumbar el indice
            log.debug("No se pudo avisar de la conversacion %s", chat_id, exc_info=True)

    def _recolector(self) -> Any:
        """El recolector de siempre, sin cola: encolar es el último paso."""
        from app.history.seed_collector import RecentSeedCollector

        existente = getattr(self._runtime, "seed_collector", None)
        recolector = RecentSeedCollector(
            self._runtime.database,
            user_id=getattr(existente, "user_id", None)
            or getattr(self._runtime, "runtime_owner_user_id", None),
            account_id=getattr(existente, "account_id", None)
            or getattr(self._runtime, "runtime_owner_account_id", None),
            seed_queue=None,
        )
        bus = getattr(self._runtime, "bus", None)
        if bus is not None:
            recolector.publish = bus.publish
        return recolector

    def _encolar(self, chat_ids: list[int]) -> list[str]:
        if not chat_ids:
            return []
        cola = getattr(self._runtime, "seed_queue", None)
        if cola is None:
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
        try:
            return cola.enqueue(jids)
        except Exception:  # noqa: BLE001 - encolar no puede deshacer las anclas
            log.exception("No se pudo encolar el lote del indice Web")
            return []


class InventarioNoDisponible(RuntimeError):
    """El índice no se puede pedir ahora. Nada se ha tocado."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
