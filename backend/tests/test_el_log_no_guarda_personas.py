"""El fichero de log no puede ser un segundo archivo de datos personales.

LO QUE SE MIDIO
---------------
``diagnostics/`` ocupaba 120 MB: 107 MB en ``app.log.1`` mas 13 MB en
``app.log``. El tope estaba puesto ahi a proposito --cinco tandas de 20 MB--
pensando solo en el espacio.

Pero un log de este proyecto no es texto neutro. Lleva con que conversacion
esta trabajando el motor, y hasta ahora tambien **telefonos en claro**. El
filtro tapaba los JID completos --exige el ``@s.whatsapp.net`` detras-- y
dejaba pasar el numero pelado, que es justo como lo escriben los avisos de
atribucion::

    [AUTH] IDENTIDAD INVERTIDA: la cuenta 91a8470b esta registrada como
           573002389304 y la sesion que hay en su carpeta es de 573008927374

Tres numeros de dos personas reales, en un fichero que guardaba 120 MB de
historial.

LAS DOS COSAS QUE ESTO FIJA
---------------------------
1. un telefono suelto se tapa igual que uno con servidor detras;
2. y lo que NO es un telefono --un PID, un numero de secuencia, un id de
   chat-- se queda entero, porque un log que lo tapa todo no sirve para nada.
"""

from __future__ import annotations

import logging

import pytest

from app.core.logging_setup import SecretRedactionFilter


def _pasar(mensaje: str) -> str:
    """El mensaje tal y como quedaria escrito en el fichero."""
    registro = logging.LogRecord("x", logging.INFO, "", 0, mensaje, (), None)
    SecretRedactionFilter().filter(registro)
    return registro.getMessage()


# ---------------------------------------------------------------------------
# 1. Los telefonos se tapan, con servidor detras o sin el
# ---------------------------------------------------------------------------


def test_un_telefono_suelto_se_tapa():
    """El caso real: los avisos de atribucion escriben el numero pelado."""
    salida = _pasar(
        "IDENTIDAD INVERTIDA: la cuenta 91a8470b esta registrada como "
        "573002389304 y la sesion que hay en su carpeta es de 573008927374"
    )

    assert "573002389304" not in salida
    assert "573008927374" not in salida
    assert "573002***" in salida, "se tapo entero: sin prefijo no se sigue un caso"
    assert "573008***" in salida


def test_un_jid_completo_se_sigue_tapando():
    """Lo que ya funcionaba no puede romperse."""
    salida = _pasar("chat=573001112233@s.whatsapp.net timeout intento=2")

    assert "573001112233" not in salida
    assert "573001***@s.whatsapp.net" in salida


@pytest.mark.parametrize(
    "servidor", ["s.whatsapp.net", "lid", "c.us", "g.us"]
)
def test_todos_los_servidores_de_whatsapp(servidor):
    salida = _pasar(f"destino 573009998877@{servidor}")

    assert "573009998877" not in salida


def test_el_log_no_deja_el_numero_ni_al_final_de_la_linea():
    """Sin nada detras tampoco: el patron no puede depender del contexto."""
    salida = _pasar("telefono real 573008927374")

    assert "573008927374" not in salida


# ---------------------------------------------------------------------------
# 2. Lo que NO es un telefono se queda entero
# ---------------------------------------------------------------------------
#
# Un filtro que tapa cualquier cifra larga deja el log inservible: no se puede
# seguir un PID, ni un numero de secuencia, ni cruzar una marca de tiempo.


@pytest.mark.parametrize(
    "mensaje",
    [
        "segmento chat=7 secuencia=12 mensajes=50",
        "Cerrojo de sesion adquirido (service.py PID 18380)",
        "App-state 'regular_high': 1 mutaciones",
        "1226 adjunto(s) atribuidos a su cuenta",
        "Mantenimiento: nada que reconciliar (94 ms)",
        "revision=c3d4e5f9a0b1",
    ],
)
def test_lo_que_no_es_un_telefono_no_se_toca(mensaje):
    assert _pasar(mensaje) == mensaje


def test_una_marca_de_tiempo_no_se_confunde_con_un_telefono():
    """`1700000000` es un instante, no una persona.

    Este log esta lleno de marcas Unix en segundos --anclas, cursores,
    ventanas de reintento-- y tienen exactamente diez digitos. Tapandolas se
    pierde justo lo que sirve para seguir un historial atascado, asi que el
    patron pide ONCE como minimo.
    """
    salida = _pasar("oldest_message_timestamp=1700000000 en el ancla")

    assert "1700000000" in salida


def test_un_telefono_de_once_digitos_SI_se_tapa():
    """El limite esta justo encima de la marca de tiempo, no mas arriba."""
    salida = _pasar("numero 12345678901 suelto")

    assert "12345678901" not in salida


# ---------------------------------------------------------------------------
# 3. El fichero no puede volver a guardar 120 MB
# ---------------------------------------------------------------------------


def test_el_log_tiene_un_tope_pequeno():
    """Cuanto mas historial se guarda, mas hay que proteger.

    Con el tope anterior --20 MB x 5-- se llego a 120 MB de texto que incluia
    identificadores de conversaciones. Lo unico que se consulta de verdad son
    las ultimas horas.
    """
    import inspect

    from app.core import logging_setup

    fuente = inspect.getsource(logging_setup.setup_logging)

    assert "maxBytes=5 * 1024 * 1024" in fuente
    assert "backupCount=1" in fuente

    # El techo real, calculado: fichero vivo + tandas guardadas.
    techo_mb = 5 * (1 + 1)
    assert techo_mb <= 20, f"el log puede crecer hasta {techo_mb} MB"


def test_el_filtro_se_aplica_al_FICHERO_y_no_solo_a_la_consola():
    """En la consola se pierde; en el fichero se queda."""
    import inspect

    from app.core import logging_setup

    fuente = inspect.getsource(logging_setup.setup_logging)

    assert fuente.count("addFilter(redaction)") >= 2, (
        "el fichero se quedaria sin redactar, que es el que persiste"
    )


# ---------------------------------------------------------------------------
# 4. Los casos que se escaparon al medirlo contra el log de verdad
# ---------------------------------------------------------------------------
#
# El primer patron parecia completo y dejaba pasar catorce numeros reales. Se
# vieron pasando el filtro sobre `diagnostics/app.log` linea a linea, que es
# la unica forma de saberlo: escribir casos a mano solo prueba lo que uno ya
# se imagino.


def test_un_telefono_al_FINAL_DE_LA_FRASE_se_tapa():
    """El caso mas comun, y el que se escapaba.

    El punto estaba en la exclusion por la derecha --para no partir versiones
    ni direcciones IP-- y con el se colaba todo lo que acaba en punto. En el
    log real eran catorce lineas asi.
    """
    salida = _pasar("sus credenciales son de 573008927374. Manda el fichero.")

    assert "573008927374" not in salida
    assert "573008***" in salida


def test_dos_puntos_tampoco_son_frontera():
    salida = _pasar("sus chats son de 573002389304: no se reetiqueta")

    assert "573002389304" not in salida


def test_el_almacen_de_signal_nombra_por_telefono():
    """``session-573243116421.0.json`` es el telefono de un contacto.

    Sale en los avisos de archivado de una sesion revocada, que listan los
    ficheros que no se pudieron mover.
    """
    salida = _pasar("siguen ahi: creds.json, session-573243116421.0.json")

    assert "573243116421" not in salida
    assert "session-573243***" in salida


@pytest.mark.parametrize(
    "carpeta",
    [
        "session-20260911185133-revoked-reason-loggedOut",
        "session-20260910-104511-revoked",
    ],
)
def test_la_FECHA_de_una_sesion_archivada_se_respeta(carpeta):
    """Es el nombre de una carpeta, no una persona.

    Taparla deja el aviso sin la unica pista para encontrarla. Ningun
    indicativo de pais empieza por 20; los anos de este siglo, todos.
    """
    assert _pasar(f"Sesion archivada en {carpeta}") == f"Sesion archivada en {carpeta}"


def test_una_version_de_whatsapp_no_se_toca():
    """`2.3000.1047203841` es lo que anuncia el cliente, no un telefono."""
    salida = _pasar("Version resuelta en vivo: 2.3000.1047203841")

    assert "2.3000.1047203841" in salida


def test_el_recorte_del_volcado_hex_no_se_recorta_dos_veces():
    """`_recortar_hex` deja los primeros caracteres para saber QUE campo llego.

    Esa es la unica razon de que ese aviso exista. Pueden ser doce digitos
    seguidos --``000102030405``-- y encajaban en el patron de telefono, asi
    que el recorte quedaba recortado otra vez y el aviso perdia su contenido.
    """
    salida = _pasar("receiver: empty text id=X hex=000102030405060708090a0b0c0d0e0f")

    assert "hex=000102030405" in salida, "se comio el prefijo que identifica el campo"
