"""Cada cuenta es de quien dicen SUS credenciales, no de quien se le diga.

LO QUE PASO, MEDIDO
-------------------
El usuario vinculo dos telefonos y vio una sola cuenta, con conversaciones que
no reconocia como separadas. La base decia esto::

    fila 91a8470b   phone_number = 573002389304    session_status = linked
    fila cad145b1   phone_number = (ninguno)       session_status = never_linked

Y el ``creds.json`` de esas mismas dos cuentas decia esto otro::

    91a8470b/baileys/creds.json   me.id = 573008927374   me.name = "Dora Niebles"
    cad145b1/baileys/creds.json   me.id = 573002389304   me.name = "Ale"

O sea: la cuenta que el selector enseñaba como "+573002389304" era el telefono
de Dora con la etiqueta de Ale, y el telefono de Ale --vinculado de verdad,
con credenciales completas-- estaba en una fila `never_linked`, invisible.

No habia ninguna conversacion de una persona dentro de la cuenta de otra: cada
fila tenia lo suyo. Lo que estaba mal era QUE FILA se enseñaba y CON QUE
NOMBRE. Desde fuera eso se ve exactamente igual que "los chats estan
mezclados", y por eso costo encontrarlo.

Encima, con la etiqueta cambiada, el mismo telefono de Dora acabo vinculado en
DOS filas (10:11 y 10:47): 132 conversaciones guardadas dos veces, con los
mismos identificadores de mensaje --654 de 654 en la mas grande.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.auth.atribucion import (
    identidad_en_disco,
    reconciliar_con_las_credenciales,
    ya_vinculado_en_otra_cuenta,
)
from app.core.session_paths import carpeta_de_cuenta


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def _escribir_creds(ajustes, account_id, *, telefono, nombre=None, lid=None):
    """Deja un `creds.json` como el que escribe Baileys al vincular."""
    destino = carpeta_de_cuenta(ajustes, account_id) / "baileys"
    destino.mkdir(parents=True, exist_ok=True)
    yo = {"id": f"{telefono}:17@s.whatsapp.net"}
    if nombre:
        yo["name"] = nombre
    if lid:
        yo["lid"] = lid
    (destino / "creds.json").write_text(
        json.dumps({"me": yo, "registered": True}), encoding="utf-8"
    )
    return destino / "creds.json"


@pytest.fixture
def ajustes(settings, tmp_path):
    """Unos ajustes con carpeta de sesion SOLO para esta prueba.

    El fixture global apunta a la carpeta de verdad --``backend/session``--
    porque casi ninguna prueba escribe ahi. Estas si: crean `creds.json` de
    cuentas inventadas. Escribiendolos en la carpeta real quedaron quince
    carpetas de cuentas que no existen dentro de la sesion viva del usuario, y
    ademas las pruebas leian las credenciales REALES de quien estuviera
    vinculado en ese momento -- asi que fallaban o pasaban segun si habia un
    telefono conectado en ese instante.

    No se puede llamar `settings` y sombrear al global: `database` es de
    sesion y lo necesita, y pytest rechaza que una fixture de sesion dependa
    de una de funcion.
    """
    import dataclasses

    return dataclasses.replace(settings, session_dir=tmp_path / "session")


@pytest.fixture
def cuenta_en_disco(ajustes, session, cuenta):
    """La cuenta del fixture, con credenciales escritas a medida."""

    def poner(*, telefono, nombre=None, lid=None):
        return _escribir_creds(
            ajustes, cuenta.id, telefono=telefono, nombre=nombre, lid=lid
        )

    return poner


class _Base:
    """Reutiliza la sesion transaccional de la prueba."""

    def __init__(self, session):
        self._session = session

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


# ---------------------------------------------------------------------------
# 1. Leer la verdad del disco
# ---------------------------------------------------------------------------


def test_se_lee_el_telefono_real_de_las_credenciales(ajustes, cuenta, cuenta_en_disco):
    cuenta_en_disco(telefono="573008927374", nombre="Dora Niebles", lid="855390@lid")

    real = identidad_en_disco(ajustes, cuenta.id)

    assert real is not None
    assert real.telefono == "573008927374"
    assert real.nombre == "Dora Niebles"
    assert real.lid == "855390@lid"


def test_sin_credenciales_no_se_inventa_nada(ajustes):
    """Una cuenta recien creada que aun no vinculo es un estado legitimo."""
    assert identidad_en_disco(ajustes, uuid.uuid4()) is None


def test_unas_credenciales_a_medias_tampoco_cuentan(ajustes, cuenta):
    """Hay fichero pero la vinculacion no llego a completarse."""
    destino = carpeta_de_cuenta(ajustes, cuenta.id) / "baileys"
    destino.mkdir(parents=True, exist_ok=True)
    (destino / "creds.json").write_text(json.dumps({"me": {}}), encoding="utf-8")

    assert identidad_en_disco(ajustes, cuenta.id) is None


def test_un_fichero_roto_no_tumba_nada(ajustes, cuenta):
    destino = carpeta_de_cuenta(ajustes, cuenta.id) / "baileys"
    destino.mkdir(parents=True, exist_ok=True)
    (destino / "creds.json").write_text("{esto no es json", encoding="utf-8")

    assert identidad_en_disco(ajustes, cuenta.id) is None


def test_el_nombre_con_emoji_se_conserva(ajustes, cuenta, cuenta_en_disco):
    """"Ale✨🏍️" es un nombre real de este caso, y tiene que sobrevivir."""
    cuenta_en_disco(telefono="573002389304", nombre="Ale✨🏍️")

    real = identidad_en_disco(ajustes, cuenta.id)

    assert real.nombre == "Ale✨🏍️"


# ---------------------------------------------------------------------------
# 2. La base se corrige desde el disco
# ---------------------------------------------------------------------------


def test_una_fila_con_el_numero_de_OTRO_se_corrige(
    ajustes, session, cuenta, cuenta_en_disco
):
    """El fallo exacto: la fila decia 573002389304 y era el telefono de Dora."""
    cuenta.phone_number = "573002389304"
    cuenta.wa_pn = "573002389304@s.whatsapp.net"
    session.flush()
    cuenta_en_disco(telefono="573008927374", nombre="Dora Niebles")

    informe = reconciliar_con_las_credenciales(_Base(session), ajustes)

    session.expire_all()
    assert cuenta.phone_number == "573008927374", (
        "la fila sigue con el numero de otro telefono"
    )
    assert cuenta.wa_pn.startswith("573008927374")
    assert str(cuenta.id)[:8] in informe.corregidas


def test_una_cuenta_vinculada_marcada_como_que_no_se_activa(
    ajustes, session, cuenta, cuenta_en_disco
):
    """Es lo que dejaba el telefono de Ale fuera del selector.

    `levantar_las_vinculadas` elige por `session_status`. Con `never_linked`
    la cuenta no se levanta, no aparece, y el usuario ve una sola teniendo dos.
    """
    cuenta.session_status = "never_linked"
    session.flush()
    cuenta_en_disco(telefono="573002389304", nombre="Ale✨🏍️")

    informe = reconciliar_con_las_credenciales(_Base(session), ajustes)

    session.expire_all()
    assert cuenta.session_status == "linked"
    assert cuenta.display_name == "Ale✨🏍️"
    assert str(cuenta.id)[:8] in informe.activadas


def test_una_revocada_NO_se_resucita(ajustes, session, cuenta, cuenta_en_disco):
    """Ahi WhatsApp dijo expresamente que no. Reactivarla seria ignorarlo."""
    cuenta.session_status = "revoked"
    session.flush()
    cuenta_en_disco(telefono="573008927374")

    reconciliar_con_las_credenciales(_Base(session), ajustes)

    session.expire_all()
    assert cuenta.session_status == "revoked"


def test_el_nombre_que_puso_el_usuario_manda(
    ajustes, session, cuenta, cuenta_en_disco
):
    """Lo que alguien escribio a mano no lo pisa el nombre del telefono."""
    cuenta.display_name = "El movil de la abuela"
    session.flush()
    cuenta_en_disco(telefono="573008927374", nombre="Dora Niebles")

    reconciliar_con_las_credenciales(_Base(session), ajustes)

    session.expire_all()
    assert cuenta.display_name == "El movil de la abuela"


def test_una_cuenta_sin_credenciales_se_deja_en_paz(ajustes, session, cuenta):
    """Creada para vincular y aun sin escanear: no hay nada que corregir."""
    cuenta.session_status = "never_linked"
    session.flush()

    informe = reconciliar_con_las_credenciales(_Base(session), ajustes)

    session.expire_all()
    assert cuenta.session_status == "never_linked"
    assert not informe.hubo_cambios


def test_pasar_dos_veces_no_cambia_nada_la_segunda(
    ajustes, session, cuenta, cuenta_en_disco
):
    """Corre en cada arranque: no puede estar reescribiendo siempre."""
    cuenta_en_disco(telefono="573008927374", nombre="Dora Niebles")

    primera = reconciliar_con_las_credenciales(_Base(session), ajustes)
    segunda = reconciliar_con_las_credenciales(_Base(session), ajustes)

    assert primera.hubo_cambios
    assert not segunda.hubo_cambios


# ---------------------------------------------------------------------------
# 3. El mismo telefono dos veces se detecta
# ---------------------------------------------------------------------------


def _otra_cuenta(session, user_id):
    from app.models import WhatsAppAccount

    ident = uuid.uuid4()
    fila = WhatsAppAccount(
        id=ident,
        user_id=user_id,
        session_status="linked",
        session_storage_key=f"accounts/{ident}",
    )
    session.add(fila)
    session.flush()
    return fila


def test_el_mismo_telefono_en_dos_filas_se_denuncia(
    ajustes, session, cuenta, cuenta_en_disco
):
    """Vincular dos veces no da dos copias: da la misma copia duplicada.

    Paso de verdad -- 132 conversaciones guardadas dos veces, con los mismos
    identificadores de mensaje.

    EL HUECO QUE LA RESTRICCION NO CUBRE
    ------------------------------------
    `uq_whatsapp_accounts_user_pn` impide dos filas con el mismo `wa_pn`, y
    con eso el caso normal ya no puede darse. Pero `wa_pn` puede ser NULL --y
    PostgreSQL trata los NULL como distintos-- asi que DOS cuentas sin sellar
    con las credenciales del MISMO telefono en disco pasan la restriccion sin
    despeinarse.

    Es un estado alcanzable: dos veces "Agregar cuenta" y el mismo movil
    escaneado en las dos. La restriccion no lo ve; esto si.
    """
    cuenta.wa_pn = None
    session.flush()
    cuenta_en_disco(telefono="573008927374", nombre="Dora Niebles")

    gemela = _otra_cuenta(session, cuenta.user_id)
    gemela.wa_pn = None
    session.flush()
    _escribir_creds(ajustes, gemela.id, telefono="573008927374", nombre="Dora")

    informe = reconciliar_con_las_credenciales(_Base(session), ajustes)

    assert "573008927374" in informe.duplicadas, (
        "dos cuentas sin sellar con el mismo telefono pasaron sin denuncia"
    )
    assert len(informe.duplicadas["573008927374"]) == 2


def test_dos_telefonos_distintos_no_son_duplicado(
    ajustes, session, cuenta, cuenta_en_disco
):
    """El caso normal, y el que tiene que seguir funcionando."""
    cuenta_en_disco(telefono="573008927374", nombre="Dora Niebles")
    otra = _otra_cuenta(session, cuenta.user_id)
    _escribir_creds(ajustes, otra.id, telefono="573002389304", nombre="Ale✨🏍️")

    informe = reconciliar_con_las_credenciales(_Base(session), ajustes)

    assert informe.duplicadas == {}


def test_se_puede_preguntar_antes_de_sellar(ajustes, session, cuenta, cuenta_en_disco):
    """Para rechazar la segunda vinculacion en vez de duplicar."""
    cuenta_en_disco(telefono="573008927374")
    nueva = _otra_cuenta(session, cuenta.user_id)

    choca = ya_vinculado_en_otra_cuenta(
        _Base(session),
        ajustes,
        user_id=cuenta.user_id,
        telefono="573008927374",
        excepto=nueva.id,
    )

    assert choca == str(cuenta.id)[:8]


def test_un_telefono_nuevo_no_choca_con_nada(
    ajustes, session, cuenta, cuenta_en_disco
):
    cuenta_en_disco(telefono="573008927374")
    nueva = _otra_cuenta(session, cuenta.user_id)

    assert (
        ya_vinculado_en_otra_cuenta(
            _Base(session),
            ajustes,
            user_id=cuenta.user_id,
            telefono="573002389304",
            excepto=nueva.id,
        )
        is None
    )


# ---------------------------------------------------------------------------
# 4. La carpeta BASE, que no lleva el identificador en la ruta
# ---------------------------------------------------------------------------
#
# Hay dos sitios donde Baileys deja las credenciales:
#
#   session/accounts/<id>/baileys/creds.json   los runtimes por cuenta
#   session/baileys/creds.json                 el runtime BASE
#
# El base arranca con el servicio, antes de que exista ninguna cuenta, y sigue
# escribiendo en la carpeta plana aunque la fila diga otra cosa. Se comprobo
# tras vincular un telefono de cero::
#
#     session_storage_key = 'accounts/36e15dde-...'   (lo que dice la base)
#     creds.json real     =  session/baileys/         (donde esta de verdad)
#
# Mirando solo la carpeta por cuenta, la PRIMERA cuenta --un solo telefono, el
# caso mas normal-- se quedaba sin verificar por nadie. Justo la que se
# etiqueto mal.


def _creds_en_la_base(ajustes, *, telefono, nombre=None):
    from pathlib import Path

    destino = Path(ajustes.session_dir) / "baileys"
    destino.mkdir(parents=True, exist_ok=True)
    yo = {"id": f"{telefono}:17@s.whatsapp.net"}
    if nombre:
        yo["name"] = nombre
    (destino / "creds.json").write_text(
        json.dumps({"me": yo, "registered": True}), encoding="utf-8"
    )


def test_la_cuenta_del_runtime_base_tambien_se_verifica(ajustes, session, cuenta):
    """Sin esto, la primera cuenta vinculada no la comprueba nadie."""
    cuenta.session_status = "never_linked"
    session.flush()
    _creds_en_la_base(ajustes, telefono="573008927374", nombre="Dora Niebles")

    reconciliar_con_las_credenciales(_Base(session), ajustes)

    session.expire_all()
    assert cuenta.phone_number == "573008927374"
    assert cuenta.display_name == "Dora Niebles"
    assert cuenta.session_status == "linked"


def test_con_DOS_candidatas_no_se_atribuye_a_ninguna(ajustes, session, cuenta):
    """Adivinar es exactamente como se etiqueto mal una cuenta.

    Si dos filas no tienen credenciales propias, cualquiera podria ser la
    duena de la carpeta base. Antes que sellar la que no es, no se sella
    ninguna: eso se ve y se corrige; lo otro no.
    """
    otra = _otra_cuenta(session, cuenta.user_id)
    otra.phone_number = None
    session.flush()
    _creds_en_la_base(ajustes, telefono="573008927374", nombre="Dora Niebles")

    reconciliar_con_las_credenciales(_Base(session), ajustes)

    session.expire_all()
    assert cuenta.phone_number is None
    assert otra.phone_number is None


def test_la_carpeta_base_no_pisa_a_quien_tiene_las_suyas(
    ajustes, session, cuenta, cuenta_en_disco
):
    """Su propio fichero manda sobre el de la carpeta comun."""
    cuenta_en_disco(telefono="573002389304", nombre="Ale✨🏍️")
    _creds_en_la_base(ajustes, telefono="573008927374", nombre="Dora Niebles")

    reconciliar_con_las_credenciales(_Base(session), ajustes)

    session.expire_all()
    assert cuenta.phone_number == "573002389304"
    assert cuenta.display_name == "Ale✨🏍️"


# ---------------------------------------------------------------------------
# 5. Las huerfanas se barren, y no por estetica
# ---------------------------------------------------------------------------
#
# `identidad_de_la_cuenta` atribuye la carpeta del runtime base SOLO si hay una
# unica candidata sin credenciales propias. Cada "Agregar cuenta" abandonado
# deja una fila mas; con seis, se niega a adivinar y devuelve None. Y entonces
# la cuenta se sella sin identidad. Se midio:
#
#     2c9ef826  linked  207 chats  ACTIVA
#               tel=NULL  nombre=NULL  wa_pn=NULL
#
# En el selector eso se lee "Cuenta sin vincular" mientras extrae chats.


def test_las_huerfanas_se_borran(ajustes, session, cuenta):
    """Filas sin numero, sin fecha, sin credenciales y sin un solo chat."""
    from app.auth.atribucion import barrer_cuentas_huerfanas

    cuenta.wa_pn = "573008927374@s.whatsapp.net"
    session.flush()
    for _ in range(3):
        _otra_cuenta(session, cuenta.user_id)

    borradas = barrer_cuentas_huerfanas(
        _Base(session), ajustes, user_id=cuenta.user_id
    )

    assert len(borradas) == 2, "se respeta la ultima; las demas se van"


def test_la_MAS_RECIENTE_se_respeta(ajustes, session, cuenta):
    """Puede ser la que se esta escaneando ahora mismo."""
    from app.auth.atribucion import barrer_cuentas_huerfanas
    from app.models import WhatsAppAccount

    cuenta.wa_pn = "573008927374@s.whatsapp.net"
    session.flush()
    primera = _otra_cuenta(session, cuenta.user_id)
    ultima = _otra_cuenta(session, cuenta.user_id)

    barrer_cuentas_huerfanas(_Base(session), ajustes, user_id=cuenta.user_id)

    session.expire_all()
    assert session.get(WhatsAppAccount, ultima.id) is not None
    assert session.get(WhatsAppAccount, primera.id) is None


def test_una_huerfana_CON_CHATS_no_se_toca(ajustes, session, cuenta):
    """Ahi hay contenido de alguien, aunque la fila parezca a medias."""
    from app.models import Chat, WhatsAppAccount

    from app.auth.atribucion import barrer_cuentas_huerfanas

    cuenta.wa_pn = "573008927374@s.whatsapp.net"
    session.flush()
    con_algo = _otra_cuenta(session, cuenta.user_id)
    session.add(
        Chat(
            jid="573001112233@s.whatsapp.net",
            chat_type="individual",
            whatsapp_account_id=con_algo.id,
        )
    )
    _otra_cuenta(session, cuenta.user_id)
    _otra_cuenta(session, cuenta.user_id)
    session.flush()

    barrer_cuentas_huerfanas(_Base(session), ajustes, user_id=cuenta.user_id)

    session.expire_all()
    assert session.get(WhatsAppAccount, con_algo.id) is not None


def test_una_huerfana_CON_CREDENCIALES_no_se_toca(ajustes, session, cuenta):
    """Esa esta vinculada aunque la base no lo sepa todavia."""
    from app.auth.atribucion import barrer_cuentas_huerfanas
    from app.models import WhatsAppAccount

    cuenta.wa_pn = "573008927374@s.whatsapp.net"
    session.flush()
    con_creds = _otra_cuenta(session, cuenta.user_id)
    _escribir_creds(ajustes, con_creds.id, telefono="573002389304")
    _otra_cuenta(session, cuenta.user_id)
    _otra_cuenta(session, cuenta.user_id)

    barrer_cuentas_huerfanas(_Base(session), ajustes, user_id=cuenta.user_id)

    session.expire_all()
    assert session.get(WhatsAppAccount, con_creds.id) is not None


def test_con_UNA_sola_cuenta_no_se_borra_nada(ajustes, session, cuenta):
    """Nunca dejar a alguien sin ninguna fila."""
    from app.auth.atribucion import barrer_cuentas_huerfanas
    from app.models import WhatsAppAccount

    cuenta.wa_pn = None
    cuenta.linked_at = None
    session.flush()

    borradas = barrer_cuentas_huerfanas(
        _Base(session), ajustes, user_id=cuenta.user_id
    )

    session.expire_all()
    assert borradas == []
    assert session.get(WhatsAppAccount, cuenta.id) is not None


def test_barrer_DESATASCA_la_atribucion(ajustes, session, cuenta):
    """La razon de fondo, y la que no es evidente.

    Con varias candidatas sin credenciales propias, la carpeta del runtime
    base no se puede atribuir --no se sabe de cual es-- y la cuenta acaba
    sellada sin nombre. Con una sola, se atribuye.
    """
    from app.auth.atribucion import identidad_de_la_cuenta
    from app.models import WhatsAppAccount

    _creds_en_la_base(ajustes, telefono="573008927374", nombre="Dora Niebles")
    cuenta.wa_pn = None
    cuenta.linked_at = None
    session.flush()

    # Con mas de una candidata NO se atribuye: adivinar es como se etiqueto
    # mal una cuenta la primera vez.
    estorbo = _otra_cuenta(session, cuenta.user_id)
    assert identidad_de_la_cuenta(session, ajustes, cuenta.id) is None

    # Quitada la de mas, vuelve a ser inequivoco.
    session.delete(estorbo)
    session.flush()
    session.expire_all()

    real = identidad_de_la_cuenta(session, ajustes, cuenta.id)
    assert real is not None, "sigue sin poder atribuirse la carpeta base"
    assert real.telefono == "573008927374"
