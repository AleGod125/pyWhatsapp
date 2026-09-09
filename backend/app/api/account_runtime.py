"""De quien es el runtime que contesta a esta peticion. UN solo camino.

EL FALLO QUE CIERRA
-------------------
Las rutas hacian::

    rt = runtime()          # el UNICO del proceso

y con eso leian estado, emparejamiento y codigo QR. Con la cuenta de A
conectada, el usuario B --recien registrado-- se encontraba::

    POST /session/pair  -> 409 SESSION_ALREADY_CONNECTED   (el estado de A)
    GET  /session/qr    -> el codigo QR de A

No es que el frontend se equivocara: el backend le contestaba con la cuenta de
otro.

LA CADENA, Y NO OTRA
--------------------
::

    usuario autenticado  ->  membresia  ->  whatsapp_account  ->  runtime

Nunca "el runtime que haya". Nunca la unica cuenta que exista. Nunca un
identificador que venga del navegador: si el cliente pudiera decir de quien es
la cuenta, cambiarlo bastaria para apoderarse de la de otro.
"""

from __future__ import annotations

from typing import Any

from flask import current_app

from app.core.logging_setup import get_logger

log = get_logger("APP")


def registro() -> Any:
    """El registro de runtimes de este proceso, si lo hay."""
    return current_app.config.get("RUNTIME_REGISTRY")


def cuenta_del_usuario_actual(*, crear: bool = False) -> Any:
    """La cuenta de WhatsApp de quien hace esta peticion.

    :param crear: si no tiene ninguna, crearla. Solo para el emparejamiento:
        el resto de rutas debe encontrarla ya hecha, y si no la hay la
        respuesta correcta es "vincula", no "toma la de otro".
    """
    from app.api.routes import runtime as runtime_base
    from app.auth.web import usuario_actual

    yo = usuario_actual()
    if yo is None:
        return None

    rt = runtime_base()
    cuentas = getattr(rt, "whatsapp_accounts", None)
    if cuentas is None or rt.database is None:
        return None

    from app.auth.memberships import cuenta_efectiva_de

    with rt.database.transaction() as sesion:
        mia = cuenta_efectiva_de(sesion, yo.id)
        if mia is not None:
            return mia

    if not crear:
        return None
    # `asegurar_cuenta` toma el usuario de la cookie, nunca del navegador.
    return cuentas.asegurar_cuenta(yo.id)


def runtime_de_mi_cuenta(*, crear: bool = False) -> Any:
    """El runtime de la cuenta de quien hace esta peticion.

    Devuelve ``None`` cuando esa persona todavia no tiene cuenta -- y eso
    significa "hay que vincular", nunca "usa la de al lado".

    Si no hay registro montado se devuelve el runtime de siempre: durante la
    transicion hay procesos que aun no lo construyen, y ahi solo existe una
    cuenta, asi que la respuesta es la misma.
    """
    from app.api.routes import runtime as runtime_base

    cuenta = cuenta_del_usuario_actual(crear=crear)
    if cuenta is None:
        return None

    reg = registro()
    if reg is None:
        # Sin registro montado no hay runtimes por cuenta. Antes se devolvia
        # "el de siempre", y con dos cuentas eso es entregarle a alguien la
        # sesion de otro. Solo se devuelve si es SUYO.
        base = runtime_base()
        return base if _es_el_runtime_de(base, cuenta) else None

    # ¿Es esta la cuenta del runtime que YA esta corriendo?
    #
    # El proceso arranca con un runtime que tiene la sesion abierta y el
    # cerrojo tomado. Si es el de esta cuenta hay que adoptarlo, no construir
    # otro: dos clientes sobre la misma identidad se pelean por el Signal
    # Store, y el segundo ni siquiera puede abrirlo.
    if reg.get(cuenta.id) is None:
        base = runtime_base()
        if _es_el_runtime_de(base, cuenta):
            return reg.adoptar(cuenta.id, base)

    rt = reg.get_or_start(cuenta.id)
    if rt is None:
        # No se pudo levantar el suyo. Se contesta que no, NO el de otro:
        # devolver el runtime ajeno es exactamente el fallo que esto cierra.
        log.warning(
            "[APP] no hay runtime para la cuenta %s", str(cuenta.id)[:8]
        )
    return rt


def cuenta_del_runtime_base(base: Any) -> Any:
    """De que cuenta es el runtime que ya esta corriendo. ``None`` si no consta.

    Se usa en el arranque, ANTES de levantar las demas: sin saber cual es la
    suya, el arranque multicuenta le construiria un SEGUNDO runtime a la
    cuenta que ya esta funcionando -- dos clientes sobre el mismo Signal
    Store, y el segundo sin poder ni abrirlo.

    Tres vias, de mas fiable a menos, y ninguna adivina:

    1. el runtime lo declara;
    2. lo dice la carpeta, comparada con `session_storage_key`, que es donde
       la base guarda donde vive la sesion de cada cuenta;
    3. hay UNA sola cuenta, asi que no hay otro sitio de donde pueda ser.
    """
    # PRECONDICION: el runtime base solo puede reclamar una cuenta si su
    # carpeta CONTIENE de verdad una sesion.
    #
    # Este es el fallo que se midio. Tras migrar la sesion a
    # `session/accounts/<id>/`, el runtime base seguia construido con la
    # carpeta plana --vacia ya-- y se adoptaba igual. Resultado::
    #
    #     [APP] runtime existente adoptado para la cuenta 5d91a4f6
    #     [WA]  Sesion no encontrada en ...\session\device.json
    #
    # Adoptar un runtime que apunta a una carpeta sin identidad es peor que no
    # adoptar nada: la cuenta se queda atada a un sitio donde no esta su
    # sesion, y el registro ya no le construye la suya. Sin esta comprobacion
    # la cuenta pedia un codigo QR teniendo su identidad intacta en disco.
    from app.core.session_paths import carpeta_de_cuenta, hay_sesion_en

    carpeta_base = getattr(getattr(base, "settings", None), "session_dir", None)

    def _la_cuenta_ya_tiene_la_suya(cuenta_id: Any) -> bool:
        """Si esa cuenta guarda su identidad en su propia carpeta."""
        if carpeta_base is None:
            return False
        try:
            propia = carpeta_de_cuenta(
                type("_", (), {"session_dir": carpeta_base})(), cuenta_id
            )
        except Exception:  # noqa: BLE001
            return False
        return hay_sesion_en(propia)

    def _reclamable(cuenta_id: Any) -> Any:
        """La cuenta, salvo que su sesion viva en su propia carpeta.

        Si la cuenta ya tiene la suya, el runtime base NO puede reclamarla: lo
        que tenga que abrirla es un runtime apuntando ahi, no este.
        """
        if _la_cuenta_ya_tiene_la_suya(cuenta_id):
            return None
        return cuenta_id

    declarada = getattr(base, "runtime_owner_account_id", None)
    if declarada is not None:
        return _reclamable(declarada)

    base_db = getattr(base, "database", None)
    carpeta = carpeta_base
    if base_db is None:
        return None

    try:
        from sqlalchemy import select

        from app.models import WhatsAppAccount

        with base_db.transaction() as sesion:
            filas = sesion.execute(
                select(
                    WhatsAppAccount.id,
                    WhatsAppAccount.session_storage_key,
                    WhatsAppAccount.wa_pn,
                    WhatsAppAccount.wa_lid,
                )
            ).all()
    except Exception:  # noqa: BLE001 - ante la duda, no se atribuye
        return None

    if carpeta is not None:
        normalizada = str(carpeta).replace("\\", "/")
        for fila in filas:
            clave = fila[1]
            if clave and normalizada.endswith(str(clave).strip("/")):
                return _reclamable(fila[0])

        # LA IDENTIDAD DECIDE, Y NO DEPENDE DE CUANTAS CUENTAS HAYA.
        #
        # Sin esto quedaba un agujero con final feo: instalacion con la sesion
        # de A todavia suelta en `session/`, B vincula, y al siguiente
        # arranque hay DOS cuentas. Entonces ni la clave coincide ni vale el
        # "hay una sola", asi que no se atribuia a nadie, el registro le
        # construia a A un runtime apuntando a su carpeta --vacia-- y a A se
        # le pedia un codigo QR teniendo su identidad intacta en disco.
        #
        # `device.json` dice su PN y su LID, y la base guarda los de cada
        # cuenta. Si coincide con UNA, es esa. Eso es evidencia, no descarte.
        from pathlib import Path

        from app.core.session_paths import identidad_de

        pn, lid = identidad_de(Path(str(carpeta)))
        if pn or lid:
            coinciden = [
                fila
                for fila in filas
                if (pn and fila[2] == pn) or (lid and fila[3] == lid)
            ]
            if len(coinciden) == 1:
                return _reclamable(coinciden[0][0])

    return _reclamable(filas[0][0]) if len(filas) == 1 else None


def _es_el_runtime_de(base: Any, cuenta: Any) -> bool:
    """Si el runtime que ya corre pertenece a ESA cuenta.

    Dos formas de saberlo, y ninguna es adivinar:

    1. el runtime dice de quien es (`runtime_owner_account_id`);
    2. todavia no lo dice, y en toda la base hay UNA sola cuenta -- entonces
       no hay otro sitio de donde pueda ser. Es el caso de la transicion:
       la sesion suelta de `session/` es de la unica cuenta que existe.

    Con dos cuentas y un runtime sin dueno declarado se contesta que NO. Mejor
    levantar uno nuevo que arriesgarse a darle a alguien la sesion de otro.
    """
    suya = cuenta_del_runtime_base(base)
    return suya is not None and str(suya) == str(cuenta.id)


def montar_registro(app: Any, runtime: Any) -> Any:
    """Deja el registro en la aplicacion y le adopta el runtime que ya corre.

    El runtime que llega es el de la cuenta que ya existia, con su sesion
    abierta. Se adopta tal cual: rehacerlo apuntando a la carpeta nueva
    significaria dos clientes sobre la misma identidad, y el segundo no podria
    ni abrir el Signal Store que el primero tiene cogido.
    """
    from app.core.runtime_registry import RuntimeRegistry

    # Montar el registro no puede impedir que la aplicacion arranque: hay
    # entornos --diagnostico, modo local-- donde el runtime no trae ni base de
    # datos. Sin ella no hay membresias que resolver, y el resolutor devuelve
    # el runtime de siempre, que es lo correcto ahi.
    reg = RuntimeRegistry(
        getattr(runtime, "settings", None), getattr(runtime, "database", None)
    )

    # PRIMERO se adopta el que ya corre. El orden no es un detalle: si el
    # arranque multicuenta fuera antes, le construiria un SEGUNDO runtime a la
    # cuenta que ya esta funcionando.
    cuenta = cuenta_del_runtime_base(runtime)
    if cuenta is not None:
        reg.adoptar(cuenta, runtime)

    app.config["RUNTIME_REGISTRY"] = reg
    return reg


def levantar_las_demas(app: Any) -> list[Any]:
    """Levanta un runtime por cada cuenta vinculada que aun no lo tenga.

    Se llama DESPUES de montar el registro, cuando el runtime que ya corria
    esta adoptado. `get_or_start` devuelve el que hubiera, asi que la cuenta
    que ya funciona no se toca y solo nacen las que faltan.

    Nunca lanza: una cuenta que no arranque no puede impedir que el servicio
    atienda a las demas.
    """
    reg = app.config.get("RUNTIME_REGISTRY")
    if reg is None:
        return []
    try:
        return reg.levantar_las_vinculadas()
    except Exception:  # noqa: BLE001 - el arranque no se cae por esto
        log.exception("[APP] fallo levantando los runtimes de las cuentas")
        return []
