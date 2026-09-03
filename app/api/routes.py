"""Endpoints de ``/api/v1``.

NADA DE TKINTER
---------------
Este modulo, y todo ``app/api``, no puede importar ``tkinter``, ni widgets, ni
``root.after``, ni tocar frames. Toda la comunicacion pasa por ``AppRuntime``,
los servicios y el bus de eventos. Hay una prueba que lo verifica recorriendo
el paquete.

NADA DE RUTAS LOCALES
---------------------
Las respuestas nunca llevan una ruta del sistema de archivos. Los adjuntos se
sirven por URL. Ver :mod:`app.api.serializers`.

PAGINACION POR KEYSET
---------------------
``/chats/<id>/messages`` acepta ``before_timestamp`` y ``before_id``, nunca un
OFFSET: con cientos de miles de filas el OFFSET degrada, y ademas la clave
compuesta desempata los mensajes que comparten timestamp, cosa habitual en
History Sync.
"""

from __future__ import annotations

import itertools
import json
import time
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request, send_file

from app.api.serializers import (
    chat_to_json,
    iso,
    message_to_json,
    historia_to_json,
    media_to_json,
    qr_to_json,
    state_to_json,
    sync_to_json,
)
from app.core.logging_setup import get_logger
from app.services import repository as repo

log = get_logger("API")

api = Blueprint("api_v1", __name__, url_prefix="/api/v1")

# Tope duro por peticion. Un cliente no puede pedirse la conversacion entera
# de golpe: eso es justo lo que la paginacion existe para evitar.
MAX_LIMIT = 500
DEFAULT_LIMIT = 200

# Cada cuanto se manda un latido por el SSE cuando no pasa nada. Sin el, un
# proxy o el propio navegador dan la conexion por muerta.
SSE_HEARTBEAT = 15.0

# Comentario SSE. No es relleno: mantiene viva la conexion y hace que una
# desconexion se detecte al escribir, en vez de dejar el generador colgado
# para siempre sobre un cliente que ya no esta.
SSE_COMMENT = ": latido" + "\n\n"


def runtime() -> Any:
    return current_app.config["RUNTIME"]


def _session():
    """Sesion de PostgreSQL de una sola peticion."""
    rt = runtime()
    if rt.database is None:
        return None
    return rt.database.session()


def _entero(nombre: str, defecto: int | None = None) -> int | None:
    crudo = request.args.get(nombre)
    if crudo is None or crudo.strip() == "":
        return defecto
    try:
        return int(crudo)
    except ValueError:
        return defecto


def _error(mensaje: str, codigo: int = 400, **extra: Any):
    cuerpo = {"error": mensaje}
    cuerpo.update(extra)
    return jsonify(cuerpo), codigo


def _error_code(codigo_error: str, mensaje: str, http: int, **extra: Any):
    """Error con codigo estable, para que el frontend pueda ramificar.

    El texto es para leer; el ``code`` es para programar contra el. Ramificar
    por el mensaje ataria el frontend a la redaccion exacta.
    """
    cuerpo: dict[str, Any] = {"error": {"code": codigo_error, "message": mensaje}}
    cuerpo.update(extra)
    return jsonify(cuerpo), http


# ---------------------------------------------------------------------------
# Salud y sesion
# ---------------------------------------------------------------------------


@api.get("/health")
def health():
    """Comprobacion ligera. NO recorre la tabla de mensajes."""
    rt = runtime()
    info = rt.info()
    salud: dict[str, Any] = {
        "status": "ok" if info.database else "degraded",
        "owner": info.owner,
        "state": info.state,
        "database": info.database,
        "whatsapp_enabled": info.whatsapp_enabled,
        "session_file_present": info.session_file,
        "api_version": "v1",
    }
    # Quien manda sobre la sesion. En /health tambien, porque es lo primero
    # que se mira cuando algo no cuadra.
    from app.api.serializers import owner_to_json

    salud.update(owner_to_json(rt))
    if rt.database is not None:
        try:
            detalle = rt.database.health()
            salud["postgres_version"] = detalle.get("server_version")
            salud["postgres_database"] = detalle.get("database")
        except Exception as exc:  # noqa: BLE001 - health nunca debe reventar
            salud["status"] = "degraded"
            salud["database_error"] = str(exc)[:200]
    return jsonify(salud)


@api.get("/session")
def session_state():
    """Estado de la sesion.

    NO se deduce de que exista ``device.json``: ese archivo puede estar y la
    sesion estar revocada. El estado real lo lleva la maquina de estados, que
    solo pasa a CONNECTED con el ``<success>`` del servidor.
    """
    return jsonify(state_to_json(runtime()))


@api.post("/session/pair")
def session_pair():
    """Reintento manual de la vinculacion. NO hace falta en el camino normal.

    El QR se genera solo cuando el backend arranca sin sesion valida. Este
    endpoint existe para reintentar si algo fallo, no como paso obligatorio.

    Es idempotente: si ya hay una vinculacion en marcha, lo dice y NO lanza
    otra. Dos vinculaciones simultaneas abririan dos conexiones y produirian
    dos QR, de los cuales solo uno serviria.
    """
    rt = runtime()
    if not rt.info().whatsapp_enabled:
        return _error_code(
            "WHATSAPP_DISABLED",
            "El backend esta en modo local y no puede vincular WhatsApp.",
            409,
        )

    estado = rt.state.state.value
    if estado == "CONNECTED":
        return _error_code(
            "SESSION_ALREADY_CONNECTED",
            "La sesion ya esta conectada; no hay nada que vincular.",
            409,
        )

    if rt.pairing.available:
        # Ya hay un QR vigente: se devuelve ese, no se genera otro.
        return (
            jsonify(
                {
                    "status": "pairing_in_progress",
                    "restarted": False,
                    "qr": qr_to_json(rt),
                    "session": state_to_json(rt),
                }
            ),
            200,
        )

    if rt.pairing.renewing:
        return (
            jsonify(
                {
                    "status": "pairing_starting",
                    "restarted": False,
                    "session": state_to_json(rt),
                }
            ),
            202,
        )

    # Sin QR vigente (caducado, o el flujo murio): se reinicia.
    reiniciado = rt.pairing.renew()
    rt.pairing.start_watchdog()
    return (
        jsonify(
            {
                "status": "pairing_restarted" if reiniciado else "pairing_in_progress",
                "restarted": bool(reiniciado),
                "session": state_to_json(rt),
            }
        ),
        202,
    )


@api.get("/session/qr")
def session_qr():
    """Metadatos del QR vigente. NO devuelve el payload.

    El payload es una credencial de vinculacion: quien lo tenga puede enlazar
    un dispositivo a la cuenta. Se sirve solo como imagen, no se guarda en
    disco y no se registra en los logs.
    """
    return jsonify(qr_to_json(runtime()))


@api.get("/session/qr/image")
def session_qr_image():
    """PNG del QR vigente. Nunca uno caducado."""
    import io

    from app.core.qr_render import render_qr

    rt = runtime()
    payload = rt.pairing.payload()
    if payload is None:
        if rt.pairing.expired:
            # Distinto de "no hay ninguno": hubo uno y dejo de valer. El 410
            # le dice al frontend que espere el siguiente ``session.qr`` en
            # lugar de reintentar en bucle.
            return _error_code(
                "QR_EXPIRED", "El codigo QR expiro.", 410, qr=qr_to_json(rt)
            )
        return _error_code(
            "QR_NOT_AVAILABLE", "No hay ningun codigo QR vigente.", 404,
            session=state_to_json(rt),
        )

    tamano = _entero("size", 456) or 456
    imagen = render_qr(payload, max_pixels=max(120, min(1024, tamano)))
    buffer = io.BytesIO()
    imagen.save(buffer, format="PNG")
    buffer.seek(0)
    respuesta = send_file(buffer, mimetype="image/png")
    # El QR rota cada pocos segundos: cachearlo mostraria uno ya muerto.
    respuesta.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    respuesta.headers["Pragma"] = "no-cache"
    respuesta.headers["Expires"] = "0"
    respuesta.headers["X-QR-Generation"] = str(rt.pairing.generation)
    return respuesta


# ---------------------------------------------------------------------------
# Chats y mensajes
# ---------------------------------------------------------------------------


@api.get("/chats")
def chats():
    """Sidebar. Ordenado por ``last_message_timestamp`` descendente."""
    sesion = _session()
    if sesion is None:
        return _error("la base de datos no esta disponible", 503)
    try:
        busqueda = (request.args.get("search") or "").strip() or None
        limite = min(_entero("limit", 500) or 500, 1000)
        resumenes = repo.list_chat_summaries(sesion, search=busqueda, limit=limite)
    finally:
        sesion.close()
    return jsonify({"chats": [chat_to_json(c) for c in resumenes], "count": len(resumenes)})


@api.get("/chats/<int:chat_id>")
def chat_detail(chat_id: int):
    sesion = _session()
    if sesion is None:
        return _error("la base de datos no esta disponible", 503)
    try:
        resumen = repo.chat_summary(sesion, chat_id)
        if resumen is None:
            return _error("chat no encontrado", 404)
        stats = repo.get_chat_stats(sesion, chat_id)
        estado_historico = repo.history_state_for(sesion, resumen.jid)
    finally:
        sesion.close()

    cuerpo = chat_to_json(resumen)
    # El estado historico va SIEMPRE: sin el, el frontend no puede distinguir
    # "no queda nada" de "no se ha podido pedir nada", y acaba diciendo
    # "historial sincronizado" sobre un chat con cero mensajes.
    cuerpo["history"] = historia_to_json(estado_historico, stats.total)
    cuerpo["stats"] = {
        "total": stats.total,
        "oldest_timestamp": stats.oldest_timestamp,
        "newest_timestamp": stats.newest_timestamp,
        "oldest_at": iso(stats.oldest_timestamp),
        "newest_at": iso(stats.newest_timestamp),
    }
    return jsonify(cuerpo)


@api.get("/chats/<int:chat_id>/messages")
def chat_messages(chat_id: int):
    """Ultimos N mensajes, los anteriores a un cursor, o los posteriores.

    Tres modos, siempre en orden cronologico ascendente:

    * sin cursor            -> los mas recientes;
    * ``before_timestamp``  -> la pagina anterior (scroll hacia arriba);
    * ``after_timestamp``   -> lo que ha entrado despues (reconciliacion).

    El tercero existe porque el tiempo real NO puede ser la unica fuente. Si
    el frontend pierde la conexion SSE, al volver pregunta "que me he perdido
    desde este mensaje" y se pone al dia sin recargar la conversacion entera.
    PostgreSQL es la fuente de verdad; SSE es solo el transporte.
    """
    limite = min(_entero("limit", DEFAULT_LIMIT) or DEFAULT_LIMIT, MAX_LIMIT)
    antes_ts = _entero("before_timestamp")
    antes_id = _entero("before_id")
    despues_ts = _entero("after_timestamp")
    despues_id = _entero("after_id")

    if (antes_ts is None) != (antes_id is None):
        return _error(
            "before_timestamp y before_id van juntos: la paginacion es por "
            "clave compuesta (timestamp, id), no por timestamp suelto",
            400,
        )
    if (despues_ts is None) != (despues_id is None):
        return _error(
            "after_timestamp y after_id van juntos: la paginacion es por "
            "clave compuesta (timestamp, id), no por timestamp suelto",
            400,
        )
    if antes_ts is not None and despues_ts is not None:
        return _error(
            "before_* y after_* son direcciones opuestas: usa una u otra",
            400,
        )

    sesion = _session()
    if sesion is None:
        return _error("la base de datos no esta disponible", 503)
    try:
        if repo.chat_summary(sesion, chat_id) is None:
            return _error("chat no encontrado", 404)
        if despues_ts is not None:
            filas = repo.get_messages_after(
                sesion, chat_id, despues_ts, despues_id, limite
            )
        elif antes_ts is not None:
            filas = repo.get_messages_before(
                sesion, chat_id, antes_ts, antes_id, limite
            )
        else:
            filas = repo.get_recent_messages(sesion, chat_id, limit=limite)
        adjuntos = repo.media_for_messages(sesion, [f.id for f in filas])
        total = repo.get_chat_message_count(sesion, chat_id)
    finally:
        sesion.close()

    mensajes = [message_to_json(f, adjuntos.get(f.id)) for f in filas]
    # El cursor para la siguiente pagina sale del mensaje mas antiguo de esta.
    siguiente = None
    if filas:
        siguiente = {
            "before_timestamp": filas[0].timestamp,
            "before_id": filas[0].id,
        }
    # Y el de reconciliacion sale del mas RECIENTE: es el "ya tengo hasta
    # aqui" que el frontend guarda para preguntar que se perdio tras una
    # reconexion. El flujo de eventos puede perder mensajes; PostgreSQL no.
    reconciliacion = None
    if filas:
        reconciliacion = {
            "after_timestamp": filas[-1].timestamp,
            "after_id": filas[-1].id,
        }
    return jsonify(
        {
            "chat_id": chat_id,
            "messages": mensajes,
            "count": len(mensajes),
            "stored_total": total,
            "next_cursor": siguiente,
            # "ya tengo hasta aqui": lo que el frontend guarda para
            # preguntar que se perdio tras una reconexion.
            "sync_cursor": reconciliacion,
            # ``False`` cuando la pagina vino vacia: no queda nada anterior
            # ALMACENADO. No dice nada sobre lo que WhatsApp pueda tener.
            "has_more": bool(filas) and len(filas) == limite,
        }
    )


@api.post("/chats/<int:chat_id>/history/recheck")
def chat_history_recheck(chat_id: int):
    """Vuelve a mirar si un chat sin ancla ya puede excavarse.

    Es la accion del boton "reintentar historial". NO pide nada al servidor:
    resuelve los alias del contacto, busca un mensaje con ID real de WhatsApp
    y, si no lo encuentra, reinterpreta los blobs de History Sync que ya estan
    en disco. Si aparece un ancla el chat vuelve a la cola de excavacion; si
    no, se queda en ``waiting_seed`` y se dice tal cual.

    Nunca se fabrica un cursor. Un ``ON_DEMAND`` anclado en un id inventado
    recibe un ACK y despues nada, que es exactamente el fallo que mas costo
    diagnosticar.
    """
    rt = runtime()
    if rt.database is None:
        return _error("la base de datos no esta disponible", 503)

    from app.services.history_recheck import HistoryRecheck

    resultado = HistoryRecheck(rt.database, rt.settings).recheck(chat_id)
    if resultado is None:
        return _error("chat no encontrado", 404)

    cuerpo = resultado.to_json()
    # El estado historico completo, con la misma forma que en /chats/<id>,
    # para que el frontend pueda refrescar la ficha sin una segunda peticion.
    sesion = _session()
    if sesion is not None:
        try:
            stats = repo.get_chat_stats(sesion, chat_id)
            cuerpo["history"] = historia_to_json(
                repo.history_state_for(sesion, resultado.chat_jid), stats.total
            )
        finally:
            sesion.close()
    return jsonify(cuerpo)


# ---------------------------------------------------------------------------
# Multimedia
# ---------------------------------------------------------------------------


def _media_row(media_id: int):
    from sqlalchemy import select

    from app.models import MediaFile

    sesion = _session()
    if sesion is None:
        return None, None
    try:
        fila = sesion.execute(
            select(MediaFile).where(MediaFile.id == media_id)
        ).scalar_one_or_none()
        if fila is None:
            return None, None
        # Se copia lo necesario antes de cerrar la sesion.
        datos = {
            "id": fila.id,
            "media_type": fila.media_type,
            "mime_type": fila.mime_type,
            "file_name": fila.file_name,
            "file_size": fila.file_size,
            "duration_seconds": fila.duration_seconds,
            "width": fila.width,
            "height": fila.height,
            "download_status": fila.download_status,
            "local_path": fila.local_path,
            "message_id": fila.message_id,
            "chat_id": fila.chat_id,
        }
    finally:
        sesion.close()
    return datos, datos.get("local_path")


class _MediaView:
    """Vista de solo lectura, para reutilizar el serializador."""

    def __init__(self, datos: dict[str, Any]) -> None:
        for clave, valor in datos.items():
            setattr(self, clave, valor)


@api.get("/media/<int:media_id>")
def media_detail(media_id: int):
    datos, _ = _media_row(media_id)
    if datos is None:
        return _error("adjunto no encontrado", 404)
    return jsonify(media_to_json(_MediaView(datos)))


@api.post("/media/<int:media_id>/retry")
def media_retry(media_id: int):
    """Reintenta la descarga de UN adjunto. Nada mas.

    Una imagen rota no puede obligar a reextraer una conversacion entera. Este
    endpoint no lanza backfill, ni ``ON_DEMAND``, ni una sincronizacion: mira
    la fila, comprueba que haya con que descargar, y la pone en cola.

    Respuestas:

    * 200 ``already_available``  el archivo ya esta;
    * 202 ``already_pending``    ya estaba en cola o descargandose;
    * 202 ``queued``             se ha vuelto a encolar;
    * 409 ``MEDIA_METADATA_INSUFFICIENT``  sin clave o sin ruta no hay nada
      que intentar, y fingir que se reintenta seria mentir;
    * 404 si el adjunto no existe.

    Un estado terminal (``unavailable``/``expired``) SI se puede reintentar
    desde aqui: es terminal para los reintentos AUTOMATICOS, que si no
    volverian a fallar en cada arranque llenando el log. Cuando lo pide una
    persona, se intenta.
    """
    from sqlalchemy import select, update

    from app.models import MediaFile

    rt = runtime()
    sesion = _session()
    if sesion is None:
        return _error("la base de datos no esta disponible", 503)

    try:
        fila = sesion.execute(
            select(MediaFile).where(MediaFile.id == media_id)
        ).scalar_one_or_none()
        if fila is None:
            return _error("adjunto no encontrado", 404)

        estado = fila.download_status
        message_id = fila.message_id
        tiene_datos = bool(fila.direct_path) and bool(fila.media_key)

        if estado == "downloaded":
            # Se comprueba que el archivo siga estando: una fila puede decir
            # "descargado" y el archivo haber desaparecido del disco.
            local = _archivo_local(
                {"id": fila.id, "local_path": fila.local_path}
            )
            if local is not None:
                return (
                    jsonify(
                        {
                            "status": "already_available",
                            "media_id": media_id,
                            "message_id": message_id,
                            "download_status": estado,
                        }
                    ),
                    200,
                )
            # El archivo no esta: se vuelve a pedir, si hay con que.
            log.warning(
                "El adjunto %s figuraba descargado pero el archivo no esta; "
                "se reintenta",
                media_id,
            )

        elif estado in ("pending", "downloading"):
            return (
                jsonify(
                    {
                        "status": "already_pending",
                        "media_id": media_id,
                        "message_id": message_id,
                        "download_status": estado,
                    }
                ),
                202,
            )

        if not tiene_datos:
            return _error_code(
                "MEDIA_METADATA_INSUFFICIENT",
                "Este adjunto no trae la clave o la ruta que hacen falta para "
                "descargarlo. No hay nada que reintentar.",
                409,
                media_id=media_id,
                message_id=message_id,
                download_status=estado,
            )

        # A la cola, y con los intentos a cero: el tope de reintentos existe
        # para los automaticos, no para lo que pide una persona.
        sesion.execute(
            update(MediaFile)
            .where(MediaFile.id == media_id)
            .values(download_status="pending", download_attempts=0, last_error=None)
        )
        sesion.commit()
    finally:
        sesion.close()

    # Se avisa al worker para que lo coja en su proxima ronda. NO se reinicia
    # el worker ni se lanza ninguna excavacion.
    try:
        rt.bus.publish("media_retry_requested", {"media_id": media_id})
    except Exception:  # noqa: BLE001 - el aviso es opcional
        log.debug("No se pudo publicar el aviso de reintento")

    return (
        jsonify(
            {
                "status": "queued",
                "media_id": media_id,
                "message_id": message_id,
                "download_status": "pending",
            }
        ),
        202,
    )


@api.post("/messages/<int:message_id>/media/recover")
def message_media_recover(message_id: int):
    """Igual que el anterior, pero por mensaje: el frontend tiene el mensaje.

    Resuelve el ``media_id`` y delega. No duplica ni una regla.
    """
    from sqlalchemy import select

    from app.models import MediaFile

    sesion = _session()
    if sesion is None:
        return _error("la base de datos no esta disponible", 503)
    try:
        media_id = sesion.execute(
            select(MediaFile.id).where(MediaFile.message_id == message_id)
        ).scalars().first()
    finally:
        sesion.close()

    if media_id is None:
        return _error("ese mensaje no tiene ningun adjunto", 404)
    return media_retry(media_id)


def _archivo_local(datos: dict[str, Any]) -> Path | None:
    """Ruta en disco del adjunto. NUNCA sale de este modulo."""
    if not datos.get("local_path"):
        return None
    raiz = Path(runtime().settings.media_dir).resolve()
    candidato = (raiz / datos["local_path"]).resolve()
    # El ``local_path`` sale de la base, pero se comprueba igualmente que cae
    # dentro de la carpeta de multimedia: una fila manipulada no puede acabar
    # sirviendo un archivo cualquiera del disco.
    try:
        candidato.relative_to(raiz)
    except ValueError:
        log.warning(
            "Ruta de adjunto fuera de MEDIA_DIR; se rechaza (id=%s)",
            datos.get("id"),
        )
        return None
    return candidato if candidato.exists() else None


@api.get("/media/<int:media_id>/file")
def media_file(media_id: int):
    datos, _ = _media_row(media_id)
    if datos is None:
        return _error("adjunto no encontrado", 404)
    if datos["download_status"] in ("unavailable", "expired"):
        # El mensaje existe; el archivo ya no. Son dos cosas distintas y el
        # frontend tiene que poder distinguirlas para pintar el aviso.
        return _error(
            "el archivo ya no esta disponible en WhatsApp",
            410,
            status=datos["download_status"],
            media=media_to_json(_MediaView(datos)),
        )
    ruta = _archivo_local(datos)
    if ruta is None:
        return _error(
            "el adjunto todavia no se ha descargado",
            404,
            status=datos["download_status"],
        )
    return send_file(
        ruta,
        mimetype=datos.get("mime_type") or None,
        download_name=datos.get("file_name") or ruta.name,
    )


@api.get("/media/<int:media_id>/thumbnail")
def media_thumbnail(media_id: int):
    """Miniatura cacheada. Se genera una vez y se reutiliza."""
    from app.services.thumbnails import ensure_thumbnail

    datos, _ = _media_row(media_id)
    if datos is None:
        return _error("adjunto no encontrado", 404)
    if datos["media_type"] not in ("image", "sticker", "gif"):
        return _error("este tipo de adjunto no tiene miniatura", 404)
    ruta = _archivo_local(datos)
    if ruta is None:
        return _error("el adjunto todavia no se ha descargado", 404)

    lado = min(max(_entero("size", 320) or 320, 48), 640)
    miniatura = ensure_thumbnail(
        Path(runtime().settings.media_dir), ruta, (lado, lado)
    )
    if miniatura is None:
        return _error("no se pudo generar la miniatura", 500)
    respuesta = send_file(miniatura, mimetype="image/jpeg")
    # La miniatura es inmutable: su nombre incluye el hash del original.
    respuesta.headers["Cache-Control"] = "public, max-age=86400"
    return respuesta


# ---------------------------------------------------------------------------
# Sincronizacion y eventos
# ---------------------------------------------------------------------------


@api.get("/sync/status")
def sync_status():
    """Progreso del trabajo de fondo Y del ciclo manual. Nunca bloquea."""
    rt = runtime()
    cuerpo = sync_to_json(rt)
    cuerpo["session"] = state_to_json(rt)

    trabajo = getattr(rt, "sync_job", None)
    if trabajo is not None:
        # El ciclo manual manda sobre las claves que comparten: es lo que el
        # usuario acaba de pedir y de lo que espera ver progreso.
        cuerpo.update(trabajo.snapshot())
    else:
        cuerpo.setdefault("state", "idle")
        cuerpo.setdefault("job_id", None)

    cuerpo["connected"] = rt.state.state.value == "CONNECTED"
    cuerpo["sync_state"] = getattr(rt, "sync_state", "IDLE")
    # Contadores por ETAPA: dicen exactamente donde muere un mensaje.
    cuerpo["diagnostics"] = dict(getattr(rt, "counters", {}))
    cuerpo["diagnostics"]["decrypt_errors"] = int(getattr(rt, "decrypt_errors", 0) or 0)

    # Contadores del backfill, separados de los del receptor. Mezclarlos fue
    # justo el fallo: un TIMEOUT se anunciaba con "nuevos=6" porque durante la
    # espera habian entrado seis mensajes EN VIVO, que no los trajo esta via.
    # La clave va SIEMPRE, aunque todavia no haya backfill: una forma estable
    # ahorra al frontend tener que distinguir "cero" de "no existe".
    backfill = getattr(rt, "backfill", None)
    stats = getattr(backfill, "stats", None)
    cuerpo["backfill_metrics"] = {
        "busy": bool(getattr(backfill, "busy", False)),
        "in_flight": sorted(getattr(backfill, "in_flight", ()) or ()),
        "requests": int(getattr(stats, "requests_sent", 0) or 0),
        "responses": int(getattr(stats, "responses_received", 0) or 0),
        "timeouts": int(getattr(stats, "timeouts", 0) or 0),
        "no_cursor": int(getattr(stats, "no_cursor", 0) or 0),
        "errors": int(getattr(stats, "errors", 0) or 0),
            # Solo lo que llego por History Sync. Los mensajes en vivo NO
            # cuentan aqui, por muy dentro de la ventana que hayan entrado.
        "inserted_from_history": int(getattr(stats, "messages_new", 0) or 0),
    }
    # Y lo acumulado, aparte. Mezclarlos hacia que una sincronizacion pareciera
    # haber recorrido el doble de chats de los que existen.
    acumulado = getattr(backfill, "lifetime", None)
    cuerpo["backfill_lifetime"] = {
        "chats_processed": int(getattr(acumulado, "chats_processed", 0) or 0),
        "requests": int(getattr(acumulado, "requests_sent", 0) or 0),
        "responses": int(getattr(acumulado, "responses_received", 0) or 0),
        "inserted_from_history": int(getattr(acumulado, "messages_new", 0) or 0),
    }
    if rt.database is not None:
        try:
            sesion2 = rt.database.session()
            try:
                cuerpo["chats"] = repo.history_counters(sesion2)
            finally:
                sesion2.close()
        except Exception:  # noqa: BLE001 - las metricas no pueden reventar
            log.debug("No se pudieron leer las metricas de chats")
    if rt.database is not None:
        try:
            sesion = rt.database.session()
            try:
                media = repo.media_stats(sesion)
            finally:
                sesion.close()
            cuerpo["media"] = media
            cuerpo["media_pending"] = int(media.get("pending", 0))
        except Exception:  # noqa: BLE001 - el estado no puede reventar
            log.debug("No se pudieron leer las cifras de multimedia")
    return jsonify(cuerpo)


@api.post("/sync/run")
def sync_run():
    """Lanza un ciclo de sincronizacion manual.

    Responde 202 y vuelve: el ciclo puede durar minutos y bloquear la peticion
    dejaria al navegador esperando sin poder ni leer el progreso.

    NO ejecuta ningun script: llama a los mismos servicios internos que usa el
    arranque. Lanzar ``probe_chat.py`` como subproceso abriria un segundo
    proceso que pelearia por el cerrojo de la sesion con este.

    Esto NO es lo que trae los mensajes nuevos: eso ocurre solo mientras la
    sesion este conectada. Este ciclo es para el historial, la reconciliacion
    y la multimedia pendiente.
    """
    from app.services.sync_job import SyncAlreadyRunningError, SyncUnavailableError

    rt = runtime()
    trabajo = getattr(rt, "sync_job", None)
    if trabajo is None:
        return _error_code(
            "WHATSAPP_DISABLED",
            "El backend esta en modo local y no puede sincronizar WhatsApp.",
            409,
        )

    try:
        job_id = trabajo.start(rt)
    except SyncAlreadyRunningError as exc:
        return _error_code(
            "SYNC_ALREADY_RUNNING", str(exc), 409, sync=trabajo.snapshot()
        )
    except SyncUnavailableError as exc:
        # 409 y no 503: no es que el servicio este caido, es que el estado
        # actual no permite la operacion. El frontend puede reaccionar.
        return _error_code(exc.code, str(exc), 409, session=state_to_json(rt))

    return (
        jsonify({"started": True, "job_id": job_id, "state": "running"}),
        202,
    )


# Traduccion de los eventos internos al vocabulario del frontend. Los nombres
# internos son del protocolo; los de fuera describen QUE ha cambiado.
#
# ``message_stored`` y ``media_ready`` NO estan aqui: los traduce
# :mod:`app.api.live_events`, porque producen varios eventos y llevan datos
# que hay que ir a buscar a la base.
EVENT_NAMES: dict[str, str] = {
    # OJO: no se traduce el ``qr`` del cliente, sino el ``pairing_qr_ready``
    # que publica el PairingManager DESPUES de anotarlo. Los dos eventos
    # llegan a suscriptores distintos y compiten: traduciendo el primero, el
    # generador SSE podia serializar la generacion ANTERIOR. Se midio: llegaba
    # "generation 2" cuando el gestor ya iba por la 3.
    "pairing_qr_ready": "session.qr",
    "paired": "session.state",
    "connected": "session.state",
    # El socket puede morirse y volver. El frontend tiene que enterarse de
    # las dos cosas: mientras se reconecta NO entra ni un mensaje, y seguir
    # pintando "Conectado" seria mentir.
    "reconnecting": "session.state",
    "reconnected": "session.state",
    # Recuperacion de historiales pendientes. Se pasan tal cual: el nombre ya
    # esta en el vocabulario del frontend.
    "history.recovery.started": "history.recovery.started",
    "history.recovery.progress": "history.recovery.progress",
    "history.recovery.completed": "history.recovery.completed",
    "history.seed.found": "history.seed.found",
    "history.seed.not_found": "history.seed.not_found",
    "history.backfill.started": "history.backfill.started",
    "session_valid": "session.state",
    "logged_out": "session.state",
    "disconnected": "session.state",
    "client_error": "session.state",
    "client_stopped": "session.state",
    "session_state_changed": "session.state",
    "history_ingested": "history.progress",
    "waiting_initial_history": "history.progress",
    "initial_history_ready": "history.progress",
    "media_downloaded": "media.updated",
    "backfill_done": "backfill.progress",
    "backfill_progress": "backfill.progress",
    "sync_progress": "sync.status",
    "sync_state_changed": "sync.status",
    "chats_seeded": "chat.updated",
    "contacts_synced": "chat.updated",
    "maintenance_done": "chat.updated",
    "status": "sync.status",
}

# Eventos que el frontend solo entiende con el estado completo delante.
_ESTADO_COMPLETO = {"session.state", "sync.status"}


# Secuencia monotona de eventos SSE. Permite al frontend detectar un hueco
# tras una reconexion y saber que tiene que revalidar contra PostgreSQL, que
# sigue siendo la fuente de verdad: el bus vive en memoria y no guarda nada.
_sse_seq = itertools.count(1)


def _sse(nombre: str, datos: Any) -> str:
    cuerpo = json.dumps(datos, ensure_ascii=False, default=str)
    return f"id: {next(_sse_seq)}\nevent: {nombre}\ndata: {cuerpo}\n\n"


def _ahora_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).astimezone().isoformat()


def eventos_para(evento: Any, rt: Any) -> list[tuple[str, Any]]:
    """Todos los eventos SSE que produce UN evento interno.

    Un mensaje nuevo produce dos (``message.created`` y ``chat.updated``):
    cambia la conversacion y tambien la fila del sidebar, y el frontend no
    deberia tener que recargar los 40 chats para enterarse de una previa.
    """
    from app.api.live_events import translate

    enriquecidos = translate(evento, rt)
    if enriquecidos:
        return enriquecidos

    interno = getattr(evento, "name", "")
    nombre = EVENT_NAMES.get(interno)
    if nombre is None:
        return []

    if nombre == "session.qr":
        return [(nombre, qr_to_json(rt))]
    if nombre == "session.state":
        return [(nombre, state_to_json(rt))]
    if nombre == "sync.status":
        return [(nombre, sync_to_json(rt))]

    carga = getattr(evento, "payload", None)
    return [(nombre, {"event": interno, "payload": _serializable(carga)})]


@api.get("/events/stream")
def events_stream():
    """Server-Sent Events.

    Cada cliente recibe su propia cola: el bus reparte una copia a cada uno,
    de modo que abrir la ventana Tkinter no deja al navegador sin eventos.

    El QR NUNCA viaja por aqui como payload: se avisa de que hay uno nuevo y
    el cliente lo pide como imagen.
    """
    rt = runtime()

    def generar():
        # Estado completo de entrada: quien se conecta a mitad tiene que saber
        # donde esta, no esperar al siguiente cambio.
        yield _sse("session.state", state_to_json(rt))
        yield _sse("sync.status", sync_to_json(rt))
        if rt.pairing is not None and rt.pairing.available:
            yield _sse("session.qr", qr_to_json(rt))

        ultimo_latido = time.monotonic()
        for evento in rt.bus.stream(timeout=1.0):
            ahora = time.monotonic()
            if evento is None:
                if ahora - ultimo_latido >= SSE_HEARTBEAT:
                    ultimo_latido = ahora
                    # Latido CON CONTENIDO: ademas de mantener viva la
                    # conexion, dice en que estado esta el backend. Un
                    # comentario mudo no permitia distinguir "todo en orden"
                    # de "el backend se quedo colgado".
                    yield _sse(
                        "heartbeat",
                        {
                            "ts": _ahora_iso(),
                            "session_state": rt.state.state.value,
                            "sync_state": getattr(rt, "sync_state", "IDLE"),
                        },
                    )
                continue

            for nombre, datos in eventos_para(evento, rt):
                ultimo_latido = ahora
                yield _sse(nombre, datos)

    respuesta = Response(generar(), mimetype="text/event-stream")
    respuesta.headers["Cache-Control"] = "no-cache"
    respuesta.headers["X-Accel-Buffering"] = "no"
    respuesta.headers["Connection"] = "keep-alive"
    return respuesta


def _serializable(valor: Any) -> Any:
    """Reduce el payload a algo que quepa en JSON, sin filtrar de mas."""
    if valor is None or isinstance(valor, (str, int, float, bool)):
        return valor
    if isinstance(valor, dict):
        return {str(k): _serializable(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_serializable(v) for v in valor]
    return str(valor)



# ---------------------------------------------------------------------------
# Recuperacion de historiales pendientes
# ---------------------------------------------------------------------------
#
# Hay conversaciones que llegaron del pairing como pura metadata y sin un solo
# identificador de mensaje. ``HISTORY_SYNC_ON_DEMAND`` va anclado por
# definicion, asi que sin esa primera referencia no se puede pedir nada.
#
# La sesion auxiliar existe SOLO para conseguirla. No escribe mensajes, ni
# multimedia, ni historial: su unico efecto sobre la base es dejar el cursor
# del chat. Todo lo demas lo hace el motor de siempre.
#
# Su vinculacion es INDEPENDIENTE, con su propio QR. El de pywhats sigue en
# ``/session/qr/image`` y son cosas distintas.


def _recovery():
    """El servicio de recuperacion, creado una sola vez por proceso."""
    rt = runtime()
    existente = getattr(rt, "history_recovery", None)
    if existente is not None:
        return existente

    from app.services.history_recovery import HistoryRecoveryService

    servicio = HistoryRecoveryService(rt.settings, rt.database, publish=rt.bus.publish)
    rt.history_recovery = servicio
    return servicio


def _lanzar_recuperacion(chat_id: int | None):
    """Arranca un intento y devuelve su estado. Comun a las dos entradas."""
    rt = runtime()
    if rt.database is None:
        return _error("la base de datos no esta disponible", 503)

    servicio = _recovery()
    disponible, motivo = servicio.provider.available()
    if not disponible:
        return _error_code(
            "RECOVERY_UNAVAILABLE",
            motivo or "la recuperacion avanzada no esta disponible",
            409,
        )

    try:
        trabajo = servicio.start(rt, chat_id)
    except RuntimeError as exc:
        # Ya hay uno en marcha: se devuelve ESE, para que el frontend pueda
        # seguirlo en vez de tener que reintentar a ciegas.
        activo = servicio.active_job()
        return _error_code(
            "RECOVERY_BUSY",
            str(exc),
            409,
            job=activo.to_json() if activo else None,
        )

    cuerpo = trabajo.to_json()
    if chat_id is not None:
        cuerpo["chat_id"] = chat_id
    return jsonify(cuerpo), 202


@api.post("/history/web-bootstrap/recover-pending")
def recover_pending():
    """Intenta recuperar TODAS las conversaciones que esperan referencia.

    Responde enseguida con un ``job_id``: el proceso puede tardar minutos y
    puede pedir un QR auxiliar, asi que bloquear la peticion dejaria al
    navegador esperando sin poder leer el progreso.
    """
    return _lanzar_recuperacion(None)


@api.get("/history/web-bootstrap/recover-pending/status/<job_id>")
def recover_pending_status(job_id: str):
    """Progreso del intento. ``qr_required`` significa QR AUXILIAR."""
    trabajo = _recovery().get(job_id)
    if trabajo is None:
        return _error("trabajo no encontrado", 404)
    return jsonify(trabajo.to_json())


@api.post("/chats/<int:chat_id>/history/recover")
def chat_history_recover(chat_id: int):
    """Lo mismo, para una sola conversacion."""
    from sqlalchemy import select

    from app.models import SEEDLESS_STATUSES, Chat, ChatHistoryState

    sesion = _session()
    if sesion is None:
        return _error("la base de datos no esta disponible", 503)
    try:
        fila = sesion.execute(
            select(Chat.jid, ChatHistoryState.history_status)
            .outerjoin(ChatHistoryState, ChatHistoryState.chat_jid == Chat.jid)
            .where(Chat.id == chat_id)
        ).first()
    finally:
        sesion.close()

    if fila is None:
        return _error("chat no encontrado", 404)
    if fila[1] not in SEEDLESS_STATUSES:
        # Ya tiene referencia: la sesion auxiliar no aporta nada.
        return _error_code(
            "CHAT_NOT_WAITING_SEED",
            f"Esa conversacion no espera referencia (estado: {fila[1]}).",
            409,
            chat_id=chat_id,
            history_status=fila[1],
        )
    return _lanzar_recuperacion(chat_id)


@api.get("/history/web-bootstrap/qr")
def web_bootstrap_qr():
    """PNG del QR AUXILIAR. NO es el de la sesion principal.

    Vincula un dispositivo ADICIONAL a la cuenta, con su propia sesion. El QR
    de pywhats sigue estando en ``/session/qr/image`` y son cosas distintas.
    """
    import io

    from app.core.qr_render import render_qr

    servicio = _recovery()
    activo = servicio.active_job()
    payload = getattr(activo, "qr_payload", None) if activo else None
    if not payload:
        return _error_code(
            "NO_AUXILIARY_QR",
            "No hay ningun codigo auxiliar pendiente.",
            404,
        )

    imagen = render_qr(payload)
    memoria = io.BytesIO()
    imagen.save(memoria, format="PNG")
    memoria.seek(0)
    respuesta = send_file(memoria, mimetype="image/png")
    # Igual que el principal: un QR es una credencial de vinculacion.
    respuesta.headers["Cache-Control"] = "no-store"
    return respuesta


@api.get("/history/web-bootstrap/session")
def web_bootstrap_session():
    """Si la sesion auxiliar ya esta vinculada (para saber si hara falta QR)."""
    servicio = _recovery()
    disponible, motivo = servicio.provider.available()
    return jsonify(
        {
            "available": disponible,
            "reason": motivo,
            "linked": servicio.provider.linked(),
        }
    )


@api.delete("/history/web-bootstrap/session")
def web_bootstrap_forget():
    """Elimina SOLO la sesion auxiliar.

    La principal, el Signal Store y PostgreSQL quedan intactos. Es lo que
    permite quitar toda esta funcion sin consecuencias.
    """
    return jsonify({"removed": _recovery().provider.forget()})
