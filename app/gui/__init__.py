"""Interfaz Tkinter.

Es UN adaptador mas sobre los servicios, no el centro de la aplicacion. La API
Flask es otro. Ninguno de los dos habla con el otro: ambos van contra
``AppRuntime`` y los servicios.

``App`` vivia en ``app/gui.py``; ahora esta en :mod:`app.gui.app_window`, y se
reexporta aqui para que ``from app.gui import App`` siga funcionando.

Nada de este paquete puede importarse desde ``app/api``: la API no depende de
Tkinter, y hay una prueba que lo verifica.
"""

from __future__ import annotations

from app.gui.app_window import (
    ACCENT,
    App,
    BG,
    BORDER,
    ERROR,
    FG,
    MUTED,
    PairingView,
    StatusView,
    WARN,
    center,
    enable_dpi_awareness,
)

__all__ = [
    "ACCENT",
    "App",
    "BG",
    "BORDER",
    "ERROR",
    "FG",
    "MUTED",
    "PairingView",
    "StatusView",
    "WARN",
    "center",
    "enable_dpi_awareness",
]
