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
    if nombre == "history.progress":
        return (
            f"chats={len(datos.get('chat_jids') or [])} "
            f"mensajes={datos.get('messages', 0)} "
            f"filas={len(datos.get('chats') or [])}"
        )
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
    if nombre == "history_ingested":
        return _historial_ingerido(carga, runtime)
    if nombre == "chat_renamed":
        return _grupo_renombrado(carga, runtime)
    return []


def _grupo_renombrado(carga: Any, runtime: Any) -> list[tuple[str, Any]]:
    """Un grupo acaba de recibir su nombre: se manda su fila entera.

    La pantalla cambia esa conversacion en el sitio, sin recargar la lista.
    """
    if not isinstance(carga, dict) or not carga.get("jid"):
        return []
    sesion = _session(runtime)
    if sesion is None:
        return []
    try:
        cuenta = getattr(runtime, "runtime_owner_account_id", None)
        filas = _filas_de(sesion, [str(carga["jid"])], cuenta)
        return [("chat.updated", {"chat": filas[0]})] if filas else []
    except Exception:  # noqa: BLE001 - no poder avisar no deshace el nombre
        log.debug("No se pudo construir la fila del grupo renombrado")
        return []
    finally:
        sesion.close()


#: Cuantas conversaciones se mandan enteras en un aviso de historial. Por
#: encima de esto va solo la lista de identificadores y la pantalla se refresca
#: una vez: un `INITIAL_BOOTSTRAP` toca decenas de golpe y mandar todas las
#: filas convertiria un aviso en una carga util enorme.
TOPE_DE_FILAS = 25

#: Cuantas conversaciones NUEVAS se anuncian una a una en un mismo bloque.
#:
#: Mas alto que `TOPE_DE_FILAS` a proposito: un `INITIAL_BOOTSTRAP` trae
#: cuarenta de golpe y son justo las que el usuario quiere ver caer. El coste
#: es una consulta por conversacion contra PostgreSQL local, que es
#: milisegundos; el tope solo esta para que un blob anomalo no genere miles.
TOPE_DE_NUEVOS = 200


def _historial_ingerido(carga: Any, runtime: Any) -> list[tuple[str, Any]]:
    """Entro historial: que conversaciones cambiaron y como quedaron.

    POR QUE VAN LAS FILAS DENTRO
    ----------------------------
    Antes esto decia solo en que conversaciones habia entrado algo, y la
    pantalla tenia que pedir la lista entera para enterarse del contador nuevo.
    Una excavacion produce un aviso por cada bloque de cincuenta mensajes, asi
    que recuperar tres mil eran sesenta peticiones contra la misma lista.

    Con la fila dentro, la pantalla actualiza en el sitio: contador, previa y
    estado. Y el chat abierto puede recargar solo sus mensajes, que es lo
    unico que el aviso no trae.
    """
    if not isinstance(carga, dict):
        # Compatibilidad: antes viajaba una cadena suelta.
        return [("history.progress", {"summary": str(carga) if carga else ""})]

    jids = [j for j in (carga.get("chat_jids") or []) if j]
    nuevos = [j for j in (carga.get("new_chat_jids") or []) if j]
    datos: dict[str, Any] = {
        "summary": carga.get("summary"),
        "chat_jids": jids,
        "messages": int(carga.get("messages") or 0),
        "sync_type": carga.get("sync_type"),
    }

    sesion = _session(runtime)
    if sesion is None:
        return [("history.progress", datos)]

    cuenta = getattr(runtime, "runtime_owner_account_id", None)
    try:
        filas = _filas_de(sesion, jids[:TOPE_DE_FILAS], cuenta) if jids else []
        if filas:
            datos["chats"] = filas

        # UNA CONVERSACION NUEVA, UN AVISO.
        #
        # Antes solo se mandaba `history.progress`, y la pantalla o pedia la
        # lista entera o no se enteraba. El resultado era un panel quieto
        # durante toda la extraccion: para ver lo ya extraido habia que
        # recargar con F5.
        #
        # Con un `chat.created` por conversacion, la pantalla las va soltando
        # una a una segun aparecen, que es justo lo que se ve al extraer.
        avisos: list[tuple[str, Any]] = [("history.progress", datos)]
        por_jid = {f.get("jid"): f for f in filas if isinstance(f, dict)}
        pendientes = [j for j in nuevos if j not in por_jid]
        if pendientes:
            por_jid.update(
                {f.get("jid"): f for f in _filas_de(sesion, pendientes[:TOPE_DE_NUEVOS], cuenta)}
            )
        for jid in nuevos[:TOPE_DE_NUEVOS]:
            fila = por_jid.get(jid)
            if fila is not None:
                avisos.append(("chat.created", {"chat": fila}))
        return avisos
    except Exception:  # noqa: BLE001 - sin filas el aviso sigue sirviendo
        log.debug("No se pudieron enriquecer las conversaciones del historial")
        return [("history.progress", datos)]
    finally:
        sesion.close()


def _filas_de(sesion: Any, jids: list[str], cuenta: Any) -> list[dict]:
    """La fila completa de cada conversacion, lista para la pantalla.

    Acotado por CUENTA: el mismo JID existe en tantas filas como cuentas
    hablen con ese contacto, y sin la cuenta no hay forma de saber cual es.
    """
    from app.api.serializers import chat_to_json
    from app.services import repository as repo
    from app.services.account_scope import chat_id_de

    salida: list[dict] = []
    for jid in jids:
        chat_id = chat_id_de(sesion, jid, account_id=cuenta)
        if chat_id is None:
            continue
        resumen = repo.chat_summary(sesion, chat_id)
        # LA MISMA REGLA QUE EL LISTADO. Sin esto, los avisos en vivo metian
        # por la puerta de atras las conversaciones que `/chats` deja fuera:
        # durante una excavacion aparecian "+0" y chats con un solo aviso de
        # cifrado, que es justo lo que el filtro existe para evitar.
        if resumen is not None and repo.se_lista(resumen):
            salida.append(chat_to_json(resumen))
    return salida


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
        if resumen is not None and repo.se_lista(resumen):
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
