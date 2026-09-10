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
        """La cuenta ACTIVA del usuario, creandola si no tiene ninguna.

        Esto es "sigue donde estabas". Para ANADIR otra esta
        :meth:`crear_cuenta_nueva`, que es una decision distinta y explicita:
        aqui, con una cuenta ya vinculada, devolverla es lo correcto -- crear
        una segunda porque alguien recargo la pagina no lo seria.
        """
        from app.auth.memberships import conceder, cuenta_activa_de

        with self._database.transaction() as session:
            # La que este mirando; si no hay ninguna marcada, la mas
            # antigua. Sin `ORDER BY`, "la primera" cambiaba entre dos
            # peticiones y el usuario abriria un WhatsApp u otro.
            fila = cuenta_activa_de(session, user_id)
            if fila is None:
                fila = session.execute(
                    select(WhatsAppAccount)
                    .where(WhatsAppAccount.user_id == user_id)
                    .order_by(WhatsAppAccount.created_at, WhatsAppAccount.id)
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
                # La membresia, y ACTIVA. Sin ella el usuario tiene cuenta
                # pero ninguna seleccionada, y no veria sus propios chats.
                conceder(
                    session, user_id=user_id, account_id=nuevo_id, activar=True
                )
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

    def crear_cuenta_nueva(
        self, user_id: Any, *, display_name: str | None = None
    ) -> WhatsAppAccount:
        """OTRA cuenta de WhatsApp para el mismo usuario. Siempre crea.

        Se separa de :meth:`asegurar_cuenta` a proposito: aquella significa
        "sigue donde estabas" y esta "quiero vincular otro numero". Con una
        sola funcion, recargar la pantalla de vinculacion habria ido creando
        cuentas vacias una detras de otra.

        NO SE ACTIVA AQUI. Se activa cuando termine de vincularse: cambiar el
        contexto a una cuenta que todavia no tiene sesion dejaria al usuario
        mirando una lista vacia mientras escanea, y sin forma de volver.

        Su carpeta de sesion (``session/accounts/<id>/``) y su carpeta en
        Drive salen del id nuevo, asi que nacen separadas de las de la cuenta
        anterior sin hacer nada mas. En Drive la carpeta se LLAMA como su
        dueno --"WhatsApp Dora Niebles"-- pero se localiza por el id, que no
        cambia aunque la persona se cambie el nombre de perfil.
        """
        import uuid as _uuid

        from app.auth.memberships import conceder

        nuevo_id = _uuid.uuid4()
        with self._database.transaction() as session:
            fila = WhatsAppAccount(
                id=nuevo_id,
                user_id=user_id,
                session_status="never_linked",
                session_storage_key=_clave_de_almacenamiento(nuevo_id),
                display_name=(display_name or "").strip()[:120] or None,
            )
            session.add(fila)
            session.flush()
            conceder(session, user_id=user_id, account_id=nuevo_id)
            session.expunge(fila)
        log.info(
            "[AUTH] cuenta de WhatsApp ANADIDA: usuario=%s cuenta=%s",
            str(user_id)[:8],
            str(nuevo_id)[:8],
        )
        return fila

    def renombrar(self, user_id: Any, account_id: Any, nombre: str) -> bool:
        """Cambia el nombre visible. Solo si esa cuenta es de ese usuario.

        El nombre es de la INTERFAZ. No toca la carpeta de sesion ni el
        espacio de Drive, que van por identificador: renombrar no puede dejar
        huerfana una copia de seguridad, y dos cuentas pueden llamarse igual
        sin chocar.
        """
        from app.auth.memberships import tiene_acceso

        limpio = (nombre or "").strip()[:120]
        with self._database.transaction() as session:
            if not tiene_acceso(session, user_id, account_id):
                return False
            fila = session.get(WhatsAppAccount, account_id)
            if fila is None:
                return False
            fila.display_name = limpio or None
            session.flush()
        return True

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

    def marcar_vinculada(
        self,
        user_id: Any,
        *,
        pn: str | None,
        lid: str | None,
        account_id: Any = None,
        profile_name: str | None = None,
    ) -> Any:
        """Anota que ESTA cuenta quedo vinculada.

        DEVUELVE EL ID DE LA CUENTA QUE DE VERDAD SE SELLO, o ``None``.

        Casi siempre es la que se pidio. Pero si resulta que en su carpeta hay
        la sesion de OTRO telefono, lo que se sella es la cuenta de ESE
        telefono --se busca la suya o se crea-- y se devuelve esa. Quien llamo
        tiene que mirar lo que vuelve: si no es la que pidio, su runtime esta
        apuntando a la cuenta equivocada.

        Es truthy/falsy igual que antes, asi que `if not sellada:` sigue
        valiendo.

        DEVUELVE ALGO A PROPOSITO
        -------------------------
        Antes no devolvia nada y quien llamaba escribia "marcada como
        vinculada" en el log pasara lo que pasara. Se vio en la instalacion
        del usuario: esa linea salia cada pocos segundos mientras la cuenta
        seguia `never_linked` y sin numero, porque el metodo se rendia pronto
        --dos cuentas y sin `account_id`-- y volvia en silencio. Un log que
        afirma lo contrario de lo que pasa es peor que no tener log.


        ``account_id`` no es opcional de verdad: sin el hay que adivinar cual
        de las cuentas del usuario se acaba de vincular. Con una sola no hay
        nada que adivinar y se usa esa; CON VARIAS NO SE ANOTA NADA.

        POR QUE, MEDIDO
        ---------------
        El respaldo "la mas antigua" venia de cuando solo podia haber una
        cuenta. Con cuatro, sello a la equivocada. En la instalacion del
        usuario, sus cuatro cuentas acabaron con el numero, el LID y el nombre
        de OTRO telefono::

            creds de 69900813  ->  573002389304   (su numero real)
            wa_pn en la base   ->  573008927374   (el del otro telefono)

        Y ese ``wa_pn`` es lo que marca el "chat contigo mismo" y lo que
        permite atribuir una sesion suelta a su cuenta. Con el cambiado, la
        aplicacion cree que una cuenta es otra.

        No anotar deja la fila como estaba --``never_linked``, sin numero-- y
        eso se ve y se corrige. Anotar sobre la cuenta equivocada no se ve.

        ``profile_name`` es el nombre que WhatsApp da al perfil. Se usa como
        nombre visible SOLO si el usuario no le puso uno: lo que el escribio
        manda sobre lo que diga el telefono.
        """
        ahora = datetime.now(timezone.utc)
        with self._database.transaction() as session:
            fila = None
            if account_id is not None:
                fila = session.get(WhatsAppAccount, account_id)
                # Y que sea de ese usuario: marcar como vinculada la cuenta de
                # otro por un identificador equivocado seria mucho peor que no
                # marcar nada.
                if fila is not None and str(fila.user_id) != str(user_id):
                    fila = None
            if fila is None:
                suyas = session.execute(
                    select(WhatsAppAccount)
                    .where(WhatsAppAccount.user_id == user_id)
                    .order_by(WhatsAppAccount.created_at, WhatsAppAccount.id)
                ).scalars().all()
                if len(suyas) != 1:
                    if suyas:
                        log.warning(
                            "NO se marca ninguna cuenta como vinculada: el "
                            "usuario tiene %d y no se dijo cual. Sellar la "
                            "equivocada le pondria el numero de otro telefono. "
                            "Pasa `account_id`.",
                            len(suyas),
                        )
                    return False
                fila = suyas[0]
            if fila is None:
                return False

            # MANDAN LAS CREDENCIALES DE ESTA CUENTA, no lo que llegue por
            # parametro.
            #
            # `pn` viene del flujo de vinculacion, y ese camino tiene por
            # donde equivocarse. Se midio equivocandose DOS veces:
            #
            #   fila 91a8470b  phone_number = 573002389304  creds = 573008927374
            #   fila 36e15dde  wa_pn/wa_lid = los de Ale    creds = Dora Niebles
            #
            # En los dos casos el selector enseñaba el telefono de una persona
            # con el numero de otra, y desde fuera se veia igual que "los chats
            # estan mezclados". No lo estaban: la etiqueta era de otro.
            #
            # `creds.json` lo escribe Baileys con lo que contesta WhatsApp: es
            # el unico sitio que sabe sin dudar quien hay al otro lado.
            from app.auth.atribucion import identidad_de_la_cuenta

            real = identidad_de_la_cuenta(session, self._settings, fila.id)
            if real is not None:
                # HAY FICHERO: manda el fichero, diga lo que diga el parametro.
                # Se comparan los NUMEROS, no las cadenas enteras.
                #
                # `wa_pn` llega con el identificador de dispositivo pegado
                # --`573008927374:33@s.whatsapp.net`-- asi que comparar la
                # cadena hacia saltar el aviso contra si mismo:
                #
                #     "Se intento sellar la cuenta como 573008927374, pero sus
                #      credenciales son de 573008927374"
                #
                # Un WARNING que se contradice solo estorba al leer el log.
                if pn and pn.split("@")[0].split(":")[0] != real.telefono:
                    log.warning(
                        "[AUTH] Se intento sellar la cuenta %s como %s, pero "
                        "sus credenciales son de %s. Manda el fichero.",
                        str(fila.id)[:8],
                        pn.split("@")[0],
                        real.telefono,
                    )
                # EL LID SALE DE LA MISMA FUENTE QUE EL NUMERO.
                #
                # Antes era `real.lid or lid`, y con unas credenciales sin LID
                # --que las hay-- se colaba el que venia por parametro. Se
                # midio: la cuenta quedaba con el numero de Dora y el LID de
                # Ale. Una identidad a medias de cada uno es peor que
                # cualquiera de las dos enteras: el LID es lo que marca el
                # chat propio y lo que empareja contactos.
                # IDENTIDAD INVERTIDA: la fila YA estaba registrada a otro
                # numero.
                #
                # Este caso no lo ve la comprobacion de arriba, y es el que
                # se observo en produccion. Se escanea el QR de Dora en el
                # flujo de una fila que estaba registrada como Ale: Baileys
                # escribe las credenciales de Dora en ESA carpeta, asi que el
                # fichero y el socket coinciden --los dos dicen Dora-- y nada
                # salta. La fila de Ale se convertia en Dora en silencio, con
                # los chats de Ale colgando de una etiqueta que decia Dora.
                #
                # En Drive se veia como "WhatsApp Ale" con las conversaciones
                # de Dora dentro.
                #
                # Quien manda aqui es lo que la fila YA tenia registrado: es
                # a esa identidad a la que pertenecen sus chats. No se pisa.
                ya_registrado = (fila.wa_pn or "").split("@")[0].split(":")[0]
                if ya_registrado and ya_registrado != real.telefono:
                    log.error(
                        "[AUTH] IDENTIDAD INVERTIDA: la cuenta %s esta "
                        "registrada como %s y la sesion que hay en su carpeta "
                        "es de %s. Sus chats son de %s: no se reetiqueta.",
                        str(fila.id)[:8],
                        ya_registrado,
                        real.telefono,
                        ya_registrado,
                    )
                    # NO basta con rechazar.
                    #
                    # Rechazando y ya, la sesion queda conectada pero sin poder
                    # escribir en ninguna parte --el guardia de ingesta la
                    # bloquea, y con razon-- asi que el usuario se queda en un
                    # callejon sin salida: el telefono vinculado y nada que
                    # aparezca por ningun lado.
                    #
                    # Lo correcto es entender lo que de verdad ha pasado: han
                    # escaneado OTRO telefono. Eso no es un error que reportar,
                    # es una cuenta nueva. Se busca la suya --o se crea-- y se
                    # sella esa; quien reasigna el runtime y mueve la carpeta
                    # es quien llamo, que es el unico que puede parar el
                    # cliente para hacerlo sin romper la sesion.
                    destino = self._cuenta_para_ese_telefono(
                        session, user_id=user_id, identidad=real
                    )
                    if destino is None:
                        return None
                    # La cuenta anterior se queda con SUS chats y SU numero,
                    # pero sin sesion utilizable: se la acaban de sobrescribir.
                    fila.session_status = "disconnected"
                    fila.updated_at = ahora
                    session.flush()
                    log.warning(
                        "[AUTH] %s pasa a 'disconnected' (su sesion la piso "
                        "otro telefono) y %s recibe la vinculacion de %s",
                        str(fila.id)[:8],
                        str(destino)[:8],
                        real.telefono,
                    )
                    return destino

                pn, lid = real.pn, real.lid
                profile_name = profile_name or real.nombre
            elif pn:
                # SIN FICHERO se sella con lo que llegue, pero se avisa.
                #
                # Negarse aqui seria peor que el problema: dejaria la cuenta
                # sin sellar --invisible en el selector y sin runtime-- que es
                # exactamente el sintoma que se esta arreglando. Y hay un
                # momento legitimo en que el fichero aun no esta escrito.
                #
                # Lo que cerro el fallo medido no es esta rama: es que
                # `identidad_de_la_cuenta` mire TAMBIEN la carpeta del runtime
                # base. Sin eso, 36e15dde --cuyas credenciales viven en
                # `session/baileys/`-- parecia no tener ninguna y se dejaba
                # escribir el numero de Ale encima. Con eso, se resuelve a
                # Dora y el numero ajeno se rechaza arriba.
                log.info(
                    "[AUTH] La cuenta %s se sella como %s sin poder "
                    "contrastarlo con ningun creds.json.",
                    str(fila.id)[:8],
                    pn.split("@")[0],
                )

            # UN TELEFONO, UNA CUENTA.
            #
            # Vincular el mismo telefono dos veces no da dos copias: da la
            # misma copia duplicada, con los mismos identificadores de mensaje
            # en dos filas. Paso -- 132 conversaciones guardadas dos veces.
            # Y anadir "otra cuenta" que resulta ser la misma es el caso mas
            # facil de hacer sin querer.
            telefono_ahora = (
                real.telefono
                if real is not None
                else (pn or "").split("@")[0].split(":")[0]
            )
            gemela = (
                self._otra_cuenta_con_el_mismo_telefono(
                    session,
                    user_id=user_id,
                    telefono=telefono_ahora,
                    excepto=fila.id,
                )
                if telefono_ahora
                else None
            )
            if gemela is not None:
                log.error(
                    "[AUTH] El telefono %s YA esta vinculado en la cuenta %s. "
                    "No se sella %s: seria la misma copia por duplicado.",
                    telefono_ahora,
                    gemela,
                    str(fila.id)[:8],
                )
                # `error`, que es un estado que la base admite y que
                # `LINKED_STATUSES` no incluye: asi no se le levanta runtime ni
                # se le pide historial a un telefono que ya tiene su cuenta.
                fila.session_status = "error"
                fila.updated_at = ahora
                session.flush()
                return False

            fila.session_status = "linked"
            fila.wa_pn = pn or fila.wa_pn
            # Con fichero, la identidad ENTERA sale de el -- incluido un LID
            # ausente. Conservar el que hubiera dejaria el LID de otro
            # telefono pegado a este numero, y el LID es lo que marca el chat
            # propio y lo que empareja contactos.
            fila.wa_lid = real.lid if real is not None else (lid or fila.wa_lid)
            if pn:
                fila.phone_number = pn.split("@")[0].split(":")[0]
            nombre = (profile_name or "").strip()[:120]
            if nombre and not fila.display_name:
                fila.display_name = nombre
            fila.linked_at = fila.linked_at or ahora
            fila.last_connected_at = ahora
            fila.updated_at = ahora
            session.flush()
            return fila.id

    def _cuenta_para_ese_telefono(
        self, session: Any, *, user_id: Any, identidad: Any
    ) -> Any:
        """La fila que le corresponde a ESE telefono. Se reutiliza o se crea.

        Se busca por `wa_pn` --no por las credenciales en disco-- porque lo que
        interesa aqui es a que fila pertenece ese numero segun la base, y
        puede existir de antes: revocada, desconectada, o creada y nunca
        vinculada. Reutilizarla conserva los chats que ya tuviera.
        """
        telefono = identidad.telefono
        candidatas = session.execute(
            select(WhatsAppAccount).where(WhatsAppAccount.user_id == user_id)
        ).scalars().all()
        for otra in candidatas:
            suyo = (otra.wa_pn or "").split("@")[0].split(":")[0]
            if suyo == telefono:
                otra.session_status = "linked"
                otra.wa_lid = identidad.lid
                otra.phone_number = telefono
                if identidad.nombre and not otra.display_name:
                    otra.display_name = identidad.nombre[:120]
                session.flush()
                log.info(
                    "[AUTH] %s ya era de %s: se reutiliza en vez de duplicar",
                    str(otra.id)[:8],
                    telefono,
                )
                return otra.id

        import uuid as _uuid

        nueva_id = _uuid.uuid4()
        session.add(
            WhatsAppAccount(
                id=nueva_id,
                user_id=user_id,
                wa_pn=identidad.pn,
                wa_lid=identidad.lid,
                phone_number=telefono,
                display_name=(identidad.nombre or "")[:120] or None,
                session_status="linked",
                # La clave sale del id nuevo, igual que la carpeta: quien mueva
                # la sesion tiene que dejarla donde esta clave dice.
                session_storage_key=_clave_de_almacenamiento(nueva_id),
                linked_at=datetime.now(timezone.utc),
            )
        )
        session.flush()
        log.info(
            "[AUTH] cuenta NUEVA %s para el telefono %s",
            str(nueva_id)[:8],
            telefono,
        )
        return nueva_id

    def _otra_cuenta_con_el_mismo_telefono(
        self, session: Any, *, user_id: Any, telefono: str, excepto: Any
    ) -> str | None:
        """Otra cuenta del usuario cuya sesion es ESE mismo telefono.

        Se compara contra las CREDENCIALES de cada cuenta, no contra
        `phone_number`: ese campo es justamente el que puede estar mal, y
        compararse con el dejaria pasar el duplicado precisamente cuando hay
        una etiqueta equivocada de por medio.
        """
        from app.auth.atribucion import identidad_en_disco

        filas = session.execute(
            select(WhatsAppAccount).where(WhatsAppAccount.user_id == user_id)
        ).scalars().all()
        for otra in filas:
            if str(otra.id) == str(excepto):
                continue
            suya = identidad_en_disco(self._settings, otra.id)
            if suya is not None and suya.telefono == telefono:
                return str(otra.id)[:8]
        return None

    def marcar_estado(
        self, user_id: Any, estado: str, *, account_id: Any = None
    ) -> None:
        """Anota el estado de ESA cuenta.

        Igual que :meth:`marcar_vinculada`: sin ``account_id`` hay que adivinar
        cual de las cuentas del usuario cambio de estado, y con dos la
        eleccion seria una cualquiera -- se anotaria "desconectada" sobre la
        que sigue funcionando. Se admite vacio por las llamadas antiguas, y
        entonces se usa la mas antigua.
        """
        with self._database.transaction() as session:
            fila = None
            if account_id is not None:
                fila = session.get(WhatsAppAccount, account_id)
                if fila is not None and str(fila.user_id) != str(user_id):
                    fila = None
            if fila is None:
                # SOLO se adivina cuando no hay nada que adivinar.
                #
                # El respaldo "la mas antigua" es de cuando solo podia haber
                # una cuenta. Con dos, anotar `revoked` sobre la equivocada
                # deja al usuario con la cuenta buena marcada como muerta y la
                # muerta marcada como viva -- exactamente al reves, y sin nada
                # en el log que lo delate. Antes que eso, no anotar.
                suyas = session.execute(
                    select(WhatsAppAccount)
                    .where(WhatsAppAccount.user_id == user_id)
                    .order_by(WhatsAppAccount.created_at, WhatsAppAccount.id)
                ).scalars().all()
                if len(suyas) != 1:
                    if suyas:
                        log.warning(
                            "No se anota '%s': el usuario tiene %d cuentas y "
                            "no se dijo cual. Pasa `account_id`.",
                            estado,
                            len(suyas),
                        )
                    return
                fila = suyas[0]
            if fila is None:
                return
            fila.session_status = estado
            fila.updated_at = datetime.now(timezone.utc)
            session.flush()

    def rutas(self, user_id: Any) -> RutasDeSesion:
        return rutas_de(self._settings, user_id)
