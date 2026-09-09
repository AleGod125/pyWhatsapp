"""Un solo ``.env`` en la raiz para los dos proyectos del repositorio.

LA ESTRUCTURA
-------------
::

    pyWhatsapp/
      .env          <- compartido
      backend/      <- PROJECT_ROOT (la carpeta que contiene `app/`)
      frontend/

EL FALLO QUE FIJA ESTE ARCHIVO
------------------------------
``load_settings()`` miraba UNICAMENTE ``PROJECT_ROOT / ".env"``. Al pasar el
backend a su propia carpeta, ese sitio dejo de tener nada y no se cargaba
ninguna variable. No saltaba ningun error: la URL de PostgreSQL se construia
con los valores por defecto y quedaba **sin contrasena**::

    postgresql+psycopg://postgres@localhost:5432/whatsapp_backup

El fallo aparecia mucho despues, al conectar, y sin decir en ningun momento
que el problema era donde se estaba buscando el archivo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import PROJECT_ROOT, buscar_env, load_settings


def test_la_raiz_del_backend_es_la_que_contiene_app():
    """No se cuentan carpetas con `parent.parent`: se busca `app/`."""
    assert (PROJECT_ROOT / "app").is_dir()
    assert PROJECT_ROOT.name == "backend"


def test_se_encuentra_el_env_de_la_raiz_del_repositorio(tmp_path):
    """Subiendo, no mirando un solo sitio."""
    repo = tmp_path / "repo"
    backend = repo / "backend"
    backend.mkdir(parents=True)
    (repo / ".env").write_text("POSTGRES_PASSWORD=x\n", encoding="utf-8")

    assert buscar_env(backend) == repo / ".env"


def test_el_env_del_backend_gana_al_de_la_raiz(tmp_path):
    """Si alguien crea `backend/.env` es para pisar lo compartido."""
    repo = tmp_path / "repo"
    backend = repo / "backend"
    backend.mkdir(parents=True)
    (repo / ".env").write_text("POSTGRES_PASSWORD=raiz\n", encoding="utf-8")
    (backend / ".env").write_text("POSTGRES_PASSWORD=local\n", encoding="utf-8")

    assert buscar_env(backend) == backend / ".env"


def test_sin_ningun_env_se_devuelve_None_en_vez_de_inventar_una_ruta(tmp_path):
    solo = tmp_path / "vacio"
    solo.mkdir()

    assert buscar_env(solo) is None


def test_en_ESTE_repositorio_el_env_esta_en_la_raiz():
    """La comprobacion sobre el arbol de verdad, no sobre uno de mentira."""
    hallado = buscar_env()

    assert hallado is not None, "no se encuentra ningun .env"
    assert hallado == PROJECT_ROOT.parent / ".env"


def test_la_configuracion_cargada_TRAE_la_contrasena():
    """El sintoma exacto: una URL sin contrasena y ni un aviso.

    Se comprueba que la contrasena viaja, no cual es.
    """
    if buscar_env() is None:
        pytest.skip("esta maquina no tiene .env")

    url = str(load_settings().database_url)

    assert "@" in url, url
    usuario_y_clave = url.split("://", 1)[1].split("@", 1)[0]
    assert ":" in usuario_y_clave, (
        "la URL de PostgreSQL se construyo sin contrasena: no se cargo el .env"
    )


def test_las_rutas_siguen_ancladas_en_backend():
    """El `.env` es compartido; los DATOS no.

    `session/`, `data/` y `data/media/` son del backend y viven bajo su
    carpeta. Anclarlos en la raiz del repositorio los sacaria de donde estan
    -- y `session/` es la identidad de WhatsApp, que no se puede mover sin
    perder la vinculacion.
    """
    if buscar_env() is None:
        pytest.skip("esta maquina no tiene .env")

    ajustes = load_settings()

    for ruta in (ajustes.session_dir, ajustes.data_dir, ajustes.media_dir):
        assert PROJECT_ROOT in Path(ruta).parents or Path(ruta) == PROJECT_ROOT, ruta
