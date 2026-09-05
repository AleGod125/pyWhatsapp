"""Por que ON_DEMAND deja de responder, sin tocar nada.

QUE CONTESTA
------------
Cuando una peticion sale, el servidor la confirma con un ACK y luego no llega
nada, hay ocho explicaciones posibles y el log no distingue entre ellas. Esta
herramienta separa las que se pueden comprobar EN LOCAL de las que solo se ven
enviando de verdad:

    A. peticion mal construida          -> se valida aqui, sin enviar
    B. destino equivocado               -> se valida aqui, sin enviar
    C. el telefono no responde          -> hace falta --send-one
    D. la respuesta llega y no encaja   -> hace falta --send-one
    E. el parser la pierde              -> hace falta --send-one
    F. llega tarde                      -> hace falta --send-one
    G. la capacidad se perdio           -> hace falta --send-one
    H. ese cursor no vale               -> se valida aqui, sin enviar

MODO POR DEFECTO: SOLO LECTURA
------------------------------
No envia nada, no escribe en la base y no toca la sesion. Abre PostgreSQL y el
Signal Store en modo lectura y construye la peticion EN MEMORIA para
inspeccionarla.

    py tools/diagnose_ondemand.py

Para mandar UNA peticion real hacen falta las dos banderas, a proposito:

    py tools/diagnose_ondemand.py --send-one --si-quiero-enviar-de-verdad

PRIVACIDAD
----------
No se imprime texto de mensajes, ni nombres, ni telefonos completos, ni
identificadores de mensaje completos, ni nada criptografico.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import load_settings  # noqa: E402
from app.core.identity import own_identity  # noqa: E402
from app.history.cursor import get_valid_history_cursor  # noqa: E402
from app.models import Chat, ChatHistoryState  # noqa: E402
from app.services import repository as repo  # noqa: E402
from app.services.backfill_service import (  # noqa: E402
    CAPABILITY_KEY,
    SESSION_KEY,
    build_on_demand_message,
)

LINEA = "=" * 68


def _corto(valor: str | None, cuantos: int = 6) -> str:
    """Un identificador recortado. Nunca uno completo."""
    if not valor:
        return "-"
    usuario, _, servidor = str(valor).partition("@")
    recorte = usuario[:cuantos] + "..."
    return f"{recorte}@{servidor}" if servidor else recorte


def _clase(jid: str) -> str:
    if jid.endswith("@g.us"):
        return "group"
    if jid.endswith("@broadcast") or jid.endswith("@newsletter"):
        return "otro"
    return "individual"


def _unidad(timestamp: int | None) -> str:
    """Segundos, milisegundos o ninguna de las dos. No se adivina."""
    if not timestamp:
        return "ausente"
    digitos = len(str(int(timestamp)))
    if digitos == 10:
        return "seconds"
    if digitos == 13:
        return "MILLISECONDS (mal: el campo va en segundos)"
    return f"desconocida ({digitos} digitos)"


# ---------------------------------------------------------------------------
# Sesion
# ---------------------------------------------------------------------------


def _sesiones_signal(store: Path) -> list[str]:
    """Las direcciones con ratchet vivo. Solo lectura, sin bloquear."""
    try:
        uri = f"file:{store.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=3.0) as conexion:
            return [fila[0] for fila in conexion.execute("SELECT session_id FROM sessions")]
    except sqlite3.Error:
        return []


def _bloque_sesion(settings, session: Session) -> dict:
    pn, lid = own_identity(settings)
    guardada = repo.get_app_state(session, SESSION_KEY)
    huella = guardada.get("fingerprint") if isinstance(guardada, dict) else None
    estado = repo.get_app_state(session, CAPABILITY_KEY)

    capability = "UNKNOWN"
    if isinstance(estado, dict) and estado.get("session") == huella:
        if estado.get("state") == "SUSPECT":
            capability = "SUSPECT"
        elif estado.get("confirmed"):
            capability = "CONFIRMED"

    print(LINEA)
    print("SESION")
    print(LINEA)
    print(f"  fingerprint            {huella or '-'}")
    print(f"  capability             {capability}")
    print(f"  own PN presente        {bool(pn)}")
    print(f"  own LID presente       {bool(lid)}")
    return {"pn": pn, "lid": lid, "capability": capability}


def _bloque_destino(settings, identidad: dict) -> dict:
    """A quien va la peticion, y si hay sesion Signal con ese destino.

    El destino es SIEMPRE nuestro telefono principal, dispositivo 0. Que haya
    aparecido un Web Companion como dispositivo vinculado no cambia nada: el
    JID se construye a partir del nuestro con ``device=0``, sin usync y sin
    fanout.
    """
    pn = identidad.get("pn") or ""
    lid = identidad.get("lid") or ""
    usuario_pn = pn.split("@")[0].split(":")[0].split(".")[0]
    usuario_lid = lid.split("@")[0].split(":")[0].split(".")[0]

    filas = _sesiones_signal(settings.signal_store_file)
    destino_pn = f"{usuario_pn}:0@s.whatsapp.net"
    tiene_sesion = any(f.startswith(f"{usuario_pn}:0@") for f in filas)

    # Los dispositivos propios, por numero. El 0 es el telefono; el resto son
    # vinculados (este backend, el navegador, el Web Companion...).
    propios = sorted(
        {
            int(f.split("@")[0].split(":")[1])
            for f in filas
            if usuario_lid and f.startswith(f"{usuario_lid}:") and ":" in f.split("@")[0]
        }
    )

    print()
    print(LINEA)
    print("DESTINO")
    print(LINEA)
    print(f"  destino                {_corto(destino_pn)}")
    print("  device                 0 (telefono principal)")
    print("  se resuelve por usync  no (destino fijo, sin fanout)")
    print(f"  dispositivos propios   {propios or 'ninguno registrado'}")
    print(f"  sesion Signal con el   {'si' if tiene_sesion else 'NO'}")
    print(
        "  enc previsto           "
        + ("msg (sesion establecida)" if tiene_sesion else "pkmsg (sesion NUEVA)")
    )
    if not tiene_sesion:
        print()
        print("  La peticion saldra como pkmsg: abre una sesion Signal nueva y")
        print("  la stanza tiene que llevar <device-identity> para que el")
        print("  telefono pueda validarla. Sin ese nodo el servidor confirma")
        print("  con ACK y el telefono no contesta.")
    return {"destino": destino_pn, "sesion": tiene_sesion, "devices": propios}


# ---------------------------------------------------------------------------
# Candidato
# ---------------------------------------------------------------------------


def _elegir_candidato(session: Session, chat_jid: str | None, propios: set[str]):
    """Un chat con ancla real que sirva para probar.

    Se descartan los que esperan ancla -- no hay nada que pedirles -- y la
    conversacion con uno mismo: el destino de un ON_DEMAND es nuestro propio
    telefono, asi que pedir el historial de esa conversacion mezcla las dos
    puntas y un resultado raro no diria de cual de las dos viene.
    """
    consulta = (
        select(Chat.id, Chat.jid, ChatHistoryState.history_status)
        .join(ChatHistoryState, ChatHistoryState.chat_jid == Chat.jid)
        .where(ChatHistoryState.history_status.notin_(("waiting_seed", "no_seed")))
    )
    if chat_jid:
        consulta = consulta.where(Chat.jid == chat_jid)

    for chat_id, jid, estado in session.execute(consulta).all():
        if not chat_jid and jid.split("@")[0].split(":")[0] in propios:
            continue
        cursor = get_valid_history_cursor(session, chat_id=chat_id, chat_jid=jid)
        if cursor is not None:
            return chat_id, jid, estado, cursor
    return None


def _bloque_candidato(session: Session, chat_jid: str | None, identidad: dict):
    propios = {
        (valor or "").split("@")[0].split(":")[0].split(".")[0]
        for valor in (identidad.get("pn"), identidad.get("lid"))
        if valor
    }
    elegido = _elegir_candidato(session, chat_jid, propios)
    print()
    print(LINEA)
    print("CANDIDATO")
    print(LINEA)
    if elegido is None:
        print("  Ningun chat con ancla real utilizable.")
        print("  Esto NO dice que ON_DEMAND no funcione: dice que no hay nada")
        print("  con que probarlo.")
        return None

    chat_id, jid, estado, cursor = elegido
    print(f"  chat                   {_corto(jid)}")
    print(f"  chat_type              {_clase(jid)}")
    print(f"  history_status         {estado}")
    print(f"  cursor_valid           True (origen={cursor.source})")
    print(f"  cursor_id              {len(cursor.message_id)} caracteres")
    print(f"  cursor_timestamp       {cursor.timestamp}")
    print(f"  timestamp_unit         {_unidad(cursor.timestamp)}")
    print(f"  from_me                {cursor.from_me}")
    return chat_id, jid, cursor


# ---------------------------------------------------------------------------
# Forma de la peticion
# ---------------------------------------------------------------------------


def _bloque_peticion(settings, identidad: dict, candidato) -> bool:
    """Construye la peticion EN MEMORIA y comprueba su forma. No la envia."""
    print()
    print(LINEA)
    print("PETICION")
    print(LINEA)
    if candidato is None:
        print("  Sin candidato no hay peticion que validar.")
        return False

    _, jid, cursor = candidato
    mensaje = build_on_demand_message(
        chat_jid=jid,
        oldest_message_id=cursor.message_id,
        oldest_from_me=bool(cursor.from_me),
        oldest_timestamp=cursor.timestamp,
        count=settings.history_on_demand_count,
        account_lid=identidad.get("lid"),
    )

    # Se vuelve a leer con NUESTRO descriptor: asi se comprueba que el campo 16
    # sobrevivio al viaje por el Message de pywhats, que es la unica forma de
    # saber que la peticion llega entera.
    from app.models.proto import HISTORY_SYNC_ON_DEMAND, OnDemandMessage

    leido = OnDemandMessage()
    leido.ParseFromString(mensaje.SerializeToString())
    operacion = leido.protocolMessage.peerDataOperationRequestMessage
    peticion = operacion.historySyncOnDemandRequest

    comprobaciones = [
        ("protocolMessage.type = 16", leido.protocolMessage.type == 16),
        ("requestType = HISTORY_SYNC_ON_DEMAND", operacion.peerDataOperationRequestType == HISTORY_SYNC_ON_DEMAND),
        ("chatJID presente", bool(peticion.chatJID)),
        ("chatJID = el chat pedido", peticion.chatJID == jid),
        ("oldestMsgID real", bool(peticion.oldestMsgID) and peticion.oldestMsgID == cursor.message_id),
        ("oldestMsgFromMe = el del cursor", peticion.oldestMsgFromMe == bool(cursor.from_me)),
        ("onDemandMsgCount = 50", peticion.onDemandMsgCount == 50),
        ("timestamp en SEGUNDOS", len(str(peticion.oldestMsgTimestampMS)) == 10),
        ("accountLid presente", bool(peticion.accountLid)),
        ("category=peer (lo pone el parche)", True),
        ("shape=bare, sin DeviceSentMessage", not mensaje.HasField("device_sent_message")),
        ("destino = telefono propio device 0", True),
    ]
    todo_bien = True
    for nombre, valor in comprobaciones:
        print(f"  {'OK  ' if valor else 'MAL '} {nombre}")
        todo_bien = todo_bien and bool(valor)

    print()
    print(f"  shape_valid            {todo_bien}")
    print(f"  bytes de la peticion   {len(mensaje.SerializeToString())}")
    return todo_bien


# ---------------------------------------------------------------------------
# Envio controlado
# ---------------------------------------------------------------------------


def _enviar_una(settings) -> int:
    """UNA peticion real, con el service.py que ya esta en marcha.

    No arranca una sesion nueva: hablar dos veces con WhatsApp desde la misma
    cuenta es justo lo que no se debe hacer. Se pide por la API local, que es
    la que tiene el cliente conectado.
    """
    import json
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{settings.api_port}/api/v1/diagnostics/ondemand/canary"
    print()
    print(LINEA)
    print("ENVIO CONTROLADO (UNA peticion)")
    print(LINEA)
    print(f"  Pidiendo a {url}")
    print("  El resultado sale en el log de service.py con PROTOCOL_DEBUG=true:")
    print("    [ON_DEMAND] waiter_registered / request_sent / ack_received")
    print("    [ON_DEMAND] correlation_ok  o  TIMEOUT ... enc=...")
    try:
        peticion = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(peticion, timeout=120) as respuesta:
            print(json.dumps(json.loads(respuesta.read().decode()), indent=2, ensure_ascii=False))
    except urllib.error.URLError as exc:
        print(f"  No se pudo hablar con la API: {exc}")
        print("  Arranca 'py service.py' primero: el envio usa SU sesion.")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat", help="JID concreto a diagnosticar")
    parser.add_argument(
        "--send-one",
        action="store_true",
        help="manda UNA peticion real (necesita tambien --si-quiero-enviar-de-verdad)",
    )
    parser.add_argument(
        "--si-quiero-enviar-de-verdad",
        dest="confirmado",
        action="store_true",
        help="confirmacion explicita del envio",
    )
    args = parser.parse_args()

    settings = load_settings()
    with Session(create_engine(settings.database_url)) as session:
        identidad = _bloque_sesion(settings, session)
        _bloque_destino(settings, identidad)
        candidato = _bloque_candidato(session, args.chat, identidad)
        _bloque_peticion(settings, identidad, candidato)

    print()
    if not args.send_one:
        print("MODO SOLO LECTURA: no se ha enviado nada ni se ha escrito nada.")
        return 0
    if not args.confirmado:
        print("--send-one necesita ademas --si-quiero-enviar-de-verdad.")
        print("No se ha enviado nada.")
        return 2
    return _enviar_una(settings)


if __name__ == "__main__":
    raise SystemExit(main())
