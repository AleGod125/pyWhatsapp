"""Una foto de la sesión principal, con la misma vara que el navegador.

CUANDO SE USA
-------------
La escalera automática —la del registrador dentro del servicio— saca las
fotos de los 0, 30, 60 y 120 segundos sin que nadie tenga que acertar el
momento. Esta herramienta es para lo demás: una captura suelta, con etiqueta,
cuando hace falta mirar algo fuera de la ventana.

    py tools/j31_capture.py --etiqueta despues_de_own_live
    py tools/j31_capture.py --etiqueta con_todo --todas-las-fuentes

LA DIFERENCIA QUE IMPORTA
-------------------------
Por defecto se cuentan sólo las anclas del ARRANQUE (§18): fuera ``on_demand``,
que necesita un ancla previa para poder pedirse, y fuera ``live``, que mide la
actividad de la cuenta y no lo que trajo vincular. Con ``--todas-las-fuentes``
se cuenta todo, que sirve para ver el estado del producto pero **no** para
comparar bootstraps.

SOLO LECTURA
------------
Consulta PostgreSQL y escribe un JSON en ``debug/plan_j31/``. No toca la
sesión, no pide nada a la red y no modifica ni una fila.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import load_settings  # noqa: E402
from app.discovery.symmetric_snapshot import fotografiar_principal  # noqa: E402

LINEA = "=" * 68
CARPETA = pathlib.Path("debug/plan_j31")


def leer_t0(lado: str) -> float | None:
    """El T0 que apuntó el registrador. No se inventa uno si falta."""
    ruta = CARPETA / f"{lado}_t0.json"
    if not ruta.exists():
        return None
    try:
        return float(json.loads(ruta.read_text(encoding="utf-8"))["t0_epoch"])
    except (ValueError, KeyError, OSError):
        return None


def cuenta_activa(sesion) -> object | None:
    from app.models import WhatsAppAccount

    return sesion.execute(select(WhatsAppAccount.id).limit(1)).scalar()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--etiqueta", required=True, help="nombre de esta captura")
    parser.add_argument(
        "--todas-las-fuentes",
        action="store_true",
        help="cuenta tambien on_demand y live; NO sirve para comparar bootstraps",
    )
    parser.add_argument(
        "--t0",
        type=float,
        default=None,
        help="epoch del T0; por defecto el que apunto el registrador",
    )
    args = parser.parse_args()

    CARPETA.mkdir(parents=True, exist_ok=True)

    t0 = args.t0 if args.t0 is not None else leer_t0("primary")
    if t0 is None:
        t0 = time.time()
        aviso = "sin T0 apuntado: la edad de sesion de esta foto no es comparable"
    else:
        aviso = None

    settings = load_settings()
    motor = create_engine(settings.database_url)
    with Session(motor) as sesion:
        foto = fotografiar_principal(
            sesion,
            account_id=cuenta_activa(sesion),
            t0=t0,
            etiqueta=args.etiqueta,
            solo_bootstrap=not args.todas_las_fuentes,
        )

    cuerpo = foto.to_json()
    if aviso:
        cuerpo["notes"]["warning"] = aviso
    destino = CARPETA / f"primary_{args.etiqueta}.json"
    destino.write_text(json.dumps(cuerpo, indent=2, ensure_ascii=False), encoding="utf-8")

    metricas = cuerpo["metrics"]
    print(LINEA)
    print(f"PRINCIPAL: {args.etiqueta}  ({cuerpo['mode']})")
    print(LINEA)
    print(f"  edad de sesion            {cuerpo['session_age_seconds']} s")
    print(f"  conversaciones crudas     {metricas['raw_chat_count']}")
    print(f"  conversaciones de usuario {metricas['user_chat_count']}")
    print(f"    individuales            {metricas['individual_count']}")
    print(f"    grupos                  {metricas['group_count']}")
    print(f"    entidades especiales    {metricas['special_entity_count']}")
    print(f"  con nombre                {metricas['chats_with_name']}")
    print(f"  con actividad             {metricas['chats_with_activity']}")
    print(f"  con mensaje real          {metricas['chats_with_real_message']}")
    print(f"  CON ANCLA                 {metricas['chats_with_valid_seed']}")
    print(f"  con ancla (de usuario)    {metricas['user_chats_with_valid_seed']}")
    print(f"  mensajes                  {metricas['cached_message_count']}")
    print(f"  identificadores distintos {metricas['unique_wamid_count']}")
    print()
    print("  por clase:", cuerpo["by_class"] or "nada")
    print("  por origen del ancla:", cuerpo["by_seed_source"] or "ninguno")
    if aviso:
        print()
        print(f"  AVISO: {aviso}")
    print()
    print(f"Guardado en {destino}")
    print("Solo lectura: no se ha escrito ni una fila.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
