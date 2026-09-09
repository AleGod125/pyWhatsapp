"""Huellas de las sesiones Signal propias. Solo lectura, sin material de clave.

PARA QUE SIRVE
--------------
El Plan J3.1 quiere saber si vincular el navegador toca algo del estado Signal
de la sesión principal. La respuesta esperada es que no —son dos dispositivos
distintos de la misma cuenta, con sus propios ratchets— pero «esperada» no es
«medida», y hay un fallo abierto de mensajes propios que no llegan.

Así que se toma una huella antes y otra después, y se comparan. Si algo
cambió, se sabrá cuándo; si no cambió, esa hipótesis queda descartada con
datos en vez de con un razonamiento.

    py tools/j31_signal_fingerprint.py --etiqueta antes_de_vincular_web
    py tools/j31_signal_fingerprint.py --comparar antes_de_vincular_web despues

QUE NO SALE DE AQUI (§84)
-------------------------
Ni claves de identidad, ni prekeys, ni claves de ratchet o de cadena. De cada
sesión sale un hash del registro, su tamaño y la dirección **truncada**. Un
hash permite decir «cambió» sin decir a qué cambió, que es justo lo que hace
falta.

SOLO LECTURA
------------
Abre el Signal Store en modo lectura. No descifra, no escribe y no toca la red.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.core.config import load_settings  # noqa: E402

LINEA = "=" * 68
CARPETA = pathlib.Path("debug/plan_j31")


def _huella(dato) -> str:
    """Un hash del contenido. Dice si cambió, no dice a qué."""
    if dato is None:
        return "-"
    crudo = dato if isinstance(dato, (bytes, bytearray)) else str(dato).encode("utf-8")
    return hashlib.sha256(crudo).hexdigest()[:16]


def _direccion(session_id: str) -> str:
    """La dirección, reconocible pero sin el número entero.

    Interesa distinguir PN de LID y saber que son dos direcciones distintas del
    mismo aparato. Para eso basta el servidor y cuatro dígitos.
    """
    texto = str(session_id or "")
    usuario, _, resto = texto.partition("@")
    if not resto:
        return f"{_huella(texto)}"
    servidor = resto.split(".")[0] if "." in resto else resto
    cola = usuario[-4:] if len(usuario) >= 4 else usuario
    return f"...{cola}@{servidor}"


def _clase(session_id: str) -> str:
    texto = str(session_id or "")
    if "@lid" in texto:
        return "LID"
    if "@s.whatsapp.net" in texto or "@c.us" in texto:
        return "PN"
    return "otra"


def tomar_huella(ruta: pathlib.Path, etiqueta: str) -> dict:
    if not ruta.exists():
        return {
            "label": etiqueta,
            "taken_at": time.time(),
            "store_present": False,
            "sessions": [],
        }

    conexion = sqlite3.connect(f"file:{ruta}?mode=ro", uri=True)
    try:
        sesiones = []
        for session_id, estado in conexion.execute(
            "SELECT session_id, state FROM sessions"
        ):
            sesiones.append(
                {
                    "address": _direccion(session_id),
                    "kind": _clase(session_id),
                    "record_fingerprint": _huella(estado),
                    "record_bytes": len(estado) if estado is not None else 0,
                }
            )
        identidades = {}
        for session_id, identidad in conexion.execute(
            "SELECT session_id, identity FROM identities"
        ):
            identidades[_direccion(session_id)] = _huella(identidad)
        prekeys = conexion.execute("SELECT COUNT(*) FROM prekeys").fetchone()[0]
        pares = conexion.execute("SELECT COUNT(*) FROM lid_map").fetchone()[0]
    finally:
        conexion.close()

    sesiones.sort(key=lambda s: (s["kind"], s["address"]))
    return {
        "label": etiqueta,
        "taken_at": time.time(),
        "store_present": True,
        "store_mtime": ruta.stat().st_mtime,
        "session_count": len(sesiones),
        "pn_sessions": sum(1 for s in sesiones if s["kind"] == "PN"),
        "lid_sessions": sum(1 for s in sesiones if s["kind"] == "LID"),
        "prekeys": prekeys,
        "lid_map_pairs": pares,
        "sessions": sesiones,
        "identities": identidades,
    }


def comparar(antes: dict, despues: dict) -> dict:
    """Qué cambió entre dos huellas (§85)."""
    a = {s["address"]: s for s in antes.get("sessions", [])}
    b = {s["address"]: s for s in despues.get("sessions", [])}
    nuevas = sorted(set(b) - set(a))
    idas = sorted(set(a) - set(b))
    cambiadas = sorted(
        d
        for d in set(a) & set(b)
        if a[d]["record_fingerprint"] != b[d]["record_fingerprint"]
    )
    return {
        "new_sessions": nuevas,
        "gone_sessions": idas,
        "changed_sessions": cambiadas,
        "prekeys_before": antes.get("prekeys"),
        "prekeys_after": despues.get("prekeys"),
        "changed": bool(nuevas or idas or cambiadas),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--etiqueta", help="nombre de esta huella")
    parser.add_argument(
        "--comparar", nargs=2, metavar=("ANTES", "DESPUES"),
        help="compara dos huellas ya tomadas",
    )
    args = parser.parse_args()

    CARPETA.mkdir(parents=True, exist_ok=True)

    if args.comparar:
        antes_ruta = CARPETA / f"signal_{args.comparar[0]}.json"
        despues_ruta = CARPETA / f"signal_{args.comparar[1]}.json"
        for ruta in (antes_ruta, despues_ruta):
            if not ruta.exists():
                print(f"No existe {ruta}")
                return 1
        antes = json.loads(antes_ruta.read_text(encoding="utf-8"))
        despues = json.loads(despues_ruta.read_text(encoding="utf-8"))
        resultado = comparar(antes, despues)
        print(LINEA)
        print(f"SIGNAL: {args.comparar[0]} -> {args.comparar[1]}")
        print(LINEA)
        print(f"  sesiones nuevas      {resultado['new_sessions'] or 'ninguna'}")
        print(f"  sesiones que se van  {resultado['gone_sessions'] or 'ninguna'}")
        print(f"  sesiones cambiadas   {resultado['changed_sessions'] or 'ninguna'}")
        print(f"  prekeys              {resultado['prekeys_before']} -> {resultado['prekeys_after']}")
        print()
        if resultado["changed"]:
            print("  CAMBIO el estado Signal entre las dos huellas.")
            print("  Ojo: trafico normal tambien lo cambia. Coincidir en el")
            print("  tiempo con el emparejamiento del navegador NO demuestra")
            print("  que lo causara.")
        else:
            print("  NO cambio nada.")
        return 0

    if not args.etiqueta:
        parser.error("hace falta --etiqueta o --comparar")

    settings = load_settings()
    ruta = pathlib.Path(str(settings.session_file) + ".signal.db")
    huella = tomar_huella(ruta, args.etiqueta)
    destino = CARPETA / f"signal_{args.etiqueta}.json"
    destino.write_text(
        json.dumps(huella, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(LINEA)
    print(f"HUELLA SIGNAL: {args.etiqueta}")
    print(LINEA)
    if not huella["store_present"]:
        print("  No hay Signal Store. No hay sesion vinculada.")
    else:
        print(f"  sesiones          {huella['session_count']}  "
              f"(PN {huella['pn_sessions']}, LID {huella['lid_sessions']})")
        print(f"  prekeys           {huella['prekeys']}")
        print(f"  pares PN<->LID    {huella['lid_map_pairs']}")
        print()
        print(f"  {'direccion':<22} {'tipo':<6} {'huella':<18} {'bytes':>7}")
        for sesion in huella["sessions"]:
            print(f"  {sesion['address']:<22} {sesion['kind']:<6} "
                  f"{sesion['record_fingerprint']:<18} {sesion['record_bytes']:>7}")
    print()
    print(f"Guardado en {destino}")
    print("Sin material de clave: solo hashes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
