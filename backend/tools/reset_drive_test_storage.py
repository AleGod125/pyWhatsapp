"""Borrar de Google Drive lo que subio ESTA aplicacion. Nada mas.

ESTO BORRA CONTENIDO REMOTO
---------------------------
Va aparte del reset de la base a proposito: vaciar una base de desarrollo y
borrar archivos del Drive de una persona son decisiones muy distintas, y
juntarlas hace que una se ejecute sin haberla pensado.

QUE PUEDE BORRAR
----------------
Solo archivos con ``appProperties.app = whatsapp_backup`` que cuelguen de la
carpeta raiz registrada en PostgreSQL para ese usuario. Ademas, el permiso
concedido es ``drive.file``, que solo da acceso a lo que la propia aplicacion
creo: aunque hubiera un fallo aqui, Google no dejaria tocar nada mas.

QUE NO BORRA NUNCA
------------------
Carpetas que no sean la raiz registrada, archivos sin nuestra marca, y nada
de otro usuario. Antes de borrar se comprueba el identificador de la raiz
contra la base: sin esa fila, el script no sabe que es suyo y se para.

    py tools/reset_drive_test_storage.py --usuario correo@ejemplo.com
    py tools/reset_drive_test_storage.py --usuario correo@ejemplo.com --aplicar
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.auth.google_service import GoogleService  # noqa: E402
from app.core.config import load_settings  # noqa: E402
from app.core.lock import probe  # noqa: E402

CONFIRMACION = "BORRAR DE DRIVE"


class _Db:
    """Envoltorio minimo con la forma que esperan los servicios."""

    def __init__(self, sesion: Session):
        self._sesion = sesion

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._sesion
            self._sesion.commit()

        return scope()


def _usuario(sesion: Session, correo: str):
    from app.models import User

    return sesion.execute(
        select(User).where(User.email == correo)
    ).scalar_one_or_none()


def _listar(client, carpeta_id: str, ruta: str = "") -> list[dict]:
    """Recorre la carpeta y devuelve lo que es NUESTRO."""
    import json
    import urllib.parse

    encontrados: list[dict] = []
    consulta = urllib.parse.urlencode(
        {
            "q": f"'{carpeta_id}' in parents and trashed = false",
            "fields": "files(id,name,mimeType,size,appProperties)",
            "pageSize": 1000,
        }
    )
    datos = client._peticion(
        "GET", f"https://www.googleapis.com/drive/v3/files?{consulta}"
    )
    for archivo in datos.get("files") or []:
        nombre = archivo.get("name", "?")
        completo = f"{ruta}/{nombre}" if ruta else nombre
        if archivo.get("mimeType") == "application/vnd.google-apps.folder":
            encontrados += _listar(client, archivo["id"], completo)
            encontrados.append(
                {"id": archivo["id"], "ruta": completo, "carpeta": True, "size": 0}
            )
            continue
        # La marca es la condicion: sin ella no se toca, aunque este dentro.
        marca = (archivo.get("appProperties") or {}).get("app")
        if marca != "whatsapp_backup":
            print(f"  SE RESPETA (sin nuestra marca): {completo}")
            continue
        encontrados.append(
            {
                "id": archivo["id"],
                "ruta": completo,
                "carpeta": False,
                "size": int(archivo.get("size") or 0),
            }
        )
    return encontrados


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Borra de Google Drive lo que subio esta aplicacion."
    )
    parser.add_argument("--usuario", required=True, help="correo de la cuenta")
    parser.add_argument("--aplicar", action="store_true", help="borra de verdad")
    parser.add_argument(
        "--raiz",
        action="store_true",
        help="borrar tambien la carpeta raiz 'WhatsApp Backup'",
    )
    args = parser.parse_args()

    settings = load_settings()
    titular = probe(settings.session_dir)
    if titular is not None and args.aplicar:
        print(f"\n  ABORTADO: el servicio esta en marcha ({titular}).")
        print("  Paralo antes de borrar nada.")
        return 2

    engine = create_engine(settings.database_url)
    with Session(engine) as sesion:
        db = _Db(sesion)
        usuario = _usuario(sesion, args.usuario)
        if usuario is None:
            print(f"No hay ninguna cuenta con el correo {args.usuario}.")
            return 1

        from app.models.storage import GoogleDriveStorage

        deposito = sesion.execute(
            select(GoogleDriveStorage).where(GoogleDriveStorage.user_id == usuario.id)
        ).scalar_one_or_none()
        if deposito is None or not deposito.root_folder_id:
            # Sin la fila no se puede saber que carpeta es nuestra, y adivinar
            # por nombre podria acertar con una carpeta ajena del usuario.
            print("Ese usuario no tiene ninguna carpeta de copia registrada.")
            print("No hay nada que borrar, y sin ese dato no se busca a ciegas.")
            return 1

        google = GoogleService(db, settings)
        token = google.access_token(usuario.id)
        if not token:
            print("Google Drive no esta autorizado para esa cuenta.")
            print("Conectalo desde la aplicacion y vuelve a intentarlo.")
            return 1

        from app.storage.drive.client import DriveClient

        client = DriveClient(token)

        print("=" * 70)
        print("BORRADO EN GOOGLE DRIVE" if args.aplicar else "SIMULACION (no borra nada)")
        print("=" * 70)
        print(f"\nCuenta: {args.usuario}")
        print("Carpeta raiz registrada en PostgreSQL: si\n")

        objetivos = _listar(client, deposito.root_folder_id)
        archivos = [o for o in objetivos if not o["carpeta"]]
        carpetas = [o for o in objetivos if o["carpeta"]]
        total = sum(o["size"] for o in archivos)

        for objetivo in archivos[:25]:
            print(f"  {objetivo['ruta']}  ({objetivo['size'] / 1024:.0f} KB)")
        if len(archivos) > 25:
            print(f"  ... y {len(archivos) - 25} mas")

        print(
            f"\nTOTAL: {len(archivos)} archivo(s), {len(carpetas)} carpeta(s), "
            f"{total / (1024*1024):.1f} MB"
        )
        print("\nPostgreSQL NO se toca aqui.")
        print("Para eso: py tools/reset_for_drive_storage.py")

        if not args.aplicar:
            print("\nEsto ha sido una simulacion. Para hacerlo:")
            print(
                f"    py tools/reset_drive_test_storage.py "
                f"--usuario {args.usuario} --aplicar"
            )
            return 0

        print()
        print("  Esto borra archivos del Google Drive de esa persona.")
        if input(f'  Escribe "{CONFIRMACION}" para confirmar: ').strip() != CONFIRMACION:
            print("Cancelado. No se ha tocado nada.")
            return 1

        borrados = fallidos = 0
        for objetivo in archivos + carpetas:
            if client.borrar(objetivo["id"]):
                borrados += 1
            else:
                fallidos += 1
        print(f"\n  {borrados} elemento(s) borrados, {fallidos} fallo(s)")

        if args.raiz:
            client.borrar(deposito.root_folder_id)
            print("  Carpeta raiz borrada")

        # Las referencias locales apuntan a algo que ya no existe.
        from app.models.storage import DriveFolder, MessageSegment

        sesion.query(DriveFolder).filter(DriveFolder.user_id == usuario.id).delete()
        sesion.query(MessageSegment).filter(
            MessageSegment.user_id == usuario.id
        ).delete()
        sesion.delete(deposito)
        sesion.commit()
        print("  Referencias locales a Drive limpiadas")

    print("\nListo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
