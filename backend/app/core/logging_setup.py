"""Logging con etiquetas por subsistema y un filtro anti-secretos.

Formato de los mensajes de la aplicacion:

    [APP] Iniciando...
    [DB]  PostgreSQL disponible

Etiquetas en uso: APP, CONFIG, DB, WA, PAIRING, QR, SIGNAL, SYNC, BACKFILL,
MEDIA, GUI, PREVIEW.

El filtro :class:`SecretRedactionFilter` es una red de seguridad de ultimo
recurso, no una excusa para loguear material sensible: la regla sigue siendo
no pasar secretos al logger.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

# Etiquetas conocidas. Se exponen como helpers ``log_app``, ``log_db``, ...
TAGS = (
    "APP",
    "CONFIG",
    "DB",
    "WA",
    "PAIRING",
    "QR",
    "SIGNAL",
    "SYNC",
    "BACKFILL",
    "MEDIA",
    "API",
    "SSE",
    # Vistas previas del panel. Antes era [COMPAT], de la epoca en que el
    # diagnostico de tipos vivia entre los parches de pywhats.
    "PREVIEW",
    # Mensajes en vivo. Tiene etiqueta propia para que un problema de
    # multimedia o de excavacion no se confunda con uno de recepcion: cuando
    # todo se mezclaba bajo [APP] costaba ver donde moria un mensaje.
    "LIVE",
    # Cuentas, sesiones web y OAuth. Separada de [API] a proposito: los
    # intentos de acceso y los cambios de credencial son lo primero que se
    # mira cuando algo huele raro, y no pueden quedar sepultados entre
    # peticiones normales.
    "AUTH",
    # Google Drive.
    "DRIVE",
    # Busqueda de anclas de historial. Etiqueta propia porque es lo que se
    # mira para saber si una conversacion sin historial puede recuperarlo.
    "PLAN_E",
    # Segmentos, cola de subidas y cache local. Separada de [DRIVE] porque
    # una cosa es el pipeline y otra el proveedor: si manana el destino no es
    # Drive, [STORAGE] sigue significando lo mismo.
    "STORAGE",
    # Web Companion: el dispositivo vinculado aparte que sirve para medir que
    # ve WhatsApp Web. Etiqueta propia porque es experimental y opcional, y
    # tiene que poder distinguirse de la sesion de verdad de un vistazo.
    "WEB",
    # La prueba simetrica del Plan J3.1: los peldanos de la escalera y el
    # congelado de metricas. Etiqueta propia porque es la traza de UN
    # experimento con fecha, y mezclarla con [WEB] o [SYNC] haria imposible
    # leer despues que se midio y cuando.
    "J31",
)

#: Cuantos caracteres hexadecimales se dejan ver de un volcado.
#:
#: Seis bytes bastan para leer el numero de campo y la longitud del primer
#: elemento del protobuf, que es para lo que sirve ese log. No bastan para
#: llevarse el contenido.
HEX_VISIBLE = 12


def _recortar_hex(coincidencia: "re.Match[str]") -> str:
    """Deja la cabecera del volcado y tapa el resto."""
    etiqueta, crudo = coincidencia.group(1), coincidencia.group(2)
    if len(crudo) <= HEX_VISIBLE:
        return f"{etiqueta}{crudo}"
    return f"{etiqueta}{crudo[:HEX_VISIBLE]}...[{len(crudo) // 2} bytes redactados]"


# Patrones redactados si se cuelan en un mensaje ya formateado.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # password dentro de una URL de conexion
    (re.compile(r"(://[^:/@\s]+):([^@\s]+)@"), r"\1:***@"),
    # asignaciones tipo password=..., secret=..., token=..., api_key=...
    (
        re.compile(
            r"\b(password|passwd|pwd|secret|token|api_key|apikey)\s*[=:]\s*(\S+)",
            re.IGNORECASE,
        ),
        r"\1=***",
    ),
    # Volcados hexadecimales del cuerpo YA DESCIFRADO de un mensaje.
    #
    # pywhats registra en INFO ``receiver: empty text id=... len=... hex=...``
    # cuando su ``_extract_text`` no reconoce la variante. Esa cadena es el
    # protobuf entero en claro: lleva el texto del mensaje, las URL de
    # multimedia y el resto de la metadata. Se midieron 220 apariciones en un
    # solo archivo de log, todas en INFO.
    #
    # Se recorta en vez de borrarse: la cabecera dice QUE campo llego, que es
    # para lo que existe ese aviso, y el contenido se queda fuera.
    (re.compile(r"\b(hex=)([0-9a-fA-F]{16,})"), _recortar_hex),
    # URL de descarga de multimedia: llevan la ruta directa del adjunto.
    (re.compile(r"https?://[^\s]*whatsapp\.(?:net|com)/[^\s]*"), "https://***"),
    # Un JID completo es un numero de telefono, o el identificador con el que
    # WhatsApp senala a una persona. pywhats los escribe enteros en INFO
    # (``sender: preparing message id=... to=573001112233@s.whatsapp.net``, los
    # acuses de recibo, los avisos de descifrado). Se dejan los primeros
    # digitos, que bastan para seguir una conversacion en el log, y se tapa el
    # resto.
    (
        re.compile(
            r"(?<![0-9])([0-9]{6})[0-9]{2,}"
            r"(?=(?::[0-9]+)?@(?:s\.whatsapp\.net|lid|c\.us|g\.us))"
        ),
        r"\1***",
    ),
    # UN TELEFONO SUELTO, sin el ``@s.whatsapp.net`` detras.
    #
    # El patron de arriba exige el servidor, y por eso dejaba pasar todo lo
    # que escribe el numero pelado. Los avisos de atribucion son justo asi:
    #
    #     [AUTH] IDENTIDAD INVERTIDA: la cuenta 91a8470b esta registrada como
    #            573002389304 y la sesion que hay en su carpeta es de
    #            573008927374
    #
    # Tres numeros de dos personas reales, en claro, en un fichero que se
    # guardaba 120 MB de historial. Se deja el prefijo --pais y operadora,
    # que es lo que sirve para seguir un caso-- y se tapa el resto.
    #
    # Va DESPUES del patron de JID a proposito: aquel ya dejo su marca y esto
    # no vuelve a tocarlo, porque exige que no haya ``@`` justo detras.
    #
    # ONCE DIGITOS COMO MINIMO, y no diez. Una marca de tiempo Unix en
    # segundos tiene exactamente diez (``1700000000``), y este log esta lleno
    # de ellas: anclas, cursores, ventanas de reintento. Tapandolas se pierde
    # justamente lo que sirve para seguir un historial atascado.
    #
    # Un telefono con indicativo de pais no baja de once (``573002389304`` son
    # doce). El solape queda en los trece digitos de una marca en
    # milisegundos; ahi se tapa de mas, y se prefiere asi: perder una cifra de
    # diagnostico se nota y se arregla, filtrar el telefono de alguien no.
    # Ni el punto ni los dos puntos valen como frontera por la derecha.
    #
    # Estaban en la exclusion para no partir versiones ni direcciones IP, y
    # dejaban pasar lo mas comun de todo: el numero al FINAL DE LA FRASE.
    # Sobre el log real quedaban catorce asi::
    #
    #     ...sus credenciales son de 573008927374. Manda el fichero.
    #     ...y en su carpeta es de 573008927374. Sus chats son de 573002389304:
    #
    # No hacian falta para lo que protegian: los tramos de una IP o de una
    # version tienen tres o cuatro cifras, y aqui se piden once como minimo.
    # El guion si se queda: cubre los nombres con fecha --
    # ``session-20260911185133-revoked``-- que no son de nadie.
    # El `(?<!hex=)` protege lo que el recorte de arriba dejo a proposito.
    #
    # `_recortar_hex` conserva los primeros caracteres del volcado para poder
    # saber QUE campo llego --que es la unica razon de que ese aviso exista--
    # y esos caracteres pueden ser doce digitos seguidos (``000102030405``),
    # que encajan aqui. Sin esta guarda, el recorte quedaba recortado otra vez
    # y el aviso perdia su unico contenido util.
    (
        re.compile(
            r"(?<!hex=)(?<![0-9a-zA-Z_./-])([0-9]{6})[0-9]{5,9}(?![0-9a-zA-Z_@-])"
        ),
        r"\1***",
    ),
    # El almacen de Signal nombra sus ficheros POR EL TELEFONO del contacto:
    # ``session-573243116421.0.json``. Salen en los avisos de archivado de una
    # sesion revocada, que listan lo que no se pudo mover.
    #
    # El patron de arriba no llega: el guion de ``session-`` es frontera por
    # la izquierda, y tiene que serlo para no comerse las fechas de
    # ``session-20260911185133-revoked``. Asi que este caso va aparte.
    # El `(?!20)` separa un telefono de una FECHA. Las carpetas de sesion
    # archivada se llaman ``session-20260911185133-revoked``, y esos catorce
    # digitos tambien entran en el patron. Tapandolos se pierde justo lo que
    # sirve para encontrar la carpeta. Ningun indicativo de pais empieza por
    # 20; los anos de este siglo, todos.
    (re.compile(r"\bsession-(?!20)([0-9]{6})[0-9]{5,9}"), r"session-\1***"),
)


class SecretRedactionFilter(logging.Filter):
    """Redacta secretos evidentes del mensaje ya formateado."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - un log roto no debe tumbar la app
            return True

        redacted = message
        for pattern, replacement in _REDACTIONS:
            redacted = pattern.sub(replacement, redacted)

        if redacted != message:
            # Se reemplaza el mensaje ya interpolado y se descartan los args
            # para que el formatter no vuelva a interpolar el original.
            record.msg = redacted
            record.args = ()
        return True


class TaggedFormatter(logging.Formatter):
    """Antepone ``[TAG]`` usando el sufijo del nombre del logger.

    ``app.db`` -> ``[DB]``; los loggers de terceros (``pywhats.pairing``)
    conservan su nombre completo para no disfrazar su origen.
    """

    def __init__(self, *, show_time: bool) -> None:
        fmt = "%(asctime)s %(tagged)s %(message)s" if show_time else "%(tagged)s %(message)s"
        super().__init__(fmt=fmt)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """``2026-09-02 21:39:18.245 -05:00``: fecha, milisegundos y desfase.

        Con la hora a secas no se podian cruzar los tiempos del telefono, de
        WhatsApp Web, del backend y de PostgreSQL. Hacen falta las tres cosas:

        * la FECHA, porque una sesion cruza la medianoche;
        * los MILISEGUNDOS, porque los mensajes de una rafaga caen en el mismo
          segundo y el orden importa;
        * el DESFASE, porque las capturas del telefono son hora local y los
          timestamps de WhatsApp son UTC.

        Ojo con no confundir dos cosas distintas: esta es la hora a la que se
        ESCRIBE la linea, no la hora del mensaje de WhatsApp.
        """
        from datetime import datetime, timezone

        momento = datetime.fromtimestamp(record.created, timezone.utc).astimezone()
        if datefmt:
            return momento.strftime(datefmt)
        desfase = momento.strftime("%z")  # +HHMM
        desfase = f"{desfase[:3]}:{desfase[3:]}" if desfase else ""
        return f"{momento.strftime('%Y-%m-%d %H:%M:%S')}.{momento.microsecond // 1000:03d} {desfase}"

    def format(self, record: logging.LogRecord) -> str:
        name = record.name
        if name.startswith("app."):
            tag = name.split(".", 1)[1].split(".")[0].upper()
            record.tagged = f"[{tag}]"
        elif name == "app":
            record.tagged = "[APP]"
        else:
            record.tagged = f"[{name}]"

        if record.levelno >= logging.WARNING:
            record.tagged = f"{record.tagged} {record.levelname}:"
        return super().format(record)


def setup_logging(
    level: str = "INFO",
    *,
    log_file: Path | None = None,
    show_time: bool = True,
    quiet_libraries: bool = True,
) -> None:
    """Configura el logging raiz. Idempotente: reemplaza los handlers previos.

    :param level: nivel para los loggers de la aplicacion.
    :param log_file: si se indica, ademas escribe a ese archivo (siempre DEBUG).
    :param show_time: incluir hora en la salida de consola. Por defecto SI:
        sin ella no se puede cruzar lo que hizo el backend con lo que se ve en
        el telefono, y eso ha costado varias rondas de diagnostico.
    :param quiet_libraries: subir a WARNING los loggers ruidosos de terceros.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(logging.DEBUG)  # el filtrado real ocurre por handler
    redaction = SecretRedactionFilter()

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(getattr(logging, level, logging.INFO))
    console.setFormatter(TaggedFormatter(show_time=show_time))
    console.addFilter(redaction)
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        # EL LOG ROTA. Antes era un `FileHandler` a secas, en DEBUG y sin
        # tope: crecia mientras el servicio estuviera vivo. Se midio en 106 MB
        # --el fichero mas grande del proyecto despues del entorno virtual--
        # y con el una consulta tan simple como "que paso hace un rato" habia
        # que hacerla sobre cien megas de texto.
        #
        # DOS TANDAS DE 5 MB, no cinco de 20.
        #
        # El tope anterior eran 120 MB, y se alcanzaron: 107 MB en `app.log.1`
        # mas 13 MB en `app.log`. Un log no es solo espacio -- este lleva
        # identificadores de conversaciones y, hasta el filtro de arriba,
        # telefonos en claro. Cuanto mas historial se guarda, mas hay que
        # proteger y mas se arrastra al copiar el proyecto.
        #
        # Diez megas cubren de sobra lo unico que se consulta de verdad, que
        # son las ultimas horas. Lo anterior no se ha mirado nunca.
        #
        # `delay` evita crear el fichero hasta que haya algo que escribir.
        from logging.handlers import RotatingFileHandler

        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=1,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        file_handler.addFilter(redaction)
        root.addHandler(file_handler)

    if quiet_libraries:
        # SQLAlchemy emite el SQL completo en INFO; websockets es muy verboso.
        for noisy in ("sqlalchemy.engine", "websockets", "websockets.client", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        # pywhats emite un WARNING por cada host de CDN que falla. Con
        # cientos de adjuntos antiguos (404/410) eso son miles de lineas que
        # no aportan nada: el resultado real ya se resume por lotes.
        logging.getLogger("pywhats.media.download").setLevel(logging.ERROR)


def get_logger(tag: str) -> logging.Logger:
    """Devuelve el logger de un subsistema. ``get_logger("DB")`` -> ``[DB]``."""
    normalized = tag.strip().upper()
    if normalized not in TAGS:
        raise ValueError(f"etiqueta de log desconocida: {tag!r}. Conocidas: {TAGS}")
    return logging.getLogger(f"app.{normalized.lower()}")


# ---------------------------------------------------------------------------
# Ruido de terceros
# ---------------------------------------------------------------------------


class _SoloPeticionesInteresantes(logging.Filter):
    """Deja pasar los fallos HTTP y calla el resto.

    Werkzeug escribe una linea por peticion. Con el panel abierto eso son
    decenas por minuto —multimedia, estado, SSE— y entierran lo unico que hay
    que ver: la conexion, la sincronizacion y los errores.

    Los 4xx y 5xx SI pasan: un fallo silencioso es peor que ruido.
    """

    #: Codigos que no dicen nada cuando todo va bien.
    NORMALES = (" 200 ", " 204 ", " 206 ", " 301 ", " 302 ", " 304 ")

    def filter(self, record: logging.LogRecord) -> bool:
        mensaje = record.getMessage()
        if any(codigo in mensaje for codigo in self.NORMALES):
            return False
        return True


def silenciar_ruido_http(activo: bool = True) -> None:
    """Quita del INFO las peticiones HTTP correctas.

    Con ``HTTP_ACCESS_LOG=true`` se vuelven a ver todas: sirve para
    diagnosticar el frontend, y entonces el ruido es justo lo que se busca.
    """
    registro = logging.getLogger("werkzeug")
    for filtro in list(registro.filters):
        if isinstance(filtro, _SoloPeticionesInteresantes):
            registro.removeFilter(filtro)
    if activo:
        registro.addFilter(_SoloPeticionesInteresantes())


class RateLimitedLogger:
    """Agrupa avisos repetidos en vez de escribir cien iguales.

    Cien fallos identicos de Signal no son cien problemas: son uno que ocurre
    cien veces. Escribirlos todos entierra los demas y no anade informacion.

    El PRIMERO se ve siempre —hay que saber que pasa— y despues se cuenta y se
    resume cada tanto. Avisos DISTINTOS nunca se agrupan entre si.
    """

    def __init__(self, logger: logging.Logger, ventana: float = 60.0) -> None:
        self._log = logger
        self._ventana = ventana
        self._contador: dict[str, int] = {}
        self._ultimo: dict[str, float] = {}

    def info(self, clave: str, mensaje: str, *args: object) -> None:
        """Igual que :meth:`warning`, pero para lo que no es un problema.

        Hay avisos repetidos que describen algo NORMAL --el primer mensaje de
        cada interlocutor no se puede descifrar hasta que exista sesion
        Signal-- y pintarlos de WARNING hace que un arranque sano parezca una
        averia. Se agrupan igual; solo cambia el nivel.
        """
        self._emitir(self._log.info, clave, mensaje, *args)

    def warning(self, clave: str, mensaje: str, *args: object) -> None:
        self._emitir(self._log.warning, clave, mensaje, *args)

    def _emitir(self, escribir, clave: str, mensaje: str, *args: object) -> None:
        import time

        ahora = time.monotonic()
        visto = self._ultimo.get(clave)

        if visto is None:
            self._ultimo[clave] = ahora
            self._contador[clave] = 0
            escribir(mensaje, *args)
            return

        self._contador[clave] = self._contador.get(clave, 0) + 1
        if ahora - visto >= self._ventana:
            repetidos = self._contador[clave]
            self._ultimo[clave] = ahora
            self._contador[clave] = 0
            if repetidos:
                escribir(
                    "%s (y %d mas en los ultimos %.0f s)",
                    mensaje % args if args else mensaje,
                    repetidos,
                    self._ventana,
                )

    def flush(self) -> None:
        """Publica lo acumulado. Para el cierre o un resumen a peticion."""
        for clave, repetidos in list(self._contador.items()):
            if repetidos:
                self._log.warning("%s: %d repeticiones", clave, repetidos)
            self._contador[clave] = 0
