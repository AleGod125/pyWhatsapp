"""Registrar nuestro propio par PN <-> LID en el mapa de Signal.

EL PROBLEMA, MEDIDO
-------------------
Los mensajes que el usuario envia DESDE SU TELEFONO llegan al companion (es la
copia que WhatsApp reparte a los dispositivos vinculados), pero mueren antes de
tocar nuestro codigo::

    receiver: decrypt failed id=ACD24F01... from=86531142340710@lid type=msg:
              no session for peer 86531142340710@lid

``86531142340710@lid`` es NUESTRO PROPIO LID. Y la sesion con ese dispositivo
SI existe: esta guardada bajo la otra direccion, la de telefono::

    sessions:  573002389304:0@s.whatsapp.net   <- nuestro PN, el telefono
               64940106866902:0@lid            <- Isaac

Es el mismo dispositivo con dos identificadores. La busqueda falla porque va
por la direccion literal.

POR QUE NO MIGRA SOLO
---------------------
pywhats YA sabe migrar una sesion de PN a LID: lo hace
``_migrate_known_lid_sender``, que consulta el ``lid_map``. Pero ese mapa se
aprende de los atributos ``sender_pn`` / ``sender_lid`` de los stanzas
entrantes, y para nuestro propio dispositivo no llego nunca. Se comprobo: el
mapa tenia UNA sola entrada, la de Isaac::

    pn_user=573243116421  ->  lid_user=64940106866902

La nuestra no estaba.

LA SOLUCION
-----------
Sembrar el mapa con nuestro propio par, que NO hay que adivinar: lo persistio
el propio pairing en ``device.json`` (``jid`` y ``lid``). A partir de ahi el
mecanismo de pywhats hace el resto: al llegar un mensaje desde nuestro LID,
migra la sesion que ya existe bajo el PN y descifra.

QUE NO SE TOCA
--------------
Nada de criptografia. No se crea ninguna sesion, ni se deriva ninguna clave,
ni se salta ninguna verificacion. Se escribe UNA fila en una tabla de
correspondencia entre dos identificadores, con datos que ya son nuestros.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("SIGNAL")


def store_fingerprint(store: Any) -> str:
    """Huella corta y NO sensible del Signal Store.

    Sirve para comprobar que la siembra, el cliente y el receptor hablan del
    MISMO archivo. La ruta completa no se registra: puede llevar el nombre de
    usuario del sistema. Se deriva de la ruta absoluta resuelta, asi que dos
    formas distintas de nombrar el mismo archivo dan la misma huella.
    """
    import hashlib
    from pathlib import Path as _Path

    try:
        resuelta = _Path(str(store)).resolve()
    except OSError:
        resuelta = _Path(str(store))
    return hashlib.sha256(str(resuelta).lower().encode()).hexdigest()[:8]


def _user(jid: str | None) -> str | None:
    """Parte de usuario de un JID. ``None`` si no hay nada utilizable."""
    if not jid:
        return None
    usuario = jid.split("@")[0].split(":")[0].split(".")[0]
    return usuario or None


def seed(settings: Any, *, lid_hint: str | None = None) -> bool:
    """Escribe el par PN<->LID propio en el ``lid_map`` del Signal Store.

    Devuelve ``True`` si el mapa quedo con nuestro par. Nunca lanza: sin este
    apano el sistema sigue funcionando exactamente como antes (los mensajes
    salientes no se descifran), asi que un fallo aqui no puede impedir el
    arranque.

    Es idempotente: ``SqliteLidMap.set`` limpia cualquier par conflictivo
    antes de insertar, y aqui se comprueba primero si ya esta.

    EL LID PUEDE NO ESTAR TODAVIA EN EL DISCO
    -----------------------------------------
    En una vinculacion nueva el LID no llega con el ``pair-success``: llega
    DESPUES, en el ``<success>`` del servidor. Se midio en un pairing limpio::

        15:45:17  post_connect -> "Sin identidad propia completa todavia"
        15:45:18  activator: <success> ... lid=8653...@lid

    Un segundo de diferencia, y en ese segundo la siembra ya habia fallado y
    no habia nadie que la repitiera. Por eso se acepta ``lid_hint``: el
    dispositivo VIVO conoce el LID en cuanto llega el ``<success>``, sin
    depender de cuando se escriba ``device.json``.

    El indicio solo se usa si el disco no lo tiene; nunca lo sobrescribe.
    """
    from app.core.identity import own_identity

    pn_jid, lid_jid = own_identity(settings)
    pn_user, lid_user = _user(pn_jid), _user(lid_jid)
    if not lid_user and lid_hint:
        lid_user = _user(lid_hint)
        if lid_user:
            log.debug("LID tomado del dispositivo vivo; el disco no lo tenia aun")
    if not pn_user or not lid_user:
        log.debug("Sin identidad propia completa todavia; no se siembra el lid_map")
        return False

    store: Path = settings.signal_store_file
    if not store.exists():
        # Aun no hay Signal Store: es un pairing nuevo. Se sembrara en el
        # siguiente arranque, cuando ya exista.
        log.debug("Signal Store todavia no existe; no se siembra el lid_map")
        return False

    try:
        conexion = sqlite3.connect(str(store), timeout=5.0)
    except sqlite3.Error as exc:
        log.debug("No se pudo abrir el Signal Store para sembrar el lid_map: %s", exc)
        return False

    try:
        existe = conexion.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='lid_map'"
        ).fetchone()
        if not existe:
            log.debug("El Signal Store no tiene lid_map todavia")
            return False

        actual = conexion.execute(
            "SELECT lid_user FROM lid_map WHERE pn_user = ?", (pn_user,)
        ).fetchone()
        if actual and str(actual[0]) == lid_user:
            log.debug("El par PN<->LID propio ya estaba registrado")
            conexion.close()
            return verify(store, pn_user, lid_user)

        # Mismo comportamiento que ``SqliteLidMap.set``: el mapa es
        # bidireccional, asi que se limpia cualquier par en conflicto por
        # cualquiera de los dos lados antes de insertar, todo en una
        # transaccion para no dejarlo a medias.
        with conexion:
            conexion.execute(
                "DELETE FROM lid_map WHERE pn_user = ? OR lid_user = ?",
                (pn_user, lid_user),
            )
            conexion.execute(
                "INSERT INTO lid_map (pn_user, lid_user) VALUES (?, ?)",
                (pn_user, lid_user),
            )
    except sqlite3.Error as exc:
        log.warning("No se pudo sembrar el lid_map propio: %s", exc)
        return False
    finally:
        conexion.close()

    return verify(store, pn_user, lid_user)


def verify(store: Path, pn_user: str, lid_user: str) -> bool:
    """Comprueba que el mapa resuelve de VERDAD, y lo dice.

    No basta con haber escrito la fila: lo que importa es que consultar
    nuestro LID devuelva nuestro PN, que es exactamente lo que hara
    ``_migrate_known_lid_sender`` cuando llegue un mensaje nuestro. Si no
    resuelve, NO se anuncia como aplicada: un "compat activa" que miente es
    peor que no tenerla, porque manda a buscar el fallo a otro sitio.

    Nunca se registra el identificador completo: es un numero de telefono.
    """
    try:
        conexion = sqlite3.connect(
            f"file:{store.as_posix()}?mode=ro", uri=True, timeout=5.0
        )
    except sqlite3.Error as exc:
        log.error("No se pudo verificar el lid_map propio: %s", exc)
        return False

    try:
        fila = conexion.execute(
            "SELECT pn_user FROM lid_map WHERE lid_user = ?", (lid_user,)
        ).fetchone()
    except sqlite3.Error as exc:
        log.error("No se pudo verificar el lid_map propio: %s", exc)
        return False
    finally:
        conexion.close()

    resuelve = bool(fila) and str(fila[0]) == pn_user
    # Detalle de la comprobacion: a DEBUG. Lo que importa en INFO es si el
    # mapa resuelve o no, y eso se dice abajo en una sola linea.
    log.debug(
        "identidad propia lista pn=%s lid=%s store_fp=%s resuelve=%s",
        bool(pn_user),
        bool(lid_user),
        store_fingerprint(store),
        resuelve,
    )

    if not resuelve:
        log.error(
            "El lid_map propio NO resuelve: los mensajes que envies desde el "
            "telefono llegaran cifrados y no se podran leer ('no session for "
            "peer <tu propio LID>'). La compatibilidad NO se da por aplicada."
        )
        return False

    log.info(
        "Par PN<->LID propio registrado: los mensajes que envies desde el "
        "telefono ya podran descifrarse"
    )
    return True


def apply(settings: Any) -> bool:
    """Punto de entrada de la capa de compatibilidad."""
    return seed(settings)
