"""Registrar los adjuntos que se guardaron sin su fila en ``media_files``.

QUE PASO
--------
El multimedia SALIENTE se perdia entero. El mensaje se guardaba bien (chat
correcto, ``from_me=True``, tipo correcto), pero el adjunto no llegaba a
registrarse, asi que no habia nada que descargar y la interfaz mostraba
"Imagen no disponible".

La causa estaba en una sola linea, repetida en los dos registradores de
``LiveMessageService``::

    Message.chat_jid == jid_to_string(message.chat)

``message.chat`` es lo que dice pywhats, o sea NUESTRO propio identificador,
porque un mensaje saliente llega como copia desde nuestro dispositivo. Pero la
fila ya se habia guardado bajo el chat del DESTINATARIO. La busqueda no
encontraba nada, se devolvia ``False`` y el adjunto desaparecia sin un solo
error en el log.

Y no faltaba ningun dato: en la traza del protobuf estaban ``media_key``,
``direct_path``, los dos hashes, la ``url``, la miniatura y hasta el ``ptt``.

QUE HACE ESTE SCRIPT
--------------------
Busca mensajes de tipo descargable que tengan ``raw_proto`` pero ninguna fila
en ``media_files``, extrae el adjunto del protobuf y lo registra en
``pending`` para que el worker lo baje. No descarga nada aqui ni pide nada al
servidor.

USO
---
    python scripts/repair_missing_media.py            # auditoria
    python scripts/repair_missing_media.py --apply    # registra, en UNA transaccion

NUNCA se imprime ``media_key``, ni los hashes, ni el ``direct_path``: solo si
estan presentes.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert  # noqa: E402

from app.core.config import load_settings  # noqa: E402
from app.core.database import Database  # noqa: E402
from app.core.identity import mask  # noqa: E402
from app.core.message_parser import interpret_message_bytes  # noqa: E402
from app.models import MediaFile, Message  # noqa: E402
from app.services.live_service import DOWNLOADABLE_MEDIA_TYPES  # noqa: E402


@dataclass
class Huerfano:
    """Un mensaje con adjunto en el protobuf y sin fila que lo represente."""

    id: int
    whatsapp_message_id: str | None
    chat_id: int
    chat_jid: str
    message_type: str
    timestamp: int
    source: str
    media_type: str
    mime_type: str | None
    file_size: int | None
    duration_seconds: int | None
    tiene_clave: bool
    tiene_direct_path: bool
    tiene_hashes: bool


def auditar(session) -> list[Huerfano]:
    sin_media = ~select(MediaFile.id).where(
        MediaFile.message_id == Message.id
    ).exists()

    filas = session.execute(
        select(
            Message.id,
            Message.whatsapp_message_id,
            Message.chat_id,
            Message.chat_jid,
            Message.message_type,
            Message.timestamp,
            Message.source,
            Message.raw_proto,
        ).where(
            Message.raw_proto.isnot(None),
            Message.message_type.in_(sorted(DOWNLOADABLE_MEDIA_TYPES)),
            sin_media,
        )
    )

    huerfanos: list[Huerfano] = []
    for fila in filas:
        suelto = interpret_message_bytes(bytes(fila.raw_proto))
        if suelto is None or suelto.media is None:
            # Sin adjunto legible no hay nada que registrar. No se inventa.
            continue
        media = suelto.media
        huerfanos.append(
            Huerfano(
                id=fila.id,
                whatsapp_message_id=fila.whatsapp_message_id,
                chat_id=fila.chat_id,
                chat_jid=fila.chat_jid,
                message_type=fila.message_type,
                timestamp=fila.timestamp,
                source=fila.source,
                media_type=media.media_type,
                mime_type=media.mime_type,
                file_size=media.file_size,
                duration_seconds=media.duration_seconds,
                tiene_clave=bool(media.media_key),
                tiene_direct_path=bool(media.direct_path),
                tiene_hashes=bool(media.file_sha256 and media.file_enc_sha256),
            )
        )
    return huerfanos


def informar(huerfanos: list[Huerfano]) -> None:
    if not huerfanos:
        print("No hay adjuntos sin registrar.")
        return

    print(f"{len(huerfanos)} adjunto(s) presentes en el protobuf y sin fila:\n")
    for h in huerfanos:
        print(f"  message_id        = {h.id}")
        print(f"  wamid             = {h.whatsapp_message_id}")
        print(f"  chat              = {mask(h.chat_jid)}")
        print(f"  type              = {h.message_type} -> media_type={h.media_type}")
        print(f"  mime              = {h.mime_type}")
        print(f"  file_length       = {h.file_size}")
        print(f"  duration          = {h.duration_seconds}")
        print(f"  media_key present = {h.tiene_clave}")
        print(f"  direct_path prsnt = {h.tiene_direct_path}")
        print(f"  hashes present    = {h.tiene_hashes}")
        print(f"  source            = {h.source}")
        print()

    por_tipo: dict[str, int] = {}
    for h in huerfanos:
        por_tipo[h.media_type] = por_tipo.get(h.media_type, 0) + 1
    print("Resumen por tipo:")
    for tipo, cuantos in sorted(por_tipo.items(), key=lambda x: -x[1]):
        print(f"  {tipo}: {cuantos}")

    descargables = sum(
        1 for h in huerfanos if h.tiene_clave and h.tiene_direct_path and h.tiene_hashes
    )
    print(
        f"\nCon datos suficientes para descargar: {descargables} de {len(huerfanos)}."
    )
    if descargables < len(huerfanos):
        print(
            "Los demas se registraran igual: la fila conserva lo que si vino, y "
            "el worker los marcara 'unavailable' en vez de perderlos."
        )


def aplicar(session, huerfanos: list[Huerfano]) -> int:
    """Registra las filas en ``pending``. El llamador maneja la transaccion."""
    registrados = 0
    for h in huerfanos:
        crudo = session.execute(
            select(Message.raw_proto).where(Message.id == h.id)
        ).scalar_one()
        suelto = interpret_message_bytes(bytes(crudo))
        if suelto is None or suelto.media is None:
            continue
        media = suelto.media

        session.execute(
            insert(MediaFile)
            .values(
                message_id=h.id,
                chat_id=h.chat_id,
                whatsapp_message_id=h.whatsapp_message_id,
                media_type=media.media_type,
                mime_type=media.mime_type,
                file_name=media.file_name,
                file_size=media.file_size,
                duration_seconds=media.duration_seconds,
                width=media.width,
                height=media.height,
                direct_path=media.direct_path,
                media_key=media.media_key,
                file_sha256=media.file_sha256,
                file_enc_sha256=media.file_enc_sha256,
                download_status="pending",
            )
            .on_conflict_do_nothing(constraint="uq_media_files_message_type")
        )
        registrados += 1
    return registrados


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Registra los adjuntos que estan en el protobuf pero no en "
            "media_files. Sin --apply no escribe nada."
        )
    )
    parser.add_argument("--apply", action="store_true", help="registra las filas")
    args = parser.parse_args()

    database = Database(load_settings())
    database.connect()

    with database.transaction() as session:
        huerfanos = auditar(session)
        informar(huerfanos)
        if not args.apply:
            print("\nModo auditoria: no se ha escrito nada. Usa --apply para registrar.")
            return 0
        if not huerfanos:
            return 0
        registrados = aplicar(session, huerfanos)

    print(
        f"\nRegistrados {registrados} adjunto(s) en 'pending'. El worker de "
        "multimedia los descargara en su siguiente ronda."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
