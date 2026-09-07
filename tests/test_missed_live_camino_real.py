"""El camino del borde reciente, EJECUTADO. No simulado.

EL FALLO QUE ESTO CIERRA
------------------------
En una prueba real, al intentar cerrar los agujeros de tres conversaciones::

    missed_live_filler.py:112
      -> BackfillService.rellenar_borde_reciente()
        -> _wamids_de()
           NameError: name 'Message' is not defined

    [LIVE] bordes por mensajes perdidos:
           3 conversaciones, 0 rellenadas, 0 mensajes recuperados

POR QUE NO LO VIO NADIE
-----------------------
El bloque del borde reciente --``_wamids_de``, ``_marca_mas_antigua`` y el que
elige el ancla-- **nunca habia tenido un llamante en produccion**. Su logica de
decision si estaba probada, pero por separado y sin tocar la base. Las
consultas no se habian ejecutado ni una vez, asi que un import que faltaba
desde el primer dia pudo quedarse ahi.

La leccion no es "faltaba un import": es que se probo la decision y no el
camino. Por eso estas pruebas **ejecutan las consultas de verdad** contra la
base, sin sustituir ninguna de las tres funciones.

LO QUE PROTEGEN
---------------
Que las tres consultas del borde reciente se puedan ejecutar. Con el import
quitado, todas fallan con ``NameError``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import Chat, Message


class _DatabaseDeSesion:
    def __init__(self, session):
        self._session = session

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


AHORA = 1_788_700_000


@pytest.fixture
def conversacion(session, cuenta):
    """Una conversacion con mensajes REALES, con su identificador de WhatsApp."""
    jid = f"{uuid.uuid4().int % 10**15}@lid"
    chat = Chat(jid=jid, chat_type="individual", name="Contacto", whatsapp_account_id=cuenta.id)
    session.add(chat)
    session.flush()
    wamids = []
    for n in range(3):
        wamid = uuid.uuid4().hex[:20].upper()
        wamids.append(wamid)
        session.add(
            Message(
                chat_id=chat.id,
                chat_jid=jid,
                whatsapp_message_id=wamid,
                timestamp=AHORA - (n * 600),
                from_me=False,
                message_type="text",
                source="live",
            )
        )
    session.flush()
    return {"session": session, "chat": chat, "jid": jid, "wamids": wamids}


def _motor(conversacion, settings):
    from app.services.backfill_service import BackfillService

    return BackfillService(settings, _DatabaseDeSesion(conversacion["session"]))


# ---------------------------------------------------------------------------
# Las tres consultas que nunca se habian ejecutado
# ---------------------------------------------------------------------------


def test_LOS_WAMIDS_YA_GUARDADOS_SE_PUEDEN_CONSULTAR(conversacion, settings):
    """LA PRUEBA QUE FALTABA. Con el import quitado, esto es un NameError."""
    motor = _motor(conversacion, settings)

    conocidos = motor._wamids_de(conversacion["jid"])

    assert conocidos == set(conversacion["wamids"])


def test_la_marca_mas_antigua_se_puede_consultar(conversacion, settings):
    motor = _motor(conversacion, settings)

    marca = motor._marca_mas_antigua(conversacion["wamids"])

    assert marca == AHORA - 1200


def test_de_una_conversacion_vacia_no_se_conoce_ningun_wamid(conversacion, settings):
    motor = _motor(conversacion, settings)
    assert motor._wamids_de("nadie@lid") == set()


def test_sin_wamids_la_marca_es_nula(conversacion, settings):
    motor = _motor(conversacion, settings)
    assert motor._marca_mas_antigua([]) is None


# ---------------------------------------------------------------------------
# El ancla del borde, tambien de verdad
# ---------------------------------------------------------------------------


def test_el_filler_encuentra_el_ancla_MAS_NUEVA(conversacion, settings):
    """Se baja desde el mensaje mas nuevo: es donde empieza el agujero."""
    from app.services.missed_live_filler import MissedLiveFiller

    filler = MissedLiveFiller(
        _DatabaseDeSesion(conversacion["session"]), _motor(conversacion, settings)
    )

    ancla = filler._ancla_del_borde(conversacion["jid"])

    assert ancla is not None
    assert ancla.timestamp == AHORA
    assert ancla.wa_msg_id == conversacion["wamids"][0]
    assert ancla.source == "missed_live"
    assert ancla.chat_id == conversacion["chat"].id


def test_sin_conversacion_no_hay_ancla(conversacion, settings):
    from app.services.missed_live_filler import MissedLiveFiller

    filler = MissedLiveFiller(
        _DatabaseDeSesion(conversacion["session"]), _motor(conversacion, settings)
    )
    assert filler._ancla_del_borde("no-existe@lid") is None


def test_UNA_CONVERSACION_SIN_MENSAJES_REALES_NO_INVENTA_ANCLA(session, cuenta, settings):
    """Sin una referencia real no se pide nada. No se fabrica ninguna."""
    from app.services.missed_live_filler import MissedLiveFiller

    jid = f"{uuid.uuid4().int % 10**15}@lid"
    chat = Chat(jid=jid, chat_type="individual", whatsapp_account_id=cuenta.id)
    session.add(chat)
    session.flush()
    # Un mensaje SIN identificador de WhatsApp: no sirve como ancla.
    session.add(
        Message(
            chat_id=chat.id,
            chat_jid=jid,
            whatsapp_message_id=None,
            timestamp=AHORA,
            from_me=False,
            message_type="text",
            source="live",
        )
    )
    session.flush()

    from app.services.backfill_service import BackfillService

    filler = MissedLiveFiller(
        _DatabaseDeSesion(session), BackfillService(settings, _DatabaseDeSesion(session))
    )
    assert filler._ancla_del_borde(jid) is None


# ---------------------------------------------------------------------------
# El ciclo entero, con el motor en duda y sin el
# ---------------------------------------------------------------------------


def test_con_el_motor_en_duda_NO_se_pide_nada(conversacion, settings, monkeypatch):
    """La seguridad no se ablanda porque sepamos que falta un mensaje."""
    import asyncio

    from app.services.missed_live import AgujerosEnVivo
    from app.services.missed_live_filler import MissedLiveFiller

    motor = _motor(conversacion, settings)
    monkeypatch.setattr(motor, "capability_state", lambda: "SUSPECT")
    pedidos = []

    async def _no_deberia(*a, **k):
        pedidos.append(1)

    monkeypatch.setattr(motor, "rellenar_borde_reciente", _no_deberia)

    registro = AgujerosEnVivo()
    registro.anotar(
        conversacion["jid"], "huella-1", "unknown one-time pre-key id 17"
    )
    filler = MissedLiveFiller(
        _DatabaseDeSesion(conversacion["session"]), motor, registro=registro
    )

    resumen = asyncio.run(filler.cerrar_pendientes(client=object()))

    assert pedidos == []
    assert resumen["rellenados"] == 0


def test_con_el_motor_confirmado_SI_se_pide_y_se_cuenta(
    conversacion, settings, monkeypatch
):
    """El camino completo: agujero -> ancla real -> peticion -> recuento."""
    import asyncio

    from app.services.missed_live import AgujerosEnVivo
    from app.services.missed_live_filler import MissedLiveFiller

    motor = _motor(conversacion, settings)
    monkeypatch.setattr(motor, "capability_state", lambda: "CONFIRMED")
    anclas = []

    async def _rellenar(client, ancla, *, db_mas_nuevo):
        # Se comprueba que llega el ancla REAL, no una inventada.
        anclas.append(ancla)
        return type("R", (), {"mensajes": 4})()

    monkeypatch.setattr(motor, "rellenar_borde_reciente", _rellenar)

    registro = AgujerosEnVivo()
    registro.anotar(
        conversacion["jid"], "huella-1", "unknown one-time pre-key id 17"
    )
    filler = MissedLiveFiller(
        _DatabaseDeSesion(conversacion["session"]), motor, registro=registro
    )

    resumen = asyncio.run(filler.cerrar_pendientes(client=object()))

    assert len(anclas) == 1
    assert anclas[0].wa_msg_id == conversacion["wamids"][0]
    assert resumen == {"chats": 1, "rellenados": 1, "mensajes": 4, "omitidos": 0}
    # Trajo mensajes: el agujero se da por cerrado.
    assert registro.pendientes() == []


def test_si_no_trae_nada_el_agujero_SIGUE_anotado(
    conversacion, settings, monkeypatch
):
    """Perder la anotacion sin haber recuperado nada seria darlo por hecho."""
    import asyncio

    from app.services.missed_live import AgujerosEnVivo
    from app.services.missed_live_filler import MissedLiveFiller

    motor = _motor(conversacion, settings)
    monkeypatch.setattr(motor, "capability_state", lambda: "CONFIRMED")

    async def _sin_nada(client, ancla, *, db_mas_nuevo):
        return type("R", (), {"mensajes": 0})()

    monkeypatch.setattr(motor, "rellenar_borde_reciente", _sin_nada)

    registro = AgujerosEnVivo()
    registro.anotar(conversacion["jid"], "h", "unknown one-time pre-key id 17")
    filler = MissedLiveFiller(
        _DatabaseDeSesion(conversacion["session"]), motor, registro=registro
    )

    asyncio.run(filler.cerrar_pendientes(client=object()))

    assert len(registro.pendientes()) == 1


def test_ES_IDEMPOTENTE_Y_DEJA_DE_INSISTIR(conversacion, settings, monkeypatch):
    """Se puede llamar muchas veces; no pide para siempre."""
    import asyncio

    from app.services.missed_live import AgujerosEnVivo
    from app.services.missed_live_filler import MAXIMO_DE_INTENTOS, MissedLiveFiller

    motor = _motor(conversacion, settings)
    monkeypatch.setattr(motor, "capability_state", lambda: "CONFIRMED")
    veces = []

    async def _sin_nada(client, ancla, *, db_mas_nuevo):
        veces.append(1)
        return type("R", (), {"mensajes": 0})()

    monkeypatch.setattr(motor, "rellenar_borde_reciente", _sin_nada)

    registro = AgujerosEnVivo()
    registro.anotar(conversacion["jid"], "h", "unknown one-time pre-key id 17")
    filler = MissedLiveFiller(
        _DatabaseDeSesion(conversacion["session"]), motor, registro=registro
    )

    for _ in range(MAXIMO_DE_INTENTOS + 5):
        asyncio.run(filler.cerrar_pendientes(client=object()))

    assert len(veces) == MAXIMO_DE_INTENTOS


def test_UN_CHAT_QUE_FALLA_NO_PARA_A_LOS_DEMAS(conversacion, settings, monkeypatch):
    """Una conversacion problematica no puede tumbar la pasada entera."""
    import asyncio

    from app.services.missed_live import AgujerosEnVivo
    from app.services.missed_live_filler import MissedLiveFiller

    motor = _motor(conversacion, settings)
    monkeypatch.setattr(motor, "capability_state", lambda: "CONFIRMED")

    async def _revienta(client, ancla, *, db_mas_nuevo):
        raise RuntimeError("esta conversacion da problemas")

    monkeypatch.setattr(motor, "rellenar_borde_reciente", _revienta)

    registro = AgujerosEnVivo()
    registro.anotar(conversacion["jid"], "h", "unknown one-time pre-key id 17")
    filler = MissedLiveFiller(
        _DatabaseDeSesion(conversacion["session"]), motor, registro=registro
    )

    resumen = asyncio.run(filler.cerrar_pendientes(client=object()))

    assert resumen["chats"] == 1
    assert resumen["rellenados"] == 0


# ---------------------------------------------------------------------------
# El guardia contra la regresion
# ---------------------------------------------------------------------------


def test_el_modelo_esta_importado_donde_se_usa():
    """Guardia directo: las consultas del borde necesitan `Message` en scope.

    Es la comprobacion que habria convertido tres horas de rastreo en un fallo
    de un segundo.
    """
    import app.services.backfill_service as modulo

    assert "Message" in vars(modulo), (
        "backfill_service usa Message en las consultas del borde reciente"
    )


# ---------------------------------------------------------------------------
# PN / LID: la conversacion es la misma aunque el remitente llegue por el otro
# ---------------------------------------------------------------------------


def test_UN_REMITENTE_POR_TELEFONO_ENCUENTRA_SU_CHAT_POR_LID(session, cuenta, settings):
    """Medido sobre la base local, y fallaba en silencio.

    `573002389304@s.whatsapp.net` no tiene fila de chat propia; su
    conversacion esta guardada por LID. Sin canonicalizar, el agujero de ese
    contacto se anotaba y no hacia absolutamente nada: ni ancla, ni peticion,
    ni aviso.
    """
    from app.models import Contact
    from app.services.backfill_service import BackfillService
    from app.services.missed_live_filler import MissedLiveFiller

    sufijo = uuid.uuid4().int % 10**12
    telefono = f"57300{sufijo}@s.whatsapp.net"
    lid = f"{sufijo}@lid"

    # La conversacion existe SOLO por LID.
    chat = Chat(jid=lid, chat_type="individual", name="Mismo contacto", whatsapp_account_id=cuenta.id)
    session.add(chat)
    session.flush()
    session.add(
        Message(
            chat_id=chat.id,
            chat_jid=lid,
            whatsapp_message_id=uuid.uuid4().hex[:20].upper(),
            timestamp=AHORA,
            from_me=False,
            message_type="text",
            source="live",
        )
    )
    # Y el alias que los une, que es el que ya usa el colector de anclas.
    session.add(Contact(jid=telefono, lid=lid, whatsapp_account_id=cuenta.id))
    session.flush()

    filler = MissedLiveFiller(
        _DatabaseDeSesion(session),
        BackfillService(settings, _DatabaseDeSesion(session)),
    )

    ancla = filler._ancla_del_borde(telefono)

    assert ancla is not None, "el agujero del telefono tenia que encontrar su chat"
    assert ancla.chat_id == chat.id
    assert ancla.chat_jid == lid


def test_un_remitente_desconocido_no_inventa_conversacion(session, settings):
    """Si no hay chat ni alias, no se pide nada. Sin adivinar."""
    from app.services.backfill_service import BackfillService
    from app.services.missed_live_filler import MissedLiveFiller

    filler = MissedLiveFiller(
        _DatabaseDeSesion(session),
        BackfillService(settings, _DatabaseDeSesion(session)),
    )
    assert filler._ancla_del_borde(f"{uuid.uuid4().int % 10**14}@lid") is None

