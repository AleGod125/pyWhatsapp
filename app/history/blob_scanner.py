"""Extraer anclas de los blobs de History Sync ya guardados. UNA vez cada uno.

EL PROBLEMA QUE RESUELVE
------------------------
La revision de conversaciones pendientes descomprimia e interpretaba los
mismos archivos una vez POR CHAT. Con 28 pendientes y 4 blobs eso son 112
lecturas completas para descubrir exactamente lo mismo que la primera.

Aqui se recorre cada archivo UNA vez, se sacan todas las anclas que contenga y
se anota su huella. La proxima vez solo se miran los que no se habian visto.

NO REINGIERE MENSAJES
---------------------
Solo saca referencias. Los mensajes de esos blobs ya estan donde tienen que
estar: PostgreSQL como indice, Drive como contenido. Volver a insertarlos
seria trabajo repetido con riesgo de duplicar.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import select

from app.core.logging_setup import get_logger
from app.history.seed_collector import (
    SeedCandidate,
    desde_web_message_info,
    fuente_de_sync,
)
from app.models import ScannedBlob

log = get_logger("PLAN_E")


@dataclass
class ResultadoDeEscaneo:
    blobs_totales: int = 0
    blobs_nuevos: int = 0
    blobs_saltados: int = 0
    candidatos: int = 0
    ilegibles: int = 0
    por_tipo: dict[str, int] | None = None

    def __post_init__(self) -> None:
        if self.por_tipo is None:
            self.por_tipo = {}


def huella_de(ruta: Path) -> str:
    """SHA-256 del archivo.

    Identifica el CONTENIDO, no el nombre: renombrarlo o volver a archivarlo
    no lo convierte en un blob nuevo.
    """
    h = hashlib.sha256()
    with ruta.open("rb") as f:
        while trozo := f.read(1024 * 1024):
            h.update(trozo)
    return h.hexdigest()


def tipo_del_nombre(ruta: Path) -> str | None:
    """El tipo de sincronizacion, leido del nombre del archivo.

    Los blobs se archivan como ``<fecha>-<TIPO>-chunkNNN-<huella>.pb``. Leerlo
    del nombre evita descomprimir un archivo solo para saber si interesa.
    """
    partes = ruta.stem.split("-")
    return partes[1] if len(partes) >= 2 else None


class BlobSeedScanner:
    """Saca anclas de los blobs guardados, sin repetir trabajo."""

    def __init__(self, database: Any, settings: Any, *, account_id: Any = None) -> None:
        self._database = database
        self._settings = settings
        self._account_id = account_id

    @property
    def carpeta(self) -> Path:
        return Path(self._settings.data_dir) / "history"

    def blobs(self) -> list[Path]:
        carpeta = self.carpeta
        if not carpeta.exists():
            return []
        return sorted(carpeta.glob("*.pb"))

    # -- Escaneo -------------------------------------------------------------

    def escanear(
        self, *, solo_nuevos: bool = True, marcar: bool = True
    ) -> tuple[list[SeedCandidate], ResultadoDeEscaneo]:
        """Recorre los blobs y devuelve los candidatos que contengan.

        Con ``solo_nuevos`` se saltan los ya escaneados: es lo que convierte
        una revision de decenas de lecturas en una barata.

        ``marcar=False`` permite mirar sin dejar constancia — el modo de
        simulacion, que no puede cambiar nada.
        """
        resultado = ResultadoDeEscaneo()
        candidatos: list[SeedCandidate] = []
        vistos = self._huellas_conocidas() if solo_nuevos else set()

        for ruta in self.blobs():
            resultado.blobs_totales += 1
            try:
                huella = huella_de(ruta)
            except OSError:
                resultado.ilegibles += 1
                continue

            if huella in vistos:
                resultado.blobs_saltados += 1
                continue

            tipo = tipo_del_nombre(ruta)
            resultado.por_tipo[tipo or "?"] = resultado.por_tipo.get(tipo or "?", 0) + 1

            try:
                del_blob = list(self._candidatos_de(ruta, tipo))
            except Exception:  # noqa: BLE001 - un blob roto no para el resto
                log.debug("No se pudo leer un blob de historial")
                resultado.ilegibles += 1
                continue

            resultado.blobs_nuevos += 1
            resultado.candidatos += len(del_blob)
            candidatos.extend(del_blob)

            if marcar:
                self._marcar(huella, tipo, len(del_blob))

        return candidatos, resultado

    def _candidatos_de(self, ruta: Path, tipo: str | None) -> Iterator[SeedCandidate]:
        """Las referencias de mensaje que haya dentro.

        Se usa ``parse_web_message_info``, el interprete del proyecto, y no se
        hurga en el protobuf a mano: ``HistorySyncMsg.message`` son bytes
        crudos, y ese interprete ya sabe leerlos, resolver el chat y
        distinguir los mensajes de protocolo. Duplicar esa logica seria
        duplicar tambien sus casos raros.
        """
        from pywhats.proto import history_sync_pb2 as hs

        from app.core.message_parser import parse_web_message_info

        sync = hs.HistorySync()
        sync.ParseFromString(ruta.read_bytes())
        fuente = fuente_de_sync(tipo)

        for conversacion in sync.conversations:
            for envoltorio in conversacion.messages:
                crudo = getattr(envoltorio, "message", None)
                if not crudo:
                    continue
                mensaje = parse_web_message_info(crudo)
                if mensaje is None:
                    continue
                yield SeedCandidate(
                    # El JID del MENSAJE manda: en los blobs puede no coincidir
                    # con el de la conversacion, y el del mensaje es el que
                    # corresponde a donde esta guardado.
                    chat_jid=mensaje.chat_jid or conversacion.id,
                    wa_msg_id=mensaje.whatsapp_message_id,
                    timestamp=mensaje.timestamp,
                    from_me=bool(mensaje.from_me),
                    source=fuente,
                    message_type=mensaje.message_type,
                )

    # -- Registro ------------------------------------------------------------

    def _huellas_conocidas(self) -> set[str]:
        if self._account_id is None:
            return set()
        with self._database.transaction() as sesion:
            return set(
                sesion.execute(
                    select(ScannedBlob.sha256).where(
                        ScannedBlob.whatsapp_account_id == self._account_id
                    )
                )
                .scalars()
                .all()
            )

    def _marcar(self, huella: str, tipo: str | None, encontradas: int) -> None:
        if self._account_id is None:
            return
        with self._database.transaction() as sesion:
            ya = sesion.execute(
                select(ScannedBlob).where(
                    ScannedBlob.whatsapp_account_id == self._account_id,
                    ScannedBlob.sha256 == huella,
                )
            ).scalar_one_or_none()
            if ya is None:
                sesion.add(
                    ScannedBlob(
                        whatsapp_account_id=self._account_id,
                        sha256=huella,
                        sync_type=tipo,
                        seeds_found=encontradas,
                    )
                )
                sesion.flush()

    def hay_blobs_nuevos(self) -> bool:
        """Barato: solo compara huellas, sin abrir nada.

        Permite que una revision automatica no haga trabajo pesado cuando no
        ha cambiado nada.
        """
        if self._account_id is None:
            return bool(self.blobs())
        conocidas = self._huellas_conocidas()
        for ruta in self.blobs():
            try:
                if huella_de(ruta) not in conocidas:
                    return True
            except OSError:
                continue
        return False
