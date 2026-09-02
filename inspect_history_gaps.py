"""Distribucion diaria del historial y busqueda de huecos. SOLO LECTURA.

    python inspect_history_gaps.py --name "VirtualTec Marco"
    python inspect_history_gaps.py --chat <jid> --date 2026-08-10

Un intervalo largo sin mensajes NO se declara hueco por si solo: una
conversacion puede estar inactiva. Esta herramienta ensena la distribucion
para que la decision la tome quien puede compararla con el telefono.

Nunca imprime el texto de los mensajes.

HERRAMIENTA DE DIAGNOSTICO (seccion 35). El mantenimiento normal lo hace
``main.py`` solo, a traves de ``MaintenanceService``. Esto se ejecuta a mano
cuando hace falta mirar por dentro; el uso corriente no lo necesita.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import text

from app.config import load_settings
from app.database import Database
from app.logging_setup import setup_logging
from inspect_db import mask


def local_date(ts: int) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
    )


def local_time(ts: int) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%H:%M:%S")
    )


def resolve_chat(connection, *, name: str | None, jid: str | None):
    if jid:
        row = connection.execute(
            text("SELECT id, jid FROM chats WHERE jid = :j"), {"j": jid}
        ).first()
        return (row[0], row[1], jid) if row else None

    row = connection.execute(
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Huecos de historial (solo lectura)")
    parser.add_argument("--name", help="nombre del contacto")
    parser.add_argument("--chat", help="JID exacto del chat")
    parser.add_argument("--date", help="detalle de un dia concreto, YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=0, help="distribucion de los N mayores")
    args = parser.parse_args(argv)

    settings = load_settings()
    setup_logging("WARNING")
    database = Database(settings)
    database.connect()

    try:
        with database.engine.connect() as c:
            if args.top:
                print("CHATS CON MAS MENSAJES")
                for row in c.execute(
                    text(
                        "SELECT COALESCE(co.display_name, ch.jid), COUNT(m.id), "
                        "       MIN(m.timestamp), MAX(m.timestamp) "
                        "FROM chats ch LEFT JOIN messages m ON m.chat_id = ch.id "
                        "LEFT JOIN contacts co ON co.jid = ch.jid OR co.lid = ch.jid "
                        "GROUP BY 1 ORDER BY 2 DESC LIMIT :n"
                    ),
                    {"n": args.top},
                ):
                    print(
                        f"  {str(row[0])[:28]:<30} {row[1]:>5}  "
                        f"{local_date(row[2]) if row[2] else '-'} -> "
                        f"{local_date(row[3]) if row[3] else '-'}"
                    )
                return 0

            if not (args.name or args.chat):
                parser.error("indica --name, --chat o --top")

            found = resolve_chat(c, name=args.name, jid=args.chat)
            if found is None:
                print("Chat no encontrado")
                return 1
            chat_id, chat_jid, label = found

            print("=" * 58)
            print(f"CHAT: {label}")
            print(f"JID : {mask(chat_jid)}   (chat_id={chat_id})")
            print("=" * 58)

            rows = c.execute(
                text(
                    "SELECT timestamp, whatsapp_message_id, from_me, message_type, source "
                    "FROM messages WHERE chat_id = :c ORDER BY timestamp, id"
                ),
                {"c": chat_id},
            ).all()
            if not rows:
                print("Sin mensajes almacenados")
                return 0

            print(f"\nTotal en PostgreSQL: {len(rows)}")
            print(f"Mas antiguo : {local_date(rows[0][0])} {local_time(rows[0][0])}")
            print(f"Mas reciente: {local_date(rows[-1][0])} {local_time(rows[-1][0])}")

            if args.date:
                print(f"\nDETALLE DE {args.date}")
                print("-" * 58)
                print(f"  {'hora':<10} {'wamid':<14} {'from_me':<8} {'tipo':<12} source")
                n = 0
                for ts, wamid, from_me, tipo, source in rows:
                    if local_date(ts) != args.date:
                        continue
                    n += 1
                    print(
                        f"  {local_time(ts):<10} {(wamid or '-')[:12]:<14} "
                        f"{str(from_me):<8} {tipo:<12} {source}"
                    )
                print(f"\n  mensajes almacenados ese dia: {n}")
                return 0

            print("\nDISTRIBUCION POR DIA")
            print("-" * 58)
            por_dia = Counter(local_date(r[0]) for r in rows)
            dias = sorted(por_dia)
            for dia in dias:
                barra = "#" * min(50, por_dia[dia])
                print(f"  {dia}  {por_dia[dia]:>4}  {barra}")

            # Dias del calendario sin ningun mensaje, entre el primero y el
            # ultimo. Se informa, NO se declara hueco: puede ser inactividad.
            from datetime import date, timedelta

            inicio = date.fromisoformat(dias[0])
            fin = date.fromisoformat(dias[-1])
            vacios = []
            actual = inicio
            while actual <= fin:
                if actual.isoformat() not in por_dia:
                    vacios.append(actual.isoformat())
                actual += timedelta(days=1)

            print(f"\n  dias con mensajes: {len(dias)}")
            print(f"  dias del rango sin ninguno: {len(vacios)}")
            if vacios:
                print(f"  {', '.join(vacios[:14])}{' ...' if len(vacios) > 14 else ''}")
            print(
                "\n  Un dia vacio NO es necesariamente un hueco: puede ser"
                "\n  inactividad. Comparar con el telefono para decidir."
            )
    finally:
        database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
