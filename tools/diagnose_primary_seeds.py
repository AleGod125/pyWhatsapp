"""¿Cuántas conversaciones puede recuperar la sesión principal SOLA? Solo lectura.

LA PREGUNTA
-----------
Para pedirle el pasado a WhatsApp hace falta una referencia real por
conversación. La pregunta que decide si el segundo dispositivo sigue siendo
necesario es ésta, y sólo ésta:

    ¿cuántas conversaciones tienen una referencia que NO trajo el navegador?

Aquí se contesta separando las fuentes con cuidado, porque mezclarlas es
exactamente lo que hizo falta tres fases para descubrir.

    py tools/diagnose_primary_seeds.py
    py tools/diagnose_primary_seeds.py --detalle

SOLO LECTURA
------------
Consulta PostgreSQL. No abre blobs, no toca la sesión, no pide nada a la red y
no escribe ni una fila. Y no consulta al segundo dispositivo: si pudiera
hacerlo, la cifra dejaría de significar «lo que consigue la principal sola».
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import load_settings  # noqa: E402
from app.discovery.primary_seed_resolver import (  # noqa: E402
    FUENTES_DEL_NAVEGADOR,
    FUENTES_DE_MENSAJE_ACEPTABLES,
)

LINEA = "=" * 70


def _hash(jid: str) -> str:
    return hashlib.sha256((jid or "").encode()).hexdigest()[:8]


def _lista(valores) -> str:
    return ", ".join(f"'{v}'" for v in sorted(valores))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detalle", action="store_true", help="conversacion a conversacion")
    args = parser.parse_args()

    settings = load_settings()
    motor = create_engine(settings.database_url)
    web = _lista(FUENTES_DEL_NAVEGADOR)
    propias = _lista(FUENTES_DE_MENSAJE_ACEPTABLES)

    with motor.connect() as c:
        total = int(c.execute(text("SELECT COUNT(*) FROM chats")).scalar() or 0)
        con_cualquiera = int(
            c.execute(
                text(
                    "SELECT COUNT(DISTINCT chat_jid) FROM history_seeds WHERE valid"
                )
            ).scalar()
            or 0
        )
        con_propia = int(
            c.execute(
                text(
                    "SELECT COUNT(DISTINCT chat_jid) FROM history_seeds "
                    f"WHERE valid AND source NOT IN ({web})"
                )
            ).scalar()
            or 0
        )
        sin_propia_con_mensaje = int(
            c.execute(
                text(
                    "SELECT COUNT(*) FROM chats ch WHERE NOT EXISTS ("
                    "  SELECT 1 FROM history_seeds s WHERE s.chat_jid = ch.jid "
                    f"   AND s.valid AND s.source NOT IN ({web})) "
                    "AND EXISTS ("
                    "  SELECT 1 FROM messages m WHERE m.chat_jid = ch.jid "
                    f"   AND m.whatsapp_message_id IS NOT NULL AND m.source IN ({propias}))"
                )
            ).scalar()
            or 0
        )
        sin_nada = int(
            c.execute(
                text(
                    "SELECT COUNT(*) FROM chats ch WHERE NOT EXISTS ("
                    "  SELECT 1 FROM history_seeds s WHERE s.chat_jid = ch.jid "
                    f"   AND s.valid AND s.source NOT IN ({web})) "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM messages m WHERE m.chat_jid = ch.jid "
                    f"   AND m.whatsapp_message_id IS NOT NULL AND m.source IN ({propias}))"
                )
            ).scalar()
            or 0
        )
        por_fuente = list(
            c.execute(
                text(
                    "SELECT source, COUNT(DISTINCT chat_jid) FROM history_seeds "
                    "WHERE valid GROUP BY 1 ORDER BY 2 DESC"
                )
            )
        )
        blobs_vistos = int(
            c.execute(text("SELECT COUNT(*) FROM scanned_blobs")).scalar() or 0
        )
        filas = (
            list(
                c.execute(
                    text(
                        "SELECT ch.jid, ("
                        "  SELECT string_agg(DISTINCT s.source, '+') FROM history_seeds s "
                        "  WHERE s.chat_jid = ch.jid AND s.valid), ("
                        "  SELECT COUNT(*) FROM messages m WHERE m.chat_jid = ch.jid "
                        "   AND m.whatsapp_message_id IS NOT NULL) "
                        "FROM chats ch ORDER BY ch.jid"
                    )
                )
            )
            if args.detalle
            else []
        )

    blobs_en_disco = len(list((settings.data_dir / "history").glob("*.pb")))

    print(LINEA)
    print("LO QUE PUEDE RECUPERAR LA SESION PRINCIPAL SOLA")
    print(LINEA)
    print(f"  conversaciones                        {total}")
    print(f"  con ancla, venga de donde venga       {con_cualquiera}")
    print()
    print("  QUITANDO lo que trajo el segundo dispositivo:")
    print(f"    con ancla PROPIA                    {con_propia}")
    print(f"    sin ancla propia, con mensaje real  {sin_propia_con_mensaje}  <- recuperables")
    print(f"    sin ancla propia y sin mensaje      {sin_nada}")
    print()
    alcanzable = con_propia + sin_propia_con_mensaje
    if total:
        print(f"  alcanzable sin el navegador           {alcanzable}/{total}"
              f"  ({round(alcanzable / total * 100)}%)")
    print()
    print("  de donde salio cada ancla:")
    for fuente, cuantas in por_fuente:
        marca = "  (navegador)" if str(fuente) in FUENTES_DEL_NAVEGADOR else ""
        print(f"    {str(fuente):<20} {cuantas:>4}{marca}")
    print()
    print(f"  blobs archivados en disco             {blobs_en_disco}")
    print(f"  blobs ya escaneados                   {blobs_vistos}")
    if blobs_en_disco > blobs_vistos:
        print(f"    quedan {blobs_en_disco - blobs_vistos} por leer; el ciclo de")
        print("    sincronizacion los recorre solo, una vez cada uno.")

    if args.detalle:
        print()
        print(LINEA)
        print("POR CONVERSACION")
        print(LINEA)
        print(f"  {'chat':<10} {'mensajes reales':>16}  fuentes del ancla")
        for jid, fuentes, mensajes in filas:
            print(f"  {_hash(jid):<10} {mensajes:>16}  {fuentes or '(ninguna)'}")

    print()
    print(f"  {sin_nada} conversacion(es) necesitan de verdad la recuperacion")
    print("  avanzada: no hay ni ancla propia ni un solo mensaje real suyo.")
    print()
    print("Solo lectura: no se ha escrito ni una fila. No se consulto al navegador.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
