"""Devolver a su conversacion los mensajes salientes que acabaron en el chat propio.

QUE PASO
--------
WhatsApp reparte una copia de lo que envias desde el movil a cada dispositivo
vinculado. Esa copia llega con NUESTRO propio identificador como remitente, y
pywhats la archivaba donde apuntaba el remitente (``receiver.py:394``:
``chat_jid = sender_jid``) en vez de donde decia el protobuf. El resultado
medido: mensajes dirigidos a otras personas guardados en el chat personal.

QUE CUENTA COMO PRUEBA
----------------------
Un mensaje solo es candidato si su propio ``raw_proto`` lo demuestra:

  1. el campo 31 (``device_sent_message``) esta presente, lo que significa que
     lo envio este dispositivo;
  2. su ``destination_jid`` NO es una identidad propia.

No se usa el tipo, ni el remitente, ni la fecha, ni ninguna heuristica. Un
mensaje sin ``raw_proto`` nunca es candidato: no hay con que demostrarlo, y
ante la duda no se toca.

EL AUTO-MENSAJE SE QUEDA
------------------------
Una nota para uno mismo tambien lleva el envoltorio, con ``destination_jid``
igual a la identidad propia. Su sitio ES el chat personal, asi que la
condicion (2) lo excluye. En los datos reales hay una imagen exactamente asi.

USO
---
    python scripts/repair_outgoing_routing.py              # auditoria, no toca nada
    python scripts/repair_outgoing_routing.py --apply      # mueve, en UNA transaccion
    python scripts/repair_outgoing_routing.py --show-text  # incluye el texto

Sin ``--apply`` no se escribe una sola fila. El texto de los mensajes no se
imprime salvo que se pida a proposito.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select, text as sql_text, update  # noqa: E402

from app.core.config import load_settings  # noqa: E402
from app.core.database import Database  # noqa: E402
from app.core.device_sent import route  # noqa: E402
from app.core.identity import mask, own_identity  # noqa: E402
from app.models import Chat, MediaFile, Message  # noqa: E402
from app.services import repository as repo  # noqa: E402
from app.services.chat_alias import canonical_chat_jid  # noqa: E402


@dataclass
class Candidato:
    """Un mensaje que el protobuf demuestra dirigido a otra conversacion."""

    id: int
    whatsapp_message_id: str | None
    chat_actual: str
    chat_actual_id: int
    destino_jid: str
    chat_destino: str
    message_type: str
    timestamp: int
    source: str
    texto: str | None
    ya_existe_en_destino: bool = False
    # Un auto-mensaje ya esta en su sitio, pero pywhats lo marco recibido.
    # Corregir la autoria no lo mueve de conversacion.
    solo_autoria: bool = False


def auditar(session, propios: frozenset[str]) -> list[Candidato]:
    """Recorre los mensajes con ``raw_proto`` y devuelve los que hay que mover.

    Se recorre la tabla entera a proposito: el fallo no distingue por chat, y
    limitar la busqueda al chat propio daria por bueno cualquier otro sitio
    donde el mismo error hubiera dejado un mensaje.
    """
    candidatos: list[Candidato] = []
    filas = session.execute(
        select(
            Message.id,
            Message.whatsapp_message_id,
            Message.chat_id,
            Message.chat_jid,
            Message.message_type,
            Message.timestamp,
            Message.source,
            Message.text,
            Message.from_me,
            Message.raw_proto,
        ).where(Message.raw_proto.isnot(None))
    )

    for fila in filas:
        decidido = route(
            bytes(fila.raw_proto), chat_jid=fila.chat_jid, own_identifiers=propios
        )
        if not decidido.es_saliente:
            # Entrante: sin envoltorio no hay nada que demostrar ni que tocar.
            continue

        destino = canonical_chat_jid(session, decidido.chat_jid)
        if destino == fila.chat_jid:
            # Esta en la conversacion correcta. Puede seguir estando mal la
            # AUTORIA: pywhats marca ``from_me=False`` incluso cuando el
            # envoltorio demuestra que lo enviamos nosotros. Es el caso de las
            # notas para uno mismo, que no se mueven a ningun sitio.
            if fila.from_me:
                continue
            candidatos.append(
                Candidato(
                    id=fila.id,
                    whatsapp_message_id=fila.whatsapp_message_id,
                    chat_actual=fila.chat_jid,
                    chat_actual_id=fila.chat_id,
                    destino_jid=decidido.chat_jid,
                    chat_destino=destino,
                    message_type=fila.message_type,
                    timestamp=fila.timestamp,
                    source=fila.source,
                    texto=fila.text,
                    solo_autoria=True,
                )
            )
            continue

        duplicado = False
        if fila.whatsapp_message_id:
            duplicado = session.execute(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.chat_jid == destino,
                    Message.whatsapp_message_id == fila.whatsapp_message_id,
                    Message.id != fila.id,
                )
            ).scalar_one() > 0

        candidatos.append(
            Candidato(
                id=fila.id,
                whatsapp_message_id=fila.whatsapp_message_id,
                chat_actual=fila.chat_jid,
                chat_actual_id=fila.chat_id,
                destino_jid=decidido.chat_jid,
                chat_destino=destino,
                message_type=fila.message_type,
                timestamp=fila.timestamp,
                source=fila.source,
                texto=fila.text,
                ya_existe_en_destino=duplicado,
            )
        )
    return candidatos


def informar(candidatos: list[Candidato], *, mostrar_texto: bool) -> None:
    if not candidatos:
        print("No hay mensajes mal enrutados: nada que reparar.")
        return

    mover = [c for c in candidatos if not c.solo_autoria]
    autoria = [c for c in candidatos if c.solo_autoria]
    print(
        f"{len(mover)} mensaje(s) que el protobuf situa en otra conversacion, y "
        f"{len(autoria)} bien situado(s) pero marcado(s) como recibido(s):\n"
    )
    for c in candidatos:
        print(f"  message_id      = {c.id}")
        print(f"  wamid           = {c.whatsapp_message_id}")
        print(f"  actual_chat     = {mask(c.chat_actual)}")
        print(f"  destination_jid = {mask(c.destino_jid)}")
        print(f"  destination_chat= {mask(c.chat_destino)}")
        print(f"  type            = {c.message_type}")
        print(f"  timestamp       = {c.timestamp}")
        print(f"  source          = {c.source}")
        if c.solo_autoria:
            print("  ACCION          = solo autoria (from_me -> True); NO se mueve")
        if c.ya_existe_en_destino:
            print("  NOTA            = ya existe en el destino; se elimina el duplicado")
        if mostrar_texto:
            print(f"  text            = {c.texto!r}")
        print()

    origen: dict[str, int] = {}
    for c in candidatos:
        origen[c.chat_actual] = origen.get(c.chat_actual, 0) + 1
    print("Resumen por chat de origen:")
    for jid, cuantos in sorted(origen.items(), key=lambda x: -x[1]):
        print(f"  {mask(jid)}: {cuantos}")


def aplicar(session, candidatos: list[Candidato]) -> dict[str, int]:
    """Mueve los mensajes. El llamador abre y cierra la transaccion.

    Cualquier excepcion se propaga para que la transaccion entera revierta: o
    se mueven todos o no se mueve ninguno.
    """
    movidos = eliminados = 0
    afectados: set[str] = set()

    corregidos = 0
    for c in candidatos:
        if c.solo_autoria:
            # No cambia de chat: solo se corrige quien lo envio.
            session.execute(
                update(Message).where(Message.id == c.id).values(from_me=True)
            )
            corregidos += 1
            continue

        afectados.add(c.chat_actual)
        afectados.add(c.chat_destino)

        if c.ya_existe_en_destino:
            # El mensaje YA esta bien guardado en su chat (llego tambien por
            # History Sync). Mover esta copia violaria el indice unico
            # (chat_jid, wamid), que es justo la deduplicacion. Se elimina la
            # copia mal enrutada, no la buena.
            session.execute(
                sql_text("DELETE FROM media_files WHERE message_id = :m"),
                {"m": c.id},
            )
            session.execute(sql_text("DELETE FROM messages WHERE id = :m"), {"m": c.id})
            eliminados += 1
            continue

        chat_destino_id = repo.upsert_chat(
            session,
            jid=c.chat_destino,
            chat_type=_tipo_de_chat(c.chat_destino),
        )
        session.execute(
            update(Message)
            .where(Message.id == c.id)
            .values(
                chat_id=chat_destino_id,
                chat_jid=c.chat_destino,
                # El envoltorio ES la prueba de autoria: lo envio este
                # dispositivo. pywhats lo habia dejado en False.
                from_me=True,
            )
        )
        # El adjunto cuelga del mensaje, y lleva su propia copia del chat.
        session.execute(
            update(MediaFile)
            .where(MediaFile.message_id == c.id)
            .values(chat_id=chat_destino_id)
        )
        movidos += 1

    # Vista previa, ultimo mensaje y estado de historial de CADA chat tocado,
    # tanto el que pierde mensajes como el que los gana.
    for jid in sorted(afectados):
        _refrescar_chat(session, jid)

    return {
        "movidos": movidos,
        "eliminados": eliminados,
        "autoria": corregidos,
        "chats": len(afectados),
    }


def _tipo_de_chat(jid: str) -> str:
    from app.core.message_parser import classify_chat

    return classify_chat(jid)


def _refrescar_chat(session, jid: str) -> None:
    """Recalcula ultimo mensaje y estado de historial desde los datos reales."""
    ultimo = session.execute(
        select(Message.text, Message.message_type, Message.timestamp, Message.raw_proto)
        .where(Message.chat_jid == jid)
        .order_by(Message.timestamp.desc())
        .limit(1)
    ).first()

    if ultimo is not None:
        from app.core.previews import preview_for

        session.execute(
            update(Chat)
            .where(Chat.jid == jid)
            .values(
                last_message=preview_for(
                    ultimo.message_type,
                    ultimo.text,
                    raw_proto=bytes(ultimo.raw_proto) if ultimo.raw_proto else None,
                ),
                last_message_timestamp=ultimo.timestamp,
            )
        )
    else:
        session.execute(
            update(Chat)
            .where(Chat.jid == jid)
            .values(last_message=None, last_message_timestamp=None)
        )

    # Recuenta message_count y los extremos. No toca el cursor ni el estado:
    # mover un mensaje no cambia lo que WhatsApp nos ha entregado.
    repo.refresh_history_state(session, jid)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Devuelve a su conversacion los mensajes salientes mal enrutados. "
            "Sin --apply no escribe nada."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="aplica los cambios en una unica transaccion (por defecto: solo audita)",
    )
    parser.add_argument(
        "--show-text",
        action="store_true",
        help="incluye el texto de los mensajes en el informe (por defecto no)",
    )
    args = parser.parse_args()

    settings = load_settings()
    pn, lid = own_identity(settings)
    propios = frozenset(i for i in (pn, lid) if i)
    if not propios:
        print(
            "No hay identidad propia en la sesion: sin ella no se puede "
            "distinguir una nota para uno mismo de un mensaje a otra persona. "
            "No se toca nada.",
            file=sys.stderr,
        )
        return 2
    print(f"Identidad propia: {', '.join(sorted(mask(i) for i in propios))}\n")

    database = Database(settings)
    database.connect()

    with database.transaction() as session:
        candidatos = auditar(session, propios)
        informar(candidatos, mostrar_texto=args.show_text)

        if not args.apply:
            print("\nModo auditoria: no se ha escrito nada. Usa --apply para reparar.")
            return 0
        if not candidatos:
            return 0

        antes = _conteos(session, candidatos)
        resumen = aplicar(session, candidatos)
        session.flush()
        despues = _conteos(session, candidatos)

    print(
        f"\nReparado: {resumen['movidos']} movido(s), "
        f"{resumen['eliminados']} duplicado(s) eliminado(s), "
        f"{resumen['autoria']} autoria(s) corregida(s), "
        f"{resumen['chats']} chat(s) actualizados."
    )
    print("\nConteos por chat (antes -> despues):")
    for jid in sorted(antes):
        print(f"  {mask(jid)}: {antes[jid]} -> {despues[jid]}")
    return 0


def _conteos(session, candidatos: list[Candidato]) -> dict[str, int]:
    jids = {c.chat_actual for c in candidatos} | {c.chat_destino for c in candidatos}
    return {
        jid: session.execute(
            select(func.count()).select_from(Message).where(Message.chat_jid == jid)
        ).scalar_one()
        for jid in jids
    }


if __name__ == "__main__":
    raise SystemExit(main())
