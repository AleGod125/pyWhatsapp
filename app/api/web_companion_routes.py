"""Panel de diagnostico del Web Companion. Solo lectura.

QUE EXPONE
----------
Tres cosas, y ninguna cambia nada:

``GET  /web-companion/status``       en que estado esta, y su QR si lo pide
``POST /web-companion/inventory``    que ve Web frente a lo que tiene Python
``POST /web-companion/probe``        de las que esperan, cuantas tienen ancla
``POST /web-companion/seeds/apply``  ESCRIBE: convierte esas anclas en cursor
``POST /web-companion/seeds/resume`` continua una recuperacion pausada

EL QR ES OTRO
-------------
El Web Companion es un dispositivo vinculado DISTINTO, con su propia sesion.
Su QR no sustituye al emparejamiento principal y no debe confundirse con el:
la respuesta lo marca como experimental para que el frontend no pueda
presentarlo como el de siempre.

UNA SOLA RUTA ESCRIBE
---------------------
``seeds/apply``, y solo cuando alguien la llama a proposito. El sondeo sigue
siendo de solo lectura y lo dice en su respuesta: una medicion no puede acabar
mutando la base porque se pulse el boton equivocado.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request, send_file

from app.core.logging_setup import get_logger

log = get_logger("WEB")

web_companion = Blueprint("web_companion", __name__)


def _runtime() -> Any:
    from app.api.routes import runtime

    return runtime()


def _supervisor() -> Any:
    return getattr(_runtime(), "web_companion", None)


def _error(code: str, message: str, status: int = 409, **extra: Any):
    return jsonify({"error": {"code": code, "message": message}, **extra}), status


def _principal_lista():
    """``None`` si la conexion principal esta lista; si no, la respuesta.

    La sesion principal manda. Sin ella, este dispositivo no arranca, no
    publica codigo y no sondea: lo unico que conseguiria es que el usuario
    escanee el codigo equivocado mientras el que hace falta es el otro.
    """
    from app.core.primary import mensaje_para_el_usuario, razon_no_lista

    motivo = razon_no_lista(_runtime())
    if motivo is None:
        return None
    return _error(
        "PRIMARY_NOT_READY",
        mensaje_para_el_usuario(motivo),
        409,
        primary_reason=motivo,
    )


def _sondeador() -> Any:
    from app.web_companion.probe import WebCompanionProbe

    rt = _runtime()
    return WebCompanionProbe(rt.database, rt.web_companion)


@web_companion.get("/web-companion/status")
def estado():
    """En que estado esta el companion. Nunca bloquea."""
    supervisor = _supervisor()
    if supervisor is None:
        return jsonify(
            {
                "enabled": False,
                "state": "disabled",
                "experimental": True,
                "reason": "el backend no tiene Web Companion",
            }
        )

    cuerpo = supervisor.snapshot()
    disponible, motivo = supervisor.comprobar_entorno()
    cuerpo["can_start"] = disponible
    cuerpo["reason"] = motivo
    # Marcado SIEMPRE: su QR no es el del emparejamiento principal.
    cuerpo["experimental"] = True

    # La conexion principal manda. Si no esta lista, este dispositivo queda
    # suspendido: no se destruye nada -- si ya estaba vinculado lo sigue
    # estando -- pero deja de anunciar un codigo que no toca escanear.
    from app.core.primary import razon_no_lista

    razon = razon_no_lista(_runtime())
    cuerpo["blocked_by_primary"] = razon is not None
    cuerpo["primary_reason"] = razon
    if razon is not None:
        cuerpo["can_start"] = False
        cuerpo["qr_available"] = False
    return jsonify(cuerpo)


@web_companion.get("/web-companion/qr/image")
def qr_imagen():
    """PNG del QR vigente del Web Companion.

    Se pinta AQUI, no en el navegador. Dos motivos:

    * el proyecto ya lo hace asi para el emparejamiento principal, con el
      mismo renderizador — dos formas de dibujar el mismo tipo de codigo se
      acaban desincronizando;
    * el payload es material de vinculacion, y mandarlo en un JSON lo deja a
      la vista de cualquiera que mire la respuesta.

    Es el QR del COMPANION, no el de pywhats. Son sesiones distintas.
    """
    import io

    from app.core.qr_render import render_qr

    supervisor = _supervisor()
    if supervisor is None or not supervisor.habilitado:
        return _error("WEB_COMPANION_DISABLED", "El Web Companion esta apagado.")

    # Ni se genera ni se entrega uno anterior. Un codigo guardado de antes de
    # que cayera la sesion principal es justamente lo que hace que el usuario
    # siga intentando vincular el dispositivo que no toca.
    bloqueo = _principal_lista()
    if bloqueo is not None:
        return bloqueo

    payload, generacion = supervisor.qr_payload()
    if not payload:
        # 404 y no una imagen vacia: "todavia no hay" es una respuesta, y el
        # panel tiene que poder distinguirla de un codigo que no se ve.
        return _error(
            "WEB_COMPANION_QR_NOT_AVAILABLE",
            "El Web Companion no tiene ningun codigo QR vigente.",
            404,
            state=supervisor.snapshot().get("state"),
        )

    tamano = request.args.get("size", type=int) or 456
    imagen = render_qr(payload, max_pixels=max(120, min(1024, tamano)))
    buffer = io.BytesIO()
    imagen.save(buffer, format="PNG")
    buffer.seek(0)
    respuesta = send_file(buffer, mimetype="image/png")
    # El QR rota: cachearlo mostraria uno ya muerto.
    respuesta.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    respuesta.headers["Pragma"] = "no-cache"
    respuesta.headers["X-QR-Generation"] = str(generacion)
    return respuesta


@web_companion.post("/web-companion/start")
def arrancar():
    """Levanta el worker a peticion. Idempotente."""
    supervisor = _supervisor()
    if supervisor is None:
        return _error("WEB_COMPANION_DISABLED", "El Web Companion no esta disponible.")
    disponible, motivo = supervisor.comprobar_entorno()
    if not disponible:
        return _error("WEB_COMPANION_UNAVAILABLE", motivo)
    bloqueo = _principal_lista()
    if bloqueo is not None:
        return bloqueo
    supervisor.start()
    return jsonify(supervisor.snapshot()), 202


@web_companion.post("/web-companion/stop")
def parar():
    supervisor = _supervisor()
    if supervisor is None:
        return _error("WEB_COMPANION_DISABLED", "El Web Companion no esta disponible.")
    supervisor.stop()
    return jsonify(supervisor.snapshot())


@web_companion.post("/web-companion/inventory")
def inventario():
    """Que conversaciones ve WhatsApp Web frente a las que ya tiene Python."""
    from app.web_companion.supervisor import WebCompanionNoDisponible

    supervisor = _supervisor()
    if supervisor is None or not supervisor.habilitado:
        return _error("WEB_COMPANION_DISABLED", "El Web Companion esta apagado.")

    bloqueo = _principal_lista()
    if bloqueo is not None:
        return bloqueo

    cuenta = getattr(_runtime(), "runtime_owner_account_id", None)
    try:
        return jsonify(_sondeador().inventario(cuenta))
    except WebCompanionNoDisponible as exc:
        return _error(exc.code, str(exc), 409, state=supervisor.snapshot().get("state"))


@web_companion.post("/web-companion/probe")
def sondear():
    """De las conversaciones que esperan, ¿cuantas tienen referencia real?

    SOLO MIDE. La respuesta lo dice explicitamente (``read_only``,
    ``mutations``, ``on_demand_requests``) para que nadie tenga que fiarse.
    """
    from app.web_companion.supervisor import WebCompanionNoDisponible

    # La principal, primero. Contestar "el segundo dispositivo no esta en
    # marcha" cuando lo que falta es el emparejamiento principal manda al
    # usuario a arreglar lo que no toca.
    bloqueo = _principal_lista()
    if bloqueo is not None:
        return bloqueo

    supervisor = _supervisor()
    if supervisor is None or not supervisor.habilitado:
        return _error("WEB_COMPANION_DISABLED", "El Web Companion esta apagado.")
    # Se comprueba ANTES de mirar la base: "apagado" es una respuesta util, y
    # devolver un resultado vacio haria pensar que se midio algo.
    if not supervisor.vivo:
        return _error(
            "WEB_COMPANION_NOT_RUNNING",
            "El Web Companion no esta en marcha.",
            409,
            state=supervisor.snapshot().get("state"),
        )

    cuenta = getattr(_runtime(), "runtime_owner_account_id", None)
    espera = float(request.json.get("timeout", 300.0)) if request.is_json else 300.0
    try:
        return jsonify(_sondeador().sondear(cuenta, timeout=espera))
    except WebCompanionNoDisponible as exc:
        return _error(exc.code, str(exc), 409, state=supervisor.snapshot().get("state"))


@web_companion.post("/web-companion/seeds/apply")
def aplicar_semillas():
    """Convierte las referencias de WhatsApp Web en anclas reales.

    ESTA RUTA SI ESCRIBE. Vuelve a medir primero -- aplicar sobre una foto de
    hace un rato escribiria anclas de un estado que ya no existe --, guarda
    las que pasen la validacion de siempre, promueve a ``pending`` las
    conversaciones que asi consigan cursor y encola esas para excavar.

    Se niega si ON_DEMAND no esta confirmado en esta sesion: promover 22
    conversaciones cuando el motor no responde solo produce 22 esperas
    agotadas, y despues no hay forma de saber si el ancla era mala o si el
    telefono estaba dormido.
    """
    from app.web_companion.apply import AplicacionRechazada, WebSeedApplier
    from app.web_companion.supervisor import WebCompanionNoDisponible

    bloqueo = _principal_lista()
    if bloqueo is not None:
        return bloqueo

    rt = _runtime()
    espera = float(request.json.get("timeout", 300.0)) if request.is_json else 300.0
    aplicador = WebSeedApplier(rt)

    bus = getattr(rt, "bus", None)
    if bus is not None:
        bus.publish("web_seed_apply_started", {})
    try:
        resultado = aplicador.aplicar(timeout=espera).to_json()
    except AplicacionRechazada as exc:
        return _error(exc.code, str(exc), 409, **exc.extra)
    except WebCompanionNoDisponible as exc:
        return _error(exc.code, str(exc), 409)

    cola = getattr(rt, "seed_queue", None)
    resultado["queue"] = cola.estado() if cola is not None else None
    if bus is not None:
        bus.publish("web_seed_apply_complete", resultado)
    return jsonify(resultado)


@web_companion.post("/web-companion/seeds/resume")
def reanudar_recuperacion():
    """Continua una recuperacion que se paro porque el telefono no respondia.

    No pide nada nuevo ni cambia ningun estado: solo levanta la pausa, y solo
    si la sesion esta viva y ON_DEMAND sigue confirmado. Los chats que
    quedaban siguen en la cola desde que se paro.
    """
    cola = getattr(_runtime(), "seed_queue", None)
    if cola is None:
        return _error("QUEUE_NOT_READY", "Todavia no hay cola de recuperacion.")

    if not cola.reanudar():
        return _error(
            "PHONE_STILL_UNAVAILABLE",
            (
                "El telefono sigue sin responder. Abre WhatsApp en el telefono "
                "y mantenlo conectado antes de continuar."
            ),
            409,
            queue=cola.estado(),
        )
    return jsonify({"resumed": True, "queue": cola.estado()})


@web_companion.post("/web-companion/inventory/preview")
def previsualizar_indice():
    """El indice tal cual lo ve WhatsApp Web. NO reconcilia, NO escribe.

    Es el mismo comando que usa la ruta de refresco, pero sin la parte que
    muta: no crea conversaciones, no escribe anclas y no encola nada. Existe
    para poder medir la cobertura antes de aplicarla.
    """
    # La principal, primero: lo mismo que en el sondeo.
    bloqueo = _principal_lista()
    if bloqueo is not None:
        return bloqueo

    supervisor = _supervisor()
    if supervisor is None or not supervisor.habilitado:
        return _error("WEB_COMPANION_DISABLED", "El Web Companion esta apagado.")
    if not supervisor.vivo:
        return _error(
            "WEB_COMPANION_NOT_RUNNING",
            "El Web Companion no esta en marcha.",
            409,
            state=supervisor.snapshot().get("state"),
        )
    if not supervisor.snapshot().get("web_client_ready"):
        return _error(
            "WEB_COMPANION_NOT_READY",
            "El Web Companion todavia no ha terminado de conectar.",
        )

    from app.web_companion.inventory import WebInventoryService

    espera = float(request.json.get("timeout", 300.0)) if request.is_json else 300.0
    # El MISMO reparto que usa el indice de verdad. Medir con otras condiciones
    # daria un numero que no describe lo que va a pasar al aplicarlo: las
    # prioridades y las omisiones cambian a que conversaciones se les pide.
    reparto = WebInventoryService(_runtime())._reparto()
    respuesta = supervisor.enviar(
        {
            "cmd": "web_inventory",
            "priority_chat_jids": reparto["prioritarios"],
            "skip_chat_jids": reparto["omitir"],
            "known_chat_jids": reparto["conocidos"],
        },
        timeout=espera,
    )
    if respuesta.get("error"):
        return _error("WEB_INVENTORY_FAILED", str(respuesta.get("error")))
    return jsonify(respuesta)


@web_companion.post("/web-companion/inventory/refresh")
def refrescar_indice():
    """Pide el indice completo a WhatsApp Web y lo reconcilia. ESCRIBE.

    Es el reparto nuevo: Web dice QUE conversaciones existen y cual es el
    ultimo mensaje real de cada una; esta ruta las reconcilia contra la base,
    crea las que faltaban y entrega las anclas al recolector de siempre.

    No pide historial. Cuando una conversacion consigue ancla entra en la cola
    de siempre y el motor de siempre la excava.
    """
    from app.web_companion.inventory import (
        InventarioNoDisponible,
        WebInventoryService,
    )

    bloqueo = _principal_lista()
    if bloqueo is not None:
        return bloqueo

    rt = _runtime()
    espera = float(request.json.get("timeout", 300.0)) if request.is_json else 300.0
    try:
        resultado = WebInventoryService(rt).refrescar(timeout=espera).to_json()
    except InventarioNoDisponible as exc:
        return _error(exc.code, str(exc), 409)

    bus = getattr(rt, "bus", None)
    if bus is not None:
        bus.publish("web_inventory_done", resultado)
    return jsonify(resultado)


@web_companion.post("/web-companion/j31/snapshot")
def instantanea_j31():
    """Plan J3.1: la foto del almacen del navegador. LEE, no pide nada.

    Es la ruta que permite medir al navegador con la MISMA vara que a la
    sesion principal: mismas metricas, misma clasificacion de conversaciones,
    mismo criterio de ancla valida.

    A diferencia de `inventory/preview`, esta no llama a `fetchMessages` ni a
    `loadEarlierMsgs`. Esa distincion es el centro de la fase: si le pedimos
    cosas al navegador mientras medimos que trae por si solo, la cifra deja de
    responder a la pregunta.

    `mode` distingue las dos mediciones que no se pueden mezclar:
    `native_store` es lo que tiene el navegador solo, y `after_probes` lo que
    tiene despues de que nuestro sondeo le haya pedido.
    """
    import time as _time

    bloqueo = _principal_lista()
    if bloqueo is not None:
        return bloqueo

    supervisor = _supervisor()
    if supervisor is None or not supervisor.habilitado:
        return _error("WEB_COMPANION_DISABLED", "El Web Companion esta apagado.")
    if not supervisor.vivo:
        return _error(
            "WEB_COMPANION_NOT_RUNNING",
            "El Web Companion no esta en marcha.",
            409,
            state=supervisor.snapshot().get("state"),
        )
    if not supervisor.snapshot().get("store_ready"):
        return _error(
            "WEB_COMPANION_NOT_READY",
            "El almacen del navegador todavia no esta listo.",
        )

    cuerpo = request.json if request.is_json else {}
    modo = str(cuerpo.get("mode") or "native_store")
    etiqueta = str(cuerpo.get("label") or "manual")
    t0 = float(cuerpo.get("t0") or _time.time())

    respuesta = supervisor.enviar({"cmd": "j31_store_snapshot"}, timeout=60.0)
    if respuesta.get("error"):
        return _error("J31_SNAPSHOT_FAILED", str(respuesta.get("error")))

    from app.discovery.symmetric_snapshot import fotografiar_navegador

    foto = fotografiar_navegador(respuesta, t0=t0, etiqueta=etiqueta, modo=modo)
    return jsonify(foto.to_json())
