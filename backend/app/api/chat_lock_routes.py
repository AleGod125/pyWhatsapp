"""El codigo de acceso de los chats restringidos.

LO QUE SE INVESTIGO, Y LO QUE SALIO
-----------------------------------
La pregunta era si se puede abrir un chat restringido con EL MISMO codigo
secreto que el usuario puso en WhatsApp, como hace WhatsApp Web. La respuesta
es que no, y conviene que quede escrito para que nadie lo vuelva a intentar:

1. ``SyncActionValue.chatLockSettings`` trae un campo ``secretCode``, pero son
   BYTES OPACOS. WhatsApp no manda el codigo: manda un derivado con el que el
   telefono comprueba, y no hay forma de volver atras desde ahi.
2. Baileys ni siquiera llega a eso. ``lockChatAction`` es la unica accion de
   app-state que no procesa, asi que el evento no aparece por ninguna parte.
3. Y lo mas importante: **los mensajes de un chat restringido no estan
   cifrados con ese codigo**. Llegan y se guardan como los de cualquier otro.
   El bloqueo de WhatsApp es un control de acceso de SU interfaz, no una capa
   de cifrado. Aunque tuvieramos el codigo, no descifraria nada, porque no hay
   nada que descifrar.

QUE SE HACE ENTONCES
--------------------
Un codigo LOCAL, de esta aplicacion, que tapa la seccion igual que WhatsApp
Web tapa la suya. Se dice con todas las letras en la pantalla: no es el codigo
de WhatsApp y no puede serlo.

Y una advertencia, que es lo que el usuario pidio primero: para que un chat
llegue marcado como restringido, el bloqueo tiene que estar puesto EN EL
TELEFONO. Sin eso ``locked`` no se activa nunca y la seccion sale vacia -- lo
cual, sin explicacion, parece que la funcion esta rota.

DONDE VIVE
----------
El hash, en ``app_state`` bajo ``chat_lock:<user_id>``, igual que las
preferencias: es una tabla de clave/valor con JSONB y el aislamiento lo da la
propia clave. No hace falta migracion.

El "esta abierto ahora", en la cookie de sesion de Flask, que va firmada. Ahi
y no en la base a proposito: un pestillo abierto es de ESTE navegador. Si
viviera en el servidor, abrirlo en el ordenador de casa lo dejaria abierto en
el del trabajo, que es justo lo contrario de lo que significa un pestillo.
"""

from __future__ import annotations

import time
from typing import Any

from flask import Blueprint, jsonify, request, session

from app.auth.web import requiere_sesion, usuario_actual
from app.core.logging_setup import get_logger

log = get_logger("API")

chat_lock = Blueprint("chat_lock", __name__)

#: Prefijo de la clave en ``app_state``. El identificador del usuario va
#: detras, y eso es lo que impide que uno lea el de otro.
PREFIJO = "chat_lock:"

#: Clave dentro de la cookie de sesion.
PESTILLO = "chat_lock_abierto"

#: Cuanto dura abierto sin tocar nada. WhatsApp Web vuelve a pedirlo solo; que
#: se quede abierto para siempre convierte el codigo en un adorno.
MINUTOS_ABIERTO = 15

#: Largo del codigo. WhatsApp usa seis digitos para su bloqueo de chats y es
#: lo que el usuario espera teclear; pedirle una contrasena de cuenta seria
#: otra cosa distinta con el mismo nombre.
MINIMO = 6
MAXIMO = 64


def _clave(user_id: Any) -> str:
    return f"{PREFIJO}{user_id}"


def _hasher():
    """El mismo Argon2id que las contrasenas, SIN su politica de contrasenas.

    ``PasswordHasherService.hash`` exige ocho caracteres y variedad, que es
    correcto para entrar en la cuenta y no para esto: WhatsApp acepta seis
    digitos en su bloqueo de chats, y pedirle al usuario otra cosa con el mismo
    nombre solo confunde. El coste por intento es el mismo, que es lo que de
    verdad protege un codigo corto.
    """
    from app.auth.passwords import PasswordHasherService

    return PasswordHasherService()


def _hash_de(codigo: str) -> str:
    return _hasher().hash_sin_politica(codigo)


def _guardado(user_id: Any) -> dict[str, Any]:
    """Lo que hay guardado de este usuario. ``{}`` si no ha puesto codigo."""
    from app.api.routes import runtime
    from app.services import repository as repo

    rt = runtime()
    if rt is None or rt.database is None:
        return {}
    with rt.database.transaction() as sesion:
        valor = repo.get_app_state(sesion, _clave(user_id))
    return valor if isinstance(valor, dict) else {}


def _abierto() -> bool:
    """Si el pestillo de ESTE navegador sigue abierto."""
    hasta = session.get(PESTILLO)
    return bool(hasta) and float(hasta) > time.time()


def abierto_para_la_peticion() -> bool:
    """Lo que consulta ``/chats`` antes de servir la seccion restringida.

    Vive aqui para que la regla este en un solo sitio: si la duracion cambia,
    cambia para quien pregunta y para quien abre.
    """
    return _abierto()


def hay_codigo(user_id: Any) -> bool:
    return bool(_guardado(user_id).get("hash"))


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------


@chat_lock.get("/chat-lock")
@requiere_sesion
def estado():
    """Si hay codigo puesto y si ahora mismo esta abierto.

    No devuelve el hash ni nada derivado de el: quien pregunta solo necesita
    saber que pantalla pintar.
    """
    usuario = usuario_actual()
    return jsonify(
        {
            "configurado": hay_codigo(usuario.id),
            "abierto": _abierto(),
            "minutos": MINUTOS_ABIERTO,
        }
    )


@chat_lock.post("/chat-lock")
@requiere_sesion
def poner():
    """Pone o cambia el codigo local.

    Para CAMBIARLO hay que saber el anterior. Sin eso, cualquiera que se
    siente delante de una sesion abierta lo sustituye por el suyo y el
    pestillo no ha servido de nada.
    """
    usuario = usuario_actual()
    cuerpo = request.get_json(silent=True) or {}
    nuevo = str(cuerpo.get("codigo") or "")
    anterior = str(cuerpo.get("codigo_actual") or "")

    if not (MINIMO <= len(nuevo) <= MAXIMO):
        return (
            jsonify(
                {
                    "error": f"el codigo tiene que medir entre {MINIMO} y "
                    f"{MAXIMO} caracteres"
                }
            ),
            400,
        )

    guardado = _guardado(usuario.id)
    if guardado.get("hash"):
        resultado = _hasher().verify(guardado["hash"], anterior)
        if not resultado.valida:
            return jsonify({"error": "el codigo actual no es correcto"}), 403

    from app.api.routes import runtime
    from app.services import repository as repo

    rt = runtime()
    if rt is None or rt.database is None:
        return jsonify({"error": "la base de datos no esta disponible"}), 503
    with rt.database.transaction() as sesion:
        repo.set_app_state(
            sesion,
            _clave(usuario.id),
            {"hash": _hash_de(nuevo), "puesto_en": int(time.time())},
        )

    # Ponerlo lo deja abierto: el usuario acaba de demostrar que lo sabe, y
    # pedirselo otra vez en el segundo siguiente solo molesta.
    session[PESTILLO] = time.time() + MINUTOS_ABIERTO * 60
    log.info("Codigo de chats restringidos actualizado")
    return jsonify({"configurado": True, "abierto": True})


@chat_lock.post("/chat-lock/abrir")
@requiere_sesion
def abrir():
    """Comprueba el codigo y abre el pestillo de este navegador."""
    usuario = usuario_actual()
    cuerpo = request.get_json(silent=True) or {}
    codigo = str(cuerpo.get("codigo") or "")

    guardado = _guardado(usuario.id)
    if not guardado.get("hash"):
        return jsonify({"error": "todavia no has puesto un codigo"}), 409

    resultado = _hasher().verify(guardado["hash"], codigo)
    if not resultado.valida:
        # No se dice si fallo por poco ni cuantos intentos quedan: cualquier
        # detalle de mas es una pista. El hasher ya gasta el mismo tiempo
        # acierte o falle.
        log.info("Codigo de chats restringidos incorrecto")
        return jsonify({"error": "codigo incorrecto"}), 403

    session[PESTILLO] = time.time() + MINUTOS_ABIERTO * 60
    return jsonify({"abierto": True, "minutos": MINUTOS_ABIERTO})


@chat_lock.post("/chat-lock/cerrar")
@requiere_sesion
def cerrar():
    """Vuelve a echar el pestillo. Sin preguntar nada."""
    session.pop(PESTILLO, None)
    return jsonify({"abierto": False})
