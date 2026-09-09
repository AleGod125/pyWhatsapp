"""Leer mensajes desde el almacenamiento. La fuente del contenido.

EL REPARTO
----------
PostgreSQL dice QUE mensajes hay y DONDE estan: chat, orden, segmento y linea.
Drive tiene el contenido. Este modulo es el puente.

POR QUE NO SE RECORRE EL ARCHIVO
--------------------------------
Cada mensaje guarda su ``segment_index``. Sin el habria que leer el segmento
entero buscando cual es cual; con el, se salta directo a la linea.

POR QUE SE AGRUPA POR SEGMENTO
------------------------------
Una pagina de 200 mensajes suele caber en uno o dos segmentos. Descargar el
mismo archivo 200 veces seria 200 llamadas a Google para leer lo mismo. Se
descarga UNA vez por segmento y se resuelven todas sus lineas.

CUANDO EL CONTENIDO NO ESTA
---------------------------
Si un mensaje todavia no se ha subido, se responde con lo que hay en
PostgreSQL y se dice que es provisional. Lo que NO se hace es devolver una
lista vacia fingiendo que no hay nada: eso es mentir sobre una copia.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.core.logging_setup import get_logger
from app.models.storage import MessageSegment
from app.storage import segments as seg
from app.storage.encryption import huellas_iguales, sha256_de
from app.storage.interface import StorageError

log = get_logger("STORAGE")

#: Cuantos segmentos ya descifrados se guardan en memoria. Una conversacion se
#: lee hacia atras pagina a pagina, y casi siempre repite el mismo archivo.
#: Es CACHE: vaciarla no cambia ningun resultado, solo cuesta una descarga.
CACHE_SEGMENTOS = 8


class ContenidoNoDisponible(StorageError):
    """El contenido esta en Drive y ahora mismo no se puede traer."""

    def __init__(self, mensaje: str) -> None:
        super().__init__("STORAGE_TEMPORARILY_UNAVAILABLE", mensaje, reintentable=True)


class ContenidoCorrupto(StorageError):
    """El segmento no supera la comprobacion de integridad."""

    def __init__(self) -> None:
        super().__init__(
            "STORAGE_INTEGRITY_FAILURE",
            "El contenido guardado no supera la comprobacion de integridad.",
        )


@dataclass
class MensajeResuelto:
    """Un mensaje con su contenido, venga de donde venga."""

    id: int
    text: str | None
    fuente: str  # "drive" | "local"
    extra: dict[str, Any] = field(default_factory=dict)


class MessageReader:
    """Resuelve el contenido de los mensajes de una pagina."""

    def __init__(self, database: Any, storage_service: Any) -> None:
        self._database = database
        self._storage = storage_service
        self._cache: "OrderedDict[uuid.UUID, list[dict]]" = OrderedDict()

    # -- Lectura -------------------------------------------------------------

    def resolver(
        self, filas: list[Any], *, user_id: uuid.UUID, almacenamiento: Any
    ) -> dict[int, MensajeResuelto]:
        """El contenido de esos mensajes, por identificador.

        ``filas`` son objetos ``Message`` ya paginados por PostgreSQL. Aqui no
        se decide QUE mensajes salen: solo de donde viene su contenido.
        """
        resueltos: dict[int, MensajeResuelto] = {}

        # Los que aun no estan subidos se sirven de PostgreSQL. Es una etapa
        # transitoria, y se marca como tal para no confundirla con lo que ya
        # esta a salvo.
        por_segmento: dict[uuid.UUID, list[Any]] = {}
        for fila in filas:
            if fila.segment_id is None or fila.storage_status != "ready":
                resueltos[fila.id] = MensajeResuelto(
                    id=fila.id, text=fila.text, fuente="local"
                )
                continue
            por_segmento.setdefault(fila.segment_id, []).append(fila)

        if not por_segmento:
            return resueltos

        log.info(
            "[STORAGE] resolviendo pagina mensajes=%d segmentos=%d fuente=drive",
            sum(len(v) for v in por_segmento.values()),
            len(por_segmento),
        )

        for segmento_id, mensajes in por_segmento.items():
            lineas = self._lineas_de(
                segmento_id, user_id=user_id, almacenamiento=almacenamiento
            )
            for fila in mensajes:
                cuerpo = self._linea(lineas, fila)
                if cuerpo is None:
                    # El indice no cuadra con el archivo. NO se inventa: se
                    # sirve lo local y se deja constancia.
                    log.warning(
                        "[STORAGE] linea %s no encontrada en su segmento",
                        fila.segment_index,
                    )
                    resueltos[fila.id] = MensajeResuelto(
                        id=fila.id, text=fila.text, fuente="local"
                    )
                    continue
                resueltos[fila.id] = MensajeResuelto(
                    id=fila.id,
                    text=cuerpo.get("text"),
                    fuente="drive",
                    extra=cuerpo,
                )
        return resueltos

    @staticmethod
    def _linea(lineas: list[dict], fila: Any) -> dict | None:
        """La linea de ese mensaje. Por posicion, y comprobada por id.

        La posicion es lo rapido; el identificador es lo que garantiza que se
        entrega el mensaje correcto. Si no coinciden, se busca: mejor pagar una
        pasada que devolver el mensaje equivocado.
        """
        indice = fila.segment_index
        if indice is not None and 0 <= indice < len(lineas):
            candidato = lineas[indice]
            if candidato.get("id") == fila.id:
                return candidato
        for linea in lineas:
            if linea.get("id") == fila.id:
                return linea
        return None

    def _lineas_de(
        self, segmento_id: uuid.UUID, *, user_id: uuid.UUID, almacenamiento: Any
    ) -> list[dict]:
        if segmento_id in self._cache:
            self._cache.move_to_end(segmento_id)
            return self._cache[segmento_id]

        lineas = self._descargar(segmento_id, user_id, almacenamiento)
        self._cache[segmento_id] = lineas
        while len(self._cache) > CACHE_SEGMENTOS:
            self._cache.popitem(last=False)
        return lineas

    def _descargar(
        self, segmento_id: uuid.UUID, user_id: uuid.UUID, almacenamiento: Any
    ) -> list[dict]:
        with self._database.transaction() as sesion:
            fila = sesion.get(MessageSegment, segmento_id)
            if fila is None:
                raise ContenidoNoDisponible("Ese bloque de mensajes ya no consta.")
            # La propiedad se comprueba SIEMPRE, aunque el mensaje ya se haya
            # filtrado por chat: son dos comprobaciones distintas y perder una
            # significaria leer el contenido de otro.
            if fila.user_id != user_id:
                raise ContenidoNoDisponible("Ese bloque de mensajes no es tuyo.")
            datos = {
                "file_id": fila.drive_file_id,
                "cifrado": bool(fila.encrypted),
                "huella": fila.ciphertext_sha256,
            }

        if not datos["file_id"]:
            raise ContenidoNoDisponible(
                "Ese bloque de mensajes todavia no ha terminado de guardarse."
            )

        try:
            crudo = almacenamiento.read_file(datos["file_id"])
        except StorageError as exc:
            # Se propaga el motivo real: "no se pudo traer" no es lo mismo que
            # "no hay mensajes", y el frontend tiene que poder distinguirlo.
            raise ContenidoNoDisponible(exc.message) from exc

        # Se comprueba el archivo tal y como esta guardado, sin descifrar. Si
        # no cuadra, algo cambio por el camino.
        if datos["huella"] and not huellas_iguales(sha256_de(crudo), datos["huella"]):
            log.error(
                "[STORAGE] fallo de integridad segmento=%s", str(segmento_id)[:8]
            )
            raise ContenidoCorrupto()

        cifrado = self._storage.cifrado_de(user_id) if datos["cifrado"] else None
        try:
            contenido = seg.desempaquetar(crudo, encryption=cifrado)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "[STORAGE] no se pudo abrir el segmento=%s", str(segmento_id)[:8]
            )
            raise ContenidoCorrupto() from exc

        return list(seg.leer_lineas(contenido))

    def vaciar_cache(self) -> None:
        """Solo para pruebas: demuestra que la lectura no depende de la cache."""
        self._cache.clear()


def read_message_segment(
    database: Any,
    storage_service: Any,
    segmento_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    almacenamiento: Any,
) -> list[dict]:
    """Un segmento entero, comprobado y descifrado.

    Punto unico de lectura: ``routes.py`` no descarga ni descifra nada por su
    cuenta. Duplicar esa logica seria duplicar tambien las comprobaciones de
    propiedad e integridad, y basta olvidar una.
    """
    lector = MessageReader(database, storage_service)
    return lector._descargar(segmento_id, user_id, almacenamiento)
