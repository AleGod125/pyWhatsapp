"""Herramientas de diagnostico. NO forman parte del funcionamiento normal.

Viven detras de un interruptor apagado por defecto y existen para responder
preguntas concretas con datos reales, no para operar el sistema. Ninguna de
ellas escribe mensajes, multimedia ni estados de historial.

QUE HAY AQUI AHORA
------------------
El snapshot COMPLETO de app-state. Es la medicion que faltaba: lo que se midio
antes fueron 61 parches INCREMENTALES, y las dos colecciones donde viven las
acciones que llevan rangos de mensajes (``regular`` y ``regular_low``) no
mandaron nada porque ya estaban al dia.

``fetch(name, full_sync=True)`` es otra cosa: borra la version guardada y pide
el estado ENTERO de la coleccion. Si en algun sitio hay claves de mensaje
reales para los chats que esperan semilla, es ahi.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from flask import Blueprint, current_app, jsonify

from app.core.logging_setup import get_logger

log = get_logger("BACKFILL")

diagnostics = Blueprint("diagnostics_v1", __name__, url_prefix="/api/v1/diagnostics")

# Colecciones de app-state, en el orden en que interesa mirarlas: ``regular``
# es donde viven archivar y marcar como leido, que son las acciones que
# necesitan decir HASTA QUE mensaje.
COLECCIONES = ("regular", "regular_low", "critical_unblock_low", "regular_high")


def _runtime() -> Any:
    return current_app.config["RUNTIME"]


@diagnostics.post("/appstate/full-sync")
def appstate_full_sync():
    """Pide el snapshot COMPLETO de una coleccion de app-state y lo mide.

    Solo observa: las mutaciones se inspeccionan buscando ``MessageKey``
    reales y NO se escribe nada en PostgreSQL ni se lanza ningun backfill.

    Requiere ``COMPAT_APPSTATE_SEEDS=true``, porque sin la instrumentacion
    activa las mutaciones pasarian sin medirse.
    """
    from flask import request

    rt = _runtime()
    if not rt.settings.compat_appstate_seeds:
        return (
            jsonify(
                {
                    "error": {
                        "code": "APPSTATE_SEEDS_DISABLED",
                        "message": (
                            "La instrumentacion esta apagada. Arranca con "
                            "COMPAT_APPSTATE_SEEDS=true para poder medir."
                        ),
                    }
                }
            ),
            409,
        )

    cliente = getattr(rt.client, "_client", None)
    syncer = getattr(cliente, "_app_state_syncer", None)
    loop = getattr(rt.client, "_loop", None)
    if syncer is None or loop is None:
        return (
            jsonify(
                {
                    "error": {
                        "code": "SESSION_NOT_CONNECTED",
                        "message": "Hace falta la sesion conectada para pedir el snapshot.",
                    }
                }
            ),
            409,
        )

    cuerpo = request.get_json(silent=True) or {}
    nombre = str(cuerpo.get("collection") or "regular")
    if nombre not in COLECCIONES:
        return (
            jsonify(
                {
                    "error": {
                        "code": "UNKNOWN_COLLECTION",
                        "message": f"Colecciones validas: {', '.join(COLECCIONES)}",
                    }
                }
            ),
            400,
        )

    from app.compat import appstate_seeds

    antes = appstate_seeds.report()
    marca = {
        "mutations": antes.mutations_scanned,
        "candidates": antes.real_candidates,
    }

    log.info("[SEED] snapshot COMPLETO de app-state: coleccion=%s", nombre)
    comenzo = time.monotonic()
    try:
        futuro = asyncio.run_coroutine_threadsafe(
            syncer.fetch(nombre, full_sync=True), loop
        )
        # Un snapshot no deberia tardar mas que esto; si tarda, algo va mal y
        # es mejor decirlo que dejar la peticion HTTP colgada.
        mutaciones = futuro.result(timeout=120)
    except Exception as exc:  # noqa: BLE001 - se reporta, no se traga
        log.exception("El snapshot de app-state fallo")
        return (
            jsonify(
                {
                    "error": {
                        "code": "FULL_SYNC_FAILED",
                        "message": str(exc)[:300],
                    },
                    "collection": nombre,
                }
            ),
            500,
        )

    duracion = int((time.monotonic() - comenzo) * 1000)
    despues = appstate_seeds.report()
    appstate_seeds.log_summary()

    resultado = {
        "collection": nombre,
        "snapshot_received": True,
        "duration_ms": duracion,
        "mutations_decoded": len(mutaciones or []),
        "mutations_scanned": despues.mutations_scanned - marca["mutations"],
        "message_keys_found": despues.mutations_with_message_key,
        "valid_candidates": despues.real_candidates - marca["candidates"],
        "unique_chats": despues.unique_chats,
        "rejected": {
            "not_real_id": despues.rejected_not_real_id,
            "wrong_chat": despues.rejected_wrong_chat,
            "broadcast": despues.rejected_broadcast,
        },
        # Nada de esto toca la base: es una medicion.
        "db_writes": 0,
        "backfill_triggered": False,
    }
    # Que campos trae de verdad cada SyncActionValue. Es lo que separa "mi
    # detector no encontro nada" de "no hay nada que encontrar".
    from pywhats.proto import SyncActionValue

    conocidos = {f.number: f.name for f in SyncActionValue.DESCRIPTOR.fields}
    resultado["wire_fields"] = [
        {
            "field": numero,
            "count": cuantas,
            "name": conocidos.get(numero) or "NO MODELADO POR pywhats",
        }
        for numero, cuantas in sorted(appstate_seeds.campos_vistos().items())
    ]
    resultado["waiting_seed_matches"] = _emparejar_con_fantasmas(rt, despues)
    log.info(
        "[SEED] snapshot %s: mutaciones=%d claves=%d candidatos=%d chats=%d (%d ms)",
        nombre,
        resultado["mutations_decoded"],
        resultado["message_keys_found"],
        resultado["valid_candidates"],
        resultado["unique_chats"],
        duracion,
    )
    return jsonify(resultado)


def _emparejar_con_fantasmas(rt: Any, informe: Any) -> list[dict[str, Any]]:
    """Cuales de los candidatos sirven a un chat que ESPERA semilla.

    Encontrar claves no basta: lo que decide si esta via vale es que alguna
    pertenezca a un chat sin historial. Se compara por usuario, para que un
    contacto que aparece por telefono y por LID cuente como el mismo.
    """
    from sqlalchemy import select

    from app.core.identity import mask
    from app.models import SEEDLESS_STATUSES, Chat, ChatHistoryState

    if rt.database is None or not informe.por_chat:
        return []

    def usuario(jid: str) -> str:
        return jid.split("@")[0].split(":")[0].split(".")[0]

    coincidencias: list[dict[str, Any]] = []
    try:
        with rt.database.transaction() as sesion:
            dormidos = sesion.execute(
                select(Chat.id, Chat.jid, Chat.name).join(
                    ChatHistoryState, ChatHistoryState.chat_jid == Chat.jid
                ).where(ChatHistoryState.history_status.in_(tuple(SEEDLESS_STATUSES)))
            ).all()
    except Exception:  # noqa: BLE001
        log.debug("No se pudieron leer los chats sin semilla")
        return []

    por_usuario = {usuario(jid): candidatos for jid, candidatos in informe.por_chat.items()}
    for chat_id, jid, nombre in dormidos:
        candidatos = por_usuario.get(usuario(jid))
        if not candidatos:
            continue
        coincidencias.append(
            {
                "chat_id": chat_id,
                "chat_name": nombre,
                "jid_masked": mask(jid),
                "seeds": [
                    {
                        "message_id_fp": c.huella,
                        "from_me": c.from_me,
                        "collection": c.collection,
                        "index_type": c.index_type,
                    }
                    for c in candidatos
                ],
            }
        )
    return coincidencias
