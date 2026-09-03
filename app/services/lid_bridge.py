"""Puente entre LID y numero de telefono, para poder resolver nombres.

El problema, medido sobre datos reales: los chats llegan identificados por
``@lid`` (32 de 39) mientras que los nombres del History Sync vienen por
``@s.whatsapp.net``. Sin traduccion, CERO chats casan con un contacto y el
sidebar acaba mostrando el LID en crudo.

pywhats mantiene el mapeo que va aprendiendo en su Signal Store, tabla
``lid_map``::

    CREATE TABLE lid_map (pn_user TEXT PRIMARY KEY, lid_user TEXT NOT NULL UNIQUE)

Este modulo lo lee en SOLO LECTURA y copia lo aprendido a ``contacts.lid``, que
es NUESTRA columna. No se escribe una sola fila en el Signal Store, no se
mueve a PostgreSQL y no se abre en modo escritura mientras el cliente lo usa:
solo se consulta (seccion 19 del brief).

Ojo con las expectativas: el mapeo crece segun WhatsApp lo va revelando. Al
principio puede tener una sola entrada. No se inventa ninguna correspondencia
ni se deduce un telefono a partir de un LID, porque no son convertibles.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.logging_setup import get_logger
from app.models import Contact

log = get_logger("WA")


def read_lid_map(signal_db: Path) -> dict[str, str]:
    """``{lid_user: pn_user}`` leido del Signal Store de pywhats.

    Se abre con URI en modo ``ro``: la conexion es de solo lectura de verdad,
    no por convenio. Cualquier fallo devuelve un mapa vacio, porque no poder
    resolver nombres nunca debe impedir que la aplicacion funcione.
    """
    if not signal_db.exists():
        return {}
    try:
        uri = f"file:{signal_db.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute("SELECT pn_user, lid_user FROM lid_map").fetchall()
    except sqlite3.Error as exc:
        log.debug("No se pudo leer el lid_map de pywhats: %s", exc)
        return {}
    return {lid_user: pn_user for pn_user, lid_user in rows}


def sync_lid_map(session: Session, signal_db: Path) -> int:
    """Copia el mapeo a ``contacts.lid``. Devuelve cuantos contactos actualizo.

    Solo se rellena el hueco: si un contacto ya tenia LID no se toca.
    """
    mapping = read_lid_map(signal_db)
    if not mapping:
        return 0

    updated = 0
    for lid_user, pn_user in mapping.items():
        result = session.execute(
            update(Contact)
            .where(
                Contact.jid == f"{pn_user}@s.whatsapp.net",
                Contact.lid.is_(None),
            )
            .values(lid=f"{lid_user}@lid")
        )
        updated += result.rowcount or 0

    if updated:
        log.info(
            "Mapeo LID aplicado a %d contactos (%d correspondencias conocidas)",
            updated,
            len(mapping),
        )
    else:
        log.debug("El lid_map tiene %d entradas; nada nuevo que aplicar", len(mapping))
    return updated
