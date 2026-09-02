"""Miniaturas cacheadas en disco.

POR QUE EN DISCO Y NO SOLO EN MEMORIA (seccion 37)
--------------------------------------------------
Decodificar y reescalar una foto de 4 MB cuesta decenas de milisegundos. Con
200 burbujas y el usuario yendo y viniendo entre chats, eso se nota. Una cache
en memoria ayuda dentro de la sesion, pero al reabrir la aplicacion todo el
trabajo se repite.

Aqui se guarda el resultado en ``<MEDIA_DIR>/cache/thumbs/<sha1>-<w>x<h>.jpg``.
La clave incluye ruta, tamano y fecha de modificacion del original: si el
archivo cambia, la miniatura se regenera sola en vez de quedarse obsoleta.

NUNCA se carga el original entero para pintar una burbuja (seccion 38).
``Image.draft()`` hace que JPEG se decodifique ya reducido, asi que un archivo
de 20 MB no pasa por memoria a tamano completo.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.logging_setup import get_logger

log = get_logger("MEDIA")

CACHE_SUBDIR = Path("cache") / "thumbs"

# Calidad del JPEG cacheado. 82 es indistinguible a este tamano y ocupa poco.
JPEG_QUALITY = 82


def cache_dir(media_root: Path) -> Path:
    return Path(media_root) / CACHE_SUBDIR


def cache_key(source: Path, box: tuple[int, int]) -> str:
    """Clave estable que incluye tamano y fecha del original.

    Sin la fecha de modificacion, sustituir un archivo por otro con el mismo
    nombre dejaria para siempre la miniatura antigua.
    """
    try:
        stat = source.stat()
        firma = f"{source.resolve()}|{stat.st_size}|{int(stat.st_mtime)}"
    except OSError:
        firma = str(source)
    digest = hashlib.sha1(firma.encode("utf-8", "replace")).hexdigest()
    return f"{digest}-{box[0]}x{box[1]}.jpg"


def thumbnail_path(media_root: Path, source: Path, box: tuple[int, int]) -> Path:
    return cache_dir(media_root) / cache_key(source, box)


def ensure_thumbnail(
    media_root: Path, source: Path, box: tuple[int, int]
) -> Path | None:
    """Devuelve la miniatura en disco, generandola solo la primera vez.

    ``None`` si el archivo no se puede leer como imagen. Un archivo corrupto
    no es motivo para romper la conversacion: la burbuja caera a su tarjeta.
    """
    if not source.exists():
        return None

    destino = thumbnail_path(media_root, source, box)
    if destino.exists() and destino.stat().st_size > 0:
        return destino

    try:
        from PIL import Image

        destino.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as imagen:
            # draft() reduce durante la decodificacion, no despues: un JPEG
            # grande nunca llega a memoria a tamano completo.
            try:
                imagen.draft("RGB", box)
            except (AttributeError, ValueError):
                pass
            imagen = imagen.convert("RGB")
            imagen.thumbnail(box)
            # Escritura atomica: si el proceso muere a medias no queda una
            # miniatura truncada que la cache daria por buena para siempre.
            temporal = destino.with_suffix(".tmp")
            imagen.save(temporal, "JPEG", quality=JPEG_QUALITY, optimize=True)
            temporal.replace(destino)
        return destino
    except Exception:  # noqa: BLE001 - una imagen ilegible no rompe el chat
        log.debug("No se pudo generar la miniatura de %s", source.name)
        return None


def clear_cache(media_root: Path) -> int:
    """Vacia la cache de miniaturas. Devuelve cuantos archivos borro.

    Solo borra DERIVADOS: los originales no se tocan. No se llama sola en
    ningun sitio; existe para poder regenerar las miniaturas a mano si
    hiciera falta.
    """
    carpeta = cache_dir(media_root)
    if not carpeta.is_dir():
        return 0
    borrados = 0
    for archivo in carpeta.glob("*.jpg"):
        try:
            archivo.unlink()
            borrados += 1
        except OSError:
            continue
    return borrados
