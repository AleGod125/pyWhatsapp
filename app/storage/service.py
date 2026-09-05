"""El servicio de almacenamiento: claves, segmentos y encolado.

Es lo que usa el resto del sistema. La ingesta no sabe que existe Google: le
entrega mensajes y este modulo decide cuando cerrar un segmento y encolarlo.

EL ORDEN IMPORTA
----------------
El mensaje se escribe en PostgreSQL y su trabajo de subida se crea en la MISMA
transaccion. Nunca se sube nada desde el hilo que recibe de WhatsApp: una
llamada a Drive puede tardar segundos y ese hilo tiene que seguir recibiendo.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from app.core.logging_setup import get_logger
from app.models import Chat, Message, WhatsAppAccount
from app.models.storage import MessageSegment, UserStorageKey
from app.storage import segments as seg
from app.storage.encryption import (
    StorageEncryption,
    derivar_kek,
    desenvolver_dek,
    envolver_dek,
    nueva_dek,
)
from app.storage.jobs import StorageJobQueue

log = get_logger("STORAGE")


class StorageBlocked(RuntimeError):
    """Hay demasiado pendiente de subir.

    No se borra nada ni se deja de recibir: se avisa. Dejar crecer la cola sin
    limite acabaria llenando el disco, y borrar mensajes en silencio para
    hacer sitio seria peor que cualquier aviso.
    """

    code = "STORAGE_BLOCKED"


@dataclass
class LimitesDeSegmento:
    max_mensajes: int = 1000
    max_bytes: int = 5 * 1024 * 1024
    max_edad: float = 60.0

    @classmethod
    def desde(cls, settings: Any) -> "LimitesDeSegmento":
        return cls(
            max_mensajes=int(
                getattr(settings, "drive_message_segment_max_messages", 1000)
            ),
            max_bytes=int(getattr(settings, "drive_message_segment_max_bytes", 5242880)),
            max_edad=float(getattr(settings, "drive_segment_max_age_seconds", 60)),
        )


class StorageService:
    """Coordina claves, segmentos y cola. No habla con Google."""

    def __init__(self, database: Any, settings: Any) -> None:
        self._database = database
        self._settings = settings
        self._jobs = StorageJobQueue(database)
        self._limites = LimitesDeSegmento.desde(settings)
        #: Segmentos a medio llenar, por chat. Su contenido tambien esta en
        #: PostgreSQL, asi que perderlos al reiniciar no pierde mensajes: se
        #: reconstruyen desde la base.
        self._abiertos: dict[int, seg.SegmentoAbierto] = {}

    @property
    def jobs(self) -> StorageJobQueue:
        return self._jobs

    @property
    def habilitado(self) -> bool:
        return bool(getattr(self._settings, "drive_storage_enabled", False))

    # -- Claves --------------------------------------------------------------

    def clave_de(self, user_id: uuid.UUID) -> bytes:
        """La DEK del usuario, creandola la primera vez.

        Se genera una por usuario: con una sola clave para todos, un descuido
        abriria el contenido de todo el mundo a la vez.
        """
        kek = derivar_kek(
            getattr(self._settings, "app_encryption_key", None),
            getattr(self._settings, "storage_kek", None),
        )
        with self._database.transaction() as sesion:
            fila = sesion.execute(
                select(UserStorageKey).where(UserStorageKey.user_id == user_id)
            ).scalar_one_or_none()
            if fila is not None:
                return desenvolver_dek(fila.encrypted_dek, kek)

            dek = nueva_dek()
            sesion.add(
                UserStorageKey(
                    user_id=user_id, encrypted_dek=envolver_dek(dek, kek), key_version=1
                )
            )
            sesion.flush()
            log.info("Clave de contenido creada para el usuario")
            return dek

    def cifrado_de(self, user_id: uuid.UUID) -> StorageEncryption | None:
        if not getattr(self._settings, "storage_encryption_enabled", True):
            return None
        return StorageEncryption(self.clave_de(user_id))

    # -- Contrapresion -------------------------------------------------------

    def comprobar_espacio(self, user_id: uuid.UUID) -> None:
        limite = int(getattr(self._settings, "max_pending_storage_bytes", 0) or 0)
        if limite <= 0:
            return
        pendiente = self._jobs.bytes_pendientes(user_id)
        if pendiente >= limite:
            raise StorageBlocked(
                f"Hay {pendiente // (1024*1024)} MB esperando a subirse y el "
                f"limite son {limite // (1024*1024)} MB. Revisa la conexion con "
                f"Google Drive. NO se ha borrado ningun mensaje."
            )

    # -- Segmentos -----------------------------------------------------------

    def anadir_mensaje(
        self, sesion: Any, mensaje: Message, *, user_id: uuid.UUID, account_id: uuid.UUID
    ) -> None:
        """Mete el mensaje en el segmento abierto de su chat.

        Recibe la sesion de la transaccion en curso: el indice del mensaje y
        su trabajo de subida tienen que entrar juntos o no entrar.
        """
        if not self.habilitado:
            return

        abierto = self._abiertos.get(mensaje.chat_id)
        if abierto is None:
            abierto = self._abrir(sesion, mensaje.chat_id, user_id)
            self._abiertos[mensaje.chat_id] = abierto

        posicion = abierto.anadir(seg.desde_fila(mensaje))
        mensaje.segment_index = posicion
        mensaje.storage_status = "pending"

        motivo = abierto.debe_cerrarse(
            max_mensajes=self._limites.max_mensajes,
            max_bytes=self._limites.max_bytes,
            max_edad=self._limites.max_edad,
            ahora=time.monotonic(),
        )
        if motivo:
            self.cerrar(sesion, mensaje.chat_id, user_id=user_id, account_id=account_id,
                        motivo=motivo)

    def _abrir(self, sesion: Any, chat_id: int, user_id: uuid.UUID) -> seg.SegmentoAbierto:
        chat = sesion.get(Chat, chat_id)
        siguiente = (
            sesion.execute(
                select(func.coalesce(func.max(MessageSegment.sequence_number), 0)).where(
                    MessageSegment.chat_id == chat_id
                )
            ).scalar()
            or 0
        ) + 1
        return seg.SegmentoAbierto(
            chat_id=chat_id,
            chat_jid=chat.jid if chat else "",
            sequence_number=siguiente,
            abierto_en=time.monotonic(),
        )

    def cerrar(
        self,
        sesion: Any,
        chat_id: int,
        *,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        motivo: str = "manual",
    ) -> MessageSegment | None:
        """Cierra el segmento, lo registra y encola su subida.

        No sube nada: solo deja constancia. Subir aqui bloquearia al que
        recibe de WhatsApp durante toda una llamada de red.
        """
        abierto = self._abiertos.pop(chat_id, None)
        if abierto is None or abierto.cuenta == 0:
            return None

        fila = MessageSegment(
            user_id=user_id,
            whatsapp_account_id=account_id,
            chat_id=chat_id,
            chat_jid=abierto.chat_jid,
            sequence_number=abierto.sequence_number,
            first_timestamp=abierto.primer_timestamp,
            last_timestamp=abierto.ultimo_timestamp,
            message_count=abierto.cuenta,
            uncompressed_bytes=abierto.bytes_sin_comprimir,
            status="building",
            encrypted=bool(getattr(self._settings, "storage_encryption_enabled", True)),
            closed_at=_ahora(),
        )
        sesion.add(fila)
        sesion.flush()

        # Los mensajes de este segmento apuntan a el. Sin esta referencia,
        # recuperar un mensaje concreto obligaria a abrir todos los archivos.
        sesion.execute(
            Message.__table__.update()
            .where(
                Message.chat_id == chat_id,
                Message.segment_id.is_(None),
                Message.storage_status == "pending",
            )
            .values(segment_id=fila.id)
        )

        self._jobs.encolar(
            sesion,
            user_id=user_id,
            account_id=account_id,
            job_type="message_segment",
            entity_id=str(fila.id),
            payload_bytes=abierto.bytes_sin_comprimir,
            detail={"chat_id": chat_id, "sequence": abierto.sequence_number},
        )
        log.info(
            "[STORAGE] Segmento listo chat=%s secuencia=%d mensajes=%d motivo=%s",
            chat_id,
            abierto.sequence_number,
            abierto.cuenta,
            motivo,
        )
        return fila

    def cerrar_vencidos(self, *, user_id: uuid.UUID, account_id: uuid.UUID) -> int:
        """Cierra los segmentos que llevan demasiado abiertos.

        Sin esto, un chat con poco trafico dejaria su segmento abierto
        indefinidamente y esos mensajes no llegarian nunca a Drive.
        """
        ahora = time.monotonic()
        vencidos = [
            chat_id
            for chat_id, abierto in self._abiertos.items()
            if abierto.debe_cerrarse(
                max_mensajes=self._limites.max_mensajes,
                max_bytes=self._limites.max_bytes,
                max_edad=self._limites.max_edad,
                ahora=ahora,
            )
        ]
        if not vencidos:
            return 0
        with self._database.transaction() as sesion:
            for chat_id in vencidos:
                self.cerrar(
                    sesion, chat_id, user_id=user_id, account_id=account_id,
                    motivo="edad",
                )
        return len(vencidos)

    # -- Barrido de lo que falta por subir -----------------------------------

    def encolar_pendientes(
        self, *, user_id: uuid.UUID, account_id: uuid.UUID, limite: int = 2000
    ) -> int:
        """Agrupa en segmentos los mensajes que todavia no estan en Drive.

        POR QUE UN BARRIDO Y NO UN GANCHO EN CADA SITIO
        -----------------------------------------------
        Los mensajes entran por tres caminos —bootstrap inicial, ON_DEMAND y
        live— y cada uno persiste a su manera. Colgar un gancho de cada uno
        significa tres sitios donde olvidarlo, y basta olvidarlo en uno para
        que ese origen no llegue nunca al almacenamiento.

        Mirando la tabla se cubren los tres por igual, y ademas se recogen los
        que quedaron atras: al encender esto por primera vez habia miles de
        mensajes ya guardados que ningun gancho habria tocado.

        Devuelve cuantos segmentos se han cerrado.
        """
        if not self.habilitado:
            return 0

        cerrados = 0
        with self._database.transaction() as sesion:
            pendientes = (
                sesion.execute(
                    select(Message)
                    .join(Chat, Chat.id == Message.chat_id)
                    .where(
                        Chat.whatsapp_account_id == account_id,
                        Message.storage_status == "local",
                    )
                    .order_by(Message.chat_id, Message.timestamp, Message.id)
                    .limit(limite)
                )
                .scalars()
                .all()
            )
            if not pendientes:
                return 0

            log.info(
                "[STORAGE] %d mensaje(s) sin subir; agrupandolos en segmentos",
                len(pendientes),
            )
            por_chat: dict[int, list[Message]] = {}
            for mensaje in pendientes:
                por_chat.setdefault(mensaje.chat_id, []).append(mensaje)

            for chat_id, mensajes in por_chat.items():
                for mensaje in mensajes:
                    self.anadir_mensaje(
                        sesion, mensaje, user_id=user_id, account_id=account_id
                    )
                # Se cierra lo que quede abierto de ese chat: este barrido
                # trata con historial ya guardado, no con un chat vivo al que
                # aun puedan llegarle mensajes en los proximos segundos.
                if self.cerrar(
                    sesion,
                    chat_id,
                    user_id=user_id,
                    account_id=account_id,
                    motivo="barrido",
                ):
                    cerrados += 1
        return cerrados

    # -- Reconstruccion tras un cierre brusco --------------------------------

    def reconstruir(self, segmento_id: uuid.UUID) -> bytes | None:
        """Rehace el contenido de un segmento desde PostgreSQL.

        Es lo que permite que un cierre brusco no pierda nada: el segmento en
        memoria desaparece, pero sus mensajes siguen en la base y el trabajo
        pendiente dice cuales eran.
        """
        with self._database.transaction() as sesion:
            filas = (
                sesion.execute(
                    select(Message)
                    .where(Message.segment_id == segmento_id)
                    .order_by(Message.segment_index, Message.id)
                )
                .scalars()
                .all()
            )
            if not filas:
                return None
            lineas = [seg.desde_fila(f) for f in filas]

        bloque = seg.SegmentoAbierto(chat_id=0, chat_jid="", sequence_number=0)
        for linea in lineas:
            bloque.anadir(linea)
        return bloque.contenido()


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def cuenta_de(sesion: Any, user_id: uuid.UUID) -> WhatsAppAccount | None:
    return (
        sesion.execute(
            select(WhatsAppAccount).where(WhatsAppAccount.user_id == user_id)
        )
        .scalars()
        .first()
    )
