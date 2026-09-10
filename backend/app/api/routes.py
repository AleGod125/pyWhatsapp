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
    """El runtime base del proceso. INFRAESTRUCTURA, no sesion de WhatsApp.

    Sirve para llegar a la base de datos y a la configuracion. Para cualquier
    cosa que dependa de una sesion de WhatsApp --estado, emparejamiento,
    codigo QR, chats-- hay que usar `runtime_de_mi_cuenta()`, que resuelve por
    membresia. Usar este de ahi es como se acaba contestando con la cuenta de
    otra persona.
    """
    return current_app.config["RUNTIME"]


def runtime_de_mi_cuenta(*, crear: bool = False, pedida: Any = None) -> Any:
    """El runtime de la cuenta de quien hace la peticion. Puede ser ``None``.

    ``pedida`` deja elegir CUAL de sus cuentas, y hace falta para vincular una
    nueva: sin ella se resuelve la ACTIVA, y "agregar cuenta" acababa
    emparejando sobre la que ya estaba. El identificador se comprueba contra
    las cuentas de quien pregunta, asi que cambiarlo en la URL no da acceso a
    la de otro.
    """
    from app.api.account_runtime import runtime_de_mi_cuenta as _resolver

    return _resolver(crear=crear, pedida=pedida)


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
    # EL ESTADO ES EL DE MI RUNTIME, NO EL DEL PROCESO.
    #
    # `runtime()` devuelve el de quien vinculara primero en esta maquina. Con
    # dos personas, a la segunda se le contaba el estado de la primera:
    # CONNECTED, su fase de sincronizacion, su generacion de codigo QR. Los
    # tres campos de acceso se corregian despues a mano, pero el resto del
    # cuerpo seguia siendo ajeno.
    mio = runtime_de_mi_cuenta()
    yo = usuario_actual()

    # De quien es esta vinculacion se decide por MEMBRESIA, no por el estado
    # global del proceso.
    #
    # EL FALLO, TAL Y COMO SE VIO: usuario A con su WhatsApp conectado; B se
    # registra, entra, y la pantalla le dice "Cuenta vinculada" -- la de A.
    # Deducir el acceso de "hay un WhatsApp conectado en este servidor" es
    # justo lo que no se puede hacer en cuanto hay mas de una persona.
    #
    # La membresia es la UNICA fuente. Antes habia detras un respaldo que
    # preguntaba `dueno_actual()` --"quien tiene una cuenta vinculada en esta
    # base"--, y esa pregunta no distingue equipos de personas: con A
    # vinculado, cualquier B recibia el estado de A. Un respaldo que responde
    # con los datos de otro es peor que no tener respaldo.
    de_otro = False
    base = runtime()  # solo por la base de datos, que es del proceso
    if yo is not None and base.database is not None:
        try:
            from app.auth.memberships import cuenta_efectiva_de

            with base.database.transaction() as sesion_db:
                mia = cuenta_efectiva_de(sesion_db, yo.id)
            # Sin cuenta propia esta persona no tiene vinculacion, haya lo que
            # haya conectado en la maquina.
            de_otro = mia is None
        except Exception:  # noqa: BLE001 - ante la duda, se pide vincular
            log.debug("No se pudo resolver la membresia; se pedira vincular")
            de_otro = True

    # El cuerpo se construye DESPUES de saber si hay cuenta propia.
    #
    # Sin ella no se recorta el estado ajeno campo a campo --eso deja pasar
    # todo lo que nadie se acuerde de recortar--: sencillamente no se mira ese
    # runtime. Se responde el estado de "no hay vinculacion", que es la verdad
    # para esta persona.
    if mio is None:
        from app.api.serializers import estado_sin_vinculacion

        cuerpo = estado_sin_vinculacion(base)
    else:
        cuerpo = state_to_json(mio)

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
    # AQUI NO HAY NINGUN GUARDA DE DISPOSITIVO, y es deliberado.
    #
    # Antes se comprobaba si "la vinculacion de este equipo" era de quien
    # preguntaba, y si no, se contestaba 409 ACCOUNT_RUNTIME_IN_USE. Eso
    # convertia el equipo en propiedad del primero que vinculara: con la
    # cuenta de A conectada, B --que no ha vinculado nada-- no podia ni pedir
    # su codigo. Se midio: "Este dispositivo tiene una vinculacion de WhatsApp
    # en marcha de otro usuario", con B mirando una pantalla sin QR.
    #
    # El equipo no es de nadie. Cada persona vincula SU cuenta, en SU runtime,
    # con SU carpeta de sesion. Lo que impide ver lo ajeno no es un guarda de
    # proceso: es que todo lo de abajo se resuelve por membresia.

    # El emparejamiento ocurre en el runtime de MI cuenta, creandola si hace
    # falta. Antes se usaba el runtime unico del proceso, y por eso con la
    # cuenta de otra persona conectada esto contestaba 409 --su estado-- o
    # devolvia su codigo QR.
    # LA CUENTA QUE SE PIDE, no "la que estabas mirando".
    #
    # Esto ignoraba `?account_id=` a proposito, de cuando habia una sola
    # cuenta por usuario: `asegurar_cuenta()` devuelve LA ACTIVA, que era la
    # respuesta correcta entonces.
    #
    # Con varias cuentas es el fallo de raiz del emparejamiento. La secuencia
    # medida:
    #
    #   1. "+ Agregar cuenta" crea la fila B (`never_linked`) y NO la activa
    #      --deliberado: activarla dejaria al usuario mirando una lista vacia
    #      mientras escanea--;
    #   2. el frontend llama a `/session/pair?account_id=B`;
    #   3. aqui se ignoraba y se cogia la ACTIVA, o sea A;
    #   4. el QR que se enseñaba era el del runtime de A;
    #   5. al escanear el segundo telefono, sus credenciales caian en la
    #      carpeta de A -> IDENTIDAD INVERTIDA.
    #
    # El identificador SIGUE sin creerse por si solo: `cuenta_del_usuario_actual`
    # lo comprueba contra las cuentas de quien pregunta, asi que cambiarlo en
    # la URL no da acceso a la de otro. Lo que se admite es elegir entre LAS
    # SUYAS, que es justo lo que "agregar cuenta" necesita.
    pedida = request.args.get("account_id") or None

    # LA CUENTA Y EL RUNTIME, RESUELTOS IGUAL. Ese era el fallo.
    #
    # `runtime_de_mi_cuenta` ya honraba `?account_id=` --lo lee por su cuenta
    # en `_cuenta_pedida_en_la_peticion`-- asi que `rt` SI era el runtime de la
    # cuenta pedida. La que se resolvia mal era la otra mitad: `cuenta` salia
    # de `asegurar_cuenta()`, que devuelve LA ACTIVA.
    #
    # Y abajo se hace `rt.iniciar_vinculacion(usuario, cuenta.id)`. O sea: el
    # runtime de B se marcaba como A. Al llegar el pair-success, se sellaba A.
    # En el log se veia asi, y parecia que los eventos se cruzaban entre
    # workers:
    #
    #     14:19:44  runtime levantado para la cuenta 76a24d67
    #     14:20:13  pair-success -> Cuenta e2492d66 marcada como vinculada
    #
    # No se cruzaba nada. El runtime nuevo estaba ahi, con la etiqueta del
    # viejo puesta.
    #
    # `cuenta_del_usuario_actual` comprueba `tiene_acceso` antes de devolver
    # nada, asi que cambiar el identificador en la URL no da acceso a la cuenta
    # de otro: solo deja elegir entre las propias.
    from app.api.account_runtime import cuenta_del_usuario_actual

    cuenta = cuenta_del_usuario_actual(crear=True, pedida=pedida)
    if cuenta is None:
        return _error_code(
            "ACCOUNT_NOT_FOUND",
            "No se encontro esa cuenta de WhatsApp entre las tuyas.",
            404,
        )
    rt = runtime_de_mi_cuenta(crear=True, pedida=pedida)
    if rt is None:
        return _error_code(
            "ACCOUNT_RUNTIME_UNAVAILABLE",
            "No se pudo preparar la sesion de WhatsApp de tu cuenta.",
            503,
        )
    # Si este backend soporta WhatsApp o no es una propiedad del PROCESO --el
    # modo local no lo trae-- y no de una cuenta. Se pregunta al runtime base:
    # preguntarselo al de la cuenta daria "modo local" en cuanto el suyo aun
    # no ha arrancado su cliente.
    if not runtime().info().whatsapp_enabled:
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
    # EL PUNTO CRITICO. El codigo QR sale del runtime de la cuenta de QUIEN
    # PREGUNTA, no del que este corriendo. Servir el de otro no es un detalle:
    # es entregarle a alguien la llave para vincular un dispositivo a una
    # cuenta que no es suya.
    rt = runtime_de_mi_cuenta()
    if rt is None:
        return _error_code(
            "PAIRING_REQUIRED",
            "Todavia no tienes una cuenta de WhatsApp vinculada.",
            409,
        )
    return jsonify(qr_to_json(rt))


@api.get("/session/qr/image")
@requiere_sesion
def session_qr_image():
    """PNG del QR vigente. Nunca uno caducado."""
    import io

    from app.core.qr_render import render_qr

    # La imagen tambien: es el mismo secreto, dibujado.
    rt = runtime_de_mi_cuenta()
    if rt is None:
        return _error_code(
            "PAIRING_REQUIRED",
            "Todavia no tienes una cuenta de WhatsApp vinculada.",
            409,
        )
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


def _mi_runtime(*, crear: bool = False):
    """El runtime de MI cuenta de WhatsApp, o ``(None, respuesta de error)``.

    Es el resolutor que tiene que usar TODA ruta que toque una sesion de
    WhatsApp: emparejamiento, codigo QR, estado, sincronizacion, historial,
    multimedia, recuperacion y eventos.

    `runtime()` --el del proceso-- sirve para infraestructura: base de datos,
    ajustes, salud, y saber si este backend trae WhatsApp. Para cualquier otra
    cosa entrega el runtime de quien vinculara primero en la maquina, que con
    dos personas es el de otro.

    Quien no tiene cuenta recibe 409 y no el runtime de al lado: "no tienes
    vinculacion" es la respuesta correcta, "toma la de tu companero" no.
    """
    rt = runtime_de_mi_cuenta(crear=crear)
    if rt is None:
        return None, _error_code(
            "PAIRING_REQUIRED",
            "Todavia no tienes una cuenta de WhatsApp vinculada.",
            409,
        )
    return rt, None


# `_conflicto_de_sesion()` ESTUVO AQUI Y SE HA IDO.
#
# Preguntaba dos cosas --de quien es el runtime de este proceso, y quien tiene
# una cuenta marcada como vinculada en toda la base-- y con cualquiera de las
# dos negaba el emparejamiento. Las dos son preguntas de EQUIPO, no de
# persona, y por eso el segundo usuario nunca podia vincular: bastaba que
# alguien hubiera vinculado antes en la misma maquina.
#
# Un equipo puede sostener tantas vinculaciones como cuentas haya. Lo que no
# puede es dejar que una persona vea la de otra, y eso ya no lo sostiene un
# guarda: lo sostiene que cada ruta resuelva por membresia
# (`runtime_de_mi_cuenta`, `ownership.cuentas_de`). Un guarda se puede olvidar
# en una ruta nueva; la resolucion por membresia no, porque sin ella no hay
# de donde sacar los datos.


def _cuenta_visible(sesion) -> Any:
    """La cuenta cuyo contenido se enseña en ESTA peticion, o ``None``.

    ``?account_id=`` permite pedir otra de las del usuario; si no viene, se usa
    la suya. Lo que llega del navegador se COMPRUEBA contra sus cuentas: sin
    eso, cambiar un parametro de la URL leeria la copia de otra persona.
    """
    from flask import request

    yo = usuario_actual()
    if yo is None:
        return None
    return ownership.cuenta_visible_de(
        sesion, yo.id, request.args.get("account_id") or None
    )


def _acotado_a_la_cuenta(sesion) -> list:
    """La cuenta visible como LISTA, que es lo que esperan los repositorios.

    Una sola, nunca todas: acotar por usuario mezclaria en un mismo listado
    los chats de dos WhatsApp distintos. Con la lista vacia los repositorios
    aplican una condicion imposible, que es lo correcto para quien todavia no
    tiene cuenta.
    """
    cuenta = _cuenta_visible(sesion)
    return [cuenta] if cuenta is not None else []


def _identificadores_propios() -> "tuple[str | None, str | None] | None":
    """El (PN, LID) de la sesion vinculada, para marcar el chat propio.

    Nunca lanza: si no se puede resolver, los chats se sirven igual y
    simplemente ninguno queda marcado.
    """
    try:
        from app.core.identity import own_identity

        rt, _ = _mi_runtime()
        return own_identity(rt.settings) if rt is not None else None
    except Exception:  # noqa: BLE001 - marcar es un extra, no un requisito
        return None


def _restringidos_abiertos() -> bool:
    """Si esta peticion puede ver los chats restringidos.

    Nunca lanza: si el modulo del pestillo no esta disponible por lo que sea,
    la respuesta es que NO. Un fallo tiene que cerrar la puerta, no abrirla.
    """
    try:
        from app.api.chat_lock_routes import abierto_para_la_peticion

        return abierto_para_la_peticion()
    except Exception:  # noqa: BLE001 - ante la duda, cerrado
        return False


def _fuera_de_la_cuenta_en_contexto(sesion, chat_id: int) -> bool:
    """Si ese chat NO es de la cuenta de WhatsApp que se esta mirando.

    POR QUE NO BASTA CON `chat_es_de`
    ---------------------------------
    `chat_es_de` contesta "es suyo", mirando TODAS sus cuentas, y esta bien
    que lo haga: es una pregunta de propiedad. Pero lo que se puede VER es
    otra cosa -- es el WhatsApp que tiene abierto ahora mismo.

    Sin esta distincion, cambiar de cuenta no cambiaba lo que se estaba
    leyendo: la lista se vaciaba, pero el identificador del chat seguia en la
    URL y el servidor lo servia igual. El usuario cambiaba de cuenta y seguia
    viendo la conversacion de la otra, que es exactamente lo contrario de
    tener dos WhatsApp separados.

    Se responde 404, no 403: un 403 confirmaria que ese chat existe en otra
    de sus cuentas, y de paso deja al frontend un camino comun -- "aqui no
    esta" es lo mismo que ha de hacer con un identificador inventado.
    """
    from sqlalchemy import select

    from app.models import Chat

    cuentas = _acotado_a_la_cuenta(sesion)
    if not cuentas:
        return False
    dueno = sesion.execute(
        select(Chat.whatsapp_account_id).where(Chat.id == chat_id)
    ).scalar_one_or_none()
    if dueno is None:
        return False
    return str(dueno) not in {str(c) for c in cuentas}


def _restringido_y_cerrado(sesion, chat_id: int) -> bool:
    """Si este chat esta restringido y el pestillo no esta abierto.

    Ocultarlo del listado no basta: quien sepa el identificador puede pedir el
    chat o sus mensajes directamente. El pestillo tiene que valer en los dos
    sitios o no vale en ninguno.
    """
    from sqlalchemy import select

    from app.models import Chat

    if _restringidos_abiertos():
        return False
    return bool(
        sesion.execute(
            select(Chat.locked).where(Chat.id == chat_id)
        ).scalar_one_or_none()
    )


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
        cuentas = _acotado_a_la_cuenta(sesion)
        # Por defecto solo las conversaciones donde se ha escrito algo.
        #
        # `?todos=1` las trae todas, incluidas las que aun no se han excavado.
        # No se borra nada: una conversacion aparece sola en cuanto recibe su
        # primer mensaje real.
        todos = (request.args.get("todos") or "").lower() in ("1", "true", "si")
        # QUE SECCION se pide: la lista normal, los archivados o los
        # restringidos. Lo que no sea uno de los tres es la normal: un
        # parametro mal escrito en la URL no puede acabar ensenando lo que
        # estaba escondido.
        vista = (request.args.get("vista") or "normal").strip().lower()
        if vista not in ("normal", "archivados", "restringidos"):
            vista = "normal"
        # EL PESTILLO SE COMPRUEBA AQUI, no solo en la pantalla.
        #
        # Una seccion que se tapa unicamente en el frontend no esta tapada:
        # basta con escribir la URL a mano. Y responder 403 con la lista
        # dentro seria peor todavia.
        if vista == "restringidos" and not _restringidos_abiertos():
            return _error("los chats restringidos estan bloqueados", 423)
        resumenes = repo.list_chat_summaries(
            sesion,
            search=busqueda,
            limit=limite,
            accounts=cuentas,
            solo_con_mensajes=not todos,
            vista=vista,
        )
        # Cuantas quedaron fuera. Se dice: un filtro que esconde sin avisar es
        # indistinguible de una extraccion que no trajo nada.
        total = (
            len(resumenes)
            if todos
            else len(
                repo.list_chat_summaries(
                    sesion,
                    search=busqueda,
                    limit=limite,
                    accounts=cuentas,
                    vista=vista,
                )
            )
        )
        # Cuantos hay en las OTRAS secciones. Va siempre, tambien desde la
        # lista normal: es lo que permite pintar "Archivados 12" sin tener que
        # pedir la seccion entera solo para contar. Se cuenta con el mismo
        # filtro de contenido que la lista que se esta viendo, para que el
        # numero de la cabecera y el de la seccion no se contradigan.
        def _cuantos(seccion: str) -> int:
            return len(
                repo.list_chat_summaries(
                    sesion,
                    limit=limite,
                    accounts=cuentas,
                    solo_con_mensajes=not todos,
                    vista=seccion,
                )
            )

        secciones = {
            "archivados": _cuantos("archivados"),
            "restringidos": _cuantos("restringidos"),
        }
    finally:
        sesion.close()
    propios = _identificadores_propios()
    return jsonify(
        {
            "chats": [chat_to_json(c, propios) for c in resumenes],
            "count": len(resumenes),
            "total_conversaciones": total,
            "sin_mensajes": max(0, total - len(resumenes)),
            "vista": vista,
            "secciones": secciones,
        }
    )


@api.get("/chats/<int:chat_id>")
@requiere_drive
def chat_detail(chat_id: int):
    sesion = _session()
    if sesion is None:
        return _error("la base de datos no esta disponible", 503)
    try:
        if _no_es_mio(sesion, ownership.chat_es_de, chat_id):
            return _error("chat no encontrado", 404)
        if _fuera_de_la_cuenta_en_contexto(sesion, chat_id):
            return _error("chat no encontrado", 404)
        if _restringido_y_cerrado(sesion, chat_id):
            return _error("este chat esta restringido", 423)
        resumen = repo.chat_summary(sesion, chat_id)
        if resumen is None:
            return _error("chat no encontrado", 404)
        stats = repo.get_chat_stats(sesion, chat_id)
        estado_historico = repo.history_state_for(sesion, resumen.jid)
    finally:
        sesion.close()

    cuerpo = chat_to_json(resumen, _identificadores_propios())
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
        if _fuera_de_la_cuenta_en_contexto(sesion, chat_id):
            return _error("chat no encontrado", 404)
        if _restringido_y_cerrado(sesion, chat_id):
            return _error("este chat esta restringido", 423)
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
    # El runtime de MI cuenta. `runtime()` entrega el de quien vinculara
    # primero en esta maquina, que con dos personas es el de otro.
    rt, fallo = _mi_runtime()
    if fallo is not None:
        return fallo
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

    # El runtime de MI cuenta. `runtime()` entrega el de quien vinculara
    # primero en esta maquina, que con dos personas es el de otro.
    rt, fallo = _mi_runtime()
    if fallo is not None:
        return fallo
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
    # De LECTURA: sin cuenta propia no se responde 409 --el frontend sondea
    # esto y se quedaria sin saber que mostrar-- pero tampoco se cuenta la
    # sincronizacion de otro. Se dice que no hay nada que sincronizar, que es
    # la verdad para quien todavia no ha vinculado.
    rt = runtime_de_mi_cuenta()
    if rt is None:
        from app.api.serializers import estado_sin_vinculacion

        return jsonify(
            {"state": "IDLE", "session": estado_sin_vinculacion(runtime())}
        )

    # LA MISMA respuesta que da el SSE. Dos formas de construirla eran dos
    # verdades, y el frontend las mezclaba sin saberlo.
    cuerpo = estado_de_sync(rt)
    cuerpo["session"] = state_to_json(rt)

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
    # Las cifras son de MIS cuentas, no de las de la maquina. Sin acotar, el
    # panel de una persona ensenaba totales que incluian las conversaciones y
    # los adjuntos de otra.
    if rt.database is not None:
        try:
            sesion2 = rt.database.session()
            try:
                # De la cuenta VISIBLE, la misma que la lista. Con las
                # de todas, el panel decia 5000 mensajes mientras la lista
                # enseñaba los 800 de un solo WhatsApp.
                mias = _acotado_a_la_cuenta(sesion2)
                cuerpo["chats"] = repo.history_counters(sesion2, accounts=mias)
                media = repo.media_stats(sesion2, accounts=mias)
            finally:
                sesion2.close()
            cuerpo["media"] = media
            cuerpo["media_pending"] = int(media.get("pending", 0))
        except Exception:  # noqa: BLE001 - las metricas no pueden reventar
            log.debug("No se pudieron leer las metricas del panel")
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

    # El runtime de MI cuenta. `runtime()` entrega el de quien vinculara
    # primero en esta maquina, que con dos personas es el de otro.
    rt, fallo = _mi_runtime()
    if fallo is not None:
        return fallo
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

    # El runtime de MI cuenta. `runtime()` entrega el de quien vinculara
    # primero en esta maquina, que con dos personas es el de otro.
    rt, fallo = _mi_runtime()
    if fallo is not None:
        return fallo
    # La conexion principal manda, y se pregunta ENTERA. Que el estado diga
    # CONNECTED no basta: un emparejamiento a medias deja el objeto en pie sin
    # identidad ni Signal, y con eso no se puede excavar nada. Se evalua aqui,
    # antes que ninguna otra fase, porque si falta esto lo demas sobra.
    motivo_principal = razon_no_lista(rt)
    principal_listo = motivo_principal is None

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
            "counts": resumen,
            "queue": estado_cola,
        }
    )


def _fase_de_onboarding(principal_listo, resumen, cola, motivo_principal=None):
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
    # Aqui se miraba el estado del SEGUNDO dispositivo (`pairing_web`,
    # `waiting_web`) y el del vigilante de recuperacion. Los dos se retiraron
    # con sus proveedores: ahora solo hay una vinculacion que atender.
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
    """Revisa TODAS las conversaciones recuperables y vuelve a intentarlo.

    Es el mismo ciclo que "buscar novedades", con dos diferencias, y las dos
    responden al mismo fallo: una excavacion que se corta a medias deja
    conversaciones marcadas a las que ya no vuelve nadie.

    1. adelanta una vez la espera de reintento de las que la estaban
       cumpliendo, porque el usuario acaba de pedir que se intente AHORA;
    2. reabre las que se quedaron en ``timeout``, ``error`` o
       ``server_limited``: los tres describen un corte --nadie contesto, algo
       fallo, el servidor corto-- y ninguno dice nada sobre si queda historial.

    Lo que NO hace, y conviene decirlo porque la palabra "completo" invita a
    pensarlo: no borra mensajes, ni anclas, ni multimedia, ni nada de Drive;
    no vuelve a emparejar; no toca la sesion. Cada conversacion reabierta sigue
    desde su ancla, asi que solo llega lo que falta.

    Y tampoco reabre las que el telefono dio por terminadas (``exhausted``):
    eso no es un corte, es una respuesta, y volver a pedirla gasta una peticion
    para recibir cero mensajes.

    UNA SOLA A LA VEZ
    -----------------
    Si ya hay un ciclo corriendo se devuelve 409 con la instantanea del que
    corre, en vez de lanzar otro. Es lo que sostiene que el boton "solo
    funcione una vez hasta que se acabe": el frontend lo tapa, y esto lo
    garantiza aunque el frontend falle.
    """
    from app.services.sync_job import SyncAlreadyRunningError, SyncUnavailableError

    # El runtime de MI cuenta. `runtime()` entrega el de quien vinculara
    # primero en esta maquina, que con dos personas es el de otro.
    rt, fallo = _mi_runtime()
    if fallo is not None:
        return fallo
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

    # El runtime de MI cuenta. `runtime()` entrega el de quien vinculara
    # primero en esta maquina, que con dos personas es el de otro.
    rt, fallo = _mi_runtime()
    if fallo is not None:
        return fallo
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
            # Por `chat_id`, que es lo que la ruta ya resolvio: el jid deja
            # de ser unico en cuanto dos cuentas comparten un contacto.
            select(ChatHistoryState).where(ChatHistoryState.chat_id == chat_id)
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
            .where(ChatHistoryState.chat_id == chat_id)
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
    # La cuenta recien vinculada pasa a ser la activa. El selector cambia sin
    # recargar; sin esto, se escaneaba el QR del segundo telefono y el panel
    # seguia enseñando el primero.
    "account.activated": "account.activated",
    # El nombre de la cuenta llega despues del sellado --WhatsApp manda
    # `me.name` un instante despues del pair-success-- asi que el selector
    # tiene que poder enterarse sin recargar.
    "account.updated": "account.updated",
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
    # Y el detalle POR CONVERSACION, para la vista del chat abierto. La lista
    # se conforma con `chat.status`; la conversacion que el usuario tiene
    # delante necesita saber si esta recuperando, si espera referencia, si ya
    # termino, y cuantos mensajes van entrando.
    "history_chat_started": "history.chat.started",
    "history_chat_progress": "history.chat.progress",
    "history_chat_retrying": "history.chat.retrying",
    "history_chat_waiting_seed": "history.chat.waiting_seed",
    "history_chat_completed": "history.chat.completed",
    "history_chat_error": "history.chat.error",
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
    # Que cuenta se esta mirando es cosa de su dueno, no de un espectador.
    "account.",
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


def _latidos():
    """Solo el paso del tiempo, para quien no tiene bus que escuchar.

    Imita la forma de ``EventBus.stream``: entrega ``None`` cada segundo, que
    es lo que el bucle interpreta como "no ha pasado nada". Asi el codigo del
    stream es el mismo con bus y sin el.
    """
    while True:
        time.sleep(1.0)
        yield None


def estado_de_sync(rt: Any) -> dict[str, Any]:
    """Como va la sincronizacion, con el ciclo manual incluido.

    POR QUE EXISTE
    --------------
    Habia DOS respuestas distintas a la misma pregunta. ``/sync/status``
    mezclaba ``sync_to_json`` con la instantanea del ciclo manual; el SSE
    mandaba solo ``sync_to_json``. Y ``sync_to_json`` no sabe nada del ciclo:
    no lleva ``state``, ni ``mode``, ni ``phase``, ni cuantas conversaciones
    van.

    El frontend trata ``sync.status`` como el estado COMPLETO --lo sustituye
    entero-- asi que cada aviso por SSE borraba lo que sabia del ciclo en
    marcha: el cartel de "espera, no recargues" se apagaba solo a mitad de una
    excavacion y el boton volvia a aparecer. La instantanea del ciclo llegaba
    a publicarse (``_emitir`` la manda) y aqui se tiraba para reconstruirla
    sin ella.

    Ahora las dos vias dicen lo mismo, que es lo unico que puede sostener un
    estado que sobreviva a un F5.
    """
    cuerpo = dict(sync_to_json(rt))
    trabajo = getattr(rt, "sync_job", None)
    if trabajo is not None:
        # El ciclo manual manda sobre las claves que comparten: es lo que el
        # usuario acaba de pedir y de lo que espera ver progreso.
        cuerpo.update(trabajo.snapshot())
    else:
        cuerpo.setdefault("state", "idle")
        cuerpo.setdefault("job_id", None)
    return cuerpo


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
        return [(nombre, estado_de_sync(rt))]

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

    CADA CUENTA, SU BUS
    -------------------
    Se escucha el bus del runtime de **la cuenta de quien se conecta**, que
    resuelve por membresia. Antes se escuchaba el bus del runtime base --uno
    para todo el proceso-- y el aislamiento dependia de un filtro que tapaba
    los eventos ajenos uno a uno.

    Tapar no es aislar: basta que un evento nuevo no entre en la lista de los
    que se filtran para que empiece a llegarle a quien no debe. Escuchando el
    bus correcto, los eventos de otra cuenta **no existen** en esta conexion.

    El filtro por dueno se conserva como segunda barrera. Con el bus ya
    separado no deberia hacer falta, y precisamente por eso se queda: si
    alguna via publicara en el bus equivocado, esto lo para igual.

    Quien todavia no tiene cuenta no tiene runtime, y recibe solo latidos: no
    hay nada suyo que contar, y contarle lo de otro es justo lo que no puede
    pasar.
    """
    base = runtime()
    yo = usuario_actual()
    mio = runtime_de_mi_cuenta()
    rt = mio or base
    # Sin cuenta propia no se escucha ningun bus ajeno.
    sin_cuenta = mio is None
    # Y ser dueno es, exactamente, tener runtime propio. Preguntarselo al
    # runtime del proceso --`es_mia_la_sesion`-- daba verdadero para quien
    # simplemente vinculo primero en esta maquina.
    es_dueno = yo is not None and not sin_cuenta

    def generar():
        # Estado completo de entrada: quien se conecta a mitad tiene que saber
        # donde esta, no esperar al siguiente cambio.
        #
        # Sin cuenta propia, el estado NEUTRO. Antes se mandaba
        # `state_to_json(base)` -- el de quien vinculara primero en la maquina
        # -- y era lo primero que recibia un navegador recien conectado.
        from app.api.serializers import estado_sin_vinculacion

        yield _sse(
            "session.state",
            estado_sin_vinculacion(base) if sin_cuenta else state_to_json(rt),
        )
        if es_dueno and not sin_cuenta:
            yield _sse("sync.status", estado_de_sync(rt))
            if rt.pairing is not None and rt.pairing.available:
                yield _sse("session.qr", qr_to_json(rt))

        ultimo_latido = time.monotonic()
        # SIN CUENTA NO SE ENGANCHA A NINGUN BUS.
        #
        # Antes se escuchaba `rt.bus` --que sin cuenta propia era el del
        # runtime base, o sea el de quien hubiera vinculado primero-- y el
        # aislamiento lo sostenia el filtro por nombre de evento de mas abajo.
        # Eso es tapar, no aislar: basta un evento nuevo que no empiece por uno
        # de los prefijos conocidos para que empiece a llegarle a quien no
        # debe, y nadie se entera hasta que pasa.
        #
        # Quien no tiene vinculacion no tiene nada que escuchar. Se le manda el
        # latido, que es lo que mantiene viva la conexion y le dice al frontend
        # que el backend responde.
        bus = None if sin_cuenta else rt.bus
        # CON REENVIO DE LO RECIENTE, y esto es lo que arregla el F5.
        #
        # El bus guarda los ultimos eventos justo para esto, y nadie se los
        # pedia. Se midio: la extraccion inicial entrega las 42 conversaciones
        # cuatro segundos despues de vincular, y el navegador todavia esta
        # montando el panel. Cuando su EventSource por fin se conecta, esos
        # avisos ya se tiraron -- el panel se quedaba con lo que hubiera
        # cargado y no se movia hasta recargar a mano.
        #
        # Reenviarlos es seguro: la pantalla mete cada conversacion por id
        # (`upsertChat`), asi que ver dos veces la misma no la duplica.
        for evento in _latidos() if bus is None else bus.stream(
            timeout=1.0, replay=True
        ):
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
                            # Sin cuenta no se cuenta el estado de otro runtime,
                            # ni siquiera en el latido.
                            "session_state": (
                                "PAIRING_REQUIRED"
                                if sin_cuenta
                                else rt.state.state.value
                            ),
                            "sync_state": (
                                "IDLE"
                                if sin_cuenta
                                else getattr(rt, "sync_state", "IDLE")
                            ),
                        },
                    )
                continue

            for nombre, datos in eventos_para(evento, rt):
                # Segunda barrera. El bus ya es el de esta cuenta, asi que
                # esto no deberia descartar nada -- y por eso se queda: si
                # alguna via publicara en el bus equivocado, aqui se para.
                if (not es_dueno or sin_cuenta) and _es_de_la_sesion(nombre):
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


def _recheck_pendientes(rt):
    """El servicio de revision de ESE runtime, creado una sola vez.

    Uno por CUENTA, no por proceso: publica en `rt.bus` y revisa las
    conversaciones de esa cuenta.
    """
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
    # El runtime de MI cuenta. `runtime()` entrega el de quien vinculara
    # primero en esta maquina, que con dos personas es el de otro.
    rt, fallo = _mi_runtime()
    if fallo is not None:
        return fallo
    if rt.database is None:
        return _error("la base de datos no esta disponible", 503)

    auto = request.args.get("auto", "").lower() in ("1", "true", "yes")
    servicio = _recheck_pendientes(rt)
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
    rt, fallo = _mi_runtime()
    if fallo is not None:
        return fallo
    trabajo = _recheck_pendientes(rt).get(job_id)
    if trabajo is None:
        return _error("trabajo no encontrado", 404)
    return jsonify(trabajo.to_json())


@api.post("/chats/<int:chat_id>/history/priority")
@requiere_drive
def chat_history_priority(chat_id: int):
    """El usuario abrio este chat: pasa al principio de la cola.

    POR QUE HACE FALTA
    ------------------
    La excavacion atendia las conversaciones por actividad, de la mas nueva a
    la mas vieja. Abrir una conversacion no cambiaba nada: si estaba en la
    posicion treinta, le tocaba en la posicion treinta, y cada puesto puede
    costar hasta 45 segundos. El usuario abria un chat y no pasaba nada.

    QUE HACE, Y QUE NO
    ------------------
    Sube la prioridad y despierta al motor. **No** encola una peticion nueva:
    de que no haya dos peticiones a la vez de la misma conversacion se encarga
    la guardia por chat del motor, asi que pulsar diez veces sube la prioridad
    una vez y no produce diez peticiones.

    Tampoco pide nada al servidor por si mismo. Un chat sin ancla sigue sin
    poder excavarse por mucha prioridad que tenga: se queda esperando
    referencia, y se dice tal cual en la respuesta.
    """
    # El runtime de MI cuenta. `runtime()` entrega el de quien vinculara
    # primero en esta maquina, que con dos personas es el de otro.
    rt, fallo = _mi_runtime()
    if fallo is not None:
        return fallo

    sesion = _session()
    if sesion is None:
        return _error("la base de datos no esta disponible", 503)
    try:
        if _no_es_mio(sesion, ownership.chat_es_de, chat_id):
            return _error("chat no encontrado", 404)
        from app.models import Chat, ChatHistoryState

        fila = sesion.get(Chat, chat_id)
        if fila is None:
            return _error("chat no encontrado", 404)
        chat_jid = fila.jid
        estado = (
            sesion.query(ChatHistoryState)
            .filter(ChatHistoryState.chat_id == chat_id)
            .one_or_none()
        )
        situacion = estado.history_status if estado is not None else None
    finally:
        sesion.close()

    backfill = getattr(rt, "backfill", None)
    planificador = getattr(backfill, "scheduler", None)
    if planificador is None:
        return jsonify(
            {
                "chat_id": chat_id,
                "prioritized": False,
                "reason": "SCHEDULER_UNAVAILABLE",
                "state": situacion,
            }
        )

    planificador.marcar_interactiva(chat_jid)

    # Y se le da un empujon por la MISMA cola que usa todo lo demas, en vez de
    # inventar un camino nuevo: encolar es lo que despierta al motor cuando
    # aparece un ancla, y sirve igual cuando lo que aparece es un usuario
    # mirando. `enqueue` no bloquea, no espera y no lanza.
    despertado = False
    cola = getattr(rt, "seed_queue", None)
    encolar = getattr(cola, "enqueue", None)
    if callable(encolar) and situacion != "waiting_seed":
        try:
            despertado = bool(encolar([chat_jid]))
        except Exception:  # noqa: BLE001 - priorizar no puede tumbar la peticion
            log.debug("No se pudo encolar el chat priorizado")

    return jsonify(
        {
            "chat_id": chat_id,
            "prioritized": True,
            "woken": despertado,
            "state": situacion,
            # Sin ancla la prioridad no sirve de nada todavia, y el frontend
            # tiene que poder decirlo en vez de girar un spinner para siempre.
            "waiting_seed": situacion == "waiting_seed",
        }
    )
