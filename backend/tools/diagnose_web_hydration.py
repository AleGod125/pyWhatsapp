"""¿Cuánto tarda WhatsApp Web en materializar sus mensajes?

LA PREGUNTA
-----------
La misma cuenta dio dos resultados muy distintos:

    sesión vinculada el día anterior    22 de 25 conversaciones con referencia
    sesión recién vinculada             6 de 32

La diferencia no estaba en el código. WhatsApp Web va materializando los
mensajes poco a poco, y un sondeo hecho a los dos minutos de escanear el
código es una foto de casi nada.

Esta herramienta repite el sondeo cada cierto tiempo y enseña la evolución,
para poder decidir con datos cuánto conviene esperar.

    py tools/diagnose_web_hydration.py
    py tools/diagnose_web_hydration.py --cada 60 --durante 1800

SOLO LECTURA
------------
Usa el mismo endpoint que el botón «Probar cobertura Web», que declara
`read_only: true`, `mutations: 0` y `on_demand_requests: 0`. No aplica
referencias, no escribe y no pide historial.

Necesita `service.py` en marcha: el sondeo lo hace su Web Companion.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import load_settings  # noqa: E402

LINEA = "=" * 72


def _pedir(url: str, timeout: float = 300.0) -> dict | None:
    peticion = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
            return json.loads(respuesta.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode())
        except Exception:  # noqa: BLE001
            return {"error": {"code": f"HTTP_{exc.code}", "message": str(exc)}}
    except urllib.error.URLError as exc:
        return {"error": {"code": "SIN_API", "message": str(exc)}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cada", type=float, default=60.0, help="segundos entre sondeos (60)"
    )
    parser.add_argument(
        "--durante", type=float, default=900.0, help="cuanto tiempo medir (900)"
    )
    args = parser.parse_args()

    settings = load_settings()
    base = f"http://127.0.0.1:{settings.api_port}/api/v1/web-companion"

    print(LINEA)
    print("HIDRATACION DE WHATSAPP WEB")
    print(LINEA)
    print(f"  un sondeo cada {args.cada:.0f}s durante {args.durante:.0f}s")
    print("  solo lectura: no se aplica ninguna referencia")
    print()
    print(
        f"  {'hora':<10}{'t+':>7}{'esperan':>9}{'visibles':>10}"
        f"{'con msg':>9}{'usables':>9}  observacion"
    )
    print("  " + "-" * 68)

    comenzo = time.monotonic()
    anterior: int | None = None
    mejor = 0
    rondas = 0

    while time.monotonic() - comenzo < args.durante:
        rondas += 1
        datos = _pedir(f"{base}/probe")
        ahora = datetime.now().strftime("%H:%M:%S")
        transcurrido = time.monotonic() - comenzo

        if datos is None or "error" in datos:
            motivo = (datos or {}).get("error", {})
            codigo = motivo.get("code", "?") if isinstance(motivo, dict) else "?"
            print(f"  {ahora:<10}{transcurrido:>6.0f}s  {codigo}")
            if codigo == "SIN_API":
                print()
                print("  Arranca 'py service.py' primero: el sondeo lo hace su")
                print("  Web Companion, no esta herramienta.")
                return 1
        else:
            usables = int(datos.get("seed_usable", 0) or 0)
            mejor = max(mejor, usables)
            nota = ""
            if anterior is not None:
                if usables > anterior:
                    nota = f"+{usables - anterior} (sigue hidratando)"
                elif usables < anterior:
                    nota = "menos que antes (la pagina se recargo?)"
                else:
                    nota = "sin cambios"
            print(
                f"  {ahora:<10}{transcurrido:>6.0f}s"
                f"{int(datos.get('waiting', 0) or 0):>9}"
                f"{int(datos.get('visible_store', 0) or 0):>10}"
                f"{int(datos.get('with_messages', 0) or 0):>9}"
                f"{usables:>9}  {nota}"
            )
            anterior = usables

        restante = args.durante - (time.monotonic() - comenzo)
        if restante <= 0:
            break
        time.sleep(min(args.cada, restante))

    print()
    print(LINEA)
    print(f"  {rondas} sondeo(s). Mejor cobertura observada: {mejor} referencias.")
    print(LINEA)
    print()
    print("  Si el numero sube con el tiempo, conviene esperar antes de dar")
    print("  por buena la cobertura. Si baja, la sesion Web se estaba")
    print("  recargando: no es un fallo del extractor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
