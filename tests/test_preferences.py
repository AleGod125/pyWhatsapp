"""Las preferencias del usuario: tema, idioma, tipografía, alias.

QUE SE PROTEGE AQUI
-------------------
Dos cosas, y la segunda es la que importa de verdad.

**Una.** Que lo que llega por HTTP no se guarde tal cual. El cuerpo de la
petición lo escribe el navegador, y estos valores acaban en un `style` del
documento: aceptar texto libre sería dejar escribir CSS.

**Dos.** Que un usuario no pueda leer ni pisar las preferencias de otro. Es una
tabla de clave/valor compartida, así que el aislamiento lo da la clave — y una
clave mal construida no se nota hasta que dos personas usan la aplicación.
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("flask")

from app.api.preferences_routes import PREFIJO, _limpiar  # noqa: E402


# ---------------------------------------------------------------------------
# Lo que se acepta guardar
# ---------------------------------------------------------------------------


def test_un_tema_conocido_se_guarda():
    assert _limpiar({"theme": "midnight"})["theme"] == "midnight"


def test_un_tema_inventado_se_descarta():
    """Sin ruido: se ignora y se sigue. No es un error del usuario."""
    assert "theme" not in _limpiar({"theme": "purpurina"})


def test_un_idioma_que_no_hablamos_se_descarta():
    assert "language" not in _limpiar({"language": "klingon"})
    assert _limpiar({"language": "pt"})["language"] == "pt"


def test_la_escala_se_queda_dentro_de_lo_usable():
    """Una preferencia que rompe la pantalla no es una preferencia."""
    assert _limpiar({"font_scale": 99})["font_scale"] == 1.5
    assert _limpiar({"font_scale": 0.1})["font_scale"] == 0.9
    assert _limpiar({"font_scale": 1.25})["font_scale"] == 1.25


def test_solo_se_aceptan_colores_con_forma_de_color():
    """Esto acaba en un `style` del documento: no puede ser texto libre."""
    limpio = _limpiar(
        {
            "custom_theme": {
                "primary": "#49c899",
                "accent": "#abc",
                "background": "red; position: fixed",
                "text": "url(http://x)",
                "border": "#zzzzzz",
            }
        }
    )
    assert limpio["custom_theme"] == {"primary": "#49c899", "accent": "#abc"}


def test_una_fuente_arbitraria_no_pasa():
    """Solo fuentes que ya están en el sistema. Nada que haya que descargar."""
    assert "font_family" not in _limpiar({"font_family": "MiFuenteRara"})
    assert _limpiar({"font_family": "Georgia"})["font_family"] == "Georgia"


def test_lo_que_no_se_reconoce_no_se_guarda():
    """La petición no puede convertirse en un sitio donde meter cualquier cosa."""
    assert _limpiar({"loQueSea": {"grande": "x" * 10_000}}) == {}


def test_un_cuerpo_que_no_es_un_objeto_no_rompe_nada():
    assert _limpiar(None) == {}
    assert _limpiar("hola") == {}
    assert _limpiar([1, 2, 3]) == {}


# ---------------------------------------------------------------------------
# Alias de conversaciones
# ---------------------------------------------------------------------------


def test_un_alias_se_guarda_recortado():
    limpio = _limpiar({"chat_aliases": {"29910": "  Primo Juan  "}})
    assert limpio["chat_aliases"] == {"29910": "Primo Juan"}


def test_un_alias_vacio_significa_volver_al_nombre_original():
    """Se quita, no se guarda vacío: son la misma intención dicha de dos formas."""
    limpio = _limpiar({"chat_aliases": {"29910": "   "}})
    assert limpio["chat_aliases"] == {}


def test_un_alias_larguisimo_se_corta():
    limpio = _limpiar({"chat_aliases": {"1": "x" * 500}})
    assert len(limpio["chat_aliases"]["1"]) == 80


def test_los_alias_no_pueden_crecer_sin_limite():
    """Es una preferencia, no un almacén."""
    muchos = {str(i): f"nombre {i}" for i in range(5000)}
    assert len(_limpiar({"chat_aliases": muchos})["chat_aliases"]) <= 2000


# ---------------------------------------------------------------------------
# Contra la API de verdad
# ---------------------------------------------------------------------------


def test_de_entrada_no_hay_ninguna_preferencia(cliente):
    respuesta = cliente.get("/api/v1/preferences")
    assert respuesta.status_code == 200
    assert respuesta.get_json()["preferences"] == {}


def test_lo_guardado_se_vuelve_a_leer(cliente):
    cliente.put("/api/v1/preferences", json={"theme": "amoled", "language": "en"})

    guardadas = cliente.get("/api/v1/preferences").get_json()["preferences"]
    assert guardadas["theme"] == "amoled"
    assert guardadas["language"] == "en"


def test_guardar_FUSIONA_en_vez_de_reemplazar(cliente):
    """La pantalla de apariencia manda su trozo y la de idioma el suyo.

    Con reemplazo, la última en guardar borraría lo de la otra.
    """
    cliente.put("/api/v1/preferences", json={"theme": "light"})
    cliente.put("/api/v1/preferences", json={"font_scale": 1.25})

    guardadas = cliente.get("/api/v1/preferences").get_json()["preferences"]
    assert guardadas["theme"] == "light"
    assert guardadas["font_scale"] == 1.25


def test_restaurar_una_seccion_no_toca_las_demas(cliente):
    cliente.put(
        "/api/v1/preferences",
        json={"theme": "amoled", "language": "fr", "chat_aliases": {"1": "Ana"}},
    )

    cliente.delete("/api/v1/preferences?section=appearance")

    guardadas = cliente.get("/api/v1/preferences").get_json()["preferences"]
    assert "theme" not in guardadas
    assert guardadas["language"] == "fr"


def test_restaurar_la_apariencia_NO_borra_los_alias(cliente):
    """Los alias los escribió el usuario. Perderlos por cambiar de tema sería
    una sorpresa desagradable."""
    cliente.put(
        "/api/v1/preferences", json={"theme": "amoled", "chat_aliases": {"1": "Primo Juan"}}
    )

    cliente.delete("/api/v1/preferences?section=appearance")

    guardadas = cliente.get("/api/v1/preferences").get_json()["preferences"]
    assert guardadas["chat_aliases"] == {"1": "Primo Juan"}


def test_restaurar_todo_deja_las_preferencias_vacias(cliente):
    cliente.put("/api/v1/preferences", json={"theme": "amoled", "language": "de"})

    cliente.delete("/api/v1/preferences")

    assert cliente.get("/api/v1/preferences").get_json()["preferences"] == {}


def test_una_seccion_que_no_existe_se_dice(cliente):
    respuesta = cliente.delete("/api/v1/preferences?section=inventada")
    assert respuesta.status_code == 400
    assert respuesta.get_json()["error"]["code"] == "UNKNOWN_SECTION"


def test_un_anonimo_no_llega_a_las_preferencias(runtime):
    """La API entera exige sesión, y esto no es una excepción."""
    from app.api import create_app

    aplicacion = create_app(runtime)
    aplicacion.config.update(TESTING=True)
    sin_cookie = aplicacion.test_client()

    assert sin_cookie.get("/api/v1/preferences").status_code in (401, 403)


# ---------------------------------------------------------------------------
# Aislamiento entre usuarios
# ---------------------------------------------------------------------------


def test_cada_usuario_tiene_su_propia_clave():
    """Es una tabla compartida: el aislamiento lo da la clave.

    Si dos usuarios pudieran compartirla, uno vería —y pisaría— el tema y los
    alias del otro. Los alias llevan nombres que el usuario escribió sobre sus
    contactos.
    """
    from app.api.preferences_routes import _clave

    assert _clave(1) != _clave(2)
    assert _clave(1).startswith(PREFIJO)
    assert _clave(1).endswith("1")


def test_las_preferencias_de_otro_usuario_no_se_ven(cliente, runtime, session):
    """Se escriben a mano las de otro y no aparecen por ninguna parte."""
    from app.api.preferences_routes import _clave
    from app.services import repository as repo

    with runtime.database.transaction() as sesion:
        repo.set_app_state(sesion, _clave(999999), {"theme": "midnight"})

    cliente.put("/api/v1/preferences", json={"theme": "light"})
    mias = cliente.get("/api/v1/preferences").get_json()["preferences"]

    assert mias["theme"] == "light", "las mias, no las suyas"

    with runtime.database.transaction() as sesion:
        suyas = repo.get_app_state(sesion, _clave(999999))
    assert suyas == {"theme": "midnight"}, "y las suyas siguen intactas"


def test_no_hizo_falta_ninguna_migracion():
    """Viven en `app_state`, que ya existía y ya era JSONB.

    Una tabla propia tendría sentido si hubiera que consultar por dentro
    —«todos los que usan el tema oscuro»—, y no hay ningún caso así.
    """
    from app.models import AppState

    columnas = {c.name for c in AppState.__table__.columns}
    assert {"key", "value"} <= columnas
