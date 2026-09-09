"""Las preferencias del usuario: tema, idioma, tipografía, alias de chats.

POR QUE EN EL SERVIDOR Y NO SOLO EN EL NAVEGADOR
-----------------------------------------------
`localStorage` sirve para que la pantalla se pinte al instante con lo que ya
sabía, y para eso se sigue usando. Pero es del navegador, no del usuario: se
pierde al limpiar datos, no viaja a otro equipo y no sobrevive a un modo
privado. Las preferencias son del usuario, así que la fuente es PostgreSQL.

DONDE SE GUARDAN, Y POR QUE AHI
-------------------------------
En ``app_state``, que ya es una tabla de clave/valor con JSONB, bajo la clave
``user_prefs:<user_id>``. **No hace falta ninguna migración**: la tabla existe,
el tipo es el adecuado y el aislamiento entre usuarios lo da la propia clave.

Crear una tabla para esto tendría sentido si hubiera que consultar por dentro
—«todos los que usan el tema oscuro»—, y no hay ningún caso así. Cuando lo
haya, se migra.

LO QUE NO SE HACE AQUI
----------------------
Un alias de conversación **no** toca `chats.name` ni `contacts.display_name`.
Se guarda aparte, con el resto de preferencias, y la pantalla lo aplica al
mostrar. Sobrescribir el nombre de origen haría imposible volver a él, y el
nombre de origen es un dato de WhatsApp, no nuestro.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

from app.auth.web import requiere_sesion, usuario_actual
from app.core.logging_setup import get_logger

log = get_logger("API")

preferences = Blueprint("preferences", __name__)

#: Prefijo de la clave. El identificador del usuario va detrás, y eso es lo
#: que impide que uno lea las de otro: no hay consulta que pueda cruzarlos.
PREFIJO = "user_prefs:"

#: Lo que se acepta guardar, y qué valores valen. Todo lo que no esté aquí se
#: descarta en silencio: el cuerpo de la petición lo escribe el navegador, y
#: no puede convertirse en un sitio donde meter cualquier cosa.
TEMAS = {"system", "light", "dark", "amoled", "midnight", "custom"}
IDIOMAS = {"auto", "es", "en", "pt", "fr", "de", "it"}
DENSIDADES = {"compact", "cozy", "roomy"}
FUENTES = {
    "system",
    "Inter",
    "Roboto",
    "Poppins",
    "Montserrat",
    "Nunito",
    "Arial",
    "Georgia",
}
INTERLINEADOS = {"tight", "normal", "relaxed"}

#: Escala del texto. Fuera de este rango la interfaz deja de ser usable, y una
#: preferencia que rompe la pantalla no es una preferencia.
ESCALA_MINIMA = 0.9
ESCALA_MAXIMA = 1.5

#: Tope de alias. Es una preferencia, no un almacén: sin límite, el cuerpo de
#: la petición podría crecer sin control.
MAX_ALIAS = 2000
MAX_LARGO_DE_ALIAS = 80


def _clave(user_id: Any) -> str:
    return f"{PREFIJO}{user_id}"


def _color(valor: Any) -> str | None:
    """Un color hexadecimal, o nada.

    Se valida la FORMA, no el gusto: lo que llega va a un `style` del
    documento, así que aceptar texto libre sería dejar escribir CSS.
    """
    if not isinstance(valor, str):
        return None
    texto = valor.strip()
    if len(texto) not in (4, 7) or not texto.startswith("#"):
        return None
    return texto if all(c in "0123456789abcdefABCDEF" for c in texto[1:]) else None


def _limpiar(crudo: Any) -> dict[str, Any]:
    """Deja SOLO lo que se reconoce, con la forma que se espera."""
    if not isinstance(crudo, dict):
        return {}

    limpio: dict[str, Any] = {}

    def texto_de(clave: str, permitidos: set[str]) -> None:
        valor = crudo.get(clave)
        if isinstance(valor, str) and valor in permitidos:
            limpio[clave] = valor

    texto_de("theme", TEMAS)
    texto_de("language", IDIOMAS)
    texto_de("density", DENSIDADES)
    texto_de("font_family", FUENTES)
    texto_de("line_height", INTERLINEADOS)

    escala = crudo.get("font_scale")
    if isinstance(escala, (int, float)):
        limpio["font_scale"] = max(ESCALA_MINIMA, min(ESCALA_MAXIMA, float(escala)))

    for bandera in ("high_contrast", "reduce_motion", "focus_visible"):
        if isinstance(crudo.get(bandera), bool):
            limpio[bandera] = crudo[bandera]

    colores = crudo.get("custom_theme")
    if isinstance(colores, dict):
        validos = {
            nombre: color
            for nombre, valor in colores.items()
            if isinstance(nombre, str) and (color := _color(valor))
        }
        if validos:
            limpio["custom_theme"] = validos

    # -- Alias de conversaciones -------------------------------------------
    alias = crudo.get("chat_aliases")
    if isinstance(alias, dict):
        limpios: dict[str, str] = {}
        for identificador, nombre in list(alias.items())[:MAX_ALIAS]:
            if not isinstance(identificador, str) or not isinstance(nombre, str):
                continue
            recortado = nombre.strip()[:MAX_LARGO_DE_ALIAS]
            # Un alias vacío significa «vuelve al nombre original», y eso se
            # expresa quitándolo, no guardando una cadena vacía.
            if recortado:
                limpios[identificador] = recortado
        limpio["chat_aliases"] = limpios

    disposicion = crudo.get("dashboard")
    if isinstance(disposicion, dict):
        orden = disposicion.get("order")
        ocultos = disposicion.get("hidden")
        guardado: dict[str, Any] = {}
        if isinstance(orden, list):
            guardado["order"] = [str(x)[:64] for x in orden[:50]]
        if isinstance(ocultos, list):
            guardado["hidden"] = [str(x)[:64] for x in ocultos[:50]]
        if guardado:
            limpio["dashboard"] = guardado

    return limpio


def _leer(sesion: Any, user_id: Any) -> dict[str, Any]:
    from app.services import repository as repo

    guardado = repo.get_app_state(sesion, _clave(user_id))
    return guardado if isinstance(guardado, dict) else {}


@preferences.get("/preferences")
@requiere_sesion
def obtener():
    """Las preferencias del usuario que pregunta. Nunca las de otro."""
    from app.api.routes import runtime

    yo = usuario_actual()
    if yo is None:
        return jsonify({"preferences": {}})

    rt = runtime()
    with rt.database.transaction() as sesion:
        return jsonify({"preferences": _leer(sesion, yo.id)})


@preferences.put("/preferences")
@requiere_sesion
def guardar():
    """Fusiona lo que llegue con lo que había. NO reemplaza el conjunto.

    Fusionar y no reemplazar importa: la pantalla de apariencia manda su
    trozo, la de idioma el suyo, y con reemplazo la última en guardar borraría
    lo de la otra.
    """
    from app.api.routes import runtime
    from app.services import repository as repo

    yo = usuario_actual()
    if yo is None:
        return jsonify({"preferences": {}})

    entrante = _limpiar(request.get_json(silent=True) or {})
    rt = runtime()
    with rt.database.transaction() as sesion:
        actuales = dict(_leer(sesion, yo.id))
        actuales.update(entrante)
        repo.set_app_state(sesion, _clave(yo.id), actuales)
    return jsonify({"preferences": actuales})


@preferences.delete("/preferences")
@requiere_sesion
def restaurar():
    """Vuelve a los valores predeterminados.

    Se puede restaurar una sección concreta —``?section=appearance``— o todo.
    Los alias NO entran en ninguna sección de apariencia: son datos que el
    usuario escribió, y perderlos por restaurar un tema sería una sorpresa
    desagradable. Sólo se van con ``section=chats``.
    """
    from app.api.routes import runtime
    from app.services import repository as repo

    yo = usuario_actual()
    if yo is None:
        return jsonify({"preferences": {}})

    secciones: dict[str, tuple[str, ...]] = {
        "appearance": ("theme", "custom_theme"),
        "typography": ("font_family", "font_scale", "density", "line_height"),
        "accessibility": ("high_contrast", "reduce_motion", "focus_visible"),
        "dashboard": ("dashboard",),
        "chats": ("chat_aliases",),
        "language": ("language",),
    }
    seccion = request.args.get("section")

    rt = runtime()
    with rt.database.transaction() as sesion:
        if seccion is None:
            repo.set_app_state(sesion, _clave(yo.id), {})
            return jsonify({"preferences": {}})
        if seccion not in secciones:
            return (
                jsonify(
                    {
                        "error": {
                            "code": "UNKNOWN_SECTION",
                            "message": f"No existe la seccion '{seccion}'.",
                        }
                    }
                ),
                400,
            )
        actuales = dict(_leer(sesion, yo.id))
        for clave in secciones[seccion]:
            actuales.pop(clave, None)
        repo.set_app_state(sesion, _clave(yo.id), actuales)
    return jsonify({"preferences": actuales})
