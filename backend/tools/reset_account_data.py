"""Vaciar los datos de UNA cuenta, sin tocar su vinculación de WhatsApp.

PARA QUE SIRVE
--------------
Probar la extracción desde cero **sin volver a escanear ningún código**. Se
borra lo que la aplicación ha recogido —conversaciones, mensajes, anclas,
multimedia— y se deja intacto lo que identifica al dispositivo ante WhatsApp.

LA DIFERENCIA CON ``reset_test_account.py``
-------------------------------------------
Aquélla deja la instalación como recién puesta: borra también la sesión y el
Signal Store, así que obliga a volver a vincular. Ésta **no**. La identidad y
el ratchet no se tocan, y son indivisibles: media identidad no sirve de nada, y
recrearla cuesta un código QR.

    py tools/reset_account_data.py --listar                          # ver cuentas
    py tools/reset_account_data.py --account-id <UUID>               # solo enseña
    py tools/reset_account_data.py --account-id <UUID> --execute --confirm BORRAR_DATOS

EL IDENTIFICADOR ES OBLIGATORIO
-------------------------------
Sin ``--account-id`` esto aborta, y no hay ningún atajo que borre «todas». Un
borrado global existía ya en la otra herramienta, y esconderlo detrás de un
argumento que se puede olvidar es como se pierden los datos de otra cuenta.

QUE NO SE TOCA, NUNCA
---------------------
``session/`` entera —``device.json``, el Signal Store, las prekeys de
compatibilidad y el LocalAuth del navegador—, el usuario, su login, sus
credenciales de Google, y la fila de ``whatsapp_accounts``: la cuenta sigue
vinculada al terminar.

OJO CON GOOGLE DRIVE
--------------------
Los archivos remotos no se tocan. Pero el índice local que los enlazaba cuelga
de los mensajes que se borran, así que el backup anterior deja de listarse.
Para una prueba de extracción eso es lo que se quiere; si además hubiera una
relectura automática desde Drive, se avisa antes de borrar.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import load_settings  # noqa: E402

LINEA = "=" * 70

#: La frase. No es una molestia gratuita: esto borra el trabajo de una sesión
#: entera y un `--yes` se teclea sin pensar.
FRASE = "BORRAR_DATOS"

#: Lo que se vacía, EN ESTE ORDEN: las hojas antes que las ramas.
#:
#: Cada entrada dice cómo se llega desde la fila hasta la cuenta. Las que no
#: llevan ``whatsapp_account_id`` se resuelven por su conversación, que sí lo
#: lleva; así una fila de otra cuenta no puede colarse ni por descuido.
BORRADOS = (
    (
        "media_files",
        "DELETE FROM media_files WHERE chat_id IN "
        "(SELECT id FROM chats WHERE whatsapp_account_id = :cuenta)",
    ),
    (
        "message_segments",
        "DELETE FROM message_segments WHERE whatsapp_account_id = :cuenta",
    ),
    (
        "messages",
        "DELETE FROM messages WHERE chat_id IN "
        "(SELECT id FROM chats WHERE whatsapp_account_id = :cuenta)",
    ),
    (
        "history_requests",
        "DELETE FROM history_requests WHERE chat_id IN "
        "(SELECT id FROM chats WHERE whatsapp_account_id = :cuenta)",
    ),
    (
        "chat_history_state",
        "DELETE FROM chat_history_state WHERE chat_id IN "
        "(SELECT id FROM chats WHERE whatsapp_account_id = :cuenta)",
    ),
    ("history_seeds", "DELETE FROM history_seeds WHERE whatsapp_account_id = :cuenta"),
    ("storage_jobs", "DELETE FROM storage_jobs WHERE whatsapp_account_id = :cuenta"),
    # Los blobs ya leidos: sin esto, el escaner los da por vistos y no
    # volveria a sacar ni un ancla de ellos.
    ("scanned_blobs", "DELETE FROM scanned_blobs WHERE whatsapp_account_id = :cuenta"),
    ("chats", "DELETE FROM chats WHERE whatsapp_account_id = :cuenta"),
)

#: Los recuentos, con la misma condición que el borrado. Se enseñan antes.
CUENTAS = tuple(
    (nombre, sql.replace("DELETE FROM", "SELECT COUNT(*) FROM", 1))
    for nombre, sql in BORRADOS
)

#: Sin ``whatsapp_account_id`` ni conversación por la que llegar. Se ofrece
#: aparte porque un contacto no es dato de extracción: es la agenda.
CONTACTOS_SQL = "DELETE FROM contacts"

#: Lo que jamás toca esta herramienta. Se enseña para que se vea.
INTOCABLE = (
    "session/ entera (device.json, Signal Store, prekeys, LocalAuth)",
    "users, user_sessions, user_storage_keys",
    "google_credentials, google_drive_storage, drive_folders",
    "whatsapp_accounts (la cuenta sigue vinculada)",
    "los archivos remotos de Google Drive",
)


def _cuentas_disponibles(conexion) -> list[tuple]:
    return list(
        conexion.execute(
            text(
                "SELECT a.id, a.session_status, u.email, "
                "(SELECT COUNT(*) FROM chats c WHERE c.whatsapp_account_id = a.id) "
                "FROM whatsapp_accounts a JOIN users u ON u.id = a.user_id "
                "ORDER BY a.id"
            )
        )
    )


def _salud_de_la_sesion(settings) -> dict[str, bool]:
    """Lo que pide §35: qué hay antes y después, para poder compararlo."""
    raiz = pathlib.Path(settings.session_dir)
    return {
        "session_present": (raiz / "device.json").exists(),
        "signal_present": (raiz / "device.json.signal.db").exists(),
        "compat_prekey_present": (raiz / "compat_prekey.db").exists(),
    }


def _avisar_de_drive(settings) -> None:
    """Si el almacenamiento pudiera devolver el historial nada más borrarlo."""
    if not getattr(settings, "drive_storage_enabled", False):
        return
    print()
    print("  AVISO SOBRE GOOGLE DRIVE")
    print("  El almacenamiento en Drive esta ENCENDIDO. Los archivos remotos")
    print("  no se tocan, y el indice local que los enlazaba se borra con los")
    print("  mensajes, asi que la copia anterior dejara de listarse.")
    print("  Si quieres una prueba sin ninguna relectura, apaga")
    print("  DRIVE_STORAGE_ENABLED mientras dure el experimento.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", help="la cuenta a vaciar. OBLIGATORIO.")
    parser.add_argument("--listar", action="store_true", help="ver las cuentas")
    parser.add_argument("--execute", action="store_true", help="borrar de verdad")
    parser.add_argument("--confirm", default="", help=f"la frase: {FRASE}")
    parser.add_argument(
        "--borrar-contactos",
        action="store_true",
        help="vaciar tambien la agenda, para simular un arranque limpio",
    )
    args = parser.parse_args()

    settings = load_settings()
    motor = create_engine(settings.database_url)

    if args.listar:
        with motor.connect() as conexion:
            filas = _cuentas_disponibles(conexion)
        print(LINEA)
        print("CUENTAS DE WHATSAPP EN ESTA INSTALACION")
        print(LINEA)
        if not filas:
            print("  Ninguna.")
        for id_, estado, correo, chats in filas:
            print(f"  {id_}  {str(estado):<10} {chats:>4} chats  {correo}")
        return 0

    # -- El identificador es obligatorio. Sin atajos. -----------------------
    if not args.account_id:
        print("Falta --account-id. Esta herramienta NO borra 'todas las cuentas':")
        print("hay que decir cual, siempre.")
        print()
        print("  py tools/reset_account_data.py --listar")
        return 2

    with motor.connect() as conexion:
        existe = conexion.execute(
            text("SELECT session_status FROM whatsapp_accounts WHERE id = :c"),
            {"c": args.account_id},
        ).fetchone()
        if existe is None:
            print(f"No hay ninguna cuenta con id {args.account_id}.")
            print("  py tools/reset_account_data.py --listar")
            return 2

        recuentos = []
        for nombre, sql in CUENTAS:
            cuantas = conexion.execute(text(sql), {"cuenta": args.account_id}).scalar()
            recuentos.append((nombre, int(cuantas or 0)))
        contactos = int(
            conexion.execute(text("SELECT COUNT(*) FROM contacts")).scalar() or 0
        )

    salud_antes = _salud_de_la_sesion(settings)

    print(LINEA)
    print(f"SE VACIARIA LA CUENTA {args.account_id}")
    print(LINEA)
    for nombre, cuantas in recuentos:
        print(f"    {nombre:<22} {cuantas:>7} fila(s)")
    if args.borrar_contactos:
        print(f"    {'contacts':<22} {contactos:>7} fila(s)  (--borrar-contactos)")
    else:
        print(f"    {'contacts':<22} {'--':>7}           (usa --borrar-contactos)")

    print()
    print("  NO SE TOCA:")
    for linea in INTOCABLE:
        print(f"    {linea}")
    print()
    print("  SALUD DE LA SESION (antes):")
    for clave, valor in salud_antes.items():
        print(f"    {clave:<24} {'si' if valor else 'no'}")
    print()
    print(f"  La cuenta sigue vinculada ({existe[0]}). NO hara falta ningun QR.")
    _avisar_de_drive(settings)

    if not args.execute:
        print()
        print("MODO DE PRUEBA: no se ha borrado nada.")
        print("Para borrar de verdad:")
        print(
            f"  py tools/reset_account_data.py --account-id {args.account_id} "
            f"--execute --confirm {FRASE}"
        )
        return 0

    if args.confirm != FRASE:
        print()
        print(f"Falta la confirmacion. Anade: --confirm {FRASE}")
        return 2

    print()
    print(LINEA)
    print("BORRANDO")
    print(LINEA)
    with motor.begin() as conexion:
        for nombre, sql in BORRADOS:
            resultado = conexion.execute(text(sql), {"cuenta": args.account_id})
            print(f"  {nombre:<22} {resultado.rowcount:>7} fila(s) borradas")
        if args.borrar_contactos:
            resultado = conexion.execute(text(CONTACTOS_SQL))
            print(f"  {'contacts':<22} {resultado.rowcount:>7} fila(s) borradas")

    salud_despues = _salud_de_la_sesion(settings)
    print()
    print("  SALUD DE LA SESION (despues):")
    for clave, valor in salud_despues.items():
        marca = "" if salud_antes[clave] == valor else "   <-- CAMBIO INESPERADO"
        print(f"    {clave:<24} {'si' if valor else 'no'}{marca}")

    if salud_antes != salud_despues:
        print()
        print("  AVISO: la sesion NO deberia haber cambiado. Revisalo antes de")
        print("  arrancar el servicio.")
        return 1

    print()
    print("Hecho. La sesion de WhatsApp y el Signal Store estan intactos:")
    print("arranca 'py service.py' y la extraccion empezara de cero sin pedir QR.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
