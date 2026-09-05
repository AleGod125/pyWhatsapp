"""``BackupStorage`` sobre Google Drive.

Implementa el contrato de :mod:`app.storage.interface` para UN usuario. Las
carpetas creadas se recuerdan en PostgreSQL: preguntarle a Drive por la
carpeta de cada chat antes de cada subida seria una llamada de red para
averiguar algo que no cambia.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, BinaryIO, Iterator

from sqlalchemy import select

from app.core.logging_setup import get_logger
from app.models.storage import DriveFolder, GoogleDriveStorage
from app.storage.drive.client import LIMITE_SIMPLE, DriveClient
from app.storage.interface import (
    ArchivoSubido,
    PropiedadesDeArchivo,
    StorageError,
)

log = get_logger("STORAGE")

#: Nombre de la carpeta raiz dentro del Drive del usuario. Es lo unico que el
#: usuario ve en su Drive, asi que se escribe para una persona.
CARPETA_RAIZ = "WhatsApp Backup"


class DriveBackupStorage:
    """Almacenamiento de un usuario. Nunca de varios."""

    def __init__(
        self,
        *,
        user_id: uuid.UUID,
        client: DriveClient,
        database: Any,
    ) -> None:
        self._user_id = user_id
        self._client = client
        self._database = database

    # -- Preparacion ---------------------------------------------------------

    def ensure_user_storage(self) -> str:
        """Carpeta raiz + fila en PostgreSQL. Idempotente.

        Si ya hay identificador guardado no se pregunta a Drive: es el caso
        normal y hacerlo costaria una llamada de red por peticion.
        """
        with self._database.transaction() as sesion:
            fila = sesion.execute(
                select(GoogleDriveStorage).where(
                    GoogleDriveStorage.user_id == self._user_id
                )
            ).scalar_one_or_none()
            if fila is not None and fila.root_folder_id:
                return fila.root_folder_id

        # Con ``drive.file`` solo vemos lo que creamos nosotros, asi que
        # buscar por nombre no puede tropezar con carpetas ajenas.
        raiz = self._client.asegurar_carpeta(CARPETA_RAIZ)

        with self._database.transaction() as sesion:
            fila = sesion.execute(
                select(GoogleDriveStorage).where(
                    GoogleDriveStorage.user_id == self._user_id
                )
            ).scalar_one_or_none()
            if fila is None:
                fila = GoogleDriveStorage(user_id=self._user_id, root_folder_id=raiz)
                sesion.add(fila)
            else:
                fila.root_folder_id = raiz
            fila.updated_at = _ahora()
            fila.last_verified_at = _ahora()
            sesion.flush()

        log.info("Carpeta raiz de la copia lista en Drive")
        return raiz

    def ensure_account_storage(self, account_id: str) -> str:
        return self._asegurar_ruta(f"accounts/{account_id}")

    def ensure_chat_storage(self, account_id: str, chat_id: str) -> str:
        return self._asegurar_ruta(f"accounts/{account_id}/chats/{chat_id}")

    def carpeta_de_mensajes(self, account_id: str, chat_id: str) -> str:
        return self._asegurar_ruta(
            f"accounts/{account_id}/chats/{chat_id}/messages"
        )

    def carpeta_de_multimedia(self, account_id: str, chat_id: str) -> str:
        return self._asegurar_ruta(f"accounts/{account_id}/chats/{chat_id}/media")

    def _asegurar_ruta(self, ruta: str) -> str:
        """Crea la cadena de carpetas que haga falta, recordando cada nivel."""
        cacheado = self._buscar_en_cache(ruta)
        if cacheado:
            return cacheado

        padre = self.ensure_user_storage()
        acumulada = ""
        for parte in ruta.split("/"):
            acumulada = f"{acumulada}/{parte}" if acumulada else parte
            existente = self._buscar_en_cache(acumulada)
            if existente:
                padre = existente
                continue
            padre = self._client.asegurar_carpeta(parte, padre=padre)
            self._recordar(acumulada, padre)
        return padre

    def _buscar_en_cache(self, ruta: str) -> str | None:
        with self._database.transaction() as sesion:
            return sesion.execute(
                select(DriveFolder.folder_id).where(
                    DriveFolder.user_id == self._user_id, DriveFolder.path == ruta
                )
            ).scalar_one_or_none()

    def _recordar(self, ruta: str, folder_id: str) -> None:
        with self._database.transaction() as sesion:
            ya = sesion.execute(
                select(DriveFolder).where(
                    DriveFolder.user_id == self._user_id, DriveFolder.path == ruta
                )
            ).scalar_one_or_none()
            if ya is None:
                sesion.add(
                    DriveFolder(
                        user_id=self._user_id, path=ruta, folder_id=folder_id
                    )
                )
            else:
                ya.folder_id = folder_id
            sesion.flush()

    # -- Escritura -----------------------------------------------------------

    def store_bytes(
        self,
        *,
        carpeta: str,
        nombre: str,
        datos: bytes,
        propiedades: PropiedadesDeArchivo,
        mime_type: str = "application/octet-stream",
    ) -> ArchivoSubido:
        respuesta = self._client.subir_simple(
            nombre=nombre,
            padre=carpeta,
            datos=datos,
            propiedades=propiedades.to_dict(),
            mime_type=mime_type,
        )
        subido = self._comprobar(respuesta, len(datos), nombre)
        self._anotar_subida(subido.size)
        return subido

    def store_stream(
        self,
        *,
        carpeta: str,
        nombre: str,
        origen: Iterator[bytes],
        tamano: int,
        propiedades: PropiedadesDeArchivo,
        mime_type: str = "application/octet-stream",
    ) -> ArchivoSubido:
        """Subida reanudable. Para archivos grandes."""
        sesion_url = self._client.iniciar_reanudable(
            nombre=nombre,
            padre=carpeta,
            propiedades=propiedades.to_dict(),
            tamano=tamano,
            mime_type=mime_type,
        )
        respuesta = self._client.subir_por_partes(sesion_url, origen, tamano)
        subido = self._comprobar(respuesta, tamano, nombre)
        self._anotar_subida(subido.size)
        return subido

    @staticmethod
    def _comprobar(respuesta: dict, esperado: int, nombre: str) -> ArchivoSubido:
        """Que Drive responda 200 no basta.

        Se contrasta el tamano que dice haber guardado con el que se envio: un
        archivo truncado que se da por bueno es una copia que falla el dia que
        se necesita, y hasta entonces nadie lo sabe.
        """
        file_id = respuesta.get("id")
        if not file_id:
            raise StorageError(
                "DRIVE_NO_FILE_ID", "Drive no devolvio identificador de archivo."
            )
        declarado = respuesta.get("size")
        if declarado is not None and int(declarado) != esperado:
            raise StorageError(
                "DRIVE_SIZE_MISMATCH",
                f"El archivo subido mide {declarado} bytes y deberia medir {esperado}.",
                reintentable=True,
            )
        return ArchivoSubido(file_id=file_id, size=esperado)

    def _anotar_subida(self, bytes_subidos: int) -> None:
        with self._database.transaction() as sesion:
            fila = sesion.execute(
                select(GoogleDriveStorage).where(
                    GoogleDriveStorage.user_id == self._user_id
                )
            ).scalar_one_or_none()
            if fila is None:
                return
            fila.bytes_uploaded = (fila.bytes_uploaded or 0) + bytes_subidos
            fila.files_uploaded = (fila.files_uploaded or 0) + 1
            fila.last_upload_at = _ahora()
            fila.updated_at = _ahora()
            sesion.flush()

    # -- Lectura -------------------------------------------------------------

    def read_file(self, file_id: str) -> bytes:
        return self._client.descargar(file_id)

    def read_range(self, file_id: str, inicio: int, fin: int) -> bytes:
        return self._client.descargar_rango(file_id, inicio, fin)

    def open_stream(self, file_id: str) -> BinaryIO:
        import io

        return io.BytesIO(self._client.descargar(file_id))

    # -- Otros ---------------------------------------------------------------

    def exists(self, file_id: str) -> bool:
        return self._client.existe(file_id)

    def delete(self, file_id: str) -> bool:
        return self._client.borrar(file_id)

    def health_check(self) -> dict:
        return self._client.about()


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def limite_de_subida_simple() -> int:
    """Por encima de esto se sube por partes."""
    return LIMITE_SIMPLE
