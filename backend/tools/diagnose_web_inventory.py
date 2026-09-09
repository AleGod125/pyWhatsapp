"""¿Qué ve WhatsApp Web, y de cuántas conversaciones puede dar una referencia?

EL REPARTO NUEVO
----------------
WhatsApp Web indexa: qué conversaciones existen y cuál es el último mensaje
real de cada una. La sesión principal excava: `ON_DEMAND`, bloques de
cincuenta, `MORE`, `FINAL`.

Esta herramienta mide la mitad del índice, que es la que decide si el resto
puede funcionar: sin una referencia real no se le puede pedir nada al teléfono.

    py tools/diagnose_web_inventory.py

SOLO LECTURA
------------
Usa el mismo comando que el índice normal, pero **no** reconcilia, no crea
conversaciones y no escribe ni una fila. Necesita `service.py` en marcha: el
índice lo produce su Web Companion.

PRIVACIDAD
----------
Ni texto, ni nombres, ni teléfonos completos, ni identificadores completos.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import load_settings  # noqa: E402
from app.history.seed_collector import SeedCandidate, validar  # noqa: E402
from app.models import SEEDLESS_STATUSES, Chat, ChatHistoryState  # noqa: E402

LINEA = "=" * 70


def _clase(jid: str) -> str:
    if jid.endswith("@g.us"):
        return "grupo"
    if jid.endswith("@lid"):
        return "individual (LID)"
    return "individual"


def _indice(settings, *, timeout: float) -> dict | None:
    """Pide el índice al worker que ya está en marcha."""
    import json
    import urllib.error
    import urllib.request

    url = (
        f"http://127.0.0.1:{settings.api_port}"
        f"/api/v1/web-companion/inventory/preview"
    )
    peticion = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
            return json.loads(respuesta.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode())
        except Exception:  # noqa: BLE001
            return {"error": {"code": f"HTTP_{exc.code}"}}
    except urllib.error.URLError:
        return None


def _sondeo(settings, *, timeout: float) -> dict | None:
    """El sondeo ANTIGUO, para comparar. Tambien solo lectura.

    Si el sondeo encuentra referencias y el indice no, no es que WhatsApp Web
    no tenga mensajes: es que el indice no llega a preguntar. Esa comparacion
    es la que encontro este fallo.
    """
    import json
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{settings.api_port}/api/v1/web-companion/probe"
    peticion = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
            return json.loads(respuesta.read().decode())
    except Exception:  # noqa: BLE001 - comparar es opcional
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    settings = load_settings()
    datos = _indice(settings, timeout=args.timeout)

    print(LINEA)
    print("INDICE DE WHATSAPP WEB")
    print(LINEA)

    if datos is None:
        print("  No se pudo hablar con la API.")
        print("  Arranca 'py service.py' primero: el indice lo produce su")
        print("  Web Companion, no esta herramienta.")
        return 1
    if "error" in datos:
        motivo = datos["error"]
        codigo = motivo.get("code", "?") if isinstance(motivo, dict) else "?"
        print(f"  El indice no esta disponible: {codigo}")
        if isinstance(motivo, dict) and motivo.get("message"):
            print(f"  {motivo['message']}")
        return 1

    metricas = datos.get("metrics") or {}
    filas = datos.get("chats") or []

    print(f"  origen                         {datos.get('source', '?')}")
    print(f"  conversaciones vistas          {metricas.get('total', 0)}")
    print(f"    individuales                 {metricas.get('individual', 0)}")
    print(f"    grupos                       {metricas.get('group', 0)}")
    print()
    print("  De donde salio la referencia:")
    print(f"    del ultimo mensaje           {metricas.get('seed_from_last_message', 0)}")
    print(f"    ya materializado en memoria  {metricas.get('seed_from_store', 0)}")
    print(f"    pidiendo UNO a la red        {metricas.get('seed_from_fetch1', 0)}")
    print(f"    sin ninguna                  {metricas.get('sin_candidato', 0)}")
    print()
    print("  Pidiendo UNO a la red:")
    print(f"    intentadas                   {metricas.get('fetch1_attempted', 0)}")
    print(f"    devolvieron mensaje          {metricas.get('fetch1_success', 0)}")
    print(f"    devolvieron vacio            {metricas.get('fetch1_empty', 0)}")
    print(f"    fallaron                     {metricas.get('fetch1_error', 0)}")
    print(f"    omitidas (ya resueltas)      {metricas.get('fetch1_skipped', 0)}")
    print()
    # Dos cifras, no una. Si se ven mensajes y no salen referencias, el
    # problema esta en el filtro; si no se ven, esta en WhatsApp Web. Con un
    # solo numero las dos cosas son indistinguibles.
    print("  Ver un mensaje NO es tener una referencia:")
    print(f"    conversaciones con mensaje   {metricas.get('messages_found', 0)}")
    print(f"    referencias validas          {metricas.get('valid_seeds', 0)}")
    print(f"    mensajes vistos y descartados {metricas.get('seed_invalid', 0)}")

    if datos.get("rejections"):
        print()
        print("  Motivos de rechazo del worker:")
        for motivo, cuantos in sorted(datos["rejections"].items()):
            print(f"    {motivo:<34} {cuantos}")

    sin_referencia = {}
    for fila in filas:
        if fila.get("candidate"):
            continue
        motivo = str(fila.get("no_seed_reason") or "WEB_NO_CANDIDATE")
        sin_referencia[motivo] = sin_referencia.get(motivo, 0) + 1
    if sin_referencia:
        print()
        print("  Las que se quedaron sin referencia, por que:")
        for motivo, cuantos in sorted(sin_referencia.items()):
            print(f"    {motivo:<34} {cuantos}")

    # -- Ahora contra lo que hay aqui ---------------------------------------
    with Session(create_engine(settings.database_url)) as sesion:
        conocidos = {j for (j,) in sesion.execute(select(Chat.jid)).all() if j}
        esperando = {
            j
            for (j,) in sesion.execute(
                select(ChatHistoryState.chat_jid).where(
                    ChatHistoryState.history_status.in_(SEEDLESS_STATUSES)
                )
            ).all()
            if j
        }

    vistos = {str(f.get("chat_jid")) for f in filas if f.get("chat_jid")}
    nuevos = vistos - conocidos
    faltan = conocidos - vistos

    print()
    print(LINEA)
    print("CONTRA LA BASE")
    print(LINEA)
    print(f"  conversaciones aqui            {len(conocidos)}")
    print(f"  solo en Web (se importarian)   {len(nuevos)}")
    print(f"  aqui y Web no las ve           {len(faltan)}")
    print(f"  esperando referencia aqui      {len(esperando)}")

    if nuevos:
        clases = Counter(_clase(j) for j in nuevos)
        print(f"    y son: {dict(clases)}")

    # -- Y cuantas pasan NUESTRA validacion ---------------------------------
    validas = 0
    rechazadas: Counter = Counter()
    resolverian = 0
    for fila in filas:
        crudo = fila.get("candidate")
        if not crudo:
            continue
        motivo = validar(
            SeedCandidate(
                chat_jid=str(crudo.get("chat_jid") or ""),
                wa_msg_id=crudo.get("wa_msg_id"),
                timestamp=crudo.get("timestamp"),
                from_me=bool(crudo.get("from_me")),
                source=str(crudo.get("source") or "web_discovery"),
                message_type=crudo.get("message_type"),
            )
        )
        if motivo is None:
            validas += 1
            if str(fila.get("chat_jid")) in esperando:
                resolverian += 1
        else:
            rechazadas[motivo] += 1

    total = int(metricas.get("total", 0) or 0)
    cobertura = round(validas / total * 100) if total else 0

    print()
    print(LINEA)
    print("COBERTURA DE REFERENCIAS")
    print(LINEA)
    print(f"  validas para nuestro motor     {validas} de {total}  ({cobertura}%)")
    print(f"  resolverian una que espera     {resolverian} de {len(esperando)}")
    if rechazadas:
        print()
        print("  Rechazadas por Python:")
        for motivo, cuantos in rechazadas.most_common():
            print(f"    {motivo:<44} {cuantos}")

    # -- Los dos grupos que de verdad importan ------------------------------
    #
    # El total mezcla conversaciones que ya estaban resueltas. Lo que decide
    # si esto sirve es la cobertura de las que esperan y de las que solo ve
    # Web: las demas no necesitan nada.
    con_referencia = {
        str(f.get("chat_jid")) for f in filas if f.get("candidate")
    }
    print()
    print(LINEA)
    print("LOS DOS GRUPOS QUE IMPORTAN")
    print(LINEA)
    print(
        f"  esperando referencia           "
        f"{len(esperando & con_referencia)} de {len(esperando)}"
    )
    print(
        f"  solo en Web                    "
        f"{len(nuevos & con_referencia)} de {len(nuevos)}"
    )

    # -- Contra el sondeo antiguo -------------------------------------------
    sondeo = _sondeo(settings, timeout=args.timeout)
    if sondeo and not sondeo.get("error"):
        resumen = sondeo.get("summary") or sondeo
        del_sondeo = {
            str(f.get("chat_jid"))
            for f in (sondeo.get("chats") or [])
            if f.get("candidate")
        }
        print()
        print(LINEA)
        print("CONTRA EL SONDEO ANTIGUO")
        print(LINEA)
        print(f"  el sondeo encuentra            {len(del_sondeo)}")
        print(f"  el indice encuentra            {len(con_referencia)}")
        solo_sondeo = del_sondeo - con_referencia
        solo_indice = con_referencia - del_sondeo
        print(f"  solo el sondeo                 {len(solo_sondeo)}")
        print(f"  solo el indice                 {len(solo_indice)}")
        if solo_sondeo:
            # Si el sondeo ve algo que el indice no, es un fallo del indice:
            # los dos leen el mismo Store con el mismo extractor.
            motivos = Counter()
            porJid = {str(f.get("chat_jid")): f for f in filas}
            for jid in solo_sondeo:
                fila = porJid.get(jid)
                motivos[
                    "no la ve el indice"
                    if fila is None
                    else str(fila.get("no_seed_reason") or "?")
                ] += 1
            print("  y el indice dice de ellas:")
            for motivo, cuantos in motivos.most_common():
                print(f"    {motivo:<44} {cuantos}")
        if isinstance(resumen, dict) and resumen.get("with_messages") is not None:
            print(f"  (el sondeo vio mensajes en     {resumen.get('with_messages')})")

    print()
    print("Solo lectura: no se ha creado ninguna conversacion ni escrito ninguna")
    print("referencia. Para aplicarlo de verdad, el boton de sincronizar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
