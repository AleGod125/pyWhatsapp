"""Endpoints de ``/api/v1``.

NADA DE TKINTER
---------------
Este modulo, y todo ``app/api``, no puede importar ninguna capa de interfaz ni
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

from app.auth import ownership
from app.storage.interface import StorageError
from app.auth.web import requiere_drive, requiere_sesion, usuario_actual
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
@requiere_sesion
def session_state():
    """Estado de la sesion.

    NO se deduce de que exista ``device.json``: ese archivo puede estar y la
    sesion estar revocada. El estado real lo lleva la maquina de estados, que
    solo pasa a CONNECTED con el ``<success>`` del servidor.

    Aqui NO se responde 409 cuando la vinculacion es de otro usuario: este
    endpoint es justo lo que el frontend consulta para orientarse, y negarselo
    lo dejaria sin saber que mostrar. Se dice la verdad en el cuerpo.
    """
    rt = runtime()
    cuerpo = state_to_json(rt)

    cuentas = getattr(rt, "whatsapp_accounts", None)
    yo = usuario_actual()
    dueno = cuentas.dueno_actual() if cuentas is not None else None
    de_otro = dueno is not None and yo is not None and dueno != yo.id

    # No se dice de QUIEN es: ni nombre, ni telefono, ni nada suyo.
    cuerpo["owned_by_another_user"] = de_otro
    if de_otro:
        # Para este usuario NO hay vinculacion, aunque el equipo tenga una.
        cuerpo["linked"] = False
        cuerpo["connected"] = False
        cuerpo["pairing_required"] = True
    return jsonify(cuerpo)


@api.post("/session/pair")
@requiere_drive
def session_pair():
    """Inicia la vinculacion PARA el usuario que la pide.

    Es la UNICA via de generar un codigo QR. El arranque ya no vincula solo:
    una vinculacion sin dueno acaba en manos del primero que pase, y el codigo
    quedaba hecho antes de que existiera ningun usuario.

    Exige ademas Google Drive, porque es donde va a guardarse la copia:
    vincular WhatsApp sin sitio donde guardar solo aplaza el problema.

    Es idempotente: si ya hay una vinculacion en marcha, devuelve ESA y no
    lanza otra. Dos vinculaciones simultaneas abririan dos conexiones y
    produirian dos QR, de los cuales solo uno serviria.
    """
    choque = _conflicto_de_sesion()
    if choque is not None:
        return choque

    rt = runtime()
    cuenta = _asegurar_cuenta_de_whatsapp(rt)
    if cuenta is None:
        return _error("la base de datos no esta disponible", 503)
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

    # Aqui empieza de verdad. Se fija el dueno ANTES de generar nada: un QR
    # sin dueno es el fallo que este cambio arregla.
    rt.iniciar_vinculacion(usuario_actual().id, cuenta.id)

    reiniciado = rt.pairing.renew()
    rt.pairing.start_watchdog()
    return (
        jsonify(
            {
                "status": "pairing_restarted" if reiniciado else "pairing_started",
                "restarted": bool(reiniciado),
                "session": state_to_json(rt),
            }
        ),
        202,
    )


@api.get("/session/qr")
@requiere_sesion
def session_qr():
    """Metadatos del QR vigente. NO devuelve el payload.

    El payload es una credencial de vinculacion: quien lo tenga puede enlazar
    un dispositivo a la cuenta. Se sirve solo como imagen, no se guarda en
    disco y no se registra en los logs.
    """
    choque = _conflicto_de_sesion()
    if choque is not None:
        return choque

    return jsonify(qr_to_json(runtime()))


@api.get("/session/qr/image")
@requiere_sesion
def session_qr_image():
    """PNG del QR vigente. Nunca uno caducado."""
    choque = _conflicto_de_sesion()
    if choque is not None:
        return choque

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
#
# Todo lo que recibe un id por la URL comprueba de quien es. Sin eso, cambiar
# el numero en la barra de direcciones seria suficiente para leer la copia de
# otra persona.
#
# Se responde 404, no 403: un 403 sobre un id ajeno confirma que ese id
# existe, y iterando se averigua cuanto tiene guardado otro usuario.


def _no_es_mio(sesion, comprobacion, elemento_id: int) -> bool:
    return not comprobacion(sesion, elemento_id, usuario_actual().id)


def _resolver_contenido(filas):
    """El contenido de esos mensajes, desde el almacenamiento.

    Sin almacenamiento configurado se cae a lo que haya en PostgreSQL: es lo
    correcto mientras la copia no ha empezado a subirse, y se distingue en la
    respuesta.
    """
    from app.storage.reader import MensajeResuelto, MessageReader

    rt = runtime()
    usuario = usuario_actual()
    almacenamiento_activo = getattr(rt, "storage", None)

    pendientes = [f for f in filas if f.storage_status == "ready" and f.segment_id]
    if not pendientes or almacenamiento_activo is None:
        return {
            f.id: MensajeResuelto(id=f.id, text=f.text, fuente="local") for f in filas
        }

    lector = getattr(rt, "_message_reader", None)
    if lector is None:
        lector = MessageReader(rt.database, almacenamiento_activo)
        rt._message_reader = lector

    return lector.resolver(
        filas,
        user_id=usuario.id,
        almacenamiento=rt.storage_para(usuario.id),
    )


def _asegurar_cuenta_de_whatsapp(rt):
    """La cuenta de WhatsApp del usuario actual, creandola si no la tiene.

    Una por usuario en esta fase. El identificador NUNCA llega del navegador:
    sale de la cookie, porque si el cliente pudiera decir de quien es la
    cuenta, cambiarlo bastaria para apoderarse de la de otro.
    """
    cuentas = getattr(rt, "whatsapp_accounts", None)
    if cuentas is None:
        return None
    return cuentas.asegurar_cuenta(usuario_actual().id)


def _conflicto_de_sesion():
    """``None`` si el usuario puede usar la sesion de WhatsApp de este equipo.

    En esta fase el runtime sostiene UNA vinculacion. Si es de otro usuario,
    se responde con un conflicto generico en vez de dejarle ver una copia
    ajena o de arrancarle la sesion al dueno.

    Se comprueban DOS cosas: quien tiene la cuenta marcada como vinculada en
    la base, y quien tiene el runtime de este proceso. La segunda cubre el
    hueco entre pedir la vinculacion y completarla: durante ese rato la base
    todavia no dice ``linked``, pero el QR ya existe y es de alguien.
    """
    from app.auth.whatsapp_accounts import ConflictoDeSesion

    rt = runtime()
    yo = usuario_actual()
    if yo is None:
        return None

    if not rt.es_mia_la_sesion(yo.id):
        return _error_code(
            "ACCOUNT_RUNTIME_IN_USE",
            "Este dispositivo tiene una vinculacion de WhatsApp en marcha de "
            "otro usuario.",
            409,
        )

    cuentas = getattr(rt, "whatsapp_accounts", None)
    if cuentas is None:
        return None
    try:
        cuentas.exigir_propiedad(yo.id)
    except ConflictoDeSesion as choque:
        # No se dice de QUIEN es: ni nombre, ni telefono, ni nada suyo.
        return _error_code(choque.code, str(choque), 409)
    return None


@api.get("/chats")
@requiere_drive
def chats():
    """Sidebar. Ordenado por ``last_message_timestamp`` descendente."""
    sesion = _session()
    if sesion is None:
        return _error("la base de datos no esta disponible", 503)
    try:
        busqueda = (request.args.get("search") or "").strip() or None
        limite = min(_entero("limit", 500) or 500, 1000)
        resumenes = repo.list_chat_summaries(
            sesion,
            search=busqueda,
            limit=limite,
            accounts=ownership.cuentas_de(sesion, usuario_actual().id),
        )
    finally:
        sesion.close()
    return jsonify({"chats": [chat_to_json(c) for c in resumenes], "count": len(resumenes)})


@api.get("/chats/<int:chat_id>")
@requiere_drive
def chat_detail(chat_id: int):
    sesion = _session()
    if sesion is None:
        return _error("la base de datos no esta disponible", 503)
    try:
        if _no_es_mio(sesion, ownership.chat_es_de, chat_id):
            return _error("chat no encontrado", 404)
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
@requiere_drive
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
        if _no_es_mio(sesion, ownership.chat_es_de, chat_id):
            return _error("chat no encontrado", 404)
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

    # El CONTENIDO se resuelve desde el almacenamiento. PostgreSQL ha dicho
    # que mensajes son y en que orden; el texto vive en Drive.
    try:
        contenido = _resolver_contenido(filas)
    except StorageError as exc:
        # NO se devuelve una lista vacia: "no se pudo traer" y "no hay
        # mensajes" son cosas muy distintas, y confundirlas hace que una copia
        # parezca perdida cuando solo esta lejos.
        return _error_code(exc.code, exc.message, 503)

    mensajes = []
    for f in filas:
        cuerpo = message_to_json(f, adjuntos.get(f.id))
        resuelto = contenido.get(f.id)
        if resuelto is not None:
            cuerpo["text"] = resuelto.text
            # De donde salio. Sirve para saber si la copia ya esta a salvo, y
            # es lo que hace comprobable que Drive es la fuente.
            cuerpo["content_source"] = resuelto.fuente
        mensajes.append(cuerpo)

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
            # Mensajes de este chat todavia sin subir. Con la pagina vacia,
            # distingue "no hay historial" de "todavia se esta guardando":
            # decir lo primero cuando pasa lo segundo hace creer que se
            # perdio la conversacion.
            "storage_pending": _pendientes_de_subir(chat_id),
        }
    )


def _pendientes_de_subir(chat_id: int) -> int:
    """Cuantos mensajes de ese chat siguen solo en PostgreSQL."""
    from sqlalchemy import func, select

    from app.models import Message

    sesion = _session()
    if sesion is None:
        return 0
    try:
        return int(
            sesion.execute(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.chat_id == chat_id,
                    Message.storage_status != "ready",
                )
            ).scalar()
            or 0
        )
    finally:
        sesion.close()


@api.post("/chats/<int:chat_id>/history/recheck")
@requiere_drive
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

    sesion = _session()
    if sesion is None:
        return _error("la base de datos no esta disponible", 503)
    try:
        if _no_es_mio(sesion, ownership.chat_es_de, chat_id):
            return _error("chat no encontrado", 404)
    finally:
        sesion.close()

    resultado = HistoryRecheck(
        rt.database, rt.settings, rt.runtime_owner_account_id
    ).recheck(chat_id)
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
        # La propiedad se comprueba AQUI, que es por donde pasan las cuatro
        # rutas de multimedia. Repartirla por cada una seria cuatro sitios
        # donde olvidarla, y basta olvidarla en uno.
        if not ownership.media_es_de(sesion, media_id, usuario_actual().id):
            return None, None
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
            # Donde vive el original si la copia local ya se desalojo.
            "drive_file_id": fila.drive_file_id,
            "storage_status": fila.storage_status,
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
@requiere_drive
def media_detail(media_id: int):
    datos, _ = _media_row(media_id)
    if datos is None:
        return _error("adjunto no encontrado", 404)
    return jsonify(media_to_json(_MediaView(datos)))


@api.post("/media/<int:media_id>/retry")
@requiere_drive
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
        # La propiedad se comprueba ANTES que cualquier otra cosa. Si se
        # mirara despues, un 409 sobre un adjunto ajeno ya confirmaria que
        # existe: el orden de las comprobaciones tambien filtra.
        if _no_es_mio(sesion, ownership.media_es_de, media_id):
            return _error("adjunto no encontrado", 404)

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
@requiere_drive
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
        if _no_es_mio(sesion, ownership.mensaje_es_de, message_id):
            return _error("mensaje no encontrado", 404)
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
@requiere_drive
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
    # 1. La copia local, si sigue estando. Es lo mas rapido y no gasta cupo
    #    de Google. Flask ya resuelve Range/206 sobre un archivo real.
    ruta = _archivo_local(datos)
    if ruta is not None:
        return send_file(
            ruta,
            mimetype=datos.get("mime_type") or None,
            download_name=datos.get("file_name") or ruta.name,
            conditional=True,
        )

    # 2. Si no, desde Drive. Angular nunca se entera: sigue pidiendo la misma
    #    URL y no ve identificadores de archivo ni enlaces de Google.
    if datos.get("drive_file_id"):
        return _servir_desde_drive(media_id, datos)

    return _error(
        "el adjunto todavia no se ha descargado",
        404,
        status=datos["download_status"],
    )


def _servir_desde_drive(media_id: int, datos: dict):
    """Entrega el adjunto leyendo de Drive, respetando ``Range``.

    Un video no se descarga entero para entregar diez segundos: el rango se
    traduce a los trozos cifrados que lo cubren y solo se piden esos.
    """
    from app.storage.interface import StorageAuthError, StorageError
    from app.storage.media import MediaStorage

    rt = runtime()
    usuario = usuario_actual()
    if getattr(rt, "storage", None) is None:
        return _error("el almacenamiento no esta disponible", 503)

    sesion = _session()
    if sesion is None:
        return _error("la base de datos no esta disponible", 503)
    try:
        from app.models import MediaFile

        fila = sesion.get(MediaFile, media_id)
        if fila is None:
            return _error("adjunto no encontrado", 404)

        total = fila.file_size or 0
        inicio, fin = _rango_pedido(total)

        try:
            trozo = MediaStorage(rt.database, rt.settings, rt.storage).leer_rango(
                fila,
                inicio=inicio,
                fin=fin,
                almacenamiento=rt.storage_para(usuario.id),
                user_id=usuario.id,
            )
        except StorageAuthError as exc:
            return _error_code("DRIVE_NOT_AUTHORIZED", exc.message, 403)
        except StorageError as exc:
            return _error_code(exc.code, exc.message, 502)
        except FileNotFoundError:
            return _error("el adjunto ya no esta disponible", 404)
    finally:
        sesion.close()

    parcial = request.headers.get("Range") is not None
    respuesta = current_app.response_class(
        trozo.datos,
        status=206 if parcial else 200,
        mimetype=datos.get("mime_type") or "application/octet-stream",
    )
    respuesta.headers["Accept-Ranges"] = "bytes"
    respuesta.headers["Content-Length"] = str(len(trozo.datos))
    if parcial:
        respuesta.headers["Content-Range"] = trozo.content_range
    # El contenido es privado: no puede quedarse en caches intermedias.
    respuesta.headers["Cache-Control"] = "private, no-store"
    return respuesta


def _rango_pedido(total: int) -> tuple[int, int]:
    """Lee la cabecera ``Range``. Sin ella, el archivo entero."""
    crudo = request.headers.get("Range", "")
    if not crudo.startswith("bytes="):
        return 0, max(0, total - 1)
    trozo = crudo[len("bytes=") :].split(",")[0].strip()
    desde, _, hasta = trozo.partition("-")
    try:
        inicio = int(desde) if desde else 0
        fin = int(hasta) if hasta else max(0, total - 1)
    except ValueError:
        return 0, max(0, total - 1)
    return max(0, inicio), max(inicio, fin)


@api.get("/media/<int:media_id>/thumbnail")
@requiere_drive
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
@requiere_drive
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
@requiere_drive
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


@api.get("/onboarding/recovery")
def onboarding_recovery():
    """En que punto va la RECUPERACION del historial, para el usuario.

    Deliberadamente separada de ``/onboarding/status``, que ya existe en
    ``auth_routes`` y contesta otra pregunta: por donde va el usuario dentro
    del alta (login, Google, vinculacion). Aquella decide a que pantalla ir;
    esta cuenta que esta pasando una vez dentro.

    Juntarlas obligaria a que cada pantalla filtrara la mitad que no le
    importa, y una de las dos acabaria mandando sobre la otra.

    Una sola llamada con todo lo que la pantalla necesita saber: si hace falta
    escanear el codigo principal, si hace falta el segundo, si esta recuperando
    o si ya termino. El frontend no tiene que juntar cuatro endpoints ni
    deducir la fase de contadores sueltos.

    Es estado de EJECUCION: describe un momento, no un dato que haya que
    guardar. Por eso no hay tabla ni migracion.
    """
    from app.core.primary import (
        RECONNECTING,
        mensaje_para_el_usuario,
        razon_no_lista,
    )

    rt = runtime()
    # La conexion principal manda, y se pregunta ENTERA. Que el estado diga
    # CONNECTED no basta: un emparejamiento a medias deja el objeto en pie sin
    # identidad ni Signal, y con eso no se puede excavar nada. Se evalua aqui,
    # antes que ninguna otra fase, porque si falta esto lo demas sobra.
    motivo_principal = razon_no_lista(rt)
    principal_listo = motivo_principal is None

    supervisor = getattr(rt, "web_companion", None)
    web = {
        "enabled": bool(supervisor is not None and getattr(supervisor, "habilitado", False)),
        "running": bool(supervisor is not None and getattr(supervisor, "vivo", False)),
        "ready": False,
        "qr_available": False,
        "qr_generation": 0,
        "state": "disabled",
    }
    if supervisor is not None and web["enabled"]:
        instantanea = supervisor.snapshot()
        web["ready"] = bool(instantanea.get("web_client_ready"))
        web["qr_available"] = bool(instantanea.get("qr_available"))
        web["qr_generation"] = int(instantanea.get("qr_generation") or 0)
        web["state"] = str(instantanea.get("state") or "disabled")

    vigilante = getattr(rt, "auto_recovery", None)
    recuperacion = vigilante.estado.to_json() if vigilante is not None else {}

    # Los recuentos que de verdad dicen si esto termino.
    resumen = {}
    if rt.database is not None:
        from app.history.resumen import resumen_de_estado

        try:
            datos = resumen_de_estado(
                rt.database, account_id=getattr(rt, "runtime_owner_account_id", None)
            )
            resumen = {
                "chats_total": datos.chats_total,
                "waiting_seed": datos.waiting_seed,
                "pending": datos.pending,
                "fetching": datos.fetching,
                "timeout": datos.timeout,
                "exhausted": datos.exhausted,
            }
        except Exception:  # noqa: BLE001 - un recuento no puede tumbar la ruta
            resumen = {}

    cola = getattr(rt, "seed_queue", None)
    estado_cola = cola.estado() if cola is not None and hasattr(cola, "estado") else None

    return jsonify(
        {
            "phase": _fase_de_onboarding(
                principal_listo,
                web,
                recuperacion,
                resumen,
                estado_cola,
                motivo_principal,
            ),
            "primary": {
                "linked": principal_listo,
                "reason": motivo_principal,
                # Reconectando NO es "vuelve a vincular": las credenciales
                # siguen valiendo y ensenar el codigo mandaria al usuario a
                # rehacer algo que no esta roto.
                "reconnecting": motivo_principal == RECONNECTING,
                "message": mensaje_para_el_usuario(motivo_principal),
            },
            "web_companion": web,
            "recovery": recuperacion,
            "counts": resumen,
            "queue": estado_cola,
        }
    )


def _fase_de_onboarding(
    principal_listo, web, recuperacion, resumen, cola, motivo_principal=None
):
    """La fase, en el orden en que el usuario la vive.

    ``complete`` es exigente a proposito: mientras quede una conversacion
    esperando ancla o un reintento por hacer, esto es ``partial``. Decir
    "completo" con trabajo pendiente es la clase de mentira que hace dudar de
    todo lo demas.
    """
    # Primero de todo, y sin excepciones. Si el usuario estaba en cualquier
    # fase posterior y la conexion principal se cae, vuelve aqui: seguir
    # ensenandole el segundo codigo seria mandarlo a escanear el que no toca.
    if not principal_listo:
        from app.core.primary import RECONNECTING

        # Salvo un corte pasajero: ahi no hay nada que volver a vincular.
        return "reconnecting" if motivo_principal == RECONNECTING else "pairing_primary"
    if cola and cola.get("waiting_for_phone"):
        return "waiting_for_phone"
    if web.get("enabled"):
        if web.get("qr_available"):
            return "pairing_web"
        if not web.get("ready"):
            return "waiting_web"
    if recuperacion.get("waiting_reason") == "INITIAL_SYNC_RUNNING":
        return "initial_sync"
    if cola and cola.get("pending"):
        return "recovering_history"
    if resumen:
        pendientes = (
            int(resumen.get("waiting_seed") or 0)
            + int(resumen.get("pending") or 0)
            + int(resumen.get("fetching") or 0)
            + int(resumen.get("timeout") or 0)
        )
        return "complete" if pendientes == 0 else "partial"
    return "recovering_history"


@api.post("/sync/full-recovery")
@requiere_drive
def sync_full_recovery():
    """Revisa TODOS los chats recuperables y vuelve a intentarlo.

    Es el mismo ciclo, con una sola diferencia: adelanta una vez la espera de
    reintento de los chats que la estaban cumpliendo, porque el usuario acaba
    de pedir explicitamente que se intente ahora.

    Lo que NO hace, y conviene decirlo porque la palabra "completo" invita a
    pensarlo: no borra mensajes, ni anclas, ni multimedia, ni nada de Drive;
    no vuelve a emparejar; no toca la sesion; y no reabre las conversaciones
    que el telefono ya dio por terminadas -- para eso hace falta evidencia
    nueva, no un boton.
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
        job_id = trabajo.start(rt, profundo=True)
    except SyncAlreadyRunningError as exc:
        # No se lanza un segundo ciclo: se devuelve el que ya corre.
        return _error_code(
            "SYNC_ALREADY_RUNNING", str(exc), 409, sync=trabajo.snapshot()
        )
    except SyncUnavailableError as exc:
        return _error_code(exc.code, str(exc), 409, session=state_to_json(rt))

    return (
        jsonify(
            {"started": True, "job_id": job_id, "state": "running", "mode": "full"}
        ),
        202,
    )


@api.post("/chats/<int:chat_id>/history/retry")
@requiere_drive
def chat_history_retry(chat_id: int):
    """Vuelve a pedirle a WhatsApp el historial de UN chat.

    Es el boton "Volver a comprobar" de una conversacion que se quedo en
    ``timeout``: el telefono no contesto y su espera de reintento aun no ha
    vencido. Aqui se adelanta esa espera SOLO para este chat y se le pone en
    la cola.

    No es lo mismo que ``/history/recheck``, que mira lo que ya hay en casa
    sin pedir nada. Este si pide, y por eso comprueba antes que la sesion este
    conectada y que el chat tenga un ancla real: pedir sin ancla produce un
    ACK y despues silencio.

    Una peticion cada vez: entra por la misma cola y el mismo candado global
    que todo lo demas.
    """
    from sqlalchemy import select, update

    from app.history.cursor import get_valid_history_cursor
    from app.models import Chat, ChatHistoryState

    rt = runtime()
    if rt.database is None:
        return _error("la base de datos no esta disponible", 503)

    sesion = _session()
    if sesion is None:
        return _error("la base de datos no esta disponible", 503)
    try:
        if _no_es_mio(sesion, ownership.chat_es_de, chat_id):
            return _error("chat no encontrado", 404)
    finally:
        sesion.close()

    cola = getattr(rt, "seed_queue", None)
    backfill = getattr(rt, "backfill", None)
    if cola is None or backfill is None or getattr(backfill, "_client", None) is None:
        return _error_code(
            "SESSION_NOT_CONNECTED",
            "WhatsApp no esta conectado; no se puede pedir historial ahora.",
            409,
        )

    with rt.database.transaction() as db:
        jid = db.execute(select(Chat.jid).where(Chat.id == chat_id)).scalar_one_or_none()
        if jid is None:
            return _error("chat no encontrado", 404)
        estado = db.execute(
            select(ChatHistoryState).where(ChatHistoryState.chat_jid == jid)
        ).scalar_one_or_none()
        if estado is not None and estado.history_status == "exhausted":
            return _error_code(
                "CHAT_ALREADY_COMPLETE",
                "WhatsApp ya entrego todo el historial disponible de este chat.",
                409,
            )
        cursor = get_valid_history_cursor(db, chat_id=chat_id, chat_jid=jid)
        if cursor is None:
            return _error_code(
                "NO_VALID_CURSOR",
                (
                    "Este chat todavia no tiene una referencia con la que pedir "
                    "historial."
                ),
                409,
            )
        # Solo este chat. La espera de los demas se queda como estaba.
        db.execute(
            update(ChatHistoryState)
            .where(ChatHistoryState.chat_jid == jid)
            .values(next_retry_at=None)
        )

    encolados = cola.enqueue([jid])
    return jsonify(
        {
            "queued": bool(encolados),
            "chat_id": chat_id,
            "already_queued": not encolados,
            "queue": cola.estado() if hasattr(cola, "estado") else None,
        }
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
    # Revision local de historiales pendientes: la ruta normal del producto,
    # sin sesion auxiliar. Se pasan tal cual: el nombre ya esta en el
    # vocabulario del frontend.
    "history.recheck.started": "history.recheck.started",
    "history.recheck.progress": "history.recheck.progress",
    "history.recheck.completed": "history.recheck.completed",
    "history.backfill.completed": "history.backfill.completed",
    # Recuperacion auxiliar (Web Bootstrap). Apagada por defecto.
    "history.recovery.started": "history.recovery.started",
    "history.recovery.progress": "history.recovery.progress",
    "history.recovery.completed": "history.recovery.completed",
    "history.seed.found": "history.seed.found",
    "history.seed.not_found": "history.seed.not_found",
    "history.backfill.started": "history.backfill.started",
    # Referencias del Web Companion: aplicarlas es una accion explicita del
    # usuario y el panel tiene que poder seguirla sin recargar.
    "web_seed_apply_started": "history.web_seeds.started",
    "web_seed_apply_complete": "history.web_seeds.completed",
    # El telefono se durmio: la recuperacion se para sin perder nada, y eso
    # hay que decirlo. No es un error del protocolo.
    "history.waiting_for_phone": "history.waiting_for_phone",
    "history.recovery_resumed": "history.recovery_resumed",
    "session_valid": "session.state",
    "logged_out": "session.state",
    "disconnected": "session.state",
    "client_error": "session.state",
    "client_stopped": "session.state",
    "session_state_changed": "session.state",
    "history_ingested": "history.progress",
    # Un chat cambio de estado (espera ancla -> pendiente -> excavando ->
    # completo). Sin esto la pantalla ensenaba el estado del momento en que se
    # cargo: un chat podia decir "Recuperando historial" con el trabajo ya
    # terminado, o "Esperando referencia" con tres mil mensajes dentro.
    "chat_history_status": "chat.status",
    # El indice de WhatsApp Web termino: puede haber conversaciones nuevas.
    "web_inventory_done": "chat.inventory",
    # Y una por una, con su fila entera dentro. Es lo que permite que una
    # conversacion recien descubierta aparezca sola, sin recargar: el aviso
    # escueto obligaba a pedir la lista, y cincuenta conversaciones nuevas
    # eran cincuenta peticiones.
    "web_chat_created": "chat.created",
    "web_chat_updated": "chat.updated",
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


#: Eventos que cuentan algo de la sesion de WhatsApp: quien la tiene, que le
#: llega, como va su sincronizacion. Solo su dueno los recibe.
EVENTOS_DE_SESION = (
    "session.",
    "sync.",
    "message.",
    "chat.",
    "media.",
    "history.",
    "backfill.",
)


def _es_de_la_sesion(nombre: str) -> bool:
    return nombre.startswith(EVENTOS_DE_SESION)


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
@requiere_sesion
def events_stream():
    """Server-Sent Events.

    Cada cliente recibe su propia cola: el bus reparte una copia a cada uno,
    de modo que abrir la ventana Tkinter no deja al navegador sin eventos.

    El QR NUNCA viaja por aqui como payload: se avisa de que hay uno nuevo y
    el cliente lo pide como imagen.

    FILTRADO POR DUENO
    ------------------
    El bus es unico para el proceso, asi que sin filtrar aqui un usuario
    recibiria los avisos de la sesion de otro: cuando llega un mensaje, cuando
    cambia su estado, cuando hay un QR nuevo. Eso ya es informacion sobre otra
    persona aunque no lleve su contenido.

    Quien no sea el dueno de la sesion activa recibe solo lo suyo: los eventos
    de almacenamiento de su propia cuenta y los latidos.
    """
    rt = runtime()
    yo = usuario_actual()
    es_dueno = yo is not None and rt.es_mia_la_sesion(yo.id)

    def generar():
        # Estado completo de entrada: quien se conecta a mitad tiene que saber
        # donde esta, no esperar al siguiente cambio.
        yield _sse("session.state", state_to_json(rt))
        if es_dueno:
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
                # Lo que cuenta algo de la sesion de WhatsApp solo va a su
                # dueno. El bus es unico para el proceso: sin esto, un usuario
                # sabria cuando le llega un mensaje a otro.
                if not es_dueno and _es_de_la_sesion(nombre):
                    continue
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


def _web_bootstrap_apagado():
    """``None`` si esta encendida; si no, la respuesta que lo explica.

    La ruta SIGUE registrada estando apagada, y es deliberado: si no
    existiera, el navegador recibiria un 404 sin cabeceras CORS y lo
    reportaria como un error de CORS, que manda a diagnosticar el sitio
    equivocado. Registrada, el frontend obtiene un motivo legible.
    """
    if runtime().settings.web_bootstrap_enabled:
        return None
    return _error_code(
        "WEB_BOOTSTRAP_DISABLED",
        "La recuperacion auxiliar esta desactivada. El producto no la "
        "necesita: usa POST /history/recheck-pending, que revisa lo mismo "
        "sin vincular un segundo dispositivo.",
        404,
    )


def _recovery():
    """El servicio de recuperacion, creado una sola vez por proceso."""
    rt = runtime()
    existente = getattr(rt, "history_recovery", None)
    if existente is not None:
        return existente

    from app.experimental.history_recovery import HistoryRecoveryService

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
@requiere_drive
def recover_pending():
    """Intenta recuperar TODAS las conversaciones que esperan referencia.

    Responde enseguida con un ``job_id``: el proceso puede tardar minutos y
    puede pedir un QR auxiliar, asi que bloquear la peticion dejaria al
    navegador esperando sin poder leer el progreso.
    """
    apagado = _web_bootstrap_apagado()
    if apagado is not None:
        return apagado

    return _lanzar_recuperacion(None)


@api.get("/history/web-bootstrap/recover-pending/status/<job_id>")
@requiere_drive
def recover_pending_status(job_id: str):
    """Progreso del intento. ``qr_required`` significa QR AUXILIAR."""
    apagado = _web_bootstrap_apagado()
    if apagado is not None:
        return apagado

    trabajo = _recovery().get(job_id)
    if trabajo is None:
        return _error("trabajo no encontrado", 404)
    return jsonify(trabajo.to_json())


@api.post("/chats/<int:chat_id>/history/recover")
@requiere_drive
def chat_history_recover(chat_id: int):
    """Lo mismo, para una sola conversacion."""
    apagado = _web_bootstrap_apagado()
    if apagado is not None:
        return apagado

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
@requiere_drive
def web_bootstrap_qr():
    """PNG del QR AUXILIAR. NO es el de la sesion principal.

    Vincula un dispositivo ADICIONAL a la cuenta, con su propia sesion. El QR
    de pywhats sigue estando en ``/session/qr/image`` y son cosas distintas.
    """
    apagado = _web_bootstrap_apagado()
    if apagado is not None:
        return apagado

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
@requiere_drive
def web_bootstrap_session():
    """Si la sesion auxiliar ya esta vinculada (para saber si hara falta QR)."""
    apagado = _web_bootstrap_apagado()
    if apagado is not None:
        return apagado

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
@requiere_drive
def web_bootstrap_forget():
    """Elimina SOLO la sesion auxiliar.

    La principal, el Signal Store y PostgreSQL quedan intactos. Es lo que
    permite quitar toda esta funcion sin consecuencias.
    """
    apagado = _web_bootstrap_apagado()
    if apagado is not None:
        return apagado

    return jsonify({"removed": _recovery().provider.forget()})


# ---------------------------------------------------------------------------
# Revision de historiales pendientes (ruta normal del producto)
# ---------------------------------------------------------------------------
#
# Es la version en bloque del boton "volver a comprobar" de cada chat. Todo
# local: resuelve alias, busca un ancla real y reinterpreta los blobs que
# WhatsApp YA entrego. No vincula ningun dispositivo ni pide un segundo QR.


def _recheck_pendientes():
    """El servicio de revision, creado una sola vez por proceso."""
    rt = runtime()
    existente = getattr(rt, "pending_recheck", None)
    if existente is not None:
        return existente

    from app.services.pending_recheck import PendingRecheckService

    servicio = PendingRecheckService(rt.settings, rt.database, publish=rt.bus.publish)
    rt.pending_recheck = servicio
    return servicio


@api.post("/history/recheck-pending")
@requiere_drive
def history_recheck_pending():
    """Revisa TODOS los chats que esperan un ancla, sin salir de casa.

    Responde ``202`` enseguida: reinterpretar los blobs de decenas de chats
    tarda, y el progreso llega por SSE (``history.recheck.*``).

    Con ``?auto=1`` es la revision que el panel dispara al abrirse: respeta una
    espera entre ejecuciones y, si ya hay una en marcha, devuelve ESA en vez de
    fallar. Sin el parametro es el boton, que se ejecuta siempre.
    """
    rt = runtime()
    if rt.database is None:
        return _error("la base de datos no esta disponible", 503)

    auto = request.args.get("auto", "").lower() in ("1", "true", "yes")
    servicio = _recheck_pendientes()
    try:
        trabajo = servicio.start(rt, auto=auto)
    except RuntimeError as exc:
        # Ya hay una en marcha: se devuelve ESA, para que el frontend pueda
        # seguirla en vez de reintentar a ciegas.
        activo = servicio.active_job()
        return _error_code(
            "RECHECK_BUSY",
            str(exc),
            409,
            job=activo.to_json() if activo else None,
        )
    return jsonify(trabajo.to_json()), 202


@api.get("/history/recheck-pending/status/<job_id>")
@requiere_drive
def history_recheck_pending_status(job_id: str):
    """Progreso de la revision, para quien no pueda usar SSE."""
    trabajo = _recheck_pendientes().get(job_id)
    if trabajo is None:
        return _error("trabajo no encontrado", 404)
    return jsonify(trabajo.to_json())
