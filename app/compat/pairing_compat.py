"""Esperar el restart 515 antes de cerrar el socket de registro.

COMPORTAMIENTO VERIFICADO (pywhats 0.2.0):

``Pairer.run`` (``pywhats/pairing.py:476``) retorna el ``PairResult`` justo
despues de ``_reply_pair_success``, sin leer nada mas del transporte. Acto
seguido ``Client._run_pairing`` (``pywhats/client.py:167-177``) guarda el
device y ejecuta ``await sock.disconnect()`` en su bloque ``finally``, con este
comentario en el propio codigo::

    # The server sends <stream:error code="515"/> right after a
    # successful pair-success -- that's the signal to reconnect with
    # the login payload. Close the registration socket either way.

Es decir: pywhats SABE que llega el 515 pero cierra el socket sin esperarlo. En
el flujo observado, el telefono finaliza la vinculacion en esa ventana:

    pair-success -> ADV identity -> pair-device-sign -> stream:error 515
    -> guardar sesion -> reconectar -> login -> connected

Este parche mantiene el socket vivo tras ``pair-device-sign`` hasta que llega
el restart o vence un plazo corto, y REGISTRA lo que llega de verdad.

Puntos importantes:

* El 515 posterior al pairing NO es un error fatal: es el restart esperado.
  No se propaga como excepcion.
* Un timeout tampoco es fatal. Si el restart no llega, se continua igual y se
  deja constancia en el log; el flujo de login lo dira.
* No se reintenta el pairing en bucle. Se espera una vez y se sigue.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("PAIRING")

_MARKER = "_whatsapp_backup_pairing_patch"

# Ultimo restart observado, para que el cliente pueda informar sin re-parsear.
last_restart: dict[str, Any] | None = None


async def wait_for_restart(transport: Any, *, timeout: float) -> dict[str, Any] | None:
    """Lee del transporte hasta ver un ``<stream:error>`` o agotar el plazo.

    Devuelve los atributos del stanza de restart, o ``None`` si no llego.
    Nunca lanza: cualquier fallo de transporte se traduce en ``None`` porque
    en este punto el pairing YA se completo y no debe invalidarse por esto.
    """
    from pywhats.binary import decode

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            log.warning(
                "No llego el restart 515 en %.0fs tras pair-device-sign; se continua igual",
                timeout,
            )
            return None
        try:
            frame = await asyncio.wait_for(transport.recv(), timeout=remaining)
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("Timeout esperando el restart 515; se continua igual")
            return None
        except Exception as exc:  # noqa: BLE001 - el transporte puede cerrarse solo
            log.info("El transporte se cerro esperando el restart (%s); se continua", exc)
            return None

        try:
            node = decode(frame)
        except Exception:  # noqa: BLE001 - un frame ilegible no invalida el pairing
            log.debug("Frame no decodificable mientras se esperaba el restart")
            continue

        if node.tag != "stream:error":
            log.debug("Stanza <%s> mientras se esperaba el restart", node.tag)
            continue

        attrs = dict(node.attrs)
        code = attrs.get("code")
        children = [child.tag for child in node.get_children()]
        if code == "515":
            log.info("Restart 515 recibido (el esperado tras el pairing) attrs=%s", attrs)
        else:
            # No es el restart. Se informa sin ocultarlo ni tratarlo como fatal
            # aqui: quien decide es la capa de cliente.
            log.warning(
                "stream:error inesperado durante el pairing code=%s attrs=%s children=%s",
                code,
                attrs,
                children,
            )
        return {"code": code, "attrs": attrs, "children": children}


def apply(*, timeout: float = 20.0) -> bool:
    """Envuelve ``Pairer.run`` para esperar el restart. Idempotente."""
    from pywhats.pairing import Pairer

    original = Pairer.run
    if getattr(original, _MARKER, False):
        return True

    async def run(self: Any, on_qr: Any) -> Any:
        global last_restart

        result = await original(self, on_qr)
        log.info("pair-device-sign enviado; esperando el restart del servidor...")
        # Si el transporte ya no permite leer, no se insiste.
        with contextlib.suppress(Exception):
            last_restart = await wait_for_restart(self.transport, timeout=timeout)
        return result

    setattr(run, _MARKER, True)
    Pairer.run = run  # type: ignore[method-assign]

    log.info("Adaptacion de espera del restart 515 aplicada (timeout=%.0fs)", timeout)
    return True
