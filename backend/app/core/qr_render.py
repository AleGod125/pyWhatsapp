"""Generacion de la imagen del codigo QR de vinculacion.

Reglas que este modulo respeta al pie de la letra:

* El payload se codifica TAL CUAL lo emite pywhats. No se recorta, no se
  normaliza, no se reordenan las comas ni se toca un solo caracter: es un
  transporte de material criptografico y cualquier cambio lo invalida.
* El tamano se consigue eligiendo ``box_size`` (pixeles por modulo), no
  reescalando. Un resize con interpolacion bilineal o bicubica difumina los
  bordes de los modulos y arruina la lectura. Si hace falta ajustar, se usa
  NEAREST con un factor entero.
* Se conserva la zona tranquila (``border=4`` modulos, el minimo de la norma).
* Nunca se loguea el payload. Solo su longitud.
"""

from __future__ import annotations

import qrcode
from PIL import Image
from qrcode.constants import ERROR_CORRECT_L

from app.core.logging_setup import get_logger

log = get_logger("QR")

# Zona tranquila en modulos. 4 es el minimo que exige la especificacion.
BORDER_MODULES = 4

# Rango de tamano final PREFERIDO, en pixeles. El maximo se subio a 580 tras
# comprobar que con el payload real (237 caracteres, 57 modulos) el QR salia a
# 456 px: legible, pero mas pequeno de lo necesario. A 10 px/modulo son 570 px,
# bastante mas comodo para la camara. La ventana se dimensiona a partir del QR,
# no al reves, asi que ya no hay riesgo de recorte.
MIN_SIZE = 360
PREFERRED_MAX_SIZE = 580

# Pixeles por modulo. Este es el limite duro: lo que determina si el telefono
# lee el codigo es el tamano del MODULO, no el de la imagen.
MIN_BOX_SIZE = 8
MAX_BOX_SIZE = 12

# Con el payload real de WhatsApp (unos 200-250 caracteres) el QR ronda los 61
# modulos, que a 8 px/modulo dan ~488 px: por encima del maximo preferido.
# Bajar a 7 px/modulo entraria en el rango pero haria el codigo mas dificil de
# escanear. Se respeta MIN_BOX_SIZE y se deja crecer la imagen, porque la
# prioridad declarada es que el QR sea nitido y escaneable. La ventana (900x650)
# tiene sitio de sobra.
MAX_SIZE = 620

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)


def _choose_box_size(modules: int, budget: int) -> int:
    """Mayor ``box_size`` dentro de los limites que quepa en ``budget`` pixeles.

    ``modules`` incluye ya la zona tranquila. Se prefiere el QR mas grande
    posible: cuanto mayor es el modulo, mas facil lo lee la camara.

    Si ni siquiera ``MIN_BOX_SIZE`` cabe se devuelve igualmente, porque un
    modulo por debajo de 8 px es peor que una imagen algo mas grande de lo
    previsto. Quien llama decide entonces si agranda la ventana.
    """
    best = MIN_BOX_SIZE
    for box_size in range(MIN_BOX_SIZE, MAX_BOX_SIZE + 1):
        if modules * box_size <= budget:
            best = box_size
    if modules * best > budget:
        log.debug(
            "QR de %d modulos: a %d px/modulo son %d px, por encima del "
            "presupuesto (%d). Se prioriza la nitidez del modulo.",
            modules, best, modules * best, budget,
        )
    return best


def render_qr(payload: str, *, max_pixels: int | None = None) -> Image.Image:
    """Devuelve la imagen del QR para ``payload``.

    :param payload: la cadena EXACTA emitida por pywhats en el evento ``qr``.
    :param max_pixels: ancho maximo que puede permitirse quien lo va a pintar.
        Es un techo orientativo: nunca se baja de ``MIN_BOX_SIZE`` px por
        modulo, porque un QR con modulos diminutos no lo lee la camara.
    """
    if not payload:
        raise ValueError("el payload del QR esta vacio")

    # A DEBUG: el QR rota cada pocos segundos y esto eran dos lineas por
    # rotacion. El payload NUNCA se registra; solo su longitud.
    log.debug("Payload recibido, longitud=%d", len(payload))

    # ERROR_CORRECT_L es lo que usa WhatsApp Web: mas correccion significaria
    # mas modulos para el mismo dato y un QR mas denso, mas dificil de leer.
    # version=None deja que la libreria elija la minima que quepa.
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_L,
        box_size=MIN_BOX_SIZE,  # provisional; se recalcula abajo
        border=BORDER_MODULES,
    )
    qr.add_data(payload)
    qr.make(fit=True)

    # modules_count no incluye el borde; el ancho real si.
    total_modules = qr.modules_count + 2 * BORDER_MODULES
    budget = PREFERRED_MAX_SIZE if max_pixels is None else max_pixels
    qr.box_size = _choose_box_size(total_modules, budget)

    image = qr.make_image(fill_color=BLACK, back_color=WHITE).convert("RGB")

    # Si aun asi se queda corto (payloads pequenos -> pocos modulos), se
    # amplia con un factor ENTERO y NEAREST: cada modulo sigue siendo un
    # bloque de pixeles nitido, sin suavizado.
    if image.width < MIN_SIZE:
        factor = -(-MIN_SIZE // image.width)  # techo de la division
        image = image.resize(
            (image.width * factor, image.height * factor), resample=Image.Resampling.NEAREST
        )

    log.debug(
        "Imagen generada=%dx%d border=%d", image.width, image.height, BORDER_MODULES
    )
    log.debug(
        "Modulos=%d box_size=%d version=%s", qr.modules_count, qr.box_size, qr.version
    )
    return image
