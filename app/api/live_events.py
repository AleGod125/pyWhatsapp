"""Traduccion de eventos internos a eventos SSE del frontend.

QUE HACE
--------
Los servicios publican eventos con el vocabulario del protocolo
(``message_stored``, ``media_ready``) y con lo minimo: identificadores. El
frontend necesita otra cosa: la burbuja lista para pintar y la fila del
sidebar ya actualizada.

Aqui se traduce lo uno en lo otro. Un evento interno puede producir VARIOS
eventos SSE: guardar un mensaje cambia la conversacion y tambien el sidebar.

POR QUE NO SE HACE EN EL SERVICIO
---------------------------------
``app/services`` no conoce adaptadores: no puede construir URLs ``/api/v1/...``
ni saber que forma tiene el JSON. Traducir aqui mantiene esa direccion.

UNA CONSULTA POR EVENTO, NO POR CLIENTE
---------------------------------------
El bus entrega el MISMO objeto a todos los suscriptores, asi que el resultado
se memoriza en el propio evento. Con cinco pestanas abiertas se hace una
consulta, no cinco.
"""

from __future__ import annotations

import threading
from typing import Any

from app.api.serializers import chat_to_json, message_to_json
from app.core.logging_setup import get_logger

log = get_logger("SSE")

# Clave donde se memoriza la traduccion dentro del evento.
_CACHE_KEY = "sse_payloads"

# El memo se escribe bajo cerrojo. Sin el, dos clientes SSE que traducen el
# MISMO evento a la vez pasan los dos la comprobacion antes de que ninguno
# haya guardado el resultado: se hacen dos consultas y se emiten dos eventos
# identicos. Se midio con dos pestanas abiertas: cada 'media.updated' salia
# por duplicado, mismo chat, misma media, mismo estado.
_memo_lock = threading.Lock()


def translate(event: Any, runtime: Any) -> list[tuple[str, Any]]:
    """``[(nombre_sse, datos), ...]`` para un evento interno.

    Lista vacia si ese evento no interesa al frontend.
    """
    extra = getattr(event, "extra", None)
    if isinstance(extra, dict):
        # Comprobacion rapida sin cerrojo: el caso normal es que ya este.
        memorizado = extra.get(_CACHE_KEY)
        if memorizado is not None:
            return memorizado

        with _memo_lock:
            # Segunda comprobacion DENTRO del cerrojo: otro cliente pudo
            # haberlo calculado mientras esperabamos.
            memorizado = extra.get(_CACHE_KEY)
            if memorizado is not None:
                return memorizado
            salida = _translate(event, runtime)
            extra[_CACHE_KEY] = salida
            _registrar(salida)
            return salida

    salida = _translate(event, runtime)
    _registrar(salida)
    return salida


def _registrar(salida: list[tuple[str, Any]]) -> None:
    """Traza UNA vez por evento, no una por cliente conectado."""
    for nombre, datos in salida:
        log.info("%s %s", nombre, _resumen(nombre, datos))


def _resumen(nombre: str, datos: Any) -> str:
    """Una linea corta para el log. NUNCA el texto del mensaje.

    El contenido de una conversacion no va a los logs: se dice que chat y que
    tipo, que es lo que hace falta para seguir el pipeline.
    """
    if not isinstance(datos, dict):
        return ""
    if nombre == "message.created":
        mensaje = datos.get("message") or {}
        return (
            f"chat={datos.get('chat_id')} id={datos.get('message_id') or mensaje.get('id')} "
            f"tipo={mensaje.get('type', '?')}"
        )
    if nombre == "chat.updated":
        return f"chat={datos.get('chat_id')} mensajes={datos.get('message_count')}"
    if nombre == "media.updated":
        return (
            f"chat={datos.get('chat_id')} media={datos.get('media_id')} "
            f"estado={datos.get('status')}"
        )
    return ""


def _translate(event: Any, runtime: Any) -> list[tuple[str, Any]]:
    nombre = getattr(event, "name", "")
    carga = getattr(event, "payload", None)

    if nombre == "message_stored":
        return _mensaje_guardado(carga, runtime)
    if nombre == "media_ready":
        return _adjunto_listo(carga, runtime)
    return []


def _mensaje_guardado(carga: Any, runtime: Any) -> list[tuple[str, Any]]:
    """Un mensaje nuevo: la burbuja y la fila del sidebar.

    Un DUPLICADO no produce ningun evento. History Sync y el receptor en vivo
    se solapan a proposito, y la base ya lo deduplica por wamid; dejar pasar
    el aviso haria aparecer la burbuja dos veces en una pantalla que no
    consulta la base.
    """
    if not isinstance(carga, dict):
        return []
    if not carga.get("new"):
        return []

    chat_id = carga.get("chat_id")
    message_id = carga.get("message_id")
    if chat_id is None:
        return []

    salida: list[tuple[str, Any]] = []
    sesion = _session(runtime)
    if sesion is None:
        # Sin base no se puede enriquecer, pero el aviso sigue siendo util:
        # el frontend puede recargar ese chat por su cuenta.
        return [("message.created", {"chat_id": chat_id, "message_id": message_id})]

    try:
        from app.services import repository as repo

        if message_id is not None:
            fila = _fetch_message(sesion, message_id)
            if fila is not None:
                adjuntos = repo.media_for_messages(sesion, [message_id])
                salida.append(
                    (
                        "message.created",
                        {
                            "chat_id": chat_id,
                            "message": message_to_json(fila, adjuntos.get(message_id)),
                        },
                    )
                )
        if not salida:
            # No se pudo servir la burbuja completa; se manda el aviso escueto
            # y el frontend decide. Es el contrato minimo, no un fallo.
            salida.append(
                ("message.created", {"chat_id": chat_id, "message_id": message_id})
            )

        resumen = repo.chat_summary(sesion, chat_id)
        if resumen is not None:
            fila_sidebar = chat_to_json(resumen)
            salida.append(
                (
                    "chat.updated",
                    {
                        "chat_id": chat_id,
                        "preview": fila_sidebar["preview"],
                        "last_message_at": fila_sidebar["last_message_at"],
                        "message_count": fila_sidebar["message_count"],
                        "chat": fila_sidebar,
                    },
                )
            )
    except Exception:  # noqa: BLE001 - un aviso roto no puede tumbar el stream
        log.exception("No se pudo preparar el evento de mensaje nuevo")
    finally:
        sesion.close()
    return salida


def _adjunto_listo(carga: Any, runtime: Any) -> list[tuple[str, Any]]:
    """Un adjunto termino de descargarse: la burbuja ya puede pintarlo."""
    if not isinstance(carga, dict):
        return []
    media_id = carga.get("media_id")
    if media_id is None:
        return []

    datos: dict[str, Any] = {
        "media_id": media_id,
        "message_id": carga.get("message_id"),
        "chat_id": carga.get("chat_id"),
        "status": carga.get("status", "downloaded"),
    }

    sesion = _session(runtime)
    if sesion is not None:
        try:
            from app.api.serializers import media_to_json

            fila = _fetch_media(sesion, media_id)
            if fila is not None:
                completo = media_to_json(fila)
                datos["status"] = completo["status"]
                datos["file_url"] = completo["file_url"]
                datos["thumbnail_url"] = completo["thumbnail_url"]
                datos["media"] = completo
        except Exception:  # noqa: BLE001
            log.debug("No se pudo enriquecer el adjunto %s", media_id)
        finally:
            sesion.close()

    datos.setdefault("file_url", None)
    datos.setdefault("thumbnail_url", None)
    return [("media.updated", datos)]


# ---------------------------------------------------------------------------
# Acceso a datos
# ---------------------------------------------------------------------------


def _session(runtime: Any):
    base = getattr(runtime, "database", None)
    if base is None:
        return None
    try:
        return base.session()
    except Exception:  # noqa: BLE001
        return None


def _fetch_message(session: Any, message_id: int):
    """La burbuja, con las MISMAS columnas que usa la paginacion.

    Sin ``raw_proto``: es la columna mas pesada y no hace falta para pintar.
    """
    from sqlalchemy import select

    from app.models import Message
    from app.services.repository import _PAGE_COLUMNS

    return session.execute(
        select(*_PAGE_COLUMNS).where(Message.id == message_id)
    ).first()


def _fetch_media(session: Any, media_id: int):
    from sqlalchemy import select

    from app.models import MediaFile

    return session.execute(
        select(
            MediaFile.id,
            MediaFile.media_type,
            MediaFile.mime_type,
            MediaFile.file_name,
            MediaFile.file_size,
            MediaFile.duration_seconds,
            MediaFile.width,
            MediaFile.height,
            MediaFile.download_status,
        ).where(MediaFile.id == media_id)
    ).first()
