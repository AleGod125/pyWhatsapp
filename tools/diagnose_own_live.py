"""¿Aparecen en la app los mensajes que envío desde mi teléfono? Solo lectura.

LA PREGUNTA
-----------
Cuando escribo desde el móvil, WhatsApp manda una copia a este companion. Si
esa copia no se puede autenticar, se pide el reenvío — y si el reenvío no
llega, el mensaje se queda fuera de la copia local aunque en WhatsApp Web sí
se vea.

Esta herramienta responde tres cosas con datos, no con suposiciones:

* ¿cuántas copias propias llegaron y cuántas se autenticaron?
* ¿cuántas se pidieron otra vez, y cuántas volvieron?
* ¿hay conversaciones donde lo último guardado sea más viejo que lo que ve
  WhatsApp Web?

    py tools/diagnose_own_live.py
    py tools/diagnose_own_live.py --last 20

SOLO LECTURA
------------
No descifra, no pide nada al teléfono, no escribe ni una fila y no toca Signal.
Consulta la base y, si el servicio está en marcha, le pregunta su estado.

PRIVACIDAD
----------
Ni texto, ni teléfonos completos, ni identificadores completos. **Nunca**
material de claves.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import load_settings  # noqa: E402
from app.models import Chat, Message  # noqa: E402

LINEA = "=" * 70


def _corto(valor: str | None, visible: int = 6) -> str:
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
            .strftime("%Y-%m-%d %H:%M:%S")
        )
    except Exception:  # noqa: BLE001
        return str(ts)


def _estado_del_servicio(settings, *, timeout: float = 10.0) -> dict | None:
    """Lo que el servicio en marcha sabe de esta sesión. Solo lectura."""
    import json
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{settings.api_port}/api/v1/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as respuesta:
            return json.loads(respuesta.read().decode())
    except Exception:  # noqa: BLE001 - sin servicio se sigue con la base
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--last", type=int, default=10, help="cuantos mensajes propios")
    args = parser.parse_args()

    settings = load_settings()
    motor = create_engine(settings.database_url)

    print(LINEA)
    print("MIS MENSAJES, LOS QUE ENVIO DESDE EL TELEFONO")
    print(LINEA)

    with Session(motor) as sesion:
        total = sesion.execute(
            select(func.count()).select_from(Message).where(Message.from_me.is_(True))
        ).scalar()
        print(f"  copias propias guardadas       {total}")

        ultimos = sesion.execute(
            select(
                Message.whatsapp_message_id,
                Message.chat_jid,
                Message.timestamp,
                Message.message_type,
                Message.source,
            )
            .where(Message.from_me.is_(True))
            .order_by(Message.timestamp.desc())
            .limit(args.last)
        ).all()

        if ultimos:
            print()
            print(f"  Las {len(ultimos)} mas recientes:")
            print(f"    {'cuando':<21} {'chat':<22} {'tipo':<10} via")
            for wamid, jid, ts, tipo, origen in ultimos:
                print(
                    f"    {_fecha(ts):<21} {_corto(jid):<22} "
                    f"{str(tipo or '-'):<10} {origen or '-'}"
                )

        # -- Donde puede faltar algo por arriba ----------------------------
        #
        # Lo ultimo guardado de cada conversacion frente a la actividad que la
        # propia conversacion declara. Si la actividad es mas nueva que el
        # ultimo mensaje, ahi falta el borde.
        print()
        print(LINEA)
        print("BORDES QUE PODRIAN TENER UN AGUJERO")
        print(LINEA)
        filas = sesion.execute(
            select(
                Chat.jid,
                Chat.last_message_timestamp,
                func.max(Message.timestamp),
            )
            .join(Message, Message.chat_jid == Chat.jid, isouter=True)
            .group_by(Chat.jid, Chat.last_message_timestamp)
        ).all()

        sospechosos = [
            (jid, actividad, ultimo)
            for jid, actividad, ultimo in filas
            if actividad and ultimo and int(actividad) > int(ultimo) + 60
        ]
        if not sospechosos:
            print("  Ninguno: lo guardado esta al dia con la actividad conocida.")
        else:
            print(f"  {len(sospechosos)} conversacion(es):")
            print(f"    {'chat':<22} {'ultimo guardado':<21} actividad conocida")
            for jid, actividad, ultimo in sorted(
                sospechosos, key=lambda f: -(int(f[1]) - int(f[2]))
            )[:15]:
                print(f"    {_corto(jid):<22} {_fecha(ultimo):<21} {_fecha(actividad)}")
            print()
            print("  Una diferencia aqui NO prueba que falte un mensaje: la")
            print("  actividad puede venir del indice de WhatsApp Web. Es donde")
            print("  mirar, no un veredicto.")

    # -- Lo que sabe el servicio en marcha ----------------------------------
    estado = _estado_del_servicio(settings)
    print()
    print(LINEA)
    print("LA SESION EN MARCHA")
    print(LINEA)
    if estado is None:
        print("  El servicio no contesta. Arranca 'py service.py' para ver")
        print("  los contadores de descifrado y de acuses de reintento.")
        return 0

    print(f"  estado                         {estado.get('status', '?')}")
    print(f"  sesion                         {estado.get('state', '?')}")
    print(f"  sesion guardada                {'si' if estado.get('session_file_present') else 'no'}")
    print(f"  WhatsApp habilitado            {'si' if estado.get('whatsapp_enabled') else 'no'}")
    print()
    print("  Los contadores de copias propias y de acuses salen en el registro")
    print("  del servicio, en las lineas [OWN_LIVE] y app.live.")

    _direcciones_propias(settings)
    _resumen_own_live(settings)

    print()
    print("Solo lectura: no se ha escrito ni una fila.")
    return 0


# ---------------------------------------------------------------------------
# La direccion criptografica: donde estaba el fallo
# ---------------------------------------------------------------------------


def _direcciones_propias(settings) -> None:
    """Bajo que direccion vive la sesion con nuestro propio telefono.

    AQUI ESTABA EL BUG, y por eso esto es lo primero que hay que mirar.

    Un mensaje escrito desde el telefono llega dirigido a NUESTRO PROPIO LID.
    La sesion con ese aparato existe, pero guardada bajo la otra direccion, la
    del numero. Son dos identificadores del mismo telefono, y la busqueda va
    por la direccion literal, asi que no la encuentra:

        no session for peer <nuestro propio LID>

    pywhats sabe resolverlo --migra la sesion del numero al LID-- pero para eso
    necesita el par PN<->LID en el ``lid_map``, y ese par no llega solo: hay
    que sembrarlo. Si la linea de abajo dice que NO esta, los mensajes propios
    no se van a poder descifrar.
    """
    import sqlite3

    from app.core.identity import own_identity

    print()
    print(LINEA)
    print("LA DIRECCION CON LA QUE LLEGAN MIS COPIAS")
    print(LINEA)

    pn_jid, lid_jid = own_identity(settings)
    pn = (pn_jid or "").split("@")[0].split(".")[0]
    lid = (lid_jid or "").split("@")[0].split(".")[0]
    if not pn or not lid:
        print("  Todavia no hay identidad propia completa. Sin ella no se")
        print("  puede sembrar el par, y las copias propias no se descifran.")
        return

    almacen = settings.signal_store_file
    if not almacen.exists():
        print("  No hay Signal Store: no hay vinculacion.")
        return

    try:
        conexion = sqlite3.connect(f"file:{almacen.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"  No se pudo leer el Signal Store: {exc}")
        return

    try:
        par = conexion.execute(
            "SELECT pn_user FROM lid_map WHERE lid_user = ?", (lid,)
        ).fetchone()
        sesiones = [
            sid for (sid,) in conexion.execute("SELECT session_id FROM sessions")
        ]
    except sqlite3.Error as exc:
        print(f"  No se pudo leer el Signal Store: {exc}")
        return
    finally:
        conexion.close()

    resuelve = bool(par) and str(par[0]) == pn
    por_pn = [s for s in sesiones if s.startswith(f"{pn}:")]
    por_lid = [s for s in sesiones if s.startswith(f"{lid}:")]

    print(f"  par PN<->LID propio sembrado   {'si' if resuelve else 'NO'}")
    print(f"  sesion por numero (PN)         {len(por_pn)}")
    print(f"  sesion por LID                 {len(por_lid)}")
    print()
    if resuelve:
        print("  Bien. Cuando llegue una copia dirigida al LID, la sesion del")
        print("  numero se migra sola y el mensaje se descifra.")
    else:
        print("  ESTO ES EL FALLO. Sin el par, una copia dirigida a tu propio")
        print("  LID no encuentra sesion y muere con 'no session for peer'.")
        print("  Se siembra al llegar el <success>; reinicia 'py service.py'.")


# ---------------------------------------------------------------------------
# El resumen que pide el plan
# ---------------------------------------------------------------------------

#: Lo que se busca en el registro. Cada patron contesta una casilla, y ninguno
#: depende del texto del mensaje: el registro no lo lleva, a proposito.
_HUELLAS = {
    "no_session": "no session for peer",
    "mac_fail": "mac check failed",
    "pkmsg": "type=pkmsg",
    "opk_desconocida": "unknown one-time pre-key id",
}


def _resumen_own_live(settings) -> None:
    """El recuento de §79, contando SOLO nuestras propias copias.

    Contar todos los fallos de descifrado mezcla los mensajes de otros con los
    nuestros, y son problemas distintos: el de un tercero es su ratchet, el
    nuestro es una direccion que no se encuentra. Se filtra por remitente.
    """
    import pathlib as _pathlib

    from app.core.identity import own_identity

    _pn, lid_jid = own_identity(settings)
    lid = (lid_jid or "").split("@")[0].split(".")[0]

    registro = _pathlib.Path(settings.diagnostics_dir) / "app.log"
    print()
    print(LINEA)
    print("[OWN_LIVE] RESUMEN")
    print(LINEA)
    if not lid or not registro.exists():
        print("  Sin registro o sin identidad todavia.")
        return

    conteo = dict.fromkeys(_HUELLAS, 0)
    lineas = 0
    with registro.open("r", encoding="utf-8", errors="replace") as fichero:
        try:
            fichero.seek(max(0, registro.stat().st_size - 4_000_000))
            fichero.readline()
        except OSError:
            pass
        for linea in fichero:
            if lid not in linea or "decrypt failed" not in linea:
                continue
            lineas += 1
            for clave, patron in _HUELLAS.items():
                if patron in linea:
                    conteo[clave] += 1

    print(f"  fallos en copias PROPIAS       {lineas}")
    for clave in ("no_session", "mac_fail", "opk_desconocida"):
        print(f"    {clave:<28} {conteo[clave]}")
    print()
    print("  (ultimos 4 MB del registro, y solo lo dirigido a tu propio LID)")
    print()
    if conteo["no_session"]:
        print("  'no_session' en copias propias apunta al par PN<->LID: mira")
        print("  la seccion de arriba.")
    if conteo["opk_desconocida"]:
        print("  'opk_desconocida' es un pkmsg que reusa una clave de un solo")
        print("  uso ya consumida. La compatibilidad de reutilizacion de")
        print("  ratchet lo cubre cuando el establecimiento es el mismo.")


if __name__ == "__main__":
    raise SystemExit(main())
