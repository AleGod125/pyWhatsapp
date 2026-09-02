"""Persistencia de los mensajes que llegan en vivo.

Pipeline (seccion 26 del brief):

    WhatsApp -> evento 'message' -> normalizacion -> PostgreSQL -> aviso a la GUI

Corre en el hilo del cliente, no en el de Tkinter, para que escribir en la base
no congele la interfaz. Y es deliberadamente ligero: el receptor de mensajes es
lo mas prioritario del programa y no debe quedarse esperando a nadie.

Sobre ``raw_proto``: el evento ``message`` de pywhats entrega un dataclass ya
normalizado (id, chat, sender, text, timestamp, from_me, media) y NO expone el
``WebMessageInfo`` en crudo, asi que en un mensaje live esa columna queda a
NULL. No es una perdida definitiva: si el mismo mensaje vuelve a llegar en un
History Sync, el upsert rellena el hueco sin tocar lo que ya habia
(``bulk_upsert_messages`` usa COALESCE para eso).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app import repository as repo
from app.database import Database
from app.logging_setup import get_logger
from app.message_parser import classify_chat
from app.previews import preview_for
from app.models import MediaFile, Message
from app.repository import IncomingMessage

log = get_logger("WA")

# kind del adjunto -> tipo normalizado nuestro.
_MEDIA_KINDS = {
    "image": "image",
    "video": "video",
    "audio": "audio",
    "document": "document",
    "sticker": "sticker",
}


@dataclass
class LiveStats:
    received: int = 0
    stored: int = 0
    duplicates: int = 0
    media: int = 0
    protocol_filtered: int = 0


def jid_to_string(jid: Any) -> str | None:
    """``JID`` de pywhats -> cadena canonica, conservando el servidor real.

    No se convierte un ``@lid`` en telefono ni al reves: son espacios de
    identificadores distintos y mezclarlos corrompe los datos.
    """
    if jid is None:
        return None
    if isinstance(jid, str):
        return jid or None
    user = getattr(jid, "user", None)
    server = getattr(jid, "server", None)
    if not user:
        return None
    return f"{user}@{server}" if server else str(user)


def message_type_for(message: Any) -> str:
    media = getattr(message, "media", None)
    if media is None:
        return "text" if message.text else "unknown"
    kind = _MEDIA_KINDS.get(getattr(media, "kind", ""), "unknown")
    if kind == "audio" and getattr(media, "ptt", False):
        return "voice_note"
    return kind


class LiveMessageService:
    """Guarda cada mensaje entrante en PostgreSQL en cuanto llega."""

    def __init__(self, database: Database, *, own_jid: str | None = None) -> None:
        self._database = database
        self._own_jid = own_jid
        self.stats = LiveStats()

    def handle(self, message: Any) -> dict[str, Any] | None:
        """Persiste un mensaje. Devuelve un resumen para refrescar la GUI.

        Nunca lanza: un fallo guardando no puede tumbar el receptor. Si algo
        va mal se registra con traza y se sigue escuchando.
        """
        try:
            return self._store(message)
        except Exception:  # noqa: BLE001 - el receptor es lo prioritario
            log.exception("No se pudo guardar un mensaje live; la sesion continua")
            return None

    def _store(self, message: Any) -> dict[str, Any] | None:
        chat_jid = jid_to_string(getattr(message, "chat", None))
        if not chat_jid:
            log.debug("Mensaje live sin chat identificable; se ignora")
            return None

        self.stats.received += 1

        # Clasificacion por PROTOBUF, no por "el texto viene vacio".
        # pywhats emite los ProtocolMessage por este mismo evento, y sin este
        # filtro acaban como mensajes del chat (75 filas medidas en la
        # auditoria del chat propio).
        from app.compat.protocol_flag import last_raw_message
        from app.message_classifier import MessageClass, classify_message_bytes

        raw_proto = last_raw_message()
        clase = (
            classify_message_bytes(raw_proto)
            if raw_proto
            else MessageClass.UNKNOWN_NEEDS_REVIEW
        )
        if clase in (MessageClass.PROTOCOL_INTERNAL, MessageClass.SIGNAL_CONTROL):
            self.stats.protocol_filtered += 1
            log.debug(
                "Evento interno (%s) descartado: no es un mensaje de conversacion",
                clase.value,
            )
            return None
        sender_jid = jid_to_string(getattr(message, "sender", None))
        from_me = bool(getattr(message, "from_me", False))
        if from_me and self._own_jid:
            sender_jid = self._own_jid

        # Los identificadores @lid van a su columna: no se mezclan con los JID.
        sender_lid = sender_jid if sender_jid and sender_jid.endswith("@lid") else None
        if sender_lid:
            sender_jid = None

        message_type = message_type_for(message)
        text = getattr(message, "text", "") or None
        media = getattr(message, "media", None)
        if not text and media is not None and getattr(media, "caption", ""):
            text = media.caption

        # pywhats no desenvuelve ``deviceSentMessage``: las fotos que el
        # usuario se envia a si mismo llegaban con ``media=None`` y acababan
        # guardadas como 'unknown'. Cuando pywhats no sabe que es, se mira el
        # protobuf, que si lo dice (seccion 32).
        propio_metadata: dict[str, Any] | None = None
        if message_type == "unknown" and raw_proto:
            from app.message_parser import interpret_message_bytes

            suelto = interpret_message_bytes(raw_proto)
            if suelto is not None and suelto.message_type != "unknown":
                message_type = suelto.message_type
                text = text or suelto.text
                propio_metadata = {}
                if suelto.wrappers:
                    propio_metadata["wrappers"] = suelto.wrappers
                if suelto.proto_type:
                    propio_metadata["proto_type"] = suelto.proto_type
                log.debug(
                    "Tipo recuperado del protobuf: %s (%s)",
                    message_type,
                    ",".join(suelto.wrappers) or "sin envoltorio",
                )

        with self._database.transaction() as session:
            chat_id = repo.upsert_chat(
                session,
                jid=chat_jid,
                chat_type=classify_chat(chat_jid),
                last_message=preview_for(
                    message_type, text, raw_proto=raw_proto, metadata=propio_metadata
                ),
                last_message_timestamp=int(message.timestamp),
            )

            inserted = repo.bulk_upsert_messages(
                session,
                {chat_jid: chat_id},
                [
                    IncomingMessage(
                        chat_jid=chat_jid,
                        timestamp=int(message.timestamp),
                        source="live",
                        # El id del evento es el ID REAL de WhatsApp.
                        whatsapp_message_id=getattr(message, "id", None) or None,
                        sender_jid=sender_jid,
                        sender_lid=sender_lid,
                        message_type=message_type,
                        text=text,
                        from_me=from_me,
                        raw_metadata=propio_metadata,
                        # Fidelidad: hasta ahora los mensajes live se
                        # guardaban sin raw_proto porque el evento no lo
                        # exponia. Ahora si.
                        raw_proto=raw_proto,
                    )
                ],
            )
            if inserted:
                self.stats.stored += 1
            else:
                # Ya lo teniamos: History Sync y live se solapan a proposito.
                self.stats.duplicates += 1

            if media is not None:
                if self._register_media(session, chat_id, message, media, message_type):
                    self.stats.media += 1
            elif raw_proto and message_type in _MEDIA_KINDS.values() | {
                "voice_note", "gif"
            }:
                # El adjunto lo encontro nuestro parser, no pywhats (caso
                # deviceSentMessage). Se registra igual para que el worker lo
                # descargue sin esperar a un reinicio.
                if self._register_parsed_media(
                    session, chat_id, message, raw_proto, message_type
                ):
                    self.stats.media += 1

            repo.refresh_history_state(session, chat_jid)

        log.info(
            "Mensaje live guardado chat=%s tipo=%s %s",
            chat_jid.split("@")[0][:6] + "...",
            message_type,
            "(nuevo)" if inserted else "(duplicado)",
        )
        return {"chat_jid": chat_jid, "chat_id": chat_id, "new": bool(inserted)}

    def _register_media(
        self, session: Any, chat_id: int, message: Any, media: Any, message_type: str
    ) -> bool:
        """Crea la fila del adjunto en ``pending`` para que el worker lo baje."""
        message_id = session.execute(
            select(Message.id).where(
                Message.chat_jid == jid_to_string(message.chat),
                Message.whatsapp_message_id == message.id,
            )
        ).scalar_one_or_none()
        if message_id is None:
            return False

        media_type = message_type if message_type in _MEDIA_KINDS.values() else message_type
        if getattr(media, "kind", "") == "video" and getattr(media, "mimetype", "").startswith(
            "image/gif"
        ):
            media_type = "gif"

        statement = insert(MediaFile).values(
            message_id=message_id,
            chat_id=chat_id,
            whatsapp_message_id=message.id,
            media_type=media_type,
            mime_type=getattr(media, "mimetype", "") or None,
            file_name=getattr(media, "filename", "") or None,
            file_size=int(getattr(media, "file_length", 0)) or None,
            direct_path=getattr(media, "direct_path", "") or None,
            media_key=bytes(getattr(media, "media_key", b"")) or None,
            file_sha256=bytes(getattr(media, "file_sha256", b"")) or None,
            file_enc_sha256=bytes(getattr(media, "file_enc_sha256", b"")) or None,
            download_status="pending",
        )
        session.execute(
            statement.on_conflict_do_nothing(constraint="uq_media_files_message_type")
        )
        return True

    def _register_parsed_media(
        self,
        session: Any,
        chat_id: int,
        message: Any,
        raw_proto: bytes,
        message_type: str,
    ) -> bool:
        """Registra un adjunto detectado por nuestro parser, no por pywhats.

        Mismo destino y mismas garantias que :meth:`_register_media`: fila en
        ``pending`` y ``ON CONFLICT DO NOTHING`` para no duplicar ni pisar una
        descarga ya hecha. Lo unico distinto es de donde salen los datos.
        """
        from app.message_parser import interpret_message_bytes

        suelto = interpret_message_bytes(raw_proto)
        if suelto is None or suelto.media is None:
            return False

        message_id = session.execute(
            select(Message.id).where(
                Message.chat_jid == jid_to_string(message.chat),
                Message.whatsapp_message_id == message.id,
            )
        ).scalar_one_or_none()
        if message_id is None:
            return False

        media = suelto.media
        session.execute(
            insert(MediaFile)
            .values(
                message_id=message_id,
                chat_id=chat_id,
                whatsapp_message_id=message.id,
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
        return True
