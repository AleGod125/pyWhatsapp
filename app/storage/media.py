"""Multimedia: cache local, servido por rangos y desalojo.

LA COPIA LOCAL ES CACHE, NO EL ORIGINAL
---------------------------------------
El original vive en Drive. ``data/media/`` es una copia rapida que se puede
tirar. Por eso se desaloja por tamano y por edad: dar por infinito el disco
del usuario es como se llena un disco.

NUNCA SE BORRA ANTES DE COMPROBAR
---------------------------------
Un archivo local solo se puede desalojar si el remoto existe, tiene
identificador guardado y su tamano cuadra. Borrar por haber recibido un 200 es
como se pierde contenido sin que nadie se entere hasta que hace falta.

RANGOS SOBRE CONTENIDO CIFRADO
------------------------------
AES-GCM no se puede descifrar por la mitad, asi que la multimedia se cifra en
trozos independientes. Un rango de bytes se traduce a un rango de trozos y
solo se descargan esos: servir diez segundos de un video no baja los 2 GB.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import select

from app.core.logging_setup import get_logger
from app.models import MediaFile
from app.storage.encryption import (
    CHUNK_STORED_BYTES,
    Cabecera,
    StorageEncryption,
    huellas_iguales,
    rango_a_trozos,
    sha256_de_archivo,
)

log = get_logger("STORAGE")

#: Cuanto se lee de la cabecera para saber como esta guardado el archivo. La
#: cabecera es pequena; 512 bytes sobran y es una sola peticion.
CABECERA_MAX = 512


@dataclass(frozen=True)
class TrozoServido:
    """Lo que se devuelve para un ``Range``."""

    datos: bytes
    inicio: int
    fin: int
    total: int

    @property
    def content_range(self) -> str:
        return f"bytes {self.inicio}-{self.fin}/{self.total}"


class MediaStorage:
    """Lee multimedia, venga de la cache o de Drive."""

    def __init__(self, database: Any, settings: Any, storage_service: Any) -> None:
        self._database = database
        self._settings = settings
        self._storage = storage_service

    # -- Lectura -------------------------------------------------------------

    def ruta_local(self, fila: MediaFile) -> Path | None:
        """La copia en cache, si sigue estando."""
        if not fila.local_path:
            return None
        ruta = Path(fila.local_path)
        if not ruta.is_absolute():
            ruta = Path(self._settings.media_dir) / ruta
        return ruta if ruta.exists() else None

    def leer_rango(
        self,
        fila: MediaFile,
        *,
        inicio: int,
        fin: int,
        almacenamiento: Any,
        user_id: uuid.UUID,
    ) -> TrozoServido:
        """Un rango de bytes en claro, mire donde mire.

        Si hay copia local se sirve de ahi —es lo mas rapido y no gasta cupo
        de Google—; si no, se traduce el rango a trozos y se piden solo esos.
        """
        total = fila.file_size or 0
        local = self.ruta_local(fila)
        if local is not None:
            total = local.stat().st_size
            fin = min(fin, total - 1)
            with local.open("rb") as f:
                f.seek(inicio)
                datos = f.read(fin - inicio + 1)
            self._tocar(fila.id)
            return TrozoServido(datos=datos, inicio=inicio, fin=fin, total=total)

        if not fila.drive_file_id:
            raise FileNotFoundError("el archivo no esta ni en cache ni en Drive")

        return self._rango_desde_drive(
            fila, inicio=inicio, fin=fin, almacenamiento=almacenamiento, user_id=user_id
        )

    def _rango_desde_drive(
        self,
        fila: MediaFile,
        *,
        inicio: int,
        fin: int,
        almacenamiento: Any,
        user_id: uuid.UUID,
    ) -> TrozoServido:
        cabecera_crudo = almacenamiento.read_range(fila.drive_file_id, 0, CABECERA_MAX - 1)
        cabecera, largo = Cabecera.leer(cabecera_crudo)

        total = cabecera.plaintext_size or fila.file_size or 0
        if total <= 0:
            raise FileNotFoundError("no se conoce el tamano del archivo")
        fin = min(fin, total - 1)

        if not cabecera.chunked:
            # Archivo pequeno guardado de una pieza: se descarga entero.
            crudo = almacenamiento.read_file(fila.drive_file_id)
            claro = self._descifrar_entero(crudo, user_id)
            return TrozoServido(
                datos=claro[inicio : fin + 1], inicio=inicio, fin=fin, total=total
            )

        rango = rango_a_trozos(
            inicio, fin, cabecera_bytes=largo, plaintext_size=total
        )
        crudo = almacenamiento.read_range(
            fila.drive_file_id, rango.byte_inicial, rango.byte_final
        )

        cifrado = self._storage.cifrado_de(user_id)
        piezas: list[bytes] = []
        for posicion, indice in enumerate(
            range(rango.primer_trozo, rango.ultimo_trozo + 1)
        ):
            desde = posicion * CHUNK_STORED_BYTES
            trozo = crudo[desde : desde + CHUNK_STORED_BYTES]
            if not trozo:
                break
            piezas.append(
                cifrado.decrypt_chunk(trozo, indice, cabecera_crudo[:largo])
                if cifrado is not None
                else trozo
            )

        completo = b"".join(piezas)
        datos = completo[rango.recorte_inicial : rango.recorte_inicial + rango.longitud]
        return TrozoServido(datos=datos, inicio=inicio, fin=inicio + len(datos) - 1, total=total)

    def _descifrar_entero(self, crudo: bytes, user_id: uuid.UUID) -> bytes:
        cifrado = self._storage.cifrado_de(user_id)
        if cifrado is None:
            _, largo = Cabecera.leer(crudo)
            return crudo[largo:]
        claro, _ = cifrado.decrypt_bytes(crudo)
        return claro

    def _tocar(self, media_id: int) -> None:
        """Marca el uso, para que el desalojo LRU sepa que sigue haciendo falta."""
        with self._database.transaction() as sesion:
            fila = sesion.get(MediaFile, media_id)
            if fila is not None:
                fila.last_accessed_at = _ahora()
                sesion.flush()

    # -- Desalojo ------------------------------------------------------------

    def puede_desalojar(self, fila: MediaFile) -> tuple[bool, str]:
        """Si es seguro borrar la copia local. ``(si, motivo_si_no)``.

        Las tres condiciones son obligatorias. Con dos de tres, un archivo
        truncado en Drive se convertiria en contenido perdido.
        """
        if fila.storage_status != "ready":
            return False, "todavia no esta subido"
        if not fila.drive_file_id:
            return False, "no hay identificador en Drive"
        if not fila.stored_bytes:
            return False, "no se comprobo el tamano subido"
        return True, ""

    def desalojar(self, *, limite_bytes: int, ttl_horas: int) -> dict[str, int]:
        """Libera cache: primero lo caducado, luego lo mas antiguo sin usar.

        Solo toca archivos que estan a salvo en Drive. Los que no lo esten se
        quedan aunque la cache se pase de tamano: es preferible ocupar disco a
        perder contenido.
        """
        borrados = 0
        liberados = 0
        protegidos = 0

        with self._database.transaction() as sesion:
            filas = (
                sesion.execute(
                    select(MediaFile)
                    .where(MediaFile.local_path.is_not(None))
                    .order_by(
                        MediaFile.last_accessed_at.asc().nulls_first(),
                        MediaFile.id.asc(),
                    )
                )
                .scalars()
                .all()
            )
            candidatos = []
            total = 0
            for fila in filas:
                ruta = self.ruta_local(fila)
                if ruta is None:
                    continue
                tamano = ruta.stat().st_size
                total += tamano
                seguro, _ = self.puede_desalojar(fila)
                if not seguro:
                    protegidos += 1
                    continue
                candidatos.append((fila, ruta, tamano))

            caducado = _ahora() - timedelta(hours=max(1, ttl_horas))
            for fila, ruta, tamano in candidatos:
                usado = fila.last_accessed_at or fila.uploaded_at
                vencido = usado is None or _con_zona(usado) < caducado
                sobra = total - liberados > limite_bytes
                if not (vencido or sobra):
                    continue
                try:
                    ruta.unlink()
                except OSError:
                    continue
                fila.local_path = None
                borrados += 1
                liberados += tamano
            sesion.flush()

        if borrados:
            log.info(
                "Cache local: %d archivo(s) liberados (%.1f MB). Siguen en Drive.",
                borrados,
                liberados / (1024 * 1024),
            )
        if protegidos:
            log.debug(
                "%d archivo(s) NO se desalojan: todavia no estan a salvo en Drive",
                protegidos,
            )
        return {
            "removed": borrados,
            "freed_bytes": liberados,
            "protected": protegidos,
        }

    # -- Comprobacion --------------------------------------------------------

    def verificar(self, fila: MediaFile) -> tuple[bool, str | None]:
        """Contrasta la copia local con la huella guardada."""
        ruta = self.ruta_local(fila)
        if ruta is None:
            return False, "no hay copia local"
        if not fila.plaintext_sha256:
            return True, None
        if huellas_iguales(sha256_de_archivo(ruta), fila.plaintext_sha256):
            return True, None
        return False, "la copia local no coincide con su huella"


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _con_zona(momento: datetime) -> datetime:
    return momento if momento.tzinfo else momento.replace(tzinfo=timezone.utc)


def trocear(datos: bytes, tamano: int = 64 * 1024) -> Iterator[bytes]:
    for i in range(0, len(datos), tamano):
        yield datos[i : i + tamano]
