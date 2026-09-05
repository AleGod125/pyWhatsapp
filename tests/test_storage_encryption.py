"""Cifrado del contenido antes de subirlo.

Es lo que hace que un archivo en Drive sea opaco: ni Google, ni quien acceda a
esa cuenta, ni quien consiga el enlace puede leer nada sin una clave que nunca
sale de este servidor.
"""

from __future__ import annotations

import io
import os

import pytest

from app.storage.encryption import (
    CHUNK_PLAIN_BYTES,
    CHUNK_STORED_BYTES,
    Cabecera,
    ClaveDeAlmacenamientoInvalida,
    EncryptionError,
    StorageEncryption,
    derivar_kek,
    desenvolver_dek,
    envolver_dek,
    huellas_iguales,
    nueva_dek,
    rango_a_trozos,
    sha256_de,
    tamano_cifrado,
)

CLAVE_APP = "0FkR9Nx3qZbT7wLmYcPvJhGdSaUeXiOn2M4KpQrTvBw="


@pytest.fixture
def cifrado() -> StorageEncryption:
    return StorageEncryption(nueva_dek())


# ---------------------------------------------------------------------------
# Claves
# ---------------------------------------------------------------------------


def test_cada_usuario_tiene_su_clave():
    """Con una sola clave para todos, un descuido lo abre todo."""
    assert nueva_dek() != nueva_dek()
    assert len(nueva_dek()) == 32


def test_la_dek_se_guarda_envuelta_y_se_recupera():
    dek, kek = nueva_dek(), derivar_kek(CLAVE_APP)
    envuelta = envolver_dek(dek, kek)

    assert dek not in envuelta, "la clave no puede aparecer en claro"
    assert desenvolver_dek(envuelta, kek) == dek


def test_otra_kek_no_abre_la_dek():
    dek = nueva_dek()
    envuelta = envolver_dek(dek, derivar_kek(CLAVE_APP))

    with pytest.raises(EncryptionError):
        desenvolver_dek(envuelta, derivar_kek("b3RyYUNsYXZlRGlzdGludGFEZUxhUHJpbWVyYTE="))


def test_la_kek_NO_es_la_clave_de_los_secretos_del_servidor():
    """Domino separado.

    ``APP_ENCRYPTION_KEY`` protege los tokens de Google. Si la misma clave
    cifrara ademas el contenido de todos los usuarios, comprometerla abriria
    las dos cosas a la vez.
    """
    kek = derivar_kek(CLAVE_APP)
    import base64

    assert kek != base64.urlsafe_b64decode(CLAVE_APP)


def test_una_kek_propia_tiene_prioridad():
    import base64

    propia = base64.urlsafe_b64encode(os.urandom(32)).decode()
    assert derivar_kek(CLAVE_APP, propia) == base64.urlsafe_b64decode(propia)


def test_sin_ninguna_clave_se_dice_que_hacer():
    with pytest.raises(ClaveDeAlmacenamientoInvalida) as fallo:
        derivar_kek(None)
    assert "APP_ENCRYPTION_KEY" in str(fallo.value)


# ---------------------------------------------------------------------------
# Segmentos: de una pieza
# ---------------------------------------------------------------------------


def test_lo_cifrado_no_contiene_el_texto(cifrado):
    claro = b'{"text":"un secreto muy concreto"}'
    guardado = cifrado.encrypt_bytes(claro, entity="message_segment")

    assert b"un secreto muy concreto" not in guardado


def test_ida_y_vuelta(cifrado):
    claro = b'{"id":1}\n{"id":2}\n' * 300
    vuelta, cabecera = cifrado.decrypt_bytes(
        cifrado.encrypt_bytes(claro, entity="message_segment", compression="gzip")
    )
    assert vuelta == claro
    assert cabecera.compression == "gzip"
    assert cabecera.entity == "message_segment"


def test_dos_cifrados_del_mismo_texto_son_distintos(cifrado):
    """Cada uno con su nonce: si no, se veria que dos mensajes son iguales."""
    claro = b"lo mismo"
    assert cifrado.encrypt_bytes(claro, entity="x") != cifrado.encrypt_bytes(
        claro, entity="x"
    )


def test_un_byte_cambiado_hace_fallar_el_descifrado(cifrado):
    """Cifrado autenticado: no devuelve basura, falla."""
    guardado = bytearray(cifrado.encrypt_bytes(b"contenido", entity="x"))
    guardado[-1] ^= 0x01

    with pytest.raises(EncryptionError):
        cifrado.decrypt_bytes(bytes(guardado))


def test_manipular_la_cabecera_tambien_falla(cifrado):
    """La cabecera va autenticada: no se puede cambiar como se lee el archivo."""
    guardado = bytearray(cifrado.encrypt_bytes(b"contenido", entity="x"))
    posicion = guardado.find(b'"entity"')
    guardado[posicion + 1] = ord("X")

    with pytest.raises(EncryptionError):
        cifrado.decrypt_bytes(bytes(guardado))


def test_otra_clave_no_puede_leer(cifrado):
    guardado = cifrado.encrypt_bytes(b"privado", entity="x")
    with pytest.raises(EncryptionError):
        StorageEncryption(nueva_dek()).decrypt_bytes(guardado)


def test_un_archivo_sin_la_marca_se_rechaza(cifrado):
    with pytest.raises(EncryptionError):
        cifrado.decrypt_bytes(b"esto no es nuestro")


def test_un_formato_futuro_se_dice_claramente(cifrado):
    guardado = bytearray(cifrado.encrypt_bytes(b"x", entity="x"))
    posicion = guardado.find(b'"format_version":1')
    guardado[posicion + 17] = ord("9")

    with pytest.raises(EncryptionError) as fallo:
        cifrado.decrypt_bytes(bytes(guardado))
    assert "version" in str(fallo.value).lower()


# ---------------------------------------------------------------------------
# Multimedia: troceado y rangos
# ---------------------------------------------------------------------------


@pytest.fixture
def video(cifrado):
    """Dos trozos y medio de contenido, ya cifrado."""
    datos = os.urandom(int(CHUNK_PLAIN_BYTES * 2.5))
    partes = list(cifrado.encrypt_stream(io.BytesIO(datos), plaintext_size=len(datos)))
    return datos, partes[0], b"".join(partes)


def test_el_tamano_cifrado_se_puede_predecir(video):
    """Hace falta para la subida reanudable: Drive exige el total por adelantado."""
    datos, cabecera, completo = video
    assert len(completo) == tamano_cifrado(len(datos), len(cabecera))


@pytest.mark.parametrize(
    "inicio,largo",
    [(0, 100), (CHUNK_PLAIN_BYTES - 50, 100), (CHUNK_PLAIN_BYTES + 7, 5000)],
)
def test_un_rango_se_sirve_sin_bajar_el_archivo(cifrado, video, inicio, largo):
    """Lo que permite reproducir un video sin descargar los 2 GB."""
    datos, cabecera, completo = video
    fin = inicio + largo - 1

    rango = rango_a_trozos(
        inicio, fin, cabecera_bytes=len(cabecera), plaintext_size=len(datos)
    )
    piezas = []
    for posicion, indice in enumerate(range(rango.primer_trozo, rango.ultimo_trozo + 1)):
        desde = len(cabecera) + indice * CHUNK_STORED_BYTES
        piezas.append(
            cifrado.decrypt_chunk(
                completo[desde : desde + CHUNK_STORED_BYTES], indice, cabecera
            )
        )
    unido = b"".join(piezas)
    servido = unido[rango.recorte_inicial : rango.recorte_inicial + rango.longitud]

    assert servido == datos[inicio : fin + 1]


def test_un_rango_pequeno_toca_un_solo_trozo(video):
    datos, cabecera, _ = video
    rango = rango_a_trozos(
        100, 200, cabecera_bytes=len(cabecera), plaintext_size=len(datos)
    )
    assert rango.primer_trozo == rango.ultimo_trozo == 0


def test_un_trozo_en_la_posicion_equivocada_se_detecta(cifrado, video):
    """El indice va autenticado: reordenar trozos no pasa desapercibido."""
    _, cabecera, completo = video
    primero = completo[len(cabecera) : len(cabecera) + CHUNK_STORED_BYTES]

    with pytest.raises(EncryptionError):
        cifrado.decrypt_chunk(primero, 1, cabecera)


def test_cifrar_por_trozos_no_carga_el_archivo_en_memoria():
    """Se mira el CODIGO, no el texto.

    El docstring del metodo menciona ``file.read()`` justo para explicar por
    que no se usa, y buscarlo como cadena daria un falso positivo sobre la
    propia advertencia.
    """
    import ast
    import inspect
    import textwrap

    arbol = ast.parse(textwrap.dedent(inspect.getsource(StorageEncryption.encrypt_stream)))
    funcion = arbol.body[0]
    if isinstance(funcion.body[0], ast.Expr) and isinstance(
        funcion.body[0].value, ast.Constant
    ):
        del funcion.body[0]  # fuera el docstring
    codigo = ast.unparse(arbol)

    assert "yield" in codigo, "tiene que ser un generador"
    # ``read()`` SIN argumento lee el archivo entero. Con tamano, no.
    assert ".read()" not in codigo, (
        "leer el archivo entero reservaria su tamano en RAM"
    )
    assert "read(CHUNK_PLAIN_BYTES)" in codigo, "tiene que leer de trozo en trozo"


# ---------------------------------------------------------------------------
# Huellas
# ---------------------------------------------------------------------------


def test_las_huellas_se_comparan_en_tiempo_constante():
    import inspect

    assert "compare_digest" in inspect.getsource(huellas_iguales)


def test_una_huella_ausente_nunca_cuenta_como_igual():
    assert huellas_iguales(None, "abc") is False
    assert huellas_iguales("abc", None) is False
    assert huellas_iguales("", "") is False


def test_la_huella_detecta_el_cambio():
    assert sha256_de(b"a") != sha256_de(b"b")
    assert huellas_iguales(sha256_de(b"a"), sha256_de(b"a"))


def test_la_cabecera_no_lleva_nada_sensible():
    """Va en claro al principio del archivo: solo dice COMO leerlo."""
    crudo = Cabecera(entity="media", chunked=True, plaintext_size=123).to_bytes()
    for prohibido in (b"key", b"token", b"password", b"jid", b"phone"):
        assert prohibido not in crudo.lower()
