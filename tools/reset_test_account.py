"""Dejar la cuenta como recién instalada, para una prueba desde cero.

ESTO BORRA. LEE ESTO ANTES DE EJECUTARLO.
=========================================
Por defecto **no borra nada**: enseña qué borraría y se va. Para que borre de
verdad hacen falta las dos banderas y la frase completa::

    py tools/reset_test_account.py                       # solo enseña
    py tools/reset_test_account.py --execute --confirm RESET_TEST_ACCOUNT

QUE BORRA
---------
Los datos de la copia y la vinculación de WhatsApp: conversaciones, mensajes,
multimedia, anclas, peticiones, estados de historial y la cuenta de WhatsApp;
la sesión de pywhats con su Signal Store; los blobs de historial archivados; y
la sesión del Web Companion.

QUE NO BORRA POR DEFECTO
------------------------
* **Tu usuario ni tu login de Google.** Volver a registrarse no suele formar
  parte de lo que se prueba.
* **Nada de Google Drive.** Ni carpetas, ni segmentos, ni credenciales.

Y eso último se sostiene solo: al volver a vincular se crea una cuenta de
WhatsApp NUEVA, con identificador nuevo, y el almacenamiento cuelga de ese
identificador. La prueba escribe en su propia carpeta y no puede leer ni pisar
la anterior.

PARA DEJARLO COMO RECIÉN INSTALADO
----------------------------------
Dos banderas más, cada una con su confirmación, porque cada una borra algo
distinto y de una no se vuelve::

    --delete-user            el usuario, sus sesiones y sus credenciales de
                             Google. Arrancarás desde el registro.

    --delete-drive-backup    los ARCHIVOS del backup en tu Google Drive.
                             IRREVERSIBLE: Drive no tiene papelera para esto
                             una vez vaciada, y nosotros no la vaciamos pero
                             tampoco podemos garantizar que esté.

Las tres juntas dejan la aplicación como el primer día::

    py tools/reset_test_account.py --execute --confirm RESET_TEST_ACCOUNT \
        --delete-user --delete-drive-backup --confirm-drive BORRAR_MI_BACKUP
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import load_settings  # noqa: E402

LINEA = "=" * 68

#: La frase exacta. No es una molestia gratuita: esta herramienta borra el
#: trabajo de varios días y un `--yes` se teclea sin pensar.
FRASE = "RESET_TEST_ACCOUNT"

#: Confirmacion aparte para los archivos de Drive. Es la unica parte de todo
#: esto que sale de esta maquina y no se puede deshacer.
FRASE_DRIVE = "BORRAR_MI_BACKUP"

#: Lo que se anade con ``--delete-user``. En este orden: las hojas primero.
TABLAS_DE_USUARIO = (
    "drive_folders",
    "google_drive_storage",
    "google_credentials",
    "user_storage_keys",
    "user_sessions",
    "users",
)

#: En qué orden se vacían las tablas. De las hojas hacia la raíz: al revés,
#: las claves foráneas lo impiden.
TABLAS = (
    "media_files",
    "message_segments",
    "messages",
    "history_requests",
    "history_seeds",
    "scanned_blobs",
    "chat_history_state",
    "chats",
    "contacts",
    "storage_jobs",
    "app_state",
    "whatsapp_accounts",
)

#: Lo que NUNCA se toca, y por que.
#:
#: ``drive_folders``, ``google_drive_storage`` y ``google_credentials`` guardan
#: DONDE esta el backup en Drive y como llegar. Borrarlos no borraria los
#: archivos, pero dejaria huerfano lo subido y obligaria a reconectar Google
#: sin necesidad. La vinculacion nueva crea sus propias carpetas.
PRESERVADAS = (
    "users",
    "user_sessions",
    "user_storage_keys",
    "google_credentials",
    "google_drive_storage",
    "drive_folders",
)

#: Lo que hay que quitar del disco para que la vinculación sea de verdad nueva.
def _rutas(settings) -> list[tuple[str, Path]]:
    sesion = Path(settings.session_dir)
    return [
        ("sesion de pywhats", sesion / "device.json"),
        ("Signal Store", Path(settings.signal_store_file)),
        ("prekeys de compatibilidad", sesion / "compat_prekey.db"),
        ("sesion del Web Companion", sesion / "web_companion"),
        # El OTRO dispositivo vinculado experimental. Faltaba, y su ausencia
        # se midio: `check_clean_state` daba la limpieza por incompleta porque
        # esta carpeta sobrevivia. Una sesion vinculada superviviente es justo
        # lo que invalida una prueba que dice partir de cero.
        ("sesion del bootstrap web", sesion / "web_bootstrap"),
        ("blobs de historial archivados", Path("data/history")),
        ("cache local", Path("data/cache")),
        # Los laterales de SQLite. Borrar el .db y dejar el -wal deja escritas
        # sin volcar de la vinculacion anterior: SQLite las recupera al abrir
        # un archivo nuevo con el mismo nombre.
        ("diario del Signal Store", Path(str(settings.signal_store_file) + "-wal")),
        ("indice del Signal Store", Path(str(settings.signal_store_file) + "-shm")),
        ("diario de las prekeys", sesion / "compat_prekey.db-wal"),
        ("indice de las prekeys", sesion / "compat_prekey.db-shm"),
    ]


def _contar(settings) -> dict[str, int]:
    """Cuántas filas hay ahora en cada tabla que se vaciaría."""
    from sqlalchemy import text

    salida: dict[str, int] = {}
    with Session(create_engine(settings.database_url)) as sesion:
        for tabla in TABLAS:
            try:
                salida[tabla] = int(
                    sesion.execute(text(f"SELECT count(*) FROM {tabla}")).scalar() or 0
                )
            except Exception:  # noqa: BLE001 - una tabla que no exista cuenta 0
                # PostgreSQL aborta la transaccion entera al primer error: sin
                # este rollback, TODAS las tablas siguientes contaban 0 y el
                # informe decia que no habia nada que borrar.
                sesion.rollback()
                salida[tabla] = 0
    return salida


def _preservado(settings) -> dict[str, int]:
    """Lo que se queda. Se cuenta para poder comprobarlo después."""
    from sqlalchemy import text

    salida: dict[str, int] = {}
    with Session(create_engine(settings.database_url)) as sesion:
        for tabla in PRESERVADAS:
            try:
                salida[tabla] = int(
                    sesion.execute(text(f"SELECT count(*) FROM {tabla}")).scalar() or 0
                )
            except Exception:  # noqa: BLE001
                sesion.rollback()
                salida[tabla] = 0
    return salida


def _mostrar(settings, *, tambien_usuario=False, tambien_drive=False) -> None:
    filas = _contar(settings)
    print(LINEA)
    print("SE BORRARIA")
    print(LINEA)
    for tabla, cuantas in filas.items():
        marca = " " if cuantas else "."
        print(f"  {marca} {tabla:<28} {cuantas} fila(s)")

    print()
    print("  Del disco:")
    for etiqueta, ruta in _rutas(settings):
        existe = "existe" if ruta.exists() else "no existe"
        print(f"    {etiqueta:<32} {existe}")

    preservadas = dict(_preservado(settings))

    if tambien_usuario:
        print()
        print("  Y ademas, por --delete-user:")
        for tabla in TABLAS_DE_USUARIO:
            print(f"    {tabla:<28} {preservadas.pop(tabla, 0)} fila(s)")
        print("    Arrancaras desde el registro, como un usuario nuevo.")

    if tambien_drive:
        print()
        print("  Y en GOOGLE DRIVE, por --delete-drive-backup:")
        for etiqueta, folder_id in _carpetas_de_drive(settings):
            print(f"    {etiqueta:<28} {folder_id[:12]}...")
        print()
        print("    ESTO NO SE PUEDE DESHACER. Los archivos del backup se van.")

    print()
    print(LINEA)
    print("NO SE TOCA")
    print(LINEA)
    if not preservadas:
        print("    (nada: se borra todo)")
    for tabla, cuantas in preservadas.items():
        print(f"    {tabla:<28} {cuantas} fila(s)")
    if not tambien_drive:
        print()
        print("  Los ARCHIVOS de Google Drive no se tocan: siguen en tu Drive.")
        print("  Lo que si desaparece es el indice local que los enlazaba,")
        print("  porque cuelga de los mensajes que se borran. El backup")
        print("  anterior queda guardado, pero la aplicacion dejara de listarlo.")
    print()
    print("  Al volver a vincular se crea una cuenta de WhatsApp NUEVA, con")
    print("  identificador nuevo. El almacenamiento cuelga de ese identificador,")
    print("  asi que la prueba escribe en su propia carpeta y no puede leer ni")
    print("  pisar el backup anterior.")


def _carpetas_de_drive(settings) -> list[tuple[str, str]]:
    """Las carpetas raiz del backup, con su id de Drive.

    Solo las RAIZ: borrando una, Drive se lleva todo lo que cuelga. Buscar
    archivo por archivo seria mas lento y dejaria huecos si alguno cambio de
    sitio.
    """
    from sqlalchemy import select

    from app.models import DriveFolder, GoogleDriveStorage

    salida: list[tuple[str, str]] = []
    with Session(create_engine(settings.database_url)) as sesion:
        for fila in sesion.execute(select(GoogleDriveStorage)).scalars():
            if fila.root_folder_id:
                salida.append(("carpeta raiz del backup", fila.root_folder_id))
        # Y por si alguna raiz quedo registrada solo como ruta.
        for fila in sesion.execute(
            select(DriveFolder).where(~DriveFolder.path.contains("/"))
        ).scalars():
            if fila.folder_id and all(fila.folder_id != f for _, f in salida):
                salida.append((f"carpeta '{fila.path}'", fila.folder_id))
    return salida


def _borrar_en_drive(settings) -> None:
    """Borra las carpetas del backup EN Google Drive.

    Se hace ANTES de tocar la base: las credenciales viven ahi, y sin ellas no
    hay forma de llegar a Drive. Al reves quedarian los archivos huerfanos y
    sin manera de encontrarlos desde aqui.
    """
    from sqlalchemy import select

    from app.auth.google_service import GoogleService
    from app.core.database import Database
    from app.models import User
    from app.storage.drive.client import DriveClient

    carpetas = _carpetas_de_drive(settings)
    if not carpetas:
        print("  No hay ninguna carpeta de backup registrada en Drive.")
        return

    base = Database(settings)
    base.connect()
    try:
        servicio = GoogleService(base, settings)
        with Session(create_engine(settings.database_url)) as sesion:
            usuarios = [u for u in sesion.execute(select(User.id)).scalars()]

        token = None
        for user_id in usuarios:
            try:
                token = servicio.access_token(user_id)
            except Exception:  # noqa: BLE001
                token = None
            if token:
                break

        if not token:
            print("  No se pudo obtener un token de Google: NADA se borro en Drive.")
            print("  Los archivos siguen en tu Drive; puedes borrarlos a mano.")
            return

        cliente = DriveClient(token)
        for etiqueta, folder_id in carpetas:
            if cliente.borrar(folder_id):
                print(f"  {etiqueta:<32} borrada de Drive")
            else:
                print(f"  {etiqueta:<32} NO se pudo borrar (sigue en tu Drive)")
    finally:
        try:
            base.close()
        except Exception:  # noqa: BLE001
            pass


def _borrar(settings, *, tambien_usuario: bool = False) -> None:
    from sqlalchemy import text

    motor = create_engine(settings.database_url)
    with Session(motor) as sesion:
        # Una sola transaccion: o se vacia todo o no se vacia nada. Un reset a
        # medias deja un estado que no es ni el viejo ni el nuevo.
        # Si una tabla falla, NO se sigue: en PostgreSQL la transaccion ya
        # esta abortada y todo lo demas contaria 0 borrados sin haber borrado
        # nada. Un reset que dice "hecho" sin haberlo hecho es peor que uno
        # que falla.
        objetivo = TABLAS + (TABLAS_DE_USUARIO if tambien_usuario else ())
        with sesion.begin():
            for tabla in objetivo:
                borradas = sesion.execute(text(f"DELETE FROM {tabla}")).rowcount
                print(f"  {tabla:<28} {borradas or 0} fila(s) borradas")

    print()
    for etiqueta, ruta in _rutas(settings):
        if not ruta.exists():
            print(f"  {etiqueta:<32} no estaba")
            continue
        try:
            if ruta.is_dir():
                shutil.rmtree(ruta)
            else:
                ruta.unlink()
            print(f"  {etiqueta:<32} borrado")
        except OSError as exc:
            print(f"  {etiqueta:<32} NO se pudo borrar: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true", help="borrar de verdad (necesita --confirm)"
    )
    parser.add_argument(
        "--confirm", default="", help=f"la frase exacta: {FRASE}"
    )
    parser.add_argument(
        "--delete-user",
        action="store_true",
        help="borrar tambien el usuario, sus sesiones y las credenciales de Google",
    )
    parser.add_argument(
        "--delete-drive-backup",
        action="store_true",
        help="borrar tambien los ARCHIVOS del backup en Google Drive (irreversible)",
    )
    parser.add_argument(
        "--confirm-drive",
        default="",
        help=f"confirmacion aparte para Drive: {FRASE_DRIVE}",
    )
    args = parser.parse_args()

    settings = load_settings()
    _mostrar(
        settings,
        tambien_usuario=args.delete_user,
        tambien_drive=args.delete_drive_backup,
    )

    if not args.execute:
        print()
        print("MODO DE PRUEBA: no se ha borrado nada.")
        print("Para borrar de verdad:")
        print(f"  py tools/reset_test_account.py --execute --confirm {FRASE}")
        return 0

    if args.confirm != FRASE:
        print()
        print(f"Falta la confirmacion. Anade:  --confirm {FRASE}")
        print("No se ha borrado nada.")
        return 2

    # Drive lleva su propia frase. Es lo unico de todo esto que sale de esta
    # maquina, y una sola confirmacion para dos cosas tan distintas invita a
    # llevarse por delante el backup sin querer.
    if args.delete_drive_backup and args.confirm_drive != FRASE_DRIVE:
        print()
        print("--delete-drive-backup borra archivos de tu Google Drive y eso NO")
        print("se puede deshacer. Hace falta su propia confirmacion:")
        print(f"  --confirm-drive {FRASE_DRIVE}")
        print("No se ha borrado nada.")
        return 2

    # Con service.py en marcha, borrar el Signal Store mientras lo usa deja un
    # archivo a medias y una sesion que ya no descifra nada.
    from app.core.lock import SessionLock

    try:
        cerrojo = SessionLock(Path(settings.session_dir), owner="reset")
        titular = cerrojo.read()
        # Que exista el archivo NO significa que haya nadie dentro. Un
        # service.py cerrado a lo bruto -- o una ventana que se fue con la
        # sesion de Windows -- deja el cerrojo escrito. Se pregunta lo mismo
        # que pregunta el arranque: si el PID sigue vivo y si alguien lo esta
        # refrescando. Si no, el cerrojo esta muerto y no bloquea nada.
        if titular is not None and not cerrojo._is_stale(titular):
            print()
            print("Hay un service.py en marcha:")
            print(f"  {titular.describe()}")
            print("Cierralo antes de resetear: borrar la sesion mientras se usa")
            print("la deja inservible a medias.")
            return 3
        if titular is not None:
            print()
            print(
                f"Cerrojo huerfano de un service.py que ya no existe "
                f"(PID {titular.pid}); se retira."
            )
            try:
                cerrojo.path.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001 - si no se puede comprobar, se avisa
        print()
        print("AVISO: no se pudo comprobar si service.py esta en marcha.")
        print("Asegurate de haberlo cerrado.")

    print()
    print(LINEA)
    print("BORRANDO")
    print(LINEA)

    # Drive PRIMERO: las credenciales para llegar hasta alli viven en la base
    # que se vacia justo despues.
    if args.delete_drive_backup:
        print("  En Google Drive:")
        _borrar_en_drive(settings)
        print()

    _borrar(settings, tambien_usuario=args.delete_user)

    print()
    if args.delete_user:
        print("Hecho. La aplicacion arranca desde el registro, como recien")
        print("instalada, y hara falta escanear los dos codigos QR.")
    else:
        print("Hecho. Al arrancar service.py hara falta escanear los dos codigos QR.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
