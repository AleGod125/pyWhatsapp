"""¿Puede la sesión principal conseguir las referencias por sí sola?

LA PREGUNTA
-----------
Una conversación sin ancla no se le puede pedir a WhatsApp: `ON_DEMAND` va
anclado. Hasta ahora la respuesta a «¿de dónde sale el ancla?» era el segundo
dispositivo vinculado. Esta herramienta mira si hace falta.

Recorre TODAS las fuentes que la sesión principal ya recibe — los blobs de
History Sync archivados y lo que hay en PostgreSQL — y dice, conversación por
conversación, si alguna de ellas trae un identificador de mensaje REAL.

    py tools/diagnose_primary_seed_sources.py
    py tools/diagnose_primary_seed_sources.py --detalle

Sólo lee. No escribe, no pide nada al servidor y no toca la sesión.

QUE ES UN ANCLA VALIDA
----------------------
Lo decide `app.history.seed_collector.validar`, el mismo filtro que usa el
motor. Aquí no hay una segunda opinión: si aquél lo rechaza, esto también.

PRIVACIDAD
----------
Ni texto de mensajes, ni nombres, ni teléfonos completos, ni identificadores
completos. Sólo recuentos, clases y longitudes.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.compat.history_compat import parse_full  # noqa: E402
from app.core.config import load_settings  # noqa: E402
from app.history.seed_collector import SeedCandidate, validar  # noqa: E402
from app.models import (  # noqa: E402
    SEEDLESS_STATUSES,
    Chat,
    ChatHistoryState,
    HistorySeed,
    Message,
)

LINEA = "=" * 70


def _corto(jid: str) -> str:
    usuario, _, servidor = (jid or "").partition("@")
    return f"{usuario[:6]}...@{servidor}"


def _clase(jid: str) -> str:
    if jid.endswith("@g.us"):
        return "grupo"
    return "individual"


# ---------------------------------------------------------------------------
# Las fuentes
# ---------------------------------------------------------------------------


def _de_los_blobs(carpeta: Path) -> dict[str, dict]:
    """Lo que traen los blobs de History Sync archivados, por conversación.

    Se separan por tipo de sync porque no valen lo mismo: un `ON_DEMAND` sólo
    llega si YA había un ancla, así que no sirve como origen. El que cuenta es
    el `INITIAL_BOOTSTRAP`, que es lo que WhatsApp entrega al vincular.
    """
    from app.models.proto import WebMessageInfo

    salida: dict[str, dict] = {}
    if not carpeta.exists():
        return salida

    for ruta in sorted(carpeta.glob("*.pb")):
        try:
            completo = parse_full(ruta.read_bytes())
        except Exception:  # noqa: BLE001 - un blob ilegible no para el resto
            continue
        tipo = completo.sync_type
        for conversacion in completo.conversations:
            ficha = salida.setdefault(
                conversacion.jid,
                {"tipos": Counter(), "mejor": None, "mensajes": 0},
            )
            ficha["tipos"][tipo] += len(conversacion.messages)
            ficha["mensajes"] += len(conversacion.messages)

            # `ON_DEMAND` no cuenta como origen: para recibirlo hacia falta ya
            # tener un ancla.
            if tipo == "ON_DEMAND":
                continue
            for crudo, _ in conversacion.messages:
                info = WebMessageInfo()
                try:
                    info.ParseFromString(crudo)
                except Exception:  # noqa: BLE001
                    continue
                if not info.key.HasField("ID"):
                    continue
                candidato = SeedCandidate(
                    chat_jid=conversacion.jid,
                    wa_msg_id=info.key.ID,
                    timestamp=int(info.messageTimestamp) or None,
                    from_me=bool(info.key.fromMe),
                    source=f"blob:{tipo}",
                )
                if validar(candidato) is None:
                    anterior = ficha["mejor"]
                    # El MAS ANTIGUO: se excava hacia atras.
                    if anterior is None or candidato.timestamp < anterior.timestamp:
                        ficha["mejor"] = candidato
    return salida


def _de_la_base(sesion: Session) -> dict[str, SeedCandidate]:
    """Mensajes ya guardados con identificador real, que sirven de ancla."""
    salida: dict[str, SeedCandidate] = {}
    filas = sesion.execute(
        select(
            Message.chat_jid,
            Message.whatsapp_message_id,
            Message.timestamp,
            Message.from_me,
            Message.message_type,
            Message.source,
        ).where(Message.whatsapp_message_id.isnot(None))
    ).all()
    for jid, wamid, ts, from_me, tipo, origen in filas:
        candidato = SeedCandidate(
            chat_jid=jid,
            wa_msg_id=wamid,
            timestamp=ts,
            from_me=bool(from_me),
            source=f"db:{origen}",
            message_type=tipo,
        )
        if validar(candidato) is not None:
            continue
        anterior = salida.get(jid)
        if anterior is None or candidato.timestamp < anterior.timestamp:
            salida[jid] = candidato
    return salida


# ---------------------------------------------------------------------------
# Informe
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detalle", action="store_true", help="una linea por chat")
    args = parser.parse_args()

    settings = load_settings()
    blobs = _de_los_blobs(Path("data/history"))

    with Session(create_engine(settings.database_url)) as sesion:
        de_la_base = _de_la_base(sesion)
        anclas = {
            j for (j,) in sesion.execute(select(HistorySeed.chat_jid).distinct()).all()
        }
        chats = sesion.execute(
            select(Chat.jid, ChatHistoryState.history_status)
            .outerjoin(ChatHistoryState, ChatHistoryState.chat_jid == Chat.jid)
        ).all()
        mensajes_por_chat = dict(
            sesion.execute(
                select(Message.chat_jid, func.count()).group_by(Message.chat_jid)
            ).all()
        )

    print(LINEA)
    print("FUENTES PRIMARIAS DE ANCLA")
    print(LINEA)
    print(f"  blobs archivados leidos        {len(list(Path('data/history').glob('*.pb')))}")
    print(f"  conversaciones en los blobs    {len(blobs)}")
    print(f"  conversaciones en la base      {len(chats)}")

    # Que aporto cada tipo de sync.
    por_tipo: Counter = Counter()
    for ficha in blobs.values():
        for tipo, cuantos in ficha["tipos"].items():
            por_tipo[tipo] += cuantos
    print()
    print("  mensajes por tipo de History Sync:")
    for tipo, cuantos in por_tipo.most_common():
        marca = "  (no sirve de origen: exige ancla previa)" if tipo == "ON_DEMAND" else ""
        print(f"    {tipo:<22} {cuantos:>6}{marca}")

    con_primaria = 0
    esperando_con_primaria = 0
    esperando_sin_nada = 0
    detalle: list[tuple] = []

    for jid, estado in chats:
        del_blob = (blobs.get(jid) or {}).get("mejor")
        de_db = de_la_base.get(jid)
        mejor = del_blob or de_db
        if del_blob and de_db and de_db.timestamp < del_blob.timestamp:
            mejor = de_db

        esperando = estado in SEEDLESS_STATUSES
        if mejor is not None:
            con_primaria += 1
            if esperando:
                esperando_con_primaria += 1
        elif esperando:
            esperando_sin_nada += 1

        detalle.append(
            (
                jid,
                estado,
                esperando,
                mejor,
                int(mensajes_por_chat.get(jid, 0)),
                jid in anclas,
            )
        )

    print()
    print(LINEA)
    print("COBERTURA")
    print(LINEA)
    print(f"  con candidato primario         {con_primaria} de {len(chats)}")
    print(f"  esperando ancla PERO con uno   {esperando_con_primaria}")
    print(f"  esperando y sin NADA           {esperando_sin_nada}")
    print()
    if esperando_con_primaria:
        print("  Hay conversaciones esperando ancla que SI tienen candidato en")
        print("  una fuente primaria. Merece la pena revisar por que no se")
        print("  aprovecho.")
    else:
        print("  Ninguna conversacion que espera ancla tiene candidato primario.")
        print("  Lo que WhatsApp entrega a un companion no alcanza para ellas:")
        print("  la via del segundo dispositivo sigue haciendo falta.")

    if args.detalle:
        print()
        print(LINEA)
        print("POR CONVERSACION")
        print(LINEA)
        print(f"  {'chat':<24}{'estado':<16}{'msgs':>6}  candidato")
        for jid, estado, esperando, mejor, cuantos, tiene_ancla in sorted(
            detalle, key=lambda f: (f[3] is None, f[1] or "")
        ):
            if mejor is None:
                nota = "-"
            else:
                nota = (
                    f"{mejor.source} id={len(mejor.wa_msg_id)}c "
                    f"ts={mejor.timestamp} propio={mejor.from_me}"
                )
            print(
                f"  {_corto(jid):<24}{str(estado or '-'):<16}{cuantos:>6}  {nota}"
            )

    print()
    print("Solo lectura: no se ha escrito nada ni se ha pedido nada al servidor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
