"""Adjudicar a su cuenta los chats que se guardaron sin dueno.

POR QUE HACE FALTA
------------------
Los chats que entraron antes de que la ingesta supiera de quien eran tienen
``whatsapp_account_id`` a NULL. El filtro de propiedad los excluye —hace lo
correcto— y el resultado es un panel vacio con las conversaciones dentro de la
base.

POR QUE NO SE HACE SOLO
-----------------------
``UPDATE chats SET whatsapp_account_id = <la unica cuenta>`` es una operacion
que en cuanto haya dos usuarios entrega las conversaciones de uno al otro. Se
ejecuta a mano, se explica lo que va a hacer, y se niega en cuanto hay
ambiguedad.

CONDICIONES, TODAS OBLIGATORIAS
-------------------------------
* existe UNA sola cuenta de WhatsApp;
* esa cuenta consta vinculada;
* los chats a adoptar NO tienen dueno (los que ya lo tienen no se tocan).

Con dos cuentas se para: no hay forma de saber de quien es cada chat, y
repartirlos a ojo seria peor que dejarlos invisibles.

    py tools/reconciliar_propiedad.py
    py tools/reconciliar_propiedad.py --aplicar
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, select, update  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import load_settings  # noqa: E402
from app.models import Chat, Message, WhatsAppAccount  # noqa: E402
from app.models.accounts import LINKED_STATUSES  # noqa: E402

CONFIRMACION = "ADOPTAR"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Adjudica los chats sin dueno a la unica cuenta vinculada."
    )
    parser.add_argument("--aplicar", action="store_true", help="lo hace de verdad")
    parser.add_argument("--si", action="store_true", help="no preguntar")
    args = parser.parse_args()

    settings = load_settings()
    engine = create_engine(settings.database_url)

    with Session(engine) as sesion:
        cuentas = sesion.execute(select(WhatsAppAccount)).scalars().all()
        huerfanos = sesion.execute(
            select(func.count())
            .select_from(Chat)
            .where(Chat.whatsapp_account_id.is_(None))
        ).scalar()
        con_dueno = sesion.execute(
            select(func.count())
            .select_from(Chat)
            .where(Chat.whatsapp_account_id.is_not(None))
        ).scalar()

        print("=" * 68)
        print("ADOPCION DE CHATS" if args.aplicar else "SIMULACION (no cambia nada)")
        print("=" * 68)
        print(f"\nChats sin dueno:  {huerfanos}")
        print(f"Chats con dueno:  {con_dueno}")
        print(f"Cuentas:          {len(cuentas)}")

        if huerfanos == 0:
            print("\nNo hay nada que adoptar.")
            return 0

        if len(cuentas) != 1:
            print(
                f"\n  ABORTADO: hay {len(cuentas)} cuentas de WhatsApp.\n"
                "  Con mas de una no se puede saber de quien es cada chat, y\n"
                "  repartirlos a ojo entregaria las conversaciones de uno a otro."
            )
            return 2

        cuenta = cuentas[0]
        if cuenta.session_status not in LINKED_STATUSES:
            print(
                f"\n  ABORTADO: esa cuenta esta en '{cuenta.session_status}'.\n"
                "  Solo se adoptan chats para una cuenta que consta vinculada."
            )
            return 2

        mensajes = sesion.execute(
            select(func.count())
            .select_from(Message)
            .join(Chat, Chat.id == Message.chat_id)
            .where(Chat.whatsapp_account_id.is_(None))
        ).scalar()

        print(f"\nSe adjudicaran {huerfanos} chat(s) y sus {mensajes} mensaje(s)")
        print("a la unica cuenta vinculada que hay.")
        print("\nNO se toca ningun chat que ya tenga dueno.")
        print("NO se borra nada. NO se sube nada a Drive todavia.")

        if not args.aplicar:
            print("\nEsto ha sido una simulacion. Para hacerlo:")
            print("    py tools/reconciliar_propiedad.py --aplicar")
            return 0

        if not args.si:
            print()
            if input(f'Escribe {CONFIRMACION} para confirmar: ').strip() != CONFIRMACION:
                print("Cancelado. No se ha tocado nada.")
                return 1

        filas = sesion.execute(
            update(Chat)
            .where(Chat.whatsapp_account_id.is_(None))
            .values(whatsapp_account_id=cuenta.id)
        ).rowcount
        sesion.commit()

        print(f"\n  {filas} chat(s) adjudicados.")
        print()
        print("=" * 68)
        print("LISTO. Ahora:")
        print()
        print("  1. Reinicia service.py.")
        print("  2. El panel deberia mostrar tus conversaciones.")
        print("  3. El trabajador de almacenamiento empezara a agruparlas en")
        print("     segmentos y a subirlas a Drive por su cuenta.")
        print()
        print("  Mientras queden mensajes sin subir, el panel dira")
        print('  "Guardando en Google Drive", no "al dia". Es lo correcto.')
        print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
