"""Un adjunto que el CDN ya no sirve es TERMINAL, no un fallo a reintentar.

EL FALLO, MEDIDO
----------------
El worker de multimedia repetia esto cada 20 segundos, para siempre::

    [MEDIA] Descargando 368 adjuntos ... fallidos=368
    [MEDIA] Descargando  60 adjuntos ... fallidos=428
    [MEDIA] Descargando  37 adjuntos ... fallidos=465

Ni una descarga. El contador subia porque eran LOS MISMOS ficheros contados
otra vez: `_classify_failure` conocia 410 y 404 como terminales, pero no 403,
y el 403 es lo que responde el CDN de verdad.

LA EVIDENCIA
------------
Consultando la base y probando el CDN a mano, el corte por antiguedad no deja
lugar a duda::

    403 Forbidden : 379 adjuntos, del 2025-10-29 al 2026-08-08
    410 Gone      :   3 adjuntos, del 2026-08-10 al 2026-08-11
    descargados   :   6 adjuntos, del 2026-08-21 al 2026-09-02

Sin un solo solape. WhatsApp sirve los adjuntos unos 30 dias y despues
responde 403 casi siempre, 410 justo en el borde. Se reprodujo con una
peticion normal fuera de la aplicacion, asi que no depende de la sesion ni del
socket: reintentarlo es reintentar lo imposible.
"""

from __future__ import annotations

import pytest


class _MediaServiceParaClasificar:
    """Solo la clasificacion: no toca base de datos ni red."""

    def __init__(self):
        self.decidido: tuple[int, str] | None = None

    def _fail(self, media_id, status, message):  # noqa: D102
        self.decidido = (media_id, status)

    @property
    def clasificar(self):
        from app.services.media_service import MediaService

        return MediaService._classify_failure.__get__(self, type(self))


@pytest.fixture
def clasificador():
    return _MediaServiceParaClasificar()


@pytest.mark.parametrize(
    "error, esperado",
    [
        ("HTTP Error 403: Forbidden", "expired"),
        ("HTTP Error 410: Gone", "expired"),
        ("HTTP Error 404: Not Found", "unavailable"),
    ],
)
def test_lo_que_el_CDN_ya_no_sirve_es_terminal(clasificador, error, esperado):
    clasificador.clasificar(7, Exception(error))

    assert clasificador.decidido == (7, esperado)


def test_el_403_NO_se_marca_como_reintentable(clasificador):
    """Era el fallo: caia en "failed", que se vuelve a intentar cada ronda."""
    clasificador.clasificar(7, Exception("HTTP Error 403: Forbidden"))

    assert clasificador.decidido[1] != "failed"


def test_un_fallo_de_red_SI_se_reintenta(clasificador):
    """Un corte no es una caducidad: eso volvera a funcionar solo."""
    clasificador.clasificar(7, Exception("Connection reset by peer"))

    assert clasificador.decidido == (7, "failed")


def test_un_adjunto_sin_direct_path_no_se_pide_nunca_mas():
    """La URL del CDN se construye con el; sin el no hay nada que pedir.

    Habia 26 filas asi, reintentandose en cada ronda contra lo imposible.
    """
    import inspect

    from app.services.media_service import MediaService

    fuente = inspect.getsource(MediaService._download_one)
    assert "if not direct_path:" in fuente
    assert '"unavailable"' in fuente
