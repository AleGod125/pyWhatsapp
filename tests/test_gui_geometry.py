"""Geometria de la ventana de vinculacion.

Estos tests existen por un fallo real: con la ventana de tamano fijo (900x650)
y el escalado de Windows al 150%, la columna de texto crecia y empujaba al QR
fuera del borde derecho. Se perdian unos 100 px del codigo -- zona tranquila y
modulos de datos incluidos -- y el telefono no podia leerlo.

La regla que se comprueba: la ventana se dimensiona A PARTIR del QR, nunca al
reves.
"""

from __future__ import annotations

import queue

import pytest

tk = pytest.importorskip("tkinter")

# Payload con la forma y longitud del real (237 caracteres, ver logs de
# pairing): es el que provoca el QR grande que destapo el problema.
PAYLOAD = "2@" + "A" * 120 + "," + "B" * 40 + "," + "C" * 40 + "," + "D" * 32


@pytest.fixture(scope="module")
def app():
    """Un unico ``Tk()`` para todo el modulo.

    Crear y destruir varios interpretes Tcl seguidos en el mismo proceso falla
    de forma intermitente ("tk wasn't installed properly"). Como la aplicacion
    real tampoco abre mas de una ventana, compartir el root es ademas lo fiel
    al comportamiento que se quiere probar.
    """
    from app.gui import App

    try:
        instance = App(queue.Queue())
    except tk.TclError as exc:  # pragma: no cover - entorno sin display
        pytest.skip(f"sin display disponible: {exc}")
    yield instance
    try:
        instance.root.destroy()
    except tk.TclError:
        pass


def _settle(app) -> None:
    app.root.update_idletasks()
    app.root.update()


def test_el_qr_cabe_entero_en_la_ventana(app):
    """El fallo original: el borde derecho del QR quedaba fuera."""
    app.show_qr(PAYLOAD)
    _settle(app)

    image_width = app.pairing.qr_photo.width()
    label = app.pairing._qr_label

    assert label.winfo_width() >= image_width, (
        f"el label solo dispone de {label.winfo_width()}px para una imagen de "
        f"{image_width}px: el QR se esta recortando"
    )

    left = label.winfo_rootx() - app.root.winfo_rootx()
    assert left >= 0, "el QR empieza fuera del borde izquierdo"
    assert left + image_width <= app.root.winfo_width(), (
        f"el QR termina en {left + image_width}px y la ventana mide "
        f"{app.root.winfo_width()}px: se recorta por la derecha"
    )


def test_la_ventana_cubre_lo_que_pide_el_contenido(app):
    """Nada del contenido puede quedar fuera, ni el texto ni el codigo."""
    app.show_qr(PAYLOAD)
    _settle(app)

    assert app.root.winfo_reqwidth() <= app.root.winfo_width()
    assert app.root.winfo_reqheight() <= app.root.winfo_height()


def test_no_se_puede_encoger_hasta_recortar(app):
    """``minsize`` protege el QR de que el usuario reduzca la ventana."""
    app.show_qr(PAYLOAD)
    _settle(app)

    min_width, min_height = app.root.minsize()
    image_width = app.pairing.qr_photo.width()
    assert min_width >= image_width, "el minimo permitido ya recortaria el QR"
    assert min_height >= image_width


def test_la_rotacion_no_abre_otra_ventana(app):
    """pywhats rota el ref: debe sustituirse la imagen, no crear ventanas."""
    app.show_qr(PAYLOAD)
    _settle(app)
    before = len(app.root.winfo_children())
    first = app.pairing.qr_photo

    app.show_qr(PAYLOAD.replace("2@", "3@"))
    _settle(app)

    assert len(app.root.winfo_children()) == before
    assert app.pairing.qr_photo is not first, "la imagen deberia haberse sustituido"
    assert app.pairing.qr_photo is not None, "hay que conservar la referencia viva"


def test_el_qr_no_excede_la_pantalla(app):
    """El presupuesto se calcula desde el monitor, no desde una constante."""
    budget = app.pairing.qr_budget()
    assert budget <= app.root.winfo_screenwidth()
    assert budget <= app.root.winfo_screenheight()

    app.show_qr(PAYLOAD)
    _settle(app)
    assert app.root.winfo_width() <= app.root.winfo_screenwidth()
    assert app.root.winfo_height() <= app.root.winfo_screenheight()
