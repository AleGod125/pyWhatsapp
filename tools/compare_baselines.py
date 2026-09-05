"""Comparar dos fotos: la de antes de la prueba y la de después.

    py tools/compare_baselines.py data/baselines/A.json data/baselines/B.json

QUE SE MIRA
-----------
Que la prueba desde cero llegue a un resultado igual o mejor. No se exige
igualdad exacta de mensajes: WhatsApp decide qué entrega a un dispositivo
vinculado, y esa decisión no es nuestra ni es estable. Lo que sí tiene que
salir parecido es la FORMA del resultado — el número de conversaciones, cuántas
quedan sin referencia y si el motor llegó a responder.

Sólo lee JSON. No toca la base ni la sesión.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LINEA = "=" * 68

#: Los números que interesan, y si subir es bueno o malo.
#:
#: ``+`` significa que más es mejor (más mensajes recuperados). ``-`` que menos
#: es mejor (menos conversaciones sin referencia). ``=`` que sólo se informa.
METRICAS: tuple[tuple[str, str, str], ...] = (
    ("chats_total", "conversaciones", "="),
    ("messages", "mensajes", "+"),
    ("media_total", "multimedia", "+"),
    ("history_seeds", "anclas de historial", "+"),
    ("web_seeds_applied", "  de la via Web", "+"),
    ("history_requests", "peticiones ON_DEMAND", "="),
    ("waiting_seed", "esperando referencia", "-"),
    ("pending", "pendientes", "-"),
    ("timeout", "reintento pendiente", "-"),
    ("exhausted", "historial completo", "+"),
)


def _porcentaje(antes: int, despues: int) -> str:
    if antes == 0:
        return "nuevo" if despues else "="
    cambio = (despues - antes) / antes * 100
    return f"{cambio:+.0f}%"


def _veredicto(direccion: str, antes: int, despues: int) -> str:
    if despues == antes:
        return "igual"
    mejor = despues > antes if direccion == "+" else despues < antes
    if direccion == "=":
        return "distinto"
    return "mejor" if mejor else "PEOR"


def comparar(antes: dict, despues: dict) -> list[tuple]:
    filas = []
    for clave, etiqueta, direccion in METRICAS:
        a = int(antes.get(clave) or 0)
        b = int(despues.get(clave) or 0)
        filas.append((etiqueta, a, b, b - a, _porcentaje(a, b), _veredicto(direccion, a, b)))
    return filas


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("antes", type=Path)
    parser.add_argument("despues", type=Path)
    args = parser.parse_args()

    for ruta in (args.antes, args.despues):
        if not ruta.exists():
            print(f"No existe: {ruta}")
            return 1

    antes = json.loads(args.antes.read_text(encoding="utf-8"))
    despues = json.loads(args.despues.read_text(encoding="utf-8"))

    print(LINEA)
    print("COMPARACION DE BASELINES")
    print(LINEA)
    print(f"  antes    {args.antes.name}  ({antes.get('captured_at', '?')[:19]})")
    print(f"  despues  {args.despues.name}  ({despues.get('captured_at', '?')[:19]})")

    misma = antes.get("session_fingerprint") == despues.get("session_fingerprint")
    print(f"  misma vinculacion        {misma}")
    if not misma:
        # Es lo esperado en una prueba desde cero: vinculacion nueva, carpeta
        # de Drive nueva, y por tanto nada que se pise con lo anterior.
        print("    (vinculacion NUEVA: el backup anterior no se toca)")
    print(
        f"  capacidad ON_DEMAND      {antes.get('on_demand_capability')} -> "
        f"{despues.get('on_demand_capability')}"
    )

    print()
    print(f"  {'metrica':<24}{'antes':>8}{'despues':>10}{'delta':>9}{'%':>8}  veredicto")
    print("  " + "-" * 64)
    peores = []
    for etiqueta, a, b, delta, pct, veredicto in comparar(antes, despues):
        print(f"  {etiqueta:<24}{a:>8}{b:>10}{delta:>+9}{pct:>8}  {veredicto}")
        if veredicto == "PEOR":
            peores.append(etiqueta)

    print()
    if peores:
        print("  Ha empeorado en: " + ", ".join(peores))
        print("  Eso no siempre es un fallo -- WhatsApp decide que entrega --,")
        print("  pero merece mirarse antes de dar la prueba por buena.")
    else:
        print("  La prueba desde cero llega a un resultado igual o mejor.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
