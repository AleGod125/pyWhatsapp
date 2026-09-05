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

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
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
    """La clave con la que se busca la sesion. La misma que usa pywhats.

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


def huella_de_sesion(estado: Any) -> str:
    """Huella corta y NO sensible del estado de una sesion.

    Permite ver si dos direcciones guardan el MISMO ratchet o dos distintos
    sin exponer ni un byte de material criptografico: es un hash truncado de
    la serializacion, nunca la serializacion.
    """
    import hashlib

    if estado is None:
        return "-"
    try:
        crudo = estado if isinstance(estado, (bytes, bytearray)) else repr(estado).encode()
    except Exception:  # noqa: BLE001
        return "?"
    return hashlib.sha256(bytes(crudo)).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Auditoria del almacen
# ---------------------------------------------------------------------------


@dataclass
class AuditoriaDeSesiones:
    """Que sesiones existen para MIS propios dispositivos."""

    #: dispositivo -> direccion, indexado por como esta guardado
    por_pn: dict[int, str] = field(default_factory=dict)
    por_lid: dict[int, str] = field(default_factory=dict)
    #: Dispositivos que aparecen por LAS DOS direcciones. Son el problema.
    duplicados: list[int] = field(default_factory=list)
    legible: bool = False

    @property
    def hay_duplicados(self) -> bool:
        return bool(self.duplicados)

    def resumen(self) -> str:
        if not self.legible:
            return "no se pudo leer el Signal Store"
        return (
            f"dispositivos propios: por_numero={sorted(self.por_pn)} "
            f"por_lid={sorted(self.por_lid)} duplicados={sorted(self.duplicados)}"
        )


def auditar_sesiones(
    store: Path, *, own_pn_user: str | None, own_lid_user: str | None
) -> AuditoriaDeSesiones:
    """Lee el Signal Store en SOLO LECTURA y lista mis propias sesiones.

    No abre ninguna sesion, no la interpreta y no la modifica: solo lee los
    nombres de las claves, que dicen dispositivo y direccion.
    """
    auditoria = AuditoriaDeSesiones()
    if not own_pn_user and not own_lid_user:
        return auditoria
    try:
        conexion = sqlite3.connect(
            f"file:{Path(store).as_posix()}?mode=ro", uri=True, timeout=3.0
        )
    except sqlite3.Error:
        log.debug("No se pudo abrir el Signal Store para auditar sesiones propias")
        return auditoria

    try:
        filas = [f[0] for f in conexion.execute("SELECT session_id FROM sessions")]
    except sqlite3.Error:
        log.debug("El Signal Store no tiene tabla de sesiones todavia")
        return auditoria
    finally:
        conexion.close()

    auditoria.legible = True
    for direccion in filas:
        usuario, _, resto = direccion.partition(":")
        dispositivo_texto, _, servidor = resto.partition("@")
        try:
            dispositivo = int(dispositivo_texto)
        except ValueError:
            continue
        if servidor == "lid" and usuario == own_lid_user:
            auditoria.por_lid[dispositivo] = direccion
        elif servidor != "lid" and usuario == own_pn_user:
            auditoria.por_pn[dispositivo] = direccion

    auditoria.duplicados = sorted(set(auditoria.por_pn) & set(auditoria.por_lid))
    return auditoria


def avisar_de_sesiones_duplicadas(settings: Any) -> AuditoriaDeSesiones:
    """Audita y lo cuenta UNA vez, al arrancar. No arregla nada.

    Un dispositivo propio con sesion por numero Y por LID es la explicacion
    medible de que algunas copias del telefono no cuadren. Arreglarlo copiando
    o borrando estado de Signal a ciegas seria peor que el problema: la
    recuperacion correcta ya existe y es el reintento con ``pkmsg``.
    """
    from app.core.identity import own_identity

    pn, lid = own_identity(settings)
    auditoria = auditar_sesiones(
        settings.signal_store_file,
        own_pn_user=usuario_de(pn),
        own_lid_user=usuario_de(lid),
    )
    if not auditoria.legible:
        return auditoria

    log.debug("Auditoria de sesiones propias: %s", auditoria.resumen())
    if auditoria.hay_duplicados:
        log.warning(
            "Tu dispositivo %s tiene DOS sesiones Signal, una por numero y "
            "otra por LID. Son el mismo aparato con dos ratchets distintos, y "
            "por eso alguna copia de lo que escribes desde el telefono puede "
            "no cuadrar. No se toca ninguna: cuando pase, se pide el reenvio y "
            "el mensaje llega autenticado.",
            ", ".join(str(d) for d in auditoria.duplicados),
        )
    return auditoria
