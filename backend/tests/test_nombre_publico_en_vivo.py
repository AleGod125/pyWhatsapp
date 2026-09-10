"""El nombre de quien escribe llega en el mensaje, y se estaba tirando.

POR QUE ESTO ES LA UNICA FUENTE QUE FUNCIONA
--------------------------------------------
Medido sobre la instalacion real del usuario, con 327 conversaciones recien
extraidas:

* de 674 mensajes de historial mirados, **CERO** traian ``pushName``. WhatsApp
  no manda el nombre en el History Sync;
* la agenda del bootstrap llego con **8 contactos** para 327 conversaciones, y
  ninguno con LID;
* **317 de las 327** conversaciones se identifican por ``@lid``, y en esta
  version de Baileys no hay consulta inversa de LID a telefono: ``onWhatsApp``
  va de telefono a LID, no al reves.

Con eso, el panel solo podia ensenar "Desconocido (lid)". El dato que faltaba
venia en cada mensaje en vivo --``pushName``-- y ``live_service`` no lo leia.

QUE NO ES
---------
No es el nombre de la agenda del usuario: es como se ha puesto esa persona en
SU WhatsApp. ``display_name_for`` ya lo coloca DESPUES del nombre guardado,
asi que nunca pisa uno mejor.
"""

from __future__ import annotations

import uuid

import pytest

from app.models import Chat, Contact


class _MensajeEnVivo:
    """Lo minimo que mira `_anotar_nombre_publico`."""

    def __init__(self, push_name=None, from_me=False):
        self.push_name = push_name
        self.from_me = from_me


@pytest.fixture
def servicio(session, cuenta):
    """El servicio con lo justo para ejercitar la anotacion del nombre."""
    from app.services.live_service import LiveMessageService

    objeto = object.__new__(LiveMessageService)
    objeto.whatsapp_account_id = cuenta.id
    return objeto


def _contacto(session, cuenta, jid):
    return (
        session.query(Contact)
        .filter(Contact.jid == jid, Contact.whatsapp_account_id == cuenta.id)
        .one_or_none()
    )


# ---------------------------------------------------------------------------
# Se guarda
# ---------------------------------------------------------------------------


def test_el_nombre_de_quien_escribe_se_guarda(session, cuenta, servicio):
    """El fallo que este cambio cierra: el dato llegaba y se tiraba."""
    jid = f"57{uuid.uuid4().hex[:9]}@s.whatsapp.net"

    servicio._anotar_nombre_publico(session, _MensajeEnVivo("Ana"), jid, None)
    session.flush()

    assert _contacto(session, cuenta, jid).push_name == "Ana"


def test_tambien_cuando_solo_hay_LID(session, cuenta, servicio):
    """Es EL caso que importa: 317 de 327 conversaciones son `@lid`.

    Sin telefono no hay agenda que consultar, asi que el LID es la unica clave
    con la que se puede guardar y volver a encontrar el nombre.
    """
    lid = f"{uuid.uuid4().int % 10**15}@lid"

    servicio._anotar_nombre_publico(session, _MensajeEnVivo("Marco"), None, lid)
    session.flush()

    fila = _contacto(session, cuenta, lid)
    assert fila is not None and fila.push_name == "Marco"
    assert fila.lid == lid


def test_el_panel_deja_de_ensenar_el_identificador(session, cuenta, servicio):
    """De punta a punta: es para esto que existe el cambio."""
    from app.services import repository as repo

    lid = f"{uuid.uuid4().int % 10**15}@lid"
    chat_id = repo.upsert_chat(session, jid=lid, whatsapp_account_id=cuenta.id)
    session.flush()

    antes = repo.chat_summary(session, chat_id).display_name
    assert "Desconocido" in antes

    servicio._anotar_nombre_publico(session, _MensajeEnVivo("Isaac"), None, lid)
    session.flush()
    session.expire_all()

    assert repo.chat_summary(session, chat_id).display_name == "Isaac"


# ---------------------------------------------------------------------------
# Lo que NO se guarda
# ---------------------------------------------------------------------------


def test_lo_que_escribe_UNO_MISMO_no_se_anota(session, cuenta, servicio):
    """El `pushName` de un mensaje propio es el nombre de uno.

    Meterlo como contacto llenaria la agenda con la propia cuenta, y en el
    chat consigo mismo dejaria al usuario hablando con su propio nombre.
    """
    jid = f"57{uuid.uuid4().hex[:9]}@s.whatsapp.net"

    servicio._anotar_nombre_publico(
        session, _MensajeEnVivo("Yo mismo", from_me=True), jid, None
    )
    session.flush()

    assert _contacto(session, cuenta, jid) is None


def test_sin_nombre_no_se_crea_un_contacto_vacio(session, cuenta, servicio):
    jid = f"57{uuid.uuid4().hex[:9]}@s.whatsapp.net"

    servicio._anotar_nombre_publico(session, _MensajeEnVivo(None), jid, None)
    servicio._anotar_nombre_publico(session, _MensajeEnVivo("   "), jid, None)
    session.flush()

    assert _contacto(session, cuenta, jid) is None


def test_sin_remitente_no_se_inventa_una_clave(session, cuenta, servicio):
    servicio._anotar_nombre_publico(session, _MensajeEnVivo("Ana"), None, None)
    session.flush()

    assert (
        session.query(Contact)
        .filter(Contact.whatsapp_account_id == cuenta.id)
        .count()
        == 0
    )


def test_un_nombre_guardado_NO_se_pisa(session, cuenta, servicio):
    """El `push_name` va detras del nombre de la agenda, nunca por delante.

    Es como se ha puesto esa persona en su WhatsApp; el nombre guardado es
    como la conoce el usuario, y ese manda.
    """
    from app.services import repository as repo

    jid = f"57{uuid.uuid4().hex[:9]}@s.whatsapp.net"
    repo.upsert_contact(
        session,
        whatsapp_account_id=cuenta.id,
        jid=jid,
        display_name="Ana (trabajo)",
    )
    chat_id = repo.upsert_chat(session, jid=jid, whatsapp_account_id=cuenta.id)
    session.flush()

    servicio._anotar_nombre_publico(session, _MensajeEnVivo("anita93"), jid, None)
    session.flush()
    session.expire_all()

    assert repo.chat_summary(session, chat_id).display_name == "Ana (trabajo)"


def test_anotar_el_nombre_no_puede_perder_el_mensaje(session, cuenta, servicio):
    """Un fallo guardando un nombre no puede tumbar la recepcion.

    El mensaje es el dato; el nombre es un adorno util. Si el orden se
    invirtiera, un contacto raro haria perder conversaciones.
    """

    class _SesionRota:
        def execute(self, *a, **k):
            raise RuntimeError("la base dijo que no")

    servicio._anotar_nombre_publico(
        _SesionRota(), _MensajeEnVivo("Ana"), "1@s.whatsapp.net", None
    )
