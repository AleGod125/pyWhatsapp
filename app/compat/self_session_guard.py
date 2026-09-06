"""No migrar la sesión Signal de NUESTRO PROPIO teléfono. Medido, no supuesto.

EL FALLO, CON MARCAS DE TIEMPO REALES
------------------------------------
Sobre la sesión del 5 de septiembre, tras sembrar el par PN↔LID propio::

    16:35:51  enc=msg    sesion_por_pn=True   sesion_por_lid=False
    16:37:56  [LIVE] persisted ... deviceSentMessage        <- copia propia OK
    16:38:06  llega una copia propia dirigida al LID
    16:38:07  enc=pkmsg  sesion_por_pn=FALSE  sesion_por_lid=True
    16:38:08  enc=msg    sesion_por_pn=True   sesion_por_lid=True
    16:38:10  decrypt failed from=<mi LID> type=msg: mac check failed
    16:38:18  mac check failed
    16:38:51  mac check failed   ... y así el resto de la sesión

Tres segundos entre el ``pkmsg`` de ON_DEMAND y el primer MAC fallido, y a
partir de ahí ni una copia propia más.

LA CADENA, ESLABÓN A ESLABÓN
----------------------------
1. Al llegar una copia nuestra dirigida al LID, ``_migrate_known_lid_sender``
   mueve la sesión del teléfono a la dirección LID. Y **mover** incluye::

       sessions.delete(pn_key)
       identity_store.delete(pn_key)

2. Esa dirección PN que acaba de borrarse es **justo la que usa ON_DEMAND**:
   ``_target_jid()`` apunta al teléfono por número, dispositivo 0.

3. La siguiente petición de historial se encuentra sin sesión, así que hace
   X3DH y sale como ``pkmsg``. El teléfono acepta el saludo y **rehace su
   ratchet** con nosotros.

4. La copia propia siguiente llega cifrada con ese ratchet nuevo, y nosotros
   intentamos abrirla con el registro LID —que guarda el ratchet ANTERIOR, el
   que se migró en el paso 1—. MAC fallido, y ya no se recupera.

POR QUE LA MIGRACION ESTA BIEN PARA UN TERCERO Y MAL PARA NOSOTROS
------------------------------------------------------------------
Para un contacto normal es correcta: WhatsApp lo está migrando a LID de forma
permanente y nosotros **no le mandamos nada por su número**, así que borrar esa
clave no rompe nada.

Con nuestro propio teléfono es distinto, y por una razón concreta: le
escribimos. Cada petición de historial va dirigida a él por el número. Borrar
esa dirección no es limpiar algo que ya no se usa — es tirar la sesión que
estamos usando, y forzar un saludo nuevo que descoloca a la otra.

QUE HACE ESTA CAPA
------------------
Una sola cosa: **si el remitente es nuestro propio dispositivo, no migra.**
Deja que las dos direcciones vivan cada una con su sesión.

* la sesión por número sigue viva, así que ON_DEMAND continúa con ``enc=msg``
  y no vuelve a saludar;
* la copia propia dirigida al LID, si todavía no hay sesión LID, falla una vez
  y provoca un acuse de reintento. El teléfono la reenvía como ``pkmsg``, y esa
  sesión LID se crea **por el camino legítimo**: X3DH de verdad, con su
  material, no una copia.

QUE NO HACE, Y ES LO QUE IMPORTA
--------------------------------
No copia sesiones. No fusiona ratchets. No deriva claves. No mueve identidades.
No salta ninguna comprobación de MAC. No borra nada. Sólo **se abstiene** de
hacer un movimiento que estaba haciendo pywhats, y sólo para una dirección: la
nuestra.

Que existan a la vez una sesión por número y una por LID del mismo aparato no
es un fallo: son dos direcciones criptográficas distintas del mismo teléfono,
y el protocolo lo permite. El fallo era tener una de las dos **desincronizada**
por haberla movido en vez de haberla establecido.
"""

from __future__ import annotations

from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("SIGNAL")

_MARKER = "_whatsapp_backup_self_session_guard"

#: Usuario de nuestro LID y de nuestro PN, sin sufijo de dispositivo.
_own_lid_user: str | None = None
_own_pn_user: str | None = None

#: Cuántas veces nos hemos abstenido. Sirve para saber si la capa hace algo.
METRICAS: dict[str, int] = {"migraciones_evitadas": 0}


def _user(jid: str | None) -> str | None:
    """Parte de usuario de un JID, sin dispositivo.

    Un LID puede venir como ``8653...@lid``, ``8653....6@lid`` o
    ``8653...:0@lid``. El sufijo de dispositivo NO forma parte del usuario, y
    confundirlos hace que la comparación falle justo en el caso que importa.
    """
    if not jid:
        return None
    usuario = str(jid).split("@")[0].split(":")[0].split(".")[0]
    return usuario or None


def es_dispositivo_propio(sender: Any) -> bool:
    """Si este remitente somos nosotros mismos, por cualquiera de las dos vías.

    Se compara por USUARIO, no por dirección completa: el dispositivo cambia
    —el teléfono es el 0, nosotros somos otro— y lo que decide es de quién es
    la cuenta.
    """
    usuario = _user(getattr(sender, "user", None))
    if not usuario:
        return False
    return usuario in {u for u in (_own_lid_user, _own_pn_user) if u}


def apply(settings: Any) -> bool:
    """Instala la guarda. Idempotente; nunca lanza."""
    global _own_lid_user, _own_pn_user

    from app.core.identity import own_identity

    pn, lid = own_identity(settings)
    _own_pn_user, _own_lid_user = _user(pn), _user(lid)
    if not _own_lid_user or not _own_pn_user:
        # Sin identidad propia completa no se puede distinguir quiénes somos,
        # y una guarda que no sabe a quién proteger no se instala.
        log.debug("Sin identidad propia completa; no se instala la guarda de sesion")
        return False

    import pywhats.messaging.receiver as receiver_module

    original = receiver_module.Receiver._migrate_known_lid_sender
    if getattr(original, _MARKER, False):
        return True

    def _migrate_known_lid_sender(self: Any, sender: Any) -> None:  # type: ignore[no-untyped-def]
        if es_dispositivo_propio(sender):
            METRICAS["migraciones_evitadas"] += 1
            # DEBUG y no INFO: pasa una vez por cada copia propia que llega
            # antes de que exista la sesion LID, y en INFO llenaria la consola.
            log.debug(
                "No se migra la sesion de nuestro propio dispositivo (%s): la "
                "direccion por numero sigue en uso para pedir historial",
                getattr(sender, "server", "?"),
            )
            return
        original(self, sender)

    setattr(_migrate_known_lid_sender, _MARKER, True)
    receiver_module.Receiver._migrate_known_lid_sender = _migrate_known_lid_sender  # type: ignore[method-assign]

    log.debug("Guarda de sesion propia instalada (no se migra PN->LID de uno mismo)")
    return True
