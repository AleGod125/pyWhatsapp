"""Si el usuario borra la carpeta de la copia en su Drive, la copia se rehace.

EL FALLO, COMPROBADO CONTRA DRIVE
---------------------------------
En el log, cientos de lineas iguales y ni una subida avanzando::

    [DRIVE] Drive respondio 404 (notFound)
    [DRIVE] Drive respondio 404 (notFound)
    ...

Preguntandole a Drive por los identificadores guardados en PostgreSQL::

    WhatsApp Backup      -> 404 File not found: 1OLAdRBWW...
    accounts             -> 404 File not found: 1rkC874lB...
    accounts/<cuenta>    -> 404 File not found: 1P7VJzxRl...
    papelera             -> 0 bytes (borrado definitivo)

El usuario habia vaciado su Drive. ``drive_folders`` conservaba 1028 filas
describiendo un arbol que ya no existia, y nada las cuestionaba nunca: cada
subida pedia crear la carpeta del chat bajo un padre muerto, se llevaba un 404
y volvia a la cola a repetirlo.

LAS DOS COSAS QUE FALLABAN
--------------------------
1. La cache no se curaba. Es correcto no preguntarle a Drive por una carpeta
   que ya se conoce --seria una llamada de red por subida--, pero un 404
   diciendo justamente que no esta tiene que servir para algo.
2. El trabajador reintentaba lo no reintentable. Las dos ramas del ``except``
   llamaban a ``reintentar``; lo unico que cambiaba era escribir
   "[no reintentable]" delante del motivo.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.storage.drive.service import DriveBackupStorage
from app.storage.interface import PropiedadesDeArchivo, StorageError

NO_ESTA = StorageError("DRIVE_NOT_FOUND", "Ese archivo ya no esta en Drive.")

PROPIEDADES = PropiedadesDeArchivo(
    entity="message_segment", account_id="c", chat_id="1", segment_id="s", sequence=1
)


class DriveVaciado:
    """Un Drive donde las carpetas de antes ya no existen.

    Reproduce lo que hace Google: todo lo que cuelgue de un identificador que
    no esta responde 404, y lo que se cree de cero funciona con normalidad.
    """

    def __init__(self, muertas: set[str]):
        self.muertas = set(muertas)
        self.creadas: list[str] = []
        self.subidas: list[str] = []
        self._n = 0

    def _vivo(self, fid: str) -> None:
        if fid in self.muertas:
            raise NO_ESTA

    def buscar(self, nombre, *, padre=None):
        if padre:
            self._vivo(padre)
        return None  # nada preexistente: se crea todo

    def crear_carpeta(self, nombre, *, padre=None):
        if padre:
            self._vivo(padre)
        self._n += 1
        self.creadas.append(nombre)
        return f"nueva-{self._n}"

    def buscar_por_marca(self, clave, valor, *, padre=None):
        if padre:
            self._vivo(padre)
        return None  # nada preexistente: se crea todo

    def asegurar_carpeta_marcada(self, nombre, *, padre, clave, valor):
        """La carpeta de una cuenta se busca por MARCA, no por nombre.

        Se llama como la persona --"WhatsApp Dora Niebles"-- y ese nombre
        puede cambiar; la marca es el identificador de la cuenta y no cambia.
        """
        encontrada = self.buscar_por_marca(clave, valor, padre=padre)
        if encontrada is not None:
            return encontrada[0]
        return self.crear_carpeta(nombre, padre=padre)

    def asegurar_carpeta(self, nombre, *, padre=None):
        return self.buscar(nombre, padre=padre) or self.crear_carpeta(
            nombre, padre=padre
        )

    def subir_simple(self, *, nombre, padre, datos, propiedades, mime_type="x"):
        self._vivo(padre)
        self.subidas.append(padre)
        return {"id": f"archivo-{len(self.subidas)}", "size": str(len(datos))}


class _Base:
    """Reutiliza la sesion transaccional en vez de abrir otra."""

    def __init__(self, session):
        self._session = session

    def transaction(self):
        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


@pytest.fixture
def montaje(session, cuenta):
    """Un almacenamiento con la cache apuntando a carpetas que ya no estan."""
    from app.models.storage import DriveFolder, GoogleDriveStorage

    session.add(
        GoogleDriveStorage(user_id=cuenta.user_id, root_folder_id="raiz-muerta")
    )
    rutas = {
        "accounts": "acc-muerta",
        f"{cuenta.id}": "cta-muerta",
        f"{cuenta.id}/chats": "chats-muerta",
    }
    for ruta, fid in rutas.items():
        session.add(DriveFolder(user_id=cuenta.user_id, path=ruta, folder_id=fid))
    session.flush()

    drive = DriveVaciado({"raiz-muerta", *rutas.values()})
    almacen = DriveBackupStorage(
        user_id=cuenta.user_id, client=drive, database=_Base(session)
    )
    return almacen, drive, cuenta, session


# ---------------------------------------------------------------------------
# La cache se cura sola
# ---------------------------------------------------------------------------


def test_una_carpeta_nueva_se_crea_pese_a_la_cache_muerta(montaje):
    """El caso exacto del log: crear la carpeta de un chat que aun no tenia."""
    almacen, drive, cuenta, _ = montaje

    carpeta = almacen.carpeta_de_mensajes(str(cuenta.id), "499")

    assert carpeta.startswith("nueva-")
    assert "499" in drive.creadas


def test_la_raiz_tambien_se_rehace(montaje):
    """Si `accounts` no esta, su padre tampoco suele estar.

    Colgar el arbol nuevo de la raiz vieja haria que fallara exactamente igual.
    """
    almacen, drive, cuenta, _ = montaje

    almacen.carpeta_de_mensajes(str(cuenta.id), "499")

    assert "WhatsApp Backup" in drive.creadas


def test_la_cache_muerta_se_borra(montaje):
    """Dejar una sola fila vieja bastaria para volver al bucle."""
    from app.models.storage import DriveFolder

    almacen, _drive, cuenta, session = montaje
    almacen.carpeta_de_mensajes(str(cuenta.id), "499")

    vivas = (
        session.query(DriveFolder)
        .filter(DriveFolder.user_id == cuenta.user_id)
        .all()
    )
    assert vivas, "se rehizo la cache"
    assert not [f for f in vivas if f.folder_id.endswith("-muerta")]


def test_una_subida_a_carpeta_muerta_se_recupera(montaje):
    """La cache acierta, asi que el 404 solo aparece AL SUBIR.

    `_asegurar_ruta` no llega a preguntarle nada a Drive cuando la ruta ya
    esta guardada: es aqui donde se descubre que la carpeta no existe.
    """
    from app.models.storage import DriveFolder

    almacen, drive, cuenta, session = montaje
    ruta = f"{cuenta.id}/chats/7/messages"
    session.add(
        DriveFolder(user_id=cuenta.user_id, path=ruta, folder_id="hoja-muerta")
    )
    session.flush()
    drive.muertas.add("hoja-muerta")

    carpeta = almacen.carpeta_de_mensajes(str(cuenta.id), "7")
    subido = almacen.store_bytes(
        carpeta=carpeta,
        nombre="0001.jsonl.gz",
        datos=b"contenido",
        propiedades=PROPIEDADES,
    )

    assert subido.file_id
    assert drive.subidas and drive.subidas[-1] not in drive.muertas


def test_un_error_que_NO_es_404_no_tira_la_cache(montaje):
    """Solo el "no existe" justifica rehacer. Un corte de red no."""
    from app.models.storage import DriveFolder

    almacen, drive, cuenta, session = montaje
    antes = {f.folder_id for f in session.query(DriveFolder).all()}

    def revienta(nombre, *, padre=None):
        raise StorageError("DRIVE_UNREACHABLE", "sin red", reintentable=True)

    drive.asegurar_carpeta = revienta

    with pytest.raises(StorageError) as fallo:
        almacen.carpeta_de_mensajes(str(cuenta.id), "499")

    assert fallo.value.code == "DRIVE_UNREACHABLE"
    assert {f.folder_id for f in session.query(DriveFolder).all()} == antes


def test_un_drive_sano_no_paga_llamadas_de_mas(session, cuenta):
    """La cura no puede costarle nada al caso normal, que es el de siempre."""
    from app.models.storage import DriveFolder, GoogleDriveStorage

    session.add(GoogleDriveStorage(user_id=cuenta.user_id, root_folder_id="raiz"))
    ruta = f"{cuenta.id}/chats/7/messages"
    session.add(DriveFolder(user_id=cuenta.user_id, path=ruta, folder_id="viva"))
    session.flush()

    drive = DriveVaciado(set())
    almacen = DriveBackupStorage(
        user_id=cuenta.user_id, client=drive, database=_Base(session)
    )

    assert almacen.carpeta_de_mensajes(str(cuenta.id), "7") == "viva"
    assert drive.creadas == [], "pregunto a Drive por algo que ya sabia"
