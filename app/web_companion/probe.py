"""Medir que ve WhatsApp Web. SIN escribir nada.

LA PREGUNTA
-----------
De las conversaciones que estan esperando una referencia, ¿cuantas ve WhatsApp
Web, y de cuantas puede dar un mensaje real con el que pedir historial?

Nada mas. Esta fase es una medicion, no una recuperacion.

SOLO LECTURA, Y SE COMPRUEBA
----------------------------
No se anota ni un ancla, no se cambia ni un estado, no se pide ni un
``ON_DEMAND``. Hay pruebas que fijan esa frontera contando filas antes y
despues, porque es justo lo que impide que esto se convierta sin querer en un
segundo extractor.

QUIEN VALIDA
------------
Node propone; Python decide. El worker no sabe de quien es la cuenta, ni de
los alias PN/LID, ni de que conversaciones existen aqui. Sus candidatos pasan
por :func:`validar`, que es el MISMO filtro que usa el colector de anclas de
siempre — si aceptara algo que aquel rechaza, lo medido no valdria para nada.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.core.logging_setup import get_logger
from app.history.seed_collector import SeedCandidate, validar
from app.models import Chat, ChatHistoryState

log = get_logger("WEB")


@dataclass
class ResultadoDelSondeo:
    """Los numeros. Ni uno inventado."""

    waiting: int = 0
    visible_store: int = 0
    with_messages: int = 0
    #: Candidatos que devolvio el worker, antes de validarlos aqui.
    candidates: int = 0
    #: Y los que pasan NUESTRAS reglas. Es el numero que decide la fase
    #: siguiente; el de arriba solo dice cuanto propuso Node.
    seed_usable: int = 0
    sin_seed: int = 0
    rechazos: dict[str, int] = field(default_factory=dict)
    por_origen: dict[str, int] = field(default_factory=dict)
    #: Que se habria despertado, si esto no fuera solo una medicion.
    chats_despertables: list[int] = field(default_factory=list)
    #: Los candidatos que pasaron NUESTRAS reglas, tal cual.
    #:
    #: No salen en el JSON: un ancla es un identificador de mensaje real. Los
    #: usa la fase de aplicacion, que corre en el mismo proceso.
    aceptados: list[SeedCandidate] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "waiting": self.waiting,
            "visible_store": self.visible_store,
            "with_messages": self.with_messages,
            "candidates": self.candidates,
            "seed_usable": self.seed_usable,
            "sin_seed": self.sin_seed,
            "rejections": dict(self.rechazos),
            "by_source": dict(self.por_origen),
            "wakeable_chats": len(self.chats_despertables),
            # Se dice explicitamente para que nadie tenga que deducirlo.
            "mutations": 0,
            "on_demand_requests": 0,
            "read_only": True,
        }


class WebCompanionProbe:
    """Pregunta al worker y traduce lo que conteste. No escribe nada."""

    def __init__(self, database: Any, supervisor: Any) -> None:
        self._database = database
        self._supervisor = supervisor

    # -- Lo que sabe Python --------------------------------------------------

    def chats_conocidos(self, account_id: Any = None) -> list[str]:
        """Los JID que ya tiene el backend, para poder comparar."""
        with self._database.transaction() as sesion:
            consulta = select(Chat.jid)
            if account_id is not None:
                consulta = consulta.where(Chat.whatsapp_account_id == account_id)
            return [j for j in sesion.execute(consulta).scalars() if j]

    def chats_esperando(self, account_id: Any = None) -> list[tuple[int, str]]:
        """Las conversaciones sin ancla: las que motivan todo esto."""
        from app.models import SEEDLESS_STATUSES

        with self._database.transaction() as sesion:
            consulta = (
                select(Chat.id, Chat.jid)
                .join(ChatHistoryState, ChatHistoryState.chat_jid == Chat.jid)
                .where(ChatHistoryState.history_status.in_(SEEDLESS_STATUSES))
            )
            if account_id is not None:
                consulta = consulta.where(Chat.whatsapp_account_id == account_id)
            return [(f[0], f[1]) for f in sesion.execute(consulta).all()]

    # -- Inventario ----------------------------------------------------------

    def inventario(self, account_id: Any = None, *, timeout: float = 60.0) -> dict[str, Any]:
        """Que conversaciones ve Web frente a las que ya tiene Python."""
        conocidos = self.chats_conocidos(account_id)
        respuesta = self._supervisor.enviar(
            {"cmd": "inventory", "python_chat_jids": conocidos}, timeout=timeout
        )
        if respuesta.get("error"):
            return {"error": respuesta["error"], "state": respuesta.get("state")}

        metricas = respuesta.get("metrics") or {}
        desconocidos = respuesta.get("unknown_to_python") or []
        log.info(
            "inventory python=%d web_get_chats=%d web_store=%d union=%d extra=%d faltan=%d",
            metricas.get("python_chats", 0),
            metricas.get("web_get_chats", 0),
            metricas.get("web_store_chats", 0),
            metricas.get("union_chats", 0),
            metricas.get("extra_vs_python", 0),
            metricas.get("missing_vs_python", 0),
        )
        return {
            "metrics": metricas,
            "capabilities": respuesta.get("capabilities"),
            # De que clase son las que Web ve y Python no, y al reves. Solo
            # recuentos: un JID completo es un numero de telefono.
            "extra_por_clase": respuesta.get("extra_por_clase") or {},
            "faltan_por_clase": respuesta.get("faltan_por_clase") or {},
            # Solo el recuento y la forma; los identificadores completos son
            # numeros de telefono y no hacen falta para decidir nada.
            "unknown_to_python": [
                {"is_group": bool(c.get("is_group")), "msgs_in_memory": c.get("msgs_in_memory", 0)}
                for c in desconocidos
            ],
        }

    # -- Sondeo --------------------------------------------------------------

    def sondear(self, account_id: Any = None, *, timeout: float = 300.0) -> dict[str, Any]:
        """De las que esperan, ¿cuantas tienen una referencia real en Web?

        NO escribe. Lo unico que sale de aqui son numeros.
        """
        medido = self.medir(account_id, timeout=timeout)
        if isinstance(medido, dict):
            return medido
        return medido.to_json()

    def medir(
        self, account_id: Any = None, *, timeout: float = 300.0
    ) -> "ResultadoDelSondeo | dict[str, Any]":
        """Lo mismo que :meth:`sondear`, pero devolviendo el objeto.

        Existe para que la fase de aplicacion pueda usar los MISMOS candidatos
        que se acaban de medir, sin volver a preguntar y sin reimplementar la
        validacion. Sigue sin escribir nada.
        """
        esperando = self.chats_esperando(account_id)
        if not esperando:
            log.info("seed_probe waiting=0: no hay ninguna conversacion esperando")
            return ResultadoDelSondeo()

        por_jid = {jid: chat_id for chat_id, jid in esperando}
        respuesta = self._supervisor.enviar(
            {"cmd": "probe_waiting_seeds", "chat_jids": list(por_jid)}, timeout=timeout
        )
        if respuesta.get("error"):
            return {"error": respuesta["error"], "state": respuesta.get("state")}

        return self._traducir(respuesta, por_jid)

    def _traducir(self, respuesta: dict[str, Any], por_jid: dict[str, int]) -> ResultadoDelSondeo:
        """Lo que dijo Node, pasado por NUESTRAS reglas."""
        del_worker = respuesta.get("summary") or {}
        resultado = ResultadoDelSondeo(
            waiting=len(por_jid),
            visible_store=int(del_worker.get("visible_store", 0) or 0),
            with_messages=int(del_worker.get("with_messages", 0) or 0),
        )

        # Lo que rechazo NODE, por motivo. Se conserva aparte de lo que
        # rechaza Python: son dos filtros distintos y mezclarlos esconde
        # cual de los dos hay que arreglar.
        for motivo, cuantos in (del_worker.get("rejections") or {}).items():
            resultado.rechazos[f"node:{motivo}"] = cuantos

        for fila in respuesta.get("chats") or []:
            crudo = fila.get("candidate")
            if not crudo:
                resultado.sin_seed += 1
                continue

            resultado.candidates += 1
            candidato = SeedCandidate(
                chat_jid=str(crudo.get("chat_jid") or ""),
                wa_msg_id=crudo.get("wa_msg_id"),
                timestamp=crudo.get("timestamp"),
                from_me=bool(crudo.get("from_me")),
                source=str(crudo.get("source") or "web_store"),
                # Node manda el tipo para que se aplique NUESTRO filtro de
                # mensajes de protocolo; el no decide eso.
                message_type=crudo.get("message_type"),
            )

            motivo = validar(candidato)
            if motivo is None:
                motivo = self._resolver_conversacion(candidato, por_jid)

            if motivo is not None:
                resultado.sin_seed += 1
                clave = f"python:{motivo}"
                resultado.rechazos[clave] = resultado.rechazos.get(clave, 0) + 1
                continue

            resultado.seed_usable += 1
            resultado.aceptados.append(candidato)
            resultado.por_origen[candidato.source] = (
                resultado.por_origen.get(candidato.source, 0) + 1
            )
            chat_id = por_jid.get(candidato.chat_jid)
            if chat_id is not None:
                resultado.chats_despertables.append(chat_id)

        log.info(
            "seed_probe waiting=%d visibles=%d con_mensajes=%d candidatos=%d validos=%d sin_ancla=%d",
            resultado.waiting,
            resultado.visible_store,
            resultado.with_messages,
            resultado.candidates,
            resultado.seed_usable,
            resultado.sin_seed,
        )
        if resultado.rechazos:
            log.debug("motivos de rechazo: %s", resultado.rechazos)
        return resultado

    def _resolver_conversacion(
        self, candidato: SeedCandidate, por_jid: dict[str, int]
    ) -> str | None:
        """Que el chat exista AQUI, resolviendo alias como siempre.

        WhatsApp Web puede dar el mismo contacto por telefono o por LID. Se usa
        el resolutor de siempre para no dar por buena una referencia de una
        conversacion que en esta base es otra.
        """
        if candidato.chat_jid in por_jid:
            return None
        from app.services.chat_alias import canonical_chat_jid

        with self._database.transaction() as sesion:
            canonico = canonical_chat_jid(sesion, candidato.chat_jid) or candidato.chat_jid
            if canonico in por_jid:
                return None
            existe = sesion.execute(
                select(Chat.id).where(Chat.jid == canonico)
            ).scalar_one_or_none()
        if existe is None:
            return "la conversacion no existe en esta cuenta"
        return "la conversacion no estaba esperando ancla"
