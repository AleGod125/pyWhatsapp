"""Interpretacion de los eventos de sistema del chat.

QUE PROBLEMA RESUELVE
---------------------
Todo lo que no era una burbuja normal se pintaba como ``[mensaje de sistema]``.
En el telefono esos mismos eventos se leen: "llamada perdida", "los mensajes
temporales se activaron", "eliminaste este mensaje". Aqui se recupera ese
significado.

DE DONDE SALE EL SIGNIFICADO
----------------------------
Del PROTOBUF, nunca del texto ni de la fecha. Hay exactamente dos fuentes:

1. ``WebMessageInfo.messageStubType`` (campo 24) con sus
   ``messageStubParameters`` (campo 25). Es lo que WhatsApp usa para los
   avisos del chat: altas y bajas de grupo, cifrado, llamadas perdidas.
2. ``Message.call`` (campo 10) y ``Message.callLogMesssage`` (campo 69),
   que son mensajes de verdad y describen una llamada concreta.

QUE ESTA VERIFICADO Y QUE NO
----------------------------
Los stub types marcados VERIFICADO se comprobaron contra este backup
(2026-09-02, 93 filas de sistema). La comprobacion no fue "parece que encaja"
sino que el contenido de los parametros coincide con lo que el nombre exige::

    stub 72  parametro '604800'                 -> 7 dias en segundos
    stub  1  parametro '2A7311D86922D5...'      -> un ID de mensaje
    stub 27  parametro '...@lid'                -> un participante
    stub 24  parametro 'CALENDARIO CUM...'      -> una descripcion de grupo
    stub 39  sin parametros                     -> aviso de cifrado

Los marcados REFERENCIA vienen de la definicion publica de
``WebMessageInfo.StubType`` (whatsmeow, ``proto/waWeb``) y NO aparecen en
este backup, asi que no se han podido confirmar con datos propios.

REGLA QUE NO SE ROMPE
---------------------
Un stub que no este en la tabla NO se adivina. Se pinta como
``[Evento del sistema]``, se conserva su numero y ``raw_proto`` queda intacto
para poder interpretarlo mas adelante. Inventar un nombre seria peor que
admitir que no se sabe.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.logging_setup import get_logger
from app.message_parser import top_level_fields, unwrap_message

log = get_logger("SYNC")


# ---------------------------------------------------------------------------
# Tabla de stub types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StubInfo:
    """Que significa un ``messageStubType``."""

    name: str
    kind: str
    icon: str
    label: str
    # ``False`` para los avisos que WhatsApp procesa pero no ensena en el
    # hilo. Se guardan igual: no verse no es lo mismo que no existir.
    visible: bool = True
    # Verificado contra los datos de este backup, o solo de referencia.
    verified: bool = False


def _stub(
    name: str, kind: str, icon: str, label: str, *, visible: bool = True,
    verified: bool = False,
) -> StubInfo:
    return StubInfo(name, kind, icon, label, visible, verified)


STUB_TYPES: dict[int, StubInfo] = {
    # --- VERIFICADO contra este backup ---
    1: _stub("REVOKE", "revoke", "🚫", "Mensaje eliminado", verified=True),
    24: _stub("GROUP_CHANGE_DESCRIPTION", "group", "👥",
              "Se cambio la descripcion del grupo", verified=True),
    27: _stub("GROUP_PARTICIPANT_ADD", "group", "👥",
              "Se anadio un participante", verified=True),
    28: _stub("GROUP_PARTICIPANT_REMOVE", "group", "👥",
              "Se elimino un participante", verified=True),
    39: _stub("E2E_ENCRYPTED", "encryption", "🔒",
              "Los mensajes estan cifrados de extremo a extremo", verified=True),
    72: _stub("CHANGE_EPHEMERAL_SETTING", "ephemeral", "⏳",
              "Cambio la configuracion de mensajes temporales", verified=True),
    75: _stub("E2E_ENCRYPTED_NOW", "encryption", "🔒",
              "Este chat pasa a estar cifrado de extremo a extremo", verified=True),

    # --- REFERENCIA (definicion publica; sin datos locales que lo confirmen) ---
    2: _stub("CIPHERTEXT", "pending", "⏱", "Esperando este mensaje"),
    20: _stub("GROUP_CREATE", "group", "👥", "Se creo el grupo"),
    21: _stub("GROUP_CHANGE_SUBJECT", "group", "👥", "Se cambio el nombre del grupo"),
    22: _stub("GROUP_CHANGE_ICON", "group", "👥", "Se cambio la foto del grupo"),
    23: _stub("GROUP_CHANGE_INVITE_LINK", "group", "👥",
              "Se cambio el enlace de invitacion"),
    25: _stub("GROUP_CHANGE_RESTRICT", "group", "👥", "Cambiaron los permisos del grupo"),
    26: _stub("GROUP_CHANGE_ANNOUNCE", "group", "👥",
              "Cambio quien puede enviar mensajes"),
    29: _stub("GROUP_PARTICIPANT_PROMOTE", "group", "👥", "Ahora es administrador"),
    30: _stub("GROUP_PARTICIPANT_DEMOTE", "group", "👥", "Ya no es administrador"),
    31: _stub("GROUP_PARTICIPANT_INVITE", "group", "👥", "Se invito a un participante"),
    32: _stub("GROUP_PARTICIPANT_LEAVE", "group", "👥", "Salio del grupo"),
    33: _stub("GROUP_PARTICIPANT_CHANGE_NUMBER", "group", "👥",
              "Un participante cambio de numero"),
    37: _stub("GENERIC_NOTIFICATION", "notice", "ℹ", "Aviso del chat"),
    38: _stub("E2E_IDENTITY_CHANGED", "encryption", "🔒",
              "Cambio el codigo de seguridad"),
    42: _stub("INDIVIDUAL_CHANGE_NUMBER", "notice", "ℹ", "Cambio de numero"),
    43: _stub("GROUP_DELETE", "group", "👥", "Se elimino el grupo"),
    68: _stub("OVERSIZED", "notice", "ℹ", "Mensaje demasiado grande"),
    73: _stub("E2E_DEVICE_CHANGED", "encryption", "🔒", "Cambio de dispositivo"),
    74: _stub("VIEWED_ONCE", "notice", "ℹ", "Mensaje de una sola vez"),

    # --- Llamadas (REFERENCIA). En este backup no hay ninguna: WhatsApp no
    #     entrego registros de llamada al dispositivo vinculado. Se dejan
    #     implementadas para cuando lleguen, no como suposicion de que estan.
    40: _stub("CALL_MISSED_VOICE", "call_missed_voice", "📞", "Llamada perdida"),
    41: _stub("CALL_MISSED_VIDEO", "call_missed_video", "📹", "Videollamada perdida"),
    45: _stub("CALL_MISSED_GROUP_VOICE", "call_missed_voice", "📞",
              "Llamada grupal perdida"),
    46: _stub("CALL_MISSED_GROUP_VIDEO", "call_missed_video", "📹",
              "Videollamada grupal perdida"),
}

# Conjuntos utiles para el resto de la aplicacion.
CALL_KINDS = frozenset(
    {"call_missed_voice", "call_missed_video", "call_voice", "call_video"}
)


# ---------------------------------------------------------------------------
# CallLogMessage  (Message campo 69)
# ---------------------------------------------------------------------------

# Numeros de campo de ``CallLogMessage`` segun la definicion publica de
# whatsmeow (``WAWebProtobufsE2E.proto``). Aqui NO hay verificacion local:
# este backup no contiene ni un solo mensaje de este tipo. Se lee de forma
# defensiva y, si algo no encaja, se degrada a "Llamada" a secas.
_CALL_LOG_IS_VIDEO = 1
_CALL_LOG_OUTCOME = 2
_CALL_LOG_DURATION = 3

# CallLogMessage.CallOutcome
_OUTCOME_CONNECTED = 0
_OUTCOME_MISSED = 1
_OUTCOME_FAILED = 2
_OUTCOME_REJECTED = 3

_OUTCOME_LABELS = {
    _OUTCOME_CONNECTED: "Llamada",
    _OUTCOME_MISSED: "Llamada perdida",
    _OUTCOME_FAILED: "Llamada fallida",
    _OUTCOME_REJECTED: "Llamada rechazada",
}

# Campos de ``Message`` que describen una llamada.
CALL_MESSAGE_FIELD = 10       # call
CALL_LOG_MESSAGE_FIELD = 69   # callLogMesssage (el nombre lleva la errata de WhatsApp)


@dataclass(frozen=True)
class SystemEvent:
    """Un evento de sistema ya interpretado, listo para pintar."""

    kind: str
    icon: str
    label: str
    visible: bool = True
    stub_type: int | None = None
    # Solo para llamadas: duracion en segundos si el protobuf la trae.
    duration_seconds: int | None = None
    # ``True`` si se reconocio; ``False`` si es el evento generico de reserva.
    known: bool = True

    @property
    def display(self) -> str:
        """Texto completo con icono, tal como se pinta en el hilo."""
        if self.duration_seconds:
            minutos, segundos = divmod(self.duration_seconds, 60)
            return f"{self.icon} {self.label} · {minutos}:{segundos:02d}"
        return f"{self.icon} {self.label}"


UNKNOWN_EVENT = SystemEvent(
    kind="unknown", icon="•", label="Evento del sistema", known=False
)


def _varint_value(payload: bytes | None, data: bytes, number: int) -> int | None:
    """Lee un varint de primer nivel. ``top_level_fields`` no devuelve su valor.

    Se vuelve a recorrer el buffer porque el escaner generico descarta el
    contenido de los campos que no son length-delimited; aqui si hace falta.
    """
    from app.message_parser import _read_varint

    index = 0
    try:
        while index < len(data):
            key, index = _read_varint(data, index)
            field, wire = key >> 3, key & 7
            if wire == 0:
                value, index = _read_varint(data, index)
                if field == number:
                    return value
            elif wire == 1:
                index += 8
            elif wire == 2:
                length, index = _read_varint(data, index)
                index += length
            elif wire == 5:
                index += 4
            else:
                break
    except (ValueError, IndexError):
        return None
    return None


def classify_call_log(call_log_bytes: bytes) -> SystemEvent:
    """Interpreta un ``CallLogMessage``.

    Si el ``callOutcome`` no esta en la tabla no se inventa nada: se dice
    "Llamada" y se conserva si era de video, que es lo unico que se sabe.
    """
    is_video = bool(_varint_value(None, call_log_bytes, _CALL_LOG_IS_VIDEO))
    outcome = _varint_value(None, call_log_bytes, _CALL_LOG_OUTCOME)
    duration = _varint_value(None, call_log_bytes, _CALL_LOG_DURATION)

    base = _OUTCOME_LABELS.get(outcome if outcome is not None else -1)
    known = base is not None
    if base is None:
        base = "Llamada"

    if is_video:
        label = base.replace("Llamada", "Videollamada")
        icon = "📹"
        kind = "call_missed_video" if outcome == _OUTCOME_MISSED else "call_video"
    else:
        label = base
        icon = "📞"
        kind = "call_missed_voice" if outcome == _OUTCOME_MISSED else "call_voice"

    return SystemEvent(
        kind=kind,
        icon=icon,
        label=label,
        visible=True,
        duration_seconds=duration or None,
        known=known,
    )


class SystemMessageClassifier:
    """Traduce un ``WebMessageInfo`` a un :class:`SystemEvent`.

    Sin estado: es una clase por comodidad de uso y para poder sustituirla en
    los tests, no porque guarde nada.
    """

    def classify_stub(
        self, stub_type: int | None, parameters: list[str] | None = None
    ) -> SystemEvent:
        """Evento a partir del ``messageStubType``.

        ``parameters`` se acepta para poder enriquecer el texto en el futuro,
        pero HOY no se usa para decidir el tipo: eso lo determina el numero de
        stub y nada mas. Deducir el significado de un parametro seria volver a
        clasificar por contenido, que es justo lo que se quiere evitar.
        """
        if stub_type is None:
            return UNKNOWN_EVENT
        info = STUB_TYPES.get(int(stub_type))
        if info is None:
            log.debug("messageStubType %s sin interpretar", stub_type)
            return SystemEvent(
                kind="unknown",
                icon="•",
                label="Evento del sistema",
                visible=True,
                stub_type=int(stub_type),
                known=False,
            )
        return SystemEvent(
            kind=info.kind,
            icon=info.icon,
            label=info.label,
            visible=info.visible,
            stub_type=int(stub_type),
            known=True,
        )

    def classify_message(self, message_bytes: bytes | None) -> SystemEvent | None:
        """Evento si el ``Message`` describe una llamada. ``None`` si no lo es."""
        if not message_bytes:
            return None
        inner, _wrappers = unwrap_message(message_bytes)
        for number, _wire, payload in top_level_fields(inner):
            if number == CALL_LOG_MESSAGE_FIELD and payload:
                return classify_call_log(payload)
            if number == CALL_MESSAGE_FIELD:
                # ``CallMessage`` es la senalizacion de una llamada entrante;
                # no lleva resultado ni duracion, asi que solo se puede decir
                # que hubo una llamada.
                return SystemEvent(
                    kind="call_voice", icon="📞", label="Llamada", known=True
                )
        return None

    def classify_raw(self, raw_proto: bytes | None) -> SystemEvent | None:
        """Evento a partir del ``WebMessageInfo`` completo.

        Es el camino que usa la GUI: primero mira si el mensaje describe una
        llamada, y si no, cae al ``messageStubType``.
        """
        if not raw_proto:
            return None
        from app.proto import WebMessageInfo

        info = WebMessageInfo()
        try:
            info.ParseFromString(bytes(raw_proto))
        except Exception:  # noqa: BLE001 - un protobuf roto no rompe el chat
            return None

        if info.message:
            evento = self.classify_message(bytes(info.message))
            if evento is not None:
                return evento
        if info.messageStubType:
            return self.classify_stub(
                int(info.messageStubType), list(info.messageStubParameters)
            )
        return None


# Instancia compartida: no tiene estado, no hace falta una por llamada.
CLASSIFIER = SystemMessageClassifier()


def describe_system_message(
    raw_proto: bytes | None, metadata: dict | None = None
) -> SystemEvent:
    """Evento de sistema de un mensaje ya guardado.

    Prefiere ``raw_proto``, que es la fuente de verdad. Si no esta (una fila
    antigua, por ejemplo) recurre al ``stub_type`` que quedo en
    ``raw_metadata``, que se copio del mismo protobuf en su dia.
    """
    evento = CLASSIFIER.classify_raw(raw_proto)
    if evento is not None:
        return evento
    if metadata:
        stub = metadata.get("stub_type")
        if stub is not None:
            try:
                return CLASSIFIER.classify_stub(int(stub))
            except (TypeError, ValueError):
                pass
    return UNKNOWN_EVENT
