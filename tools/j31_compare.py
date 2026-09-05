"""La comparación que decide el Plan J: bootstrap contra bootstrap.

LA PREGUNTA
-----------
¿El navegador recibe de verdad un arranque mejor, o simplemente llevaba más
tiempo conectado? Las tres fases anteriores no podían contestarla porque
comparaban ocho conversaciones de un instante contra treinta y siete de varios
días.

Aquí se comparan dos fotos **de la misma edad**, sacadas con el mismo código y
con la misma clasificación. Si las edades no cuadran, se dice y no se emite
veredicto: volver a comparar edades sería repetir el error que motivó la fase.

    py tools/j31_compare.py
    py tools/j31_compare.py --peldano t120

QUE PRODUCE
-----------
``debug/plan_j31/comparison.json`` con la tabla de §66, la cobertura de §67, el
caso de §68-§72, las conversaciones que sólo ve el navegador (§73) y la
cobertura normalizada (§74). Todo con hashes, sin PII.

SOLO LECTURA
------------
Lee los JSON que ya escribió el registrador. No consulta la base, no toca la
sesión y no pide nada a la red.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.discovery.symmetric_snapshot import (  # noqa: E402
    CASO_DIFERENCIA_REAL,
    CASO_HUECO_DE_ANCLAS,
    CASO_HUECO_DE_DESCUBRIMIENTO,
    CASO_INCONCLUSO,
    CASO_PREMISA_FALSA,
    Fila,
    Foto,
    cobertura_normalizada,
    comparar,
    solo_del_navegador,
)

LINEA = "=" * 72
CARPETA = pathlib.Path("debug/plan_j31")

#: Qué significa cada caso, en una frase. Se imprime junto al veredicto porque
#: un identificador solo no le dice nada a quien lee el informe dentro de un
#: mes.
QUE_SIGNIFICA = {
    CASO_DIFERENCIA_REAL: (
        "El navegador recien nacido tiene bastantes mas anclas Y mas "
        "conversaciones. La diferencia es real: H3 se vuelve fuerte."
    ),
    CASO_PREMISA_FALSA: (
        "A la misma edad los dos se parecen. La premisa de las fases "
        "anteriores era FALSA: las 30-40 anclas del navegador eran "
        "acumulacion, no un arranque mejor."
    ),
    CASO_HUECO_DE_DESCUBRIMIENTO: (
        "El navegador ve mas conversaciones pero consigue anclas parecidas. "
        "El hueco es de DESCUBRIMIENTO, no de anclas."
    ),
    CASO_HUECO_DE_ANCLAS: (
        "Los dos ven las mismas conversaciones pero el navegador saca muchas "
        "mas anclas. El hueco es de HISTORIAL/CACHE, no de descubrimiento."
    ),
    CASO_INCONCLUSO: (
        "No se puede concluir. O las fotos no son de la misma edad, o la "
        "diferencia no apunta en la direccion que la hipotesis predecia."
    ),
}


def cargar(nombre: str) -> Foto | None:
    ruta = CARPETA / nombre
    if not ruta.exists():
        return None
    crudo = json.loads(ruta.read_text(encoding="utf-8"))
    # Un archivo congelado envuelve la foto; uno de la escalera es la foto.
    if "final" in crudo:
        crudo = crudo["final"]
    filas = [
        Fila(
            chat=f["chat"],
            clase=f["class"],
            tiene_nombre=f["has_name"],
            tiene_actividad=f["has_activity"],
            tiene_mensaje_real=f["has_real_message"],
            tiene_ancla=f["has_seed"],
            mensajes_en_memoria=f.get("cached_messages", 0),
            origen_del_ancla=f.get("seed_source"),
        )
        for f in crudo.get("chats", [])
    ]
    return Foto(
        lado=crudo["side"],
        etiqueta=crudo["label"],
        t0_epoch=crudo["t0_epoch"],
        capturado_epoch=crudo["captured_epoch"],
        modo=crudo.get("mode", "native"),
        filas=filas,
        mensajes_en_cache=crudo["metrics"]["cached_message_count"],
        wamids_distintos=crudo["metrics"]["unique_wamid_count"],
        por_origen=crudo.get("by_seed_source", {}),
        notas=crudo.get("notes", {}),
    )


def _fila(titulo: str, par: list[int]) -> str:
    return f"  {titulo:<28} {par[0]:>12} {par[1]:>12}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--peldano", default=None,
        help="que captura comparar (t000/t030/t060/t120/t300); "
             "por defecto la congelada",
    )
    args = parser.parse_args()

    if args.peldano:
        principal = cargar(f"primary_{args.peldano}.json")
        navegador = cargar(f"web_{args.peldano}.json")
        de_donde = f"peldano {args.peldano}"
    else:
        principal = cargar("PRIMARY_BOOTSTRAP_FINAL.json")
        navegador = cargar("WEB_BOOTSTRAP_FINAL.json")
        de_donde = "metricas congeladas"

    if principal is None or navegador is None:
        print("Faltan capturas. Lo que hay en debug/plan_j31/:")
        if CARPETA.exists():
            for ruta in sorted(CARPETA.glob("*.json")):
                print(f"  {ruta.name}")
        else:
            print("  (la carpeta no existe todavia)")
        print()
        print("La comparacion necesita las DOS fotos, y de la misma edad.")
        return 1

    resultado = comparar(principal, navegador)
    solo_web = solo_del_navegador(principal, navegador)
    normalizada = cobertura_normalizada(principal, navegador)

    print(LINEA)
    print(f"PRINCIPAL CONTRA NAVEGADOR  ({de_donde})")
    print(LINEA)
    print(f"  edad principal   {resultado['primary_age_seconds']} s")
    print(f"  edad navegador   {resultado['web_age_seconds']} s")
    if not resultado["symmetric"]:
        print()
        print("  LAS EDADES NO CUADRAN. Esta comparacion NO vale como")
        print("  bootstrap contra bootstrap, que es justo lo que se venia")
        print("  haciendo mal. No se emite veredicto.")
    print()
    print(f"  {'metrica':<28} {'PRINCIPAL':>12} {'NAVEGADOR':>12}")
    print(f"  {'-' * 28} {'-' * 12:>12} {'-' * 12:>12}")
    for titulo, clave in (
        ("conversaciones crudas", "raw_chats"),
        ("conversaciones de usuario", "user_chats"),
        ("  individuales", "individuals"),
        ("  grupos", "groups"),
        ("mensajes en cache", "cached_messages"),
        ("con mensaje real", "chats_with_real_message"),
        ("CON ANCLA VALIDA", "valid_seed_chats"),
        ("  de usuario", "user_seed_chats"),
        ("con nombre", "names"),
    ):
        print(_fila(titulo, resultado["table"][clave]))

    print()
    cobertura_chats = resultado["primary_user_chat_coverage_vs_web"]
    cobertura_anclas = resultado["primary_seed_coverage_vs_web"]
    print("  COBERTURA DE LA PRINCIPAL RESPECTO AL NAVEGADOR")
    print(f"    conversaciones de usuario  "
          f"{'n/d' if cobertura_chats is None else f'{cobertura_chats:.0%}'}")
    print(f"    anclas                     "
          f"{'n/d' if cobertura_anclas is None else f'{cobertura_anclas:.0%}'}")

    print()
    print(LINEA)
    print(f"CASO: {resultado['case']}")
    print(LINEA)
    for linea in QUE_SIGNIFICA[resultado["case"]].split(". "):
        if linea.strip():
            print(f"  {linea.strip().rstrip('.')}.")

    print()
    print(LINEA)
    print("SOLO EN EL NAVEGADOR")
    print(LINEA)
    print(f"  total                      {normalizada['web_only_total']}")
    print(f"  que si son conversaciones  {normalizada['web_only_user_visible']}")
    print(f"  entidades especiales       {normalizada['web_only_special']}")
    cobertura_norm = normalizada["normalized_user_chat_coverage"]
    print(f"  cobertura normalizada      "
          f"{'n/d' if cobertura_norm is None else f'{cobertura_norm:.0%}'}")
    if solo_web:
        print()
        print(f"  {'chat':<10} {'clase':<20} {'usuario':>8} {'mensaje':>8} {'ancla':>7}")
        for fila in solo_web[:40]:
            print(
                f"  {fila['chat']:<10} {fila['class']:<20} "
                f"{('si' if fila['user_visible'] else 'no'):>8} "
                f"{('si' if fila['has_real_message'] else 'no'):>8} "
                f"{('si' if fila['has_seed'] else 'no'):>7}"
            )

    cuerpo = {
        "source": de_donde,
        "comparison": resultado,
        "normalized": normalizada,
        "web_only": solo_web,
        "primary": principal.to_json()["metrics"],
        "web": navegador.to_json()["metrics"],
        "primary_by_class": principal.por_clase(),
        "web_by_class": navegador.por_clase(),
    }
    destino = CARPETA / "comparison.json"
    destino.write_text(json.dumps(cuerpo, indent=2, ensure_ascii=False), encoding="utf-8")
    print()
    print(f"Guardado en {destino}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
