"""¿Sigue respondiendo hoy una petición que WhatsApp YA respondió?

LA PREGUNTA
-----------
Una petición ON_DEMAND con cursor válido, timestamp en segundos, ``from_me``
correcto, destino correcto, forma válida, ``enc_type=msg`` y sesión Signal
establecida obtuvo ACK y ninguna respuesta. Todo lo comprobable en local
estaba bien, así que la siguiente pregunta solo se responde midiendo:

    ¿el problema es el PROTOCOLO, o son los CURSORES que usamos hoy?

Se responde con el mismo chat y dos anclas distintas:

    VARIANTE B (known-good)   el ancla EXACTA de una petición que respondió
    VARIANTE A (current)      el ancla que el motor calcularía hoy

DE DÓNDE SALE EL KNOWN-GOOD
---------------------------
De ``history_requests``, que guarda cada petición emitida con su ancla, su
estado y cuántos mensajes trajo. Hay 88 filas con ``status='received'``: son
peticiones que el teléfono contestó de verdad. ``from_me`` no está en esa
tabla y se recupera del mensaje y de la semilla, que tienen que coincidir.

MODO POR DEFECTO: SOLO LECTURA
------------------------------
No envía nada y no escribe nada.

    py tools/diagnose_ondemand_known_good.py

Para enviar UNA petición hace falta una bandera explícita, y solo una de las
dos por ejecución:

    py tools/diagnose_ondemand_known_good.py --send-known-good
    py tools/diagnose_ondemand_known_good.py --send-current

PRIVACIDAD
----------
No se imprime texto de mensajes, ni nombres, ni teléfonos completos, ni
identificadores de mensaje completos.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import load_settings  # noqa: E402
from app.core.identity import own_identity  # noqa: E402
from app.history.cursor import get_valid_history_cursor  # noqa: E402
from app.models import (  # noqa: E402
    Chat,
    ChatHistoryState,
    HistoryRequest,
    HistorySeed,
    Message,
)
from app.services import repository as repo  # noqa: E402
from app.services.backfill_service import CAPABILITY_KEY, SESSION_KEY  # noqa: E402

LINEA = "=" * 70


def _corto(jid: str | None) -> str:
    if not jid:
        return "-"
    usuario, _, servidor = jid.partition("@")
    return f"{usuario[:6]}...@{servidor}"


# ---------------------------------------------------------------------------
# Elegir el known-good
# ---------------------------------------------------------------------------


def _from_me(sesion: Session, wa_msg_id: str):
    """``from_me`` del ancla, cruzando las dos fuentes que lo saben."""
    del_mensaje = sesion.execute(
        select(Message.from_me).where(Message.whatsapp_message_id == wa_msg_id)
    ).scalar_one_or_none()
    de_la_semilla = sesion.execute(
        select(HistorySeed.from_me).where(HistorySeed.wa_msg_id == wa_msg_id)
    ).scalar_one_or_none()
    if del_mensaje is None and de_la_semilla is None:
        return None, "no consta"
    if (
        del_mensaje is not None
        and de_la_semilla is not None
        and bool(del_mensaje) != bool(de_la_semilla)
    ):
        return None, "el mensaje y la semilla discrepan"
    valor = del_mensaje if del_mensaje is not None else de_la_semilla
    fuentes = []
    if del_mensaje is not None:
        fuentes.append("mensaje")
    if de_la_semilla is not None:
        fuentes.append("semilla")
    return bool(valor), "+".join(fuentes)


def _candidatos(sesion: Session, propios: set[str]) -> list[dict]:
    """Las peticiones históricas que mejor sirven para repetir el experimento.

    Se prefiere, en este orden y por lo que dice el enunciado del problema:
    individual antes que grupo, ``from_me=False`` antes que propio, respuesta
    con mensajes (MORE) antes que vacía, y chat que siga existiendo.

    Menos variables, menos formas de equivocarse al leer el resultado.
    """
    filas = (
        sesion.execute(
            select(HistoryRequest)
            .where(HistoryRequest.status == "received")
            .order_by(HistoryRequest.id)
        )
        .scalars()
        .all()
    )

    salida: list[dict] = []
    for fila in filas:
        jid = fila.chat_jid or ""
        if jid.split("@")[0].split(":")[0] in propios:
            continue  # la conversación con uno mismo mezcla las dos puntas
        if not fila.response_count:
            continue  # respondió, pero vacía: es un FINAL, no un MORE
        chat_id = sesion.execute(
            select(Chat.id).where(Chat.jid == jid)
        ).scalar_one_or_none()
        if chat_id is None:
            continue  # el chat ya no existe: no se puede repetir
        from_me, fuente = _from_me(sesion, fila.cursor_message_id)
        if from_me is None:
            continue
        salida.append(
            {
                "request_id": fila.id,
                "chat_jid": jid,
                "chat_id": chat_id,
                "wa_msg_id": fila.cursor_message_id,
                "timestamp": int(fila.cursor_timestamp),
                "from_me": from_me,
                "from_me_source": fuente,
                "messages": int(fila.response_count),
                "sent_at": fila.sent_at,
                "es_grupo": jid.endswith("@g.us"),
            }
        )

    def orden(c: dict) -> tuple:
        return (
            c["es_grupo"],          # individual primero
            c["from_me"],           # ajeno primero
            -c["messages"],         # el que más trajo primero
        )

    return sorted(salida, key=orden)


# ---------------------------------------------------------------------------
# Bloques del informe
# ---------------------------------------------------------------------------


def _bloque_sesion(settings, sesion: Session) -> dict:
    pn, lid = own_identity(settings)
    guardada = repo.get_app_state(sesion, SESSION_KEY)
    huella = guardada.get("fingerprint") if isinstance(guardada, dict) else None
    estado = repo.get_app_state(sesion, CAPABILITY_KEY)
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
    return {"pn": pn, "lid": lid, "capability": capability, "fingerprint": huella}


def _bloque_known_good(candidato: dict) -> None:
    print()
    print(LINEA)
    print("KNOWN GOOD HISTORICO")
    print(LINEA)
    print(f"  request_id                    {candidato['request_id']}")
    print(f"  chat                          {_corto(candidato['chat_jid'])}")
    print(f"  chat_id                       {candidato['chat_id']}")
    print(f"  chat_type                     {'group' if candidato['es_grupo'] else 'individual'}")
    print("  historical_cursor_present     True")
    print(f"  historical_cursor_id_len      {len(candidato['wa_msg_id'])}")
    print(f"  historical_timestamp          {candidato['timestamp']}")
    print(f"  historical_from_me            {candidato['from_me']}  (fuente: {candidato['from_me_source']})")
    print("  historical_response           True")
    # Honestidad: en la base solo consta cuantos mensajes trajo. El
    # endOfHistoryTransferType se registro en el log, no en una columna.
    print("  historical_result             MORE (mensajes>0; el tipo de fin")
    print("                                vive en el log, no en la base)")
    print(f"  historical_messages           {candidato['messages']}")
    print(f"  historical_sent_at            {str(candidato['sent_at'])[:19]}")


def _bloque_current(sesion: Session, candidato: dict):
    actual = get_valid_history_cursor(
        sesion, chat_id=candidato["chat_id"], chat_jid=candidato["chat_jid"]
    )
    estado = sesion.execute(
        select(ChatHistoryState.history_status).where(
            ChatHistoryState.chat_jid == candidato["chat_jid"]
        )
    ).scalar_one_or_none()
    guardados = sesion.execute(
        select(func.count())
        .select_from(Message)
        .where(Message.chat_id == candidato["chat_id"])
    ).scalar()

    print()
    print(LINEA)
    print("CURRENT (mismo chat, ancla de hoy)")
    print(LINEA)
    print(f"  history_status                {estado}")
    print(f"  mensajes guardados            {guardados}")
    if actual is None:
        print("  current_cursor                NO HAY ancla utilizable hoy")
        return None
    print(f"  current_cursor_id_len         {len(actual.message_id)}")
    print(f"  current_timestamp             {actual.timestamp}")
    print(f"  current_from_me               {actual.from_me}")
    print(f"  current_source                {actual.source}")
    return actual


def _bloque_comparacion(candidato: dict, actual) -> None:
    print()
    print(LINEA)
    print("COMPARACION")
    print(LINEA)
    if actual is None:
        print("  Sin ancla actual no hay nada que comparar.")
        return
    mismo_id = actual.message_id == candidato["wa_msg_id"]
    mismo_ts = int(actual.timestamp) == candidato["timestamp"]
    mismo_fm = bool(actual.from_me) == candidato["from_me"]
    print(f"  same_cursor                   {mismo_id}")
    print(f"  same_timestamp                {mismo_ts}")
    print(f"  same_from_me                  {mismo_fm}")
    if not mismo_ts:
        delta = int(actual.timestamp) - candidato["timestamp"]
        dias = abs(delta) / 86400.0
        cual = "MAS RECIENTE" if delta > 0 else "MAS ANTIGUO"
        print(f"  el ancla de hoy es            {cual} por {dias:.1f} dias")
    print()
    print("  Lo que NO cambia entre las dos peticiones:")
    print("    destino teléfono propio device 0, count=50, timestamp en segundos,")
    print("    category=peer, shape=bare, un solo <enc>, sin DeviceSentMessage,")
    print("    correlacion por peerDataRequestSessionID, waiter antes del envio.")
    print("  Lo unico que cambia es el ANCLA.")


def _bloque_alternativas(candidatos: list[dict]) -> None:
    print()
    print(LINEA)
    print("OTRAS PETICIONES HISTORICAS UTILIZABLES")
    print(LINEA)
    for c in candidatos[1:5]:
        tipo = "group" if c["es_grupo"] else "individual"
        print(
            f"  request_id={c['request_id']:<4} {_corto(c['chat_jid']):>22} "
            f"{tipo:<11} from_me={str(c['from_me']):<5} mensajes={c['messages']}"
        )
    print()
    print("  Para usar otra:  --request-id N")


# ---------------------------------------------------------------------------
# Envio
# ---------------------------------------------------------------------------


def _enviar(settings, cuerpo: dict) -> int:
    url = f"http://127.0.0.1:{settings.api_port}/api/v1/diagnostics/ondemand/probe"
    print()
    print(LINEA)
    print(f"ENVIO CONTROLADO: {cuerpo['mode']}  (UNA sola peticion)")
    print(LINEA)
    print(f"  {url}")
    print("  Mientras corre, ningun otro ON_DEMAND puede salir: ni el ciclo")
    print("  automatico, ni la cola de despertados, ni un canary.")
    print("  No se escribe cursor, ni estado, ni intentos, ni mensajes.")
    print()
    datos = json.dumps(cuerpo).encode()
    peticion = urllib.request.Request(
        url, data=datos, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(peticion, timeout=180) as respuesta:
            salida = json.loads(respuesta.read().decode())
    except urllib.error.HTTPError as exc:
        print(json.dumps(json.loads(exc.read().decode()), indent=2, ensure_ascii=False))
        return 1
    except urllib.error.URLError as exc:
        print(f"  No se pudo hablar con la API: {exc}")
        print("  Arranca 'py service.py' primero: el envio usa SU sesion.")
        return 1

    print(json.dumps(salida, indent=2, ensure_ascii=False))
    resultado = salida.get("result", {})
    print()
    if resultado.get("history_response"):
        print("  >>> RESPONDIO. HISTORY_SYNC_NOTIFICATION correlacionada.")
    else:
        print("  >>> SIN RESPUESTA. El ACK solo confirmaba la entrega.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-id", type=int, help="usar otra peticion historica")
    grupo = parser.add_mutually_exclusive_group()
    grupo.add_argument(
        "--send-known-good",
        action="store_true",
        help="manda UNA peticion con el ancla historica exacta",
    )
    grupo.add_argument(
        "--send-current",
        action="store_true",
        help="manda UNA peticion con el ancla que el motor usaria hoy",
    )
    args = parser.parse_args()

    settings = load_settings()
    with Session(create_engine(settings.database_url)) as sesion:
        identidad = _bloque_sesion(settings, sesion)
        propios = {
            (v or "").split("@")[0].split(":")[0].split(".")[0]
            for v in (identidad["pn"], identidad["lid"])
            if v
        }
        candidatos = _candidatos(sesion, propios)
        if not candidatos:
            print()
            print("No hay ninguna peticion historica con respuesta que repetir.")
            return 1

        elegido = candidatos[0]
        if args.request_id is not None:
            porid = [c for c in candidatos if c["request_id"] == args.request_id]
            if not porid:
                print(f"\nLa peticion {args.request_id} no sirve como known-good.")
                return 2
            elegido = porid[0]

        _bloque_known_good(elegido)
        actual = _bloque_current(sesion, elegido)
        _bloque_comparacion(elegido, actual)
        _bloque_alternativas(candidatos)

    print()
    if args.send_known_good:
        return _enviar(settings, {"mode": "known_good", "request_id": elegido["request_id"]})
    if args.send_current:
        if actual is None:
            print("No hay ancla actual: --send-current no tiene nada que enviar.")
            return 2
        return _enviar(settings, {"mode": "current", "chat_jid": elegido["chat_jid"]})

    print("MODO SOLO LECTURA: no se ha enviado nada ni se ha escrito nada.")
    print()
    print("Para enviar UNA peticion (arranca 'py service.py' antes):")
    print("  py tools/diagnose_ondemand_known_good.py --send-known-good")
    print("  py tools/diagnose_ondemand_known_good.py --send-current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
