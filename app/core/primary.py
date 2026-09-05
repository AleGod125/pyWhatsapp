"""¿Está la conexión principal realmente lista? Una sola respuesta.

POR QUE HACE FALTA
------------------
Se midió el caso: `service.py` arrancó sin sesión — `STARTING → NO_SESSION`,
sin `device.json`, sin identidad, sin Signal — y aun así el segundo dispositivo
pidió su código QR. El usuario se quedó mirando el código equivocado: el que
hacía falta escanear era el principal.

La causa es que cada sitio decidía por su cuenta si «había sesión», y ninguno
lo preguntaba entero. El supervisor del segundo dispositivo, además, se
reinicia solo con espera creciente cuando su worker muere; ese reinicio no
miraba nada, así que seguía publicando códigos mucho después de que la sesión
principal hubiera dejado de existir.

LA REGLA
--------
**La sesión principal manda; el segundo dispositivo espera.** Y para que eso
se pueda cumplir tiene que haber UNA definición de «lista», no cinco parecidas.

QUE SE EXIGE
------------
Las cuatro a la vez, porque cada una sola se puede cumplir sin las otras:

* el runtime dice que está conectado (o recibiendo el historial inicial);
* hay identidad propia — el emparejamiento llegó a completarse;
* el Signal Store existe — se puede descifrar y cifrar;
* hay una cuenta de WhatsApp reconciliada.

Que exista `device.json` NO basta: un emparejamiento a medias lo deja escrito.
Que alguna vez hubiera un `pair-success` tampoco: describe el pasado.
"""

from __future__ import annotations

from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("APP")

#: Motivos, en el orden en que se comprueban. El primero que falla es el que
#: se reporta: es el que el usuario tiene que resolver primero.
NO_RUNTIME = "NO_RUNTIME"
NOT_CONNECTED = "NOT_CONNECTED"
NO_IDENTITY = "NO_IDENTITY"
NO_SIGNAL_STORE = "NO_SIGNAL_STORE"
NO_ACCOUNT = "NO_ACCOUNT"

#: Se pierde la conexión pero las credenciales siguen valiendo. NO es lo mismo
#: que «hay que volver a vincular»: el runtime está reintentando y volver a
#: enseñar el código sería mandar al usuario a rehacer algo que no está roto.
RECONNECTING = "RECONNECTING"


def primary_ready(runtime: Any) -> bool:
    """``True`` sólo si la conexión principal está lista de verdad."""
    return razon_no_lista(runtime) is None


def razon_no_lista(runtime: Any) -> str | None:
    """Por qué NO está lista, o ``None`` si lo está.

    Devuelve el motivo para poder decírselo al usuario en sus palabras, en vez
    de un «no disponible» que no explica qué hacer.
    """
    if runtime is None:
        return NO_RUNTIME

    from app.core.session_state import AppState

    estado = getattr(getattr(runtime, "state", None), "state", None)
    if estado is AppState.RECONNECTING:
        # La sesión sigue siendo válida: sólo se cayó el socket.
        return RECONNECTING
    if estado not in (AppState.CONNECTED, AppState.WAITING_INITIAL_HISTORY):
        return NOT_CONNECTED

    # La identidad se lee del dispositivo VIVO, no de un archivo que pudo
    # quedar de un emparejamiento anterior o a medias.
    if not _hay_identidad(runtime):
        return NO_IDENTITY

    ajustes = getattr(runtime, "settings", None)
    almacen = getattr(ajustes, "signal_store_file", None)
    if almacen is None or not almacen.exists():
        return NO_SIGNAL_STORE

    if getattr(runtime, "runtime_owner_account_id", None) is None:
        return NO_ACCOUNT

    return None


def _hay_identidad(runtime: Any) -> bool:
    """Identidad propia del cliente conectado.

    Se pregunta al cliente vivo y, si no se puede, a lo persistido. Un
    emparejamiento que no llegó a completarse deja `device.json` escrito pero
    sin JID utilizable, así que la existencia del archivo no vale como prueba.
    """
    cliente = getattr(getattr(runtime, "client", None), "_client", None)
    dispositivo = getattr(cliente, "device", None)
    jid = getattr(dispositivo, "jid", None)
    if jid is not None and getattr(jid, "user", None):
        return True

    ajustes = getattr(runtime, "settings", None)
    if ajustes is None:
        return False
    try:
        from app.core.identity import own_identity

        pn, _lid = own_identity(ajustes)
        return bool(pn)
    except Exception:  # noqa: BLE001 - no poder leerlo es no tenerla
        return False


def esperando_reconexion(runtime: Any) -> bool:
    """La sesión es válida y el runtime está volviendo a levantarla.

    Sirve para no mandar al usuario al código QR por un corte de red: las
    credenciales siguen sirviendo y no hay nada que volver a vincular.
    """
    return razon_no_lista(runtime) == RECONNECTING


def mensaje_para_el_usuario(motivo: str | None) -> str:
    """Qué decirle, en sus palabras y con una salida clara."""
    return {
        None: "",
        RECONNECTING: (
            "Conexión temporalmente perdida. Estamos volviendo a conectar; "
            "no hace falta que hagas nada."
        ),
        NO_RUNTIME: "El servicio todavía no está listo.",
        NOT_CONNECTED: "Necesitas vincular WhatsApp para continuar.",
        NO_IDENTITY: (
            "La vinculación no llegó a completarse. Vuelve a escanear el "
            "código para continuar."
        ),
        NO_SIGNAL_STORE: (
            "Falta la información de seguridad de la sesión. Vuelve a "
            "vincular WhatsApp."
        ),
        NO_ACCOUNT: "Necesitas vincular WhatsApp para continuar.",
    }.get(motivo, "Necesitas vincular WhatsApp para continuar.")
