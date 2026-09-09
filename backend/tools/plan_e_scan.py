"""Buscar anclas de historial en lo que YA hay guardado. Simula por defecto.

QUE HACE
--------
Recorre los blobs de History Sync archivados, extrae las referencias de
mensaje que contengan, y dice cuantas conversaciones que hoy no pueden pedir
historial podrian hacerlo.

NO manda nada al servidor. NO ingiere mensajes. Sin ``--aplicar`` tampoco
escribe en la base.

    py tools/plan_e_scan.py               # solo mira y cuenta
    py tools/plan_e_scan.py --aplicar     # anota las anclas y despierta chats

QUE NO HACE, NUNCA
------------------
Fabricar un identificador o una marca de tiempo. Si WhatsApp no ha dado un
ancla real para una conversacion, esa conversacion se queda esperando y aqui
se dice tal cual. Un ancla inventada recibe confirmacion del servidor y
despues silencio.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import load_settings  # noqa: E402
from app.history.blob_scanner import BlobSeedScanner  # noqa: E402
from app.history.seed_collector import (  # noqa: E402
    RecentSeedCollector,
    validar,
)
from app.models import (  # noqa: E402
    Chat,
    ChatHistoryState,
    User,
    WhatsAppAccount,
)


class _Db:
    """Envoltorio con la forma que esperan los servicios."""

    def __init__(self, sesion: Session, *, confirmar: bool):
        self._sesion = sesion
        self._confirmar = confirmar

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._sesion
            self._sesion.flush()
            if self._confirmar:
                self._sesion.commit()

        return scope()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Busca anclas de historial en los blobs guardados."
    )
    parser.add_argument(
        "--aplicar",
        action="store_true",
        help="anota las anclas y despierta las conversaciones que pueda",
    )
    args = parser.parse_args()

    settings = load_settings()
    engine = create_engine(settings.database_url)

    with Session(engine) as sesion:
        usuario = sesion.execute(select(User)).scalars().first()
        cuenta = sesion.execute(select(WhatsAppAccount)).scalars().first()
        if usuario is None or cuenta is None:
            print("No hay ninguna cuenta de WhatsApp vinculada.")
            return 1

        print("=" * 70)
        print("BUSQUEDA DE ANCLAS" if args.aplicar else "SIMULACION (no cambia nada)")
        print("=" * 70)

        antes = _por_estado(sesion)
        print("\nEstado actual:")
        for estado, n in sorted(antes.items(), key=lambda x: -x[1]):
            print(f"   {estado:<16} {n}")

        scanner = BlobSeedScanner(
            _Db(sesion, confirmar=args.aplicar), settings, account_id=cuenta.id
        )
        blobs = scanner.blobs()
        print(f"\nBlobs de historial guardados: {len(blobs)}")
        if not blobs:
            print("  (no hay ninguno; nada que escanear)")
            return 0

        print("Escaneando... (puede tardar con muchos archivos)")
        candidatos, resumen = scanner.escanear(
            solo_nuevos=args.aplicar, marcar=args.aplicar
        )

        print(f"\n  archivos leidos:      {resumen.blobs_nuevos}")
        print(f"  archivos ya vistos:   {resumen.blobs_saltados}")
        print(f"  ilegibles:            {resumen.ilegibles}")
        print(f"  referencias halladas: {resumen.candidatos}")
        print("\n  por tipo de sincronizacion:")
        for tipo, n in sorted(resumen.por_tipo.items(), key=lambda x: -x[1]):
            print(f"     {tipo:<22} {n}")

        # Cuales servirian de verdad, y para que conversaciones.
        validos = [c for c in candidatos if validar(c) is None]
        motivos = Counter(validar(c) for c in candidatos if validar(c) is not None)

        print(f"\n  referencias utilizables: {len(validos)}")
        if motivos:
            print("  descartadas por:")
            for motivo, n in motivos.most_common(6):
                print(f"     {motivo:<38} {n}")

        esperando = _jids_esperando(sesion)
        alcanzables = {c.chat_jid for c in validos} & esperando
        print(f"\n  conversaciones esperando ancla:        {len(esperando)}")
        print(f"  de esas, con ancla en los blobs:       {len(alcanzables)}")

        if not args.aplicar:
            print("\n" + "-" * 70)
            if alcanzables:
                print(f"Se podrian despertar {len(alcanzables)} conversacion(es).")
                print("Para hacerlo:")
                print("    py tools/plan_e_scan.py --aplicar")
            else:
                print("Ninguna conversacion pendiente tiene ancla en los blobs.")
                print()
                print("No es un fallo: significa que WhatsApp no ha entregado")
                print("ninguna referencia para ellas. Seguiran esperando, y")
                print("despertaran solas si les llega un mensaje real.")
            return 0

        # --- Aplicar ---
        colector = RecentSeedCollector(
            _Db(sesion, confirmar=True),
            user_id=usuario.id,
            account_id=cuenta.id,
        )
        for candidato in validos:
            colector.observe(candidato)
        sesion.commit()

        despues = _por_estado(sesion)
        print("\n" + "=" * 70)
        print("[PLAN_E] resumen:")
        print(f"  esperando al inicio = {antes.get('waiting_seed', 0)}")
        print(f"  anclas nuevas       = {colector.metricas.validas}")
        print(f"  ya conocidas        = {colector.metricas.duplicadas}")
        print(f"  chats despertados   = {colector.metricas.despertados}")
        print(f"  esperando al final  = {despues.get('waiting_seed', 0)}")
        print("=" * 70)
        if colector.metricas.despertados:
            print("\nReinicia service.py: el motor de siempre pedira su historial.")
        return 0


def _por_estado(sesion: Session) -> dict[str, int]:
    return dict(
        sesion.execute(
            select(ChatHistoryState.history_status, func.count()).group_by(
                ChatHistoryState.history_status
            )
        ).all()
    )


def _jids_esperando(sesion: Session) -> set[str]:
    return set(
        sesion.execute(
            select(Chat.jid)
            .join(ChatHistoryState, ChatHistoryState.chat_jid == Chat.jid)
            .where(ChatHistoryState.history_status == "waiting_seed")
        )
        .scalars()
        .all()
    )


if __name__ == "__main__":
    raise SystemExit(main())
