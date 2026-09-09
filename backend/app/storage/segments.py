"""Agrupar mensajes en segmentos: JSON Lines, comprimido y cifrado.

POR QUE AGRUPADOS
-----------------
Un mensaje por archivo daria millones de archivos en Drive. Listar una carpeta
asi es lento, recorrerla es caro, y la API tiene limites de peticiones que se
agotan enseguida. Un segmento de mil mensajes convierte mil subidas en una.

POR QUE JSON LINES
------------------
Una linea por mensaje. Se puede leer en streaming sin cargar el archivo
entero, se puede saltar a la linea N, y sigue siendo legible por una persona
si algun dia hay que recuperar a mano.

QUE NO ENTRA NUNCA
------------------
Claves Signal, claves privadas, tokens de Google, el estado del dispositivo.
Un segmento contiene conversaciones; si ademas contuviera con que descifrarlas
o con que suplantar al dispositivo, una sola filtracion lo daria todo.

EL CICLO
--------
    abierto -> se le anaden mensajes
             -> se cierra al llegar a un limite (numero, tamano o edad)
             -> se comprime, se cifra y se sube
             -> queda INMUTABLE

Cerrado no se vuelve a tocar. Reescribir un archivo de 100 MB cada vez que
llega un mensaje seria descargarlo, modificarlo y volver a subirlo entero.
"""

from __future__ import annotations

import gzip
import io
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from app.core.logging_setup import get_logger
from app.storage.encryption import StorageEncryption, sha256_de

log = get_logger("STORAGE")

#: Version del formato de linea. Va en cada segmento.
SCHEMA_VERSION = 1

#: Campos que NUNCA pueden salir hacia el almacenamiento, aunque alguien los
#: anada al diccionario del mensaje por descuido. La lista es una red de
#: seguridad, no la puerta principal: el constructor solo copia lo que conoce.
PROHIBIDOS = frozenset(
    {
        "media_key",
        "file_enc_sha256",
        "signal",
        "identity_private",
        "noise_private",
        "session",
        "prekey",
        "access_token",
        "refresh_token",
        "password",
        "password_hash",
    }
)


@dataclass
class LineaDeMensaje:
    """Un mensaje tal y como queda guardado."""

    id: int
    wamid: str | None
    timestamp: int
    from_me: bool
    sender: str | None
    type: str
    text: str | None = None
    media_ref: int | None = None
    raw_proto_b64: str | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        cuerpo: dict[str, Any] = {
            "v": SCHEMA_VERSION,
            "id": self.id,
            "wamid": self.wamid,
            "timestamp": self.timestamp,
            "from_me": self.from_me,
            "sender": self.sender,
            "type": self.type,
        }
        if self.text is not None:
            cuerpo["text"] = self.text
        if self.media_ref is not None:
            cuerpo["media_ref"] = self.media_ref
        if self.raw_proto_b64:
            # El protobuf original viaja aqui y NO en PostgreSQL: es lo que
            # permite reinterpretar un mensaje cuando el parser mejore, sin
            # que la base cargue con ese peso.
            cuerpo["raw_proto_b64"] = self.raw_proto_b64
        if self.metadata:
            cuerpo["metadata"] = {
                k: v for k, v in self.metadata.items() if k not in PROHIBIDOS
            }
        return cuerpo


def desde_fila(fila: Any, *, incluir_raw: bool = True) -> LineaDeMensaje:
    """Construye la linea a partir de un ``Message`` de SQLAlchemy.

    Copia campo a campo, sin volcar el objeto entero: asi anadir una columna a
    la tabla no puede filtrar algo nuevo al almacenamiento sin que nadie lo
    decida.
    """
    import base64

    crudo = getattr(fila, "raw_proto", None)
    return LineaDeMensaje(
        id=fila.id,
        wamid=fila.whatsapp_message_id,
        timestamp=fila.timestamp,
        from_me=bool(fila.from_me),
        sender=fila.sender_jid or fila.sender_lid,
        type=fila.message_type,
        text=fila.text,
        media_ref=None,
        raw_proto_b64=(
            base64.b64encode(crudo).decode("ascii") if incluir_raw and crudo else None
        ),
        metadata=fila.raw_metadata if isinstance(fila.raw_metadata, dict) else None,
    )


@dataclass
class SegmentoAbierto:
    """Un segmento que todavia acepta mensajes."""

    chat_id: int
    chat_jid: str
    sequence_number: int
    lineas: list[bytes] = field(default_factory=list)
    bytes_sin_comprimir: int = 0
    primer_timestamp: int | None = None
    ultimo_timestamp: int | None = None
    #: ``None`` mientras no se sabe cuando se abrio. Se usa ``None`` y no 0
    #: porque 0 es un instante valido de ``time.monotonic()`` y ademas es
    #: falsy: con 0 como centinela, la comprobacion de edad se saltaba entera
    #: y un chat tranquilo no habria subido nunca.
    abierto_en: float | None = None

    def anadir(self, mensaje: LineaDeMensaje) -> int:
        """Anade y devuelve su posicion (la linea dentro del archivo)."""
        crudo = json.dumps(
            mensaje.to_dict(), separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        posicion = len(self.lineas)
        self.lineas.append(crudo)
        self.bytes_sin_comprimir += len(crudo) + 1
        if self.primer_timestamp is None or mensaje.timestamp < self.primer_timestamp:
            self.primer_timestamp = mensaje.timestamp
        if self.ultimo_timestamp is None or mensaje.timestamp > self.ultimo_timestamp:
            self.ultimo_timestamp = mensaje.timestamp
        return posicion

    @property
    def cuenta(self) -> int:
        return len(self.lineas)

    def debe_cerrarse(
        self, *, max_mensajes: int, max_bytes: int, max_edad: float, ahora: float
    ) -> str | None:
        """El motivo por el que toca cerrarlo, o ``None``.

        La edad importa tanto como el tamano: un chat con poco trafico dejaria
        su segmento abierto indefinidamente y esos mensajes no llegarian nunca
        a Drive.
        """
        if self.cuenta >= max_mensajes:
            return "lleno"
        if self.bytes_sin_comprimir >= max_bytes:
            return "tamano"
        if self.abierto_en is not None and (ahora - self.abierto_en) >= max_edad:
            return "edad"
        return None

    def contenido(self) -> bytes:
        """El JSONL en claro, terminado en salto de linea."""
        if not self.lineas:
            return b""
        return b"\n".join(self.lineas) + b"\n"


@dataclass(frozen=True)
class SegmentoListo:
    """Un segmento ya empaquetado, a punto de subir."""

    datos: bytes
    sha256_claro: str
    sha256_cifrado: str
    bytes_sin_comprimir: int
    bytes_comprimidos: int
    bytes_almacenados: int
    cifrado: bool


def empaquetar(
    contenido: bytes, *, encryption: StorageEncryption | None
) -> SegmentoListo:
    """JSONL -> gzip -> AES-GCM.

    Se guardan las DOS huellas: la del contenido en claro sirve para
    comprobar que lo recuperado es lo que se guardo; la del archivo cifrado,
    para comprobar la subida sin tener que descifrar nada.
    """
    comprimido = _comprimir(contenido)
    huella_clara = sha256_de(contenido)

    if encryption is None:
        # Sin cifrar tambien lleva cabecera de formato: leer un archivo no
        # puede depender de recordar con que ajustes se escribio.
        from app.storage.encryption import Cabecera

        cabecera = Cabecera(
            entity="message_segment",
            chunked=False,
            compression="gzip",
            plaintext_size=len(contenido),
        ).to_bytes()
        final = cabecera + comprimido
    else:
        final = encryption.encrypt_bytes(
            comprimido, entity="message_segment", compression="gzip"
        )

    return SegmentoListo(
        datos=final,
        sha256_claro=huella_clara,
        sha256_cifrado=sha256_de(final),
        bytes_sin_comprimir=len(contenido),
        bytes_comprimidos=len(comprimido),
        bytes_almacenados=len(final),
        cifrado=encryption is not None,
    )


def _comprimir(datos: bytes) -> bytes:
    """gzip determinista.

    ``mtime=0`` a proposito: con la marca de tiempo por defecto, comprimir dos
    veces el mismo contenido da archivos distintos y la huella deja de servir
    para comparar.
    """
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=6, mtime=0) as f:
        f.write(datos)
    return buffer.getvalue()


def desempaquetar(
    datos: bytes, *, encryption: StorageEncryption | None
) -> bytes:
    """Lo contrario: descifra si hace falta y descomprime."""
    from app.storage.encryption import Cabecera

    if encryption is not None:
        try:
            comprimido, cabecera = encryption.decrypt_bytes(datos)
        except Exception:
            # Puede ser un segmento guardado sin cifrar antes de encenderlo.
            cabecera, fin = Cabecera.leer(datos)
            comprimido = datos[fin:]
    else:
        cabecera, fin = Cabecera.leer(datos)
        comprimido = datos[fin:]

    if cabecera.compression == "gzip":
        return gzip.decompress(comprimido)
    return comprimido


def leer_lineas(contenido: bytes) -> Iterator[dict[str, Any]]:
    """Recorre el JSONL sin construir una lista con todo dentro."""
    for linea in contenido.split(b"\n"):
        if not linea.strip():
            continue
        try:
            yield json.loads(linea)
        except ValueError:
            # Una linea rota no puede impedir leer las demas: se pierde ese
            # mensaje, no la conversacion entera.
            log.warning("Linea ilegible en un segmento; se salta")


def linea_numero(contenido: bytes, indice: int) -> dict[str, Any] | None:
    """El mensaje que esta en esa posicion.

    Es lo que hace que ``segment_index`` sirva de algo: sin el habria que
    recorrer el archivo entero para encontrar un mensaje concreto.
    """
    for posicion, mensaje in enumerate(leer_lineas(contenido)):
        if posicion == indice:
            return mensaje
    return None


def nombre_de_archivo(sequence_number: int) -> str:
    """``segment-000001.jsonl.gz.enc``.

    Con relleno de ceros para que el orden alfabetico coincida con el
    cronologico al mirar la carpeta desde Drive.
    """
    return f"segment-{sequence_number:06d}.jsonl.gz.enc"


def mensajes_a_lineas(filas: Iterable[Any]) -> Iterator[LineaDeMensaje]:
    for fila in filas:
        yield desde_fila(fila)
