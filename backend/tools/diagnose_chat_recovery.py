"""¿Por qué esta conversación se quedó a medias? Solo lectura.

PARA QUÉ SIRVE
--------------
Un chat que se para tiene varias explicaciones posibles, y desde fuera se
parecen todas: no hay ancla, el servidor dijo que ya no queda nada, se agotó
la espera, el ancla es vieja, o la pantalla enseña algo que ya no es cierto.
Cada una se arregla en un sitio distinto, así que lo primero es saber cuál es.

Esta herramienta responde eso con los datos que hay en PostgreSQL, y si
WhatsApp Web está en marcha también le pregunta cuál es el último mensaje que
él ve. Comparar las dos cosas es lo que distingue «falta historia vieja» de
«falta el borde reciente», que son problemas distintos.

    py tools/diagnose_chat_recovery.py --chat 12
    py tools/diagnose_chat_recovery.py --buscar isaac
    py tools/diagnose_chat_recovery.py --incompletos

SOLO LECTURA
------------
No escribe ni una fila, no pide historial, no aplica referencias y no toca el
estado de nada. Consulta la base y, como mucho, le hace al índice de WhatsApp
Web la misma pregunta de solo lectura que ya se le hace.

PRIVACIDAD
----------
Ni texto de mensajes, ni teléfonos completos, ni identificadores completos.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import load_settings  # noqa: E402
from app.models import (  # noqa: E402
    Chat,
    ChatHistoryState,
    HistoryRequest,
    HistorySeed,
    Message,
)

LINEA = "=" * 70

#: Lo que dice el servidor al terminar un bloque. 1 y 3 son FINAL: ya no queda
#: nada por ese lado. 0 y 2 son MORE: se puede seguir pidiendo.
FINAL = {1, 3}


def _corto(valor: str | None, visible: int = 6) -> str:
    """Un identificador reconocible que no identifica a nadie."""
    if not valor:
        return "-"
    if "@" in valor:
        usuario, servidor = valor.split("@", 1)
        return f"{usuario[:visible]}***@{servidor}"
    return f"{valor[:8]}…" if len(valor) > 10 else valor


def _fecha(ts) -> str:
    if not ts:
        return "-"
    from datetime import datetime, timezone

    try:
        return (
            datetime.fromtimestamp(int(ts), tz=timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M")
        )
    except Exception:  # noqa: BLE001
        return str(ts)


# ---------------------------------------------------------------------------
# Lo que sabe la base
# ---------------------------------------------------------------------------


def _retrato(sesion: Session, chat: Chat) -> dict:
    """Todo lo que se sabe de esa conversación, sin interpretarlo todavía."""
    from app.history.cursor import get_valid_history_cursor

    estado = sesion.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == chat.jid)
    ).scalar_one_or_none()

    mensajes = sesion.execute(
        select(
            func.count(Message.id),
            func.min(Message.timestamp),
            func.max(Message.timestamp),
        ).where(Message.chat_jid == chat.jid)
    ).one()

    peticiones = dict(
        sesion.execute(
            select(HistoryRequest.status, func.count())
            .where(HistoryRequest.chat_jid == chat.jid)
            .group_by(HistoryRequest.status)
        ).all()
    )

    ultima = sesion.execute(
        select(HistoryRequest)
        .where(HistoryRequest.chat_jid == chat.jid)
        .order_by(HistoryRequest.id.desc())
        .limit(1)
    ).scalar_one_or_none()

    ultimo_exito = sesion.execute(
        select(func.max(HistoryRequest.received_at)).where(
            HistoryRequest.chat_jid == chat.jid,
            HistoryRequest.status == "received",
        )
    ).scalar()

    anclas = sesion.execute(
        select(HistorySeed.source, func.count())
        .where(HistorySeed.chat_jid == chat.jid)
        .group_by(HistorySeed.source)
    ).all()

    cursor = get_valid_history_cursor(sesion, chat_jid=chat.jid)

    return {
        "chat": chat,
        "nombre": chat.name or _nombre_de_agenda(sesion, chat.jid),
        "estado": estado,
        "mensajes": int(mensajes[0] or 0),
        "mas_viejo": mensajes[1],
        "mas_nuevo": mensajes[2],
        "peticiones": peticiones,
        "ultima_peticion": ultima,
        "ultimo_exito": ultimo_exito,
        "anclas": dict(anclas),
        "cursor": cursor,
    }


def _nombre_de_agenda(sesion: Session, jid: str) -> str | None:
    """El nombre que el usuario ve, que vive en la agenda y no en el chat."""
    from app.models import Contact

    contacto = sesion.execute(
        select(Contact).where((Contact.jid == jid) | (Contact.lid == jid))
    ).scalars().first()
    if contacto is None:
        return None
    for valor in (
        contacto.display_name,
        contacto.business_name,
        contacto.push_name,
    ):
        if valor:
            return str(valor)
    return None


def _porque(retrato: dict, capacidad: str) -> tuple[str, str]:
    """Por qué está donde está, y qué haría falta para que siguiera.

    Deliberadamente determinista y sin adivinar: cada rama sale de un dato
    concreto de la base. Si ninguna encaja se dice «no está claro» en vez de
    inventar una explicación, que es lo que hace perder tiempo después.
    """
    estado = retrato["estado"]
    situacion = getattr(estado, "history_status", None)
    ultima = retrato["ultima_peticion"]

    if retrato["cursor"] is None:
        return (
            "SIN_ANCLA",
            "No hay ninguna referencia real con la que pedir historial. Hasta "
            "que aparezca una —del teléfono o del índice de WhatsApp Web— no "
            "se puede pedir nada.",
        )

    if situacion == "exhausted":
        return (
            "TERMINADO",
            "El servidor dijo que no queda más historial por ese lado. No se "
            "reabre sin una referencia nueva.",
        )

    if situacion == "fetching":
        return ("EN_CURSO", "Se está excavando ahora mismo.")

    if situacion == "timeout":
        proximo = getattr(estado, "next_retry_at", None)
        return (
            "ESPERANDO_REINTENTO",
            f"La última petición se quedó sin respuesta. Se reintenta "
            f"{'el ' + str(proximo) if proximo else 'en la próxima vuelta'}.",
        )

    if capacidad != "CONFIRMED":
        return (
            "MOTOR_EN_DUDA",
            f"Tiene con qué excavar, pero la capacidad ON_DEMAND está en "
            f"{capacidad}: se espera a que una respuesta real la confirme.",
        )

    sin_avance = int(getattr(estado, "consecutive_no_progress", 0) or 0)
    if sin_avance >= 3:
        return (
            "SIN_AVANCE",
            f"{sin_avance} respuestas válidas seguidas sin un solo mensaje "
            "nuevo. Se dio por terminado para no girar en vacío.",
        )

    if ultima is not None and ultima.status == "sent":
        return ("PETICION_VIVA", "Hay una petición enviada esperando respuesta.")

    if situacion == "pending":
        return ("EN_COLA", "Tiene ancla y está esperando su turno de excavación.")

    return ("NO_ESTA_CLARO", f"Estado '{situacion}' sin una causa evidente.")


# ---------------------------------------------------------------------------
# Lo que ve WhatsApp Web
# ---------------------------------------------------------------------------


def _indice_web(settings, *, timeout: float) -> dict[str, dict] | None:
    """El último mensaje que ve Web de cada conversación. No aplica nada."""
    import json
    import urllib.error
    import urllib.request

    url = (
        f"http://127.0.0.1:{settings.api_port}"
        f"/api/v1/web-companion/inventory/preview"
    )
    try:
        peticion = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
            datos = json.loads(respuesta.read().decode())
    except Exception:  # noqa: BLE001 - comparar con Web es opcional
        return None
    if datos.get("error"):
        return None

    porJid: dict[str, dict] = {}
    for fila in datos.get("chats") or []:
        jid = str(fila.get("chat_jid") or "")
        if jid:
            porJid[jid] = fila
    return porJid


def _huecos(retrato: dict, fila_web: dict | None) -> list[str]:
    """Qué le falta a esta conversación, comparando con lo que ve Web.

    Son dos preguntas distintas y hasta ahora se confundían:

    * **hueco reciente**: Web ve un mensaje más nuevo que el más nuevo de
      aquí. Eso no lo arregla excavar hacia atrás — falta el borde de arriba;
    * **hueco antiguo**: el ancla apunta más atrás de lo que hay guardado, así
      que queda historia vieja por recuperar.
    """
    marcas: list[str] = []
    cursor = retrato["cursor"]
    mas_viejo = retrato["mas_viejo"]

    if fila_web is not None:
        candidato = fila_web.get("candidate") or {}
        web_ts = candidato.get("timestamp") or fila_web.get("last_activity")
        # Una conversacion SIN mensajes no tiene borde: tiene todo por
        # recuperar, y de eso se encarga el camino normal.
        if web_ts and retrato["mas_nuevo"] and int(web_ts) > int(retrato["mas_nuevo"]):
            marcas.append("RECENT_GAP_DETECTED")

    if cursor is not None and mas_viejo is not None:
        if int(cursor.timestamp) < int(mas_viejo):
            marcas.append("OLD_HISTORY_PENDING")

    estado = retrato["estado"]
    if getattr(estado, "history_status", None) == "exhausted" and marcas:
        # Terminado por abajo no significa completo: puede faltar el borde
        # reciente, y eso no se ve mirando solo el estado.
        marcas.append("EXHAUSTED_PERO_INCOMPLETO")
    return marcas


# ---------------------------------------------------------------------------
# Presentación
# ---------------------------------------------------------------------------


def _pintar(retrato: dict, fila_web: dict | None, capacidad: str) -> None:
    chat = retrato["chat"]
    estado = retrato["estado"]
    cursor = retrato["cursor"]
    ultima = retrato["ultima_peticion"]

    print(LINEA)
    print(f"CHAT {chat.id}  {_corto(chat.jid)}")
    print(LINEA)
    print(f"  nombre                       {retrato['nombre'] or '(sin nombre)'}")
    print(f"  tipo                         {chat.chat_type}")
    print(f"  mensajes guardados           {retrato['mensajes']}")
    print(f"  el mas antiguo               {_fecha(retrato['mas_viejo'])}")
    print(f"  el mas nuevo                 {_fecha(retrato['mas_nuevo'])}")
    print(f"  ultima actividad             {_fecha(chat.last_message_timestamp)}")
    print()
    print(f"  estado                       {getattr(estado, 'history_status', '-')}")
    print(f"  peticiones enviadas          {getattr(estado, 'requests_sent', 0)}")
    print(f"  respuestas recibidas         {getattr(estado, 'responses_received', 0)}")
    print(f"  rondas sin avance            {getattr(estado, 'consecutive_no_progress', 0)}")
    print(f"  intentos fallidos            {getattr(estado, 'attempt_count', 0)}")
    print(f"  proximo reintento            {getattr(estado, 'next_retry_at', None) or '-'}")
    print(f"  ultimo error                 {getattr(estado, 'last_error', None) or '-'}")
    print()
    print("  Ancla con la que se excava:")
    if cursor is None:
        print("    (ninguna: no se puede pedir historial)")
    else:
        print(f"    identificador              {_corto(cursor.wa_msg_id)}")
        print(f"    marca                      {_fecha(cursor.timestamp)}")
        print(f"    de quien                   {'mia' if cursor.from_me else 'suya'}")
        print(f"    de donde salio             {cursor.source}")
    if retrato["anclas"]:
        print(f"    anclas conocidas           {retrato['anclas']}")
    print()
    print("  Peticiones de historial:")
    print(f"    por estado                 {retrato['peticiones'] or '(ninguna)'}")
    print(f"    ultimo exito               {retrato['ultimo_exito'] or '-'}")
    if ultima is not None:
        print(f"    la ultima                  {ultima.status}, {ultima.sent_at}")
        print(f"    trajo                      {ultima.response_count or 0} mensaje(s)")
    print(f"    capacidad ON_DEMAND        {capacidad}")

    if fila_web is not None:
        candidato = fila_web.get("candidate") or {}
        print()
        print("  Lo que ve WhatsApp Web:")
        print(f"    ultima actividad           {_fecha(fila_web.get('last_activity'))}")
        print(f"    su ultimo mensaje          {_corto(candidato.get('wa_msg_id'))}")
        print(f"    marca                      {_fecha(candidato.get('timestamp'))}")
        if fila_web.get("no_seed_reason"):
            print(f"    sin referencia por         {fila_web['no_seed_reason']}")

    marcas = _huecos(retrato, fila_web)
    motivo, explicacion = _porque(retrato, capacidad)
    print()
    print(f"  POR QUE ESTA ASI             {motivo}")
    print(f"    {explicacion}")

    # -- Las DOS dimensiones, por separado ---------------------------------
    #
    # Que quede historia vieja y que falte el borde reciente son problemas
    # distintos, con causas distintas y arreglos distintos. Mezclarlos en un
    # solo "incompleto" es lo que llevaba a pedir otra vez lo que ya se tenia.
    recibidas = retrato["peticiones"].get("received", 0)
    print()
    print("  HISTORIAL ANTIGUO (hacia atras)")
    print(f"    estado                     {getattr(estado, 'history_status', '-')}")
    print(f"    ancla                      {_corto(getattr(cursor, 'wa_msg_id', None))}")
    print(f"    marca del ancla            {_fecha(getattr(cursor, 'timestamp', None))}")
    print(
        f"    FINAL del servidor         "
        f"{'si' if getattr(estado, 'history_status', None) == 'exhausted' else 'no'}"
    )
    print(
        f"    peticiones respondidas     "
        f"{recibidas} de {sum(retrato['peticiones'].values())}"
    )
    print(f"    esperas agotadas           {retrato['peticiones'].get('timeout', 0)}")
    print(
        f"    queda historia vieja       "
        f"{'si' if 'OLD_HISTORY_PENDING' in marcas else 'no'}"
    )

    print()
    print("  BORDE RECIENTE (hacia arriba)")
    web_nuevo = None
    if fila_web is not None:
        candidato_web = fila_web.get("candidate") or {}
        web_nuevo = candidato_web.get("timestamp") or fila_web.get("last_activity")
    print(f"    lo mas nuevo aqui          {_fecha(retrato['mas_nuevo'])}")
    print(f"    lo mas nuevo que ve Web    {_fecha(web_nuevo)}")
    if web_nuevo and retrato["mas_nuevo"]:
        dias = (int(web_nuevo) - int(retrato["mas_nuevo"])) / 86400.0
        print(f"    diferencia                 {dias:.1f} dia(s)")
    hay_borde = "RECENT_GAP_DETECTED" in marcas
    print(f"    estado                     {'detected' if hay_borde else 'none'}")
    if hay_borde:
        ancla_web = (fila_web or {}).get("candidate") or {}
        print(f"    ancla utilizable           {_corto(ancla_web.get('wa_msg_id'))}")
        print(
            "    WhatsApp Web ve un mensaje MAS NUEVO que el ultimo"
            " guardado aqui. Eso NO lo arregla excavar hacia atras:"
            " ON_DEMAND parte del ancla y baja, asi que nunca alcanza"
            " lo que esta por encima. Hace falta anclar en esa"
            " referencia mas nueva y bajar hasta empalmar."
        )
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat", type=int, help="identificador de la conversacion")
    parser.add_argument("--buscar", help="parte del nombre o del identificador")
    parser.add_argument(
        "--incompletos",
        action="store_true",
        help="todas las que no han terminado su historial",
    )
    parser.add_argument("--limite", type=int, default=10)
    parser.add_argument("--sin-web", action="store_true", help="no preguntar a Web")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    if not (args.chat or args.buscar or args.incompletos):
        parser.error("hace falta --chat, --buscar o --incompletos")

    settings = load_settings()
    motor = create_engine(settings.database_url)

    with Session(motor) as sesion:
        consulta = select(Chat)
        if args.chat:
            consulta = consulta.where(Chat.id == args.chat)
        elif args.buscar:
            # Tambien por la agenda: `chats.name` suele estar vacio y el
            # nombre que ve el usuario sale de `contacts`. Buscar solo en la
            # conversacion no encontraria a nadie.
            patron = f"%{args.buscar.lower()}%"
            from app.models import Contact

            jids = set()
            for contacto in sesion.execute(
                select(Contact).where(
                    func.lower(func.coalesce(Contact.display_name, "")).like(patron)
                    | func.lower(func.coalesce(Contact.push_name, "")).like(patron)
                    | func.lower(func.coalesce(Contact.business_name, "")).like(patron)
                )
            ).scalars():
                for jid in (contacto.jid, contacto.lid):
                    if jid:
                        jids.add(jid)
            condicion = func.lower(func.coalesce(Chat.name, "")).like(
                patron
            ) | func.lower(Chat.jid).like(patron)
            if jids:
                condicion = condicion | Chat.jid.in_(jids)
            consulta = consulta.where(condicion)
        else:
            consulta = (
                consulta.join(ChatHistoryState, ChatHistoryState.chat_jid == Chat.jid)
                .where(ChatHistoryState.history_status != "exhausted")
                .order_by(Chat.last_message_timestamp.desc().nulls_last())
                .limit(args.limite)
            )
        chats = list(sesion.execute(consulta).scalars())

        if not chats:
            print("No hay ninguna conversacion que encaje.")
            return 1

        capacidad = _capacidad(sesion)
        indice = None if args.sin_web else _indice_web(settings, timeout=args.timeout)
        if indice is None and not args.sin_web:
            print(
                "(WhatsApp Web no contesto: se sigue sin comparar. Arranca "
                "service.py si quieres la comparacion.)\n"
            )

        for chat in chats[: args.limite]:
            retrato = _retrato(sesion, chat)
            fila = (indice or {}).get(chat.jid)
            _pintar(retrato, fila, capacidad)

    print("Solo lectura: no se ha escrito ni una fila.")
    return 0


def _capacidad(sesion: Session) -> str:
    from app.services import repository as repo
    from app.services.backfill_service import CAPABILITY_KEY

    try:
        guardado = repo.get_app_state(sesion, CAPABILITY_KEY)
    except Exception:  # noqa: BLE001
        return "UNKNOWN"
    if not isinstance(guardado, dict) or not guardado.get("confirmed"):
        return "UNKNOWN"
    return "SUSPECT" if guardado.get("state") == "SUSPECT" else "CONFIRMED"


if __name__ == "__main__":
    raise SystemExit(main())
