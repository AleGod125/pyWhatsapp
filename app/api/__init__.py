"""Adaptador HTTP. Uno mas, junto a la ventana Tkinter.

                   +-- app/gui    (Tkinter)
    AppRuntime ----+
                   +-- app/api    (Flask REST + SSE)

Los dos adaptadores no se conocen entre si. Ninguno llama al otro; ambos van
contra ``AppRuntime``, los servicios y el bus de eventos.

REGLA DURA
----------
Nada de este paquete importa ``tkinter``, ni widgets, ni ``root.after``, ni
toca frames. Hay una prueba que recorre el paquete y lo verifica: si algun dia
alguien mete un import de Tkinter aqui, la suite lo dice.
"""

from __future__ import annotations

from app.api.app_factory import create_app

__all__ = ["create_app"]
