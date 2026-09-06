"""El bloque ``<keys>`` del acuse de reintento. Sólo material público.

POR QUE HACE FALTA, Y POR QUE HASTA AHORA NO
--------------------------------------------
Con la sesión LID retirada, la copia propia ya no falla el MAC: falla por «no
hay sesión». Eso es el estado correcto, y el acuse de reintento sale::

    17:47:53.861  no session for peer <mi LID>
    17:47:53.861  sent retry receipt
    17:47:54.114  no session (mismo WAMID)  ->  sent retry receipt

Pero el teléfono **no reenvía nada como ``pkmsg``**. Y es coherente: el acuse
que mandábamos es el mínimo —``<retry>`` y ``<registration>``— y no lleva con
qué rehacer el saludo. El emisor tendría que ir al servidor a buscar nuestro
paquete de claves, y aquí no lo hace.

Baileys y whatsmeow mandan, a partir del segundo intento, un bloque
``<keys>`` con **nuestro material público** para que el otro extremo pueda
hacer X3DH sin preguntarle a nadie.

LA FORMA, SACADA DE ESTE MISMO PROYECTO
---------------------------------------
No se ha reconstruido de memoria. ``pywhats/messaging/prekey.py`` ya publica
nuestras claves con la forma de whatsmeow ``preKeyToNode``, y de ahí sale la
convención exacta que se usa aquí::

    <registration>  4 bytes big-endian
    <type>          UN byte: 0x05, el tipo de curva
    <identity>      32 bytes CRUDOS, sin prefijo
    <key>           <id> 3 bytes BE · <value> 32 bytes crudos
    <skey>          <id> · <value> · <signature> de 64 bytes

El detalle que más fácil sería equivocar es el prefijo ``0x05``: **no** va
pegado a los valores; va en su propio ``<type>``. Está así en el nodo de
subida de este repositorio, verificado, no supuesto.

QUE VIAJA, Y QUE NO
-------------------
Viaja sólo lo que es público por diseño y el servidor ya tiene: identificador
de registro, clave de identidad pública, la prefirmada con su firma, una clave
de un solo uso pública, y la identidad de dispositivo que firmó el
emparejamiento.

**Nunca** viaja una clave privada, ni estado de ratchet, ni nada de la sesión.
Hay una prueba que lo comprueba campo por campo.

LA CLAVE DE UN SOLO USO
-----------------------
Se ofrece una que **ya existe** en nuestro almacén, con su mitad privada
guardada. No se genera ninguna nueva y no se inventa ningún identificador: son
las que el propio pywhats generó y subió. Cuando llegue el ``pkmsg`` que la
use, el camino de siempre la encuentra y la consume.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("SIGNAL")

#: El byte de tipo de curva. Va SOLO, en su propio nodo.
TIPO_DE_CURVA = b"\x05"

#: A partir de qué intento se adjunta el material.
#:
#: Dos, que es lo que hacen Baileys y whatsmeow: el primer acuse es la
#: petición simple, y sólo si aquélla no bastó se manda con qué rehacer el
#: saludo. Mandarlo siempre sería regalar una clave de un solo uso en cada
#: mensaje que llegue desordenado.
INTENTO_DESDE_EL_QUE_SE_ADJUNTA = 2


@dataclass
class MaterialPublico:
    """Lo que se puede poner en el cable. Todo público, por diseño."""

    registration_id: int
    identity_public: bytes
    spk_id: int
    spk_public: bytes
    spk_signature: bytes
    opk_id: int | None
    opk_public: bytes | None
    device_identity: bytes | None

    def completo(self) -> bool:
        """Sin identidad o sin prefirmada no hay saludo posible."""
        return bool(
            self.identity_public
            and self.spk_public
            and self.spk_signature
            and self.registration_id
        )


def _id3(key_id: int) -> bytes:
    """3 bytes big-endian, como ``preKeyToNode``: los bajos de un id de 4."""
    return int(key_id).to_bytes(4, "big")[1:]


def _una_clave_de_un_solo_uso(store: Path) -> tuple[int, bytes] | None:
    """Una de las nuestras que siga sin usarse, con su mitad pública.

    Se lee del almacén y no se genera nada. Las consumidas se borran de esa
    tabla al usarse, así que cualquiera que siga ahí es ofrecible.
    """
    try:
        conexion = sqlite3.connect(f"file:{store.as_posix()}?mode=ro", uri=True, timeout=3.0)
    except sqlite3.Error:
        return None
    try:
        fila = conexion.execute(
            "SELECT key_id, public FROM prekeys ORDER BY key_id LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conexion.close()
    if not fila or fila[1] is None:
        return None
    return int(fila[0]), bytes(fila[1])


def material_de(settings: Any) -> MaterialPublico | None:
    """Reúne el material público desde lo que ya persistió el emparejamiento.

    Se lee del ``DeviceStore``, que es donde vive, en vez de hurgar en objetos
    internos del receptor: son los mismos datos y esto no depende de detalles
    privados de la librería.
    """
    try:
        from pywhats.store import DeviceStore

        device = DeviceStore.load(settings.session_file)
    except Exception:  # noqa: BLE001 - sin device store no hay acuse con claves
        log.debug("No se pudo leer el DeviceStore para el acuse con claves")
        return None

    opk = _una_clave_de_un_solo_uso(Path(settings.signal_store_file))
    material = MaterialPublico(
        registration_id=int(getattr(device, "registration_id", 0) or 0),
        identity_public=bytes(getattr(device, "identity_public", b"") or b""),
        spk_id=int(getattr(device, "signed_pre_key_id", 0) or 0),
        spk_public=bytes(getattr(device, "signed_pre_key_public", b"") or b""),
        spk_signature=bytes(getattr(device, "signed_pre_key_signature", b"") or b""),
        opk_id=opk[0] if opk else None,
        opk_public=opk[1] if opk else None,
        device_identity=getattr(device, "adv_signed_device_identity", None),
    )
    return material if material.completo() else None


def bloque_de_claves(material: MaterialPublico) -> Any | None:
    """El nodo ``<keys>``, con la forma de ``preKeyToNode``.

    Los valores van CRUDOS. El ``0x05`` va aparte, en ``<type>``: pegarlo a
    los valores es el error fácil de este bloque, y aquí se evita porque la
    forma sale del nodo de subida de este mismo repositorio.
    """
    try:
        from pywhats.binary.node import Node
    except Exception:  # noqa: BLE001
        return None

    hijos = [
        Node(tag="type", content=TIPO_DE_CURVA),
        Node(tag="identity", content=material.identity_public),
    ]
    if material.opk_id is not None and material.opk_public:
        hijos.append(
            Node(
                tag="key",
                content=[
                    Node(tag="id", content=_id3(material.opk_id)),
                    Node(tag="value", content=material.opk_public),
                ],
            )
        )
    hijos.append(
        Node(
            tag="skey",
            content=[
                Node(tag="id", content=_id3(material.spk_id)),
                Node(tag="value", content=material.spk_public),
                Node(tag="signature", content=material.spk_signature),
            ],
        )
    )
    if material.device_identity:
        hijos.append(Node(tag="device-identity", content=material.device_identity))
    return Node(tag="keys", content=hijos)
