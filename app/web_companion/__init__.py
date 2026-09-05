"""Web Companion: un dispositivo vinculado MAS, solo para medir.

Ver :mod:`app.web_companion.supervisor` para el ciclo de vida y
:mod:`app.web_companion.probe` para la validacion de lo que devuelve.
"""

from __future__ import annotations

from app.web_companion.supervisor import (
    ESTADOS,
    WebCompanionNoDisponible,
    WebCompanionSupervisor,
)

__all__ = ["ESTADOS", "WebCompanionNoDisponible", "WebCompanionSupervisor"]
