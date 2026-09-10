"""Ninguna conversacion se queda en claro en el disco.

LO QUE PASO, Y POR QUE ESTE FICHERO EXISTE
------------------------------------------
El worker archivaba cada lote de historial ANTES de interpretarlo, en
``data/history_baileys/``: un JSON por lote con cada ``WebMessageInfo`` en
base64 -- texto de los mensajes incluido. Sin cifrar, sin dueno apuntado, y sin
que nadie los volviera a mirar.

Se midio lo que costo:

1. se vacia la base -- las cuentas desaparecen, los 256 ficheros se quedan;
2. se vincula otro telefono, de otra persona, bajo otro correo de Google;
3. la reingesta de archivados los lee, no puede saber de quien son, y los
   adopta: **326 conversaciones y 6613 mensajes ajenos**;
4. todo eso con el telefono de su dueno APAGADO y sin vincular.

Tres cosas fallaban a la vez, y aqui se cierran las tres:

* se guardaba contenido en claro en el disco  -> ahora apagado por defecto;
* los ficheros no decian de quien eran        -> ahora hay carpeta por cuenta;
* un fichero sin dueno se adoptaba            -> ahora se salta.

POR QUE UNA PRUEBA Y NO SOLO EL ARREGLO
---------------------------------------
Porque el arreglo es una variable de entorno vacia y una condicion, y las dos
se revierten sin querer en un `git revert` o en una linea "temporal" para
diagnosticar. Esto lo nota.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 1. No se escribe contenido en claro
# ---------------------------------------------------------------------------


def test_los_lotes_NO_se_archivan_por_defecto(settings, monkeypatch):
    """El vector exacto de la fuga: 256 ficheros con conversaciones en claro."""
    from app.wa.baileys_client import BaileysClient

    monkeypatch.delenv("WA_HISTORY_BLOBS", raising=False)
    cliente = object.__new__(BaileysClient)
    cliente._settings = settings

    entorno = cliente._entorno()

    assert entorno["WA_BAILEYS_HISTORY_DIR"] == "", (
        "el worker volveria a dejar las conversaciones en claro en el disco"
    )


@pytest.mark.parametrize("valor", ["", "0", "no", "false", "cualquier cosa"])
def test_solo_un_SI_explicito_lo_enciende(valor, monkeypatch):
    """Una opcion que expone conversaciones no se enciende por descuido."""
    from app.wa.baileys_client import _archivar_lotes_activado

    monkeypatch.setenv("WA_HISTORY_BLOBS", valor)

    assert _archivar_lotes_activado() is False


@pytest.mark.parametrize("valor", ["1", "true", "si", "SI", "Yes"])
def test_se_puede_encender_a_proposito(valor, settings, monkeypatch):
    """Sigue sirviendo para diagnosticar, y entonces va a SU carpeta."""
    from app.wa.baileys_client import BaileysClient

    monkeypatch.setenv("WA_HISTORY_BLOBS", valor)
    cliente = object.__new__(BaileysClient)
    cliente._settings = settings

    destino = cliente._entorno()["WA_BAILEYS_HISTORY_DIR"]

    assert destino, "encendido a proposito, tiene que archivar"


# ---------------------------------------------------------------------------
# 2. Y si se enciende, cada cuenta a su carpeta
# ---------------------------------------------------------------------------


def test_cada_cuenta_archiva_en_SU_carpeta(settings):
    """Una carpeta comun es lo que hizo imposible saber de quien era cada lote.

    Con los ficheros mezclados, la reingesta tenia que adivinar el dueno de
    cada conversacion. Separados, un lote ajeno ni se abre.
    """
    import uuid

    from app.core.session_paths import ajustes_de_cuenta

    una = uuid.uuid4()
    otra = uuid.uuid4()

    a = ajustes_de_cuenta(settings, una).history_blobs_dir
    b = ajustes_de_cuenta(settings, otra).history_blobs_dir

    assert a != b, "dos cuentas archivando en el mismo sitio"
    assert str(una) in str(a)
    assert str(otra) in str(b)


def test_la_carpeta_comun_ya_no_la_usa_nadie(settings):
    """Ninguna cuenta debe escribir en `data/history_baileys/` a secas."""
    import uuid

    from app.core.session_paths import ajustes_de_cuenta

    comun = Path(settings.data_dir) / "history_baileys"
    propia = Path(ajustes_de_cuenta(settings, uuid.uuid4()).history_blobs_dir)

    assert propia != comun
    assert propia.parent == comun, "deberia colgar de ella, no ser ella"


# ---------------------------------------------------------------------------
# 3. Un lote sin dueno NO se adopta
# ---------------------------------------------------------------------------


def test_una_conversacion_que_nadie_tiene_no_se_adopta():
    """El atajo que produjo la fuga: "solo hay una cuenta, luego es suya".

    Daba por hecho que el lote lo produjo una cuenta que todavia existe. Con la
    base recien vaciada eso es falso, y contesto "es tuya" a las conversaciones
    de otra persona.
    """
    from app.services.blob_reingest import _es_mia

    # Nadie tiene esa conversacion, y en la maquina hay UNA sola cuenta.
    veredicto = _es_mia("desconocida@s.whatsapp.net", "cuenta-1", {}, True)

    assert veredicto is None, (
        "adoptar una conversacion huerfana es como se ingirieron 6613 "
        "mensajes de otra persona"
    )


def test_lo_que_SI_es_mio_se_sigue_reingiriendo():
    """La otra mitad: recuperar lo propio tiene que seguir funcionando."""
    from app.services.blob_reingest import _es_mia

    duenos = {"mia@s.whatsapp.net": {"cuenta-1"}}

    assert _es_mia("mia@s.whatsapp.net", "cuenta-1", duenos, True) is True


def test_lo_de_OTRA_cuenta_se_salta_siempre():
    from app.services.blob_reingest import _es_mia

    duenos = {"suya@s.whatsapp.net": {"cuenta-2"}}

    assert _es_mia("suya@s.whatsapp.net", "cuenta-1", duenos, True) is False


# ---------------------------------------------------------------------------
# 4. El codigo no puede volver atras sin que se note
# ---------------------------------------------------------------------------


def test_el_atajo_de_adoptar_huerfanos_no_ha_vuelto():
    """`return True if sola else None` es la linea exacta que hay que vigilar."""
    from app.services import blob_reingest

    fuente = inspect.getsource(blob_reingest._es_mia)

    assert "True if sola else" not in fuente, (
        "volvio el atajo que adopta conversaciones huerfanas"
    )


def test_nadie_mas_escribe_lotes_en_la_carpeta_comun():
    """Un segundo sitio que archive en claro seria empezar de cero."""
    import app.wa.baileys_client as cliente

    fuente = inspect.getsource(cliente)

    # La unica asignacion de la carpeta tiene que estar detras del interruptor.
    assert "_archivar_lotes_activado()" in fuente
    assert fuente.count('entorno["WA_BAILEYS_HISTORY_DIR"]') == 2, (
        "hay mas de un sitio poniendo la carpeta de lotes: uno de ellos "
        "puede saltarse el interruptor"
    )


# ---------------------------------------------------------------------------
# 5. El disco es una CACHE, no un segundo archivo
# ---------------------------------------------------------------------------
#
# `MediaStorage.desalojar` existia, estaba bien hecho --solo toca lo que esta
# a salvo en Drive, con su huella comprobada-- y NO LO LLAMABA NADIE. Por eso
# `data/media` llego a 5,8 GB de fotos, audios y documentos en claro, junto al
# codigo. Un desalojo que nadie ejecuta es lo mismo que no tenerlo.


def test_el_trabajador_desaloja_multimedia():
    """La linea que faltaba: llamarlo."""
    import app.storage.worker as trabajador

    fuente = inspect.getsource(trabajador.DriveStorageWorker)

    assert "_desalojar_multimedia" in fuente
    assert "desalojar(" in fuente, "el desalojo no llega a ejecutarse"


def test_el_trabajador_purga_el_contenido_subido():
    """Y lo mismo con el texto de los mensajes."""
    import app.storage.worker as trabajador

    fuente = inspect.getsource(trabajador.DriveStorageWorker)

    assert "_purgar_lo_confirmado" in fuente


def test_la_cache_local_es_pequena_por_defecto(settings):
    """Cinco gigas no es una cache: es un segundo archivo sin cifrar.

    La copia buena esta en Drive. Lo local cubre lo que se esta mirando, y si
    falta se baja de alli sin que el navegador se entere.
    """
    assert settings.local_media_cache_max_gb <= 1.0, (
        "la cache local volvio a ser lo bastante grande para guardar el "
        "archivo entero en claro"
    )
    assert settings.local_media_cache_ttl_hours <= 24


def test_solo_se_desaloja_lo_que_Drive_confirma():
    """Borrar del disco algo que no llego a Drive es perderlo."""
    import app.storage.media as media

    fuente = inspect.getsource(media.MediaStorage.puede_desalojar)

    # La comprobacion mira el identificador remoto y la huella, no solo el
    # estado: un `drive_file_id` puesto no prueba que el contenido llegara.
    assert "drive_file_id" in fuente


# ---------------------------------------------------------------------------
# 6. Excavar no lee del disco
# ---------------------------------------------------------------------------


def test_el_ciclo_de_sincronizacion_no_relee_el_disco():
    """`archive` era la unica fase que leia ficheros, y por ahi entro la fuga.

    Excavar es pedirle cosas al TELEFONO. Que un ciclo automatico lea del
    disco y meta lo que encuentre en la cuenta que se este mirando es una
    fuente de datos que nadie pidio y que nadie revisa.
    """
    from app.services.sync_job import PHASES

    assert "archive" not in PHASES, (
        "volvio la fase que relee lotes del disco durante la excavacion"
    )


def test_el_ciclo_no_llama_a_la_reingesta():
    """Que la fase exista no basta: lo que importa es que nadie la invoque."""
    import app.services.sync_job as trabajo

    ciclo = inspect.getsource(trabajo.SyncJob._ciclo)

    # Se busca la LLAMADA, no el nombre: el comentario que explica por que se
    # quito tambien lo menciona, y una prueba que falla por su propia
    # documentacion no sirve para nada.
    assert "await self._fase_archivo(" not in ciclo, (
        "el ciclo volvio a llamar a la reingesta de archivados"
    )


# ---------------------------------------------------------------------------
# 7. Ningun perfil lee el Drive de otro
# ---------------------------------------------------------------------------
#
# La comprobacion de propiedad existe --`fila.user_id != user_id` en
# `_descargar`-- pero la cache se miraba ANTES, y su clave era solo el
# identificador del segmento. Un acierto se saltaba la comprobacion.
#
# Y esa cache no es de un usuario: el lector se guarda en el runtime del
# PROCESO, asi que la comparten todos los que entren en la instalacion.


def test_la_cache_de_segmentos_distingue_por_usuario():
    """Sin el usuario en la clave, un acierto entrega contenido ajeno."""
    import app.storage.reader as lector

    fuente = inspect.getsource(lector.MessageReader._lineas_de)

    assert "user_id" in fuente, "la cache no mira de quien es lo que devuelve"
    assert "self._cache[segmento_id]" not in fuente, (
        "la clave volvio a ser solo el segmento: un acierto se salta la "
        "comprobacion de propiedad"
    )


def test_la_propiedad_se_comprueba_al_descargar():
    """La otra mitad: el unico camino hasta el contenido la atraviesa."""
    import app.storage.reader as lector

    fuente = inspect.getsource(lector.MessageReader._descargar)

    assert "user_id" in fuente
    assert "!=" in fuente, "desaparecio la comparacion de dueno"


def test_cada_usuario_usa_SUS_credenciales_de_Drive():
    """No existe un cliente global capaz de escribir en el Drive de cualquiera."""
    import app.core.runtime as runtime

    fuente = inspect.getsource(runtime.AppRuntime.storage_para)

    assert "access_token(user_id)" in fuente
    assert "user_id=user_id" in fuente
