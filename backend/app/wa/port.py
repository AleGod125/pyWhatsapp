"""El contrato que cumple cualquier proveedor de WhatsApp.

POR QUE EXISTE ESTE FICHERO
---------------------------
Hasta ahora no habia frontera. ``AppRuntime`` construia un ``WhatsAppClient``
y este entregaba el ``pywhats.Client`` CRUDO a ``Orchestrator.post_connect``,
que lo repartia a los servicios. De ahi que el 23 % de ``app/`` acabara
dependiendo de una libreria concreta, y que diez parches tuvieran que agarrarse
a metodos privados suyos.

Esto no cambia el comportamiento de nada. Escribe lo que YA se usaba, para que
una segunda implementacion sepa exactamente que tiene que ofrecer y para que
romperlo se note en una prueba en vez de en produccion.

LO QUE NO ESTA AQUI, A PROPOSITO
--------------------------------
Nada de envio. Ni ``send_text``, ni ``send_image``, ni ``mark_read``, ni
presencia. Se comprobo sobre todo ``app/``: de las 20 corutinas publicas del
cliente de pywhats el proyecto usa 6, y ninguna escribe en WhatsApp. Es una
copia de seguridad: solo lee.

Dejarlo fuera del contrato no es una omision, es la garantia: lo que no esta
en el puerto no se puede llamar desde arriba.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SesionRemota(Protocol):
    """Lo que los SERVICIOS reciben en ``post_connect``.

    Es el objeto que hoy es el ``pywhats.Client`` desnudo. Se escribe aparte
    del cliente porque su ciclo de vida es otro: el proveedor vive mientras
    viva el proceso; esto solo existe mientras haya sesion conectada.
    """

    @property
    def device(self) -> Any:
        """La identidad propia. Se le piden ``jid.user``, ``jid.server`` y ``lid``."""

    async def download_media(self, info: Any) -> bytes:
        """Descarga y VERIFICA un adjunto (enc-SHA256, HMAC y SHA256)."""

    async def get_group_info(self, jid: Any) -> Any:
        """Metadatos del grupo. Solo se lee ``.subject``."""


@runtime_checkable
class ClienteDeWhatsApp(Protocol):
    """Lo que ``AppRuntime`` construye y gobierna.

    Las dos implementaciones --``WhatsAppClient`` (pywhats) y
    ``BaileysClient`` (Node)-- cumplen esto, y por eso se pueden intercambiar
    con una variable de entorno.
    """

    #: Handlers SINCRONOS que corren antes de publicar el evento a la pantalla.
    #: Es por donde se persiste en PostgreSQL sin pasar por la cola de la GUI.
    #: Claves usadas hoy: ``message``, ``contact``, ``pushname``.
    sinks: dict[str, Any]

    #: Corrutina que se lanza cuando la sesion queda conectada. Recibe la
    #: :class:`SesionRemota`. La usan el backfill y el worker de multimedia,
    #: que necesitan el loop del cliente y NO deben bloquear al receptor.
    post_connect: Any

    #: Aviso a los servicios de fondo para que paren antes de cerrar la sesion.
    on_shutdown: Any

    @property
    def session_exists(self) -> bool:
        """``True`` si hay una identidad persistida reutilizable.

        Es lo que distingue "hay que ensenar un QR" de "reconectar y ya".
        """

    @property
    def device(self) -> Any:
        """La identidad propia, o ``None`` si todavia no hay sesion."""

    def start(self) -> None:
        """Arranca. NO bloquea: el trabajo se va a un hilo o a un proceso hijo."""

    def stop(self, timeout: float = 10.0) -> None:
        """Cierra ordenadamente. Nunca lanza."""


#: Los eventos que un proveedor tiene que saber emitir, con el nombre EXACTO.
#:
#: Esta lista es el contrato de verdad: cambiar uno de estos nombres rompe la
#: ingesta en silencio, porque los sinks y el traductor de SSE buscan por
#: nombre. Se comprueba en las pruebas contra las dos implementaciones.
#:
EVENTOS = (
    # Ciclo de vida
    "qr",
    "paired",
    "connected",
    # El login ACEPTADO, que no es lo mismo que el socket abierto: hasta aqui
    # el servidor todavia puede rechazar la sesion. De este evento cuelga
    # persistir la vinculacion y pasar a CONNECTED, asi que sin el la pantalla
    # se queda en el codigo QR con la sesion ya funcionando detras.
    #
    # Con pywhats no era un evento: lo producia un parche sobre
    # `SessionActivator.on_success`. Al soltar la libreria dejo de existir, y
    # por eso ahora es parte del contrato -- para que no vuelva a depender de
    # las tripas de nadie.
    "session_valid",
    "disconnected",
    "logged_out",
    # Mensajeria
    "message",
    "reaction",
    "message_edit",
    "message_revoke",
    "receipt",
    # Presencia -- no se persiste, pero se emite
    "presence",
    "chat_presence",
    # Sincronizacion
    "history_sync",
    "decrypt_error",
    # App-state: 'contact' y 'pushname' son la fuente de los nombres del panel
    "contact",
    "pushname",
    "mute",
    "pin",
    "archive",
)
