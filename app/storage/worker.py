"""El que sube a Drive. En su propio hilo, nunca en el de recepcion.

POR QUE UN HILO APARTE
----------------------
Una subida puede tardar segundos, o minutos con un video. Hacerlo dentro del
callback que recibe de WhatsApp bloquearia la recepcion durante todo ese rato:
llegarian mensajes que nadie atiende, y el socket acabaria cayendose.

QUE GARANTIZA
-------------
* Lo pendiente sobrevive a un reinicio: la cola vive en PostgreSQL.
* Un reintento NO crea un archivo nuevo: si el segmento ya tiene
  ``drive_file_id``, se da por hecho.
* Un archivo local NUNCA se borra antes de comprobar que el remoto esta.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from app.core.logging_setup import get_logger
from app.models import MediaFile, Message
from app.models.storage import GoogleDriveStorage, MessageSegment, StorageJob
from app.storage import segments as seg
from app.storage.encryption import (
    sha256_de,
    sha256_de_archivo,
    tamano_cifrado,
)
from app.storage.interface import (
    PropiedadesDeArchivo,
    StorageAuthError,
    StorageError,
    StorageQuotaError,
)

log = get_logger("STORAGE")

#: Cada cuanto mira la cola cuando no hay nada. No hace falta mas: los
#: trabajos nuevos pueden esperar unos segundos.
INTERVALO_VACIO = 5.0
INTERVALO_TRABAJO = 0.2

#: Cada cuanto se mira si quedan mensajes sin agrupar. No hace falta mas: el
#: limite de edad del segmento ya obliga a cerrar los que se quedan cortos.
INTERVALO_BARRIDO = 30.0


class DriveStorageWorker:
    """Vacia la cola de subidas. Uno por proceso."""

    def __init__(
        self,
        *,
        database: Any,
        settings: Any,
        storage_service: Any,
        google_service: Any,
        publish: Any = None,
        runtime: Any = None,
    ) -> None:
        self._runtime = runtime
        self._database = database
        self._settings = settings
        self._storage = storage_service
        self._google = google_service
        self._publish = publish
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None

    # -- Ciclo de vida -------------------------------------------------------

    def start(self) -> None:
        if not self._storage.habilitado:
            log.info("Almacenamiento en Drive desactivado (DRIVE_STORAGE_ENABLED)")
            return
        if self._hilo is not None:
            return
        # Lo que quedo a medias cuando murio el proceso anterior.
        self._storage.jobs.recuperar_huerfanos()
        self._hilo = threading.Thread(
            target=self._bucle, name="drive-storage", daemon=True
        )
        self._hilo.start()
        log.info("Trabajador de almacenamiento en marcha")

    def stop(self, timeout: float = 10.0) -> None:
        self._parar.set()
        if self._hilo is not None:
            self._hilo.join(timeout=timeout)
            self._hilo = None

    def _bucle(self) -> None:
        ultimo_barrido = 0.0
        while not self._parar.is_set():
            try:
                # Antes de subir, mirar si hay mensajes sin agrupar todavia.
                # Cubre los tres origenes —bootstrap, ON_DEMAND y live— por
                # igual, y recoge lo que quedo atras.
                ahora = time.monotonic()
                if ahora - ultimo_barrido >= INTERVALO_BARRIDO:
                    ultimo_barrido = ahora
                    self._barrer_pendientes()
                hechos = self.procesar_lote()
            except Exception:  # noqa: BLE001 - el trabajador no puede morir
                log.exception("Fallo inesperado en el trabajador de almacenamiento")
                hechos = 0
            self._parar.wait(INTERVALO_TRABAJO if hechos else INTERVALO_VACIO)

    def _barrer_pendientes(self) -> None:
        """Agrupa en segmentos lo que todavia no ha salido hacia Drive."""
        dueno = getattr(self._runtime, "runtime_owner_user_id", None)
        cuenta = getattr(self._runtime, "runtime_owner_account_id", None)
        if dueno is None or cuenta is None:
            # Sin dueno no se sabe a quien atribuir lo que se suba, y subirlo
            # a la cuenta equivocada seria peor que no subirlo.
            return
        try:
            self._storage.encolar_pendientes(user_id=dueno, account_id=cuenta)
        except Exception:  # noqa: BLE001
            log.exception("No se pudo preparar lo pendiente de subir")

    # -- Trabajo -------------------------------------------------------------

    def procesar_lote(self, limite: int = 5) -> int:
        trabajos = self._storage.jobs.reclamar(limite=limite)
        for trabajo in trabajos:
            self._procesar(trabajo)
        return len(trabajos)

    def _procesar(self, trabajo: StorageJob) -> None:
        try:
            almacenamiento = self._almacenamiento_de(trabajo.user_id)
            if trabajo.job_type == "message_segment":
                self._subir_segmento(trabajo, almacenamiento)
            elif trabajo.job_type == "media":
                self._subir_multimedia(trabajo, almacenamiento)
            else:
                raise StorageError("UNKNOWN_JOB", f"Tipo desconocido: {trabajo.job_type}")
            self._storage.jobs.completar(trabajo.id)

        except StorageAuthError as exc:
            # Reintentar no arregla un acceso revocado: solo gasta cupo. Se
            # pausa TODO lo de ese usuario y se le pide reconectar.
            log.warning("Drive rechazo el acceso: %s", exc.code)
            self._storage.jobs.pausar_todos(trabajo.user_id, exc.message)
            self._google.marcar_invalido(trabajo.user_id)
            self._avisar("storage.reauth_required", {"reason": exc.message})

        except StorageQuotaError as exc:
            log.warning("Drive limito la subida: %s", exc.code)
            self._storage.jobs.reintentar(
                trabajo.id, exc.message, retry_after=exc.retry_after
            )
            self._avisar("storage.quota", {"code": exc.code, "message": exc.message})

        except StorageError as exc:
            if exc.reintentable:
                self._storage.jobs.reintentar(trabajo.id, exc.message)
            else:
                self._storage.jobs.reintentar(trabajo.id, f"[no reintentable] {exc.message}")

        except Exception as exc:  # noqa: BLE001
            log.exception("No se pudo completar una subida")
            self._storage.jobs.reintentar(trabajo.id, str(exc)[:300])

    # -- Segmentos -----------------------------------------------------------

    def _subir_segmento(self, trabajo: StorageJob, almacenamiento: Any) -> None:
        segmento_id = uuid.UUID(trabajo.entity_id)

        with self._database.transaction() as sesion:
            fila = sesion.get(MessageSegment, segmento_id)
            if fila is None:
                log.info("El segmento ya no existe; se da el trabajo por hecho")
                return
            # Ya subido: un reintento NO puede crear una copia en Drive.
            if fila.drive_file_id and fila.status == "ready":
                return
            datos_fila = {
                "chat_id": fila.chat_id,
                "sequence": fila.sequence_number,
                "cifrado": bool(fila.encrypted),
                "cuenta": fila.whatsapp_account_id,
            }
            fila.status = "uploading"
            sesion.flush()

        contenido = self._storage.reconstruir(segmento_id)
        if not contenido:
            log.warning("Segmento sin mensajes; nada que subir")
            self._marcar_segmento(segmento_id, status="ready", file_id=None)
            return

        cifrado = self._storage.cifrado_de(trabajo.user_id) if datos_fila["cifrado"] else None
        paquete = seg.empaquetar(contenido, encryption=cifrado)

        carpeta = almacenamiento.carpeta_de_mensajes(
            str(datos_fila["cuenta"]), str(datos_fila["chat_id"])
        )
        subido = almacenamiento.store_bytes(
            carpeta=carpeta,
            nombre=seg.nombre_de_archivo(datos_fila["sequence"]),
            datos=paquete.datos,
            propiedades=PropiedadesDeArchivo(
                entity="message_segment",
                account_id=str(datos_fila["cuenta"]),
                chat_id=str(datos_fila["chat_id"]),
                segment_id=str(segmento_id),
                sequence=datos_fila["sequence"],
            ),
            mime_type="application/gzip",
        )

        self._marcar_segmento(
            segmento_id,
            status="ready",
            file_id=subido.file_id,
            paquete=paquete,
        )
        log.info(
            "[DRIVE] Subida completa entidad=segmento chat=%s tamano=%d",
            datos_fila["chat_id"],
            paquete.bytes_almacenados,
        )
        self._avisar(
            "storage.segment.ready",
            {"chat_id": datos_fila["chat_id"], "sequence": datos_fila["sequence"]},
        )

    def _marcar_segmento(
        self,
        segmento_id: uuid.UUID,
        *,
        status: str,
        file_id: str | None,
        paquete: Any = None,
    ) -> None:
        with self._database.transaction() as sesion:
            fila = sesion.get(MessageSegment, segmento_id)
            if fila is None:
                return
            fila.status = status
            fila.drive_file_id = file_id or fila.drive_file_id
            if paquete is not None:
                fila.sha256 = paquete.sha256_claro
                fila.ciphertext_sha256 = paquete.sha256_cifrado
                fila.compressed_bytes = paquete.bytes_comprimidos
                fila.stored_bytes = paquete.bytes_almacenados
            fila.uploaded_at = _ahora()
            sesion.flush()

            # Los mensajes de este segmento ya estan a salvo.
            sesion.execute(
                update(Message)
                .where(Message.segment_id == segmento_id)
                .values(storage_status="ready")
            )

    # -- Multimedia ----------------------------------------------------------

    def _subir_multimedia(self, trabajo: StorageJob, almacenamiento: Any) -> None:
        media_id = int(trabajo.entity_id)

        with self._database.transaction() as sesion:
            fila = sesion.get(MediaFile, media_id)
            if fila is None:
                return
            if fila.drive_file_id and fila.storage_status == "ready":
                return
            datos = {
                "chat_id": fila.chat_id,
                "local_path": fila.local_path,
                "mime": fila.mime_type or "application/octet-stream",
                "nombre": fila.file_name,
                "cuenta": trabajo.whatsapp_account_id,
            }
            fila.storage_status = "uploading"
            sesion.flush()

        ruta = self._ruta_local(datos["local_path"])
        if ruta is None or not ruta.exists():
            raise StorageError(
                "MEDIA_FILE_MISSING",
                "El archivo local ya no esta; no hay nada que subir.",
            )

        tamano = ruta.stat().st_size
        huella_clara = sha256_de_archivo(ruta)
        cifrado = self._storage.cifrado_de(trabajo.user_id)

        carpeta = almacenamiento.carpeta_de_multimedia(
            str(datos["cuenta"]), str(datos["chat_id"])
        )
        propiedades = PropiedadesDeArchivo(
            entity="media",
            account_id=str(datos["cuenta"]),
            chat_id=str(datos["chat_id"]),
            media_id=str(media_id),
        )
        nombre = f"media-{media_id:09d}.bin.enc"

        if cifrado is not None:
            # Se abre el archivo y se cifra por trozos segun se envia: un
            # video de 5 GB no puede pasar por memoria.
            with ruta.open("rb") as f:
                cabecera_bytes = len(
                    __import__("app.storage.encryption", fromlist=["Cabecera"])
                    .Cabecera(entity="media", chunked=True, plaintext_size=tamano)
                    .to_bytes()
                )
                esperado = tamano_cifrado(tamano, cabecera_bytes)
                subido = almacenamiento.store_stream(
                    carpeta=carpeta,
                    nombre=nombre,
                    origen=cifrado.encrypt_stream(
                        f, entity="media", plaintext_size=tamano
                    ),
                    tamano=esperado,
                    propiedades=propiedades,
                    mime_type="application/octet-stream",
                )
        else:
            with ruta.open("rb") as f:
                subido = almacenamiento.store_stream(
                    carpeta=carpeta,
                    nombre=nombre,
                    origen=iter(lambda: f.read(1024 * 1024), b""),
                    tamano=tamano,
                    propiedades=propiedades,
                    mime_type=datos["mime"],
                )

        with self._database.transaction() as sesion:
            fila = sesion.get(MediaFile, media_id)
            if fila is not None:
                fila.drive_file_id = subido.file_id
                fila.storage_status = "ready"
                fila.stored_bytes = subido.size
                fila.plaintext_sha256 = huella_clara
                fila.uploaded_at = _ahora()
                sesion.flush()

        log.info("[DRIVE] Subida completa entidad=media tamano=%d", subido.size)

    def _ruta_local(self, relativa: str | None) -> Path | None:
        if not relativa:
            return None
        ruta = Path(relativa)
        if ruta.is_absolute():
            return ruta
        return Path(self._settings.media_dir) / ruta

    # -- Auxiliares ----------------------------------------------------------

    def _almacenamiento_de(self, user_id: uuid.UUID) -> Any:
        """Construye el cliente del usuario. Nunca uno compartido.

        El token se pide en cada lote y no se cachea entre usuarios: un
        cliente global seria un cliente capaz de escribir en el Drive de
        cualquiera.
        """
        from app.storage.drive.service import DriveBackupStorage
        from app.storage.drive.client import DriveClient

        token = self._google.access_token(user_id)
        if not token:
            raise StorageAuthError(
                "Google Drive necesita reconectarse: el acceso ya no es valido."
            )
        return DriveBackupStorage(
            user_id=user_id, client=DriveClient(token), database=self._database
        )

    def _avisar(self, evento: str, datos: dict) -> None:
        if self._publish is None:
            return
        try:
            self._publish(evento, datos)
        except Exception:  # noqa: BLE001 - avisar no puede tumbar la subida
            log.debug("No se pudo publicar %s", evento)


def _ahora() -> datetime:
    return datetime.now(timezone.utc)
