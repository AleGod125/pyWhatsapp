"""Cifrado del contenido antes de subirlo.

POR QUE
-------
Google cifra el transporte y el disco de su infraestructura, pero la copia son
conversaciones privadas y Google tiene la llave de ese cifrado. Cifrando aqui,
lo que llega a Drive es opaco: ni Google, ni quien acceda a esa cuenta de
Google, ni quien robe el enlace de un archivo puede leer nada sin una clave
que nunca sale de este servidor.

QUE SE USA
----------
AES-256-GCM de ``cryptography``, que es cifrado autenticado: si un byte cambia
en el camino, el descifrado FALLA en vez de devolver basura. No hay
criptografia propia.

DOS CLAVES, NO UNA
------------------
* **DEK** (Data Encryption Key): 256 bits, una POR USUARIO. Es la que cifra
  el contenido.
* **KEK** (Key Encryption Key): del servidor. Solo envuelve las DEK.

En la base se guarda la DEK envuelta, nunca en claro. Y la KEK **no** es
``APP_ENCRYPTION_KEY``: se deriva de ella con HKDF y una etiqueta distinta, o
se configura aparte con ``STORAGE_KEK``. Reutilizar la misma clave para los
secretos del servidor y para el contenido de todos los usuarios significa que
un solo descuido lo abre todo.

DOS FORMAS DE CIFRAR, Y ES A PROPOSITO
--------------------------------------
* **Segmentos**: de una pieza. Son pequenos y siempre se leen enteros.
* **Multimedia**: en TROZOS independientes, cada uno con su nonce. AES-GCM no
  permite empezar a descifrar por la mitad, asi que sin trocear, servir el
  segundo 40 de un video exigiria descargar y descifrar el archivo entero.
  Con trozos, un rango de bytes se traduce a un rango de trozos.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
from dataclasses import dataclass
from typing import BinaryIO, Iterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: Version del formato de archivo. Va en la cabecera de cada uno: dar por
#: eterno un formato es como se pierde el acceso a los archivos viejos.
FORMAT_VERSION = 1
ENCRYPTION_VERSION = 1

#: Marca al principio del archivo, para reconocerlo sin depender del nombre.
MAGIC = b"WABK"

#: 96 bits es el nonce recomendado para GCM.
NONCE_BYTES = 12
KEY_BYTES = 32

#: Tamano de trozo en claro para multimedia. 1 MiB es el equilibrio entre
#: cuantos trozos hay que descargar para un rango pequeno y cuanto peso tiene
#: la etiqueta de autenticacion de cada uno (16 bytes).
CHUNK_PLAIN_BYTES = 1024 * 1024
TAG_BYTES = 16
CHUNK_STORED_BYTES = NONCE_BYTES + CHUNK_PLAIN_BYTES + TAG_BYTES


class EncryptionError(RuntimeError):
    """No se pudo cifrar o descifrar."""


class ClaveDeAlmacenamientoInvalida(EncryptionError):
    """Falta la KEK o no sirve."""


# ---------------------------------------------------------------------------
# Claves
# ---------------------------------------------------------------------------


def nueva_dek() -> bytes:
    """Una clave de contenido nueva. 256 bits del generador del sistema."""
    return os.urandom(KEY_BYTES)


def derivar_kek(app_encryption_key: str | None, storage_kek: str | None = None) -> bytes:
    """La clave que envuelve las DEK.

    Si hay ``STORAGE_KEK`` se usa esa. Si no, se DERIVA de
    ``APP_ENCRYPTION_KEY`` con HKDF y una etiqueta propia: derivada no es la
    misma clave, asi que quien obtenga una no obtiene la otra. Aun asi, tener
    las dos colgando de la misma raiz es una limitacion conocida, y por eso
    ``STORAGE_KEK`` existe.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    if storage_kek:
        clave = _decodificar_clave(storage_kek, "STORAGE_KEK")
        if len(clave) != KEY_BYTES:
            raise ClaveDeAlmacenamientoInvalida(
                "STORAGE_KEK tiene que ser de 32 bytes en base64 urlsafe."
            )
        return clave

    if not app_encryption_key:
        raise ClaveDeAlmacenamientoInvalida(
            "Falta APP_ENCRYPTION_KEY (o STORAGE_KEK). Sin ella no se puede "
            "proteger la clave de contenido de ningun usuario."
        )
    raiz = app_encryption_key.encode("utf-8")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=b"whatsapp-backup/storage/v1",
        # La etiqueta separa los dominios: la clave que sale de aqui no
        # coincide con la que cifra los tokens de Google.
        info=b"key-encryption-key",
    ).derive(raiz)


def envolver_dek(dek: bytes, kek: bytes) -> bytes:
    """Cifra la DEK con la KEK. Es lo unico que se guarda en la base."""
    if len(dek) != KEY_BYTES:
        raise EncryptionError("la DEK tiene que ser de 32 bytes")
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(kek).encrypt(nonce, dek, b"dek-v1")


def desenvolver_dek(envuelta: bytes, kek: bytes) -> bytes:
    try:
        nonce, cuerpo = envuelta[:NONCE_BYTES], envuelta[NONCE_BYTES:]
        return AESGCM(kek).decrypt(nonce, cuerpo, b"dek-v1")
    except (InvalidTag, ValueError) as exc:
        raise EncryptionError(
            "No se pudo abrir la clave de contenido. Si cambiaste "
            "APP_ENCRYPTION_KEY o STORAGE_KEK, los archivos ya subidos no se "
            "pueden leer con la clave nueva."
        ) from exc


def _decodificar_clave(valor: str, nombre: str) -> bytes:
    import base64

    try:
        return base64.urlsafe_b64decode(valor.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise ClaveDeAlmacenamientoInvalida(
            f"{nombre} no es base64 urlsafe valido."
        ) from exc


# ---------------------------------------------------------------------------
# Cabecera
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cabecera:
    """Lo que va en claro al principio del archivo.

    No lleva nada sensible: solo lo necesario para saber COMO leer el resto.
    Va autenticada (entra como AAD), asi que manipularla hace fallar el
    descifrado en vez de cambiar como se interpreta el archivo.
    """

    entity: str  # "message_segment" | "media"
    chunked: bool
    format_version: int = FORMAT_VERSION
    encryption_version: int = ENCRYPTION_VERSION
    compression: str = "none"
    plaintext_size: int | None = None

    def to_bytes(self) -> bytes:
        cuerpo = json.dumps(
            {
                "format_version": self.format_version,
                "encryption_version": self.encryption_version,
                "compression": self.compression,
                "entity": self.entity,
                "chunked": self.chunked,
                "plaintext_size": self.plaintext_size,
                "chunk_size": CHUNK_PLAIN_BYTES if self.chunked else None,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return MAGIC + struct.pack(">H", len(cuerpo)) + cuerpo

    @staticmethod
    def leer(datos: bytes) -> tuple["Cabecera", int]:
        """``(cabecera, bytes_consumidos)``."""
        if len(datos) < 6 or datos[:4] != MAGIC:
            raise EncryptionError("El archivo no tiene la marca esperada.")
        largo = struct.unpack(">H", datos[4:6])[0]
        fin = 6 + largo
        if len(datos) < fin:
            raise EncryptionError("Cabecera incompleta.")
        try:
            crudo = json.loads(datos[6:fin])
        except ValueError as exc:
            raise EncryptionError("Cabecera ilegible.") from exc
        if crudo.get("format_version") != FORMAT_VERSION:
            raise EncryptionError(
                f"Formato de archivo desconocido (version {crudo.get('format_version')}). "
                "Lo escribio una version posterior de la aplicacion."
            )
        return (
            Cabecera(
                entity=str(crudo.get("entity", "")),
                chunked=bool(crudo.get("chunked")),
                format_version=int(crudo["format_version"]),
                encryption_version=int(crudo.get("encryption_version", 1)),
                compression=str(crudo.get("compression", "none")),
                plaintext_size=crudo.get("plaintext_size"),
            ),
            fin,
        )


# ---------------------------------------------------------------------------
# De una pieza: segmentos
# ---------------------------------------------------------------------------


class StorageEncryption:
    """Cifra y descifra con la DEK de UN usuario.

    Se construye por usuario a proposito: no existe un objeto global capaz de
    leer el contenido de todos.
    """

    def __init__(self, dek: bytes) -> None:
        if len(dek) != KEY_BYTES:
            raise EncryptionError("la DEK tiene que ser de 32 bytes")
        self._aead = AESGCM(dek)

    # -- Una pieza ----------------------------------------------------------

    def encrypt_bytes(
        self, datos: bytes, *, entity: str, compression: str = "none"
    ) -> bytes:
        cabecera = Cabecera(
            entity=entity,
            chunked=False,
            compression=compression,
            plaintext_size=len(datos),
        ).to_bytes()
        nonce = os.urandom(NONCE_BYTES)
        # La cabecera entra como AAD: cambiarla invalida el archivo.
        return cabecera + nonce + self._aead.encrypt(nonce, datos, cabecera)

    def decrypt_bytes(self, datos: bytes) -> tuple[bytes, Cabecera]:
        cabecera, fin = Cabecera.leer(datos)
        if cabecera.chunked:
            raise EncryptionError(
                "Este archivo esta troceado; usa decrypt_range."
            )
        nonce = datos[fin : fin + NONCE_BYTES]
        try:
            claro = self._aead.decrypt(
                nonce, datos[fin + NONCE_BYTES :], datos[:fin]
            )
        except InvalidTag as exc:
            raise EncryptionError(
                "El archivo no supera la comprobacion de integridad: o esta "
                "corrupto o lo cifro otra clave."
            ) from exc
        return claro, cabecera

    # -- Troceado: multimedia -----------------------------------------------

    def encrypt_stream(
        self, origen: BinaryIO, *, entity: str = "media", plaintext_size: int | None = None
    ) -> Iterator[bytes]:
        """Cifra por trozos, leyendo poco a poco.

        Devuelve un iterador para poder subir un archivo de varios GB sin
        cargarlo en memoria: ``file.read()`` sobre un video de 5 GB reserva
        5 GB.
        """
        cabecera = Cabecera(
            entity=entity, chunked=True, plaintext_size=plaintext_size
        ).to_bytes()
        yield cabecera

        indice = 0
        while True:
            trozo = origen.read(CHUNK_PLAIN_BYTES)
            if not trozo:
                break
            yield self._cifrar_trozo(trozo, indice, cabecera)
            indice += 1

    def _cifrar_trozo(self, trozo: bytes, indice: int, cabecera: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        # El indice va en el AAD: sin el, reordenar o repetir trozos pasaria
        # desapercibido, porque cada uno se autentica por separado.
        aad = cabecera + struct.pack(">Q", indice)
        return nonce + self._aead.encrypt(nonce, trozo, aad)

    def decrypt_chunk(self, almacenado: bytes, indice: int, cabecera: bytes) -> bytes:
        nonce, cuerpo = almacenado[:NONCE_BYTES], almacenado[NONCE_BYTES:]
        aad = cabecera + struct.pack(">Q", indice)
        try:
            return self._aead.decrypt(nonce, cuerpo, aad)
        except InvalidTag as exc:
            raise EncryptionError(
                f"El trozo {indice} no supera la comprobacion de integridad."
            ) from exc


# ---------------------------------------------------------------------------
# Traduccion de rangos
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RangoCifrado:
    """Que hay que descargar de Drive para servir un rango en claro."""

    primer_trozo: int
    ultimo_trozo: int
    byte_inicial: int  # dentro del archivo cifrado
    byte_final: int  # inclusive
    recorte_inicial: int  # bytes a descartar del primer trozo descifrado
    longitud: int  # bytes en claro que se van a entregar


def rango_a_trozos(
    inicio: int, fin: int, *, cabecera_bytes: int, plaintext_size: int
) -> RangoCifrado:
    """Traduce ``bytes=inicio-fin`` a un rango del archivo cifrado.

    Es lo que hace posible servir el segundo 40 de un video sin descargar los
    2 GB: solo se piden los trozos que cubren esos bytes.
    """
    if inicio < 0 or fin < inicio or plaintext_size <= 0:
        raise ValueError("rango invalido")
    fin = min(fin, plaintext_size - 1)

    primero = inicio // CHUNK_PLAIN_BYTES
    ultimo = fin // CHUNK_PLAIN_BYTES
    return RangoCifrado(
        primer_trozo=primero,
        ultimo_trozo=ultimo,
        byte_inicial=cabecera_bytes + primero * CHUNK_STORED_BYTES,
        byte_final=cabecera_bytes + (ultimo + 1) * CHUNK_STORED_BYTES - 1,
        recorte_inicial=inicio - primero * CHUNK_PLAIN_BYTES,
        longitud=fin - inicio + 1,
    )


def tamano_cifrado(plaintext_size: int, cabecera_bytes: int) -> int:
    """Cuanto ocupara el archivo cifrado. Para comprobar tras subirlo."""
    if plaintext_size == 0:
        return cabecera_bytes
    completos, resto = divmod(plaintext_size, CHUNK_PLAIN_BYTES)
    total = cabecera_bytes + completos * CHUNK_STORED_BYTES
    if resto:
        total += NONCE_BYTES + resto + TAG_BYTES
    return total


# ---------------------------------------------------------------------------
# Huellas
# ---------------------------------------------------------------------------


def sha256_de(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def sha256_de_archivo(ruta, *, bloque: int = 1024 * 1024) -> str:
    """Huella de un archivo sin cargarlo entero en memoria."""
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        while trozo := f.read(bloque):
            h.update(trozo)
    return h.hexdigest()


def huellas_iguales(a: str | None, b: str | None) -> bool:
    """Comparacion en tiempo constante."""
    if not a or not b:
        return False
    return hmac.compare_digest(a, b)
