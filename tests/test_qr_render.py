"""Calidad de la imagen del QR.

Lo que se verifica no es estetico: un QR difuminado o sin zona tranquila no
lo lee el telefono, y ese es el unico paso del flujo que no se puede
automatizar.
"""

from __future__ import annotations

import pytest

from app.core.qr_render import BORDER_MODULES, MAX_SIZE, MIN_BOX_SIZE, MIN_SIZE, render_qr

# Forma real del payload de pywhats: cuatro campos separados por comas, el
# ref y tres claves en base64 (ver pairing.encode_qr_payload).
PAYLOAD = (
    "2@abcdefghijklmnopqrstuvwxyz0123456789ABCDEF+/=,"
    "aGVsbG8gd29ybGQgdGhpcyBpcyBhIGZha2Uga2V5IGZvciB0ZXN0cw==,"
    "YW5vdGhlciBmYWtlIGtleSB1c2VkIG9ubHkgaW4gdGhlIHRlc3Qgc3VpdGU=,"
    "dGhpcmQgZmFrZSBrZXkgZm9yIHRoZSBhZHYgc2VjcmV0IGZpZWxkIQ=="
)


def test_tamano_dentro_del_rango():
    image = render_qr(PAYLOAD)
    assert image.width == image.height, "el QR debe ser cuadrado"
    assert MIN_SIZE <= image.width <= MAX_SIZE, f"tamano fuera de rango: {image.width}"


def test_modulo_nunca_baja_del_minimo():
    """El limite duro: cada modulo debe ocupar al menos MIN_BOX_SIZE pixeles.

    Es lo que decide si el telefono puede leer el codigo. La imagen puede
    crecer por encima del tamano preferido, pero el modulo no puede encogerse.
    """
    image = render_qr(PAYLOAD)
    # El primer pixel negro de la diagonal marca el fin de la zona tranquila,
    # que mide exactamente BORDER_MODULES modulos.
    rgb = image.convert("RGB")
    first_dark = next(i for i in range(image.width) if rgb.getpixel((i, i)) == (0, 0, 0))
    box_size = first_dark // BORDER_MODULES
    assert box_size >= MIN_BOX_SIZE, f"modulos de solo {box_size}px"


def test_solo_blanco_y_negro_puros():
    """Sin grises: un solo pixel intermedio delataria interpolacion.

    Es la comprobacion que garantiza que no se ha usado bilinear/bicubic ni
    ningun suavizado en el camino.
    """
    image = render_qr(PAYLOAD)
    colors = {color for _count, color in image.convert("RGB").getcolors(maxcolors=100000)}
    assert colors <= {(0, 0, 0), (255, 255, 255)}, f"hay colores intermedios: {colors}"


def test_conserva_la_zona_tranquila():
    """El borde debe ser blanco en todo su grosor por los cuatro lados."""
    image = render_qr(PAYLOAD).convert("RGB")
    width = image.width

    # Se deduce el tamano de modulo a partir del primer pixel negro que
    # aparece en la diagonal: ese es el inicio del patron de posicion.
    first_dark = next(i for i in range(width) if image.getpixel((i, i)) == (0, 0, 0))
    quiet_zone_px = first_dark
    assert quiet_zone_px > 0, "no hay zona tranquila"
    assert quiet_zone_px % BORDER_MODULES == 0, (
        f"la zona tranquila ({quiet_zone_px}px) no es multiplo de "
        f"{BORDER_MODULES} modulos"
    )

    for offset in range(quiet_zone_px):
        for coordinate in range(width):
            assert image.getpixel((coordinate, offset)) == (255, 255, 255)
            assert image.getpixel((coordinate, width - 1 - offset)) == (255, 255, 255)
            assert image.getpixel((offset, coordinate)) == (255, 255, 255)
            assert image.getpixel((width - 1 - offset, coordinate)) == (255, 255, 255)


def test_payloads_distintos_dan_imagenes_distintas():
    """Descarta que se este cacheando o ignorando el payload."""
    first = render_qr(PAYLOAD)
    second = render_qr(PAYLOAD.replace("2@", "3@"))
    assert first.tobytes() != second.tobytes()


def test_mismo_payload_es_determinista():
    assert render_qr(PAYLOAD).tobytes() == render_qr(PAYLOAD).tobytes()


def test_payload_vacio_es_error():
    with pytest.raises(ValueError):
        render_qr("")
