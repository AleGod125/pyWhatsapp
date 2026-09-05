"""El contrato del almacenamiento. Sin Google dentro.

POR QUE EXISTE
--------------
Drive es la implementacion de hoy, no la definicion del problema. Si el
pipeline de sincronizacion llamara a la API de Google directamente, cambiar a
S3, a OneDrive o a un disco de red obligaria a tocar la ingesta, el backfill y
la multimedia. Con esta frontera, lo que cambia es una clase.

Tambien sirve para probar: los tests usan una implementacion en memoria y
ejercitan el pipeline entero sin red ni credenciales.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import BinaryIO, Iterator, Protocol


class StorageError(RuntimeError):
    """Fallo del almacenamiento, con un codigo que el llamador entiende."""

    def __init__(self, code: str, message: str, *, reintentable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        #: Si vale la pena volver a intentarlo mas tarde.
        self.reintentable = reintentable


class StorageAuthError(StorageError):
    """El acceso ya no vale. Hay que reconectar, no reintentar en bucle."""

    def __init__(self, message: str = "El acceso a Google Drive ya no es valido.") -> None:
        super().__init__("STORAGE_UNAUTHORIZED", message, reintentable=False)


class StorageQuotaError(StorageError):
    """Sin espacio, o demasiadas peticiones.

    Se distingue del resto porque NO es un fallo de la copia de WhatsApp: la
    sincronizacion sigue bien y lo que falta es sitio o cupo. Mezclarlos haria
    que el usuario buscara el problema donde no esta.
    """

    def __init__(self, code: str, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(code, message, reintentable=True)
        self.retry_after = retry_after


@dataclass(frozen=True)
class ArchivoSubido:
    """Lo que devuelve una subida, y con lo que se comprueba."""

    file_id: str
    size: int
    sha256: str | None = None


@dataclass
class RutaDeAlmacenamiento:
    """Donde va una cosa dentro de la copia del usuario."""

    account_id: str
    chat_id: str | None = None

    def carpeta_de_cuenta(self) -> str:
        return f"accounts/{self.account_id}"

    def carpeta_de_chat(self) -> str:
        if not self.chat_id:
            raise ValueError("hace falta chat_id")
        return f"{self.carpeta_de_cuenta()}/chats/{self.chat_id}"

    def carpeta_de_mensajes(self) -> str:
        return f"{self.carpeta_de_chat()}/messages"

    def carpeta_de_multimedia(self) -> str:
        return f"{self.carpeta_de_chat()}/media"


@dataclass
class PropiedadesDeArchivo:
    """Metadatos que viajan CON el archivo.

    Drive los guarda como ``appProperties``. Existen para que la copia se
    pueda reconstruir sin PostgreSQL: sin ellos, un directorio lleno de
    archivos cifrados no dice a que conversacion pertenece cada uno.

    NUNCA llevan contenido, ni telefonos, ni nada sensible.
    """

    entity: str
    account_id: str | None = None
    chat_id: str | None = None
    segment_id: str | None = None
    sequence: int | None = None
    media_id: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, str]:
        cuerpo = {"app": "whatsapp_backup", "entity": self.entity}
        for clave, valor in (
            ("account_id", self.account_id),
            ("chat_id", self.chat_id),
            ("segment_id", self.segment_id),
            ("sequence", self.sequence),
            ("media_id", self.media_id),
        ):
            if valor is not None:
                cuerpo[clave] = str(valor)
        cuerpo.update({k: str(v) for k, v in self.extra.items()})
        return cuerpo


class BackupStorage(Protocol):
    """Lo que el pipeline necesita de un almacenamiento. Nada mas."""

    # -- Preparacion --------------------------------------------------------

    def ensure_user_storage(self) -> str:
        """Crea la carpeta raiz si falta. Devuelve su identificador.

        Idempotente: llamarla dos veces no crea dos carpetas.
        """

    def ensure_account_storage(self, account_id: str) -> str: ...

    def ensure_chat_storage(self, account_id: str, chat_id: str) -> str: ...

    # -- Escritura ----------------------------------------------------------

    def store_bytes(
        self,
        *,
        carpeta: str,
        nombre: str,
        datos: bytes,
        propiedades: PropiedadesDeArchivo,
        mime_type: str = "application/octet-stream",
    ) -> ArchivoSubido:
        """Sube un archivo pequeno de una vez."""

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
        """Sube por partes, sin cargar el archivo en memoria.

        Es lo que permite subir un video de varios GB: leerlo entero para
        mandarlo reservaria esos GB de RAM.
        """

    # -- Lectura ------------------------------------------------------------

    def read_file(self, file_id: str) -> bytes: ...

    def read_range(self, file_id: str, inicio: int, fin: int) -> bytes:
        """Descarga solo esos bytes.

        Sin esto, servir diez segundos de un video obligaria a bajarlo entero.
        """

    def open_stream(self, file_id: str) -> BinaryIO: ...

    # -- Otros --------------------------------------------------------------

    def exists(self, file_id: str) -> bool: ...

    def delete(self, file_id: str) -> bool: ...

    def health_check(self) -> dict: ...
