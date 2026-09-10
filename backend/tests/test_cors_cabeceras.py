"""Toda cabecera propia del cliente tiene que estar permitida por CORS.

EL FALLO, Y POR QUE NO SE PARECE A UN FALLO DE CORS
---------------------------------------------------
El frontend empezo a mandar ``X-WhatsApp-Account`` en cada peticion --de que
cuenta de WhatsApp habla-- y no se anadio a ``allow_headers``.

El navegador rechazo el preflight y bloqueo TODAS las peticiones **antes de
enviarlas**. Lo que se vio:

* en el log del backend, nada. Ni un 400, ni un 403, ni una linea de acceso:
  las peticiones no llegaron nunca;
* en la pantalla, "No se pudo preparar la cuenta nueva" al agregar una cuenta;
* y peor: como fallaba tambien ``/session`` y ``/chats``, la aplicacion se
  comporto como si no hubiera sesion y mandaba al login una y otra vez.

Tres sintomas distintos, ninguno parecido a la causa, y el sitio donde se
mira primero --el log del servidor-- completamente limpio.

QUE FIJA ESTA PRUEBA
--------------------
Se leen las cabeceras que el cliente pone de verdad, en su codigo, y se
comprueba que el servidor las admita. No una lista escrita a mano: una lista
escrita a mano se queda vieja igual que se quedo la de CORS.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

#: El frontend vive fuera del backend, y no siempre esta al lado (una imagen
#: del backend a solas, por ejemplo). Sin el no hay nada que comprobar.
FRONTEND = Path("../frontend/src")


def _cabeceras_permitidas() -> list[str]:
    """La lista de ``allow_headers``, leida del codigo que la configura."""
    fuente = Path("app/api/app_factory.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.keyword) or nodo.arg != "allow_headers":
            continue
        return [
            elemento.value
            for elemento in nodo.value.elts  # type: ignore[attr-defined]
            if isinstance(elemento, ast.Constant)
        ]
    raise AssertionError("no se encontro `allow_headers` en la configuracion de CORS")


def _cabeceras_que_manda_el_cliente() -> set[str]:
    """Las ``X-...`` que aparecen como clave de cabecera en el frontend.

    Se buscan solo dentro de objetos de cabeceras (``'X-Algo': valor``), no
    cualquier texto que empiece por ``X-``: una cadena suelta en un comentario
    no es una cabecera.
    """
    patron = re.compile(r"['\"](X-[A-Za-z][A-Za-z0-9-]*)['\"]\s*:")
    encontradas: set[str] = set()
    for ruta in FRONTEND.rglob("*.ts"):
        if ruta.name.endswith(".spec.ts"):
            continue
        encontradas.update(patron.findall(ruta.read_text(encoding="utf-8")))
    return encontradas


@pytest.mark.skipif(not FRONTEND.is_dir(), reason="el frontend no esta presente")
def test_toda_cabecera_del_cliente_esta_permitida():
    """Si el cliente manda una que el servidor no admite, no llega NADA."""
    permitidas = {h.lower() for h in _cabeceras_permitidas()}
    usadas = _cabeceras_que_manda_el_cliente()

    faltan = sorted(h for h in usadas if h.lower() not in permitidas)

    assert not faltan, (
        f"el frontend manda {faltan} y CORS no las admite. El navegador "
        "bloqueara TODAS las peticiones en el preflight, sin dejar rastro en "
        "el log del backend. Anadelas a `allow_headers` en app_factory.py"
    )


@pytest.mark.skipif(not FRONTEND.is_dir(), reason="el frontend no esta presente")
def test_el_cliente_manda_las_dos_que_conocemos():
    """Guardia del guardia: si la deteccion dejara de encontrar nada, esto
    pasaria vacio y no protegeria de nada."""
    usadas = _cabeceras_que_manda_el_cliente()

    assert "X-CSRF-Token" in usadas
    assert "X-WhatsApp-Account" in usadas


def test_la_cabecera_de_cuenta_esta_permitida():
    """Vale aunque el frontend no este al lado: es la que provoco el fallo."""
    permitidas = {h.lower() for h in _cabeceras_permitidas()}
    assert "x-whatsapp-account" in permitidas
