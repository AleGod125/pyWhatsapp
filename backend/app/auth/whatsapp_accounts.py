"""La vinculacion de WhatsApp, con dueno.

AISLAMIENTO EN DISCO
--------------------
Cada CUENTA tiene su propia carpeta::

    session/accounts/<account_id>/device.json
    session/accounts/<account_id>/device.json.signal.db
    session/accounts/<account_id>/compat_prekey.db

Por cuenta y no por usuario: varias personas pueden compartir una cuenta de
WhatsApp, y entonces comparten identidad y Signal Store. Nombrar la carpeta
por el usuario daria dos carpetas para una sola identidad.

Identidad y Signal Store son INDIVISIBLES: van juntos o no va ninguno. Y
nunca se copia estado criptografico entre carpetas: dos cuentas son dos
identidades, y mezclarlas produce un dispositivo que no descifra nada.

EL EQUIPO NO ES DE NADIE
------------------------
Una maquina sostiene tantas vinculaciones como cuentas haya. Antes habia aqui
un modelo de "la sesion de este equipo" con un dueno unico, y con el, el
segundo usuario que entrara no podia ni pedir su codigo QR: se le contestaba
que el dispositivo estaba ocupado.

Las reglas son estas, y son de PERSONA, no de equipo:

* una cuenta de Google sostiene como mucho UNA cuenta de WhatsApp;
* una cuenta de WhatsApp puede estar en varias cuentas de Google, siempre de
  forma explicita (`app.auth.memberships`);
* lo que impide ver lo ajeno es la membresia, no un guarda de proceso.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.logging_setup import get_logger
from app.models import WhatsAppAccount

log = get_logger("AUTH")


class ConflictoDeSesion(Exception):
    """La sesion de WhatsApp de este equipo pertenece a otro usuario."""

    code = "WHATSAPP_OWNED_BY_ANOTHER_USER"

    def __init__(self) -> None:
        super().__init__(
            "Este dispositivo tiene una cuenta de WhatsApp vinculada a otro "
            "usuario. Cierra su sesion o desvincula esa cuenta para continuar."
        )


@dataclass(frozen=True)
class RutasDeSesion:
    """Donde vive el estado del companion de un usuario."""

    directorio: Path
    device: Path
    signal_store: Path
    compat_prekey: Path

    @property
    def existe(self) -> bool:
        return self.device.exists()

    @property
    def pareja_completa(self) -> bool:
        """Identidad y Signal Store, los dos o ninguno.

        Media identidad es peor que ninguna: un ``device.json`` nuevo sobre un
        store viejo produce una vinculacion que no descifra nada y cuesta
        horas de diagnosticar.
        """
        return self.device.exists() == self.signal_store.exists()


def rutas_de(settings: Any, user_id: Any) -> RutasDeSesion:
    """Las rutas de ESE usuario. No crea nada."""
    directorio = Path(settings.session_dir) / "users" / str(user_id)
    device = directorio / "device.json"
    return RutasDeSesion(
        directorio=directorio,
        device=device,
        signal_store=directorio / "device.json.signal.db",
        compat_prekey=directorio / "compat_prekey.db",
    )


def _clave_de_almacenamiento(account_id: Any) -> str:
    """Donde vive la sesion de esa cuenta, relativo a ``session/``.

    Por id de CUENTA, nunca de usuario. Varias personas pueden compartir una
    cuenta de WhatsApp --y entonces comparten identidad y Signal Store, que
    son indivisibles--, asi que nombrar la carpeta por el usuario produciria
    dos carpetas para una sola identidad.

    Tiene que coincidir con `app.core.session_paths.carpeta_de_cuenta`, que es
    quien construye la ruta de verdad.
    """
    return f"accounts/{account_id}"


class WhatsAppAccountService:
    """Alta, consulta y propiedad de las vinculaciones."""

    def __init__(self, database: Any, settings: Any) -> None:
        self._database = database
        self._settings = settings

    def cuenta_de(self, user_id: Any) -> WhatsAppAccount | None:
        with self._database.transaction() as session:
            fila = session.execute(
                select(WhatsAppAccount).where(WhatsAppAccount.user_id == user_id)
            ).scalars().first()
            if fila is not None:
                session.expunge(fila)
            return fila

    def asegurar_cuenta(self, user_id: Any) -> WhatsAppAccount:
        """La cuenta del usuario, creandola si no la tiene todavia.

        Una por usuario en esta fase. El esquema admite varias para el futuro,
        pero no se expone: soportar multi-cuenta sin interfaz para elegir solo
        produce estados que nadie puede resolver.
        """
        with self._database.transaction() as session:
            fila = session.execute(
                select(WhatsAppAccount).where(WhatsAppAccount.user_id == user_id)
            ).scalars().first()
            if fila is None:
                # El id se genera AQUI, no en el flush: la carpeta se nombra
                # por el id de la cuenta y `session_storage_key` es NOT NULL,
                # asi que tiene que existir antes de insertar la fila.
                import uuid as _uuid

                nuevo_id = _uuid.uuid4()
                fila = WhatsAppAccount(
                    id=nuevo_id,
                    user_id=user_id,
                    session_status="never_linked",
                    session_storage_key=_clave_de_almacenamiento(nuevo_id),
                )
                session.add(fila)
                session.flush()
                log.info("Cuenta de WhatsApp creada para el usuario")
            elif fila.session_storage_key != _clave_de_almacenamiento(fila.id):
                # Fila antigua con la clave por USUARIO. Se normaliza para que
                # la base diga donde vive la sesion de verdad. Es solo la
                # etiqueta: quien resuelve la ruta es `carpeta_de_cuenta`, y
                # esa siempre uso el id de cuenta. Sin esto, la base y el disco
                # discrepan y el arranque no sabe de quien es cada carpeta.
                fila.session_storage_key = _clave_de_almacenamiento(fila.id)
                # El flush va ANTES del expunge, y no es un detalle: expulsar
                # una fila con cambios pendientes los DESCARTA, la UPDATE no
                # llega a emitirse y la normalizacion se pierde en silencio.
                # Se midio: la cuenta seguia con la clave `users/<usuario>`
                # despues de pasar por aqui.
                session.flush()
            session.expunge(fila)
            return fila

    def dueno_actual(self) -> Any:
        """De quien es la UNICA vinculacion de este equipo, o ``None``.

        Estricto a proposito: solo cuentas que ya constan vinculadas. Es la
        pregunta que necesita la recuperacion al arrancar, y ampliarla haria
        que toda cuenta creada al pulsar "vincular" —aunque no llegara a
        completarse— se diera por buena.

        CON DOS VINCULADAS SE CONTESTA ``None``, Y ES LO IMPORTANTE
        ----------------------------------------------------------
        Antes se devolvia ``.first()`` sin ordenar: con A y B vinculados,
        PostgreSQL entrega la que quiera. Al arrancar, el runtime que sostiene
        la sesion suelta de A pedia aqui su dueno y podia recibir a B -- y
        entonces marcaba la cuenta de B como vinculada usando la identidad de
        A. Es el peor fallo posible en multiusuario: no da error, y le entrega
        a una persona la conversacion de otra.

        Con dos no hay respuesta correcta desde aqui, asi que no se da
        ninguna. Quien sepa de quien es cada sesion es el registro, que va por
        carpeta de cuenta.
        """
        from app.models.accounts import LINKED_STATUSES

        with self._database.transaction() as session:
            filas = (
                session.execute(
                    select(WhatsAppAccount)
                    .where(
                        WhatsAppAccount.session_status.in_(tuple(LINKED_STATUSES))
                    )
                    .limit(2)
                )
                .scalars()
                .all()
            )
            return filas[0].user_id if len(filas) == 1 else None

    def dueno_de_la_sesion_en_disco(self) -> Any:
        """A quien pertenece la sesion que hay guardada, o ``None``.

        Es OTRA pregunta que :meth:`dueno_actual`, y se usa solo al arrancar.
        Entre pedir la vinculacion y completarla hay un hueco: la cuenta ya
        existe con su ``user_id`` y todavia no consta vinculada. Si el
        servicio se reinicia justo ahi, ``dueno_actual`` diria ``None`` y la
        sesion que se conecta despues no se anotaria nunca.

        Esto NO es adoptar una sesion huerfana: el dueno se LEE de una fila
        que ya lo tiene escrito. Lo que se sigue sin hacer es inventarlo
        cuando no hay ninguna fila, o cuando hay varias candidatas y ninguna
        vinculada: ahi se devuelve ``None`` en vez de adivinar.
        """
        ya = self.dueno_actual()
        if ya is not None:
            return ya

        with self._database.transaction() as session:
            filas = (
                session.execute(
                    select(WhatsAppAccount).where(
                        WhatsAppAccount.session_status.notin_(("revoked", "error"))
                    )
                )
                .scalars()
                .all()
            )
            return filas[0].user_id if len(filas) == 1 else None

    def exigir_propiedad(self, user_id: Any) -> None:
        """Lanza si la sesion vinculada es de otro.

        Sin esto, el segundo usuario que entrara en el mismo equipo veria los
        chats del primero: la sesion en disco no sabe de quien es.
        """
        dueno = self.dueno_actual()
        if dueno is not None and dueno != user_id:
            raise ConflictoDeSesion()

    def marcar_vinculada(self, user_id: Any, *, pn: str | None, lid: str | None) -> None:
        ahora = datetime.now(timezone.utc)
        with self._database.transaction() as session:
            fila = session.execute(
                select(WhatsAppAccount).where(WhatsAppAccount.user_id == user_id)
            ).scalars().first()
            if fila is None:
                return
            fila.session_status = "linked"
            fila.wa_pn = pn or fila.wa_pn
            fila.wa_lid = lid or fila.wa_lid
            if pn:
                fila.phone_number = pn.split("@")[0].split(":")[0]
            fila.linked_at = fila.linked_at or ahora
            fila.last_connected_at = ahora
            fila.updated_at = ahora
            session.flush()

    def marcar_estado(self, user_id: Any, estado: str) -> None:
        with self._database.transaction() as session:
            fila = session.execute(
                select(WhatsAppAccount).where(WhatsAppAccount.user_id == user_id)
            ).scalars().first()
            if fila is None:
                return
            fila.session_status = estado
            fila.updated_at = datetime.now(timezone.utc)
            session.flush()

    def rutas(self, user_id: Any) -> RutasDeSesion:
        return rutas_de(self._settings, user_id)
