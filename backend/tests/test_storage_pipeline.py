"""Segmentos, cola de subidas y trabajador. Sin Google real.

Se sustituye Drive por un doble en memoria. Lo que se prueba es NUESTRA
logica, que es donde estan los fallos que importan: perder trabajos al
reiniciar, subir dos veces el mismo segmento, borrar un archivo local antes de
que el remoto este confirmado.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.storage import segments as seg
from app.storage.encryption import StorageEncryption, nueva_dek
from app.storage.interface import (
    ArchivoSubido,
    StorageAuthError,
    StorageError,
    StorageQuotaError,
)
from app.storage.jobs import ESPERAS, MAX_INTENTOS, StorageJobQueue, espera_para

CLAVE = "una contrasena larga"


# ---------------------------------------------------------------------------
# Doble de Drive
# ---------------------------------------------------------------------------


class DriveFalso:
    """Guarda en un diccionario y cuenta lo que le piden."""

    def __init__(self, *, fallos: list[Exception] | None = None):
        self.archivos: dict[str, bytes] = {}
        self.subidas = 0
        self.carpetas: dict[str, str] = {}
        self._fallos = list(fallos or [])

    # -- carpetas --
    def ensure_user_storage(self) -> str:
        return self.carpetas.setdefault("", "raiz")

    def carpeta_de_mensajes(self, cuenta: str, chat: str) -> str:
        return self.carpetas.setdefault(f"{cuenta}/{chat}/messages", f"carpeta-{chat}-m")

    def carpeta_de_multimedia(self, cuenta: str, chat: str) -> str:
        return self.carpetas.setdefault(f"{cuenta}/{chat}/media", f"carpeta-{chat}-x")

    # -- escritura --
    def store_bytes(self, *, carpeta, nombre, datos, propiedades, mime_type="x"):
        if self._fallos:
            raise self._fallos.pop(0)
        self.subidas += 1
        file_id = f"file-{len(self.archivos) + 1}"
        self.archivos[file_id] = datos
        return ArchivoSubido(file_id=file_id, size=len(datos))

    def store_stream(self, *, carpeta, nombre, origen, tamano, propiedades, mime_type="x"):
        if self._fallos:
            raise self._fallos.pop(0)
        self.subidas += 1
        cuerpo = b"".join(origen)
        file_id = f"file-{len(self.archivos) + 1}"
        self.archivos[file_id] = cuerpo
        return ArchivoSubido(file_id=file_id, size=len(cuerpo))

    # -- lectura --
    def read_file(self, file_id):
        return self.archivos[file_id]

    def read_range(self, file_id, inicio, fin):
        return self.archivos[file_id][inicio : fin + 1]

    def exists(self, file_id):
        return file_id in self.archivos

    def delete(self, file_id):
        return self.archivos.pop(file_id, None) is not None

    def health_check(self):
        return {"user": {}}


class GoogleFalso:
    def __init__(self, token: str | None = "token"):
        self.token = token
        self.invalidado = False

    def access_token(self, user_id):
        return self.token

    def marcar_invalido(self, user_id):
        self.invalidado = True


# ---------------------------------------------------------------------------
# Preparacion
# ---------------------------------------------------------------------------


@pytest.fixture
def entorno(runtime, session, settings):
    """Usuario, cuenta y chat listos, con el almacenamiento montado."""
    from app.models import Chat, WhatsAppAccount

    runtime._montar_cuentas()
    inicio = runtime.auth.register(
        email=f"st-{uuid.uuid4().hex[:8]}@example.com", password=CLAVE
    )
    cuenta = WhatsAppAccount(
        user_id=inicio.user_id,
        session_status="linked",
        session_storage_key=f"users/{inicio.user_id}",
    )
    session.add(cuenta)
    session.flush()
    chat = Chat(
        jid=f"5730{uuid.uuid4().hex[:8]}@s.whatsapp.net",
        chat_type="individual",
        whatsapp_account_id=cuenta.id,
    )
    session.add(chat)
    session.flush()
    return {
        "user_id": inicio.user_id,
        "account_id": cuenta.id,
        "chat": chat,
        "storage": runtime.storage,
        "runtime": runtime,
    }


def _mensaje(session, chat, texto: str, timestamp: int = 1_760_000_000):
    from app.models import Message

    fila = Message(
        chat_id=chat.id,
        chat_jid=chat.jid,
        whatsapp_message_id=f"WAMID{uuid.uuid4().hex[:18].upper()}",
        timestamp=timestamp,
        from_me=False,
        message_type="text",
        text=texto,
        source="live",
    )
    session.add(fila)
    session.flush()
    return fila


# ---------------------------------------------------------------------------
# Formato de segmento
# ---------------------------------------------------------------------------


def test_un_segmento_es_una_linea_por_mensaje():
    bloque = seg.SegmentoAbierto(chat_id=1, chat_jid="x@s", sequence_number=1)
    for i in range(3):
        bloque.anadir(
            seg.LineaDeMensaje(
                id=i, wamid=f"W{i}", timestamp=1000 + i, from_me=False,
                sender="a@s", type="text", text=f"hola {i}",
            )
        )
    lineas = list(seg.leer_lineas(bloque.contenido()))

    assert len(lineas) == 3
    assert lineas[1]["text"] == "hola 1"


def test_la_posicion_permite_encontrar_un_mensaje_sin_recorrerlo_todo():
    bloque = seg.SegmentoAbierto(chat_id=1, chat_jid="x@s", sequence_number=1)
    posiciones = [
        bloque.anadir(
            seg.LineaDeMensaje(
                id=i, wamid=None, timestamp=1, from_me=False, sender=None,
                type="text", text=f"m{i}",
            )
        )
        for i in range(5)
    ]
    assert posiciones == [0, 1, 2, 3, 4]
    assert seg.linea_numero(bloque.contenido(), 3)["text"] == "m3"


def test_el_segmento_NUNCA_lleva_secretos():
    """Una copia con conversaciones no puede llevar ademas con que descifrarlas."""
    linea = seg.LineaDeMensaje(
        id=1, wamid="W", timestamp=1, from_me=False, sender="a@s", type="image",
        metadata={
            "media_key": b"SECRETO".hex(),
            "file_enc_sha256": "otro",
            "access_token": "ya29.x",
            "width": 100,
        },
    )
    cuerpo = str(linea.to_dict())

    for prohibido in ("media_key", "file_enc_sha256", "access_token"):
        assert prohibido not in cuerpo
    assert "width" in cuerpo, "lo que no es secreto si se conserva"


def test_comprimir_es_determinista():
    """Sin esto, la huella no serviria para comparar dos veces lo mismo."""
    datos = b'{"a":1}\n' * 100
    assert seg._comprimir(datos) == seg._comprimir(datos)


def test_ida_y_vuelta_con_cifrado():
    cifrado = StorageEncryption(nueva_dek())
    contenido = b'{"id":1}\n{"id":2}\n'
    paquete = seg.empaquetar(contenido, encryption=cifrado)

    assert b'"id"' not in paquete.datos, "el contenido no puede verse"
    assert seg.desempaquetar(paquete.datos, encryption=cifrado) == contenido


def test_se_guardan_las_dos_huellas():
    """Una comprueba el contenido; la otra, la subida, sin descifrar nada."""
    paquete = seg.empaquetar(b'{"id":1}\n', encryption=StorageEncryption(nueva_dek()))
    assert paquete.sha256_claro != paquete.sha256_cifrado
    assert len(paquete.sha256_claro) == 64


def test_sin_cifrado_tambien_lleva_cabecera():
    """Leer un archivo no puede depender de recordar como se escribio."""
    contenido = b'{"id":1}\n'
    paquete = seg.empaquetar(contenido, encryption=None)
    assert paquete.cifrado is False
    assert seg.desempaquetar(paquete.datos, encryption=None) == contenido


def test_el_nombre_ordena_igual_que_el_tiempo():
    nombres = [seg.nombre_de_archivo(n) for n in (1, 2, 10, 100)]
    assert nombres == sorted(nombres), "el orden alfabetico tiene que ser el cronologico"


# ---------------------------------------------------------------------------
# Cuando se cierra un segmento
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cuenta,bytes_,edad,esperado",
    [
        (1000, 10, 0, "lleno"),
        (5, 9_000_000, 0, "tamano"),
        (5, 10, 120, "edad"),
        (5, 10, 0, None),
    ],
)
def test_los_tres_motivos_de_cierre(cuenta, bytes_, edad, esperado):
    bloque = seg.SegmentoAbierto(
        chat_id=1, chat_jid="x", sequence_number=1, abierto_en=0.0
    )
    bloque.lineas = [b"x"] * cuenta
    bloque.bytes_sin_comprimir = bytes_

    assert (
        bloque.debe_cerrarse(
            max_mensajes=1000, max_bytes=5_242_880, max_edad=60, ahora=edad
        )
        == esperado
    )


def test_la_edad_evita_que_un_chat_tranquilo_no_suba_nunca():
    """Sin limite de edad, un chat con poco trafico no llegaria nunca a Drive."""
    bloque = seg.SegmentoAbierto(
        chat_id=1, chat_jid="x", sequence_number=1, abierto_en=0.0
    )
    bloque.lineas = [b"un solo mensaje"]
    assert (
        bloque.debe_cerrarse(
            max_mensajes=1000, max_bytes=5_242_880, max_edad=60, ahora=61
        )
        == "edad"
    )


# ---------------------------------------------------------------------------
# La cola
# ---------------------------------------------------------------------------


def test_encolar_dos_veces_la_misma_entidad_no_duplica(entorno, session):
    """Sin esto, cada reintento subiria una copia nueva a Drive."""
    cola = StorageJobQueue(entorno["runtime"].database)
    for _ in range(3):
        cola.encolar(
            session,
            user_id=entorno["user_id"],
            account_id=entorno["account_id"],
            job_type="message_segment",
            entity_id="mismo-segmento",
        )
    from sqlalchemy import func, select

    from app.models.storage import StorageJob

    total = session.execute(
        select(func.count()).select_from(StorageJob).where(
            StorageJob.entity_id == "mismo-segmento"
        )
    ).scalar()
    assert total == 1


def test_un_trabajo_completado_no_se_reencola(entorno, session):
    cola = StorageJobQueue(entorno["runtime"].database)
    trabajo = cola.encolar(
        session, user_id=entorno["user_id"], account_id=entorno["account_id"],
        job_type="media", entity_id="42",
    )
    trabajo.status = "complete"
    session.flush()

    otra_vez = cola.encolar(
        session, user_id=entorno["user_id"], account_id=entorno["account_id"],
        job_type="media", entity_id="42",
    )
    assert otra_vez.status == "complete", "ya estaba subido; no se vuelve a subir"


def test_las_esperas_crecen_y_tienen_tope():
    esperas = [espera_para(i) for i in range(len(ESPERAS) + 3)]
    assert esperas[0] < esperas[3], "tienen que crecer"
    assert max(esperas) <= ESPERAS[-1] * 1.3, "y tener tope"


def test_se_respeta_lo_que_pide_google():
    """Adelantarse a ``Retry-After`` solo consigue otro rechazo."""
    assert espera_para(0, retry_after=120.0) == 120.0


def test_las_esperas_llevan_dispersion():
    """Tras un corte, no pueden salir todos los trabajos en el mismo instante."""
    muestras = {espera_para(2) for _ in range(20)}
    assert len(muestras) > 1


def test_un_trabajo_agotado_no_pierde_el_contenido(entorno, session):
    from app.models.storage import StorageJob

    cola = StorageJobQueue(entorno["runtime"].database)
    trabajo = cola.encolar(
        session, user_id=entorno["user_id"], account_id=entorno["account_id"],
        job_type="media", entity_id="99",
    )
    trabajo.attempts = MAX_INTENTOS
    session.flush()

    cola.reintentar(trabajo.id, "no hay manera")
    session.expire_all()
    assert session.get(StorageJob, trabajo.id).status == "failed"


def test_pausar_no_borra_nada(entorno, session):
    """Con Google revocado se para, no se tira lo pendiente."""
    from app.models.storage import StorageJob

    cola = StorageJobQueue(entorno["runtime"].database)
    trabajo = cola.encolar(
        session, user_id=entorno["user_id"], account_id=entorno["account_id"],
        job_type="media", entity_id="7",
    )
    cola.pausar_todos(entorno["user_id"], "acceso revocado")
    session.expire_all()

    fila = session.get(StorageJob, trabajo.id)
    assert fila is not None, "el trabajo NO se borra"
    assert fila.status == "paused"

    cola.reanudar(entorno["user_id"])
    session.expire_all()
    assert session.get(StorageJob, trabajo.id).status == "pending"


# ---------------------------------------------------------------------------
# Recuperacion tras un cierre brusco
# ---------------------------------------------------------------------------


def test_un_trabajo_a_medias_se_recupera(entorno, session):
    """El proceso murio a mitad de una subida: nadie mas va a terminarla."""
    from datetime import datetime, timedelta, timezone

    from app.models.storage import StorageJob

    cola = StorageJobQueue(entorno["runtime"].database)
    trabajo = cola.encolar(
        session, user_id=entorno["user_id"], account_id=entorno["account_id"],
        job_type="media", entity_id="8",
    )
    trabajo.status = "processing"
    trabajo.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
    session.flush()

    assert cola.recuperar_huerfanos(antiguedad_segundos=60) >= 1
    session.expire_all()
    assert session.get(StorageJob, trabajo.id).status == "pending"


def test_no_se_le_roba_el_trabajo_a_un_worker_vivo(entorno, session):
    from app.models.storage import StorageJob

    cola = StorageJobQueue(entorno["runtime"].database)
    trabajo = cola.encolar(
        session, user_id=entorno["user_id"], account_id=entorno["account_id"],
        job_type="media", entity_id="9",
    )
    trabajo.status = "processing"
    session.flush()

    cola.recuperar_huerfanos(antiguedad_segundos=300)
    session.expire_all()
    assert session.get(StorageJob, trabajo.id).status == "processing"


def test_un_segmento_se_rehace_desde_postgresql(entorno, session):
    """Lo que hace que un cierre brusco no pierda mensajes."""
    from app.models.storage import MessageSegment

    almacenamiento = entorno["storage"]
    fila = MessageSegment(
        user_id=entorno["user_id"],
        whatsapp_account_id=entorno["account_id"],
        chat_id=entorno["chat"].id,
        chat_jid=entorno["chat"].jid,
        sequence_number=1,
    )
    session.add(fila)
    session.flush()

    for i in range(3):
        mensaje = _mensaje(session, entorno["chat"], f"mensaje {i}")
        mensaje.segment_id = fila.id
        mensaje.segment_index = i
    session.flush()

    contenido = almacenamiento.reconstruir(fila.id)
    lineas = list(seg.leer_lineas(contenido))
    assert [linea["text"] for linea in lineas] == ["mensaje 0", "mensaje 1", "mensaje 2"]


# ---------------------------------------------------------------------------
# Contrapresion
# ---------------------------------------------------------------------------


def test_si_se_acumula_demasiado_se_avisa_sin_borrar(entorno, session, settings):
    """Crecer sin limite llena el disco; borrar en silencio pierde mensajes."""
    import dataclasses

    from app.storage.service import StorageBlocked, StorageService

    apretado = dataclasses.replace(settings, max_pending_storage_bytes=100)
    servicio = StorageService(entorno["runtime"].database, apretado)
    servicio.jobs.encolar(
        session, user_id=entorno["user_id"], account_id=entorno["account_id"],
        job_type="media", entity_id="grande", payload_bytes=10_000,
    )

    with pytest.raises(StorageBlocked) as fallo:
        servicio.comprobar_espacio(entorno["user_id"])
    assert "NO se ha borrado" in str(fallo.value)


def test_sin_limite_configurado_no_bloquea(entorno, session, settings):
    import dataclasses

    from app.storage.service import StorageService

    servicio = StorageService(
        entorno["runtime"].database,
        dataclasses.replace(settings, max_pending_storage_bytes=0),
    )
    servicio.comprobar_espacio(entorno["user_id"])  # no lanza


# ---------------------------------------------------------------------------
# Claves por usuario
# ---------------------------------------------------------------------------


def test_cada_usuario_recibe_su_propia_clave(entorno, session, runtime):
    otro = runtime.auth.register(
        email=f"otro-{uuid.uuid4().hex[:8]}@example.com", password=CLAVE
    )
    almacenamiento = entorno["storage"]

    assert almacenamiento.clave_de(entorno["user_id"]) != almacenamiento.clave_de(
        otro.user_id
    )


def test_la_clave_de_un_usuario_es_estable(entorno):
    almacenamiento = entorno["storage"]
    assert almacenamiento.clave_de(entorno["user_id"]) == almacenamiento.clave_de(
        entorno["user_id"]
    )


def test_la_clave_no_se_guarda_en_claro(entorno, session):
    from sqlalchemy import select

    from app.models.storage import UserStorageKey

    dek = entorno["storage"].clave_de(entorno["user_id"])
    guardada = session.execute(
        select(UserStorageKey.encrypted_dek).where(
            UserStorageKey.user_id == entorno["user_id"]
        )
    ).scalar_one()

    assert dek not in guardada
