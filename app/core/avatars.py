"""Avatares: iniciales y color estable por contacto.

Vive en el nucleo, no en ``app/gui``, por un motivo concreto: la API tambien
los necesita, y ``app/gui/ui_theme`` importa ``tkinter.font``. Si la API sacara
el color de alli, arrastraria Tkinter a un proceso que no tiene ventana, y eso
esta prohibido (hay una prueba que lo verifica).

Al estar aqui, la ventana y la web pintan EL MISMO color para el mismo
contacto: son la misma aplicacion vista de dos maneras, no dos aplicaciones.
"""

from __future__ import annotations

# Paleta apagada. El color se elige por hash del identificador, asi que un
# contacto conserva el suyo entre sesiones y entre adaptadores.
AVATAR_COLORS = (
    "#6b8e9e", "#8a7f9e", "#9e7f7f", "#7f9e86",
    "#9e937f", "#7f8a9e", "#8e9e7f", "#9e7f95",
)


def avatar_color(key: str) -> str:
    """Color estable para un contacto. El mismo JID da siempre el mismo."""
    if not key:
        return AVATAR_COLORS[0]
    return AVATAR_COLORS[sum(key.encode()) % len(AVATAR_COLORS)]


def initials(name: str) -> str:
    """Una o dos iniciales para el avatar.

    Los signos de puntuacion iniciales se recortan en vez de descartar la
    palabra entera: un nombre de usuario como ``@AleBv_biker`` empieza por
    ``@`` y daba ``?``, que no identifica nada.
    """
    partes: list[str] = []
    for trozo in (name or "").replace("+", " ").split():
        limpio = trozo.lstrip("@#_-.·*(<[{\"'")
        if limpio and limpio[0].isalnum():
            partes.append(limpio)
    if not partes:
        return "?"
    if len(partes) == 1:
        return partes[0][:2].upper()
    return (partes[0][0] + partes[1][0]).upper()
