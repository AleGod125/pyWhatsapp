"""Al usuario no se le cambia de cuenta por debajo.

LO QUE PASABA
-------------
Se vinculaba el segundo telefono, se elegia en el selector, y setenta y dos
segundos despues el sistema volvia solo al primero::

    12:50:09  [API] cuenta activa cambiada: cuenta=553cb783   <- el usuario
    12:51:22  [API] la cuenta activa no era presentable; se pasa a 31198759

Nadie lo pidio. El usuario creia seguir en WA2 mientras miraba WA1 -- y por
eso durante horas parecio que los chats estaban mezclados: no lo estaban, se
estaba mirando otra cuenta.

DE DONDE SALIA
--------------
`is_active` decia cual se estaba mirando pero no DE DONDE SALIO. Sin esa
distincion, la unica regla posible era "si la activa no aparece en el listado,
cambia", y esa regla no puede distinguir:

* una cuenta recien vinculada, que todavia no ha traido ni un chat y por eso
  no pasaba el filtro de "tiene algo que ensenar";
* de una cuenta que de verdad dejo de servir.

LA REGLA AHORA
--------------
La cuenta activa solo cambia si:

1. la elige una persona en el selector;
2. la que estaba se cerro DE VERDAD (``revoked`` o ``error``);
3. no habia ninguna.

Nada mas. Ni la reconciliacion, ni el health-check, ni el listado.

Y cuando el sistema la cambia por el caso 2, lo DICE
(``switched_automatically`` en la respuesta) para que el frontend pueda
avisar en vez de cambiar en silencio.
"""

from __future__ import annotations

import uuid

import pytest

from app.models import Chat, WhatsAppAccount


def _otra_cuenta(session, user_id, *, estado="linked"):
    ident = uuid.uuid4()
    fila = WhatsAppAccount(
        id=ident,
        user_id=user_id,
        session_status=estado,
        session_storage_key=f"accounts/{ident}",
    )
    session.add(fila)
    session.flush()
    return fila


def _con_un_chat(session, cuenta_id):
    session.add(
        Chat(
            jid=f"57{uuid.uuid4().hex[:9]}@s.whatsapp.net",
            chat_type="individual",
            whatsapp_account_id=cuenta_id,
        )
    )
    session.flush()


# ---------------------------------------------------------------------------
# 1. Una cuenta recien vinculada y vacia NO expulsa al usuario
# ---------------------------------------------------------------------------


def test_una_cuenta_elegida_y_vacia_se_respeta(cliente, session, cuenta_del_cliente):
    """El caso exacto: WA2 recien vinculada, aun sin un solo chat.

    Antes, "no tiene nada que ensenar" bastaba para sacarte de ella.
    """
    vieja = cuenta_del_cliente
    _con_un_chat(session, vieja.id)

    nueva = _otra_cuenta(session, vieja.user_id, estado="never_linked")
    cliente.post(f"/api/v1/accounts/{nueva.id}/activate")

    cuerpo = cliente.get("/api/v1/accounts").get_json()

    assert cuerpo["active_id"] == str(nueva.id), (
        "el sistema saco al usuario de la cuenta que acababa de elegir"
    )
    assert str(nueva.id) in {c["id"] for c in cuerpo["accounts"]}, (
        "y ademas no la enseña, asi que no habria forma de volver"
    )
    assert "switched_automatically" not in cuerpo


def test_pedir_el_listado_muchas_veces_no_la_mueve(
    cliente, session, cuenta_del_cliente
):
    """El frontend consulta esto cada pocos segundos. Era el disparador."""
    vieja = cuenta_del_cliente
    _con_un_chat(session, vieja.id)
    nueva = _otra_cuenta(session, vieja.user_id, estado="never_linked")
    cliente.post(f"/api/v1/accounts/{nueva.id}/activate")

    for _ in range(5):
        cuerpo = cliente.get("/api/v1/accounts").get_json()

    assert cuerpo["active_id"] == str(nueva.id)


# ---------------------------------------------------------------------------
# 2. Una cuenta CERRADA de verdad si se sustituye, y se dice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cerrada", ["revoked", "error"])
def test_si_la_activa_se_cerro_se_cambia_y_se_avisa(
    cliente, session, cuenta_del_cliente, cerrada
):
    """WhatsApp cerro esa vinculacion: dejar al usuario ahi es dejarlo sin salida."""
    vieja = cuenta_del_cliente
    _con_un_chat(session, vieja.id)
    nueva = _otra_cuenta(session, vieja.user_id)
    cliente.post(f"/api/v1/accounts/{nueva.id}/activate")

    fila = session.get(WhatsAppAccount, nueva.id)
    fila.session_status = cerrada
    session.flush()

    cuerpo = cliente.get("/api/v1/accounts").get_json()

    assert cuerpo["active_id"] == str(vieja.id)
    assert cuerpo["switched_automatically"]["motivo"] == cerrada, (
        "se cambio en silencio: el usuario no sabe por que esta en otra cuenta"
    )


# ---------------------------------------------------------------------------
# 3. La eleccion queda escrita, y sobrevive
# ---------------------------------------------------------------------------


def test_la_eleccion_se_marca_como_del_usuario(cliente, session, cuenta_del_cliente):
    """`is_active` no basta: hay que saber si la eligio alguien."""
    from app.models.accounts import UserWhatsAppMembership

    nueva = _otra_cuenta(session, cuenta_del_cliente.user_id)
    cliente.post(f"/api/v1/accounts/{nueva.id}/activate")

    session.expire_all()
    fila = (
        session.query(UserWhatsAppMembership)
        .filter(UserWhatsAppMembership.whatsapp_account_id == nueva.id)
        .one()
    )

    assert fila.is_active is True
    assert fila.elegida_por_el_usuario is True


def test_lo_que_pone_el_sistema_NO_cuenta_como_eleccion(session, cuenta):
    """Un valor por defecto se puede sustituir; una eleccion no."""
    from app.auth.memberships import activar_cuenta
    from app.models.accounts import UserWhatsAppMembership

    activar_cuenta(session, user_id=cuenta.user_id, account_id=cuenta.id)
    session.flush()

    fila = (
        session.query(UserWhatsAppMembership)
        .filter(UserWhatsAppMembership.whatsapp_account_id == cuenta.id)
        .one()
    )
    assert fila.is_active is True
    assert fila.elegida_por_el_usuario is False


def test_el_sistema_no_borra_una_eleccion_anterior(session, cuenta):
    """Si el sistema vuelve a poner la misma, no la degrada a "por defecto"."""
    from app.auth.memberships import activar_cuenta
    from app.models.accounts import UserWhatsAppMembership

    activar_cuenta(
        session, user_id=cuenta.user_id, account_id=cuenta.id, por_el_usuario=True
    )
    activar_cuenta(session, user_id=cuenta.user_id, account_id=cuenta.id)
    session.flush()

    fila = (
        session.query(UserWhatsAppMembership)
        .filter(UserWhatsAppMembership.whatsapp_account_id == cuenta.id)
        .one()
    )
    assert fila.elegida_por_el_usuario is True


# ---------------------------------------------------------------------------
# 4. Nadie mas escribe la cuenta activa
# ---------------------------------------------------------------------------


def test_solo_tres_sitios_cambian_la_cuenta_activa():
    """La regla, comprobada sobre el codigo y no sobre la intencion.

    `activar_cuenta` solo se puede llamar desde: el endpoint del selector, el
    listado --cuando la activa se cerro o no habia ninguna-- y la vinculacion
    de una cuenta nueva. Ni reconciliacion, ni health-check, ni backfill.
    """
    import pathlib

    raiz = pathlib.Path(__file__).resolve().parents[1] / "app"
    permitidos = {
        "api/accounts_routes.py",
        "auth/memberships.py",
        "core/runtime.py",
    }

    culpables = set()
    for fichero in raiz.rglob("*.py"):
        texto = fichero.read_text(encoding="utf-8")
        for linea in texto.splitlines():
            limpia = linea.strip()
            if limpia.startswith("#") or "activar_cuenta(" not in limpia:
                continue
            if "def activar_cuenta(" in limpia:
                continue
            relativa = fichero.relative_to(raiz).as_posix()
            if relativa not in permitidos:
                culpables.add(relativa)

    assert not culpables, (
        f"estos ficheros cambian la cuenta activa y no deberian: {culpables}"
    )


def test_la_reconciliacion_no_la_toca():
    """Concreto, porque es de donde se sospechaba.

    LEER cual es la activa si esta permitido, y hace falta: el barrido de
    cuentas huerfanas la consulta para NO borrarla. Lo que no puede hacer este
    modulo es ESCRIBIRLA.
    """
    import inspect

    from app.auth import atribucion

    fuente = inspect.getsource(atribucion)

    assert "activar_cuenta" not in fuente, "la reconciliacion cambia la activa"
    for escritura in ("is_active =", "is_active=", "values(is_active"):
        assert escritura not in fuente, (
            f"la reconciliacion ESCRIBE la cuenta activa ({escritura})"
        )


# ---------------------------------------------------------------------------
# 5. Vincular va a la cuenta que se PIDE, no a la que estabas mirando
# ---------------------------------------------------------------------------
#
# Esta es la causa raiz del emparejamiento multicuenta, y explica las
# "identidades invertidas" que se vieron durante horas.
#
# `/session/pair` ignoraba `?account_id=` a proposito: venia de cuando habia
# una sola cuenta por usuario, y entonces "la activa" era la respuesta
# correcta. Con varias:
#
#   1. "+ Agregar cuenta" crea la fila B (`never_linked`) y NO la activa
#      --deliberado: activarla dejaria al usuario mirando una lista vacia
#      mientras escanea--;
#   2. el frontend llama a `/session/pair?account_id=B`;
#   3. se ignoraba y se cogia la ACTIVA, o sea A;
#   4. el QR era el del runtime de A;
#   5. al escanear el segundo telefono, sus credenciales caian en la carpeta
#      de A, y esa fila cambiaba de dueno.


def test_el_resolutor_admite_elegir_cuenta():
    """`pedida` tiene que llegar hasta el resolutor por los dos saltos.

    `routes.runtime_de_mi_cuenta` es un envoltorio del de `account_runtime`, y
    el parametro se perdia ahi: el envoltorio no lo declaraba. Con eso, pasarlo
    desde la ruta no servia de nada y todo el emparejamiento seguia yendo a la
    cuenta activa.
    """
    import inspect

    from app.api.account_runtime import runtime_de_mi_cuenta as resolutor
    from app.api.routes import runtime_de_mi_cuenta as envoltorio

    assert "pedida" in inspect.signature(envoltorio).parameters
    assert "pedida" in inspect.signature(resolutor).parameters
    assert "pedida=pedida" in inspect.getsource(envoltorio)


def test_una_cuenta_AJENA_no_se_admite_aunque_se_pida(session, cuenta):
    """El identificador llega del navegador: no se cree por si solo."""
    import uuid as _uuid

    from app.auth.ownership import cuenta_visible_de

    ajena = _uuid.uuid4()
    visible = cuenta_visible_de(session, cuenta.user_id, str(ajena))

    assert visible is None or str(visible) != str(ajena), (
        "cambiar el parametro en la URL daria acceso a la cuenta de otro"
    )


def test_la_cuenta_y_el_runtime_se_resuelven_IGUAL():
    """El fallo no era que `rt` estuviera mal: era que `cuenta` no coincidia.

    `runtime_de_mi_cuenta` ya honraba `?account_id=`. Pero `cuenta` salia de
    `asegurar_cuenta()` --la ACTIVA-- y abajo se hace
    `rt.iniciar_vinculacion(usuario, cuenta.id)`: el runtime de la cuenta nueva
    quedaba marcado con el id de la vieja, y el pair-success sellaba la vieja.

    Los dos tienen que salir del MISMO resolutor o vuelven a divergir.
    """
    import inspect

    from app.api import routes

    fuente = inspect.getsource(routes.session_pair)

    assert "cuenta_del_usuario_actual(crear=True, pedida=pedida)" in fuente
    assert "_asegurar_cuenta_de_whatsapp" not in fuente, (
        "volvio el resolutor que devuelve la cuenta ACTIVA en vez de la pedida"
    )
    # Y que la que se pasa a `iniciar_vinculacion` sea esa, no otra.
    assert "iniciar_vinculacion(usuario_actual().id, cuenta.id)" in fuente


def test_una_cuenta_que_no_es_tuya_da_404():
    """El identificador llega del navegador y se comprueba antes de nada."""
    import inspect

    from app.api import routes

    fuente = inspect.getsource(routes.session_pair)
    tras_resolver = fuente[fuente.index("cuenta_del_usuario_actual") :]

    assert "ACCOUNT_NOT_FOUND" in tras_resolver.split("rt =")[0], (
        "sin cuenta se sigue adelante y se revienta en `cuenta.id`"
    )


def test_la_ruta_de_emparejamiento_mira_el_parametro():
    """La comprobacion sobre el codigo: es una linea facil de perder.

    Sin `pedida`, `runtime_de_mi_cuenta` devuelve el runtime de la cuenta
    ACTIVA, y todo el emparejamiento se va a la cuenta equivocada.
    """
    import inspect

    from app.api import routes

    fuente = inspect.getsource(routes.session_pair)

    assert 'request.args.get("account_id")' in fuente
    assert "pedida=pedida" in fuente


def _cookies(cliente) -> str:
    return "; ".join(
        f"{c.key}={c.value}" for c in getattr(cliente, "_cookies", {}).values()
    ) if hasattr(cliente, "_cookies") else ""
