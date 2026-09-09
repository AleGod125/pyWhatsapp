"""El contenido de los mensajes sale de Drive, no de PostgreSQL.

LA PRUEBA QUE IMPORTA
---------------------
``test_el_texto_sale_de_drive_con_postgresql_vacio``: se borra el texto de la
base, se vacia la cache, y el mensaje sigue llegando completo. Sin esa prueba,
"lee de Drive" es una afirmacion que nadie ha comprobado — el texto podria
estar saliendo de PostgreSQL sin que se note.

EL REPARTO
----------
PostgreSQL dice QUE mensajes hay y en que orden. Drive tiene el contenido.
"""

from __future__ import annotations

import uuid

import pytest

from app.auth.google import SCOPE_DRIVE
from app.storage import segments as seg
from app.storage.interface import StorageError
from app.storage.reader import ContenidoCorrupto, ContenidoNoDisponible, MessageReader
from tests.test_storage_pipeline import DriveFalso

CLAVE = "una contrasena larga"


def _correo() -> str:
    return f"drv-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
def escenario(runtime, settings, session):
    """Un chat con tres mensajes YA subidos a un Drive simulado."""
    import dataclasses

    from sqlalchemy import delete

    from app.models import Chat, GoogleCredential, Message, WhatsAppAccount
    from app.storage.service import StorageService

    session.execute(delete(WhatsAppAccount))
    session.flush()

    runtime._montar_cuentas()
    runtime._whatsapp = True
    inicio = runtime.auth.register(email=_correo(), password=CLAVE)
    session.add(
        GoogleCredential(
            user_id=inicio.user_id,
            google_subject=f"sub-{uuid.uuid4().hex[:8]}",
            scope=f"openid email profile {SCOPE_DRIVE}",
            refresh_token_encrypted=b"x",
        )
    )
    cuenta = WhatsAppAccount(
        user_id=inicio.user_id,
        session_status="linked",
        session_storage_key=f"users/{inicio.user_id}",
    )
    session.add(cuenta)
    session.flush()

    chat = Chat(
        jid=f"5734{uuid.uuid4().hex[:8]}@s.whatsapp.net",
        chat_type="individual",
        whatsapp_account_id=cuenta.id,
    )
    session.add(chat)
    session.flush()

    almacenamiento = StorageService(
        runtime.database,
        dataclasses.replace(settings, app_encryption_key=_clave_de_prueba()),
    )
    runtime.storage = almacenamiento

    textos = ["primero", "segundo con acentos: cañón", "tercero"]
    filas = []
    for i, texto in enumerate(textos):
        fila = Message(
            chat_id=chat.id,
            chat_jid=chat.jid,
            whatsapp_message_id=f"WAMID{uuid.uuid4().hex[:16].upper()}",
            timestamp=1_760_000_000 + i,
            from_me=False,
            message_type="text",
            text=texto,
            source="on_demand",
        )
        session.add(fila)
        filas.append(fila)
    session.flush()

    # Se cierra el segmento y se "sube" al Drive simulado.
    for fila in filas:
        almacenamiento.anadir_mensaje(
            session, fila, user_id=inicio.user_id, account_id=cuenta.id
        )
    segmento = almacenamiento.cerrar(
        session, chat.id, user_id=inicio.user_id, account_id=cuenta.id
    )
    session.flush()

    drive = DriveFalso()
    contenido = almacenamiento.reconstruir(segmento.id)
    paquete = seg.empaquetar(
        contenido, encryption=almacenamiento.cifrado_de(inicio.user_id)
    )
    drive.archivos["seg-1"] = paquete.datos

    segmento.drive_file_id = "seg-1"
    segmento.status = "ready"
    segmento.sha256 = paquete.sha256_claro
    segmento.ciphertext_sha256 = paquete.sha256_cifrado
    for fila in filas:
        fila.storage_status = "ready"
        fila.segment_id = segmento.id
    session.flush()

    return {
        "runtime": runtime,
        "user_id": inicio.user_id,
        "token": inicio.token,
        "chat": chat,
        "filas": filas,
        "textos": textos,
        "segmento": segmento,
        "drive": drive,
        "storage": almacenamiento,
    }


def _clave_de_prueba() -> str:
    from app.auth.crypto import generar_clave

    return generar_clave()


def _lector(escenario) -> MessageReader:
    return MessageReader(escenario["runtime"].database, escenario["storage"])


# ---------------------------------------------------------------------------
# La prueba que importa
# ---------------------------------------------------------------------------


def test_el_texto_sale_de_drive_con_postgresql_vacio(escenario, session):
    """PostgreSQL sin texto, cache vacia, contenido correcto.

    Es lo unico que demuestra que Drive es de verdad la fuente. Con el texto
    todavia en la base, cualquier implementacion parece funcionar.
    """
    for fila in escenario["filas"]:
        fila.text = None
    session.flush()

    lector = _lector(escenario)
    lector.vaciar_cache()

    resueltos = lector.resolver(
        escenario["filas"],
        user_id=escenario["user_id"],
        almacenamiento=escenario["drive"],
    )

    assert [resueltos[f.id].text for f in escenario["filas"]] == escenario["textos"]
    assert all(r.fuente == "drive" for r in resueltos.values())


def test_los_acentos_sobreviven_al_viaje(escenario, session):
    for fila in escenario["filas"]:
        fila.text = None
    session.flush()

    resueltos = _lector(escenario).resolver(
        escenario["filas"],
        user_id=escenario["user_id"],
        almacenamiento=escenario["drive"],
    )
    assert "cañón" in resueltos[escenario["filas"][1].id].text


def test_no_depende_de_ningun_archivo_local(escenario, session, tmp_path):
    """Ni session/, ni los blobs de historial, ni la cache de multimedia."""
    import ast
    import inspect

    from app.storage import reader

    arbol = ast.parse(inspect.getsource(reader))
    textos = [
        n.value
        for n in ast.walk(arbol)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    for prohibido in ("device.json", "signal.db", "session/", "data/history"):
        assert not any(prohibido in t for t in textos)


# ---------------------------------------------------------------------------
# Una descarga por segmento
# ---------------------------------------------------------------------------


def test_un_segmento_se_descarga_UNA_vez(escenario):
    """Bajar el mismo archivo por cada mensaje serian 200 llamadas a Google."""
    descargas = []
    drive = escenario["drive"]
    original = drive.read_file
    drive.read_file = lambda fid: descargas.append(fid) or original(fid)

    _lector(escenario).resolver(
        escenario["filas"],
        user_id=escenario["user_id"],
        almacenamiento=drive,
    )
    assert len(descargas) == 1, f"se descargo {len(descargas)} veces"


def test_la_cache_evita_la_segunda_descarga(escenario):
    descargas = []
    drive = escenario["drive"]
    original = drive.read_file
    drive.read_file = lambda fid: descargas.append(fid) or original(fid)

    lector = _lector(escenario)
    for _ in range(3):
        lector.resolver(
            escenario["filas"],
            user_id=escenario["user_id"],
            almacenamiento=drive,
        )
    assert len(descargas) == 1


def test_sin_cache_sigue_funcionando(escenario):
    """La cache es comodidad: vaciarla no cambia ningun resultado."""
    lector = _lector(escenario)
    primero = lector.resolver(
        escenario["filas"], user_id=escenario["user_id"], almacenamiento=escenario["drive"]
    )
    lector.vaciar_cache()
    segundo = lector.resolver(
        escenario["filas"], user_id=escenario["user_id"], almacenamiento=escenario["drive"]
    )
    assert {k: v.text for k, v in primero.items()} == {
        k: v.text for k, v in segundo.items()
    }


# ---------------------------------------------------------------------------
# Localizar el mensaje
# ---------------------------------------------------------------------------


def test_la_posicion_lleva_al_mensaje_correcto(escenario, session):
    resueltos = _lector(escenario).resolver(
        escenario["filas"], user_id=escenario["user_id"], almacenamiento=escenario["drive"]
    )
    for fila, texto in zip(escenario["filas"], escenario["textos"]):
        assert resueltos[fila.id].text == texto


def test_una_posicion_equivocada_no_devuelve_el_mensaje_de_otro(escenario, session):
    """Mejor pagar una busqueda que entregar el mensaje que no es."""
    escenario["filas"][0].segment_index = 2  # apunta al tercero
    for fila in escenario["filas"]:
        fila.text = None
    session.flush()

    resueltos = _lector(escenario).resolver(
        escenario["filas"], user_id=escenario["user_id"], almacenamiento=escenario["drive"]
    )
    assert resueltos[escenario["filas"][0].id].text == "primero"


def test_los_indices_empiezan_en_cero(escenario):
    assert [f.segment_index for f in escenario["filas"]] == [0, 1, 2]


# ---------------------------------------------------------------------------
# Cuando algo va mal
# ---------------------------------------------------------------------------


def test_si_drive_falla_se_dice_no_se_finge_vacio(escenario, session):
    """Devolver [] convertiria "no se pudo traer" en "no hay mensajes"."""
    for fila in escenario["filas"]:
        fila.text = None
    session.flush()

    drive = escenario["drive"]

    def caido(_fid):
        raise StorageError("DRIVE_UNREACHABLE", "sin conexion", reintentable=True)

    drive.read_file = caido

    with pytest.raises(ContenidoNoDisponible):
        _lector(escenario).resolver(
            escenario["filas"], user_id=escenario["user_id"], almacenamiento=drive
        )


def test_un_segmento_manipulado_se_rechaza(escenario, session):
    """No se muestran mensajes corruptos: se dice que lo estan."""
    roto = bytearray(escenario["drive"].archivos["seg-1"])
    roto[-1] ^= 0x01
    escenario["drive"].archivos["seg-1"] = bytes(roto)

    with pytest.raises(ContenidoCorrupto):
        _lector(escenario).resolver(
            escenario["filas"],
            user_id=escenario["user_id"],
            almacenamiento=escenario["drive"],
        )


def test_un_segmento_sin_subir_todavia_se_dice(escenario, session):
    escenario["segmento"].drive_file_id = None
    session.flush()

    with pytest.raises(ContenidoNoDisponible):
        _lector(escenario).resolver(
            escenario["filas"],
            user_id=escenario["user_id"],
            almacenamiento=escenario["drive"],
        )


def test_lo_que_aun_no_esta_subido_se_sirve_de_postgresql(escenario, session):
    """Etapa transitoria, y se marca como tal para no confundirla."""
    for fila in escenario["filas"]:
        fila.storage_status = "local"
        fila.segment_id = None
    session.flush()

    resueltos = _lector(escenario).resolver(
        escenario["filas"], user_id=escenario["user_id"], almacenamiento=escenario["drive"]
    )
    assert all(r.fuente == "local" for r in resueltos.values())
    assert resueltos[escenario["filas"][0].id].text == "primero"


# ---------------------------------------------------------------------------
# Propiedad
# ---------------------------------------------------------------------------


def test_otro_usuario_no_puede_leer_el_segmento(escenario, runtime):
    """Se comprueba aunque el chat ya se haya filtrado: son dos cosas."""
    otro = runtime.auth.register(email=_correo(), password=CLAVE)

    with pytest.raises(ContenidoNoDisponible):
        _lector(escenario).resolver(
            escenario["filas"],
            user_id=otro.user_id,
            almacenamiento=escenario["drive"],
        )


def test_lo_guardado_en_drive_no_se_puede_leer_a_simple_vista(escenario):
    contenido = escenario["drive"].archivos["seg-1"]
    for texto in escenario["textos"]:
        assert texto.encode("utf-8") not in contenido
