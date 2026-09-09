"""Un runtime por cuenta de WhatsApp. Nunca uno global.

EL FALLO QUE CIERRA
-------------------
Habia UN runtime en todo el proceso, y cada endpoint leia su estado. Con la
cuenta de A conectada, el usuario B --recien registrado, sin cuenta ninguna--
llegaba al emparejamiento y se encontraba::

    /session/pair   -> 409 SESSION_ALREADY_CONNECTED   (el estado de A)
    /session/qr     -> el codigo QR de A

Es decir: o no podia vincular, o escaneaba un codigo que no era suyo.

LA REGLA
--------
El runtime se busca por ``whatsapp_account_id``, jamas por usuario y jamas
"el que haya". Dos personas que comparten una cuenta comparten runtime --es la
misma sesion de WhatsApp, y duplicarla seria duplicar el Signal Store--; dos
cuentas distintas no comparten absolutamente nada.

QUE NO SE COMPARTE
------------------
Cliente de WhatsApp, Signal Store, identidad de dispositivo, estado de
emparejamiento, codigo QR, cerrojos y carpeta de sesion. Cada runtime recibe
unos ``Settings`` que apuntan a ``session/accounts/<id>/``, y con eso el
aislamiento de disco sale solo.

QUE SI SE COMPARTE
------------------
La base de datos y la configuracion general. Son infraestructura, no estado de
sesion, y duplicarlas solo gastaria conexiones.

EL FALLO DE UNA NO TUMBA A LA OTRA
----------------------------------
Si el runtime de B revienta al arrancar, el de A sigue en pie. Se anota y se
sigue: un proceso que se cae entero porque una cuenta tuvo un problema deja
sin servicio a todas las demas.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from app.core.logging_setup import get_logger

log = get_logger("APP")


class RuntimeRegistry:
    """Los runtimes vivos, uno por cuenta de WhatsApp.

    La clave es ``whatsapp_account_id`` y no ``user_id`` a proposito: varias
    personas pueden tener acceso a la MISMA cuenta, y darle un runtime a cada
    una significaria dos clientes de WhatsApp y dos Signal Stores para una
    sola identidad.
    """

    def __init__(self, settings: Any, database: Any, *, fabrica: Callable | None = None):
        self._settings = settings
        self._database = database
        self._fabrica = fabrica or _crear_runtime
        self._runtimes: dict[str, Any] = {}
        # Un cerrojo por cuenta, mas uno para el propio diccionario. El global
        # solo protege el mapa: si protegiera tambien el arranque, levantar la
        # cuenta de B dejaria bloqueada la de A mientras tanto.
        self._candado = threading.RLock()
        self._candados: dict[str, threading.RLock] = {}

    # -- Consulta ------------------------------------------------------------

    def get(self, account_id: Any) -> Any | None:
        """El runtime de esa cuenta si ya esta levantado. No lo arranca."""
        if account_id is None:
            return None
        with self._candado:
            return self._runtimes.get(str(account_id))

    def all(self) -> dict[str, Any]:
        """Copia de los runtimes vivos. Para diagnostico y apagado."""
        with self._candado:
            return dict(self._runtimes)

    def candado_de(self, account_id: Any) -> threading.RLock:
        """El cerrojo de ESA cuenta. Nunca uno global.

        Con un cerrojo global, emparejar la cuenta de B dejaria esperando a
        todo lo que quisiera hacer la de A.
        """
        clave = str(account_id)
        with self._candado:
            if clave not in self._candados:
                self._candados[clave] = threading.RLock()
            return self._candados[clave]

    def adoptar(self, account_id: Any, runtime: Any) -> Any:
        """Registra un runtime YA construido como el de esa cuenta.

        POR QUE HACE FALTA
        ------------------
        Cuando esto se estreno ya habia un runtime en marcha: el de la cuenta
        que existia, con su sesion abierta, su cerrojo tomado y su conexion
        viva. Construirle otro apuntando a la carpeta nueva significaria dos
        clientes sobre la misma identidad, y el segundo no podria ni abrir el
        Signal Store que el primero tiene cogido.

        Asi que el registro lo ADOPTA en vez de rehacerlo: la cuenta que ya
        funcionaba sigue exactamente igual, y las nuevas nacen con su carpeta
        propia. Nadie tiene que volver a escanear nada.
        """
        if account_id is None or runtime is None:
            return None
        clave = str(account_id)
        with self._candado:
            self._runtimes[clave] = runtime
        log.info("[APP] runtime existente adoptado para la cuenta %s", clave[:8])
        return runtime

    # -- Ciclo de vida -------------------------------------------------------

    def get_or_start(self, account_id: Any) -> Any | None:
        """El runtime de esa cuenta, arrancandolo si hace falta.

        Idempotente y a prueba de concurrencia: dos peticiones simultaneas del
        mismo usuario --un refresco, un doble clic-- obtienen el MISMO runtime,
        no dos clientes de WhatsApp peleandose por la misma identidad.
        """
        if account_id is None:
            return None
        clave = str(account_id)

        vivo = self.get(clave)
        if vivo is not None:
            return vivo

        with self.candado_de(clave):
            # Segunda comprobacion DENTRO del cerrojo: otra peticion pudo
            # haberlo levantado mientras esperabamos.
            vivo = self.get(clave)
            if vivo is not None:
                return vivo
            try:
                # Los ajustes por cuenta se calculan AQUI, no en la fabrica.
                #
                # Si el aislamiento dependiera de que cada fabrica se acuerde
                # de apuntar a su carpeta, bastaria una que no lo hiciera para
                # que dos cuentas compartieran identidad. Asi es estructural.
                from app.core.session_paths import ajustes_de_cuenta

                propios = ajustes_de_cuenta(self._settings, account_id)
                nuevo = self._fabrica(propios, self._database, account_id)
            except Exception:  # noqa: BLE001 - una cuenta no tumba a las demas
                log.exception(
                    "[APP] no se pudo levantar el runtime de la cuenta %s; "
                    "las demas siguen en pie",
                    str(account_id)[:8],
                )
                return None
            with self._candado:
                self._runtimes[clave] = nuevo
        log.info("[APP] runtime levantado para la cuenta %s", str(account_id)[:8])
        return nuevo

    def levantar_las_vinculadas(self) -> list[Any]:
        """Un runtime por cada cuenta de WhatsApp ya vinculada.

        Es el arranque multicuenta: al abrir el servicio, cada cuenta que
        tenga sesion guardada recupera la suya. Antes se levantaba UNA y las
        demas no existian hasta que alguien entrara.

        **Nunca dos runtimes sobre la misma cuenta**: se usa `get_or_start`,
        que devuelve el que ya hubiera. Dos clientes sobre una identidad se
        pelean por el Signal Store y el segundo no puede ni abrirlo.

        Si una cuenta falla al levantarse se anota y se sigue con las demas.
        """
        from sqlalchemy import select

        from app.models import WhatsAppAccount
        from app.models.accounts import LINKED_STATUSES

        if self._database is None:
            return []
        try:
            with self._database.transaction() as sesion:
                cuentas = (
                    sesion.execute(
                        select(WhatsAppAccount.id).where(
                            WhatsAppAccount.session_status.in_(tuple(LINKED_STATUSES))
                        )
                    )
                    .scalars()
                    .all()
                )
        except Exception:  # noqa: BLE001 - no poder listarlas no tumba el arranque
            log.exception("[APP] no se pudieron listar las cuentas vinculadas")
            return []

        levantados = []
        for cuenta in cuentas:
            rt = self.get_or_start(cuenta)
            if rt is not None:
                levantados.append(rt)
        if cuentas:
            log.info(
                "[APP] arranque multicuenta: %d de %d cuenta(s) vinculada(s) "
                "con runtime propio",
                len(levantados),
                len(cuentas),
            )
        return levantados

    def stop(self, account_id: Any) -> bool:
        """Detiene el runtime de esa cuenta. Las demas no se enteran."""
        clave = str(account_id)
        with self._candado:
            runtime = self._runtimes.pop(clave, None)
        if runtime is None:
            return False
        _parar(runtime, clave)
        return True

    def stop_all(self) -> int:
        """Para todos. Uno que falle no impide parar el resto."""
        with self._candado:
            vivos = list(self._runtimes.items())
            self._runtimes.clear()
        for clave, runtime in vivos:
            _parar(runtime, clave)
        return len(vivos)


def _parar(runtime: Any, clave: str) -> None:
    try:
        parar = getattr(runtime, "stop", None)
        if callable(parar):
            parar()
    except Exception:  # noqa: BLE001 - parar mal no puede propagarse
        log.exception("[APP] fallo deteniendo el runtime de la cuenta %s", clave[:8])


def _usuario_de(database: Any, account_id: Any) -> Any:
    """El usuario dueno de esa cuenta, o ``None`` si no se puede saber.

    Ante la duda, ``None``: dejar un runtime sin dueno cuesta que no atribuya
    lo que guarde, y eso se ve y se arregla. Adjudicarselo a quien no es
    entrega la copia de una persona a otra, y eso no se ve.
    """
    if database is None:
        return None
    try:
        from sqlalchemy import select

        from app.models import WhatsAppAccount

        with database.transaction() as sesion:
            return sesion.execute(
                select(WhatsAppAccount.user_id).where(
                    WhatsAppAccount.id == account_id
                )
            ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - no poder leerlo no impide levantarlo
        log.debug("[APP] no se pudo resolver el usuario de la cuenta")
        return None


def _crear_runtime(settings: Any, database: Any, account_id: Any) -> Any:
    """Un ``AppRuntime`` ARRANCADO, con la carpeta de sesion que le da el registro.

    No hace falta un runtime distinto: el de siempre toma su carpeta de
    ``settings.session_dir``, y el registro ya se la ha puesto apuntando a
    ``session/accounts/<id>/``. Con eso queda aislado entero -- identidad,
    Signal Store y registro de establecimientos.

    ARRANCARLO ES PARTE DE CREARLO
    ------------------------------
    Antes esto devolvia el objeto sin llamar a ``start()``, y un runtime sin
    arrancar no tiene ``client``. Se midio lo que provocaba:

    * al vincular, ``iniciar_vinculacion`` no encontraba cliente que arrancar,
      asi que no se generaba ningun QR y la pantalla se quedaba en "Codigo no
      disponible" para siempre;
    * al abrir el servicio, las cuentas vinculadas que NO fueran la adoptada
      se creaban como objetos y nunca se conectaban a WhatsApp.

    Solo parecia funcionar porque la unica cuenta era la del runtime adoptado,
    que arranca ``service.py``.

    El cerrojo es por carpeta (``session/accounts/<id>/runtime.lock``), asi
    que dos cuentas no se lo disputan.
    """
    from app.core.runtime import AppRuntime

    # `settings` ya viene apuntando a `session/accounts/<id>/`: lo resuelve el
    # registro, para que el aislamiento no dependa de esta funcion.
    runtime = AppRuntime(settings, owner=f"account:{account_id}", configure_logging=False)
    runtime.database = database
    runtime.runtime_owner_account_id = account_id

    # Y de que USUARIO es. No es un adorno: el trabajador de almacenamiento,
    # la sincronizacion y el companion web resuelven a quien pertenece lo que
    # guardan mirando `runtime_owner_user_id`. Sin el, un runtime levantado
    # por el registro conecta, descarga y no puede atribuir nada -- y al
    # conectarse escribe "Sesion conectada sin dueno registrado" en vez de
    # marcar la cuenta como vinculada.
    #
    # Sale de la fila de la cuenta, nunca del navegador ni de "el unico
    # usuario que haya".
    runtime.runtime_owner_user_id = _usuario_de(database, account_id)

    # `start()` vuelve en cuanto la conexion esta LANZADA, no establecida, asi
    # que esto no espera a WhatsApp. Si falla, el runtime se devuelve igual:
    # sin cliente no hay QR, pero con la excepcion suelta no habria ni cuenta,
    # y el usuario no veria mas que un 503 sin explicacion.
    try:
        runtime.start(connect=True)
    except Exception:  # noqa: BLE001 - una cuenta que no arranca no tumba a las demas
        log.exception(
            "[APP] la cuenta %s no pudo abrir su sesion; queda sin cliente",
            str(account_id)[:8],
        )
    return runtime
