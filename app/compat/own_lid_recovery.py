"""Una sesión LID propia que ya no puede abrir nada, se retira. Sólo esa.

LO QUE SE MIDIO
---------------
Con la guarda de sesión propia ya instalada (17:25:15), las copias del teléfono
seguían fallando::

    17:26:00  decrypt failed from=<mi LID> type=msg: mac check failed
    17:26:11  idem      17:27:16  idem      17:27:20  idem      17:31:09  idem

Y la sesión LID, muestreada dos veces con minutos de diferencia::

    86531142340710:0@lid    529 B   fp=21003e28f9cc
    86531142340710:0@lid    529 B   fp=21003e28f9cc     <- idéntica

**No cambia.** Y no puede cambiar: ``ratchet_decrypt`` lanza antes de guardar,
así que un registro que falla el MAC se queda congelado fallando para siempre.

Es un registro **heredado**: lo creó la migración de las 16:38, antes de que
existiera la guarda. La guarda impide que vuelva a pasar; no cura el que ya
estaba roto.

QUE PASABA DESPUES, Y POR QUE NO SE ARREGLABA SOLO
--------------------------------------------------
El acuse de reintento SÍ sale —``sent retry receipt to=<mi LID>``— pero el
teléfono no reenvía nada como ``pkmsg``. Es coherente: desde su lado la sesión
está perfectamente bien. El desincronizado es nuestro registro, y él no tiene
forma de saberlo. Así que reenvía con el mismo ratchet, y volvemos a fallar.

LO QUE HACE ESTA CAPA
---------------------
Cuando una copia **nuestra** falla el MAC repetidamente **contra el mismo
registro**, retira ese registro. Uno. El del LID propio.

A partir de ahí el mensaje siguiente ya no falla el MAC: falla por «no hay
sesión», que es un estado distinto y con salida — el acuse de reintento sobre
«no tengo sesión» es justo lo que lleva al emisor a rehacer el saludo y mandar
un ``pkmsg``, y ese ``pkmsg`` establece la sesión nueva por X3DH de verdad.

LO QUE NO HACE, Y ES LO QUE IMPORTA
-----------------------------------
* **No toca la sesión por número.** Es la que usa la excavación para pedirle
  historial al teléfono, y borrarla fue la causa del fallo anterior. Está
  prohibido aquí y hay una prueba que lo comprueba.
* **No copia ni fusiona ratchets.** Retirar no es mover.
* **No deriva claves ni salta ninguna verificación.** El MAC se sigue
  comprobando exactamente igual, y un mensaje que no lo pasa no se entrega.
* **No inventa una sesión nueva.** La crea el ``pkmsg`` del emisor, o no se
  crea.

Lo único que se retira es un ratchet que está demostrado que no abre nada. No
se pierde capacidad: ya no la tenía.

CONTRA LOS BUCLES
-----------------
Se lleva cuenta por huella de registro. Si el registro cambia, la cuenta se
reinicia: es otro ratchet y merece sus propios intentos. Y hay un tope de
retiradas por ejecución, para que un problema que no sea éste no acabe en
retirar y reintentar sin fin.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("SIGNAL")

_MARKER = "_whatsapp_backup_own_lid_recovery"

#: Fallos de MAC sobre el MISMO registro antes de retirarlo.
#:
#: Dos, y no uno: un fallo suelto puede ser un mensaje fuera de orden, y
#: retirar la sesión por eso costaría un saludo nuevo sin necesidad. Dos
#: seguidos contra la misma huella ya no son mala suerte.
FALLOS_ANTES_DE_RETIRAR = 2

#: Tope por ejecución. Si se llega aquí, el problema no es el que esta capa
#: sabe resolver, y seguir retirando sólo haría ruido.
MAXIMO_DE_RETIRADAS = 5

#: Lo que cuenta como «este registro no puede abrir nada».
_MOTIVOS_DE_RETIRADA = ("mac check failed", "bad mac")


@dataclass
class _Cuenta:
    """Lo que llevamos visto de un registro concreto."""

    huella: str
    fallos: int = 0
    retirado: bool = False


@dataclass
class Metricas:
    own_msg_recibidos: int = 0
    own_msg_mac_fallido: int = 0
    own_msg_sin_sesion: int = 0
    own_msg_descifrados: int = 0
    own_pkmsg_entrantes: int = 0
    own_lid_sesiones_retiradas: int = 0
    own_lid_sesiones_creadas: int = 0
    detalle: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "own_msg_received": self.own_msg_recibidos,
            "own_msg_decrypt_ok": self.own_msg_descifrados,
            "own_msg_mac_fail": self.own_msg_mac_fallido,
            "own_no_session": self.own_msg_sin_sesion,
            "own_retry_pkmsg_received": self.own_pkmsg_entrantes,
            "own_lid_session_retired": self.own_lid_sesiones_retiradas,
            "own_lid_session_rebuilt": self.own_lid_sesiones_creadas,
        }


METRICAS = Metricas()

#: Por dirección Signal. Sólo direcciones nuestras llegan aquí.
_cuentas: dict[str, _Cuenta] = {}
_retiradas = 0


def _huella(estado: Any) -> str:
    """Hash NO reversible del registro. Dice si cambió, no dice a qué."""
    if estado is None:
        return "-"
    if isinstance(estado, (bytes, bytearray)):
        crudo = bytes(estado)
    else:
        try:
            from pywhats.signal.experimental.store import serialize_state

            crudo = serialize_state(estado)
            if not isinstance(crudo, (bytes, bytearray)):
                crudo = str(crudo).encode("utf-8")
        except Exception:  # noqa: BLE001 - sin huella se trata como cambiada
            return "?"
    return hashlib.sha256(bytes(crudo)).hexdigest()[:12]


def _es_mac(exc: BaseException) -> bool:
    texto = str(exc).lower()
    return any(motivo in texto for motivo in _MOTIVOS_DE_RETIRADA)


def _es_sin_sesion(exc: BaseException) -> bool:
    return "no session" in str(exc).lower()


def reiniciar() -> None:
    """Olvida lo contado. Para las pruebas y para un re-emparejamiento."""
    global _retiradas
    _cuentas.clear()
    _retiradas = 0


def apply(settings: Any) -> bool:
    """Instala la recuperación. Idempotente; nunca lanza."""
    from app.compat import self_session_guard

    # La misma identidad que usa la guarda: si ella no sabe quiénes somos,
    # esto tampoco puede decidir nada.
    if not self_session_guard.apply(settings):
        log.debug("Sin guarda de sesion propia; no se instala la recuperacion LID")
        return False

    import pywhats.messaging.receiver as receiver_module
    from pywhats.messaging.addressing import session_id

    original = receiver_module.Receiver._decrypt_enc
    if getattr(original, _MARKER, False):
        return True

    def _decrypt_enc(self: Any, sender: Any, enc_type: str, ciphertext: bytes) -> bytes:  # type: ignore[no-untyped-def]
        propio = self_session_guard.es_dispositivo_propio(sender)
        if not propio:
            return original(self, sender, enc_type, ciphertext)

        # CUALQUIER cosa que llegue de un dispositivo propio puede ser la
        # respuesta a un acuse de reintento. Se anota antes de intentar nada:
        # si el telefono contesta, esto lo dira; y si no contesta nunca, su
        # ausencia es exactamente el hecho que hay que poder demostrar.
        try:
            from app.compat.own_retry_trace import anotar_respuesta

            anotar_respuesta(sender=sender, enc_type=enc_type)
        except Exception:  # noqa: BLE001 - el diagnostico no corta la recepcion
            pass

        sid = session_id(sender)
        es_lid = getattr(sender, "server", "") == "lid"
        antes = None
        try:
            antes = self._sessions.load(sid)
        except Exception:  # noqa: BLE001 - mirar no puede romper la recepcion
            antes = None
        huella_antes = _huella(antes)

        METRICAS.own_msg_recibidos += 1
        if enc_type == "pkmsg":
            METRICAS.own_pkmsg_entrantes += 1

        try:
            resultado = original(self, sender, enc_type, ciphertext)
        except Exception as exc:  # noqa: BLE001 - se clasifica y se RELANZA
            if _es_sin_sesion(exc):
                METRICAS.own_msg_sin_sesion += 1
            elif _es_mac(exc):
                METRICAS.own_msg_mac_fallido += 1
                if es_lid:
                    _quiza_retirar(self, sid, huella_antes)
            log.debug(
                "[OWN_LIVE] enc=%s address=%s lid_fp=%s decrypt=fallo (%s)",
                enc_type, "LID" if es_lid else "PN", huella_antes, str(exc)[:60],
            )
            # Un mensaje que no supera su verificacion NO se entrega. Se
            # relanza tal cual: el acuse de reintento lo manda pywhats.
            raise

        METRICAS.own_msg_descifrados += 1
        if enc_type == "pkmsg" and es_lid and antes is None:
            METRICAS.own_lid_sesiones_creadas += 1
            _cuentas.pop(sid, None)
            log.info(
                "[OWN_SIGNAL] address=LID:%s action=CREATED reason=pkmsg_entrante",
                getattr(sender, "device", 0),
            )
        log.debug(
            "[OWN_LIVE] enc=%s address=%s lid_fp=%s decrypt=ok",
            enc_type, "LID" if es_lid else "PN", huella_antes,
        )
        return resultado

    setattr(_decrypt_enc, _MARKER, True)
    receiver_module.Receiver._decrypt_enc = _decrypt_enc  # type: ignore[method-assign]
    log.debug("Recuperacion de sesion LID propia instalada")
    return True


def _quiza_retirar(receptor: Any, sid: str, huella: str) -> None:
    """Retira el registro LID si ya está demostrado que no abre nada.

    Sólo ESE. La sesión por número no se toca aquí ni por accidente: este
    camino sólo se llama cuando el remitente llegó por LID.
    """
    global _retiradas

    if huella in ("-", "?"):
        # No hay registro que leer, o no se pudo. Sin un ratchet concreto al
        # que atribuir el fallo no se retira nada: sin esto, un registro ya
        # retirado volveria a contarse y se entraria en el bucle que §13
        # prohibe.
        return

    cuenta = _cuentas.get(sid)
    if cuenta is None or cuenta.huella != huella:
        # Registro distinto al que veniamos contando: empieza de cero. Un
        # ratchet nuevo merece sus propios intentos.
        cuenta = _Cuenta(huella=huella)
        _cuentas[sid] = cuenta
    cuenta.fallos += 1

    if cuenta.retirado or cuenta.fallos < FALLOS_ANTES_DE_RETIRAR:
        return
    if _retiradas >= MAXIMO_DE_RETIRADAS:
        log.debug("[OWN_SIGNAL] tope de retiradas alcanzado; no se retira mas")
        return

    try:
        receptor._sessions.delete(sid)
    except Exception:  # noqa: BLE001 - no poder retirarlo no puede romper nada
        log.debug("[OWN_SIGNAL] no se pudo retirar el registro LID")
        return

    cuenta.retirado = True
    _retiradas += 1
    METRICAS.own_lid_sesiones_retiradas += 1
    log.info(
        "[OWN_SIGNAL] address=LID action=REMOVED reason=mac_fallido_repetido "
        "old_fp=%s fallos=%d. La sesion por numero NO se toca. El proximo "
        "mensaje pedira reenvio y el telefono deberia mandarlo como pkmsg.",
        huella,
        cuenta.fallos,
    )
