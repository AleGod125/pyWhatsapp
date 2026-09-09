"""Comparar lo que extrae cada proveedor. SOLO LECTURA.

PARA QUE SIRVE
--------------
Las cuatro compuertas de la migracion no se pueden juzgar de memoria. Este
arnes mide, sobre la MISMA base, lo que hay hoy -- y se vuelve a correr
despues de una extraccion con Baileys para comparar con esa foto.

    py tools/diff_proveedores.py --guardar antes-de-baileys.json
    ... (se cambia WA_PROVIDER=baileys, se vincula, se extrae) ...
    py tools/diff_proveedores.py --comparar antes-de-baileys.json

LAS CUATRO COMPUERTAS
---------------------
G1  tanda      cuantas peticiones hicieron falta y cuantos mensajes trajo cada una
G2  fin        cuantas conversaciones tienen marcador de fin del telefono
G3  LID->PN    cuantos chats `@lid` resuelven a un nombre
G4  descifrado cuantos mensajes entraron frente a cuantos fallos hubo

QUE NO HACE
-----------
No escribe en la base, no arranca ningun worker y no pide nada a WhatsApp. Solo
lee y cuenta.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_RAIZ = _Path(__file__).resolve().parent.parent
if str(_RAIZ) not in _sys.path:
    _sys.path.insert(0, str(_RAIZ))

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.core.config import load_settings
from app.core.database import Database, DatabaseError
from app.models import Chat, ChatHistoryState, Contact, HistoryRequest, Message


def medir(sesion) -> dict:
    """La foto de lo que hay ahora mismo."""
    total_chats = sesion.execute(select(func.count()).select_from(Chat)).scalar_one()
    total_msgs = sesion.execute(select(func.count()).select_from(Message)).scalar_one()

    # --- El historial que se consiguio -------------------------------------
    mas_viejo, mas_nuevo = sesion.execute(
        select(func.min(Message.timestamp), func.max(Message.timestamp))
    ).one()
    ventana_dias = (
        round((mas_nuevo - mas_viejo) / 86400, 1) if mas_viejo and mas_nuevo else 0
    )

    # --- G1: coste de las peticiones ---------------------------------------
    peticiones = dict(
        sesion.execute(
            select(HistoryRequest.status, func.count()).group_by(HistoryRequest.status)
        ).all()
    )
    respondidas, devueltos, maximo = sesion.execute(
        select(
            func.count(),
            func.coalesce(func.sum(HistoryRequest.response_count), 0),
            func.coalesce(func.max(HistoryRequest.response_count), 0),
        ).where(HistoryRequest.status == "received")
    ).one()

    # --- G2: el marcador de fin lo dice el telefono ------------------------
    por_estado = dict(
        sesion.execute(
            select(ChatHistoryState.history_status, func.count()).group_by(
                ChatHistoryState.history_status
            )
        ).all()
    )
    con_veredicto = sesion.execute(
        select(func.count())
        .select_from(ChatHistoryState)
        .where(ChatHistoryState.last_error.like("COMPLETE_%"))
    ).scalar_one()

    # --- G3: nombres y LIDs -------------------------------------------------
    individuales = sesion.execute(
        select(func.count()).select_from(Chat).where(Chat.chat_type == "individual")
    ).scalar_one()
    por_lid = sesion.execute(
        select(func.count())
        .select_from(Chat)
        .where(Chat.chat_type == "individual", Chat.jid.like("%@lid"))
    ).scalar_one()
    # Resuelven nombre con el MISMO join que usa la lista de chats.
    resuelven = sesion.execute(
        select(func.count())
        .select_from(Chat)
        .join(
            Contact,
            ((Contact.jid == Chat.jid) | (Contact.lid == Chat.jid))
            & (Contact.whatsapp_account_id == Chat.whatsapp_account_id),
            isouter=True,
        )
        .where(
            Chat.chat_type == "individual",
            func.coalesce(Chat.name, "") == "",
            func.coalesce(Contact.display_name, Contact.push_name, "") != "",
        )
    ).scalar_one()
    contactos_con_lid = sesion.execute(
        select(func.count()).select_from(Contact).where(Contact.lid.isnot(None))
    ).scalar_one()

    # --- G4: de donde vino cada mensaje ------------------------------------
    por_origen = dict(
        sesion.execute(select(Message.source, func.count()).group_by(Message.source)).all()
    )

    return {
        "medido_en": datetime.now(timezone.utc).astimezone().isoformat(),
        "totales": {"chats": total_chats, "mensajes": total_msgs},
        "G1_tanda": {
            "peticiones_por_estado": peticiones,
            "respondidas": respondidas,
            "mensajes_devueltos": int(devueltos),
            "maximo_en_una_respuesta": int(maximo),
            "media_por_respuesta": round(devueltos / respondidas, 1) if respondidas else 0,
        },
        "G2_marcador_de_fin": {
            "chats_por_estado": por_estado,
            "con_veredicto_del_telefono": con_veredicto,
            "ventana_dias": ventana_dias,
            "mas_antiguo": _fecha(mas_viejo),
            "mas_reciente": _fecha(mas_nuevo),
        },
        "G3_nombres_y_lid": {
            "chats_individuales": individuales,
            "identificados_por_lid": por_lid,
            "sin_nombre_pero_resuelven_por_contacto": resuelven,
            "contactos_con_lid_conocido": contactos_con_lid,
        },
        "G4_descifrado": {"mensajes_por_origen": por_origen},
    }


def _fecha(marca) -> str | None:
    if not marca:
        return None
    return datetime.fromtimestamp(int(marca)).date().isoformat()


def comparar(antes: dict, ahora: dict) -> None:
    print("\n=== COMPARATIVA ===\n")
    _fila("mensajes", antes["totales"]["mensajes"], ahora["totales"]["mensajes"])
    _fila("chats", antes["totales"]["chats"], ahora["totales"]["chats"])
    _fila(
        "ventana (dias)",
        antes["G2_marcador_de_fin"]["ventana_dias"],
        ahora["G2_marcador_de_fin"]["ventana_dias"],
    )
    print(
        f"  mas antiguo:      {antes['G2_marcador_de_fin']['mas_antiguo']}"
        f"  ->  {ahora['G2_marcador_de_fin']['mas_antiguo']}"
    )
    print("\n-- G1 tanda --")
    _fila(
        "media por respuesta",
        antes["G1_tanda"]["media_por_respuesta"],
        ahora["G1_tanda"]["media_por_respuesta"],
    )
    _fila(
        "maximo en una",
        antes["G1_tanda"]["maximo_en_una_respuesta"],
        ahora["G1_tanda"]["maximo_en_una_respuesta"],
    )
    _fila("respondidas", antes["G1_tanda"]["respondidas"], ahora["G1_tanda"]["respondidas"])
    print("\n-- G2 marcador de fin --")
    _fila(
        "con veredicto del telefono",
        antes["G2_marcador_de_fin"]["con_veredicto_del_telefono"],
        ahora["G2_marcador_de_fin"]["con_veredicto_del_telefono"],
    )
    print("\n-- G3 nombres y LID --")
    _fila(
        "resuelven por contacto",
        antes["G3_nombres_y_lid"]["sin_nombre_pero_resuelven_por_contacto"],
        ahora["G3_nombres_y_lid"]["sin_nombre_pero_resuelven_por_contacto"],
    )
    _fila(
        "contactos con LID",
        antes["G3_nombres_y_lid"]["contactos_con_lid_conocido"],
        ahora["G3_nombres_y_lid"]["contactos_con_lid_conocido"],
    )
    print("\n-- G4 origen de los mensajes --")
    print(f"  antes: {antes['G4_descifrado']['mensajes_por_origen']}")
    print(f"  ahora: {ahora['G4_descifrado']['mensajes_por_origen']}")
    print(
        "\nLos fallos de descifrado NO estan en la base (un mensaje que no se\n"
        "descifra no llega): para G4 se cuentan en el log, buscando\n"
        "'no se pudo descifrar'.\n"
    )


def _fila(etiqueta: str, antes, ahora) -> None:
    try:
        delta = ahora - antes
        signo = "+" if delta > 0 else ""
        extra = f"  ({signo}{round(delta, 1)})" if delta else ""
    except TypeError:
        extra = ""
    print(f"  {etiqueta:<28} {antes!s:>12}  ->  {ahora!s:>12}{extra}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guardar", help="escribe la foto actual en un JSON")
    parser.add_argument("--comparar", help="compara la foto actual contra ese JSON")
    args = parser.parse_args(argv)

    settings = load_settings()
    try:
        database = Database(settings)
        database.connect()
    except DatabaseError as exc:
        print(f"No se pudo conectar: {exc}")
        return 3

    try:
        with database.transaction() as sesion:
            foto = medir(sesion)
    finally:
        database.dispose()

    print("proveedor: baileys (unico)")
    print(json.dumps(foto, indent=2, ensure_ascii=False))

    if args.guardar:
        _Path(args.guardar).write_text(
            json.dumps(foto, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nFoto guardada en {args.guardar}")

    if args.comparar:
        ruta = _Path(args.comparar)
        if not ruta.is_file():
            print(f"No existe {ruta}")
            return 2
        comparar(json.loads(ruta.read_text(encoding="utf-8")), foto)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
