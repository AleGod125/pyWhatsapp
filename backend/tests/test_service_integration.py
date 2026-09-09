"""Que `service.py` y las pruebas construyan lo MISMO.

Aqui se comprobaba, ademas, que nuestra siembra del par PN<->LID y el
Signal Store de pywhats hablaran del mismo fichero SQLite. Esa
integracion desaparecio con la libreria: Baileys lleva su propio almacen
y resuelve la equivalencia LID<->numero por dentro.

Lo que queda es la garantia que sigue importando: que el arranque real y
el de las pruebas pasen por la misma fabrica, para que no se pruebe una
cosa y se ejecute otra.
"""

from __future__ import annotations

import pytest


def test_service_py_y_las_pruebas_usan_la_misma_fabrica():
    """Si el cableado se duplicara, una prueba podria validar otro producto."""
    import ast
    from pathlib import Path

    arbol = ast.parse(Path("service.py").read_text(encoding="utf-8"))
    llamadas = [
        getattr(n.func, "id", None) or getattr(n.func, "attr", None)
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
    ]
    assert "build_service_runtime" in llamadas
    assert "build_service_app" in llamadas
    assert "AppRuntime" not in llamadas, "service.py no construye el runtime a mano"


def test_la_fabrica_de_servicio_deja_la_base_lista(settings, tmp_path):
    """El mismo runtime que sirve la API, sin abrir la sesion de WhatsApp."""
    import dataclasses

    from app.core.runtime import build_service_app, build_service_runtime

    aislado = dataclasses.replace(
        settings,
        session_dir=tmp_path / "session",
        diagnostics_dir=tmp_path / "diagnostics",
    )
    runtime = build_service_runtime(aislado, configure_logging=False)
    try:
        assert runtime.owner == "service.py"
        assert runtime.database is not None
        assert runtime.info().whatsapp_enabled is False, (
            "la fabrica NO abre la sesion: eso lo hace start(), en su hilo"
        )

        aplicacion = build_service_app(runtime)
        aplicacion.config.update(TESTING=True)
        cliente = aplicacion.test_client()

        salud = cliente.get("/api/v1/health").get_json()
        assert salud["owner"] == "service.py"
        assert salud["database"] is True
    finally:
        runtime.stop()


