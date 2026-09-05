"""Que anclas de historial hay, y a que conversaciones alcanzan.

Solo lectura. No muestra texto de mensajes ni identificadores completos.

    py tools/inspect_history_seeds.py
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import load_settings  # noqa: E402
from app.models import Chat, ChatHistoryState, HistorySeed, ScannedBlob  # noqa: E402


def main() -> int:
    settings = load_settings()
    with Session(create_engine(settings.database_url)) as s:
        print("=" * 66)
        print("ANCLAS DE HISTORIAL")
        print("=" * 66)

        estados = dict(
            s.execute(
                select(ChatHistoryState.history_status, func.count()).group_by(
                    ChatHistoryState.history_status
                )
            ).all()
        )
        total_chats = s.execute(select(func.count()).select_from(Chat)).scalar()

        print(f"\nConversaciones: {total_chats}")
        for estado, n in sorted(estados.items(), key=lambda x: -x[1]):
            print(f"   {estado:<16} {n}")

        semillas = s.execute(select(HistorySeed)).scalars().all()
        print(f"\nAnclas guardadas: {len(semillas)}")
        if semillas:
            print("   por origen:")
            for fuente, n in Counter(x.source for x in semillas).most_common():
                print(f"      {fuente:<20} {n}")
            marcas = [x.timestamp for x in semillas]
            print(f"   mas antigua: {_fecha(min(marcas))}")
            print(f"   mas reciente: {_fecha(max(marcas))}")

        con = s.execute(
            select(func.count(func.distinct(HistorySeed.chat_id)))
        ).scalar()
        print(f"\nConversaciones con ancla: {con}")
        print(f"Conversaciones sin ancla: {total_chats - (con or 0)}")

        esperando_sin = s.execute(
            select(func.count())
            .select_from(ChatHistoryState)
            .where(
                ChatHistoryState.history_status == "waiting_seed",
                ChatHistoryState.chat_id.notin_(select(HistorySeed.chat_id)),
            )
        ).scalar()
        print(f"\nEsperando ancla y sin ninguna: {esperando_sin}")
        if esperando_sin:
            print("   No es un fallo: WhatsApp no ha entregado ninguna")
            print("   referencia para ellas. Despertaran solas si les llega")
            print("   un mensaje real.")

        blobs = s.execute(select(ScannedBlob)).scalars().all()
        print(f"\nArchivos de historial ya escaneados: {len(blobs)}")
        if blobs:
            for tipo, n in Counter(b.sync_type or "?" for b in blobs).most_common():
                print(f"   {tipo:<24} {n}")
    return 0


def _fecha(marca: int) -> str:
    return datetime.fromtimestamp(marca, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    raise SystemExit(main())
