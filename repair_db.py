"""Reparacion quirurgica de datos demostradamente erroneos.

    python repair_db.py --dry-run    detecta y reporta, NO modifica nada
    python repair_db.py --apply      aplica solo las reglas deterministas

Principios (secciones 1, 38 y 39 del encargo):

* Nada se borra sin haberlo demostrado antes con un SELECT.
* Solo se tocan filas que cumplen una regla determinista y verificable.
* Lo dudoso NO se borra: es mejor conservar una fila rara que destruir un
  mensaje real.
* Todo ocurre en una transaccion, con conteos antes y despues.
* ``raw_proto`` no se modifica jamas: es la referencia para diagnosticar.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from app.config import load_settings
from app.database import Database
from app.logging_setup import get_logger, setup_logging
from inspect_db import mask, own_identity

log = get_logger("DB")


def find_self_protocol_rows(connection: Any, self_jids: list[str]) -> list[dict]:
    """Filas del chat propio que son control del protocolo, no mensajes.

    La regla es deliberadamente estrecha y se apoya en la evidencia de la
    auditoria: TODAS las filas contaminadas comparten exactamente esta forma.

        chat_jid   = nuestra propia identidad (PN o LID)
        source     = 'live'          (llegaron por el evento message)
        type       = 'unknown'       (sin texto ni adjunto reconocible)
        text       IS NULL
        raw_proto  IS NULL           (el evento live no lo exponia)
        from_me    = false

    Un mensaje real que te hayas enviado a ti mismo NO cumple esto: tendria
    texto, o adjunto, o from_me=true. Por eso la regla no puede llevarse por
    delante contenido legitimo.
    """
    if not self_jids:
        return []
    rows = connection.execute(
        text(
            "SELECT id, chat_jid, message_type, source, timestamp "
            "FROM messages "
            "WHERE chat_jid = ANY(:jids) "
            "  AND source = 'live' "
            "  AND message_type = 'unknown' "
            "  AND text IS NULL "
            "  AND raw_proto IS NULL "
            "  AND from_me = false"
        ),
        {"jids": self_jids},
    ).all()
    return [
        {"id": r[0], "chat_jid": r[1], "type": r[2], "source": r[3], "ts": r[4]}
        for r in rows
    ]


def find_self_real_rows(connection: Any, self_jids: list[str]) -> int:
    """Filas del chat propio que SI son mensajes reales. No se tocan."""
    if not self_jids:
        return 0
    return connection.execute(
        text(
            "SELECT COUNT(*) FROM messages WHERE chat_jid = ANY(:jids) "
            "AND NOT (source = 'live' AND message_type = 'unknown' "
            "         AND text IS NULL AND raw_proto IS NULL AND from_me = false)"
        ),
        {"jids": self_jids},
    ).scalar_one()


def find_pn_lid_aliases(connection: Any) -> list[dict]:
    """Contactos con PN y LID conocidos que tienen chat en AMBOS espacios.

    Solo se listan los que tienen un mapeo CONFIRMADO en ``contacts.lid``
    (aprendido por usync). Nunca se fusiona por nombre: dos personas pueden
    llamarse igual (seccion 27).
    """
    rows = connection.execute(
        text(
            "SELECT co.jid AS pn, co.lid AS lid, co.display_name, "
            "       (SELECT COUNT(*) FROM messages m WHERE m.chat_jid = co.jid) AS n_pn, "
            "       (SELECT COUNT(*) FROM messages m WHERE m.chat_jid = co.lid) AS n_lid "
            "FROM contacts co "
            "WHERE co.lid IS NOT NULL "
            "  AND EXISTS (SELECT 1 FROM chats c WHERE c.jid = co.jid) "
            "  AND EXISTS (SELECT 1 FROM chats c WHERE c.jid = co.lid)"
        )
    ).all()
    return [
        {"pn": r[0], "lid": r[1], "name": r[2], "n_pn": r[3], "n_lid": r[4]}
        for r in rows
    ]


def find_protocol_typed_rows(connection: Any) -> int:
    """Mensajes ya guardados con tipo de protocolo, fuera del chat propio.

    Solo se CUENTAN: fuera del chat propio un ``protocol`` puede corresponder
    a un evento que WhatsApp si muestra, asi que no se borran sin analizar.
    """
    return connection.execute(
        text("SELECT COUNT(*) FROM messages WHERE message_type IN ('protocol','senderkey')")
    ).scalar_one()


def find_inconsistent_history_state(connection: Any) -> int:
    """Chats cuyo contador no coincide con los mensajes reales."""
    return connection.execute(
        text(
            "SELECT COUNT(*) FROM chat_history_state hs "
            "WHERE hs.message_count <> "
            "  (SELECT COUNT(*) FROM messages m WHERE m.chat_jid = hs.chat_jid)"
        )
    ).scalar_one()


def find_stale_previews(connection: Any, self_jids: list[str]) -> int:
    """Chats cuya vista previa proviene de una fila que se va a eliminar."""
    if not self_jids:
        return 0
    return connection.execute(
        text("SELECT COUNT(*) FROM chats WHERE jid = ANY(:jids)"), {"jids": self_jids}
    ).scalar_one()


def run(database: Database, settings: Any, *, apply: bool) -> dict:
    own_pn, own_lid = own_identity(settings)
    self_jids = [j for j in (own_pn, own_lid) if j]
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if apply else "dry-run",
        "own_pn_present": bool(own_pn),
        "own_lid_present": bool(own_lid),
    }

    print("=" * 62)
    print(f"REPAIR DB  [{'APPLY' if apply else 'DRY-RUN'}]")
    print("=" * 62)

    with database.engine.connect() as c:
        total_before = c.execute(text("SELECT COUNT(*) FROM messages")).scalar_one()
        print(f"\nMensajes antes: {total_before}")
        report["messages_before"] = total_before

        print("\nHALLAZGOS")
        print("-" * 40)

        self_protocol = find_self_protocol_rows(c, self_jids)
        self_real = find_self_real_rows(c, self_jids)
        print(f"  Self-chat contaminado (control interno) : {len(self_protocol)}")
        print(f"  Self-chat mensajes reales (se conservan): {self_real}")
        report["self_protocol_rows"] = len(self_protocol)
        report["self_real_rows"] = self_real
        for jid in self_jids:
            n = sum(1 for r in self_protocol if r["chat_jid"] == jid)
            print(f"     {mask(jid):<26} {n}")

        aliases = find_pn_lid_aliases(c)
        print(f"  Alias PN/LID con chat en ambos espacios : {len(aliases)}")
        report["pn_lid_aliases"] = len(aliases)
        for a in aliases:
            print(
                f"     {(a['name'] or '?')[:22]:<24} "
                f"pn={mask(a['pn'])}({a['n_pn']})  lid={mask(a['lid'])}({a['n_lid']})"
            )

        protocol_typed = find_protocol_typed_rows(c)
        print(f"  Mensajes con tipo de protocolo          : {protocol_typed}  (solo se cuentan)")
        report["protocol_typed"] = protocol_typed

        inconsistent = find_inconsistent_history_state(c)
        print(f"  chat_history_state con contador desfasado: {inconsistent}")
        report["inconsistent_history_state"] = inconsistent

        previews = find_stale_previews(c, self_jids)
        print(f"  Chats propios a recalcular vista previa  : {previews}")

    print("\nACCIONES")
    print("-" * 40)
    if not self_protocol and not inconsistent:
        print("  Nada que reparar.")
    else:
        print(f"  1. Eliminar {len(self_protocol)} filas de control del chat propio")
        print("     (source=live + type=unknown + text NULL + raw_proto NULL + from_me=false)")
        print(f"  2. Recalcular vista previa y contadores de {previews} chat(s) propio(s)")
        print(f"  3. Recalcular chat_history_state desfasado: {inconsistent} chat(s)")
    print("\n  NO se tocan: raw_proto, media, blobs, sesion, otros chats,")
    print("               mensajes 'unknown' fuera del chat propio.")

    if not apply:
        print("\n[DRY-RUN] No se ha modificado nada.")
        report["applied"] = False
        return report

    if not self_protocol and not inconsistent:
        report["applied"] = False
        return report

    # -- Aplicacion, en UNA transaccion ------------------------------------
    ids = [r["id"] for r in self_protocol]
    with database.engine.begin() as c:
        if ids:
            c.execute(
                text("DELETE FROM messages WHERE id = ANY(:ids)"), {"ids": ids}
            )
        # Vista previa y contadores derivados de los mensajes que QUEDAN.
        c.execute(
            text(
                "UPDATE chats SET last_message = NULL, last_message_timestamp = NULL "
                "WHERE jid = ANY(:jids) AND NOT EXISTS "
                "  (SELECT 1 FROM messages m WHERE m.chat_jid = chats.jid)"
            ),
            {"jids": self_jids or [""]},
        )
        c.execute(
            text(
                "UPDATE chat_history_state hs SET message_count = "
                "  (SELECT COUNT(*) FROM messages m WHERE m.chat_jid = hs.chat_jid)"
            )
        )

    with database.engine.connect() as c:
        total_after = c.execute(text("SELECT COUNT(*) FROM messages")).scalar_one()
    print(f"\nMensajes despues: {total_after}  (antes {total_before}, -{total_before - total_after})")
    report["messages_after"] = total_after
    report["deleted"] = len(ids)
    report["applied"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reparacion quirurgica de PostgreSQL")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="detectar sin modificar")
    group.add_argument("--apply", action="store_true", help="aplicar las correcciones")
    args = parser.parse_args(argv)

    settings = load_settings()
    setup_logging("WARNING")
    settings.ensure_directories()

    database = Database(settings)
    database.connect()
    try:
        report = run(database, settings, apply=args.apply)
    finally:
        database.dispose()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = settings.diagnostics_dir / f"repair-{stamp}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReporte: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
