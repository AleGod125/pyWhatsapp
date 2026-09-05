"""Una foto de lo que hay ahora, para poder comparar después.

PARA QUE
--------
Antes de una prueba desde cero hace falta saber contra qué se compara. «Salió
bien» no es un resultado; 41 conversaciones y 3826 mensajes sí lo es.

Esta herramienta mide y no toca nada. Guarda el resultado en
``data/baselines/<marca de tiempo>.json`` para que la comparación de después
sea contra un número escrito antes, y no contra un recuerdo.

    py tools/capture_baseline.py
    py tools/capture_baseline.py --no-guardar

PRIVACIDAD
----------
Sólo recuentos. Ni un identificador, ni un nombre, ni un teléfono, ni una
línea de mensaje.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import load_settings  # noqa: E402
from app.models import (  # noqa: E402
    Chat,
    ChatHistoryState,
    HistoryRequest,
    HistorySeed,
    MediaFile,
    Message,
    WhatsAppAccount,
)
from app.services import repository as repo  # noqa: E402
from app.services.backfill_service import CAPABILITY_KEY, SESSION_KEY  # noqa: E402

LINEA = "=" * 62


def _contar(sesion: Session, modelo) -> int:
    return int(sesion.execute(select(func.count()).select_from(modelo)).scalar() or 0)


def _por_estado(sesion: Session) -> dict[str, int]:
    return {
        str(estado): int(cuantos)
        for estado, cuantos in sesion.execute(
            select(ChatHistoryState.history_status, func.count()).group_by(
                ChatHistoryState.history_status
            )
        ).all()
    }


def capturar(settings) -> dict:
    """Los números. Todos leídos, ninguno estimado."""
    with Session(create_engine(settings.database_url)) as sesion:
        estados = _por_estado(sesion)

        guardada = repo.get_app_state(sesion, SESSION_KEY)
        huella = guardada.get("fingerprint") if isinstance(guardada, dict) else None
        capacidad_guardada = repo.get_app_state(sesion, CAPABILITY_KEY)
        capacidad = "UNKNOWN"
        if isinstance(capacidad_guardada, dict) and capacidad_guardada.get("session") == huella:
            if capacidad_guardada.get("state") == "SUSPECT":
                capacidad = "SUSPECT"
            elif capacidad_guardada.get("confirmed"):
                capacidad = "CONFIRMED"

        peticiones = {
            str(estado): int(cuantos)
            for estado, cuantos in sesion.execute(
                select(HistoryRequest.status, func.count()).group_by(HistoryRequest.status)
            ).all()
        }
        anclas_por_origen = {
            str(origen): int(cuantos)
            for origen, cuantos in sesion.execute(
                select(HistorySeed.source, func.count()).group_by(HistorySeed.source)
            ).all()
        }
        medios = {
            str(estado): int(cuantos)
            for estado, cuantos in sesion.execute(
                select(MediaFile.download_status, func.count()).group_by(
                    MediaFile.download_status
                )
            ).all()
        }

        return {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "session_fingerprint": huella,
            "on_demand_capability": capacidad,
            "accounts": _contar(sesion, WhatsAppAccount),
            "chats_total": _contar(sesion, Chat),
            "messages": _contar(sesion, Message),
            "media_total": sum(medios.values()),
            "media_by_status": medios,
            "history_seeds": _contar(sesion, HistorySeed),
            "history_seeds_by_source": anclas_por_origen,
            # Cuántas anclas vinieron de la vía Web: es lo que mide si la
            # recuperación automática hizo su trabajo.
            "web_seeds_applied": sum(
                cuantos
                for origen, cuantos in anclas_por_origen.items()
                if origen.startswith("web")
            ),
            "history_requests": _contar(sesion, HistoryRequest),
            "history_requests_by_status": peticiones,
            "chats_by_history_status": estados,
            "waiting_seed": estados.get("waiting_seed", 0)
            + estados.get("no_valid_cursor", 0),
            "exhausted": estados.get("exhausted", 0),
            "timeout": estados.get("timeout", 0),
            "pending": estados.get("pending", 0),
            "fetching": estados.get("fetching", 0),
        }


def imprimir(datos: dict) -> None:
    print(LINEA)
    print("BASELINE")
    print(LINEA)
    filas = [
        ("conversaciones", datos["chats_total"]),
        ("mensajes", datos["messages"]),
        ("multimedia", datos["media_total"]),
        ("anclas de historial", datos["history_seeds"]),
        ("  de la via Web", datos["web_seeds_applied"]),
        ("peticiones ON_DEMAND", datos["history_requests"]),
        ("", ""),
        ("esperando referencia", datos["waiting_seed"]),
        ("pendientes", datos["pending"]),
        ("excavando", datos["fetching"]),
        ("reintento pendiente", datos["timeout"]),
        ("historial completo", datos["exhausted"]),
        ("", ""),
        ("capacidad ON_DEMAND", datos["on_demand_capability"]),
        ("huella de sesion", datos["session_fingerprint"] or "-"),
    ]
    for etiqueta, valor in filas:
        if etiqueta == "":
            print()
            continue
        print(f"  {etiqueta:<24} {valor}")
    print()
    print("  peticiones por estado   " + str(datos["history_requests_by_status"]))
    print("  multimedia por estado   " + str(datos["media_by_status"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-guardar", action="store_true", help="solo imprimir, sin escribir el JSON"
    )
    args = parser.parse_args()

    settings = load_settings()
    datos = capturar(settings)
    imprimir(datos)

    if args.no_guardar:
        print("\nNo se ha guardado nada (--no-guardar).")
        return 0

    carpeta = Path("data/baselines")
    carpeta.mkdir(parents=True, exist_ok=True)
    marca = datetime.now().strftime("%Y%m%dT%H%M%S")
    destino = carpeta / f"{marca}.json"
    destino.write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nGuardado en {destino}")
    print("Los baselines no se borran: son la evidencia de la prueba.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
