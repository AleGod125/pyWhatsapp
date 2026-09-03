"""Prueba de extraccion historica dirigida a UN chat concreto.

    python probe_chat.py --name "Tia Nore"
    python probe_chat.py --chat 820255...@lid --rounds 20

No usa el selector automatico de canary: apunta al chat que se le indica y
excava hacia atras mientras haya progreso.

NO modifica el wire protocol: reutiliza tal cual el mecanismo ON_DEMAND ya
validado (shape=bare, category=peer, enc_count=1, destino PN device=0,
timestamp en segundos, count=50).

Sin GUI. Todo lo que hace se ve en el log.

HERRAMIENTA DE DIAGNOSTICO (seccion 35). El mantenimiento normal lo hace
``main.py`` solo, a traves de ``MaintenanceService``. Esto se ejecuta a mano
cuando hace falta mirar por dentro; el uso corriente no lo necesita.
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
import queue
from datetime import datetime, timezone

from sqlalchemy import text

from app.services import repository as repo
from app.core.config import load_settings
from app.core.database import Database
from app.core.logging_setup import get_logger, setup_logging
from app.whatsapp_client import ClientEvent, WhatsAppClient, prepare_pywhats

log = get_logger("BACKFILL")


def fmt(value: int | None) -> str:
    if not value:
        return "-"
    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def resolve_chat(database: Database, *, name: str | None, jid: str | None):
    with database.engine.connect() as c:
        if jid:
            row = c.execute(
                text("SELECT id, jid FROM chats WHERE jid = :j"), {"j": jid}
            ).first()
            return (row[0], row[1], jid) if row else None
        row = c.execute(
            text(
                "SELECT ch.id, ch.jid, co.display_name FROM chats ch "
                "JOIN contacts co ON co.jid = ch.jid OR co.lid = ch.jid "
                "WHERE co.display_name ILIKE :n "
                "ORDER BY (SELECT COUNT(*) FROM messages m WHERE m.chat_id = ch.id) DESC "
                "LIMIT 1"
            ),
            {"n": f"%{name}%"},
        ).first()
        return (row[0], row[1], row[2]) if row else None


def snapshot(database: Database, chat_jid: str) -> tuple[int, int | None, object]:
    session = database.session()
    try:
        return (
            repo.count_messages(session, chat_jid),
            repo.get_oldest_stored_timestamp(session, chat_jid),
            repo.get_oldest_valid_history_cursor(session, chat_jid),
        )
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extraccion dirigida a un chat")
    parser.add_argument("--name", help="nombre del contacto")
    parser.add_argument("--chat", help="JID exacto")
    parser.add_argument("--rounds", type=int, default=40, help="rondas maximas")
    args = parser.parse_args(argv)
    if not (args.name or args.chat):
        parser.error("indica --name o --chat")

    settings = load_settings()
    setup_logging(settings.log_level)
    settings.ensure_directories()

    database = Database(settings)
    database.connect()

    found = resolve_chat(database, name=args.name, jid=args.chat)
    if found is None:
        log.error("Chat no encontrado")
        database.dispose()
        return 1
    chat_id, chat_jid, label = found

    before_count, before_oldest, before_cursor = snapshot(database, chat_jid)
    user, _, server = chat_jid.partition("@")

    print()
    print("=" * 58)
    print(f"[PRUEBA] START  chat={label}")
    print(f"[PRUEBA] chat_jid={user[:6]}***@{server}")
    print(f"[PRUEBA] before_count={before_count}")
    print(f"[PRUEBA] before_oldest={fmt(before_oldest)}")
    if before_cursor is None:
        print("[PRUEBA] cursor_id=NINGUNO -> no hay ancla real, no se puede pedir")
        database.dispose()
        return 2
    print(f"[PRUEBA] cursor_id={before_cursor.message_id}")
    print(f"[PRUEBA] cursor_timestamp={before_cursor.timestamp} ({fmt(before_cursor.timestamp)})")
    print(f"[PRUEBA] count={settings.history_on_demand_count}")
    print("=" * 58)
    print()

    # -- Cableado minimo: cliente + ingesta + backfill dirigido -------------
    from app.services.backfill_service import BackfillService
    from app.compat import history_compat
    from app.services.history_gate import InitialHistoryGate
    from app.services.history_service import ingest_history_sync

    events: queue.Queue[ClientEvent] = queue.Queue()
    client = WhatsAppClient(settings, events)
    backfill = BackfillService(settings, database)
    gate = InitialHistoryGate(settle_seconds=settings.history_settle_seconds, max_wait=60.0)

    prepare_pywhats(settings)

    own_jid = None
    if settings.session_file.exists():
        import json

        try:
            data = json.loads(settings.session_file.read_text(encoding="utf-8"))
            jid = data.get("jid") or {}
            if jid.get("user"):
                own_jid = f"{jid['user']}@{jid.get('server', 's.whatsapp.net')}"
        except (OSError, ValueError):
            pass

    def ingest(full) -> None:
        gate.note_history_sync(full.sync_type)
        backfill.notify_history(full)
        try:
            with database.transaction() as session:
                result = ingest_history_sync(
                    session, full, own_jid=own_jid, signal_db=settings.signal_store_file
                )
            log.info("Ingerido: %s", result)
        except Exception:  # noqa: BLE001 - el blob queda archivado en disco
            log.exception("Fallo al persistir; el blob sigue en data/history/")

    history_compat.set_callback(ingest)

    resultado: dict = {}

    async def run(pywhats_client) -> None:
        # Se espera al historial inicial: sin cursores no hay nada que pedir.
        await gate.wait()
        backfill._client = pywhats_client
        backfill.refresh_own_identity()

        if not backfill.is_backfill_candidate(chat_jid):
            log.error("Ese chat no es candidato valido (identidad propia o difusion)")
            return

        log.info("Excavando hacia atras en %s (max %d rondas)", label, args.rounds)
        await backfill._process_chat(chat_id, chat_jid, args.rounds)
        resultado["stats"] = backfill.stats
        resultado["end_type"] = backfill._last_end_type

    client.post_connect = run
    client.start()

    # Espera a que termine el trabajo posterior a la conexion.
    import time

    deadline = time.monotonic() + 60 + args.rounds * (settings.history_request_timeout + 5)
    while time.monotonic() < deadline and "stats" not in resultado:
        time.sleep(1.0)
        try:
            while True:
                event = events.get_nowait()
                if event.name in ("client_error", "logged_out"):
                    log.error("Sesion terminada: %s", event.name)
                    deadline = 0
                    break
        except queue.Empty:
            pass

    client.stop()

    after_count, after_oldest, after_cursor = snapshot(database, chat_jid)
    with database.engine.connect() as c:
        estado = c.execute(
            text("SELECT history_status FROM chat_history_state WHERE chat_jid = :j"),
            {"j": chat_jid},
        ).scalar_one_or_none()

    stats = resultado.get("stats")
    end_type = resultado.get("end_type")
    from app.services.backfill_service import _END_TYPES

    print()
    print("=" * 58)
    print("TIA NORE / RESULTADO")
    print("=" * 58)
    print(f"  before_count   = {before_count}")
    print(f"  before_oldest  = {fmt(before_oldest)}")
    print()
    if stats is None:
        print("  rondas         = 0  (no llego a ejecutarse)")
    else:
        print(f"  peticiones     = {stats.requests_sent}")
        print(f"  respuestas     = {stats.responses_received}")
        print(f"  timeouts       = {stats.timeouts}")
        print(f"  mensajes_nuevos= {stats.messages_new}")
    print()
    print(f"  after_count    = {after_count}")
    print(f"  after_oldest   = {fmt(after_oldest)}")
    retrocedio = (
        after_oldest is not None
        and before_oldest is not None
        and after_oldest < before_oldest
    )
    print(f"  cursor_retrocedio = {'SI' if retrocedio else 'NO'}")
    print()
    if end_type is not None:
        print(f"  endOfHistoryTransferType = {end_type} ({_END_TYPES.get(end_type, '?')})")
    else:
        print("  endOfHistoryTransferType = (no recibido)")
    print(f"  status final   = {estado}")
    print()
    if stats and stats.timeouts and not stats.responses_received:
        print("  VEREDICTO: TIMEOUT. El telefono no respondio.")
        print("             NO significa que el historial este agotado.")
        print("             Comprueba que el movil este encendido y con datos.")
    elif retrocedio:
        print(f"  VEREDICTO: PROGRESO REAL. +{after_count - before_count} mensajes,")
        print("             el historial retrocedio.")
    elif stats and stats.responses_received:
        print("  VEREDICTO: WhatsApp respondio pero no ofrecio mas historial")
        print("             para ese cursor.")
    else:
        print("  VEREDICTO: sin datos concluyentes.")
    print("=" * 58)

    database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
