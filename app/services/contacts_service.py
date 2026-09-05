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

    def __init__(self, database: Database) -> None:
        self._database = database
        self.resolved = 0

    # -- Sinks de eventos ----------------------------------------------------

    def handle_contact(self, contact: Any) -> dict[str, Any] | None:
        """Evento ``contact``: nombre real de la agenda del telefono."""
        jid = _jid(getattr(contact, "jid", None))
        if not jid:
            return None

        full_name = getattr(contact, "full_name", "") or None
        first_name = getattr(contact, "first_name", "") or None
        display = full_name or first_name
        if not display:
            return None

        from app.services import repository as repo

        with self._database.transaction() as session:
            repo.upsert_contact(session, jid=jid, display_name=display)
        self.resolved += 1
        # El NOMBRE de un contacto no va a INFO: es un dato personal y ademas
        # una linea por contacto llena la consola. El resumen lo publica
        # ``resumen()`` con la cuenta, sin nombres.
        log.debug("Contacto resuelto")
        return {"jid": jid, "name": display}

    def handle_pushname(self, event: Any) -> dict[str, Any] | None:
        """Evento ``pushname``: nombre publico que el peer se ha puesto."""
        jid = _jid(getattr(event, "jid", None))
        name = getattr(event, "name", "") or None
        if not jid or not name:
            return None

        from app.services import repository as repo

        with self._database.transaction() as session:
            repo.upsert_contact(session, jid=jid, push_name=name)
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
    from pywhats.events import JID

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
