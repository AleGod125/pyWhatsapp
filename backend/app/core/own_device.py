"""De cual de MIS dispositivos viene esta copia, y con que sesion se descifra.

EL PROBLEMA, MEDIDO
-------------------
Los mensajes que el usuario escribe desde el TELEFONO a veces fallan::

    [SIGNAL] mensaje propio no descifrado (mac_fallido, type=msg, dispositivo=0)
    receiver: decrypt failed ... signal message mac check failed

y los que escribe desde WhatsApp Web llegan bien. Mirando el Signal Store real
aparece por que puede pasar eso:

    573002***:0@s.whatsapp.net    <- mi telefono, por numero
    865311***:0@lid               <- MI TELEFONO OTRA VEZ, por LID
    865311***:92@lid              <- el dispositivo vinculado (Web)

``PN:0`` y ``LID:0`` son **el mismo aparato** con **dos estados de Double
Ratchet distintos**. El de Web tiene uno solo, y por eso no falla.

COMO SE LLEGA AHI
-----------------
``migrate_pn_session_to_lid`` mueve la sesion del numero al LID y BORRA la del
numero. Despues, cuando este companion le pide historial a su propio telefono
—que va dirigido al numero, dispositivo 0— ya no encuentra sesion y establece
una nueva por X3DH. A partir de ese momento existen las dos, el telefono
avanza una sola, y los mensajes cifrados con la suya no cuadran con la copia
que quedo guardada bajo el otro nombre.

LO QUE ESTE MODULO HACE
-----------------------
MIRAR y DECIRLO. Clasifica de que dispositivo viene cada copia y audita el
almacen para poder afirmar lo anterior con datos en vez de con una teoria.

LO QUE NO HACE, Y NO VA A HACER
-------------------------------
No copia sesiones, no borra ninguna, no toca ratchets, no deriva claves y no
salta ninguna verificacion. La recuperacion de un mensaje que no cuadra es la
que ya existe y es la correcta: acuse de reintento, el emisor reenvia como
``pkmsg``, X3DH completo, y esa sesion nueva sustituye a la que estaba mal.
"""

from __future__ import annotations

from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("SIGNAL")

#: De donde salio la copia. NO se deduce del JID a secas: hace falta el
#: dispositivo, porque el telefono y un vinculado comparten identificador.
PRIMARY_PHONE = "primary_phone"
LINKED_WEB = "linked_web"
LINKED_UNKNOWN = "linked_unknown"
PEER = "peer"

#: El telefono principal es SIEMPRE el dispositivo 0. Los vinculados reciben
#: numeros de ranura mayores; el que se midio en esta cuenta es el 92.
DISPOSITIVO_PRINCIPAL = 0


def usuario_de(jid: Any) -> str | None:
    """Parte de usuario de un JID, en cadena o como objeto."""
    if jid is None:
        return None
    texto = jid if isinstance(jid, str) else getattr(jid, "user", None)
    if not texto:
        return None
    usuario = str(texto).split("@")[0].split(":")[0].split(".")[0]
    return usuario or None


def clasificar(
    sender: Any, *, own_pn_user: str | None, own_lid_user: str | None
) -> str:
    """De cual de mis dispositivos viene, o de nadie mio.

    La clasificacion usa el DISPOSITIVO de la stanza, no solo el JID. Un
    mensaje del telefono y uno de WhatsApp Web llevan el mismo identificador
    de cuenta y son cosas distintas: llegan por sesiones Signal distintas y
    fallan de maneras distintas.

    ``linked_unknown`` existe a proposito. Que sea de un vinculado no dice
    CUAL, y afirmar "Web" sin saberlo seria inventarselo.
    """
    usuario = usuario_de(sender)
    if not usuario:
        return PEER
    propios = {u for u in (own_pn_user, own_lid_user) if u}
    if usuario not in propios:
        return PEER

    dispositivo = getattr(sender, "device", None)
    if dispositivo is None:
        return LINKED_UNKNOWN
    if int(dispositivo) == DISPOSITIVO_PRINCIPAL:
        return PRIMARY_PHONE
    return LINKED_WEB


def direccion_signal(sender: Any) -> str:
    """La clave con la que se identifica la sesion de un dispositivo.

    ``usuario:dispositivo@servidor``. Se replica aqui para poder registrarla
    ANTES del descifrado sin importar el modulo del receptor en cada linea.
    """
    return (
        f"{getattr(sender, 'user', '?')}:"
        f"{getattr(sender, 'device', 0)}@"
        f"{getattr(sender, 'server', '?')}"
    )


def enmascarar(direccion: str) -> str:
    """Una direccion Signal sin el identificador completo.

    Un JID completo es un numero de telefono. En los registros va truncado.
    """
    usuario, _, resto = direccion.partition(":")
    return f"{usuario[:6]}***:{resto}" if resto else f"{usuario[:6]}***"


# Aqui vivia la auditoria de sesiones de Signal: leia el SQLite de pywhats
# (`sessions`, `identities`, `sender_keys`) para avisar de que un mismo
# telefono tuviera sesion por numero Y por LID a la vez.
#
# Se va con la libreria. Baileys guarda su almacen como ficheros JSON sueltos
# y resuelve la equivalencia LID<->numero por dentro, asi que el duplicado que
# aquello vigilaba ya no puede darse. Nadie lo llamaba fuera de sus pruebas.
