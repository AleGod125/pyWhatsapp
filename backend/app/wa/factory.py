"""Construir el cliente de WhatsApp. Un solo sitio en todo el proyecto.

POR QUE UNA FABRICA SI YA NO HAY QUE ELEGIR
-------------------------------------------
Hubo un interruptor ``WA_PROVIDER`` mientras convivian pywhats y Baileys. Ya
no: Baileys es el unico proveedor. La fabrica se queda porque hay DOS sitios
donde se construye el cliente --``AppRuntime.start`` y
``AppRuntime._crear_runtime``-- y que los dos pasen por aqui es lo que impide
que se desajusten. Ese desajuste solo se notaria a mitad de una
re-vinculacion, con la sesion ya abierta.
"""

from __future__ import annotations

from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("WA")


def crear_cliente_de_whatsapp(settings: Any, events: Any) -> Any:
    """El cliente de WhatsApp: un worker de Node sobre Baileys."""
    from app.wa.baileys_client import BaileysClient

    log.info("[WA] cliente de WhatsApp: Baileys (worker de Node)")
    return BaileysClient(settings, events)
