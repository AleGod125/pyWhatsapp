"""Auditoria de consistencia de PostgreSQL. SOLO LECTURA.

    python inspect_db.py            resumen
    python inspect_db.py --deep     auditoria completa
    python inspect_db.py --self     desglose del chat propio

Nunca imprime contenido de mensajes, raw_proto, claves ni material
criptografico: solo formas, tipos y conteos.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from app.config import load_settings
from app.database import Database
from app.logging_setup import setup_logging


def fmt_ts(value: int | None) -> str:
    if not value:
        return "-"
    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M")
    )


def mask(jid: str | None) -> str:
    """Enmascara el identificador conservando el servidor, que si es util."""
    if not jid:
        return "-"
    user, _, server = jid.partition("@")
    return f"{user[:6]}***@{server}" if server else f"{user[:6]}***"


def section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def own_identity(settings: Any) -> tuple[str | None, str | None]:
    """``(own_pn, own_lid)`` leidos del DeviceStore.

    Ambos identifican LA MISMA cuenta. No se deducen el uno del otro: se leen
    de lo que persistio el pairing.
    """
    import json as _json

    if not settings.session_file.exists():
        return None, None
    try:
        data = _json.loads(settings.session_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None

    jid = data.get("jid") or {}
    pn = (
        f"{jid['user']}@{jid.get('server', 's.whatsapp.net')}"
        if isinstance(jid, dict) and jid.get("user")
        else None
    )
    raw_lid = data.get("lid")
    lid = None
    if isinstance(raw_lid, str) and raw_lid:
        # El lid guardado puede venir con sufijo de dispositivo (".75@lid").
        user = raw_lid.split("@")[0].split(".")[0]
        lid = f"{user}@lid"
    return pn, lid


def audit(database: Database, settings: Any, *, deep: bool, self_only: bool) -> dict:
    report: dict[str, Any] = {}
    own_pn, own_lid = own_identity(settings)

    with database.engine.connect() as c:
        one = lambda q, **p: c.execute(text(q), p).scalar_one()  # noqa: E731

        print("=" * 62)
        print("DATABASE AUDIT")
        print("=" * 62)

        section("IDENTIDAD PROPIA")
        print(f"  own_pn  = {mask(own_pn)}")
        print(f"  own_lid = {mask(own_lid)}")
        report["own_pn"] = own_pn
        report["own_lid"] = own_lid

        if not self_only:
            section("TOTALES")
            for table in ("chats", "messages", "contacts", "media_files"):
                total = one(f"SELECT COUNT(*) FROM {table}")
                print(f"  {table:<14} {total}")
                report[table] = total

            section("MENSAJES POR SOURCE")
            for row in c.execute(
                text("SELECT source, COUNT(*) FROM messages GROUP BY 1 ORDER BY 2 DESC")
            ):
                print(f"  {row[0]:<18} {row[1]}")

            section("MENSAJES POR TIPO")
            tipos = c.execute(
                text(
                    "SELECT message_type, COUNT(*) FROM messages "
                    "GROUP BY 1 ORDER BY 2 DESC"
                )
            ).all()
            for tipo, total in tipos:
                print(f"  {tipo:<22} {total}")
            report["types"] = {t: n for t, n in tipos}

            section("INTEGRIDAD")
            checks = {
                "sin timestamp": "SELECT COUNT(*) FROM messages WHERE timestamp IS NULL OR timestamp = 0",
                "sin whatsapp_message_id": "SELECT COUNT(*) FROM messages WHERE whatsapp_message_id IS NULL",
                "con synthetic_identifier": "SELECT COUNT(*) FROM messages WHERE synthetic_identifier IS NOT NULL",
                "id sintetico en columna real": (
                    "SELECT COUNT(*) FROM messages WHERE whatsapp_message_id ILIKE 'opaque-%' "
                    "OR whatsapp_message_id ILIKE 'synthetic-%' OR whatsapp_message_id ILIKE 'local-%'"
                ),
                "duplicados (chat,wamid)": (
                    "SELECT COUNT(*) FROM (SELECT chat_jid, whatsapp_message_id FROM messages "
                    "WHERE whatsapp_message_id IS NOT NULL GROUP BY 1,2 HAVING COUNT(*) > 1) x"
                ),
                "mensajes huerfanos": (
                    "SELECT COUNT(*) FROM messages m LEFT JOIN chats ch ON ch.id = m.chat_id "
                    "WHERE ch.id IS NULL"
                ),
                "media huerfana": (
                    "SELECT COUNT(*) FROM media_files mf LEFT JOIN messages m ON m.id = mf.message_id "
                    "WHERE m.id IS NULL"
                ),
                "sin raw_proto": "SELECT COUNT(*) FROM messages WHERE raw_proto IS NULL",
            }
            for label, query in checks.items():
                total = one(query)
                flag = "  <-- revisar" if total and "duplicados" in label else ""
                print(f"  {label:<32} {total}{flag}")
                report[label] = total

            section("RANGO TEMPORAL")
            oldest = one("SELECT MIN(timestamp) FROM messages")
            newest = one("SELECT MAX(timestamp) FROM messages")
            print(f"  mas antiguo  {fmt_ts(oldest)}")
            print(f"  mas reciente {fmt_ts(newest)}")

            section("MULTIMEDIA")
            for row in c.execute(
                text(
                    "SELECT download_status, COUNT(*) FROM media_files "
                    "GROUP BY 1 ORDER BY 2 DESC"
                )
            ):
                print(f"  {row[0]:<16} {row[1]}")

            section("ESTADO DE HISTORIAL POR CHAT")
            for row in c.execute(
                text(
                    "SELECT history_status, COUNT(*) FROM chat_history_state "
                    "GROUP BY 1 ORDER BY 2 DESC"
                )
            ):
                print(f"  {row[0]:<18} {row[1]}")

        # -- Chat propio ----------------------------------------------------
        section("CHAT PROPIO (self)")
        self_jids = [j for j in (own_pn, own_lid) if j]
        if not self_jids:
            print("  no se pudo determinar la identidad propia")
        else:
            for jid in self_jids:
                total = one(
                    "SELECT COUNT(*) FROM messages WHERE chat_jid = :j", j=jid
                )
                print(f"  entry jid={mask(jid)} count={total}")
                report[f"self_{jid}"] = total

            print()
            print("  [SELF-AUDIT] desglose por tipo y origen:")
            rows = c.execute(
                text(
                    "SELECT message_type, source, from_me, "
                    "       COUNT(*) AS n, "
                    "       COUNT(*) FILTER (WHERE text IS NOT NULL) AS con_texto, "
                    "       COALESCE(AVG(length(raw_proto))::int, 0) AS proto_medio "
                    "FROM messages WHERE chat_jid = ANY(:jids) "
                    "GROUP BY 1,2,3 ORDER BY n DESC"
                ),
                {"jids": self_jids},
            ).all()
            print(
                f"    {'tipo':<14} {'source':<16} {'from_me':<8} {'n':>5} "
                f"{'con_texto':>10} {'raw_proto':>10}"
            )
            for tipo, source, from_me, n, con_texto, proto in rows:
                print(
                    f"    {tipo:<14} {source:<16} {str(from_me):<8} {n:>5} "
                    f"{con_texto:>10} {proto:>10}"
                )
            report["self_breakdown"] = [
                {"type": t, "source": s, "from_me": f, "n": n, "with_text": ct}
                for t, s, f, n, ct, _p in rows
            ]

        # -- Posibles duplicados PN/LID -------------------------------------
        if deep and not self_only:
            section("POSIBLES DUPLICADOS PN / LID")
            dupes = c.execute(
                text(
                    "SELECT co.display_name, COUNT(DISTINCT ch.jid) AS entradas, "
                    "       array_agg(DISTINCT split_part(ch.jid,'@',2)) AS servidores "
                    "FROM chats ch JOIN contacts co "
                    "  ON co.jid = ch.jid OR co.lid = ch.jid "
                    "WHERE co.display_name IS NOT NULL "
                    "GROUP BY 1 HAVING COUNT(DISTINCT ch.jid) > 1 "
                    "ORDER BY 2 DESC LIMIT 20"
                )
            ).all()
            if not dupes:
                print("  ninguno detectado por nombre de contacto resuelto")
            for name, entradas, servidores in dupes:
                print(f"  {name[:28]:<30} entradas={entradas} servidores={servidores}")
            report["pn_lid_dupes"] = len(dupes)

            section("CHATS QUE SOLO CONTIENEN PROTOCOLO")
            solo_protocolo = c.execute(
                text(
                    "SELECT ch.jid, COUNT(*) AS n FROM chats ch JOIN messages m ON m.chat_id = ch.id "
                    "GROUP BY ch.jid "
                    "HAVING COUNT(*) FILTER (WHERE m.message_type NOT IN "
                    "  ('protocol','senderkey','unknown','system')) = 0 "
                    "ORDER BY n DESC LIMIT 15"
                )
            ).all()
            if not solo_protocolo:
                print("  ninguno")
            for jid, n in solo_protocolo:
                print(f"  {mask(jid):<26} {n} mensajes, ninguno visible")
            report["protocol_only_chats"] = len(solo_protocolo)

            section("CHATS CON MAS MENSAJES")
            for row in c.execute(
                text(
                    "SELECT ch.jid, COALESCE(co.display_name, '') AS nombre, COUNT(m.id) AS n, "
                    "       MIN(m.timestamp), MAX(m.timestamp) "
                    "FROM chats ch LEFT JOIN messages m ON m.chat_id = ch.id "
                    "LEFT JOIN contacts co ON co.jid = ch.jid OR co.lid = ch.jid "
                    "GROUP BY ch.jid, co.display_name ORDER BY n DESC LIMIT 12"
                )
            ):
                nombre = (row[1] or mask(row[0]))[:24]
                print(
                    f"  {nombre:<26} {row[2]:>5}  {fmt_ts(row[3])} -> {fmt_ts(row[4])}"
                )

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auditoria de PostgreSQL (solo lectura)")
    parser.add_argument("--deep", action="store_true", help="auditoria completa")
    parser.add_argument("--self", dest="self_only", action="store_true",
                        help="solo el desglose del chat propio")
    parser.add_argument("--json", dest="json_out", help="guardar el reporte en un JSON")
    args = parser.parse_args(argv)

    settings = load_settings()
    setup_logging("WARNING")
    database = Database(settings)
    database.connect()
    try:
        report = audit(database, settings, deep=args.deep, self_only=args.self_only)
    finally:
        database.dispose()

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
        print(f"\nReporte guardado en {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
