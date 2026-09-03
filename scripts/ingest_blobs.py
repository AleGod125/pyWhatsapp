"""Ingesta a PostgreSQL de los blobs de History Sync archivados en disco.

Los blobs de ``data/history/`` son los que WhatsApp ya entrego, descargados y
descifrados por pywhats. Reprocesarlos NO requiere conexion: sirve para
recuperar historial capturado antes de que existiera el pipeline, y para
reinterpretar mensajes cuando mejora el normalizador, sin volver a pedir nada
al servidor.

Uso:
    python ingest_blobs.py              ingiere todos los blobs
    python ingest_blobs.py --dry-run    analiza sin escribir en la base
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
from pathlib import Path

from app.compat.history_compat import parse_full
from app.core.config import load_settings
from app.core.database import Database, DatabaseError
from app.services.history_service import IngestResult, ingest_history_sync
from app.core.logging_setup import get_logger, setup_logging

log = get_logger("SYNC")


def own_jid_from_session(session_file: Path) -> str | None:
    """JID propio leido del DeviceStore, para los mensajes con ``fromMe``."""
    if not session_file.exists():
        return None
    import json

    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
        jid = data.get("jid")
        if isinstance(jid, dict) and jid.get("user"):
            return f"{jid['user']}@{jid.get('server', 's.whatsapp.net')}"
    except (OSError, ValueError) as exc:
        log.warning("No se pudo leer el JID propio: %s", exc)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingesta de blobs de History Sync")
    parser.add_argument("--dry-run", action="store_true", help="no escribir en la base")
    args = parser.parse_args(argv)

    settings = load_settings()
    setup_logging(settings.log_level)

    blob_dir = settings.data_dir / "history"
    blobs = sorted(blob_dir.glob("*.pb"))
    if not blobs:
        log.warning("No hay blobs en %s", blob_dir)
        return 1

    own_jid = own_jid_from_session(settings.session_file)
    log.info("%d blobs; JID propio %s", len(blobs), "detectado" if own_jid else "desconocido")

    try:
        database = Database(settings)
        database.connect()
    except DatabaseError as exc:
        log.error("%s", exc)
        return 3

    total = IngestResult()
    try:
        for blob in blobs:
            sync = parse_full(blob.read_bytes())
            log.info(
                "%s: type=%s conversaciones=%d mensajes=%d",
                blob.name,
                sync.sync_type,
                len(sync.conversations),
                sync.message_count,
            )
            if args.dry_run:
                continue
            # Una transaccion por blob: si uno falla, no arrastra a los demas.
            with database.transaction() as session:
                result = ingest_history_sync(
                    session, sync, own_jid=own_jid,
                    signal_db=settings.signal_store_file,
                )
            total.conversations += result.conversations
            total.messages_seen += result.messages_seen
            total.messages_inserted += result.messages_inserted
            total.messages_unparsable += result.messages_unparsable
            total.media_detected += result.media_detected
            total.pushnames += result.pushnames
    finally:
        database.dispose()

    if args.dry_run:
        log.info("Dry-run: no se ha escrito nada")
    else:
        log.info("TOTAL: %s", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
