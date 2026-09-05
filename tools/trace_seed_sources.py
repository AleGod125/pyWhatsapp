"""¿De dónde puede salir una referencia, y cuántas hay en cada sitio? Solo lectura.

LA PREGUNTA DEL PLAN J
----------------------
Para pedirle historial a WhatsApp hace falta un ancla: identificador de mensaje
real, marca real, dirección real. Hoy la mayoría de esas anclas las aporta el
segundo dispositivo, y por eso hacen falta dos códigos QR.

La pregunta es si esas mismas anclas **ya vienen** en lo que la sesión
principal recibe y no materializa. No «si pywhats las expone» — eso ya se sabe
que no— sino si el servidor las manda.

Se distingue con cuidado algo que es fácil confundir:

* una ``Conversation`` **sin mensajes dentro** en el bootstrap;
* un chat **sin ningún ``WebMessageInfo``** en todo lo que se ha recibido.

No es lo mismo. Lo primero se midió antes; lo segundo es lo que decide.

    py tools/trace_seed_sources.py
    py tools/trace_seed_sources.py --bootstrap-solo

SOLO LECTURA
------------
Lee los blobs ya archivados en ``data/history/`` y consulta PostgreSQL. No pide
nada a la red, no descifra y no escribe ni una fila.

PRIVACIDAD
----------
Ni texto, ni teléfonos completos, ni identificadores completos. Los chats se
identifican por un hash corto y estable, que sirve para cruzar tablas sin
nombrar a nadie.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import load_settings  # noqa: E402
from app.models import Chat, HistorySeed, Message  # noqa: E402

LINEA = "=" * 70
BLOBS = pathlib.Path("data/history")


def hash_de(jid: str) -> str:
    """Un identificador estable que no nombra a nadie."""
    return hashlib.sha256((jid or "").encode()).hexdigest()[:8]


def clase_de(jid: str) -> str:
    if not jid or "@" not in jid:
        return "?"
    servidor = jid.split("@")[1]
    return {
        "g.us": "grupo",
        "lid": "individual(LID)",
        "s.whatsapp.net": "individual(PN)",
        "c.us": "individual(PN)",
        "newsletter": "newsletter",
        "broadcast": "difusion",
        "bot": "bot",
    }.get(servidor, servidor)


# ---------------------------------------------------------------------------
# Los blobs
# ---------------------------------------------------------------------------


def anclas_de_un_blob(ruta: pathlib.Path) -> dict[str, list[tuple[str, int, bool]]]:
    """Por chat, las referencias REALES que trae ese blob.

    «Real» quiere decir que pasa el MISMO filtro que usa el motor de
    excavación. No se cuenta lo que no serviría para pedir historial.
    """
    from pywhats.proto import history_sync_pb2 as hs

    from app.core.message_parser import parse_web_message_info
    from app.services.repository import is_valid_history_cursor_id

    salida: dict[str, list[tuple[str, int, bool]]] = defaultdict(list)
    try:
        sync = hs.HistorySync()
        sync.ParseFromString(ruta.read_bytes())
    except Exception:  # noqa: BLE001 - un blob ilegible no corta el recorrido
        return salida

    for conversacion in sync.conversations:
        chat_jid = conversacion.id or ""
        for envoltorio in conversacion.messages:
            crudo = getattr(envoltorio, "message", None)
            if not crudo:
                continue
            try:
                mensaje = parse_web_message_info(crudo)
            except Exception:  # noqa: BLE001
                continue
            if mensaje is None:
                continue
            wamid = getattr(mensaje, "whatsapp_message_id", None)
            marca = getattr(mensaje, "timestamp", None)
            if not wamid or not marca or not is_valid_history_cursor_id(wamid):
                continue
            salida[getattr(mensaje, "chat_jid", None) or chat_jid].append(
                (wamid, int(marca), bool(getattr(mensaje, "from_me", False)))
            )
    return salida


def recorrer(solo_bootstrap: bool) -> dict[str, dict[str, set]]:
    """Qué chats tienen ancla, y de qué tipo de blob salió."""
    porTipo: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    if not BLOBS.exists():
        return porTipo

    for ruta in sorted(BLOBS.glob("*.pb")):
        tipo = "OTRO"
        for candidato in (
            "INITIAL_BOOTSTRAP",
            "ON_DEMAND",
            "NON_BLOCKING_DATA",
            "PUSH_NAME",
            "INITIAL_STATUS_V3",
            "RECENT",
            "FULL",
        ):
            if candidato in ruta.name:
                tipo = candidato
                break
        if solo_bootstrap and tipo != "INITIAL_BOOTSTRAP":
            continue
        for chat_jid, anclas in anclas_de_un_blob(ruta).items():
            if anclas:
                porTipo[tipo]["chats"].add(chat_jid)
                for wamid, _marca, _mio in anclas:
                    porTipo[tipo]["anclas"].add(wamid)
    return porTipo


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bootstrap-solo",
        action="store_true",
        help="solo el bootstrap inicial, que es lo que hay al vincular",
    )
    args = parser.parse_args()

    settings = load_settings()
    motor = create_engine(settings.database_url)

    # -- 1. Lo que hay en los blobs ----------------------------------------
    print(LINEA)
    print("REFERENCIAS QUE TRAEN LOS BLOQUES YA RECIBIDOS")
    print(LINEA)
    porTipo = recorrer(args.bootstrap_solo)
    if not porTipo:
        print("  No hay blobs archivados en data/history/.")
    else:
        print(f"  {'tipo de bloque':<22} {'chats con ancla':>16} {'anclas':>10}")
        todos_los_chats: set[str] = set()
        for tipo, datos in sorted(
            porTipo.items(), key=lambda x: -len(x[1]["chats"])
        ):
            print(
                f"  {tipo:<22} {len(datos['chats']):>16} {len(datos['anclas']):>10}"
            )
            todos_los_chats |= datos["chats"]
        print(f"  {'TOTAL sin repetir':<22} {len(todos_los_chats):>16}")

    # -- 2. Lo que hay en la base ------------------------------------------
    print()
    print(LINEA)
    print("REFERENCIAS GUARDADAS, POR ORIGEN")
    print(LINEA)
    with Session(motor) as sesion:
        total_chats = sesion.execute(select(func.count()).select_from(Chat)).scalar()
        por_origen = sesion.execute(
            select(HistorySeed.source, func.count(func.distinct(HistorySeed.chat_jid)))
            .group_by(HistorySeed.source)
        ).all()
        print(f"  conversaciones en la base      {total_chats}")
        print()
        print(f"  {'origen del ancla':<24} {'chats distintos':>16}")
        for origen, cuantos in sorted(por_origen, key=lambda x: -x[1]):
            print(f"  {str(origen):<24} {cuantos:>16}")

        # -- 3. La pregunta que decide -------------------------------------
        #
        # De los chats que HOY tienen ancla, ¿cuántos la tendrían usando SOLO
        # lo que llega al vincular? Es la cobertura inicial de un solo QR.
        con_ancla = {
            j
            for (j,) in sesion.execute(
                select(func.distinct(HistorySeed.chat_jid))
            ).all()
            if j
        }
        con_mensajes = {
            j
            for (j,) in sesion.execute(
                select(func.distinct(Message.chat_jid))
            ).all()
            if j
        }

    del_bootstrap = porTipo.get("INITIAL_BOOTSTRAP", {}).get("chats", set())

    print()
    print(LINEA)
    print("LA PREGUNTA QUE DECIDE UN SOLO CODIGO QR")
    print(LINEA)
    print(f"  chats con ancla hoy                    {len(con_ancla)}")
    print(f"  chats con mensajes hoy                 {len(con_mensajes)}")
    print(f"  chats con ancla EN EL BOOTSTRAP        {len(del_bootstrap)}")
    if con_ancla:
        porcentaje = round(len(del_bootstrap & con_ancla) / len(con_ancla) * 100)
        print(f"  de las anclas de hoy, ya venian        {porcentaje}%")
    print()
    print("  El bootstrap es lo UNICO que llega al vincular. Todo lo demas")
    print("  —ON_DEMAND incluido— necesita un ancla previa para pedirse, asi")
    print("  que no cuenta como cobertura inicial.")

    # -- 4. Tabla por chat, sin nombrar a nadie ----------------------------
    print()
    print(LINEA)
    print("POR CONVERSACION")
    print(LINEA)
    print(f"  {'chat':<10} {'clase':<18} {'ancla hoy':>10} {'en bootstrap':>14}")
    with Session(motor) as sesion:
        jids = [j for (j,) in sesion.execute(select(Chat.jid)).all() if j]
    for jid in sorted(jids, key=clase_de)[:60]:
        print(
            f"  {hash_de(jid):<10} {clase_de(jid):<18} "
            f"{('si' if jid in con_ancla else 'no'):>10} "
            f"{('si' if jid in del_bootstrap else 'no'):>14}"
        )

    print()
    print("Solo lectura: no se ha escrito ni una fila.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
