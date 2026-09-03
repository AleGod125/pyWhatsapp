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

QUE NO HACE
-----------
Nada. No migra, no crea sesiones, no deriva claves, no altera el descifrado.
Envuelve ``_decrypt_enc`` para MIRAR antes de dejarlo pasar, y devuelve
exactamente lo que devolvia. Ni una clave ni un byte de contenido sale al log.
"""

from __future__ import annotations

from typing import Any

from app.core.logging_setup import get_logger

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
        try:
            _observar(self, sender, enc_type)
        except Exception:  # noqa: BLE001 - observar no puede romper la recepcion
            pass
        return original(self, sender, enc_type, ciphertext)

    setattr(_decrypt_enc, _MARKER, True)
    receiver_module.Receiver._decrypt_enc = _decrypt_enc  # type: ignore[assignment]

    log.info("Diagnostico de sesion para el LID propio activado")
    return True


def _observar(receiver: Any, sender: Any, enc_type: str) -> None:
    """Registra el estado de la busqueda SOLO para mensajes nuestros."""
    if getattr(sender, "server", None) != "lid":
        return
    if _user(getattr(sender, "user", "")) != _own_lid_user:
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

    log.info(
        "[LID] mensaje propio type=%s dispositivo=%s | mapa_resuelve=%s "
        "sesion_por_lid=%s sesion_por_pn_mismo_dispositivo=%s",
        enc_type,
        dispositivo,
        bool(mapeado),
        sesion_por_lid,
        sesion_por_pn,
    )

    if not sesion_por_lid and not sesion_por_pn and mapeado:
        # El caso (C): el mapa resuelve, pero el dispositivo que envia no
        # tiene sesion por telefono, asi que no hay nada que migrar. Suele
        # significar que la copia viene de OTRO dispositivo vinculado (por
        # ejemplo WhatsApp Web), no del telefono.
        log.warning(
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
