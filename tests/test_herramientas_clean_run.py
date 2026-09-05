"""Las herramientas de la prueba desde cero: medir, comparar y resetear.

POR QUE IMPORTAN ESTAS PRUEBAS
------------------------------
Dos de las tres sólo leen, así que lo que hay que proteger es la tercera.
``reset_test_account.py`` borra el trabajo de varios días, y una herramienta
así se equivoca de una sola manera: borrando cuando no debía.

De ahí que lo probado aquí sea casi todo sobre cuándo NO borra.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _ejecutar(*argumentos: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *argumentos],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        timeout=180,
    )


# ---------------------------------------------------------------------------
# El reset: sobre todo, cuando NO borra
# ---------------------------------------------------------------------------


def test_por_defecto_no_borra_nada():
    """Ejecutarlo sin argumentos tiene que ser inofensivo."""
    salida = _ejecutar("tools/reset_test_account.py")
    assert salida.returncode == 0
    assert "MODO DE PRUEBA: no se ha borrado nada" in salida.stdout
    assert "fila(s) borradas" not in salida.stdout


def test_ensena_exactamente_que_borraria():
    salida = _ejecutar("tools/reset_test_account.py")
    assert "SE BORRARIA" in salida.stdout
    for tabla in ("messages", "chats", "history_seeds", "whatsapp_accounts"):
        assert tabla in salida.stdout


def test_ensena_tambien_que_NO_borraria():
    """Lo que sobrevive es tan importante como lo que se va."""
    salida = _ejecutar("tools/reset_test_account.py")
    assert "NO SE TOCA" in salida.stdout
    for preservado in ("users", "google_credentials", "drive_folders"):
        assert preservado in salida.stdout


def test_execute_sin_la_frase_no_borra():
    salida = _ejecutar("tools/reset_test_account.py", "--execute")
    assert salida.returncode == 2
    assert "No se ha borrado nada" in salida.stdout
    assert "fila(s) borradas" not in salida.stdout


def test_una_frase_equivocada_tampoco():
    salida = _ejecutar(
        "tools/reset_test_account.py", "--execute", "--confirm", "si"
    )
    assert salida.returncode == 2
    assert "fila(s) borradas" not in salida.stdout


def test_la_frase_es_dificil_de_teclear_sin_querer():
    from tools.reset_test_account import FRASE

    assert FRASE == "RESET_TEST_ACCOUNT"
    assert FRASE.isupper() and len(FRASE) > 10


def test_google_drive_no_esta_en_la_lista_de_borrado():
    """Es la garantía que sostiene todo el aislamiento del clean-run."""
    from tools.reset_test_account import PRESERVADAS, TABLAS

    for tabla in ("drive_folders", "google_drive_storage", "google_credentials"):
        assert tabla not in TABLAS
        assert tabla in PRESERVADAS


def test_el_usuario_de_la_aplicacion_sobrevive():
    """Volver a registrarse no forma parte de lo que se prueba."""
    from tools.reset_test_account import PRESERVADAS, TABLAS

    assert "users" not in TABLAS
    assert "users" in PRESERVADAS


def test_se_borra_la_cuenta_de_whatsapp_para_que_la_nueva_sea_nueva():
    """De ahí sale el aislamiento: cuenta nueva, carpeta de Drive nueva."""
    from tools.reset_test_account import TABLAS

    assert "whatsapp_accounts" in TABLAS


def test_el_orden_respeta_las_claves_foraneas():
    """Las hojas antes que la raíz; al revés, el borrado falla a medias."""
    from tools.reset_test_account import TABLAS

    orden = list(TABLAS)
    assert orden.index("media_files") < orden.index("messages")
    assert orden.index("message_segments") < orden.index("messages")
    assert orden.index("messages") < orden.index("chats")
    assert orden.index("chat_history_state") < orden.index("chats")
    assert orden.index("chats") < orden.index("whatsapp_accounts")


def test_borra_la_sesion_para_que_haga_falta_un_QR_nuevo():
    import inspect

    from tools import reset_test_account

    fuente = inspect.getsource(reset_test_account._rutas)
    assert "device.json" in fuente
    assert "signal_store_file" in fuente
    assert "web_companion" in fuente


# ---------------------------------------------------------------------------
# El baseline
# ---------------------------------------------------------------------------


def test_el_baseline_solo_lee():
    salida = _ejecutar("tools/capture_baseline.py", "--no-guardar")
    assert salida.returncode == 0
    assert "BASELINE" in salida.stdout
    assert "No se ha guardado nada" in salida.stdout


def test_el_baseline_no_lleva_datos_personales(tmp_path):
    """Es un archivo que se guarda y se comparte: sólo recuentos."""
    from app.core.config import load_settings
    from tools.capture_baseline import capturar

    datos = capturar(load_settings())
    texto = json.dumps(datos)

    # Ni identificadores de conversación, ni teléfonos, ni texto.
    for prohibido in ("@s.whatsapp.net", "@lid", "@g.us", "jid", "text", "name"):
        assert prohibido not in texto, f"el baseline lleva {prohibido}"
    # Y todo lo que hay son números o etiquetas de estado.
    for clave in ("chats_total", "messages", "waiting_seed", "exhausted"):
        assert isinstance(datos[clave], int)


def test_el_baseline_mide_lo_que_hace_falta_comparar():
    from app.core.config import load_settings
    from tools.capture_baseline import capturar

    datos = capturar(load_settings())
    for clave in (
        "chats_total",
        "messages",
        "media_total",
        "history_seeds",
        "web_seeds_applied",
        "history_requests",
        "waiting_seed",
        "exhausted",
        "timeout",
        "pending",
        "on_demand_capability",
    ):
        assert clave in datos


def test_el_baseline_separa_las_anclas_de_la_via_web():
    """Es el número que dice si la recuperación automática hizo su trabajo."""
    from app.core.config import load_settings
    from tools.capture_baseline import capturar

    datos = capturar(load_settings())
    assert datos["web_seeds_applied"] <= datos["history_seeds"]


# ---------------------------------------------------------------------------
# El comparador
# ---------------------------------------------------------------------------


def _baseline(**cambios) -> dict:
    base = {
        "captured_at": "2026-09-05T00:00:00+00:00",
        "session_fingerprint": "abc",
        "on_demand_capability": "CONFIRMED",
        "chats_total": 41,
        "messages": 3826,
        "media_total": 629,
        "history_seeds": 4907,
        "web_seeds_applied": 22,
        "history_requests": 155,
        "waiting_seed": 3,
        "pending": 1,
        "timeout": 3,
        "exhausted": 34,
    }
    base.update(cambios)
    return base


def test_mas_mensajes_es_mejor():
    from tools.compare_baselines import comparar

    filas = dict((f[0], f[5]) for f in comparar(_baseline(), _baseline(messages=4200)))
    assert filas["mensajes"] == "mejor"


def test_menos_esperando_referencia_es_mejor():
    from tools.compare_baselines import comparar

    filas = dict((f[0], f[5]) for f in comparar(_baseline(), _baseline(waiting_seed=0)))
    assert filas["esperando referencia"] == "mejor"


def test_mas_esperando_referencia_es_PEOR():
    from tools.compare_baselines import comparar

    filas = dict((f[0], f[5]) for f in comparar(_baseline(), _baseline(waiting_seed=12)))
    assert filas["esperando referencia"] == "PEOR"


def test_un_resultado_identico_se_llama_igual():
    from tools.compare_baselines import comparar

    for fila in comparar(_baseline(), _baseline()):
        assert fila[5] == "igual"


def test_el_numero_de_conversaciones_solo_se_informa():
    """Que cambie no es bueno ni malo: depende de la cuenta, no de nosotros."""
    from tools.compare_baselines import comparar

    filas = dict((f[0], f[5]) for f in comparar(_baseline(), _baseline(chats_total=39)))
    assert filas["conversaciones"] == "distinto"


def test_el_comparador_avisa_de_una_vinculacion_nueva(tmp_path, capsys):
    """En una prueba desde cero la huella CAMBIA, y eso es lo esperado."""
    import sys as _sys

    from tools import compare_baselines

    antes = tmp_path / "antes.json"
    despues = tmp_path / "despues.json"
    antes.write_text(json.dumps(_baseline()), encoding="utf-8")
    despues.write_text(
        json.dumps(_baseline(session_fingerprint="xyz")), encoding="utf-8"
    )

    argv = _sys.argv
    _sys.argv = ["compare_baselines.py", str(antes), str(despues)]
    try:
        compare_baselines.main()
    finally:
        _sys.argv = argv

    salida = capsys.readouterr().out
    assert "vinculacion NUEVA" in salida
    assert "el backup anterior no se toca" in salida


# ---------------------------------------------------------------------------
# El borrado completo: usuario y Drive
# ---------------------------------------------------------------------------


def test_borrar_drive_necesita_SU_PROPIA_frase():
    """Una sola confirmacion para dos cosas tan distintas invita a error.

    Vaciar la base es molesto y rehacible. Borrar los archivos de Drive sale
    de esta maquina y no vuelve.
    """
    salida = _ejecutar(
        "tools/reset_test_account.py",
        "--execute",
        "--confirm",
        "RESET_TEST_ACCOUNT",
        "--delete-drive-backup",
    )
    assert salida.returncode == 2
    assert "No se ha borrado nada" in salida.stdout
    assert "fila(s) borradas" not in salida.stdout


def test_la_frase_de_drive_es_distinta_de_la_otra():
    from tools.reset_test_account import FRASE, FRASE_DRIVE

    assert FRASE_DRIVE == "BORRAR_MI_BACKUP"
    assert FRASE_DRIVE != FRASE


def test_sin_las_banderas_no_se_toca_ni_el_usuario_ni_drive():
    """El comportamiento por defecto no cambia."""
    salida = _ejecutar("tools/reset_test_account.py")
    assert "Los ARCHIVOS de Google Drive no se tocan" in salida.stdout
    assert "--delete-user" not in salida.stdout.split("MODO DE PRUEBA")[0]


def test_con_delete_user_se_avisa_de_que_habra_que_registrarse():
    salida = _ejecutar("tools/reset_test_account.py", "--delete-user")
    assert "Arrancaras desde el registro" in salida.stdout
    for tabla in ("users", "google_credentials", "user_storage_keys"):
        assert tabla in salida.stdout


def test_con_delete_drive_se_avisa_de_que_no_hay_vuelta_atras():
    salida = _ejecutar("tools/reset_test_account.py", "--delete-drive-backup")
    assert "ESTO NO SE PUEDE DESHACER" in salida.stdout
    # Y ya no se promete que Drive queda intacto.
    assert "Los ARCHIVOS de Google Drive no se tocan" not in salida.stdout


def test_el_orden_de_las_tablas_de_usuario_respeta_las_claves():
    """El usuario es la raiz: se va el ultimo."""
    from tools.reset_test_account import TABLAS_DE_USUARIO

    orden = list(TABLAS_DE_USUARIO)
    assert orden[-1] == "users"
    for hoja in ("drive_folders", "google_credentials", "user_sessions"):
        assert orden.index(hoja) < orden.index("users")


def test_drive_se_borra_ANTES_que_la_base():
    """Las credenciales para llegar a Drive viven en la base.

    Al reves quedarian los archivos huerfanos y sin forma de encontrarlos
    desde aqui.
    """
    import ast
    import inspect
    import textwrap

    from tools import reset_test_account

    codigo = ast.unparse(
        ast.parse(textwrap.dedent(inspect.getsource(reset_test_account.main)))
    )
    assert codigo.index("_borrar_en_drive") < codigo.index("_borrar(settings")


def test_solo_se_borran_las_carpetas_del_backup():
    """Las raices, no el Drive entero.

    Se leen de `google_drive_storage` y de las rutas sin barra, que son las de
    primer nivel. Borrando una, Drive se lleva lo que cuelga.
    """
    import inspect

    from tools import reset_test_account

    fuente = inspect.getsource(reset_test_account._carpetas_de_drive)
    assert "GoogleDriveStorage" in fuente
    assert "root_folder_id" in fuente
    # Nunca se listan archivos sueltos ni se borra por consulta abierta.
    assert "files/list" not in fuente
    assert "q=" not in fuente


def test_si_no_hay_token_no_se_borra_nada_en_drive():
    """Sin credenciales se avisa; no se deja el trabajo a medias en silencio."""
    import inspect

    from tools import reset_test_account

    fuente = inspect.getsource(reset_test_account._borrar_en_drive)
    assert "NADA se borro en Drive" in fuente
    assert "puedes borrarlos a mano" in fuente
