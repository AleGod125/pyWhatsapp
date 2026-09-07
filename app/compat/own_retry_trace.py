"""Seguir un acuse de reintento propio hasta ver si el telefono contesta.

POR QUE HACE FALTA
------------------
La hipotesis «con el material publico en el primer acuse, el telefono
reenvia» quedo REFUTADA por una prueba real. El registro lo dice sin margen::

    20:54:10  no session for peer 865311***@lid
    20:54:10  send_frame len=510          <- acuse CON material
    20:54:10  ack->ok id=AC009BE9 class=receipt
    ...                                    <- y nada mas. Nunca.

Y otra vez 35 segundos despues, con el mismo final.

Lo que ese registro NO dice es igual de importante: ``ack->ok`` viene del
SERVIDOR aceptando nuestra stanza, no del telefono. No sabemos si le llego, si
la entendio, o si la descarto por como iba dirigida. Y no sabemos que
atributos traia la stanza original --``recipient``, ``participant``, ``type``--
porque nadie los ha mirado nunca.

Antes de tocar el protocolo otra vez hay que ver esos dos extremos. Esto los
enseña.

QUE SE APUNTA, Y QUE NO
-----------------------
Del acuse: a donde va, con que atributos, con que contador, y que
identificadores de clave lleva. **Identificadores y tamanos, nunca material**:
ni una clave, ni un byte de contenido, ni un texto descifrado.

De la respuesta: si en los segundos siguientes llega algo desde ese mismo
dispositivo, con que tipo de cifrado y cuanto tardo.

Es temporal y es de diagnostico: no cambia ni una decision.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("SIGNAL")

#: Cuanto se espera una respuesta antes de darla por no llegada. El telefono
#: que responde lo hace en menos de un segundo; treinta es holgura de sobra.
VENTANA_DE_RESPUESTA = 30.0

#: Acuses en vuelo que se recuerdan a la vez.
MAXIMO_EN_VUELO = 50


@dataclass
class AcuseEnVuelo:
    """Un acuse mandado del que aun no se sabe si sirvio."""

    wamid: str
    destino: str
    enviado_en: float = field(default_factory=time.monotonic)
    respondido: bool = False


_en_vuelo: dict[str, AcuseEnVuelo] = {}


def _direccion(valor: Any) -> str:
    """El dispositivo, en texto y sin el numero entero a la vista."""
    texto = str(valor or "?")
    usuario, _, servidor = texto.partition("@")
    if not servidor:
        return "?"
    visible = usuario[:6] + "***" if len(usuario) > 6 else usuario
    return f"{visible}@{servidor}"


def anotar_envio(
    *,
    wamid: Any,
    destino: Any,
    atributos_del_mensaje: Any,
    atributos_del_acuse: Any,
    intentos: int,
    material: Any,
) -> None:
    """Deja constancia del acuse que acaba de salir. Nunca lanza."""
    try:
        clave = str(wamid or "")
        if not clave:
            return
        if len(_en_vuelo) >= MAXIMO_EN_VUELO:
            _en_vuelo.pop(next(iter(_en_vuelo)))
        _en_vuelo[clave] = AcuseEnVuelo(wamid=clave, destino=_direccion(destino))

        # Los atributos de la stanza ORIGINAL. Es lo que nunca se habia
        # mirado, y de donde saldria un fallo de direccionamiento.
        entrantes = dict(getattr(atributos_del_mensaje, "keys", lambda: [])())
        if not entrantes:
            try:
                entrantes = {k: str(v) for k, v in dict(atributos_del_mensaje).items()}
            except Exception:  # noqa: BLE001
                entrantes = {}

        log.debug(
            "[OWN_RETRY] original_id=%s to=%s count=%d "
            "stanza_attrs=%s receipt_attrs=%s "
            "has_keys=%s registration=%s prekey_id=%s signed_prekey_id=%s "
            "device_identity=%s",
            clave[:12],
            _direccion(destino),
            intentos,
            sorted(entrantes.keys()),
            sorted(str(k) for k in (atributos_del_acuse or {})),
            material is not None,
            getattr(material, "registration_id", None),
            getattr(material, "opk_id", None),
            getattr(material, "spk_id", None),
            "yes" if getattr(material, "device_identity", None) else "no",
        )
    except Exception:  # noqa: BLE001 - el diagnostico no puede cortar el acuse
        log.debug("No se pudo anotar el acuse propio", exc_info=True)


def anotar_respuesta(*, sender: Any, enc_type: Any, wamid: Any = None) -> None:
    """Algo llego de ese dispositivo. Se mira si cierra algun acuse."""
    try:
        ahora = time.monotonic()
        destino = _direccion(sender)
        for clave, acuse in list(_en_vuelo.items()):
            if acuse.respondido:
                continue
            if ahora - acuse.enviado_en > VENTANA_DE_RESPUESTA:
                _en_vuelo.pop(clave, None)
                continue
            if acuse.destino != destino:
                continue
            acuse.respondido = True
            log.info(
                "[OWN_RETRY_RESPONSE] original=%s new_id=%s enc=%s sender=%s "
                "latencia=%.1fs",
                clave[:12],
                str(wamid or "?")[:12],
                enc_type,
                destino,
                ahora - acuse.enviado_en,
            )
            return
    except Exception:  # noqa: BLE001 - el diagnostico no puede cortar nada
        log.debug("No se pudo correlacionar la respuesta", exc_info=True)


def sin_respuesta() -> list[AcuseEnVuelo]:
    """Acuses cuya ventana vencio sin que llegara nada. Para el informe."""
    ahora = time.monotonic()
    return [
        a
        for a in _en_vuelo.values()
        if not a.respondido and (ahora - a.enviado_en) > VENTANA_DE_RESPUESTA
    ]


def olvidar_todo() -> None:
    """Para las pruebas."""
    _en_vuelo.clear()
