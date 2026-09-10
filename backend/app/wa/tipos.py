"""Los tipos que cruzan la costura de WhatsApp.

POR QUE EXISTEN AQUI
--------------------
Vivian dentro de ``app/whatsapp_client.py``, el modulo de pywhats, aunque no
tenian nada de pywhats: un evento con nombre y carga, un identificador de
conversacion, los datos para descifrar un adjunto. Al soltar aquella libreria
se quedaban sin casa.

Estan en su propio modulo para que ninguno de los dos lados --ni el proveedor,
ni la ingesta-- tenga que importar al otro para hablar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ClientEvent:
    """Un evento del proveedor de WhatsApp, camino de la aplicacion.

    ``name`` es uno de los de ``app/wa/port.py``. La lista es el contrato: los
    sinks y el traductor de SSE buscan por nombre, asi que cambiar uno rompe
    la ingesta sin que salte ningun error.
    """

    name: str
    payload: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JID:
    """Un identificador de WhatsApp, partido en sus dos mitades.

    El resto del codigo lee ``.user`` y ``.server``. Un ``@lid`` y un
    ``@s.whatsapp.net`` son espacios distintos y NO se convierten el uno en el
    otro: mezclarlos corrompe los datos.
    """

    user: str
    server: str = "s.whatsapp.net"
    device: int = 0

    def __str__(self) -> str:
        return f"{self.user}@{self.server}"


def jid_desde(texto: Any) -> JID | None:
    """``"573001234567@s.whatsapp.net"`` -> ``JID``. ``None`` si no hay."""
    if not texto:
        return None
    crudo = str(texto)
    usuario, _, servidor = crudo.partition("@")
    if not usuario:
        return None
    return JID(user=usuario, server=servidor or "s.whatsapp.net", device=0)


@dataclass
class Identidad:
    """La identidad propia. Lo que el codigo llama ``client.device``.

    Se le piden ``jid`` (con ``.user`` y ``.server``) y ``lid``, mas los dos
    datos con los que se calcula la HUELLA de la sesion.

    ``device_id`` es el numero de RANURA que asigna el servidor y se reutiliza
    al desvincular; por si solo no distingue dos vinculaciones. Por eso va
    acompanado de ``registration_id``, que se genera nuevo cada vez.
    """

    jid: JID | None = None
    lid: str | None = None
    device_id: str = ""
    registration_id: str = ""


@dataclass
class MediaInfo:
    """Lo que hace falta para descargar y VERIFICAR un adjunto.

    Los cinco campos salen del mensaje y se guardan en ``media_files``. Los dos
    hashes no son opcionales de hecho: son los que permiten comprobar que lo
    descargado es lo que decia ser, antes y despues de descifrarlo.

    ``raw_proto`` NO ES UN EXTRA
    ---------------------------
    Baileys no descarga a partir de ``direct_path`` y ``media_key`` sueltos:
    ``downloadMediaMessage`` recibe el ``WebMessageInfo`` entero, y ademas lo
    necesita para pedirle al telefono que resuba el adjunto cuando el CDN ya
    no lo sirve (``reuploadRequest``). Sin este campo la descarga no llega ni
    a empezar.

    Se deja opcional porque el resto del proyecto construye ``MediaInfo`` para
    describir un adjunto, no siempre para bajarlo.
    """

    direct_path: str
    media_key: bytes
    file_sha256: bytes
    file_enc_sha256: bytes
    #: El tipo CRIPTOGRAFICO ("image", "video", "audio", "document"), que no
    #: siempre coincide con el tipo del mensaje: un audio de voz y una musica
    #: se descifran igual.
    media_type: str
    #: El ``WebMessageInfo`` serializado, tal cual se guardo en
    #: ``messages.raw_proto``. Es lo que de verdad descarga el adjunto.
    raw_proto: bytes | None = None
