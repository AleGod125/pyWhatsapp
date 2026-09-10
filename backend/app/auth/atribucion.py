"""De quien es cada cuenta, segun SUS PROPIAS credenciales.

EL FALLO QUE ESTO CIERRA
------------------------
Una fila de ``whatsapp_accounts`` decia una cosa y el ``creds.json`` de esa
misma cuenta decia otra::

    fila 91a8470b   phone_number = 573002389304        (lo que dice la base)
    creds.json      me.id        = 573008927374        (el telefono de verdad)
                    me.name      = "Dora Niebles"

    fila cad145b1   phone_number = (ninguno)           never_linked
    creds.json      me.id        = 573002389304        (el telefono de verdad)
                    me.name      = "Ale"

O sea: la cuenta que el usuario veia en el selector como "+573002389304" era
en realidad el telefono de Dora con la etiqueta cambiada, y el telefono de Ale
--vinculado de verdad, con sus credenciales completas-- estaba en una fila
marcada ``never_linked``, invisible.

Lo que se ve desde fuera es "los chats estan mezclados". No lo estaban: cada
fila tenia lo suyo. Lo que estaba mal era QUE FILA se enseñaba y CON QUE
NOMBRE. Y encima el mismo telefono acabo vinculado en dos filas distintas
--10:11 y 10:47-- duplicando 132 conversaciones con identificadores de mensaje
identicos.

POR QUE MANDAN LAS CREDENCIALES
-------------------------------
``creds.json`` lo escribe Baileys con lo que contesta WhatsApp al vincular.
No es una suposicion nuestra: es el unico sitio del sistema que sabe, sin
lugar a dudas, que telefono hay al otro lado de esa sesion.

El numero llegaba por parametro desde el flujo de vinculacion, y ese camino
tiene un punto donde equivocarse (``marcar_vinculada`` sin ``account_id``,
varias cuentas, sellar la que no es). Leyendo el fichero de la propia cuenta
no hay nada que adivinar.

LO QUE ESTE MODULO NO HACE
--------------------------
No borra nada ni fusiona filas. Corrige etiquetas y AVISA de los duplicados;
decidir que se hace con dos filas del mismo telefono no es cosa suya.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.logging_setup import get_logger
from app.core.session_paths import carpeta_de_cuenta
from app.models import WhatsAppAccount

log = get_logger("AUTH")


@dataclass(frozen=True)
class IdentidadReal:
    """Lo que dicen las credenciales de una cuenta. Nada inferido."""

    #: JID completo, tal cual: ``573008927374:17@s.whatsapp.net``.
    pn: str
    #: El LID, si WhatsApp lo dio.
    lid: str | None
    #: El nombre de perfil. Puede llevar emoji.
    nombre: str | None

    @property
    def telefono(self) -> str:
        """Solo los digitos: ``573008927374``."""
        return self.pn.split("@")[0].split(":")[0]


def _ruta_de_credenciales(settings: Any, account_id: Any) -> Path:
    return carpeta_de_cuenta(settings, account_id) / "baileys" / "creds.json"


def _ruta_de_la_carpeta_base(settings: Any) -> Path:
    """Donde escribe el runtime BASE, que no usa carpeta por cuenta.

    Hay dos sitios y hay que mirar los dos:

    * ``session/accounts/<id>/baileys/creds.json`` -- los runtimes por cuenta;
    * ``session/baileys/creds.json``               -- el runtime base.

    El base es el que arranca con el servicio, antes de que exista ninguna
    cuenta, y sigue escribiendo en la carpeta plana aunque la base de datos
    apunte a ``accounts/<id>``. Se comprobo: la fila decia
    ``session_storage_key = 'accounts/36e15dde-...'`` y el ``creds.json`` real
    estaba en ``session/baileys/``.

    Mirando solo la carpeta por cuenta, la PRIMERA cuenta --la mas normal, un
    solo telefono-- se quedaba sin verificar. Justo la que se etiqueto mal.
    """
    return Path(settings.session_dir) / "baileys" / "creds.json"


def _leer(ruta: Path) -> IdentidadReal | None:
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("Credenciales ilegibles en %s", ruta)
        return None

    yo = datos.get("me") or {}
    pn = (yo.get("id") or "").strip()
    if not pn:
        # Hay fichero pero la vinculacion no llego a completarse.
        return None
    return IdentidadReal(
        pn=pn,
        lid=(yo.get("lid") or "").strip() or None,
        nombre=(yo.get("name") or "").strip() or None,
    )


def identidad_en_disco(settings: Any, account_id: Any) -> IdentidadReal | None:
    """Quien es esta cuenta segun su propio ``creds.json``.

    SOLO la carpeta por cuenta. Para incluir la del runtime base --que no
    lleva el identificador en la ruta-- usa :func:`identidad_de_la_cuenta`,
    que necesita mirar las demas cuentas para no atribuirla a ciegas.

    ``None`` si no hay credenciales o si no llevan identidad -- que es el caso
    normal de una cuenta recien creada que todavia no ha vinculado nada.
    """
    return _leer(_ruta_de_credenciales(settings, account_id))


def identidad_de_la_cuenta(
    sesion: Any, settings: Any, account_id: Any
) -> IdentidadReal | None:
    """Quien es esta cuenta, mirando TAMBIEN la carpeta del runtime base.

    La carpeta base (``session/baileys/``) no lleva el identificador en la
    ruta, asi que solo se le atribuye a una cuenta cuando no hay duda: si mas
    de una se ha quedado sin credenciales propias, cualquiera podria ser la
    suya. Antes que adivinar, ``None`` -- eso deja la cuenta sin sellar, que
    se ve y se corrige; sellar la equivocada no se ve.
    """
    propia = identidad_en_disco(settings, account_id)
    if propia is not None:
        return propia

    de_la_base = _leer(_ruta_de_la_carpeta_base(settings))
    if de_la_base is None:
        return None

    fila = sesion.get(WhatsAppAccount, account_id)
    if fila is None:
        return None
    hermanas = sesion.execute(
        select(WhatsAppAccount.id).where(WhatsAppAccount.user_id == fila.user_id)
    ).scalars().all()
    sin_propias = [
        i for i in hermanas if identidad_en_disco(settings, i) is None
    ]
    if len(sin_propias) == 1 and str(sin_propias[0]) == str(account_id):
        return de_la_base
    return None


@dataclass
class Reparacion:
    """Que se corrigio y que quedo mal. Para el log y para las pruebas."""

    corregidas: list[str]
    activadas: list[str]
    duplicadas: dict[str, list[str]]

    @property
    def hubo_cambios(self) -> bool:
        return bool(self.corregidas or self.activadas)


def reconciliar_con_las_credenciales(
    database: Any, settings: Any, *, user_id: Any = None
) -> Reparacion:
    """Hace que la base diga lo que dicen las credenciales de cada cuenta.

    Para cada fila con credenciales completas:

    * se le pone SU numero, SU LID y SU nombre real;
    * si estaba ``never_linked`` teniendo credenciales, pasa a ``linked`` --
      estaba vinculada de verdad y no se habia anotado, que es justo lo que
      la dejaba fuera del selector.

    Y se avisa si dos filas resultan ser el MISMO telefono: eso duplica cada
    conversacion y no se arregla renombrando.

    No toca las filas sin credenciales: una cuenta recien creada que aun no ha
    vinculado nada es un estado legitimo.
    """
    corregidas: list[str] = []
    activadas: list[str] = []
    por_telefono: dict[str, list[str]] = {}

    with database.transaction() as sesion:
        consulta = select(WhatsAppAccount)
        if user_id is not None:
            consulta = consulta.where(WhatsAppAccount.user_id == user_id)
        filas = sesion.execute(consulta).scalars().all()

        # LA CARPETA BASE, que no lleva el identificador en la ruta.
        #
        # El runtime base escribe en `session/baileys/` y no en
        # `session/accounts/<id>/baileys/`, asi que su cuenta no se puede
        # localizar por la ruta. Se le atribuye SOLO si no hay duda: si mas de
        # una cuenta se queda sin credenciales propias, cualquiera podria ser
        # la suya y adivinar es como se etiqueto mal la primera vez.
        de_la_base = _leer(_ruta_de_la_carpeta_base(settings))
        sin_propias = [f for f in filas if identidad_en_disco(settings, f.id) is None]
        duena_de_la_base = None
        if de_la_base is not None:
            if len(sin_propias) == 1:
                duena_de_la_base = str(sin_propias[0].id)
            elif len(sin_propias) > 1:
                log.warning(
                    "[AUTH] Hay credenciales en la carpeta base (%s) y %d "
                    "cuentas sin credenciales propias: no se atribuyen a "
                    "ninguna. Adivinar es como se etiqueto mal una cuenta.",
                    de_la_base.telefono,
                    len(sin_propias),
                )

        # PRIMERA PASADA: de quien es cada fila. Sin escribir nada.
        #
        # Hace falta saberlo ANTES de tocar la base porque
        # `uq_whatsapp_accounts_user_pn` impide dos filas con el mismo `wa_pn`:
        # si se sella la segunda, la escritura revienta y se pierde tambien la
        # correccion de todas las demas. Se detecta el duplicado y se informa,
        # que es lo unico sensato -- elegir cual sobra no lo decide esto.
        identidades: dict[str, IdentidadReal] = {}
        for fila in filas:
            identidad = identidad_en_disco(settings, fila.id)
            if identidad is None and str(fila.id) == duena_de_la_base:
                identidad = de_la_base
            if identidad is None:
                continue
            identidades[str(fila.id)] = identidad
            por_telefono.setdefault(identidad.telefono, []).append(str(fila.id)[:8])

        repetidos = {t for t, c in por_telefono.items() if len(c) > 1}

        # SEGUNDA PASADA: corregir, saltando lo que no se puede.
        for fila in filas:
            identidad = identidades.get(str(fila.id))
            if identidad is None:
                continue

            corto = str(fila.id)[:8]
            if identidad.telefono in repetidos:
                # Sellarla chocaria con la unicidad y tumbaria la pasada
                # entera. Ya esta denunciado mas abajo.
                log.warning(
                    "[AUTH] La cuenta %s no se reetiqueta: su telefono (%s) "
                    "esta en mas de una cuenta.",
                    corto,
                    identidad.telefono,
                )
                continue

            cambio = False
            if fila.wa_pn != identidad.pn:
                if fila.wa_pn:
                    log.warning(
                        "[AUTH] La cuenta %s decia ser %s y sus credenciales "
                        "dicen %s. Manda el fichero.",
                        corto,
                        (fila.wa_pn or "").split("@")[0],
                        identidad.telefono,
                    )
                fila.wa_pn = identidad.pn
                cambio = True
            if identidad.lid and fila.wa_lid != identidad.lid:
                fila.wa_lid = identidad.lid
                cambio = True
            if fila.phone_number != identidad.telefono:
                fila.phone_number = identidad.telefono
                cambio = True
            # El nombre que el usuario escribio manda sobre el del telefono;
            # solo se rellena si no habia ninguno o si el que hay era el
            # numero puesto por defecto.
            if identidad.nombre and fila.display_name in (None, "", fila.phone_number):
                fila.display_name = identidad.nombre[:120]
                cambio = True

            # Tiene credenciales completas: esta vinculada, se anotara o no.
            # `revoked` NO se toca -- ahi WhatsApp dijo expresamente que no.
            if fila.session_status == "never_linked":
                fila.session_status = "linked"
                activadas.append(corto)
                cambio = True

            if cambio:
                corregidas.append(corto)
        sesion.flush()

    duplicadas = {t: c for t, c in por_telefono.items() if len(c) > 1}
    for telefono, cuentas in duplicadas.items():
        log.error(
            "[AUTH] El telefono %s esta vinculado en %d cuentas (%s): cada "
            "conversacion suya esta guardada %d veces. Hay que quedarse con "
            "una.",
            telefono,
            len(cuentas),
            ", ".join(cuentas),
            len(cuentas),
        )
    if corregidas:
        log.info(
            "[AUTH] %d cuenta(s) reetiquetadas desde sus credenciales: %s",
            len(corregidas),
            ", ".join(corregidas),
        )
    if activadas:
        log.info(
            "[AUTH] %d cuenta(s) estaban vinculadas y marcadas como que no: %s",
            len(activadas),
            ", ".join(activadas),
        )
    return Reparacion(
        corregidas=corregidas, activadas=activadas, duplicadas=duplicadas
    )


def ya_vinculado_en_otra_cuenta(
    database: Any, settings: Any, *, user_id: Any, telefono: str, excepto: Any
) -> str | None:
    """El identificador corto de otra cuenta del usuario con ESE telefono.

    Vincular el mismo telefono dos veces no da dos copias: da la misma copia
    duplicada, con los mismos identificadores de mensaje en dos sitios. Se
    comprueba ANTES de sellar la vinculacion.
    """
    with database.transaction() as sesion:
        filas = sesion.execute(
            select(WhatsAppAccount).where(WhatsAppAccount.user_id == user_id)
        ).scalars().all()
        identificadores = [f.id for f in filas]

    de_la_base = _leer(_ruta_de_la_carpeta_base(settings))
    sin_propias = [
        i for i in identificadores if identidad_en_disco(settings, i) is None
    ]
    for ident in identificadores:
        if str(ident) == str(excepto):
            continue
        identidad = identidad_en_disco(settings, ident)
        if identidad is None and len(sin_propias) == 1 and sin_propias[0] == ident:
            identidad = de_la_base
        if identidad is not None and identidad.telefono == telefono:
            return str(ident)[:8]
    return None


def barrer_cuentas_huerfanas(database: Any, settings: Any, *, user_id: Any) -> list[str]:
    """Borra las filas de intentos de vinculacion abandonados.

    QUE ES UNA HUERFANA
    -------------------
    Una fila que nacio de pulsar "Agregar cuenta" y nunca llego a vincularse:
    sin numero (``wa_pn IS NULL``), sin fecha de vinculacion, sin credenciales
    en disco y sin una sola conversacion. No es de nadie y no lleva a ninguna
    parte.

    POR QUE ESTORBAN, Y NO SOLO EN EL SELECTOR
    ------------------------------------------
    Esto parecia cosmetico y no lo es. `identidad_de_la_cuenta` atribuye la
    carpeta del runtime base --``session/baileys/``, que no lleva el
    identificador en la ruta-- SOLO si hay una unica candidata sin
    credenciales propias. Con seis huerfanas hay siete candidatas, asi que se
    niega a adivinar y devuelve ``None``.

    Y entonces la cuenta se sella sin identidad. Se midio::

        2c9ef826  linked  207 chats  ACTIVA
                  tel=NULL  nombre=NULL  wa_pn=NULL

    En el selector eso se lee "Cuenta sin vincular" mientras extrae chats. O
    sea: las huerfanas no ensucian la lista, **rompen la atribucion**.

    LO QUE NUNCA SE BORRA
    ---------------------
    * la que tenga UN solo chat -- ahi hay contenido de alguien;
    * la que tenga credenciales en disco -- esa esta vinculada aunque la base
      no lo sepa todavia;
    * la activa;
    * y la MAS RECIENTE, que es la que puede estar escaneandose ahora mismo.
    """
    from app.models import Chat
    from app.models.accounts import UserWhatsAppMembership

    borradas: list[str] = []
    with database.transaction() as sesion:
        filas = sesion.execute(
            select(WhatsAppAccount)
            .where(WhatsAppAccount.user_id == user_id)
            .order_by(WhatsAppAccount.created_at)
        ).scalars().all()
        if len(filas) < 2:
            return []

        activa = sesion.execute(
            select(UserWhatsAppMembership.whatsapp_account_id).where(
                UserWhatsAppMembership.user_id == user_id,
                UserWhatsAppMembership.is_active.is_(True),
            )
        ).scalars().first()

        candidatas = []
        for fila in filas:
            if fila.wa_pn or fila.linked_at or fila.phone_number:
                continue
            if activa is not None and str(fila.id) == str(activa):
                continue
            if identidad_en_disco(settings, fila.id) is not None:
                continue
            tiene_chats = sesion.execute(
                select(Chat.id).where(Chat.whatsapp_account_id == fila.id).limit(1)
            ).first()
            if tiene_chats:
                continue
            candidatas.append(fila)

        # La ultima se respeta: puede ser la que se esta escaneando ahora.
        for fila in candidatas[:-1] if candidatas else []:
            borradas.append(str(fila.id)[:8])
            sesion.delete(fila)
        sesion.flush()

    if borradas:
        log.info(
            "[AUTH] %d cuenta(s) sin vincular y sin nada dentro borradas: %s",
            len(borradas),
            ", ".join(borradas),
        )
    return borradas
