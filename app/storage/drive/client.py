"""Llamadas HTTP a la API de Google Drive. Sin dependencias extra.

POR QUE A MANO
--------------
Hacen falta seis operaciones: crear carpeta, buscar, subir de una vez, subir
por partes, descargar (entero o un rango) y borrar.
``google-api-python-client`` arrastra media docena de paquetes para eso.

QUE APORTA ESTE MODULO
----------------------
Traduce los errores de Google a algo que el trabajador entiende: si vale la
pena reintentar, si hay que reconectar, o si es falta de espacio. Sin esa
traduccion, un 401 y un 503 acabarian tratados igual y uno de los dos se
gestionaria mal.

NUNCA registra tokens, ni contenido, ni nombres de archivo.
"""

from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Iterator

from app.core.logging_setup import get_logger
from app.storage.interface import StorageAuthError, StorageError, StorageQuotaError

log = get_logger("DRIVE")

API = "https://www.googleapis.com/drive/v3"
SUBIDA = "https://www.googleapis.com/upload/drive/v3/files"

CARPETA_MIME = "application/vnd.google-apps.folder"

#: A partir de aqui se sube por partes en vez de de una vez.
LIMITE_SIMPLE = 5 * 1024 * 1024

#: Trozo de subida reanudable. Google exige multiplo de 256 KiB.
TROZO_SUBIDA = 8 * 1024 * 1024

TIEMPO_LIMITE = 120


class DriveClient:
    """Cliente de UN usuario. Nunca compartido entre cuentas."""

    def __init__(self, access_token: str) -> None:
        if not access_token:
            raise StorageAuthError("No hay acceso a Google Drive.")
        self._token = access_token

    # -- Carpetas -----------------------------------------------------------

    def buscar(self, nombre: str, *, padre: str | None = None) -> str | None:
        """El identificador de una carpeta por nombre, o ``None``.

        Solo se usa al preparar la copia. Despues se trabaja siempre con
        identificadores guardados: buscar por nombre en cada peticion es lento
        y ademas ambiguo, porque el usuario puede renombrar o duplicar.
        """
        seguro = nombre.replace("'", "\\'")
        consulta = [
            f"name = '{seguro}'",
            f"mimeType = '{CARPETA_MIME}'",
            "trashed = false",
        ]
        if padre:
            consulta.append(f"'{padre}' in parents")
        parametros = urllib.parse.urlencode(
            {
                "q": " and ".join(consulta),
                "fields": "files(id,name)",
                "pageSize": 10,
                "spaces": "drive",
            }
        )
        datos = self._peticion("GET", f"{API}/files?{parametros}")
        archivos = datos.get("files") or []
        return archivos[0]["id"] if archivos else None

    def crear_carpeta(self, nombre: str, *, padre: str | None = None) -> str:
        cuerpo: dict[str, Any] = {"name": nombre, "mimeType": CARPETA_MIME}
        if padre:
            cuerpo["parents"] = [padre]
        datos = self._peticion(
            "POST",
            f"{API}/files?fields=id",
            cuerpo=json.dumps(cuerpo).encode(),
            content_type="application/json",
        )
        return datos["id"]

    def asegurar_carpeta(self, nombre: str, *, padre: str | None = None) -> str:
        """Busca y, si no esta, crea. Idempotente."""
        existente = self.buscar(nombre, padre=padre)
        return existente or self.crear_carpeta(nombre, padre=padre)

    # -- Subida -------------------------------------------------------------

    def subir_simple(
        self,
        *,
        nombre: str,
        padre: str,
        datos: bytes,
        propiedades: dict[str, str],
        mime_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """Un solo request. Para archivos pequenos."""
        metadatos = {
            "name": nombre,
            "parents": [padre],
            "appProperties": propiedades,
        }
        frontera = f"==={uuid.uuid4().hex}==="
        cuerpo = b"".join(
            (
                f"--{frontera}\r\n".encode(),
                b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
                json.dumps(metadatos).encode(),
                f"\r\n--{frontera}\r\n".encode(),
                f"Content-Type: {mime_type}\r\n\r\n".encode(),
                datos,
                f"\r\n--{frontera}--\r\n".encode(),
            )
        )
        return self._peticion(
            "POST",
            f"{SUBIDA}?uploadType=multipart&fields=id,size",
            cuerpo=cuerpo,
            content_type=f"multipart/related; boundary={frontera}",
        )

    def iniciar_reanudable(
        self,
        *,
        nombre: str,
        padre: str,
        propiedades: dict[str, str],
        tamano: int,
        mime_type: str = "application/octet-stream",
    ) -> str:
        """Abre una subida por partes y devuelve su URL de sesion.

        La URL se puede guardar: si la conexion se corta a la mitad, se
        pregunta cuanto llego y se sigue desde ahi en vez de empezar de cero.
        """
        metadatos = {"name": nombre, "parents": [padre], "appProperties": propiedades}
        peticion = urllib.request.Request(
            f"{SUBIDA}?uploadType=resumable&fields=id,size",
            data=json.dumps(metadatos).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(tamano),
            },
        )
        try:
            with urllib.request.urlopen(peticion, timeout=TIEMPO_LIMITE) as respuesta:
                destino = respuesta.headers.get("Location")
        except urllib.error.HTTPError as exc:
            raise self._traducir(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise StorageError(
                "DRIVE_UNREACHABLE",
                "No se pudo contactar con Google Drive.",
                reintentable=True,
            ) from exc
        if not destino:
            raise StorageError(
                "DRIVE_NO_SESSION", "Google no devolvio sesion de subida."
            )
        return destino

    def subir_por_partes(
        self, url_sesion: str, origen: Iterator[bytes], tamano: int
    ) -> dict[str, Any]:
        """Envia el contenido en trozos. No lo carga entero en memoria."""
        enviado = 0
        buffer = bytearray()
        ultima: dict[str, Any] = {}

        def mandar(trozo: bytes, desde: int, final: bool) -> dict[str, Any]:
            hasta = desde + len(trozo) - 1
            total = str(tamano) if final else "*"
            peticion = urllib.request.Request(
                url_sesion,
                data=trozo,
                method="PUT",
                headers={
                    "Content-Length": str(len(trozo)),
                    "Content-Range": f"bytes {desde}-{hasta}/{total}",
                },
            )
            try:
                with urllib.request.urlopen(peticion, timeout=TIEMPO_LIMITE) as r:
                    crudo = r.read().decode("utf-8")
                return json.loads(crudo) if crudo else {}
            except urllib.error.HTTPError as exc:
                # 308 significa "voy bien, sigue": no es un error.
                if exc.code == 308:
                    return {}
                raise self._traducir(exc) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise StorageError(
                    "DRIVE_UNREACHABLE",
                    "Se corto la subida a Google Drive.",
                    reintentable=True,
                ) from exc

        for parte in origen:
            buffer.extend(parte)
            while len(buffer) >= TROZO_SUBIDA:
                trozo = bytes(buffer[:TROZO_SUBIDA])
                del buffer[:TROZO_SUBIDA]
                ultima = mandar(trozo, enviado, False) or ultima
                enviado += len(trozo)

        # El ultimo trozo lleva el tamano total y cierra la subida.
        ultima = mandar(bytes(buffer), enviado, True) or ultima
        return ultima

    # -- Descarga -----------------------------------------------------------

    def descargar(self, file_id: str) -> bytes:
        return self._peticion("GET", f"{API}/files/{file_id}?alt=media", crudo=True)

    def descargar_rango(self, file_id: str, inicio: int, fin: int) -> bytes:
        """Solo esos bytes. Es lo que hace posible servir un video sin bajarlo."""
        return self._peticion(
            "GET",
            f"{API}/files/{file_id}?alt=media",
            crudo=True,
            cabeceras={"Range": f"bytes={inicio}-{fin}"},
        )

    def metadatos(self, file_id: str) -> dict[str, Any]:
        return self._peticion(
            "GET",
            f"{API}/files/{file_id}?fields=id,name,size,appProperties,trashed",
        )

    def existe(self, file_id: str) -> bool:
        try:
            return not self.metadatos(file_id).get("trashed", False)
        except StorageError:
            return False

    def borrar(self, file_id: str) -> bool:
        try:
            self._peticion("DELETE", f"{API}/files/{file_id}", crudo=True)
            return True
        except StorageError as exc:
            log.info("No se pudo borrar el archivo en Drive: %s", exc.code)
            return False

    def about(self) -> dict[str, Any]:
        return self._peticion(
            "GET",
            f"{API}/about?fields=user(displayName,emailAddress),"
            "storageQuota(limit,usage)",
        )

    # -- HTTP ---------------------------------------------------------------

    def _peticion(
        self,
        metodo: str,
        url: str,
        *,
        cuerpo: bytes | None = None,
        content_type: str | None = None,
        crudo: bool = False,
        cabeceras: dict[str, str] | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self._token}"}
        if content_type:
            headers["Content-Type"] = content_type
        headers.update(cabeceras or {})

        peticion = urllib.request.Request(
            url, data=cuerpo, method=metodo, headers=headers
        )
        try:
            with urllib.request.urlopen(peticion, timeout=TIEMPO_LIMITE) as respuesta:
                datos = respuesta.read()
            if crudo:
                return datos
            return json.loads(datos.decode("utf-8")) if datos else {}
        except urllib.error.HTTPError as exc:
            raise self._traducir(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise StorageError(
                "DRIVE_UNREACHABLE",
                "No se pudo contactar con Google Drive.",
                reintentable=True,
            ) from exc
        except json.JSONDecodeError as exc:
            raise StorageError(
                "DRIVE_BAD_RESPONSE", "Google devolvio una respuesta ilegible."
            ) from exc

    @staticmethod
    def _traducir(exc: urllib.error.HTTPError) -> StorageError:
        """Convierte el error de Google en algo accionable.

        Importa la distincion: un 401 no se arregla reintentando —hay que
        reconectar— y un 429 si, esperando. Tratarlos igual significa o
        machacar a Google o pedirle al usuario que reconecte sin motivo.
        """
        motivo = ""
        try:
            detalle = json.loads(exc.read().decode("utf-8")).get("error", {})
            errores = detalle.get("errors") or [{}]
            motivo = errores[0].get("reason", "") or detalle.get("status", "")
        except Exception:  # noqa: BLE001 - el detalle es un extra
            pass

        # Nunca se registra el cuerpo entero: puede llevar nombres de archivo.
        log.info("Drive respondio %s (%s)", exc.code, motivo or "sin detalle")

        if exc.code == 401:
            return StorageAuthError()
        if motivo in ("storageQuotaExceeded", "quotaExceeded"):
            return StorageQuotaError(
                "DRIVE_QUOTA_EXCEEDED",
                "Tu Google Drive no tiene espacio libre. Libera espacio o "
                "amplia tu plan.",
            )
        if exc.code == 429 or motivo in ("rateLimitExceeded", "userRateLimitExceeded"):
            espera = exc.headers.get("Retry-After") if exc.headers else None
            return StorageQuotaError(
                "DRIVE_RATE_LIMITED",
                "Google esta limitando las peticiones. Se reintentara solo.",
                retry_after=float(espera) if espera and str(espera).isdigit() else None,
            )
        if exc.code == 403 and motivo in ("forbidden", "insufficientFilePermissions"):
            return StorageAuthError(
                "Google Drive rechazo la operacion. Vuelve a conectar tu cuenta."
            )
        if exc.code == 404:
            return StorageError("DRIVE_NOT_FOUND", "Ese archivo ya no esta en Drive.")
        if exc.code >= 500:
            return StorageError(
                "DRIVE_SERVER_ERROR",
                "Google Drive tuvo un problema temporal.",
                reintentable=True,
            )
        return StorageError("DRIVE_ERROR", f"Google Drive respondio {exc.code}.")


def adivinar_mime(nombre: str, por_defecto: str = "application/octet-stream") -> str:
    return mimetypes.guess_type(nombre)[0] or por_defecto
