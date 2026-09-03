"""Buscar anclas reales en las mutaciones de app-state. SOLO observa.

POR QUE HACE FALTA OTRA FUENTE
------------------------------
33 chats llegaron del ``INITIAL_BOOTSTRAP`` como pura metadata. Se auditaron
los 29 campos que trae de verdad una ``Conversation`` (no los 5 que modela
pywhats) y para un chat sin mensajes solo hay::

    campo 39  telefono del contacto      campo 44  categoria
    campo 49  LID del contacto           campo 23  32 bytes opacos
    resto     contadores y banderas

Ni un identificador de mensaje. Rastrear otra vez PostgreSQL, los blobs o los
alias no puede dar nada: ya se comprobo que ahi no esta.

QUE ES EL APP-STATE
-------------------
Otro canal. WhatsApp lo usa para propagar entre tus dispositivos lo que haces
con los chats: silenciar, fijar, archivar, marcar como leido, borrar,
destacar. Varias de esas acciones llevan claves de mensaje REALES, porque
"marcar como leido hasta aqui" necesita decir hasta donde.

En esta sesion esta funcionando: 9 claves, 4 colecciones, 154 mutaciones.
pywhats traduce cinco acciones y ninguna de las que llevan claves, pero
protobuf conserva los campos que no conoce.

COMO SE BUSCA
-------------
NO se rastrean cadenas sueltas. Se busca la ESTRUCTURA de un ``MessageKey``,
que el paquete si modela::

    1 remote_jid   2 from_me   3 id   4 participant

Un submensaje cuyos campos caben en ese molde, con un JID en el 1 y un
identificador que pase el filtro del backfill en el 3, es un ``MessageKey``.
Y trae su propio ``remote_jid``, que es lo que permite DEMOSTRAR a que chat
pertenece en vez de suponerlo por el indice de la mutacion.

MODO OBSERVACION
----------------
Este modulo no escribe en PostgreSQL, no cambia ningun estado de historial, no
encola nada y no pide nada al servidor. Mide y calla. Decidir si esta via
sirve es lo que viene despues, y con datos.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("BACKFILL")

_MARKER = "_whatsapp_backup_appstate_seeds"

# Campos de ``MessageKey`` segun el descriptor instalado. No se adivinan.
CAMPO_REMOTE_JID = 1
CAMPO_FROM_ME = 2
CAMPO_ID = 3
CAMPO_PARTICIPANT = 4
_CAMPOS_MESSAGE_KEY = {CAMPO_REMOTE_JID, CAMPO_FROM_ME, CAMPO_ID, CAMPO_PARTICIPANT}

# Filtro grueso; el fino lo hace ``is_valid_history_cursor_id``, que es el
# mismo que decide si el backfill puede anclarse en ese identificador.
_PARECE_ID = re.compile(r"^[0-9A-Fa-f]{16,32}$")

# Nunca sirven como ancla de una conversacion.
_SERVIDORES_EXCLUIDOS = ("broadcast", "newsletter")


@dataclass
class SeedCandidate:
    """Una clave de mensaje real, con la prueba de a que chat pertenece."""

    chat_jid: str
    message_id: str
    from_me: bool
    source: str = "app_state"
    collection: str | None = None
    index_type: str | None = None
    timestamp: int | None = None

    @property
    def huella(self) -> str:
        """Identificador acortado, para poder registrarlo sin exponerlo."""
        return hashlib.sha256(self.message_id.encode()).hexdigest()[:8]


@dataclass
class ScanReport:
    """Lo medido. Es la respuesta a "sirve esta via o no"."""

    mutations_scanned: int = 0
    mutations_with_message_key: int = 0
    real_candidates: int = 0
    duplicates: int = 0
    rejected_not_real_id: int = 0
    rejected_wrong_chat: int = 0
    rejected_broadcast: int = 0
    por_chat: dict[str, list[SeedCandidate]] = field(default_factory=dict)

    @property
    def unique_chats(self) -> int:
        return len(self.por_chat)

    def resumen(self) -> str:
        return (
            f"mutaciones={self.mutations_scanned} "
            f"con_clave={self.mutations_with_message_key} "
            f"candidatos={self.real_candidates} "
            f"chats={self.unique_chats} duplicados={self.duplicates} "
            f"descartados(id={self.rejected_not_real_id} "
            f"otro_chat={self.rejected_wrong_chat} "
            f"difusion={self.rejected_broadcast})"
        )


# Que numeros de campo aparecen de verdad en los SyncActionValue, y cuantas
# veces. Sirve para distinguir "mi detector no encontro claves" de "no hay
# claves": si aqui solo salen los campos que pywhats ya modela, es que el
# servidor no manda nada mas.
_campos_vistos: dict[int, int] = {}


def campos_vistos() -> dict[int, int]:
    return dict(_campos_vistos)


_informe = ScanReport()


def report() -> ScanReport:
    """Lo encontrado hasta ahora."""
    return _informe


def reset() -> None:
    global _informe
    _informe = ScanReport()
    _campos_vistos.clear()


# ---------------------------------------------------------------------------
# Deteccion estructural
# ---------------------------------------------------------------------------


def _texto(payload: bytes | None) -> str | None:
    if not payload:
        return None
    try:
        valor = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return valor if valor.isprintable() else None


def as_message_key(payload: bytes) -> SeedCandidate | None:
    """``SeedCandidate`` si estos bytes son un ``MessageKey`` con id real.

    El molde es el del descriptor: ningun campo fuera de {1,2,3,4}, un JID en
    el 1 y un identificador en el 3. Se exigen AMBOS: sin ``remote_jid`` no se
    puede demostrar a que chat pertenece, y sin ``id`` no hay ancla.
    """
    from app.core.message_parser import top_level_fields

    try:
        campos = top_level_fields(payload)
    except Exception:  # noqa: BLE001
        return None
    if not campos:
        return None
    if any(numero not in _CAMPOS_MESSAGE_KEY for numero, _w, _p in campos):
        return None

    remote = id_mensaje = None
    from_me = False
    for numero, wire, contenido in campos:
        if numero == CAMPO_REMOTE_JID:
            remote = _texto(contenido)
        elif numero == CAMPO_ID:
            id_mensaje = _texto(contenido)
        elif numero == CAMPO_FROM_ME and wire == 0:
            from_me = bool(contenido)

    if not remote or "@" not in remote or not id_mensaje:
        return None
    if not _PARECE_ID.match(id_mensaje):
        return None

    return SeedCandidate(
        chat_jid=remote, message_id=id_mensaje, from_me=from_me
    )


def _recorrer(datos: bytes, profundidad: int = 0) -> list[SeedCandidate]:
    """Todos los ``MessageKey`` del protobuf, bajando por los submensajes."""
    if profundidad > 5 or not datos:
        return []

    from app.core.message_parser import top_level_fields

    encontrados: list[SeedCandidate] = []
    try:
        campos = top_level_fields(datos)
    except Exception:  # noqa: BLE001
        return []

    for _numero, _wire, payload in campos:
        if not payload:
            continue
        clave = as_message_key(payload)
        if clave is not None:
            encontrados.append(clave)
            continue
        encontrados.extend(_recorrer(payload, profundidad + 1))
    return encontrados


# ---------------------------------------------------------------------------
# Validacion
# ---------------------------------------------------------------------------


def _usuario(jid: str) -> str:
    return jid.split("@")[0].split(":")[0].split(".")[0]


def _mismo_chat(clave_jid: str, indice_jid: str) -> bool:
    """Si la clave pertenece al chat que la mutacion dice tocar.

    Se compara por usuario, no por cadena: un mismo contacto aparece por
    telefono y por LID, y son el mismo chat. Lo que NO se hace es dar por
    bueno un ``remote_jid`` que no tenga nada que ver, porque entonces
    estariamos anclando un chat con el mensaje de otro.
    """
    if clave_jid == indice_jid:
        return True
    return _usuario(clave_jid) == _usuario(indice_jid)


def inspect(mutation: Any, *, collection: str | None = None) -> list[SeedCandidate]:
    """Mide lo que trae esta mutacion. Nunca lanza y nunca escribe."""
    try:
        return _inspect(mutation, collection)
    except Exception:  # noqa: BLE001 - observar no puede romper el app-state
        log.debug("No se pudo inspeccionar una mutacion de app-state")
        return []


def _inspect(mutation: Any, collection: str | None) -> list[SeedCandidate]:
    accion = getattr(mutation, "action", None)
    indice = list(getattr(mutation, "index", None) or [])
    if accion is None:
        return []

    _informe.mutations_scanned += 1

    crudo = accion.SerializeToString()
    # Se anota TODO campo presente, lo modele pywhats o no.
    from app.core.message_parser import top_level_fields

    try:
        for numero, _w, _p in top_level_fields(crudo):
            _campos_vistos[numero] = _campos_vistos.get(numero, 0) + 1
    except Exception:  # noqa: BLE001
        pass

    claves = _recorrer(crudo)
    if not claves:
        return []
    _informe.mutations_with_message_key += 1

    from app.services.repository import is_valid_history_cursor_id

    indice_jid = indice[1] if len(indice) > 1 else None
    tipo = indice[0] if indice else None
    aceptados: list[SeedCandidate] = []

    for clave in claves:
        servidor = clave.chat_jid.partition("@")[2]
        if servidor in _SERVIDORES_EXCLUIDOS:
            # Un estado o un canal no son una conversacion, y su historial no
            # se pide igual. Anclar un chat con esto seria mezclar cosas
            # distintas.
            _informe.rejected_broadcast += 1
            continue

        if not is_valid_history_cursor_id(clave.message_id):
            _informe.rejected_not_real_id += 1
            continue

        if indice_jid and not _mismo_chat(clave.chat_jid, indice_jid):
            # La clave existe, pero apunta a otra conversacion. No se puede
            # demostrar la pertenencia, asi que no vale como ancla.
            _informe.rejected_wrong_chat += 1
            continue

        clave.collection = collection
        clave.index_type = tipo
        ya = _informe.por_chat.setdefault(clave.chat_jid, [])
        if any(c.message_id == clave.message_id for c in ya):
            _informe.duplicates += 1
            continue

        ya.append(clave)
        _informe.real_candidates += 1
        aceptados.append(clave)
        log.info(
            "[SEED] app-state: chat=%s real_message_id=True source=app_state "
            "collection=%s index_type=%s id_fp=%s from_me=%s",
            _corto(clave.chat_jid),
            collection or "?",
            tipo or "?",
            clave.huella,
            clave.from_me,
        )

    return aceptados


# ---------------------------------------------------------------------------
# Instalacion
# ---------------------------------------------------------------------------


def apply(settings: Any) -> bool:
    """Envuelve el traductor de mutaciones. Transparente por construccion.

    ``app_state_mutation_to_event`` se llama UNA vez por mutacion, despues de
    que pywhats haya descifrado y verificado el MAC. Se observa lo que ya
    viene autenticado y se devuelve exactamente lo que devolvia: el evento de
    salida es identico, y hay una prueba que lo comprueba.
    """
    import pywhats.appstate.events as events_module

    original = events_module.app_state_mutation_to_event
    if getattr(original, _MARKER, False):
        return True

    def app_state_mutation_to_event(mutation: Any):  # type: ignore[no-untyped-def]
        inspect(mutation)
        return original(mutation)

    setattr(app_state_mutation_to_event, _MARKER, True)
    # pywhats lo importa DENTRO de la funcion que lo usa (``client.py:814``),
    # asi que reemplazar el atributo del modulo basta: la proxima llamada
    # resuelve el nombre otra vez.
    events_module.app_state_mutation_to_event = app_state_mutation_to_event

    log.info(
        "Busqueda de anclas en app-state activada (SOLO observa: no escribe "
        "en la base, no cambia estados y no pide historial)"
    )
    return True


def log_summary() -> None:
    """Vuelca lo medido. Se llama cuando el app-state ya ha pasado."""
    if not _informe.mutations_scanned:
        return
    log.info("[SEED] app-state: %s", _informe.resumen())
    for jid, candidatos in sorted(
        _informe.por_chat.items(), key=lambda x: -len(x[1])
    ):
        log.info("[SEED] app-state: %s -> %d ancla(s)", _corto(jid), len(candidatos))


def _corto(jid: str) -> str:
    user, _, server = jid.partition("@")
    return f"{user[:6]}***@{server}" if server else f"{user[:6]}***"
