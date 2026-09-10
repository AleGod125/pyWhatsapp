"""Las cuentas de WhatsApp de un usuario: listar, anadir, renombrar, cambiar.

POR QUE VARIAS
--------------
Una persona puede tener el WhatsApp personal y el del trabajo, o llevar el de
otra persona. Son cosas separadas: sus chats, su historial, su sesion y su
copia de seguridad no tienen nada que ver, y verlos juntos en una sola lista
no es util -- es confuso, porque nada en la fila dice de cual es.

Hasta ahora el techo era una por usuario, sostenido por ``UNIQUE(user_id)`` en
la membresia. La restriccion se retiro y la sustituye otra: **como mucho una
ACTIVA**, garantizada por un indice unico parcial. Que solo haya una activa es
lo que permite que el resto del backend siga preguntando "la cuenta de este
usuario" y reciba siempre la misma.

LO QUE NO SE FIA DEL NAVEGADOR
------------------------------
El identificador de cuenta llega del cliente en cada peticion, y aqui se
COMPRUEBA contra las membresias antes de tocar nada. Sin eso, cambiar un
parametro de la URL leeria la copia de seguridad de otra persona.

Cuando el id es ajeno se responde 404, no 403: un 403 confirmaria que esa
cuenta existe, y con eso se pueden ir tanteando identificadores.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

from app.auth.web import requiere_sesion, usuario_actual
from app.core.logging_setup import get_logger

log = get_logger("API")

accounts = Blueprint("accounts", __name__)


def _runtime_base() -> Any:
    from app.api.routes import runtime

    return runtime()


def _database() -> Any:
    rt = _runtime_base()
    return getattr(rt, "database", None)


def _cuentas_service() -> Any:
    return getattr(_runtime_base(), "whatsapp_accounts", None)


def _error(codigo: str, mensaje: str, http: int = 400):
    return jsonify({"error": {"code": codigo, "message": mensaje}}), http


def cuenta_a_json(fila: Any, *, activa: bool) -> dict[str, Any]:
    """Una cuenta, para el selector.

    NUNCA lleva material de sesion: ni claves, ni el ``session_storage_key``,
    ni nada con lo que se pudiera localizar la carpeta de credenciales. El
    numero si, porque es lo que distingue dos cuentas de la misma persona
    cuando ninguna tiene nombre.
    """
    numero = fila.phone_number or (fila.wa_pn or "").split("@")[0] or None
    return {
        "id": str(fila.id),
        # El nombre que el usuario le puso; si no hay, el del perfil de
        # WhatsApp; si tampoco, el numero. La pantalla no tiene que decidir.
        "display_name": fila.display_name or (f"+{numero}" if numero else None),
        "phone_number": numero,
        "account_type": fila.account_type or "unknown",
        "avatar_url": fila.avatar_url,
        "session_status": fila.session_status,
        "linked": fila.session_status == "linked",
        # TRES situaciones distintas, no dos:
        #
        #   linked        conectada y funcionando
        #   disconnected  el socket se cayo -- vuelve solo, no hay que hacer nada
        #   revoked       el telefono la desvinculo -- hay que escanear otra vez
        #
        # Juntar las dos ultimas en "sin vincular" hacia que un corte de red
        # pareciera una desvinculacion, y que una desvinculacion de verdad
        # pareciera un corte de red. Ninguna de las dos ayuda a decidir que
        # hacer.
        "needs_relink": fila.session_status in ("revoked", "never_linked"),
        "disconnected": fila.session_status == "disconnected",
        "linked_at": fila.linked_at.isoformat() if fila.linked_at else None,
        "last_connected_at": (
            fila.last_connected_at.isoformat() if fila.last_connected_at else None
        ),
        "active": activa,
    }


@accounts.get("/accounts")
@requiere_sesion
def listar_cuentas():
    """Las cuentas de quien pregunta, y cual esta mirando.

    Solo salen las que TIENEN ALGO QUE ENSENAR. Ver
    :func:`_tiene_algo_que_ensenar` para lo que eso significa y por que no es
    lo mismo que "esta vinculada".
    """
    database = _database()
    if database is None:
        return _error("DB_UNAVAILABLE", "la base de datos no esta disponible", 503)

    from app.auth.memberships import activar_cuenta, asegurar_activa, cuentas_de_usuario

    yo = usuario_actual()
    with database.transaction() as sesion:
        todas = cuentas_de_usuario(sesion, yo.id)
        con_chats = _cuantos_chats(sesion, [f.id for f in todas])
        filas = [f for f in todas if _tiene_algo_que_ensenar(f, con_chats)]

        # `asegurar_activa` MARCA una si no habia ninguna, y por eso esto va
        # en una transaccion. Devolver "la primera" sin marcarla dejaba que
        # este listado y la lista de chats eligieran cuentas distintas en la
        # misma peticion.
        activa = asegurar_activa(sesion, yo.id)

        # LA ACTIVA TIENE QUE SER UNA DE LAS QUE SE VEN.
        #
        # Se midio en la instalacion del usuario: la activa era una cuenta
        # revocada y sin un solo chat. El selector no podia marcar ninguna
        # --esa no aparecia-- y el panel pedia los chats de una cuenta vacia,
        # asi que la lista salia en blanco con 327 conversaciones guardadas al
        # lado. Dejar activa una cuenta que no se puede elegir no deja al
        # usuario forma de salir de ahi.
        # LA ELECCION DE LA PERSONA MANDA, Y NO SE TOCA.
        #
        # Aqui habia un cambio automatico: si la activa no aparecia en el
        # listado, se pasaba a la primera. La intencion era no dejar al
        # usuario con el selector sin marcar y una lista en blanco.
        #
        # El efecto real fue peor. Se vinculaba el segundo telefono, se
        # elegia, y setenta segundos despues el sistema volvia solo al
        # primero --porque la recien vinculada aun no tenia ni un chat y por
        # eso "no era presentable"--. Y lo hacia EN SILENCIO: el usuario creia
        # seguir mirando WA2 mientras veia WA1, que es exactamente como
        # parecio durante horas que los chats estaban mezclados.
        #
        # Ahora:
        #
        # * si la eligio una persona, se respeta SIEMPRE. Aunque este vacia,
        #   aunque acabe de nacer. Solo deja de valer si la cuenta desaparece;
        # * si la puso el sistema y ya no sirve, se sustituye y SE DICE, para
        #   que el frontend pueda avisar en vez de cambiar por debajo.
        visibles = {str(f.id) for f in filas}
        cambio_automatico = None
        if activa is not None and str(activa.id) not in visibles:
            if _cerrada_definitivamente(activa):
                # REVOCADA O EN ERROR: aqui si se cambia, aunque la hubiera
                # elegido una persona. No es que "parezca" inservible: es que
                # WhatsApp cerro esa vinculacion. Dejar al usuario mirandola
                # seria dejarlo sin salida.
                if filas:
                    activar_cuenta(sesion, user_id=yo.id, account_id=filas[0].id)
                    cambio_automatico = {
                        "desde": str(activa.id),
                        "hasta": str(filas[0].id),
                        "motivo": activa.session_status,
                    }
                    log.info(
                        "[API] la cuenta activa (%s) esta %s; se pasa a %s",
                        str(activa.id)[:8],
                        activa.session_status,
                        str(filas[0].id)[:8],
                    )
                    activa = filas[0]
            elif _fue_elegida(sesion, yo.id, activa.id):
                # Se enseña aunque el criterio general la dejara fuera: quien
                # la eligio tiene derecho a verla, y a ver por que esta vacia.
                filas.append(activa)
                filas.sort(key=lambda f: f.created_at)
            elif filas:
                activar_cuenta(sesion, user_id=yo.id, account_id=filas[0].id)
                cambio_automatico = {
                    "desde": str(activa.id),
                    "hasta": str(filas[0].id),
                }
                log.info(
                    "[API] la cuenta activa (%s) ya no sirve y NO la eligio "
                    "nadie; se pasa a %s",
                    str(activa.id)[:8],
                    str(filas[0].id)[:8],
                )
                activa = filas[0]
        elif activa is None and filas:
            activar_cuenta(sesion, user_id=yo.id, account_id=filas[0].id)
            activa = filas[0]

        activa_id = str(activa.id) if activa is not None else None
        cuerpo = [cuenta_a_json(f, activa=str(f.id) == activa_id) for f in filas]

    respuesta = {"accounts": cuerpo, "active_id": activa_id, "count": len(cuerpo)}
    if cambio_automatico is not None:
        # Que el frontend pueda AVISAR. Cambiar de cuenta por debajo y sin
        # decirlo es lo que hizo creer que los chats estaban mezclados: se
        # miraba una cuenta distinta de la que se creia.
        respuesta["switched_automatically"] = cambio_automatico
    return jsonify(respuesta)


#: Los unicos estados en los que una cuenta deja de servir POR SI MISMA.
#:
#: `whatsapp_accounts.session_status` solo admite cinco valores
#: --``never_linked``, ``linked``, ``disconnected``, ``revoked``, ``error``--
#: porque los estados transitorios (conectando, sincronizando, esperando QR)
#: son del RUNTIME, no de la cuenta: viven en `AppState` y no se guardan aqui.
#: Asi que una cuenta que acaba de vincularse y esta trayendo su historial
#: figura como ``linked``, y es presentable.
CERRADAS = frozenset({"revoked", "error"})


def _cerrada_definitivamente(fila: Any) -> bool:
    """Si esa vinculacion la cerro WhatsApp, no si parece vacia."""
    return getattr(fila, "session_status", "") in CERRADAS


def _fue_elegida(sesion: Any, user_id: Any, account_id: Any) -> bool:
    """Si esta cuenta la eligio la PERSONA, y no el sistema por defecto.

    Ante la duda --sin fila de membresia, o error leyendola-- se contesta que
    SI. Equivocarse por aqui deja al usuario mirando una cuenta que eligio;
    equivocarse por el otro lado se la cambia sin avisar, que es el fallo que
    esto viene a cerrar.
    """
    from sqlalchemy import select

    from app.models.accounts import UserWhatsAppMembership

    try:
        valor = sesion.execute(
            select(UserWhatsAppMembership.elegida_por_el_usuario).where(
                UserWhatsAppMembership.user_id == user_id,
                UserWhatsAppMembership.whatsapp_account_id == account_id,
            )
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - no poder leerlo no justifica moverlo
        return True
    return True if valor is None else bool(valor)


def _cuantos_chats(sesion: Any, ids: list) -> dict:
    """Chats por cuenta, en UNA consulta. Sin esto seria una por cuenta."""
    from sqlalchemy import func, select

    from app.models import Chat

    if not ids:
        return {}
    filas = sesion.execute(
        select(Chat.whatsapp_account_id, func.count())
        .where(Chat.whatsapp_account_id.in_(ids))
        .group_by(Chat.whatsapp_account_id)
    ).all()
    return {str(cuenta): total for cuenta, total in filas}


def _tiene_algo_que_ensenar(fila: Any, con_chats: dict) -> bool:
    """Si esta cuenta merece un sitio en el selector.

    NO ES LO MISMO QUE "ESTA VINCULADA"
    -----------------------------------
    Se llego a esto mirando una instalacion real, con cuatro cuentas y tres
    inservibles::

        9d6a0cfb  revoked       0 chats   <- y era la ACTIVA
        69900813  linked      327 chats
        e11fbe69  revoked       3 chats
        b864a6ce  never_linked  0 chats

    Filtrar por "vinculada" habria escondido tambien `e11fbe69`, y ahi hay tres
    conversaciones guardadas. Desvincular un telefono no borra lo que ya se
    copio, y esconder esa cuenta seria dejar al usuario sin ninguna forma de
    leer su propio historial.

    Lo que sobra no es "lo desvinculado": es lo que no tiene NADA detras --una
    cuenta que se creo para vincular y nunca se escaneo, o una revocada que no
    llego a traer un solo chat. Eso no es una opcion, es ruido con aspecto de
    opcion.
    """
    from app.models.accounts import LINKED_STATUSES

    if fila.session_status in LINKED_STATUSES:
        return True
    return con_chats.get(str(fila.id), 0) > 0


@accounts.post("/accounts")
@requiere_sesion
def anadir_cuenta():
    """Prepara OTRA cuenta para vincular. No toca la que ya existe.

    Devuelve la cuenta nueva; vincularla es el paso siguiente y usa el mismo
    emparejamiento de siempre, con ``?account_id=``. Aqui no se activa: hasta
    que tenga sesion, cambiar el contexto dejaria al usuario mirando una lista
    vacia mientras escanea.
    """
    # Antes de crear otra, tirar los intentos abandonados.
    #
    # Cada "Agregar cuenta" que no se llega a escanear deja una fila sin numero
    # y sin nada dentro. Se acumulan --se midieron seis-- y no solo ensucian el
    # selector: ROMPEN la atribucion de identidad, porque con varias candidatas
    # no se puede saber de quien es la sesion de la carpeta base, y entonces la
    # cuenta se sella sin nombre. Ver `barrer_cuentas_huerfanas`.
    _barrer_huerfanas_de(usuario_actual().id)

    servicio = _cuentas_service()
    if servicio is None:
        return _error("DB_UNAVAILABLE", "la base de datos no esta disponible", 503)

    datos = request.get_json(silent=True) or {}
    nombre = str(datos.get("display_name") or "").strip() or None

    yo = usuario_actual()
    try:
        fila = servicio.crear_cuenta_nueva(yo.id, display_name=nombre)
    except Exception:  # noqa: BLE001 - no se filtra el detalle interno
        log.exception("[API] no se pudo anadir la cuenta de WhatsApp")
        return _error("ACCOUNT_NOT_CREATED", "no se pudo crear la cuenta", 500)

    return jsonify({"account": cuenta_a_json(fila, activa=False)}), 201


@accounts.patch("/accounts/<account_id>")
@requiere_sesion
def renombrar_cuenta(account_id: str):
    """Cambia el nombre visible. No mueve nada en Drive.

    El espacio de la copia se nombra por identificador, no por nombre: si
    fuera al reves, renombrar dejaria huerfano el backup y dos cuentas con el
    mismo nombre chocarian.
    """
    servicio = _cuentas_service()
    if servicio is None:
        return _error("DB_UNAVAILABLE", "la base de datos no esta disponible", 503)

    datos = request.get_json(silent=True) or {}
    if "display_name" not in datos:
        return _error("MISSING_NAME", "falta display_name")

    yo = usuario_actual()
    if not servicio.renombrar(yo.id, account_id, str(datos.get("display_name") or "")):
        return _error("ACCOUNT_NOT_FOUND", "cuenta no encontrada", 404)

    return _devolver_una(account_id)


@accounts.post("/accounts/<account_id>/activate")
@requiere_sesion
def activar(account_id: str):
    """Cambia LA cuenta que el usuario esta mirando.

    A partir de aqui los listados, el historial y el canal de eventos son de
    esta. El frontend tiene que limpiar lo que tenia y volver a pedirlo: no es
    un filtro sobre una lista comun, es otro contexto.
    """
    database = _database()
    if database is None:
        return _error("DB_UNAVAILABLE", "la base de datos no esta disponible", 503)

    from app.auth.memberships import activar_cuenta

    yo = usuario_actual()
    with database.transaction() as sesion:
        # `por_el_usuario=True`: esto viene del selector, o sea de una
        # persona. A partir de aqui esa eleccion se respeta aunque la cuenta
        # parezca vacia.
        if not activar_cuenta(
            sesion, user_id=yo.id, account_id=account_id, por_el_usuario=True
        ):
            return _error("ACCOUNT_NOT_FOUND", "cuenta no encontrada", 404)

    log.info(
        "[API] cuenta activa cambiada: usuario=%s cuenta=%s",
        str(yo.id)[:8],
        str(account_id)[:8],
    )
    return _devolver_una(account_id, activa=True)


def _devolver_una(account_id: str, *, activa: bool | None = None):
    """La fila ya guardada, para que el cliente no tenga que volver a pedirla."""
    from app.auth.memberships import cuenta_activa_de, tiene_acceso
    from app.models import WhatsAppAccount

    database = _database()
    yo = usuario_actual()
    sesion = database.session()
    try:
        if not tiene_acceso(sesion, yo.id, account_id):
            return _error("ACCOUNT_NOT_FOUND", "cuenta no encontrada", 404)
        fila = sesion.get(WhatsAppAccount, account_id)
        if fila is None:
            return _error("ACCOUNT_NOT_FOUND", "cuenta no encontrada", 404)
        if activa is None:
            en_uso = cuenta_activa_de(sesion, yo.id)
            activa = en_uso is not None and str(en_uso.id) == str(fila.id)
        cuerpo = cuenta_a_json(fila, activa=bool(activa))
    finally:
        sesion.close()
    return jsonify({"account": cuerpo})


def _barrer_huerfanas_de(user_id: Any) -> None:
    """Tira los intentos de vinculacion abandonados. Nunca lanza."""
    try:
        from app.auth.atribucion import barrer_cuentas_huerfanas

        base = _database()
        ajustes = getattr(_runtime_base(), "settings", None)
        if base is None or ajustes is None:
            return
        barrer_cuentas_huerfanas(base, ajustes, user_id=user_id)
    except Exception:  # noqa: BLE001 - limpiar no puede impedir crear
        log.exception("[API] no se pudieron barrer las cuentas huerfanas")
