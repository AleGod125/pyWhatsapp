"""Descarga y almacenamiento de multimedia.

Pipeline (seccion 41 del brief):

    mensaje -> media_files(pending) -> worker -> descarga -> verificacion
    -> archivo en data/media/<tipo>/ -> status=downloaded

Se usa ``Client.download_media()``, que es la API real de pywhats. NO se
reimplementa AES ni HKDF, y no se inventa ningun endpoint de CDN: pywhats
resuelve los hosts por su cuenta.

La verificacion tampoco se duplica: ``pywhats.media.crypto.decrypt_media`` ya
comprueba el SHA256 del fichero cifrado, el HMAC y el SHA256 del contenido
descifrado. Si algo no cuadra, lanza; aqui solo se traduce a un estado.

Los workers viven en el event loop del cliente y toman el trabajo de una cola,
asi que las descargas NUNCA bloquean al receptor de mensajes.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update

from app.config import Settings
from app.database import Database
from app.logging_setup import get_logger
from app.models import MediaFile

log = get_logger("MEDIA")

# Subcarpeta por tipo, para que data/media/ sea navegable a mano.
_SUBDIRS = {
    "image": "images",
    "video": "videos",
    "gif": "gifs",
    "audio": "audio",
    "voice_note": "voice_notes",
    "sticker": "stickers",
    "document": "documents",
    "unknown": "other",
}

# Tipo de media de pywhats por cada tipo normalizado nuestro. Determina las
# claves derivadas y la ruta del CDN, asi que no puede fallar.
_CRYPTO_TYPE = {
    "image": "WhatsApp Image Keys",
    "sticker": "WhatsApp Image Keys",
    "video": "WhatsApp Video Keys",
    "gif": "WhatsApp Video Keys",
    "audio": "WhatsApp Audio Keys",
    "voice_note": "WhatsApp Audio Keys",
    "document": "WhatsApp Document Keys",
}

# Extension por defecto cuando el mimetype no ayuda.
_DEFAULT_EXTENSION = {
    "image": ".jpg",
    "sticker": ".webp",
    "video": ".mp4",
    "gif": ".mp4",
    "audio": ".ogg",
    "voice_note": ".ogg",
    "document": ".bin",
}

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Nombres reservados de Windows: un archivo llamado CON o NUL rompe el sistema.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str | None, *, fallback: str) -> str:
    """Nombre de archivo seguro en Windows y en POSIX.

    Elimina cualquier intento de path traversal y los caracteres que Windows
    no admite. Nunca devuelve una cadena vacia.
    """
    if not name:
        return fallback
    # Solo el nombre: descarta cualquier componente de ruta que viniera dentro.
    candidate = Path(name.replace("\\", "/")).name
    candidate = _UNSAFE_CHARS.sub("_", candidate).strip(" .")
    if not candidate:
        return fallback
    stem = candidate.split(".")[0].upper()
    if stem in _RESERVED:
        candidate = f"_{candidate}"
    # Margen para el prefijo del id y la extension.
    return candidate[:120]


def extension_for(mime_type: str | None, media_type: str, file_name: str | None) -> str:
    """Extension a partir del nombre original, el mimetype o el tipo."""
    if file_name and "." in file_name:
        suffix = Path(safe_filename(file_name, fallback="x")).suffix
        if 1 < len(suffix) <= 10:
            return suffix
    if mime_type:
        import mimetypes

        base = mime_type.split(";")[0].strip()
        guessed = mimetypes.guess_extension(base)
        if guessed:
            # mimetypes propone .jpe para image/jpeg; poco util.
            return ".jpg" if guessed == ".jpe" else guessed
    return _DEFAULT_EXTENSION.get(media_type, ".bin")


@dataclass
class MediaStats:
    downloaded: int = 0
    failed: int = 0
    unavailable: int = 0
    expired: int = 0
    deduplicated: int = 0

    @property
    def processed(self) -> int:
        return (
            self.downloaded + self.failed + self.unavailable
            + self.expired + self.deduplicated
        )

    def __str__(self) -> str:
        return (
            f"descargados={self.downloaded} dedup={self.deduplicated} "
            f"no disponibles={self.unavailable} caducados={self.expired} "
            f"fallidos={self.failed}"
        )


class MediaService:
    """Descarga los adjuntos pendientes sin bloquear al receptor."""

    def __init__(self, settings: Settings, database: Database, client: Any) -> None:
        self._settings = settings
        self._database = database
        self._client = client
        self._stats = MediaStats()
        self._running = False

    @property
    def stats(self) -> MediaStats:
        return self._stats

    # -- Seleccion de trabajo -----------------------------------------------

    def pending_ids(self, limit: int = 500) -> list[int]:
        """Adjuntos descargables: los que tienen con que descargarse.

        Sin ``direct_path`` o sin ``media_key`` no hay nada que intentar; esas
        filas se quedan en pending y se registran aparte, porque el mensaje
        sigue siendo valido aunque su adjunto no se pueda recuperar.
        """
        with self._database.transaction() as session:
            rows = session.execute(
                select(MediaFile.id)
                .where(
                    # 'unavailable' y 'expired' son TERMINALES: el CDN ya no
                    # tiene el archivo y reintentarlo en cada arranque solo
                    # gasta tiempo y llena el log. Solo se reintenta lo que
                    # pudo fallar por causas transitorias.
                    MediaFile.download_status.in_(("pending", "failed")),
                    MediaFile.direct_path.is_not(None),
                    MediaFile.media_key.is_not(None),
                    MediaFile.download_attempts < 3,
                )
                .order_by(MediaFile.id)
                .limit(limit)
            ).scalars()
            return list(rows)

    # -- Descarga ------------------------------------------------------------

    async def run(self) -> MediaStats:
        """Procesa toda la cola pendiente con la concurrencia configurada."""
        if self._running:
            log.debug("El servicio de multimedia ya esta en marcha")
            return self._stats
        self._running = True
        try:
            ids = self.pending_ids()
            if not ids:
                log.info("No hay multimedia pendiente")
                return self._stats

            concurrency = self._settings.media_download_concurrency
            log.info("Descargando %d adjuntos (concurrencia=%d)", len(ids), concurrency)

            queue: asyncio.Queue[int] = asyncio.Queue()
            for media_id in ids:
                queue.put_nowait(media_id)

            workers = [
                asyncio.create_task(self._worker(queue), name=f"media-{i}")
                for i in range(concurrency)
            ]
            progreso = asyncio.create_task(
                self._report_progress(len(ids)), name="media-progress"
            )
            await queue.join()
            progreso.cancel()
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

            log.info("Multimedia: %s", self._stats)
            return self._stats
        finally:
            self._running = False

    def pending_count(self) -> int:
        """Cuantos adjuntos quedan por descargar. Consulta barata e indexada."""
        with self._database.transaction() as session:
            return session.execute(
                select(func.count())
                .select_from(MediaFile)
                .where(
                    MediaFile.download_status.in_(("pending", "failed")),
                    MediaFile.direct_path.is_not(None),
                    MediaFile.media_key.is_not(None),
                    MediaFile.download_attempts < 3,
                )
            ).scalar_one()

    async def run_forever(
        self, *, interval: float = 20.0, on_progress: Any = None
    ) -> None:
        """Worker permanente: descarga lo que vaya apareciendo (seccion 13).

        Antes habia que reiniciar la aplicacion para que un adjunto llegado
        despues del arranque se descargara. Ahora el pipeline se cierra solo::

            mensaje -> media_files(pending) -> este worker -> downloaded
                    -> aviso a la GUI -> la burbuja se actualiza

        Vive en su propia tarea y la GUI nunca espera por el (seccion 14).
        Entre rondas duerme: no es un bucle ocupado. Termina limpiamente al
        cancelarse.
        """
        log.info("Worker de multimedia en marcha (revision cada %.0fs)", interval)
        try:
            while True:
                antes = self._stats.downloaded + self._stats.deduplicated
                try:
                    # Comprobar primero evita escribir "no hay multimedia
                    # pendiente" en el log cada veinte segundos para siempre.
                    if self.pending_count():
                        await self.run()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - una ronda mala no lo mata
                    log.exception("Ronda de multimedia fallida; se reintentara")
                nuevos = self._stats.downloaded + self._stats.deduplicated - antes
                if nuevos and on_progress is not None:
                    try:
                        on_progress(self._stats, nuevos)
                    except Exception:  # noqa: BLE001
                        log.debug("El aviso de progreso de multimedia fallo")
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            log.info("Worker de multimedia detenido")
            raise

    async def _report_progress(self, total: int, every: float = 10.0) -> None:
        """Resumen periodico en INFO, en lugar de una linea por adjunto."""
        try:
            while True:
                await asyncio.sleep(every)
                stats = self._stats
                log.info(
                    "Procesando multimedia %d/%d  descargados=%d dedup=%d "
                    "no disponibles=%d caducados=%d fallidos=%d",
                    stats.processed,
                    total,
                    stats.downloaded,
                    stats.deduplicated,
                    stats.unavailable,
                    stats.expired,
                    stats.failed,
                )
        except asyncio.CancelledError:
            return

    async def _worker(self, queue: asyncio.Queue[int]) -> None:
        while True:
            media_id = await queue.get()
            try:
                await self._download_one(media_id)
            except asyncio.CancelledError:
                queue.task_done()
                raise
            except Exception:  # noqa: BLE001 - un adjunto roto no para la cola
                log.exception("Fallo inesperado descargando el adjunto %d", media_id)
            finally:
                if not queue.empty() or True:
                    queue.task_done()

    async def _download_one(self, media_id: int) -> None:
        from pywhats.media.download import MediaInfo

        with self._database.transaction() as session:
            row = session.execute(
                select(
                    MediaFile.id,
                    MediaFile.message_id,
                    MediaFile.media_type,
                    MediaFile.mime_type,
                    MediaFile.file_name,
                    MediaFile.direct_path,
                    MediaFile.media_key,
                    MediaFile.file_sha256,
                    MediaFile.file_enc_sha256,
                ).where(MediaFile.id == media_id)
            ).one_or_none()
            if row is None:
                return
            session.execute(
                update(MediaFile)
                .where(MediaFile.id == media_id)
                .values(download_status="downloading")
            )

        (
            _id,
            message_id,
            media_type,
            mime_type,
            file_name,
            direct_path,
            media_key,
            file_sha256,
            file_enc_sha256,
        ) = row

        # Deduplicacion: si ya existe el mismo contenido en disco, se reutiliza
        # el archivo sin volver a descargarlo. La relacion mensaje<->media se
        # mantiene intacta: cada fila conserva la suya.
        if file_sha256:
            existing = self._existing_path(file_sha256, media_id)
            if existing is not None and (self._settings.media_dir / existing).exists():
                self._finish(media_id, existing, dedup=True)
                return

        crypto_type = _CRYPTO_TYPE.get(media_type)
        if crypto_type is None:
            self._fail(media_id, "unavailable", f"tipo sin soporte de descarga: {media_type}")
            return

        info = MediaInfo(
            direct_path=direct_path,
            media_key=bytes(media_key),
            file_sha256=bytes(file_sha256 or b""),
            file_enc_sha256=bytes(file_enc_sha256 or b""),
            media_type=crypto_type,
        )

        try:
            # download_media verifica enc-SHA256, HMAC y SHA256 del contenido.
            payload = await self._client.download_media(info)
        except Exception as exc:  # noqa: BLE001 - se clasifica abajo
            self._classify_failure(media_id, exc)
            return

        relative = self._store(payload, media_id, message_id, media_type, mime_type, file_name)
        self._finish(media_id, relative, size=len(payload))

    # -- Almacenamiento ------------------------------------------------------

    def _store(
        self,
        payload: bytes,
        media_id: int,
        message_id: int,
        media_type: str,
        mime_type: str | None,
        file_name: str | None,
    ) -> str:
        """Guarda el archivo y devuelve su ruta RELATIVA a MEDIA_DIR.

        Se guarda relativa a proposito: mover la carpeta de medios no debe
        invalidar la base de datos.
        """
        subdir = self._settings.media_dir / _SUBDIRS.get(media_type, "other")
        subdir.mkdir(parents=True, exist_ok=True)

        extension = extension_for(mime_type, media_type, file_name)
        if media_type == "document" and file_name:
            # En documentos se conserva el nombre original, que es informacion
            # del usuario, prefijado con el id para evitar colisiones.
            base = safe_filename(Path(file_name).stem, fallback=f"doc{message_id}")
            name = f"{message_id}_{base}{extension}"
        else:
            name = f"{message_id}_{media_id}{extension}"

        path = subdir / name
        path.write_bytes(payload)
        return str(path.relative_to(self._settings.media_dir)).replace("\\", "/")

    def _existing_path(self, file_sha256: bytes, exclude_id: int) -> str | None:
        with self._database.transaction() as session:
            return session.execute(
                select(MediaFile.local_path).where(
                    MediaFile.file_sha256 == file_sha256,
                    MediaFile.download_status == "downloaded",
                    MediaFile.local_path.is_not(None),
                    MediaFile.id != exclude_id,
                )
            ).scalars().first()

    # -- Resultado -----------------------------------------------------------

    def _finish(
        self, media_id: int, relative: str, *, size: int | None = None, dedup: bool = False
    ) -> None:
        values: dict[str, Any] = {
            "download_status": "downloaded",
            "local_path": relative,
            "last_error": None,
        }
        if size is not None:
            values["file_size"] = size
        with self._database.transaction() as session:
            session.execute(update(MediaFile).where(MediaFile.id == media_id).values(**values))
        if dedup:
            self._stats.deduplicated += 1
            log.debug("Adjunto %d reutiliza un archivo ya descargado", media_id)
        else:
            self._stats.downloaded += 1

    def _fail(self, media_id: int, status: str, message: str) -> None:
        with self._database.transaction() as session:
            session.execute(
                update(MediaFile)
                .where(MediaFile.id == media_id)
                .values(
                    download_status=status,
                    last_error=message[:500],
                    download_attempts=MediaFile.download_attempts + 1,
                )
            )
        if status == "unavailable":
            self._stats.unavailable += 1
        elif status == "expired":
            self._stats.expired += 1
        else:
            self._stats.failed += 1

    def _classify_failure(self, media_id: int, exc: Exception) -> None:
        """Distingue "el CDN ya no lo tiene" de "fallo la descarga".

        Un adjunto antiguo puede haber caducado en el CDN aunque el mensaje se
        conserve perfectamente. Son estados distintos y no deben confundirse.
        """
        message = str(exc) or type(exc).__name__
        lowered = message.lower()

        # Estados TERMINALES distintos, como pide la seccion 30:
        #   410 Gone      -> expired      (el CDN lo tuvo y ya no)
        #   404 Not Found -> unavailable  (el CDN no lo sirve)
        # El detalle individual va a DEBUG: con cientos de adjuntos antiguos
        # llenaba la consola sin aportar nada.
        if "410" in lowered or "gone" in lowered:
            log.debug("Adjunto %d caducado en el CDN (%s)", media_id, message[:80])
            self._fail(media_id, "expired", message)
        elif "404" in lowered or "not found" in lowered:
            log.debug("Adjunto %d no disponible en el CDN (%s)", media_id, message[:80])
            self._fail(media_id, "unavailable", message)
        else:
            log.debug("Adjunto %d fallo: %s", media_id, message[:120])
            self._fail(media_id, "failed", message)
