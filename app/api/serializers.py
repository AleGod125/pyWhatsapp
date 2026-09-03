"""Traduccion de filas de PostgreSQL a JSON para el frontend.

DOS REGLAS QUE NO SE ROMPEN
---------------------------
1. NUNCA sale una ruta del sistema de archivos. Ni ``C:\\Users\\...``, ni
   ``local_path``, ni la carpeta de multimedia. El frontend recibe URLs HTTP
   (``/api/v1/media/123/file``) y nada mas. Una ruta de Windows en la respuesta
   filtra el nombre del usuario, no sirve desde un navegador y ata el frontend
   a la maquina.
2. NUNCA sale material sensible: ni ``raw_proto``, ni ``media_key``, ni
   ``file_enc_sha256``, ni el payload del QR en texto. El QR se sirve como
   imagen.

Hay una prueba que recorre las respuestas buscando rutas de Windows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.core.avatars import avatar_color, initials
from app.core.previews import preview_for
from app.core.system_message import describe_system_message

API_PREFIX = "/api/v1"

# Estados de descarga en los que el archivo ya no se va a poder recuperar.
TERMINAL_MEDIA = ("unavailable", "expired")


def iso(timestamp: int | None) -> str | None:
    """Epoch en segundos -> ISO-8601 con zona. ``None`` si no hay valor.

    Se manda ISO ademas del epoch porque un frontend no deberia tener que
    adivinar la unidad ni la zona horaria.
    """
    if not timestamp:
        return None
    return (
        datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
        .astimezone()
        .isoformat()
    )


def chat_to_json(summary: Any) -> dict[str, Any]:
    """Fila del sidebar."""
    from app.models import COMPLETE_STATUSES, SEEDLESS_STATUSES

    return {
        "id": summary.id,
        "jid": summary.jid,
        "display_name": summary.display_name,
        "chat_type": summary.chat_type,
        "preview": summary.last_message,
        "last_message_at": iso(summary.last_message_timestamp),
        "last_message_timestamp": summary.last_message_timestamp,
        "message_count": summary.message_count,
        # El estado del historial va EN LA FILA. Sin el, el frontend solo ve
        # una vista previa vacia y pinta "Mensaje no compatible", que es falso:
        # el chat no tiene un mensaje ilegible, tiene CERO mensajes porque
        # todavia no hay una referencia con la que pedir su historial.
        "history_status": getattr(summary, "history_status", None),
        "waiting_seed": getattr(summary, "history_status", None) in SEEDLESS_STATUSES,
        "history_complete": getattr(summary, "history_status", None)
        in COMPLETE_STATUSES,
        "avatar": {
            "initials": initials(summary.display_name),
            "color": avatar_color(summary.jid),
        },
    }


def message_to_json(row: Any, media: Any = None) -> dict[str, Any]:
    """Una burbuja.

    ``raw_metadata`` no sale entero: solo se usa aqui dentro para rotular un
    evento de sistema. Y ``raw_proto`` ni se consulta: las consultas de la GUI
    y de la API lo dejan fuera a proposito por peso.
    """
    metadata = getattr(row, "raw_metadata", None)
    tipo = row.message_type

    cuerpo: dict[str, Any] = {
        "id": row.id,
        "whatsapp_message_id": row.whatsapp_message_id,
        "chat_id": getattr(row, "chat_id", None),
        "type": tipo,
        "text": row.text,
        "from_me": bool(row.from_me),
        "sender_jid": row.sender_jid,
        "sender_lid": getattr(row, "sender_lid", None),
        "timestamp": row.timestamp,
        "sent_at": iso(row.timestamp),
        "preview": preview_for(tipo, row.text, metadata=metadata),
        "media": media_to_json(media) if media is not None else None,
    }

    if tipo == "system":
        # El evento se interpreta del protobuf (aqui, del stub_type que quedo
        # en raw_metadata), nunca del texto. Un stub desconocido se declara
        # desconocido en vez de inventarle un nombre.
        evento = describe_system_message(None, metadata)
        cuerpo["system_event"] = {
            "kind": evento.kind,
            "label": evento.label,
            "icon": evento.icon,
            "display": evento.display,
            "known": evento.known,
            "stub_type": evento.stub_type,
        }
    return cuerpo


def media_to_json(media: Any) -> dict[str, Any]:
    """Adjunto. Solo URLs, jamas rutas locales."""
    descargado = media.download_status == "downloaded"
    no_disponible = media.download_status in TERMINAL_MEDIA
    visual = media.media_type in ("image", "sticker", "gif")

    return {
        "id": media.id,
        "type": media.media_type,
        "mime_type": media.mime_type,
        "file_name": media.file_name,
        "file_size": media.file_size,
        "duration_seconds": media.duration_seconds,
        "width": media.width,
        "height": media.height,
        "status": media.download_status,
        "available": descargado,
        # ``unavailable``/``expired`` son TERMINALES: el archivo ya no esta en
        # el servidor. El mensaje sigue siendo parte del backup; lo que falta
        # es el adjunto, y eso se dice explicitamente.
        "permanently_unavailable": no_disponible,
        "file_url": f"{API_PREFIX}/media/{media.id}/file" if descargado else None,
        "thumbnail_url": (
            f"{API_PREFIX}/media/{media.id}/thumbnail"
            if descargado and visual
            else None
        ),
    }


def owner_to_json(runtime: Any) -> dict[str, Any]:
    """Quien tiene el cerrojo de la sesion, y si somos nosotros.

    Se lee del cerrojo en disco, no de una variable en memoria: la pregunta
    interesante es justo cuando NO somos nosotros. Nunca se expone la ruta del
    interprete ni la linea de ordenes: son rutas locales, y esas no salen por
    la API (ver la cabecera de este modulo).
    """
    import os

    from app.core.lock import probe

    try:
        titular = probe(runtime.settings.session_dir)
    except Exception:  # noqa: BLE001 - un estado no puede reventar por esto
        titular = None

    if titular is None:
        return {
            "session_owner_pid": None,
            "session_owner_name": None,
            "this_process_owns_session": False,
        }
    return {
        "session_owner_pid": titular.pid,
        "session_owner_name": titular.owner,
        "this_process_owns_session": titular.pid == os.getpid(),
    }


def state_to_json(runtime: Any) -> dict[str, Any]:
    """Estado de la sesion. Nunca deduce "conectado" de que exista un archivo.

    SOLO datos seguros: ni payload del QR, ni claves Signal, ni secretos del
    dispositivo, ni configuracion de PostgreSQL, ni rutas locales.
    """
    from app.core.session_state import LINKED_STATES, NEEDS_PAIRING, PAIRING_STATES

    estado = runtime.state.state
    pairing = getattr(runtime, "pairing", None)

    return {
        "state": estado.value,
        "connected": estado.value == "CONNECTED",
        # "vinculado" es que este equipo pertenece a una cuenta, no que este
        # conectado ahora mismo: se puede estar vinculado y sin conexion.
        "linked": estado in LINKED_STATES and runtime.session_exists,
        "pairing_required": estado in NEEDS_PAIRING or estado in PAIRING_STATES,
        "pairing_in_progress": estado in PAIRING_STATES,
        "qr_available": bool(pairing is not None and pairing.available),
        "viewer_allowed": runtime.state.viewer_allowed,
        "generation": runtime.state.generation,
        # Que exista device.json NO significa que la sesion valga: el servidor
        # puede rechazarla con un 401. Se informa del hecho, no de la
        # conclusion.
        "session_file_present": runtime.session_exists,
        "whatsapp_enabled": runtime.info().whatsapp_enabled,
        "owner": runtime.owner,
        # QUIEN controla la sesion. Sin esto, con dos procesos arrancados no
        # habia forma de saber contra cual se estaba probando; se midio
        # exactamente eso, con un service.py del .venv y otro del Python
        # global a la vez.
        **owner_to_json(runtime),
        # Mensajes que no se pudieron descifrar en esta sesion. Es un
        # contador de diagnostico: el reintento por receipt es cosa de
        # Signal y sigue su curso sin que esto lo altere.
        "decrypt_errors": int(getattr(runtime, "decrypt_errors", 0) or 0),
    }


def qr_to_json(runtime: Any) -> dict[str, Any]:
    """Metadatos del QR. NUNCA el payload.

    ``generation`` sube con cada QR nuevo, y sirve al frontend para construir
    ``/session/qr/image?generation=N`` sin inventarse un anti-cache.
    """
    pairing = getattr(runtime, "pairing", None)
    if pairing is None:
        return {"available": False, "generation": 0, "expires_in_seconds": 0}

    foto = pairing.snapshot()
    cuerpo: dict[str, Any] = {
        "available": foto.available,
        "generation": foto.generation,
        "generated_at": foto.generated_at,
        "expires_at": foto.expires_at,
        "expires_in_seconds": foto.expires_in_seconds,
        "ttl_seconds": int(pairing.ttl_seconds),
        "expired": pairing.expired,
    }
    cuerpo["image_url"] = (
        f"{API_PREFIX}/session/qr/image?generation={foto.generation}"
        if foto.available
        else None
    )
    return cuerpo


def sync_to_json(runtime: Any) -> dict[str, Any]:
    """Progreso del trabajo de fondo, para la barra de estado del frontend."""
    orquestador = runtime.orchestrator
    if orquestador is None:
        return {
            "connection": "Sin conectar",
            "connected": False,
            "history": "en espera",
            "history_done": False,
            "media_pending": 0,
            "media_done": True,
            "backfill": "en espera",
            "backfill_done": False,
        }
    estado = orquestador.status
    return {
        "connection": estado.connection,
        "connected": estado.connected,
        "history": estado.history,
        "history_done": estado.history_done,
        "media_pending": estado.media_pending,
        "media_done": estado.media_done,
        "backfill": estado.backfill,
        "backfill_done": estado.backfill_done,
        "summary": estado.summary(),
    }


# Que significa cada estado historico, para el frontend.
_HISTORY_LABELS = {
    "pending": "pendiente de excavar",
    "fetching": "excavando",
    "exhausted": "historial completo",
    "server_limited": "el servidor corto el historial",
    "timeout": "sin respuesta del telefono",
    "error": "error al pedir historial",
    "no_valid_cursor": "sin ancla para pedir historial",
    "waiting_seed": "esperando el primer mensaje",
    "empty_confirmed": "conversacion vacia",
}


def historia_to_json(fila: Any, total: int) -> dict[str, Any]:
    """Estado historico de un chat, en terminos honestos.

    ``complete`` solo es ``True`` cuando de verdad no queda nada que pedir.
    Un chat con cero mensajes que espera semilla NO es completo, y decirlo
    era exactamente el fallo: el frontend anunciaba "historial sincronizado"
    sobre una conversacion que en el telefono si tiene mensajes.
    """
    from app.models import COMPLETE_STATUSES, SEEDLESS_STATUSES

    if fila is None:
        return {
            "status": "pending",
            "label": _HISTORY_LABELS["pending"],
            "complete": False,
            "can_dig": False,
            "waiting_seed": False,
            "message_count": total,
        }

    estado = fila[0]
    return {
        "status": estado,
        "label": _HISTORY_LABELS.get(estado, estado),
        "complete": estado in COMPLETE_STATUSES,
        # ``True`` si hay un ancla real desde la que pedir mas.
        "can_dig": bool(fila[1]) and estado not in COMPLETE_STATUSES,
        "waiting_seed": estado in SEEDLESS_STATUSES,
        "oldest_message_at": iso(fila[2]),
        "newest_message_at": iso(fila[3]),
        "message_count": total,
        "last_error": fila[5],
    }
