"""Bus de eventos compartido entre la ventana Tkinter y la API Flask.

Ninguno de los dos adaptadores conoce al otro: ambos escuchan aqui.
"""

from __future__ import annotations

from app.events.bus import Event, EventBus, Subscription

__all__ = ["Event", "EventBus", "Subscription"]
