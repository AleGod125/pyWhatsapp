"""Cierra los agujeros que dejan los mensajes en vivo indescifrables.

QUE HACE, EN UNA LINEA
----------------------
Para cada conversacion que perdio un mensaje en vivo sin remedio, pide su
borde reciente con el motor que ya existe --``rellenar_borde_reciente``-- y
deja que el historial traiga, firmado y por el camino de siempre, lo que el
cifrado en vivo no pudo entregar.

POR QUE ESTE MOTOR Y NO EL DE SIEMPRE
-------------------------------------
Son dos frentes distintos y confundirlos habria salido caro:

``ON_DEMAND`` de toda la vida excava **hacia atras** desde el ancla mas
antigua. Si el servidor ya dijo que por abajo no queda nada --que es el caso
de la conversacion medida, ``exhausted`` con 439 mensajes-- volver a pedirle
lo mismo no puede traer nada nuevo. Y reabrir ese ``exhausted`` habria borrado
un veredicto correcto para no ganar nada.

Lo que falta esta **por arriba**. ``rellenar_borde_reciente`` parte de una
referencia NUEVA y baja hasta empalmar con lo guardado, que es exactamente la
forma del agujero.

LA EVIDENCIA
------------
No es una sospecha. Un fallo de descifrado es la prueba de que llego un
mensaje concreto que no se pudo leer: mejor evidencia que comparar fechas con
el indice de WhatsApp Web, que ademas no esta disponible con el segundo
dispositivo apagado.

LO QUE NO SE HACE
-----------------
* No se reabre ningun ``exhausted``.
* No se toca el cursor historico ni se escribe ninguna ancla.
* No se fabrica ninguna referencia: el ancla del borde es un mensaje REAL ya
  guardado, con su identificador de WhatsApp de verdad.
* No se pide nada con el motor en duda: sin capacidad ``CONFIRMED`` se espera,
  igual que en todo el resto del proyecto.
"""

from __future__ import annotations

from typing import Any

from app.core.logging_setup import get_logger
from app.history.decision import RELLENAR_BORDE, decidir_borde

log = get_logger("PLAN_E")

#: Intentos por conversacion antes de dejarlo. Si tres rondas del borde no han
#: traido el mensaje, no esta donde lo estamos buscando y seguir es excavar a
#: ciegas.
MAXIMO_DE_INTENTOS = 3


class MissedLiveFiller:
    """Pide el borde reciente de las conversaciones que perdieron algo."""

    def __init__(self, database: Any, backfill: Any, *, registro: Any = None) -> None:
        self._database = database
        self._backfill = backfill
        if registro is None:
            from app.services.missed_live import registro as por_defecto

            registro = por_defecto()
        self._registro = registro

    async def cerrar_pendientes(self, client: Any) -> dict[str, Any]:
        """Intenta cerrar todos los agujeros vigentes. Nunca lanza."""
        resumen = {"chats": 0, "rellenados": 0, "mensajes": 0, "omitidos": 0}
        for agujero in self._registro.pendientes():
            resumen["chats"] += 1
            try:
                traidos = await self._cerrar_uno(client, agujero)
            except Exception:  # noqa: BLE001 - un chat no puede parar al resto
                log.exception("Fallo cerrando el borde de %s", agujero.chat_jid)
                continue
            if traidos is None:
                resumen["omitidos"] += 1
                continue
            resumen["rellenados"] += 1
            resumen["mensajes"] += traidos
        if resumen["chats"]:
            log.info(
                "[LIVE] bordes por mensajes perdidos: %d conversacion(es), "
                "%d rellenada(s), %d mensaje(s) recuperado(s)",
                resumen["chats"],
                resumen["rellenados"],
                resumen["mensajes"],
            )
        return resumen

    async def _cerrar_uno(self, client: Any, agujero: Any) -> int | None:
        """Cierra el de una conversacion. ``None`` si no se pidio nada."""
        if self._registro.intento(agujero.chat_jid) > MAXIMO_DE_INTENTOS:
            return None

        ancla = self._ancla_del_borde(agujero.chat_jid)
        decision = decidir_borde(
            hay_hueco=True,
            ancla_utilizable=ancla is not None,
            capacidad=str(self._backfill.capability_state()),
            peticion_viva=bool(getattr(self._backfill, "busy", False)),
            por_live_perdido=True,
        )
        if decision.accion != RELLENAR_BORDE:
            log.debug(
                "[LIVE] borde de %s sin pedir: %s", agujero.chat_jid, decision.motivo
            )
            return None

        resultado = await self._backfill.rellenar_borde_reciente(
            client, ancla, db_mas_nuevo=ancla.timestamp
        )
        traidos = int(getattr(resultado, "mensajes", 0) or 0)
        if traidos:
            # Solo se da por cerrado si trajo algo. Si no, se deja anotado para
            # otra ronda: perder la anotacion seria perder la unica prueba de
            # que ahi falta un mensaje.
            self._registro.cerrado(agujero.chat_jid)
        return traidos

    def _ancla_del_borde(self, chat_jid: str) -> Any:
        """El mensaje REAL mas nuevo de esa conversacion. ``None`` si no hay.

        Es desde donde se baja para empalmar. No se inventa: si la
        conversacion no tiene ni un mensaje con identificador de WhatsApp, no
        hay borde desde el que pedir y se deja como esta.
        """
        from sqlalchemy import select

        from app.history.cursor import is_valid_history_cursor_id
        from app.history.recent_gap import AnclaDelHueco
        from app.models import Chat, Message
        from app.services.chat_alias import canonical_chat_jid

        with self._database.transaction() as sesion:
            # El remitente puede llegar por telefono y la conversacion estar
            # guardada por LID, o al reves. Se resuelve con el MISMO resolutor
            # que usa el colector de anclas, para no acabar con dos verdades.
            #
            # Medido sobre la base local: `573002389304@s.whatsapp.net` no
            # tiene fila propia --directo=None-- pero canonicaliza a
            # `86531142340710@lid`, que es el chat 61022. Sin este paso el
            # agujero de ese contacto se anotaba y no hacia absolutamente
            # nada, en silencio.
            canonico = canonical_chat_jid(sesion, chat_jid) or chat_jid
            chat_id = sesion.execute(
                select(Chat.id).where(Chat.jid == canonico).limit(2)
            ).scalars().all()
            chat_id = chat_id[0] if len(chat_id) == 1 else None
            if chat_id is None:
                return None
            chat_jid = canonico
            filas = sesion.execute(
                select(
                    Message.whatsapp_message_id, Message.timestamp, Message.from_me
                )
                .where(
                    Message.chat_id == chat_id,
                    Message.whatsapp_message_id.is_not(None),
                    Message.whatsapp_message_id != "",
                )
                .order_by(Message.timestamp.desc(), Message.id.desc())
                .limit(20)
            ).all()

        for wamid, cuando, from_me in filas:
            if is_valid_history_cursor_id(wamid) and cuando:
                return AnclaDelHueco(
                    chat_id=int(chat_id),
                    chat_jid=chat_jid,
                    wa_msg_id=wamid,
                    timestamp=int(cuando),
                    from_me=bool(from_me),
                    source="missed_live",
                )
        return None
