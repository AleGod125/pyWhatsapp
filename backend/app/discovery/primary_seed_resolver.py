"""Anclas que ya tenemos y no estábamos usando. Sólo fuentes propias.

LA IDEA, EN UNA FRASE
---------------------
Para pedirle el pasado a WhatsApp hace falta **una** referencia real por
conversación. No hace falta que la sesión principal se descargue el historial
entero: le basta con un identificador de mensaje real y su marca.

Y resulta que muchas conversaciones **ya tienen** mensajes reales guardados
—llegaron por el historial inicial, por excavación o en vivo, todos por
canales autenticados— y sin embargo figuran sin ancla, porque nadie promovió
ninguno de esos mensajes a referencia.

LO QUE SE MIDIO ANTES DE ESCRIBIR ESTO
--------------------------------------
Sobre la base real, contando sólo lo que no viene del segundo dispositivo::

    conversaciones                                        51
    con ancla de fuente propia                             9
    SIN ancla propia pero CON mensaje real guardado       23   <- esto
    SIN ancla propia y sin ningún mensaje real            20

Esas 23 son gratis: el mensaje está, es real, y está autenticado. Lo único que
faltaba era anotarlo como referencia.

HONESTIDAD SOBRE LO QUE ESTO ES Y NO ES
---------------------------------------
Esto **no inventa cobertura nueva** en una instalación desde cero: si la
sesión principal nunca recibió un mensaje de una conversación, aquí no va a
aparecer. Lo que hace es que la cobertura **no se pierda**: una vez que una
conversación tiene mensajes reales, no vuelve a quedarse sin poder pedir su
pasado, y deja de depender del segundo dispositivo para siempre.

DE DONDE PUEDE SALIR UNA REFERENCIA, Y EN QUE ORDEN
---------------------------------------------------
1. una ya guardada de fuente propia — no se toca;
2. el mensaje real más reciente de la conversación en la base.

Los blobs archivados los recorre ``BlobSeedScanner``, que ya existe y ya lleva
la cuenta de cuáles ha leído. Aquí no se duplica ese trabajo.

LO QUE ESTA PROHIBIDO, Y SE COMPRUEBA
-------------------------------------
Nada de identificadores fabricados, ni marcas sin identificador, ni el id de
la base, ni el del chat, ni referencias prestadas de otra conversación. Y
**nunca** el almacén del navegador: si esto pudiera mirarlo, la mejora que
mide dejaría de significar «lo que consigue la sesión principal sola».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("PLAN_E")

#: Orígenes que NO son de la sesión principal. Una referencia que venga de
#: aquí no cuenta como cobertura propia, y por eso una conversación que sólo
#: tenga de éstas se considera pendiente de resolver.
FUENTES_DEL_NAVEGADOR = frozenset({"web_store", "web_fetch1", "web_local_fetch"})

#: De dónde puede salir un mensaje que sirva de referencia. Todos llegaron por
#: un canal autenticado: historial inicial, excavación o tiempo real.
FUENTES_DE_MENSAJE_ACEPTABLES = frozenset(
    {"initial_history", "on_demand", "live"}
)

#: La etiqueta con la que se anota. Dice de dónde salió DE VERDAD, que es lo
#: único que después permite medir si una fuente aporta algo.
ORIGEN = "stored_message"


@dataclass
class ResultadoDelResolutor:
    """Lo que pide §25: qué había, qué se encontró y qué quedó."""

    conocidos: int = 0
    esperando_antes: int = 0
    ya_tenian: int = 0
    encontrados_en_mensajes: int = 0
    promovidos: int = 0
    despertados: int = 0
    esperando_despues: int = 0
    por_fuente: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "known_chats": self.conocidos,
            "waiting_before": self.esperando_antes,
            "already_had": self.ya_tenian,
            "found_in_messages": self.encontrados_en_mensajes,
            "promoted": self.promovidos,
            "woken": self.despertados,
            "waiting_after": self.esperando_despues,
            "by_source": dict(sorted(self.por_fuente.items())),
        }

    def resumen(self) -> str:
        return (
            f"known={self.conocidos} waiting_before={self.esperando_antes} "
            f"found={self.encontrados_en_mensajes} promoted={self.promovidos} "
            f"waiting_after={self.esperando_despues}"
        )


class PrimarySeedResolver:
    """Promueve a referencia lo que la sesión principal ya tiene guardado.

    No abre ningún archivo, no pide nada a la red y no habla con el segundo
    dispositivo. Mira la base, y lo que encuentra lo entrega al colector de
    siempre, que es quien valida, deduplica y despierta la conversación.
    """

    def __init__(self, database: Any, *, account_id: Any = None) -> None:
        self._database = database
        self._account_id = account_id

    # -- Lo que falta --------------------------------------------------------

    def chats_sin_ancla_propia(self, sesion: Any) -> list[tuple[int, str]]:
        """Conversaciones sin ninguna referencia de fuente propia.

        Se pregunta por FUENTE y no por «tiene o no tiene»: una conversación
        cuya única referencia la trajo el navegador sigue dependiendo de él, y
        eso es exactamente lo que se quiere dejar de necesitar.
        """
        from sqlalchemy import select

        from app.models import Chat, HistorySeed

        propias = (
            select(HistorySeed.chat_jid)
            .where(HistorySeed.valid.is_(True))
            .where(HistorySeed.source.not_in(tuple(FUENTES_DEL_NAVEGADOR)))
        )
        if self._account_id is not None:
            propias = propias.where(
                HistorySeed.whatsapp_account_id == self._account_id
            )

        consulta = select(Chat.id, Chat.jid).where(Chat.jid.not_in(propias))
        if self._account_id is not None:
            consulta = consulta.where(Chat.whatsapp_account_id == self._account_id)
        return [(fila[0], fila[1]) for fila in sesion.execute(consulta).all()]

    # -- Lo que hay ----------------------------------------------------------

    def referencia_de(self, sesion: Any, chat_jid: str) -> Any:
        """El mensaje real más reciente de esa conversación, o ``None``.

        **El más reciente y no el más antiguo**, a propósito: la excavación va
        hacia atrás desde la referencia, así que anclar en el mensaje más nuevo
        deja por delante todo el pasado. Anclar en el más viejo dejaría fuera
        justo lo que todavía no se tiene.
        """
        from sqlalchemy import select

        from app.history.seed_collector import SeedCandidate
        from app.models import Message
        from app.services.repository import is_valid_history_cursor_id

        fila = sesion.execute(
            select(
                Message.whatsapp_message_id,
                Message.timestamp,
                Message.from_me,
                Message.source,
                Message.message_type,
            )
            .where(Message.chat_jid == chat_jid)
            .where(Message.whatsapp_message_id.is_not(None))
            .where(Message.source.in_(tuple(FUENTES_DE_MENSAJE_ACEPTABLES)))
            .order_by(Message.timestamp.desc())
            .limit(1)
        ).first()
        if fila is None:
            return None

        wamid, marca, mio, origen, tipo = fila
        # El MISMO filtro que usa el motor de excavación. Si no serviría para
        # pedir historial, no es una referencia: anotarla daría una conversación
        # que parece resuelta y después no trae nada.
        if not wamid or not is_valid_history_cursor_id(wamid):
            return None
        try:
            marca_entera = int(marca)
        except (TypeError, ValueError):
            return None
        if marca_entera <= 0:
            return None

        return SeedCandidate(
            chat_jid=chat_jid,
            wa_msg_id=wamid,
            timestamp=marca_entera,
            from_me=bool(mio),
            source=ORIGEN,
            message_type=tipo,
        )

    # -- El trabajo ----------------------------------------------------------

    def resolver(self, colector: Any, *, limite: int = 500) -> ResultadoDelResolutor:
        """Busca y promueve. Devuelve lo que pasó, para poder medirlo.

        El colector es el de siempre (§20): no se insertan filas por un camino
        paralelo, así que la validación, la deduplicación y el despertado de la
        conversación son exactamente los de todo lo demás.
        """
        from sqlalchemy import func, select

        from app.models import Chat

        resultado = ResultadoDelResolutor()
        candidatos = []

        with self._database.transaction() as sesion:
            total = select(func.count()).select_from(Chat)
            if self._account_id is not None:
                total = total.where(Chat.whatsapp_account_id == self._account_id)
            resultado.conocidos = int(sesion.execute(total).scalar() or 0)

            faltantes = self.chats_sin_ancla_propia(sesion)
            resultado.esperando_antes = len(faltantes)
            resultado.ya_tenian = resultado.conocidos - resultado.esperando_antes

            for _chat_id, chat_jid in faltantes[:limite]:
                candidato = self.referencia_de(sesion, chat_jid)
                if candidato is None:
                    continue
                resultado.encontrados_en_mensajes += 1
                candidatos.append(candidato)

        antes_validas = int(getattr(colector.metricas, "validas", 0) or 0)
        antes_despertados = int(getattr(colector.metricas, "despertados", 0) or 0)
        if candidatos:
            colector.observe_many(candidatos)
        resultado.promovidos = (
            int(getattr(colector.metricas, "validas", 0) or 0) - antes_validas
        )
        resultado.despertados = (
            int(getattr(colector.metricas, "despertados", 0) or 0) - antes_despertados
        )

        with self._database.transaction() as sesion:
            resultado.esperando_despues = len(self.chats_sin_ancla_propia(sesion))

        if resultado.encontrados_en_mensajes or resultado.esperando_antes:
            log.info("[PRIMARY_SEEDS] %s", resultado.resumen())
        return resultado
