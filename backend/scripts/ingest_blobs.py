"""Ingesta a PostgreSQL de los blobs de History Sync archivados en disco.

Los blobs de ``data/history/`` son los que WhatsApp ya entrego, descargados y
descifrados por pywhats. Reprocesarlos NO requiere conexion: sirve para
recuperar historial capturado antes de que existiera el pipeline, y para
reinterpretar mensajes cuando mejora el normalizador, sin volver a pedir nada
al servidor.

LO MISMO QUE HACE EL BOTON
--------------------------
El trabajo de verdad vive en ``app.services.blob_reingest``, que es lo que
ejecuta la fase ``archive`` de la revision completa. Aqui solo esta la linea
de comandos. Dos implementaciones de esto serian dos criterios distintos sobre
de quien es cada conversacion, y ese es justo el punto donde no puede haber
dos opiniones.

POR QUE SE PIDE LA CUENTA
-------------------------
La version anterior de este script llamaba a ``ingest_history_sync`` **sin**
``whatsapp_account_id``. Los mensajes entraban en la base sin dueno, y el
filtro de propiedad los excluye: quedaban guardados y no los veia nadie. Con
una sola cuenta vinculada se elige sola; con varias hay que decir cual.

Uso:
    py scripts/ingest_blobs.py              ingiere todos los blobs
    py scripts/ingest_blobs.py --dry-run    analiza sin escribir en la base
    py scripts/ingest_blobs.py --cuenta ID  cuando hay varias vinculadas
"""

from __future__ import annotations

# Estas herramientas se invocan como ``py scripts/<nombre>.py``, y en ese caso
# Python pone ``scripts/`` en sys.path, no la raiz del proyecto: ``import app``
# fallaria. Se anade la raiz explicitamente antes de importar nada nuestro.
import sys as _sys
from pathlib import Path as _Path

_RAIZ = _Path(__file__).resolve().parent.parent
if str(_RAIZ) not in _sys.path:
    _sys.path.insert(0, str(_RAIZ))

import argparse
import json
from pathlib import Path

from sqlalchemy import select

from app.wa.historial import parse_full, parse_full_json
from app.core.config import load_settings
from app.core.database import Database, DatabaseError
from app.core.logging_setup import get_logger, setup_logging
from app.models import WhatsAppAccount
from app.services.blob_reingest import (
    carpeta_de_blobs,
    carpeta_de_blobs_baileys,
    reingerir_blobs,
)

log = get_logger("SYNC")


def own_jid_from_session(session_file: Path) -> str | None:
    """JID propio leido del DeviceStore, para los mensajes con ``fromMe``."""
    if not session_file.exists():
        return None
    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
        jid = data.get("jid")
        if isinstance(jid, dict) and jid.get("user"):
            return f"{jid['user']}@{jid.get('server', 's.whatsapp.net')}"
    except (OSError, ValueError) as exc:
        log.warning("No se pudo leer el JID propio: %s", exc)
    return None


def _elegir_cuenta(database, pedida: str | None) -> object | None:
    """La cuenta a la que van los mensajes. ``None`` si no se puede decidir."""
    with database.transaction() as sesion:
        cuentas = list(sesion.execute(select(WhatsAppAccount.id)).scalars())

    if pedida:
        for cuenta in cuentas:
            if str(cuenta) == pedida or str(cuenta).startswith(pedida):
                return cuenta
        log.error("No hay ninguna cuenta que empiece por %s", pedida)
        return None

    if len(cuentas) == 1:
        return cuentas[0]
    if not cuentas:
        log.error(
            "No hay ninguna cuenta de WhatsApp vinculada. Los mensajes sin "
            "dueno quedan en la base sin que los vea nadie, asi que no se "
            "ingiere nada."
        )
        return None
    log.error(
        "Hay %d cuentas vinculadas: indica cual con --cuenta. Las opciones "
        "son: %s",
        len(cuentas),
        ", ".join(str(c)[:8] for c in cuentas),
    )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingesta de blobs de History Sync")
    parser.add_argument("--dry-run", action="store_true", help="no escribir en la base")
    parser.add_argument("--cuenta", help="id (o prefijo) de la cuenta destino")
    args = parser.parse_args(argv)

    settings = load_settings()
    setup_logging(settings.log_level)

    # Las DOS epocas: pywhats (protobuf, `.pb`) y Baileys (JSON). Antes esto
    # solo miraba la primera, asi que un enlace fresco sin blobs `.pb` --el
    # caso normal hoy-- decia "No hay blobs" aunque `data/history_baileys/`
    # tuviera el bootstrap entero esperando.
    blobs: list[tuple[Path, str]] = []
    carpeta_pb = carpeta_de_blobs(settings)
    blobs += [(b, "pb") for b in sorted(carpeta_pb.glob("*.pb"))]
    carpeta_json = carpeta_de_blobs_baileys(settings)
    blobs += [(b, "json") for b in sorted(carpeta_json.glob("*.json"))]
    if not blobs:
        log.warning(
            "No hay blobs en %s ni en %s", carpeta_pb, carpeta_json
        )
        return 1

    if args.dry_run:
        # Mirar sin escribir: se dice que hay en cada archivo y se para.
        total = 0
        for blob, formato in blobs:
            try:
                if formato == "pb":
                    sync = parse_full(blob.read_bytes())
                else:
                    sync = parse_full_json(json.loads(blob.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                log.warning("%s: ilegible", blob.name)
                continue
            total += sync.message_count
            log.info(
                "%s: type=%s conversaciones=%d mensajes=%d",
                blob.name,
                sync.sync_type,
                len(sync.conversations),
                sync.message_count,
            )
        log.info("Dry-run: %d blobs, %d mensajes. No se ha escrito nada", len(blobs), total)
        return 0

    try:
        database = Database(settings)
        database.connect()
    except DatabaseError as exc:
        log.error("%s", exc)
        return 3

    try:
        cuenta = _elegir_cuenta(database, args.cuenta)
        if cuenta is None:
            return 2
        log.info("%d blob(s) -> cuenta %s", len(blobs), str(cuenta)[:8])
        resultado = reingerir_blobs(
            database,
            settings,
            account_id=cuenta,
            own_jid=own_jid_from_session(settings.session_file),
        )
    finally:
        database.dispose()

    log.info("TOTAL: %s", resultado)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
