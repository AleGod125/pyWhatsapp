"""Herramientas de diagnostico. NO forman parte del funcionamiento normal.

Viven detras de un interruptor apagado por defecto y existen para responder
preguntas concretas con datos reales, no para operar el sistema. Ninguna de
ellas escribe mensajes, multimedia ni estados de historial.

QUE HAY AQUI AHORA
------------------
Dos cosas: UNA peticion ON_DEMAND controlada, para saber donde se rompe la
cadena peticion -> ACK -> HistorySync -> correlacion; y el snapshot COMPLETO
de app-state.

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


def _cursor_historico(sesion, request_id: int):
    """El cursor EXACTO de una peticion que WhatsApp ya respondio.

    Sale de ``history_requests``, que guarda cada peticion emitida con su
    ancla. ``from_me`` no esta en esa tabla, asi que se recupera del mensaje o
    de la semilla que corresponde a ese identificador -- las dos fuentes se
    comprueban y tienen que coincidir.
    """
    from sqlalchemy import select

    from app.models import HistoryRequest, HistorySeed, Message

    fila = sesion.execute(
        select(HistoryRequest).where(HistoryRequest.id == request_id)
    ).scalar_one_or_none()
    if fila is None:
        return None, "esa peticion no existe"
    if fila.status != "received":
        return None, f"esa peticion no respondio (status={fila.status})"

    del_mensaje = sesion.execute(
        select(Message.from_me).where(
            Message.whatsapp_message_id == fila.cursor_message_id
        )
    ).scalar_one_or_none()
    de_la_semilla = sesion.execute(
        select(HistorySeed.from_me).where(
            HistorySeed.wa_msg_id == fila.cursor_message_id
        )
    ).scalar_one_or_none()

    from_me = del_mensaje if del_mensaje is not None else de_la_semilla
    if from_me is None:
        return None, "no se pudo recuperar from_me de ese cursor"
    if del_mensaje is not None and de_la_semilla is not None:
        if bool(del_mensaje) != bool(de_la_semilla):
            return None, "el mensaje y la semilla discrepan en from_me"

    return {
        "chat_jid": fila.chat_jid,
        "chat_id": fila.chat_id,
        "wa_msg_id": fila.cursor_message_id,
        "timestamp": int(fila.cursor_timestamp),
        "from_me": bool(from_me),
        "historical_response": True,
        "historical_messages_count": int(fila.response_count or 0),
        "historical_sent_at": fila.sent_at.isoformat() if fila.sent_at else None,
    }, None


def _cursor_actual(sesion, chat_jid: str):
    """Lo que hoy devolveria el motor para ese mismo chat."""
    from sqlalchemy import select

    from app.history.cursor import get_valid_history_cursor
    from app.models import Chat

    # Por CUENTA. El mismo JID existe en tantas filas como cuentas hablen con
    # ese contacto: `scalar_one_or_none()` reventaba ahi, y `.first()` habria
    # devuelto la conversacion de otra persona sin avisar.
    from app.services.account_scope import chat_id_de

    chat_id = chat_id_de(sesion, chat_jid)
    if chat_id is None:
        return None, "ese chat no existe en esta base"
    cursor = get_valid_history_cursor(sesion, chat_id=chat_id, chat_jid=chat_jid)
    if cursor is None:
        return None, "ese chat no tiene ancla utilizable hoy"
    return {
        "chat_jid": chat_jid,
        "chat_id": chat_id,
        "wa_msg_id": cursor.message_id,
        "timestamp": int(cursor.timestamp),
        "from_me": bool(cursor.from_me),
        "source": cursor.source,
    }, None


@diagnostics.post("/ondemand/probe")
def ondemand_probe():
    """UNA peticion ON_DEMAND con un cursor elegido, sin mutar nada.

    Dos modos, y la unica diferencia entre ellos es el ancla:

    ``known_good``
        el cursor EXACTO de una peticion que WhatsApp respondio de verdad,
        recuperado de ``history_requests``.
    ``current``
        el mismo chat, pero con el ancla que el motor calcularia hoy.

    Compararlos responde la pregunta que ninguna otra prueba responde: si el
    problema esta en el protocolo o en los cursores que estamos usando.

    No escribe cursor, ni estado, ni intentos, ni mensajes. Lo unico que puede
    cambiar es la capacidad, y solo si llega una respuesta correlacionada.
    """
    from flask import request

    rt = _runtime()
    backfill = getattr(rt, "backfill", None)
    cliente = getattr(rt.client, "_client", None)
    loop = getattr(rt.client, "_loop", None)
    if backfill is None or cliente is None or loop is None:
        return (
            jsonify(
                {
                    "error": {
                        "code": "SESSION_NOT_CONNECTED",
                        "message": "Hace falta la sesion conectada para pedir historial.",
                    }
                }
            ),
            409,
        )

    cuerpo = request.get_json(silent=True) or {}
    modo = str(cuerpo.get("mode") or "known_good")
    if modo not in ("known_good", "current"):
        return (
            jsonify({"error": {"code": "BAD_MODE", "message": "mode: known_good | current"}}),
            400,
        )

    with rt.database.transaction() as sesion:
        if modo == "known_good":
            try:
                request_id = int(cuerpo.get("request_id"))
            except (TypeError, ValueError):
                return (
                    jsonify(
                        {
                            "error": {
                                "code": "MISSING_REQUEST_ID",
                                "message": "known_good necesita request_id de history_requests",
                            }
                        }
                    ),
                    400,
                )
            elegido, fallo = _cursor_historico(sesion, request_id)
        else:
            chat_jid = str(cuerpo.get("chat_jid") or "")
            if not chat_jid:
                return (
                    jsonify(
                        {"error": {"code": "MISSING_CHAT", "message": "current necesita chat_jid"}}
                    ),
                    400,
                )
            elegido, fallo = _cursor_actual(sesion, chat_jid)

    if elegido is None:
        return jsonify({"error": {"code": "NO_CURSOR", "message": fallo}}), 409

    if backfill.busy or backfill.in_flight:
        return (
            jsonify(
                {
                    "error": {
                        "code": "BACKFILL_BUSY",
                        "message": "Hay una peticion ON_DEMAND en curso; no se lanza otra.",
                    }
                }
            ),
            409,
        )

    log.info("[ON_DEMAND_TEST] %s iniciado", modo)
    try:
        futuro = asyncio.run_coroutine_threadsafe(
            backfill.request_diagnostico(
                cliente,
                chat_jid=elegido["chat_jid"],
                message_id=elegido["wa_msg_id"],
                timestamp=elegido["timestamp"],
                from_me=elegido["from_me"],
            ),
            loop,
        )
        limite = float(rt.settings.history_request_timeout) + 30.0
        medido = futuro.result(timeout=limite)
    except Exception as exc:  # noqa: BLE001 - se reporta, no se traga
        log.exception("[ON_DEMAND_TEST] la prueba fallo")
        return jsonify({"error": {"code": "PROBE_FAILED", "message": str(exc)[:300]}}), 500

    if medido.get("error") == "busy":
        return jsonify({"error": {"code": "BACKFILL_BUSY", "message": "en curso"}}), 409

    # El ancla no se devuelve entera: es un identificador de mensaje real.
    cursor = dict(elegido)
    cursor["wa_msg_id_len"] = len(cursor.pop("wa_msg_id", "") or "")
    cursor["chat_jid"] = cursor["chat_jid"].split("@")[0][:6] + "...@" + cursor["chat_jid"].split("@")[-1]
    return jsonify({"mode": modo, "cursor": cursor, "result": medido})


@diagnostics.post("/ondemand/canary")
def ondemand_canary():
    """UNA peticion ON_DEMAND, con la sesion que YA esta conectada.

    Existe porque desde fuera no se puede distinguir "el telefono no contesto"
    de "la respuesta llego y no encontro a quien despertar": las dos se ven
    igual, como un timeout de 45 s. Esta ruta usa el mismo constructor, el
    mismo destino y el mismo waiter que el backfill normal -- no hay un camino
    especial que pueda funcionar aqui y fallar alli -- y devuelve lo que se
    observo en cada tramo.

    Manda UNA sola peticion. No promueve chats, no inserta anclas y no toca
    ``waiting_seed``.
    """
    rt = _runtime()
    backfill = getattr(rt, "backfill", None)
    cliente = getattr(rt.client, "_client", None)
    loop = getattr(rt.client, "_loop", None)
    if backfill is None or cliente is None or loop is None:
        return (
            jsonify(
                {
                    "error": {
                        "code": "SESSION_NOT_CONNECTED",
                        "message": "Hace falta la sesion conectada para pedir historial.",
                    }
                }
            ),
            409,
        )
    if backfill.busy:
        return (
            jsonify(
                {
                    "error": {
                        "code": "BACKFILL_BUSY",
                        "message": "Ya hay una excavacion en marcha; no se lanza otra.",
                    }
                }
            ),
            409,
        )

    antes = getattr(backfill, "capability_state", lambda: "UNKNOWN")()
    peticiones_antes = backfill.stats.requests_sent
    respuestas_antes = backfill.stats.responses_received
    timeouts_antes = backfill.stats.timeouts

    log.info("[BACKFILL] canary iniciado (peticion unica de diagnostico)")
    comenzo = time.monotonic()
    try:
        futuro = asyncio.run_coroutine_threadsafe(
            backfill.run_canary(cliente, max_rounds=1), loop
        )
        # Un poco mas que el timeout de la peticion, para poder distinguir
        # "no contesto" de "esta ruta se quedo corta".
        limite = float(rt.settings.history_request_timeout) + 30.0
        exito = bool(futuro.result(timeout=limite))
    except Exception as exc:  # noqa: BLE001 - se reporta, no se traga
        log.exception("El canary de diagnostico fallo")
        return (
            jsonify({"error": {"code": "CANARY_FAILED", "message": str(exc)[:300]}}),
            500,
        )

    enviadas = backfill.stats.requests_sent - peticiones_antes
    recibidas = backfill.stats.responses_received - respuestas_antes
    resultado = {
        "request_sent": enviadas > 0,
        "requests": enviadas,
        "ack": enviadas > 0,
        "enc_type": getattr(backfill, "ultimo_enc_type", None),
        "history_response": recibidas > 0,
        "responses": recibidas,
        "timeouts": backfill.stats.timeouts - timeouts_antes,
        "latency_seconds": getattr(backfill, "ultima_latencia", None),
        "uncorrelated_responses": getattr(backfill, "respuestas_sin_waiter", 0),
        "capability_before": antes,
        "capability_after": backfill.capability_state(),
        "canary_success": exito,
        "reason": getattr(backfill, "last_canary_reason", None),
        "elapsed_seconds": round(time.monotonic() - comenzo, 2),
        # Se dice explicitamente: esta ruta no promueve nada.
        "seeds_inserted": 0,
        "waiting_seed_changed": 0,
    }
    log.info(
        "[BACKFILL] canary %s enc=%s respuesta=%s latencia=%s capability=%s",
        "OK" if exito else "sin exito",
        resultado["enc_type"],
        resultado["history_response"],
        resultado["latency_seconds"],
        resultado["capability_after"],
    )
    return jsonify(resultado)


