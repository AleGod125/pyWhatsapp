"""Por que un mensaje NUESTRO no encuentra su sesion. Solo observa.

LA CONTRADICCION QUE HAY QUE RESOLVER
-------------------------------------
En la MISMA ejecucion de ``service.py`` aparecen las dos cosas::

    [COMPAT] el LID propio resuelve a su PN=True
    receiver: decrypt failed from=86531142340710@lid type=msg:
              no session for peer 86531142340710@lid

O sea: el mapa PN<->LID resuelve, y aun asi el descifrado no encuentra sesion.
Comprobar el mapa NO era suficiente, porque el mapa es solo el primer paso de
lo que hace ``_migrate_known_lid_sender``::

    if sender.server != "lid" or sessions.load(session_id(sender)) is not None:
        return                                   # (1) ya hay sesion por LID
    pn_user = lid_map.get_pn(sender.user)
    if pn_user is None:
        return                                   # (2) el mapa no resuelve
    pn = JID(user=pn_user, server="s.whatsapp.net", device=sender.device)
    migrate_pn_session_to_lid(...)               # (3) migra ESE dispositivo

El paso (3) usa ``sender.device``. Una cuenta tiene VARIOS dispositivos
vinculados: el telefono es el 0, pero WhatsApp Web y este propio companion son
otros. Si la copia llega desde un dispositivo para el que no hay sesion por
telefono, no hay nada que migrar aunque el mapa resuelva perfectamente.

QUE HACE ESTE MODULO
--------------------
Distinguir, con datos del camino REAL del receptor, cual de los cuatro casos
es:

    A) el mapa existe pero el receptor no lo consulta
    B) el mapa existe y el PN de ESE dispositivo tiene sesion
    C) el mapa existe pero el PN de ese dispositivo tampoco tiene sesion
    D) se esta mirando otro Signal Store

Y ademas distingue POR QUE fallo, que no es lo mismo:

    A) no hay sesion    la direccion por la que llega no tiene sesion guardada
    B) fallo de MAC     hay sesion, pero el mensaje no supera su verificacion
    C) reintento bueno  el mismo mensaje vuelve como ``pkmsg`` y si descifra
    D) nunca volvio     se pidio el reenvio y no llego

Mezclar (A) y (B) llevaba a buscar el fallo en el sitio equivocado: uno se
arregla con la sesion y el otro con el ratchet.

QUE NO HACE
-----------
Nada. No migra, no crea sesiones, no deriva claves, no altera el descifrado.
Envuelve ``_decrypt_enc`` para MIRAR, y devuelve exactamente lo que devolvia.
Cuando falla, RELANZA la excepcion sin tocarla: un mensaje que no supera su
verificacion de autenticidad no se entrega, y eso no se negocia. Ni una clave
ni un byte de contenido sale al log.
"""

from __future__ import annotations

from typing import Any

from app.core.logging_setup import RateLimitedLogger, get_logger

# La etiqueta es SIGNAL: esto observa la busqueda de sesiones, que es
# territorio de Signal aunque no lo toque.
log = get_logger("SIGNAL")

_MARKER = "_whatsapp_backup_lid_diagnostics"

# Nuestro propio usuario LID, sin sufijo de dispositivo. Se fija al aplicar.
_own_lid_user: str | None = None
_own_pn_user: str | None = None


def _user(jid: str | None) -> str | None:
    if not jid:
        return None
    usuario = jid.split("@")[0].split(":")[0].split(".")[0]
    return usuario or None


def apply(settings: Any) -> bool:
    """Instala la observacion. Idempotente y sin cambiar comportamiento."""
    global _own_lid_user, _own_pn_user

    from app.core.identity import own_identity

    pn, lid = own_identity(settings)
    _own_pn_user, _own_lid_user = _user(pn), _user(lid)
    if not _own_lid_user:
        # Sin identidad propia no hay nada que distinguir.
        return False

    import pywhats.messaging.receiver as receiver_module

    original = receiver_module.Receiver._decrypt_enc
    if getattr(original, _MARKER, False):
        return True

    def _decrypt_enc(self, sender, enc_type, ciphertext):  # type: ignore[no-untyped-def]
        propio = _es_nuestro(sender)
        if propio:
            try:
                _observar(self, sender, enc_type)
            except Exception:  # noqa: BLE001 - observar no puede romper la recepcion
                pass
        try:
            resultado = original(self, sender, enc_type, ciphertext)
        except Exception as exc:
            # Se clasifica y se RELANZA sin tocar. No se captura para
            # continuar: un mensaje que no supera su verificacion no se
            # entrega, y eso no se negocia.
            if propio:
                try:
                    _clasificar(sender, enc_type, exc)
                except Exception:  # noqa: BLE001
                    pass
            raise
        if propio:
            METRICAS["descifrados"] = METRICAS.get("descifrados", 0) + 1
        return resultado

    setattr(_decrypt_enc, _MARKER, True)
    receiver_module.Receiver._decrypt_enc = _decrypt_enc  # type: ignore[assignment]

    log.debug("Diagnostico de sesion para el LID propio activado")
    return True


#: Contadores de lo que pasa con NUESTROS propios mensajes. Se exponen para
#: poder resumirlo y para que una prueba pueda comprobarlo.
METRICAS: dict[str, int] = {}

#: Los avisos repetidos se agrupan: cien fallos identicos no son cien
#: problemas. El primero se ve siempre.
_avisos = RateLimitedLogger(log, ventana=60.0)

#: De cual de mis dispositivos venia la ULTIMA copia propia observada. El
#: evento ``decrypt_error`` de pywhats no lleva el remitente, asi que sin esto
#: no habria forma de atribuirle origen a un fallo sin volver a adivinarlo.
_ultimo_origen: str | None = None


def ultimo_origen() -> str | None:
    """El origen de la ultima copia propia que paso por el descifrado."""
    return _ultimo_origen


def _es_nuestro(sender: Any) -> bool:
    """``True`` si el remitente es nuestro propio LID."""
    if getattr(sender, "server", None) != "lid":
        return False
    return _user(getattr(sender, "user", "")) == _own_lid_user


def _clasificar(sender: Any, enc_type: str, exc: BaseException) -> None:
    """Que clase de fallo fue. Son cosas distintas y se arreglan distinto.

    A) NO HAY SESION       la direccion por la que llega no tiene sesion
                           guardada. Se resuelve migrando o rehaciendo X3DH,
                           y el reenvio como ``pkmsg`` lo hace solo.
    B) FALLO DE MAC        hay sesion, pero el mensaje no supera su
                           verificacion de autenticidad. Suele ser un ratchet
                           desincronizado. NO se acepta el mensaje: eso seria
                           aceptar algo que no se ha podido autenticar.
    C) OTRO                cualquier otra cosa, sin clasificar de mas.

    Aqui solo se cuenta y se avisa. El reintento por receipt sigue su curso
    exactamente como antes.
    """
    global _ultimo_origen
    from app.core.own_device import clasificar

    motivo = str(exc).lower()
    dispositivo = getattr(sender, "device", 0)
    origen = clasificar(sender, own_pn_user=_own_pn_user, own_lid_user=_own_lid_user)
    _ultimo_origen = origen

    if "no session" in motivo:
        clave, caso = "sin_sesion", "no hay sesion para esa direccion"
    elif "mac" in motivo:
        clave, caso = "mac_fallido", "la verificacion de autenticidad no paso"
    else:
        clave, caso = "otro_fallo", "fallo de descifrado"

    METRICAS[clave] = METRICAS.get(clave, 0) + 1
    METRICAS[f"fallo:{origen}"] = METRICAS.get(f"fallo:{origen}", 0) + 1
    _avisos.warning(
        f"{clave}:{enc_type}:{origen}",
        "[LID] copia propia no descifrada (origen=%s, %s, type=%s, "
        "dispositivo=%s): %s. Se envia el acuse de reintento; el emisor "
        "deberia reenviarla como pkmsg.",
        origen,
        clave,
        enc_type,
        dispositivo,
        caso,
    )


def resumen() -> str:
    """Una linea con lo ocurrido. Para el arranque y el diagnostico."""
    if not METRICAS:
        return "mensajes propios: sin incidencias"
    partes = ", ".join(f"{k}={v}" for k, v in sorted(METRICAS.items()))
    return f"mensajes propios: {partes}"


def _observar(receiver: Any, sender: Any, enc_type: str) -> None:
    """Registra el estado de la busqueda SOLO para mensajes nuestros."""
    if not _es_nuestro(sender):
        return

    from pywhats.events import JID
    from pywhats.messaging.addressing import session_id

    dispositivo = getattr(sender, "device", 0)
    mapeado = None
    try:
        mapeado = receiver._lid_map.get_pn(sender.user)
    except Exception:  # noqa: BLE001
        pass

    sesion_por_lid = False
    try:
        sesion_por_lid = receiver._sessions.load(session_id(sender)) is not None
    except Exception:  # noqa: BLE001
        pass

    sesion_por_pn = False
    if mapeado:
        try:
            pn_jid = JID(
                user=mapeado, server="s.whatsapp.net", device=dispositivo
            )
            sesion_por_pn = receiver._sessions.load(session_id(pn_jid)) is not None
        except Exception:  # noqa: BLE001
            pass

    global _ultimo_origen
    from app.core.own_device import clasificar, direccion_signal, enmascarar

    origen = clasificar(sender, own_pn_user=_own_pn_user, own_lid_user=_own_lid_user)
    _ultimo_origen = origen
    METRICAS[f"origen:{origen}"] = METRICAS.get(f"origen:{origen}", 0) + 1

    # La traza completa de la resolucion de direccion, para poder comparar el
    # caso que falla con el que funciona sin adivinar nada. Va a DEBUG: una
    # linea por mensaje en INFO llena la consola en cuanto hay trafico.
    log.debug(
        "[LID] copia propia origen=%s type=%s dispositivo=%s | "
        "direccion_signal=%s mapa_resuelve=%s sesion_por_lid=%s "
        "sesion_por_pn_mismo_dispositivo=%s",
        origen,
        enc_type,
        dispositivo,
        enmascarar(direccion_signal(sender)),
        bool(mapeado),
        sesion_por_lid,
        sesion_por_pn,
    )

    if sesion_por_lid and sesion_por_pn:
        # LAS DOS a la vez para el MISMO dispositivo: es el mismo aparato con
        # dos ratchets. Medido en la cuenta real (dispositivo 0). Se avisa,
        # no se toca: la recuperacion correcta es el reenvio como pkmsg.
        _avisos.warning(
            f"sesion_duplicada:{dispositivo}",
            "[LID] el dispositivo %s tiene sesion por numero Y por LID a la "
            "vez: es el mismo aparato con dos ratchets, y por eso alguna copia "
            "del telefono puede no cuadrar.",
            dispositivo,
        )

    if not sesion_por_lid and not sesion_por_pn and mapeado:
        # El caso (C): el mapa resuelve, pero el dispositivo que envia no
        # tiene sesion por telefono, asi que no hay nada que migrar. Suele
        # significar que la copia viene de OTRO dispositivo vinculado (por
        # ejemplo WhatsApp Web), no del telefono.
        _avisos.warning(
            f"sin_sesion_dispositivo:{dispositivo}",
            "[LID] no hay sesion para el dispositivo %s de la cuenta propia: "
            "la copia no viene del telefono (dispositivo 0) sino de otro "
            "dispositivo vinculado. Se enviara el retry receipt de siempre y "
            "el remitente deberia reenviarlo como pkmsg.",
            dispositivo,
        )


def snapshot(receiver: Any) -> dict[str, Any]:
    """Estado actual, para una prueba de integracion. No registra nada."""
    from pywhats.events import JID
    from pywhats.messaging.addressing import session_id

    if not _own_lid_user or not _own_pn_user:
        return {"own_identity": False}

    lid_jid = JID(user=_own_lid_user, server="lid", device=0)
    pn_jid = JID(user=_own_pn_user, server="s.whatsapp.net", device=0)
    return {
        "own_identity": True,
        "map_resolves": receiver._lid_map.get_pn(_own_lid_user) == _own_pn_user,
        "session_by_lid": receiver._sessions.load(session_id(lid_jid)) is not None,
        "session_by_pn": receiver._sessions.load(session_id(pn_jid)) is not None,
    }
