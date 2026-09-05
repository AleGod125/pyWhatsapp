"""El segundo dispositivo es una mejora opcional, no un requisito.

QUE CAMBIA
----------
Antes bastaba con que quedara UNA conversación sin ancla para que el
orquestador levantara el worker. Con sesión guardada eso sólo reanuda —bien—,
pero sin ella **publica un segundo código QR**, y el usuario se encuentra un
código que no ha pedido, en medio de la lista de chats, con toda la pinta de
ser obligatorio.

No lo es. La aplicación funciona entera sin él: lo que aporta son anclas para
conversaciones cuyo historial todavía no se puede pedir desde la vinculación
principal.

LA REGLA
--------
* **Reanudar**, sí: si ya estuvo vinculado, arrancar no enseña ningún código.
* **Pedir**, sólo cuando el usuario lo active desde los ajustes.

Y apagarlo NO desvincula: parar un proceso y cerrar una sesión de WhatsApp son
cosas distintas, y confundirlas cuesta un código QR más.
"""

from __future__ import annotations

import ast
import pathlib

from app.web_companion.supervisor import WebCompanionSupervisor


class _Ajustes:
    def __init__(self, session_dir: pathlib.Path, **campos):
        self.session_dir = session_dir
        self.web_companion_enabled = True
        self.web_companion_chrome = ""
        self.web_store_load_earlier = False
        self.web_store_discovery_scroll = False
        self.plan_j31_primary_only = False
        self.plan_j31_freeze_history = False
        for clave, valor in campos.items():
            setattr(self, clave, valor)


def _supervisor(tmp_path, *, vinculado: bool) -> WebCompanionSupervisor:
    sesion = tmp_path / "session"
    (sesion / "web_companion").mkdir(parents=True)
    if vinculado:
        (sesion / "web_companion" / "Default").mkdir()
        (sesion / "web_companion" / "Default" / "estado").write_text("x", encoding="utf-8")
    return WebCompanionSupervisor(_Ajustes(sesion), raiz=tmp_path / "web_companion")


# ---------------------------------------------------------------------------
# Vinculado o no: la distincion que decide si aparece un codigo
# ---------------------------------------------------------------------------


def test_sin_localauth_no_esta_vinculado(tmp_path):
    assert _supervisor(tmp_path, vinculado=False).sesion_guardada is False


def test_con_localauth_esta_vinculado(tmp_path):
    assert _supervisor(tmp_path, vinculado=True).sesion_guardada is True


def test_una_carpeta_vacia_no_cuenta_como_vinculado(tmp_path):
    """Crear la carpeta es parte del arranque; no significa que haya sesion."""
    assert _supervisor(tmp_path, vinculado=False).sesion_guardada is False


def test_el_estado_dice_si_esta_vinculado(tmp_path):
    """El panel lo necesita para ofrecer 'activar' o solo informar."""
    foto = _supervisor(tmp_path, vinculado=True).snapshot()
    assert foto["linked"] is True
    assert foto["running"] is False


def test_sin_carpeta_de_sesion_no_revienta(tmp_path):
    supervisor = WebCompanionSupervisor(
        _Ajustes(tmp_path / "no-existe"), raiz=tmp_path / "web_companion"
    )
    assert supervisor.sesion_guardada is False


# ---------------------------------------------------------------------------
# El autoarranque, leido del codigo
# ---------------------------------------------------------------------------


def _rama_de_arranque() -> ast.FunctionDef:
    fuente = pathlib.Path("app/core/orchestrator.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    return next(
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.FunctionDef)
        and nodo.name == "_arrancar_recuperacion_automatica"
    )


def test_el_arranque_automatico_exige_sesion_guardada():
    """LA REGLA, fijada en el codigo.

    Si alguien vuelve a llamar a `start()` sin comprobar `sesion_guardada`,
    vuelve el segundo codigo QR sin pedirlo. Se lee el AST y no el texto: un
    comentario que mencione `sesion_guardada` no puede hacer pasar la prueba.
    """
    funcion = _rama_de_arranque()

    llamadas_a_start = [
        nodo
        for nodo in ast.walk(funcion)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr == "start"
    ]
    assert llamadas_a_start, "el arranque desaparecio: ya no se reanuda nunca"

    consultas = {
        nodo.args[1].value
        for nodo in ast.walk(funcion)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Name)
        and nodo.func.id == "getattr"
        and len(nodo.args) >= 2
        and isinstance(nodo.args[1], ast.Constant)
    }
    assert "sesion_guardada" in consultas, (
        "el arranque automatico ya no comprueba si hay sesion guardada: "
        "volveria a publicar un segundo codigo QR sin que nadie lo pida"
    )


def test_parar_el_worker_no_borra_la_sesion():
    """Apagar un proceso NO es desvincular un telefono.

    Se comprueba sobre el codigo de `stop`: si algun dia borra la carpeta de
    LocalAuth, desactivar la recuperacion avanzada obligaria a escanear otra
    vez para volver a activarla.
    """
    fuente = pathlib.Path("app/web_companion/supervisor.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    stop = next(
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.FunctionDef) and nodo.name == "stop"
    )
    prohibidas = {"rmtree", "unlink", "rmdir", "remove"}
    usadas = {
        nodo.func.attr
        for nodo in ast.walk(stop)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)
    }
    assert not (usadas & prohibidas), (
        f"`stop` borra archivos ({sorted(usadas & prohibidas)}): apagar la "
        "recuperacion avanzada no puede desvincular el dispositivo"
    )


def test_la_sesion_principal_no_depende_del_worker():
    """`primary_ready` no puede mirar al segundo dispositivo.

    Si lo mirara, apagar la recuperacion avanzada dejaria a la principal
    'no lista' y bloquearia el producto entero.
    """
    fuente = pathlib.Path("app/core/primary.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.FunctionDef, ast.Module)):
            cuerpo = getattr(nodo, "body", [])
            if (
                cuerpo
                and isinstance(cuerpo[0], ast.Expr)
                and isinstance(cuerpo[0].value, ast.Constant)
                and isinstance(cuerpo[0].value.value, str)
            ):
                cuerpo[0].value.value = ""
    codigo = ast.unparse(arbol).lower()
    assert "web_companion" not in codigo
    assert "companion" not in codigo
