"""Resolucion de nombres de contactos.

El problema medido: 32 de 39 chats llegan identificados por ``@lid``, los
pushnames del History Sync vienen por ``@s.whatsapp.net`` y ninguno casa, asi
que el sidebar acaba mostrando numeros crudos. Ademas ``pushName`` viene vacio
en los 100 mensajes del historial inicial, de modo que por ahi tampoco hay
nombres.

De donde salen los nombres de verdad
------------------------------------
De la sincronizacion de app-state, coleccion ``critical_unblock_low``, que es
la agenda del telefono. pywhats sabe traerla (``AppStateSyncer.fetch``) pero
solo reacciona si el servidor empuja un ``<notification type="server_sync">``:
nunca la pide por iniciativa propia. En los logs se ve al servidor avisando y a
pywhats sin hacer nada::

    ib: dirty type=account_sync ts=... (clean not implemented yet)

Este modulo la pide explicitamente al conectar. No es un parche a pywhats: se
usa su API publica tal cual.
"""

from __future__ import annotations

from typing import Any

from app.core.database import Database
from app.core.logging_setup import get_logger

log = get_logger("WA")

# Colecciones de app-state que contienen nombres.
#   critical_unblock_low -> agenda (mutaciones 'contact')
#   regular_high         -> ajustes con el pushname propio
CONTACT_COLLECTIONS = ("critical_unblock_low", "regular_high")


class ContactService:
    """Guarda los nombres que llegan por app-state."""

    def __init__(self, database: Database, *, whatsapp_account_id: Any = None) -> None:
        self._database = database
        self.resolved = 0
        # DE QUIEN son los contactos que guarda este servicio.
        #
        # La agenda es privada: el nombre que una persona le pone a un numero
        # no puede acabar en la de otra. Se guarda aqui en vez de deducirlo en
        # cada evento porque el servicio ya pertenece a una cuenta -- lo crea
        # el runtime de esa cuenta.
        self.whatsapp_account_id = whatsapp_account_id

    def _cuenta(self, sesion: Any) -> Any:
        """La cuenta de este servicio; si no la recibio, la unica que haya.

        El respaldo es de transicion y se cae solo: en cuanto existan dos
        cuentas devuelve ``None``, que es preferible a atribuirle la agenda de
        alguien a quien no es.
        """
        if self.whatsapp_account_id is not None:
            return self.whatsapp_account_id
        from app.services.account_scope import cuenta_unica

        return cuenta_unica(sesion)

    # -- Sinks de eventos ----------------------------------------------------

    def handle_contact(self, contact: Any) -> dict[str, Any] | None:
        """Evento ``contact``: lo que el telefono sabe de una persona.

        SE GUARDA TODO LO QUE VENGA, no solo el nombre de la agenda.
        ---------------------------------------------------------
        Antes esto exigia un nombre de agenda y, si no lo habia, descartaba el
        evento entero -- con el ``lid`` dentro. Y ese ``lid`` es justo la pieza
        que falta: 59 de 63 conversaciones llegan identificadas por ``@lid``
        mientras que el nombre esta guardado contra el numero, asi que sin la
        correspondencia el panel muestra identificadores aunque el nombre
        exista en la tabla.

        Ahora entra cualquiera de las tres cosas por separado: nombre de
        agenda, nombre publico y correspondencia PN<->LID. ``upsert_contact``
        no degrada lo que ya hubiera, asi que guardar de menos era perder y
        guardar de mas no cuesta nada.
        """
        jid = _jid(getattr(contact, "jid", None))
        if not jid:
            return None

        full_name = getattr(contact, "full_name", "") or None
        first_name = getattr(contact, "first_name", "") or None
        display = full_name or first_name
        push = getattr(contact, "push_name", "") or None
        lid = _jid(getattr(contact, "lid", None))
        # Un evento que no trae NADA util no se escribe: crearia una fila
        # vacia que despues taparia al contacto de verdad.
        if not (display or push or lid):
            return None

        from app.services import repository as repo

        with self._database.transaction() as session:
            repo.upsert_contact(
                session,
                whatsapp_account_id=self._cuenta(session),
                jid=jid,
                display_name=display,
                push_name=push,
                lid=lid,
            )
        self.resolved += 1
        # El NOMBRE de un contacto no va a INFO: es un dato personal y ademas
        # una linea por contacto llena la consola. El resumen lo publica
        # ``resumen()`` con la cuenta, sin nombres.
        log.debug("Contacto resuelto")
        return {"jid": jid, "name": display or push}

    def handle_pushname(self, event: Any) -> dict[str, Any] | None:
        """Evento ``pushname``: nombre publico que el peer se ha puesto."""
        jid = _jid(getattr(event, "jid", None))
        name = getattr(event, "name", "") or None
        if not jid or not name:
            return None

        from app.services import repository as repo

        with self._database.transaction() as session:
            repo.upsert_contact(
                session,
                whatsapp_account_id=self._cuenta(session),
                jid=jid,
                push_name=name,
            )
        self.resolved += 1
        return {"jid": jid, "name": name}


async def fetch_contact_names(client: Any) -> int:
    """Pide las colecciones de app-state que traen nombres.

    pywhats las sincroniza pero solo de forma reactiva; aqui se piden al
    conectar. Un fallo no es critico: sin nombres la aplicacion sigue
    funcionando, solo que el sidebar mostrara identificadores.
    """
    syncer = getattr(client, "_app_state_syncer", None)
    if syncer is None:
        log.debug("No hay AppStateSyncer disponible; no se piden nombres")
        return 0

    total = 0
    for collection in CONTACT_COLLECTIONS:
        try:
            mutations = await syncer.fetch(collection, full_sync=True)
        except Exception as exc:  # noqa: BLE001 - los nombres son un extra
            log.warning("No se pudo sincronizar '%s': %s", collection, exc)
            continue
        count = len(mutations) if mutations is not None else 0
        total += count
        log.info("App-state '%s': %d mutaciones", collection, count)
    return total


async def resolve_lids_via_usync(client: Any, database: Database, *, batch: int = 20) -> int:
    """Aprende el LID de cada contacto preguntandoselo al servidor (usync).

    POR QUE HACE FALTA
    ------------------
    Los nombres llegan por app-state con JID de telefono
    (``...@s.whatsapp.net``) pero 32 de 39 chats vienen identificados por
    ``@lid``. Sin la correspondencia, el sidebar no puede casar unos con otros
    y muestra numeros.

    ``USyncDeviceFetcher`` de pywhats resuelve usuarios y su respuesta
    (``UserSyncEntry``) YA TRAE el campo ``lid``. Lo curioso es que pywhats
    define ``Sender._remember_lid_mapping`` pero no lo invoca en ningun sitio,
    asi que ese dato se descarta y su ``lid_map`` se queda casi vacio.

    Aqui se usa el fetcher tal cual (su API publica) y el LID se guarda en
    NUESTRA columna ``contacts.lid``. No se toca el Signal Store de pywhats.
    """
    from sqlalchemy import select, update
    from app.wa.tipos import JID

    from app.models import Contact

    sender = getattr(client, "_sender", None)
    fetch = getattr(sender, "_fetch_devices", None) if sender is not None else None
    if fetch is None:
        log.debug("Sin fetcher de usync disponible; no se resuelven LIDs")
        return 0

    with database.transaction() as session:
        pending = session.execute(
            select(Contact.jid).where(
                Contact.jid.like("%@s.whatsapp.net"), Contact.lid.is_(None)
            )
        ).scalars().all()

    if not pending:
        log.debug("No hay contactos sin LID que resolver")
        return 0

    resolved = 0
    for start in range(0, len(pending), batch):
        chunk = pending[start : start + batch]
        users = [JID(user=jid.split("@")[0], server="s.whatsapp.net") for jid in chunk]
        try:
            result = await fetch(users)
        except Exception as exc:  # noqa: BLE001 - los nombres son un extra
            log.warning("usync fallo para un lote de %d contactos: %s", len(chunk), exc)
            continue

        updates: list[tuple[str, str]] = []
        for jid, entry in (result or {}).items():
            lid = getattr(entry, "lid", None)
            if lid is None:
                continue
            lid_user = getattr(lid, "user", None) or str(lid).split("@")[0]
            user = getattr(jid, "user", None) or str(jid).split("@")[0]
            if lid_user and user:
                updates.append((f"{user}@s.whatsapp.net", f"{lid_user}@lid"))

        if updates:
            with database.transaction() as session:
                for contact_jid, lid_jid in updates:
                    # SIN ACOTAR POR CUENTA, Y ES CORRECTO. Por que:
                    #
                    # La correspondencia PN<->LID es una propiedad GLOBAL de
                    # WhatsApp, no de una cuenta: un telefono tiene el mismo
                    # LID mire quien lo mire. El valor que escribe una cuenta
                    # es identico al que escribiria cualquier otra.
                    #
                    # Ademas solo rellena NULOS --el `pending` de arriba pide
                    # `Contact.lid IS NULL`-- y solo toca esa columna: ni
                    # nombre, ni mensajes, ni nada que distinga a una persona
                    # de otra. Asi que no puede contaminar: no hay ningun dato
                    # de una cuenta viajando a otra, solo un identificador
                    # publico que ya era cierto para las dos.
                    #
                    # Acotarlo obligaria a que cada cuenta repitiera el mismo
                    # usync para escribir el mismo valor.
                    session.execute(
                        update(Contact)
                        .where(Contact.jid == contact_jid)
                        .values(lid=lid_jid)
                    )
            resolved += len(updates)

    log.info("LIDs resueltos por usync: %d de %d contactos", resolved, len(pending))
    return resolved


def _jid(value: Any) -> str | None:
    """``JID`` de pywhats -> cadena, conservando el servidor tal cual."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    user = getattr(value, "user", None)
    server = getattr(value, "server", None)
    if not user:
        return None
    return f"{user}@{server}" if server else str(user)


    def resumen(self) -> str:
        """Una linea con lo hecho. Sin nombres ni telefonos."""
        return f"Contactos actualizados: {self.resolved}"


def guardar_par_lid(
    database: Database,
    lid: str | None,
    pn: str | None,
    *,
    whatsapp_account_id: Any = None,
) -> bool:
    """Anota la correspondencia PN<->LID que viene en un mensaje. (C-23)

    DE DONDE SALE
    -------------
    Baileys pone el otro lado del par en la clave del mensaje
    (``key.remoteJidAlt`` / ``key.participantAlt``). Es la via mas barata que
    existe para este dato: no cuesta ni una peticion de red y llega sola con
    cada mensaje, mientras que el usync hay que pedirlo.

    Importa porque 56 de 57 conversaciones individuales vienen identificadas
    por ``@lid``: sin la correspondencia, el panel muestra numeros.

    SE CREA LA FILA SI NO EXISTE, y esto fue un fallo real.
    ---------------------------------------------------------
    Antes esto era un ``UPDATE ... WHERE jid = pn``: solo rellenaba el hueco
    de un contacto que YA estuviera en la tabla. Y en una instalacion nueva no
    esta: la agenda entra con el bootstrap inicial, que solo llega al vincular
    de cero. Medido sobre la base real -- 205 chats, 5452 mensajes y CUATRO
    filas en ``contacts``, las cuatro de grupos. Cada par PN<->LID que llegaba
    en un mensaje se descartaba por no tener donde ponerlo, y el panel enseñaba
    identificadores.

    La fila que se crea aqui NO lleva nombre: solo la correspondencia entre dos
    identificadores del mismo telefono. El nombre lo pondra ``handle_contact``
    cuando llegue, y ``upsert_contact`` no degrada lo que ya hubiera.

    Devuelve ``True`` si escribio algo. Nunca lanza.
    """
    from app.services import repository as repo

    if not lid or not pn or not lid.endswith("@lid") or not pn.endswith("@s.whatsapp.net"):
        return False
    if whatsapp_account_id is None:
        # Sin cuenta no se puede crear la fila --``contacts`` la exige-- pero
        # si se puede rellenar el hueco de una que exista.
        return _rellenar_lid(database, lid, pn)
    try:
        with database.transaction() as session:
            repo.upsert_contact(
                session,
                whatsapp_account_id=whatsapp_account_id,
                jid=pn,
                lid=lid,
            )
        return True
    except Exception:  # noqa: BLE001 - un par que no se guarda no para nada
        log.debug("No se pudo guardar el par PN<->LID")
        return False


def _rellenar_lid(database: Database, lid: str, pn: str) -> bool:
    """Rellena el ``lid`` de un contacto que ya exista. Nunca crea."""
    from sqlalchemy import update

    from app.models import Contact

    try:
        with database.transaction() as session:
            cambiadas = session.execute(
                update(Contact)
                .where(Contact.jid == pn, Contact.lid.is_(None))
                .values(lid=lid)
            ).rowcount
        return bool(cambiadas)
    except Exception:  # noqa: BLE001
        log.debug("No se pudo rellenar el LID del contacto")
        return False
