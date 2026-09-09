"""Conversaciones que dejan de aparecer. Se anotan; no se borran.

EL PROBLEMA
-----------
Una pasada de sincronización trae una foto de las conversaciones que hay. Esa
foto puede venir **incompleta**: se midió un `INITIAL_BOOTSTRAP` con 41
conversaciones y otro, de la misma cuenta, con 39. Si faltar en una foto
bastara para borrar, dos pasadas seguidas se llevarían por delante historial
que costó horas recuperar.

Así que una ausencia **no es** una eliminación. Aquí sólo se anota la
evidencia, y hacen falta varias ausencias seguidas para llamar a algo dudoso.

LO QUE ESTO NO HACE, Y NO VA A HACER
------------------------------------
**No borra.** Ni conversaciones, ni mensajes, ni anclas, ni estados. Hoy no
hay una sola línea en el proyecto que borre una conversación por no aparecer
en una foto, y esta capa existe para que siga siendo así: si algún día alguien
quiere limpiar, tendrá la evidencia delante en vez de un impulso.

Marcar como dudosa es reversible y no cuesta nada. Borrar no se deshace.

DONDE VIVE
----------
En ``chats.raw_metadata``, que ya es JSONB. Sin columnas nuevas y sin
migración: el dato es de diagnóstico, no de producto, y no merece cambiar el
esquema.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

from app.core.logging_setup import get_logger

log = get_logger("SYNC")

#: Ausencias SEGUIDAS antes de llamar dudosa a una conversación.
#:
#: Tres, y no una: una foto incompleta es un suceso normal —se midieron 41 y
#: 39 conversaciones en dos arranques de la misma cuenta— y actuar sobre la
#: primera convertiría un hueco temporal en una decisión.
AUSENCIAS_PARA_DUDOSA = 3

#: Las claves que se guardan dentro de ``raw_metadata``.
VISTA_POR_ULTIMA_VEZ = "last_seen_sync_at"
AUSENCIAS = "missing_sync_count"
DUDOSA = "stale"


@dataclass
class ResultadoDelRecuento:
    """Qué pasó con las conversaciones de esta pasada."""

    vistas: int = 0
    ausentes: int = 0
    nuevas_dudosas: int = 0
    dudosas_totales: int = 0
    recuperadas: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "seen": self.vistas,
            "missing": self.ausentes,
            "new_stale": self.nuevas_dudosas,
            "stale_total": self.dudosas_totales,
            "recovered": self.recuperadas,
        }

    def resumen(self) -> str:
        return (
            f"vistas={self.vistas} ausentes={self.ausentes} "
            f"dudosas={self.dudosas_totales} recuperadas={self.recuperadas}"
        )


def anotar_snapshot(
    sesion: Any,
    jids_vistos: Iterable[str],
    *,
    account_id: Any = None,
    ahora: float | None = None,
) -> ResultadoDelRecuento:
    """Anota qué conversaciones aparecieron en esta foto, y cuáles no.

    Una foto **vacía** no cuenta: si la pasada no trajo ninguna conversación,
    lo que falló fue la pasada, y castigar a las 41 por eso es exactamente el
    error que esta capa evita.
    """
    from app.models import Chat

    vistos = {j for j in jids_vistos if j}
    resultado = ResultadoDelRecuento()

    if not vistos:
        log.debug("[SYNC] foto sin conversaciones: no se anota ninguna ausencia")
        return resultado

    ahora = time.time() if ahora is None else ahora

    consulta = sesion.query(Chat)
    if account_id is not None:
        consulta = consulta.filter(Chat.whatsapp_account_id == account_id)

    for chat in consulta.all():
        metadatos = dict(chat.raw_metadata or {})
        ausencias = int(metadatos.get(AUSENCIAS, 0) or 0)
        era_dudosa = bool(metadatos.get(DUDOSA))

        if chat.jid in vistos:
            resultado.vistas += 1
            metadatos[VISTA_POR_ULTIMA_VEZ] = ahora
            metadatos[AUSENCIAS] = 0
            if era_dudosa:
                # Volvio a aparecer: deja de ser dudosa. La duda se quita con
                # la misma facilidad con la que se puso.
                metadatos[DUDOSA] = False
                resultado.recuperadas += 1
        else:
            resultado.ausentes += 1
            ausencias += 1
            metadatos[AUSENCIAS] = ausencias
            if ausencias >= AUSENCIAS_PARA_DUDOSA and not era_dudosa:
                metadatos[DUDOSA] = True
                resultado.nuevas_dudosas += 1

        if metadatos.get(DUDOSA):
            resultado.dudosas_totales += 1

        # `raw_metadata` es JSONB: hay que reasignar el diccionario entero
        # para que SQLAlchemy vea el cambio. Mutarlo en el sitio no marca la
        # fila como sucia y el cambio se pierde sin decir nada.
        chat.raw_metadata = metadatos

    if resultado.ausentes or resultado.dudosas_totales:
        log.info("[SYNC] conversaciones: %s", resultado.resumen())
    return resultado


def es_dudosa(chat: Any) -> bool:
    """Si esta conversación lleva varias fotos seguidas sin aparecer."""
    return bool((getattr(chat, "raw_metadata", None) or {}).get(DUDOSA))
