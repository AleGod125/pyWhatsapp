"""Cada WhatsApp tiene SU carpeta en Drive, y se llama como su dueno.

LO QUE EL USUARIO VE EN SU DRIVE
-------------------------------
::

    WhatsApp Backup/
      WhatsApp Dora Niebles/
        chats/<id>/messages
        chats/<id>/media
      WhatsApp Ale✨🏍️/
        chats/...

Antes ese nivel era ``accounts/<uuid>``: correcto --ya estaba separado por
cuenta-- pero ilegible. Abriendo Drive no habia forma de saber cual era cual,
y el usuario pidio poder distinguirlas y elegir cual mira desde el selector.

Y manana puede aparecer un tercer telefono. No hay nada fijo: una carpeta por
cuenta vinculada, con el nombre que tenga esa cuenta.

POR QUE LA CARPETA NO SE BUSCA POR SU NOMBRE
--------------------------------------------
Porque el nombre cambia y se repite:

* alguien se cambia el nombre de perfil en WhatsApp -> buscando por nombre no
  se encuentra su carpeta, se crea una SEGUNDA, y la copia queda partida en
  dos sitios sin que nada lo diga;
* dos personas se llaman igual -> la busqueda devuelve la carpeta de la otra,
  y una cuenta escribe dentro de la de otra.

Asi que la carpeta se marca con el identificador de la cuenta --que no cambia
nunca-- y el nombre pasa a ser solo la etiqueta que se lee.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from app.storage.drive.service import MARCA_DE_CUENTA, DriveBackupStorage


class _Base:
    def __init__(self, session):
        self._session = session

    def transaction(self):
        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


class DriveFalso:
    """Un Drive con memoria: recuerda nombres, padres y marcas."""

    def __init__(self):
        self.carpetas: dict[str, dict] = {}
        self.renombrados: list[tuple[str, str]] = []
        self._n = 0

    def _nueva(self, nombre, padre, marca):
        self._n += 1
        ident = f"id-{self._n}"
        self.carpetas[ident] = {
            "nombre": nombre,
            "padre": padre,
            "marca": marca or {},
        }
        return ident

    def buscar(self, nombre, *, padre=None):
        for ident, datos in self.carpetas.items():
            if datos["nombre"] == nombre and datos["padre"] == padre:
                return ident
        return None

    def buscar_por_marca(self, clave, valor, *, padre=None):
        for ident, datos in self.carpetas.items():
            if datos["marca"].get(clave) == valor and datos["padre"] == padre:
                return ident, datos["nombre"]
        return None

    def crear_carpeta(self, nombre, *, padre=None, marca=None):
        return self._nueva(nombre, padre, marca)

    def renombrar(self, file_id, nombre):
        self.renombrados.append((file_id, nombre))
        self.carpetas[file_id]["nombre"] = nombre

    def asegurar_carpeta(self, nombre, *, padre=None):
        return self.buscar(nombre, padre=padre) or self.crear_carpeta(
            nombre, padre=padre
        )

    def asegurar_carpeta_marcada(self, nombre, *, padre, clave, valor):
        encontrada = self.buscar_por_marca(clave, valor, padre=padre)
        if encontrada is None:
            return self.crear_carpeta(nombre, padre=padre, marca={clave: valor})
        ident, actual = encontrada
        if actual != nombre:
            self.renombrar(ident, nombre)
        return ident

    def nombre_de(self, ident):
        return self.carpetas[ident]["nombre"]


@pytest.fixture
def almacen(session, cuenta):
    drive = DriveFalso()
    return (
        DriveBackupStorage(
            user_id=cuenta.user_id, client=drive, database=_Base(session)
        ),
        drive,
    )


def _con_nombre(session, cuenta, nombre=None, telefono=None):
    if nombre is not None:
        cuenta.display_name = nombre
    if telefono is not None:
        cuenta.wa_pn = f"{telefono}@s.whatsapp.net"
        cuenta.phone_number = telefono
    session.flush()


# ---------------------------------------------------------------------------
# 1. La carpeta se llama como la persona
# ---------------------------------------------------------------------------


def test_la_carpeta_lleva_el_nombre_del_perfil(session, cuenta, almacen):
    """Es lo que el usuario pidio: abrir Drive y saber cual es cual."""
    caja, drive = almacen
    _con_nombre(session, cuenta, nombre="Dora Niebles")

    carpeta = caja.carpeta_de_mensajes(str(cuenta.id), "7")

    nombres = [d["nombre"] for d in drive.carpetas.values()]
    assert "WhatsApp Dora Niebles" in nombres
    assert carpeta


def test_los_emoji_del_nombre_sobreviven(session, cuenta, almacen):
    """"Ale✨🏍️" es el nombre real de una de las cuentas de esta instalacion."""
    caja, drive = almacen
    _con_nombre(session, cuenta, nombre="Ale✨🏍️")

    caja.ensure_account_storage(str(cuenta.id))

    assert "WhatsApp Ale✨🏍️" in [d["nombre"] for d in drive.carpetas.values()]


def test_sin_nombre_de_perfil_se_usa_el_numero(session, cuenta, almacen):
    """Recien vinculada, WhatsApp puede no haber dado todavia el nombre."""
    caja, _ = almacen
    _con_nombre(session, cuenta, nombre="", telefono="573002389304")

    assert caja.etiqueta_de_cuenta(str(cuenta.id)) == "WhatsApp +573002389304"


def test_sin_nada_se_usa_el_identificador(session, almacen):
    """Nunca una carpeta sin nombre: en Drive eso es inservible."""
    caja, _ = almacen
    huerfana = uuid.uuid4()

    etiqueta = caja.etiqueta_de_cuenta(str(huerfana))

    assert etiqueta == f"WhatsApp {str(huerfana)[:8]}"


@pytest.mark.parametrize(
    "sucio, esperado",
    [
        ("Dora/Niebles", "WhatsApp DoraNiebles"),
        ("Ale's phone", "WhatsApp Ales phone"),
        ("x" * 200, "WhatsApp " + "x" * 60),
    ],
)
def test_un_nombre_raro_no_rompe_la_ruta(session, cuenta, almacen, sucio, esperado):
    """Una barra partiria la ruta en Drive; una comilla, la busqueda."""
    caja, _ = almacen
    _con_nombre(session, cuenta, nombre=sucio)

    assert caja.etiqueta_de_cuenta(str(cuenta.id)) == esperado


def test_un_nombre_en_blanco_cae_al_identificador(session, cuenta, almacen):
    """Mejor ocho caracteres que identifican que un "sin nombre" repetido.

    Con varias cuentas sin nombre, todas se llamarian igual y no habria forma
    de distinguirlas en Drive.
    """
    caja, _ = almacen
    cuenta.display_name = "   "
    cuenta.wa_pn = None
    cuenta.phone_number = None
    session.flush()

    assert caja.etiqueta_de_cuenta(str(cuenta.id)) == f"WhatsApp {str(cuenta.id)[:8]}"


# ---------------------------------------------------------------------------
# 2. Una cuenta, UNA carpeta -- pase lo que pase con el nombre
# ---------------------------------------------------------------------------


def test_cambiar_de_nombre_RENOMBRA_no_duplica(session, cuenta, almacen):
    """Si esto fallara, la copia quedaria partida en dos carpetas."""
    caja, drive = almacen
    _con_nombre(session, cuenta, nombre="Dora")
    primera = caja.ensure_account_storage(str(cuenta.id))

    # La persona se cambia el nombre en WhatsApp y la cache se olvida.
    _con_nombre(session, cuenta, nombre="Dora Niebles")
    caja._olvidar_todo()
    segunda = caja.ensure_account_storage(str(cuenta.id))

    assert segunda == primera, "se creo una segunda carpeta para la misma cuenta"
    assert drive.nombre_de(primera) == "WhatsApp Dora Niebles"
    assert drive.renombrados, "no se renombro: la etiqueta se quedo vieja"


def test_dos_cuentas_con_EL_MISMO_nombre_no_comparten_carpeta(
    session, cuenta, almacen
):
    """Dos personas pueden llamarse igual. La marca las separa; el nombre no."""
    from app.models import WhatsAppAccount

    caja, drive = almacen
    _con_nombre(session, cuenta, nombre="Ale")

    otra_id = uuid.uuid4()
    session.add(
        WhatsAppAccount(
            id=otra_id,
            user_id=cuenta.user_id,
            session_status="linked",
            session_storage_key=f"accounts/{otra_id}",
            display_name="Ale",
        )
    )
    session.flush()

    una = caja.ensure_account_storage(str(cuenta.id))
    otra = caja.ensure_account_storage(str(otra_id))

    assert una != otra, "dos cuentas escribiendo en la misma carpeta de Drive"


def test_la_carpeta_queda_marcada_con_la_cuenta(session, cuenta, almacen):
    """La marca es lo que permite encontrarla despues de un renombrado."""
    caja, drive = almacen
    _con_nombre(session, cuenta, nombre="Dora Niebles")

    ident = caja.ensure_account_storage(str(cuenta.id))

    assert drive.carpetas[ident]["marca"] == {MARCA_DE_CUENTA: str(cuenta.id)}


# ---------------------------------------------------------------------------
# 3. Y por dentro cada cuenta sigue separada
# ---------------------------------------------------------------------------


def test_mensajes_y_multimedia_cuelgan_de_SU_cuenta(session, cuenta, almacen):
    caja, drive = almacen
    _con_nombre(session, cuenta, nombre="Dora Niebles")

    raiz = caja.ensure_account_storage(str(cuenta.id))
    mensajes = caja.carpeta_de_mensajes(str(cuenta.id), "7")
    media = caja.carpeta_de_multimedia(str(cuenta.id), "7")

    def sube_hasta_la_cuenta(ident):
        vistos = 0
        while ident is not None and vistos < 10:
            if ident == raiz:
                return True
            ident = drive.carpetas.get(ident, {}).get("padre")
            vistos += 1
        return False

    assert sube_hasta_la_cuenta(mensajes)
    assert sube_hasta_la_cuenta(media)
    assert mensajes != media


def test_el_chat_de_una_cuenta_no_cae_en_la_carpeta_de_otra(
    session, cuenta, almacen
):
    """Mismo numero de chat en dos cuentas: son carpetas distintas."""
    from app.models import WhatsAppAccount

    caja, _ = almacen
    otra_id = uuid.uuid4()
    session.add(
        WhatsAppAccount(
            id=otra_id,
            user_id=cuenta.user_id,
            session_status="linked",
            session_storage_key=f"accounts/{otra_id}",
            display_name="Ale",
        )
    )
    session.flush()

    una = caja.carpeta_de_mensajes(str(cuenta.id), "7")
    otra = caja.carpeta_de_mensajes(str(otra_id), "7")

    assert una != otra
