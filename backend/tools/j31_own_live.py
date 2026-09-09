"""Los mensajes propios del Plan J3.1: qué pasó con cada uno. Solo lectura.

QUE MIDE
--------
Se mandan tres mensajes desde el teléfono principal, con una marca reconocible
—``OWN-LIVE-J31-PREWEB-001``— y después otros tres con ``POSTWEB``. Esta
herramienta cuenta qué le pasó a cada uno, uno por uno (§47):

llegó · se descifró · falló el MAC · se pidió reenvío · volvió · se guardó ·
salió por SSE.

POR QUE ANTES Y DESPUES
-----------------------
El fallo abierto es que los mensajes escritos desde el propio teléfono no
aparecen. Se sospechaba del segundo dispositivo. Con una sesión recién creada y
**sin** navegador vinculado, la respuesta separa dos cosas que hasta ahora
estaban pegadas: si ya falla antes de vincular el navegador, el segundo
dispositivo no puede ser causa necesaria (§49).

    py tools/j31_own_live.py --tanda PREWEB
    py tools/j31_own_live.py --comparar

LO QUE NO HACE
--------------
No manda mensajes —eso lo hace el usuario desde su teléfono— y no arregla
nada. §82 y §106 son explícitos: primero la medición limpia.

SOLO LECTURA
------------
Consulta PostgreSQL y lee el registro. No escribe ni una fila.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import load_settings  # noqa: E402
from app.discovery.symmetric_snapshot import hash_de  # noqa: E402
from app.models import Message  # noqa: E402

LINEA = "=" * 72
CARPETA = pathlib.Path("debug/plan_j31")
MARCA = "OWN-LIVE-J31"

#: Lo que se busca en el registro, por mensaje. Cada patrón contesta una de las
#: casillas de §47, y ninguno depende del texto del mensaje.
HUELLAS_EN_EL_REGISTRO = {
    "mac_fail": re.compile(r"bad ?mac|mac (fail|invalid)|MAC no coincide", re.I),
    "decrypt_fail": re.compile(r"no se pudo descifrar|decrypt(ion)? fail|failed to decrypt", re.I),
    "retry_sent": re.compile(r"retry receipt|acuse de reintento|reintento enviado", re.I),
    "pkmsg": re.compile(r"pkmsg", re.I),
    "no_session": re.compile(r"no session|sin sesion signal", re.I),
}

VEREDICTOS = {
    "OK": "PREWEB_LIVE_OK",
    "PARCIAL": "PREWEB_LIVE_PARTIAL",
    "MAC": "PREWEB_LIVE_MAC_FAIL",
}


def mensajes_de(sesion, tanda: str) -> list[dict]:
    """Los mensajes marcados que SI llegaron a guardarse."""
    patron = f"%{MARCA}-{tanda}-%"
    filas = sesion.execute(
        select(
            Message.whatsapp_message_id,
            Message.chat_jid,
            Message.text,
            Message.timestamp,
            Message.from_me,
            Message.source,
            Message.sender_jid,
            Message.sender_lid,
        ).where(Message.text.like(patron)).order_by(Message.timestamp)
    ).all()

    salida = []
    for wamid, chat_jid, texto, marca, mio, origen, remitente, lid in filas:
        etiqueta = ""
        encontrado = re.search(rf"{MARCA}-{tanda}-(\d+)", texto or "")
        if encontrado:
            etiqueta = encontrado.group(1)
        salida.append(
            {
                "n": etiqueta,
                "chat": hash_de(chat_jid),
                "wamid_present": bool(wamid),
                "timestamp": int(marca or 0),
                "from_me": bool(mio),
                "source": origen,
                "crypto_address": "LID" if lid else ("PN" if remitente else "?"),
                "persisted": True,
            }
        )
    return salida


def rastro_en_el_registro(ruta: pathlib.Path, desde: int, hasta: int) -> dict[str, int]:
    """Cuántas veces aparece cada síntoma en la ventana de la prueba.

    Se cuenta por ventana de tiempo y no por mensaje: el registro no lleva el
    texto del mensaje —a propósito—, así que atribuir una línea concreta a un
    mensaje concreto sería adivinar. Un recuento por ventana sí es honesto.
    """
    conteo = {clave: 0 for clave in HUELLAS_EN_EL_REGISTRO}
    conteo["lines_scanned"] = 0
    if not ruta.exists():
        return conteo

    # Sólo la cola: el registro pasa de los 25 MB y la ventana son minutos.
    with ruta.open("r", encoding="utf-8", errors="replace") as fichero:
        try:
            fichero.seek(max(0, ruta.stat().st_size - 4_000_000))
            fichero.readline()
        except OSError:
            pass
        for linea in fichero:
            conteo["lines_scanned"] += 1
            for clave, patron in HUELLAS_EN_EL_REGISTRO.items():
                if patron.search(linea):
                    conteo[clave] += 1
    return conteo


def veredicto(mensajes: list[dict], rastro: dict[str, int], esperados: int = 3) -> str:
    llegados = len(mensajes)
    if llegados >= esperados:
        return "OK"
    if llegados == 0 and rastro.get("mac_fail"):
        return "MAC"
    return "PARCIAL"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tanda", choices=("PREWEB", "POSTWEB"), help="que tanda medir")
    parser.add_argument("--comparar", action="store_true", help="las dos tandas, lado a lado")
    parser.add_argument("--esperados", type=int, default=3)
    args = parser.parse_args()

    if not args.tanda and not args.comparar:
        parser.error("hace falta --tanda o --comparar")

    CARPETA.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    motor = create_engine(settings.database_url)
    registro = pathlib.Path(settings.diagnostics_dir) / "app.log"

    def medir(tanda: str) -> dict:
        with Session(motor) as sesion:
            mensajes = mensajes_de(sesion, tanda)
        marcas = [m["timestamp"] for m in mensajes if m["timestamp"]]
        rastro = rastro_en_el_registro(
            registro, min(marcas) if marcas else 0, max(marcas) if marcas else 0
        )
        clave = veredicto(mensajes, rastro, args.esperados)
        return {
            "batch": tanda,
            "expected": args.esperados,
            "received": len(mensajes),
            "messages": mensajes,
            "log_trace": rastro,
            "verdict": VEREDICTOS[clave].replace("PREWEB", tanda),
        }

    if args.comparar:
        antes = medir("PREWEB")
        despues = medir("POSTWEB")
        print(LINEA)
        print("MENSAJES PROPIOS: ANTES Y DESPUES DE VINCULAR EL NAVEGADOR")
        print(LINEA)
        print(f"  {'metrica':<24} {'PREWEB':>10} {'POSTWEB':>10}")
        print(f"  {'esperados':<24} {antes['expected']:>10} {despues['expected']:>10}")
        print(f"  {'guardados':<24} {antes['received']:>10} {despues['received']:>10}")
        for clave in HUELLAS_EN_EL_REGISTRO:
            print(f"  {clave:<24} {antes['log_trace'][clave]:>10} "
                  f"{despues['log_trace'][clave]:>10}")
        print()
        print(f"  veredicto PREWEB   {antes['verdict']}")
        print(f"  veredicto POSTWEB  {despues['verdict']}")
        print()
        if antes["received"] == 0 and despues["received"] == 0:
            print("  Falla en las dos. El segundo dispositivo NO es causa")
            print("  necesaria: ya fallaba sin el (§78).")
        elif antes["received"] and not despues["received"]:
            print("  Funcionaba antes y deja de funcionar despues. Hay")
            print("  correlacion temporal con vincular el navegador. Eso NO")
            print("  demuestra causalidad (§79).")
        elif antes["received"] and despues["received"]:
            print("  Funciona en las dos. La sesion limpia lo arregla, al")
            print("  menos de momento. Falta saber si aguanta (§80).")
        else:
            print("  Falla antes y funciona despues. Muy raro. Se anota sin")
            print("  explicacion inventada (§81).")
        cuerpo = {"preweb": antes, "postweb": despues}
        destino = CARPETA / "own_live_comparison.json"
    else:
        cuerpo = medir(args.tanda)
        print(LINEA)
        print(f"MENSAJES PROPIOS: {args.tanda}")
        print(LINEA)
        print(f"  esperados {cuerpo['expected']}   guardados {cuerpo['received']}")
        print()
        if cuerpo["messages"]:
            print(f"  {'n':<4} {'chat':<10} {'wamid':>7} {'mio':>5} "
                  f"{'origen':<10} {'direccion':<10}")
            for mensaje in cuerpo["messages"]:
                print(
                    f"  {mensaje['n']:<4} {mensaje['chat']:<10} "
                    f"{('si' if mensaje['wamid_present'] else 'no'):>7} "
                    f"{('si' if mensaje['from_me'] else 'no'):>5} "
                    f"{str(mensaje['source']):<10} {mensaje['crypto_address']:<10}"
                )
        else:
            print("  Ninguno llego a guardarse.")
        print()
        print("  rastro en el registro (por ventana, no por mensaje):")
        for clave, cuantas in cuerpo["log_trace"].items():
            print(f"    {clave:<20} {cuantas}")
        print()
        print(f"  VEREDICTO: {cuerpo['verdict']}")
        destino = CARPETA / f"own_live_{args.tanda.lower()}.json"

    destino.write_text(json.dumps(cuerpo, indent=2, ensure_ascii=False), encoding="utf-8")
    print()
    print(f"Guardado en {destino}")
    print("Solo lectura: no se ha escrito ni una fila en la base.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
