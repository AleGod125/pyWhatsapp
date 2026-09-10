"""``BackupStorage`` sobre Google Drive.

Implementa el contrato de :mod:`app.storage.interface` para UN usuario. Las
carpetas creadas se recuerdan en PostgreSQL: preguntarle a Drive por la
carpeta de cada chat antes de cada subida seria una llamada de red para
averiguar algo que no cambia.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, BinaryIO, Iterator

from sqlalchemy import delete, select

from app.core.logging_setup import get_logger
from app.models.storage import DriveFolder, GoogleDriveStorage
from app.storage.drive.client import LIMITE_SIMPLE, DriveClient
from app.storage.interface import (
    ArchivoSubido,
    PropiedadesDeArchivo,
    StorageError,
)

log = get_logger("STORAGE")

#: Nombre de la carpeta raiz dentro del Drive del usuario. Es lo unico que el
#: usuario ve en su Drive, asi que se escribe para una persona.
CARPETA_RAIZ = "WhatsApp Backup"

#: Con que se marca en Drive la carpeta de cada cuenta. Es el identificador de
#: la cuenta, y es lo que permite encontrarla aunque le cambien el nombre.
MARCA_DE_CUENTA = "wa_account_id"

#: Lo maximo que se deja de un nombre de perfil. Drive admite mas, pero un
#: nombre larguisimo hace la carpeta imposible de leer en la lista.
LARGO_DE_NOMBRE = 60


def _limpiar_nombre(nombre: str) -> str:
    """Deja un nombre de perfil en algo que sirva de carpeta.

    Se quitan las barras --parten la ruta en Drive-- y los caracteres de
    control. Los emoji se quedan: forman parte del nombre que la persona
    eligio, y Drive los admite.
    """
    limpio = "".join(
        c for c in (nombre or "") if c not in "/\\" and (c >= " " or c == " ")
    ).strip()
    # Las comillas simples romperian la consulta de busqueda de Drive.
    limpio = limpio.replace("'", "")
    return limpio[:LARGO_DE_NOMBRE].strip() or "sin nombre"


class DriveBackupStorage:
    """Almacenamiento de un usuario. Nunca de varios."""

    def __init__(
        self,
        *,
        user_id: uuid.UUID,
        client: DriveClient,
        database: Any,
    ) -> None:
        self._user_id = user_id
        self._client = client
        self._database = database
        #: Identificador de carpeta -> ruta que lo produjo. Solo en memoria, y
        #: solo para saber QUE ruta rehacer cuando una subida se encuentra con
        #: que su carpeta ya no existe: `store_bytes` recibe el identificador,
        #: no la ruta, y sin esto no habria forma de reconstruirla.
        self._rutas_por_id: dict[str, str] = {}

    # -- Preparacion ---------------------------------------------------------

    def ensure_user_storage(self) -> str:
        """Carpeta raiz + fila en PostgreSQL. Idempotente.

        Si ya hay identificador guardado no se pregunta a Drive: es el caso
        normal y hacerlo costaria una llamada de red por peticion.
        """
        with self._database.transaction() as sesion:
            fila = sesion.execute(
                select(GoogleDriveStorage).where(
                    GoogleDriveStorage.user_id == self._user_id
                )
            ).scalar_one_or_none()
            if fila is not None and fila.root_folder_id:
                return fila.root_folder_id

        # Con ``drive.file`` solo vemos lo que creamos nosotros, asi que
        # buscar por nombre no puede tropezar con carpetas ajenas.
        raiz = self._client.asegurar_carpeta(CARPETA_RAIZ)

        with self._database.transaction() as sesion:
            fila = sesion.execute(
                select(GoogleDriveStorage).where(
                    GoogleDriveStorage.user_id == self._user_id
                )
            ).scalar_one_or_none()
            if fila is None:
                fila = GoogleDriveStorage(user_id=self._user_id, root_folder_id=raiz)
                sesion.add(fila)
            else:
                fila.root_folder_id = raiz
            fila.updated_at = _ahora()
            fila.last_verified_at = _ahora()
            sesion.flush()

        log.info("Carpeta raiz de la copia lista en Drive")
        return raiz

    # -- Una carpeta por cuenta, con el nombre de su dueno -------------------
    #
    # Lo que el usuario ve en su Drive::
    #
    #     WhatsApp Backup/
    #       WhatsApp Dora Niebles/      <- una cuenta
    #         chats/<id>/messages
    #         chats/<id>/media
    #       WhatsApp Ale/               <- otra, independiente
    #         chats/...
    #
    # Antes ese nivel era el UUID de la cuenta: correcto pero ilegible, y sin
    # forma de saber cual es cual desde Drive. Ahora es el nombre, y la carpeta
    # se localiza por una MARCA con el identificador -- asi cambiar de nombre
    # en WhatsApp renombra la carpeta en vez de partir la copia en dos.
    #
    # Las rutas internas siguen usando el identificador, que no cambia: es lo
    # que se guarda en la cache de carpetas.

    def ensure_account_storage(self, account_id: str) -> str:
        return self._asegurar_ruta(account_id)

    def ensure_chat_storage(self, account_id: str, chat_id: str) -> str:
        return self._asegurar_ruta(f"{account_id}/chats/{chat_id}")

    def carpeta_de_mensajes(self, account_id: str, chat_id: str) -> str:
        return self._asegurar_ruta(f"{account_id}/chats/{chat_id}/messages")

    def carpeta_de_multimedia(self, account_id: str, chat_id: str) -> str:
        return self._asegurar_ruta(f"{account_id}/chats/{chat_id}/media")

    def etiqueta_de_cuenta(self, account_id: str) -> str:
        """Como se llama en Drive la carpeta de esta cuenta.

        Se prefiere el nombre que la persona tiene puesto; si no hay, su
        numero; y si tampoco, el identificador. Nunca se devuelve vacio: una
        carpeta sin nombre en Drive es inservible.
        """
        from app.models import WhatsAppAccount

        nombre = ""
        try:
            with self._database.transaction() as sesion:
                fila = sesion.get(WhatsAppAccount, uuid.UUID(str(account_id)))
                if fila is not None:
                    # El numero real vive en las credenciales, y `wa_pn` es lo
                    # que se copio de ellas. `phone_number` puede estar puesto
                    # a mano, asi que va despues.
                    telefono = (fila.wa_pn or "").split("@")[0].split(":")[0]
                    nombre = (
                        (fila.display_name or "").strip()
                        or (f"+{telefono}" if telefono else "")
                        or (f"+{fila.phone_number}" if fila.phone_number else "")
                    )
        except Exception:  # noqa: BLE001 - sin nombre se usa el identificador
            log.debug("No se pudo leer el nombre de la cuenta %s", account_id)

        if not nombre:
            nombre = str(account_id)[:8]
        return f"WhatsApp {_limpiar_nombre(nombre)}"

    def _asegurar_ruta(self, ruta: str) -> str:
        """El identificador de la carpeta, creando lo que falte.

        Si Drive contesta que el padre no existe, la cache esta describiendo
        un Drive que ya no es. Se tira entera y se reconstruye. Ver
        :meth:`_rehacer_desde_cero`.
        """
        cacheado = self._buscar_en_cache(ruta)
        if cacheado:
            self._rutas_por_id[cacheado] = ruta
            return cacheado
        try:
            return self._crear_cadena(ruta)
        except StorageError as exc:
            if exc.code != "DRIVE_NOT_FOUND":
                raise
            return self._rehacer_desde_cero(ruta)

    def _crear_cadena(self, ruta: str) -> str:
        """Recorre la ruta nivel a nivel, recordando cada uno.

        El PRIMER tramo es siempre el identificador de la cuenta, y ese no se
        crea por nombre sino por marca: ver :meth:`etiqueta_de_cuenta`.
        """
        padre = self.ensure_user_storage()
        acumulada = ""
        for indice, parte in enumerate(ruta.split("/")):
            acumulada = f"{acumulada}/{parte}" if acumulada else parte
            existente = self._buscar_en_cache(acumulada)
            if existente:
                padre = existente
                continue
            if indice == 0:
                padre = self._client.asegurar_carpeta_marcada(
                    self.etiqueta_de_cuenta(parte),
                    padre=padre,
                    clave=MARCA_DE_CUENTA,
                    valor=str(parte),
                )
            else:
                padre = self._client.asegurar_carpeta(parte, padre=padre)
            self._recordar(acumulada, padre)
        self._rutas_por_id[padre] = ruta
        return padre

    def _rehacer_desde_cero(self, ruta: str) -> str:
        """Olvida lo recordado y vuelve a crear la ruta. Se intenta UNA vez.

        POR QUE HACE FALTA
        ------------------
        La cache guarda identificadores de Drive para no preguntar por ellos en
        cada subida. Eso es correcto mientras las carpetas existan, y deja de
        serlo en cuanto el usuario borra la carpeta de la copia desde su Drive
        --que es algo que puede hacer cuando quiera y sin avisar.

        Lo que se veia entonces: la carpeta raiz, ``accounts`` y la de la cuenta
        respondiendo ``404 File not found`` una detras de otra, ninguna subida
        avanzando, y la papelera vacia (borrado definitivo, no hay nada que
        restaurar). Los identificadores seguian en PostgreSQL describiendo un
        Drive que ya no existia, y nada los cuestionaba nunca: cada trabajo
        reintentaba contra el mismo padre muerto hasta agotar los doce intentos.

        SE BORRA TODO, NO SOLO LA HOJA
        ------------------------------
        Cuando Drive dice que un padre no esta, no se sabe a que altura se
        rompio la cadena: puede ser la carpeta del chat o la raiz entera. Mirar
        nivel a nivel serian tantas llamadas como niveles, y por un caso que es
        raro. Se tira la cache del usuario --que es solo cache, no hay dato
        propio en ella-- y se reconstruye a demanda: la ruta de este trabajo
        ahora, las demas cuando les toque.

        LO QUE NO SE PIERDE
        -------------------
        Ni un mensaje ni un adjunto. Los segmentos ya subidos que apuntaban a
        Drive quedan sin respaldo remoto --porque el usuario lo borro-- pero su
        contenido sigue en PostgreSQL, que es la fuente de verdad.
        """
        log.warning(
            "Drive no encuentra las carpetas guardadas: se rehacen. Suele ser "
            "que la carpeta de la copia se borro desde Google Drive."
        )
        self._olvidar_todo()
        return self._crear_cadena(ruta)

    def _olvidar_todo(self) -> None:
        """Vacia la cache de carpetas del usuario, raiz incluida."""
        self._rutas_por_id.clear()
        with self._database.transaction() as sesion:
            sesion.execute(
                delete(DriveFolder).where(DriveFolder.user_id == self._user_id)
            )
            # La raiz tambien: si `accounts` no existe, lo normal es que su
            # padre tampoco. Dejarla puesta haria que la reconstruccion colgara
            # el arbol nuevo de una carpeta muerta y volviera a fallar igual.
            #
            # Se vacia en vez de borrar la fila: ahi estan los contadores de lo
            # subido, que no tienen la culpa. Y cadena vacia en vez de NULL
            # porque la columna es NOT NULL --lo cual es correcto: "no se cual
            # es la raiz" es un estado momentaneo, no un dato que se guarde.
            # `ensure_user_storage` la trata como ausente y la vuelve a crear.
            fila = sesion.execute(
                select(GoogleDriveStorage).where(
                    GoogleDriveStorage.user_id == self._user_id
                )
            ).scalar_one_or_none()
            if fila is not None:
                fila.root_folder_id = ""
            sesion.flush()

    def _buscar_en_cache(self, ruta: str) -> str | None:
        with self._database.transaction() as sesion:
            return sesion.execute(
                select(DriveFolder.folder_id).where(
                    DriveFolder.user_id == self._user_id, DriveFolder.path == ruta
                )
            ).scalar_one_or_none()

    def _recordar(self, ruta: str, folder_id: str) -> None:
        with self._database.transaction() as sesion:
            ya = sesion.execute(
                select(DriveFolder).where(
                    DriveFolder.user_id == self._user_id, DriveFolder.path == ruta
                )
            ).scalar_one_or_none()
            if ya is None:
                sesion.add(
                    DriveFolder(
                        user_id=self._user_id, path=ruta, folder_id=folder_id
                    )
                )
            else:
                ya.folder_id = folder_id
            sesion.flush()

    # -- Escritura -----------------------------------------------------------

    def store_bytes(
        self,
        *,
        carpeta: str,
        nombre: str,
        datos: bytes,
        propiedades: PropiedadesDeArchivo,
        mime_type: str = "application/octet-stream",
    ) -> ArchivoSubido:
        try:
            respuesta = self._client.subir_simple(
                nombre=nombre,
                padre=carpeta,
                datos=datos,
                propiedades=propiedades.to_dict(),
                mime_type=mime_type,
            )
        except StorageError as exc:
            # La carpeta estaba en cache, asi que `_asegurar_ruta` no llego a
            # preguntarle nada a Drive y no pudo enterarse de que ya no existe.
            # Es aqui, al subir, donde se descubre.
            if exc.code != "DRIVE_NOT_FOUND":
                raise
            respuesta = self._client.subir_simple(
                nombre=nombre,
                padre=self._recuperar_carpeta(carpeta),
                datos=datos,
                propiedades=propiedades.to_dict(),
                mime_type=mime_type,
            )
        subido = self._comprobar(respuesta, len(datos), nombre)
        self._anotar_subida(subido.size)
        return subido

    def store_stream(
        self,
        *,
        carpeta: str,
        nombre: str,
        origen: Iterator[bytes],
        tamano: int,
        propiedades: PropiedadesDeArchivo,
        mime_type: str = "application/octet-stream",
    ) -> ArchivoSubido:
        """Subida reanudable. Para archivos grandes."""
        try:
            sesion_url = self._client.iniciar_reanudable(
                nombre=nombre,
                padre=carpeta,
                propiedades=propiedades.to_dict(),
                tamano=tamano,
                mime_type=mime_type,
            )
        except StorageError as exc:
            if exc.code != "DRIVE_NOT_FOUND":
                raise
            sesion_url = self._client.iniciar_reanudable(
                nombre=nombre,
                padre=self._recuperar_carpeta(carpeta),
                propiedades=propiedades.to_dict(),
                tamano=tamano,
                mime_type=mime_type,
            )
        respuesta = self._client.subir_por_partes(sesion_url, origen, tamano)
        subido = self._comprobar(respuesta, tamano, nombre)
        self._anotar_subida(subido.size)
        return subido

    def _recuperar_carpeta(self, muerta: str) -> str:
        """Identificador nuevo para una carpeta que Drive ya no reconoce.

        Se necesita la RUTA para poder rehacerla, y quien sube solo tiene el
        identificador; de ahi ``_rutas_por_id``.

        Si no se sabe de que ruta salio, la cache se vacia IGUALMENTE. Un 404
        al subir ya demuestra que lo guardado describe un Drive que no existe,
        y dejarlo puesto seria condenar al trabajo a repetir el mismo fallo:
        ``_asegurar_ruta`` volveria a acertar en la cache, devolveria el mismo
        identificador muerto y nadie llegaria nunca a preguntarle a Drive. Ese
        es exactamente el bucle que hay que romper.
        """
        ruta = self._rutas_por_id.get(muerta)
        if not ruta:
            self._olvidar_todo()
            raise StorageError(
                "DRIVE_NOT_FOUND",
                "La carpeta de destino ya no esta en Drive. Se ha olvidado lo "
                "guardado y se rehara en el proximo intento.",
                reintentable=True,
            )
        return self._rehacer_desde_cero(ruta)

    @staticmethod
    def _comprobar(respuesta: dict, esperado: int, nombre: str) -> ArchivoSubido:
        """Que Drive responda 200 no basta.

        Se contrasta el tamano que dice haber guardado con el que se envio: un
        archivo truncado que se da por bueno es una copia que falla el dia que
        se necesita, y hasta entonces nadie lo sabe.
        """
        file_id = respuesta.get("id")
        if not file_id:
            raise StorageError(
                "DRIVE_NO_FILE_ID",
                "Drive no devolvio identificador de archivo.",
                reintentable=True,
            )
        declarado = respuesta.get("size")
        if declarado is not None and int(declarado) != esperado:
            raise StorageError(
                "DRIVE_SIZE_MISMATCH",
                f"El archivo subido mide {declarado} bytes y deberia medir {esperado}.",
                reintentable=True,
            )
        return ArchivoSubido(file_id=file_id, size=esperado)

    def _anotar_subida(self, bytes_subidos: int) -> None:
        with self._database.transaction() as sesion:
            fila = sesion.execute(
                select(GoogleDriveStorage).where(
                    GoogleDriveStorage.user_id == self._user_id
                )
            ).scalar_one_or_none()
            if fila is None:
                return
            fila.bytes_uploaded = (fila.bytes_uploaded or 0) + bytes_subidos
            fila.files_uploaded = (fila.files_uploaded or 0) + 1
            fila.last_upload_at = _ahora()
            fila.updated_at = _ahora()
            sesion.flush()

    # -- Lectura -------------------------------------------------------------

    def read_file(self, file_id: str) -> bytes:
        return self._client.descargar(file_id)

    def read_range(self, file_id: str, inicio: int, fin: int) -> bytes:
        return self._client.descargar_rango(file_id, inicio, fin)

    def open_stream(self, file_id: str) -> BinaryIO:
        import io

        return io.BytesIO(self._client.descargar(file_id))

    # -- Otros ---------------------------------------------------------------

    def exists(self, file_id: str) -> bool:
        return self._client.existe(file_id)

    def delete(self, file_id: str) -> bool:
        return self._client.borrar(file_id)

    def health_check(self) -> dict:
        return self._client.about()


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def limite_de_subida_simple() -> int:
    """Por encima de esto se sube por partes."""
    return LIMITE_SIMPLE
