"""Paleta y tipografia de la interfaz, en un solo sitio.

Antes cada vista definia sus propios ``#ffffff`` y sus propias ``tkfont.Font``,
asi que el sidebar y la conversacion no se parecian del todo y cambiar un
color obligaba a tocar tres archivos.

Criterios (secciones 25 a 28):

* Inspirado en WhatsApp Desktop, no copiado. Verde muy suave para lo propio,
  gris claro para lo recibido, fondos casi blancos.
* Nada saturado. Los unicos colores vivos son el acento y el rojo de error, y
  se usan con cuentagotas.
* Segoe UI en Windows, con alternativas reales en el resto: pedirle a Tk una
  fuente que no existe no falla, se queda en la del sistema y la jerarquia de
  tamanos se pierde.
"""

from __future__ import annotations

import sys
import tkinter.font as tkfont
from typing import Any

# ---------------------------------------------------------------------------
# Colores
# ---------------------------------------------------------------------------

# Fondos
BG = "#ffffff"                # panel de conversacion
CHAT_BG = "#f3f4f6"           # lienzo del hilo, apenas mas oscuro que la burbuja
SIDEBAR_BG = "#f7f8fa"
HEADER_BG = "#ffffff"
STATUS_BG = "#f2f3f5"

# Texto
FG = "#111b21"                # principal
MUTED = "#667781"             # secundario
FAINT = "#9aa5ab"             # terciario (contadores, metadatos)

# Lineas
BORDER = "#e6e9eb"
DIVIDER = "#eceff1"

# Burbujas
BUBBLE_MINE = "#dcf8c6"       # verde muy suave
BUBBLE_THEIRS = "#ffffff"
BUBBLE_BORDER = "#e4e7e9"

# Seleccion y acentos
SELECTED = "#e7f1ea"          # verde azulado muy suave
HOVER = "#eef0f2"
ACCENT = "#128c7e"            # verde WhatsApp apagado
LINK = "#1d7bd6"
WARN = "#d97706"
ERROR = "#dc2626"
OK = "#16a34a"

# Separadores de dia y tarjetas centrales de sistema
DAY_BG = "#e8ebed"
SYSTEM_BG = "#eef1f3"

# Avatares: paleta apagada, se elige por hash del JID para que un contacto
# tenga siempre el mismo color.
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
    """Una o dos iniciales para el avatar."""
    partes = [p for p in name.replace("+", " ").split() if p and p[0].isalnum()]
    if not partes:
        return "?"
    if len(partes) == 1:
        return partes[0][:2].upper()
    return (partes[0][0] + partes[1][0]).upper()


# ---------------------------------------------------------------------------
# Tipografia
# ---------------------------------------------------------------------------

# En Windows Segoe UI esta siempre; fuera se usa lo que haya. Que la lista
# tenga alternativas de verdad importa: Tk no avisa si la familia no existe.
_FAMILIES = {
    "win32": "Segoe UI",
    "darwin": "SF Pro Text",
}
FONT_FAMILY = _FAMILIES.get(sys.platform, "DejaVu Sans")

# Jerarquia de la seccion 28.
SIZE_TITLE = 12       # nombre en la cabecera
SIZE_NAME = 11        # nombre de contacto en el sidebar
SIZE_BODY = 10        # texto del mensaje
SIZE_META = 9         # metadatos
SIZE_SMALL = 8        # hora, contadores


class Fonts:
    """Fuentes compartidas. Se crean una vez, tras existir el ``root``.

    Tkinter exige que exista una ventana antes de instanciar una ``Font``, asi
    que no pueden ser constantes de modulo.
    """

    def __init__(self) -> None:
        def make(size: int, weight: str = "normal", slant: str = "roman") -> Any:
            return tkfont.Font(
                family=FONT_FAMILY, size=size, weight=weight, slant=slant
            )

        self.title = make(SIZE_TITLE, "bold")
        self.name = make(SIZE_NAME, "bold")
        self.body = make(SIZE_BODY)
        self.body_bold = make(SIZE_BODY, "bold")
        self.meta = make(SIZE_META)
        self.small = make(SIZE_SMALL)
        self.small_bold = make(SIZE_SMALL, "bold")
        self.avatar = make(SIZE_NAME, "bold")

    def measure_body(self, text: str) -> int:
        return self.body.measure(text)


_FONTS: Fonts | None = None


def fonts() -> Fonts:
    """Instancia compartida, ligada al interprete Tcl que este vivo.

    Una ``Font`` pertenece al interprete que la creo. Si esa ventana se
    destruye (cerrar y volver a abrir, o varias pruebas seguidas) la fuente
    cacheada queda huerfana y cualquier uso lanza "application has been
    destroyed". Por eso se comprueba antes de devolverla y se reconstruye si
    hace falta, en vez de confiar en que solo habra un ``root`` en la vida
    del proceso.
    """
    global _FONTS
    if _FONTS is not None:
        try:
            _FONTS.body.measure("x")
            return _FONTS
        except Exception:  # noqa: BLE001 - TclError y derivados
            _FONTS = None
    _FONTS = Fonts()
    return _FONTS


def reset_fonts() -> None:
    """Olvida las fuentes. Solo para las pruebas, que abren varios ``root``."""
    global _FONTS
    _FONTS = None
