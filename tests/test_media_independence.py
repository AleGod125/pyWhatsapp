"""El mensaje no depende de su archivo, y un archivo roto no reextrae nada.

LA SEPARACION QUE SE FIJA AQUI
------------------------------
Guardar un mensaje y descargar su adjunto son dos cosas distintas, y la
primera no puede esperar a la segunda::

    receiver -> descifrar -> clasificar -> COMMIT -> SSE      (inmediato)
                                             |
                                             +-> cola de multimedia (despues)

Una nota de voz tiene que aparecer en la conversacion en cuanto llega, aunque
el audio tarde treinta segundos. Y si el archivo no se puede recuperar, el
mensaje sigue siendo parte de la copia: lo que falta es el adjunto, y eso se
dice, no se esconde.

Y AL REVES
----------
Una imagen rota no puede obligar a reextraer una conversacion entera. El
reintento es de UN archivo: ni backfill, ni ``ON_DEMAND``, ni sincronizacion.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import MediaFile
from tests.test_live_types import FakeDatabase, _fila
from tests.test_outgoing_routing import ISAAC, ISAAC_LID, OWN_LID, OWN_PN, entrante, envuelto, evento

from app.services.live_service import LiveMessageService


@pytest.fixture
def servicio(session):
    return LiveMessageService(FakeDatabase(session), own_jid=OWN_PN, own_lid=OWN_LID)


@pytest.fixture
def crudo(monkeypatch):
    import app.compat.protocol_flag as protocol_flag

    def poner(datos: bytes):
        monkeypatch.setattr(protocol_flag, "last_raw_message", lambda: datos)

    return poner


# ---------------------------------------------------------------------------
# El mensaje va primero
# ---------------------------------------------------------------------------


def test_el_mensaje_se_guarda_aunque_el_adjunto_no_se_registre(
    servicio, session, crudo, monkeypatch
):
    """Lo importante es el mensaje. El archivo va detras, y puede fallar."""
    import app.services.live_service as live_service

    monkeypatch.setattr(
        live_service.LiveMessageService,
        "_register_parsed_media",
        lambda *a, **k: False,
    )
    crudo(envuelto(ISAAC_LID, imagen=True))
    resultado = servicio.handle(evento(id="INDEP001"))
    session.flush()

    assert resultado is not None
    fila = _fila(session, "INDEP001")
    assert fila is not None, "el mensaje NO puede perderse porque falle el adjunto"
    assert fila.message_type == "image"
    assert fila.chat_jid == ISAAC_LID


def test_un_fallo_registrando_el_adjunto_no_tumba_la_recepcion(
    servicio, session, crudo, monkeypatch
):
    """Ni siquiera si el registro del adjunto lanza."""
    import app.services.live_service as live_service

    def explota(*a, **k):
        raise RuntimeError("fallo simulado del adjunto")

    monkeypatch.setattr(
        live_service.LiveMessageService, "_register_parsed_media", explota
    )
    crudo(envuelto(ISAAC_LID, imagen=True))

    # ``handle`` nunca lanza: el receptor es lo prioritario.
    assert servicio.handle(evento(id="INDEP002")) is None


def test_el_adjunto_nace_en_pending_no_bloquea(servicio, session, crudo):
    """El mensaje se sirve ya; el archivo lo baja el worker por su cuenta."""
    crudo(envuelto(ISAAC_LID, audio_ptt=True))
    servicio.handle(evento(id="INDEP003"))
    session.flush()

    adjunto = session.execute(
        select(MediaFile).where(MediaFile.whatsapp_message_id == "INDEP003")
    ).scalar_one()
    assert adjunto.download_status == "pending"
    assert adjunto.local_path is None


# ---------------------------------------------------------------------------
# La API no esconde un mensaje por su archivo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("estado", ["pending", "downloading", "failed", "unavailable", "expired"])
def test_un_mensaje_con_adjunto_no_descargado_se_sirve_igual(estado):
    """"No renderizado" y "no existe" son cosas distintas."""
    from app.api.serializers import media_to_json

    class _Adjunto:
        id = 7
        media_type = "image"
        mime_type = "image/jpeg"
        file_name = None
        file_size = 100
        duration_seconds = None
        width = None
        height = None
        download_status = estado

    cuerpo = media_to_json(_Adjunto())
    assert cuerpo["id"] == 7, "el adjunto sigue presente en la respuesta"
    assert cuerpo["status"] == estado
    assert cuerpo["available"] is False
    assert cuerpo["file_url"] is None


def test_un_adjunto_terminal_se_marca_como_tal():
    """El mensaje es parte de la copia; lo que falta es el archivo."""
    from app.api.serializers import media_to_json

    class _Adjunto:
        id = 8
        media_type = "video"
        mime_type = "video/mp4"
        file_name = None
        file_size = 1
        duration_seconds = None
        width = None
        height = None
        download_status = "unavailable"

    cuerpo = media_to_json(_Adjunto())
    assert cuerpo["permanently_unavailable"] is True
    assert cuerpo["status"] == "unavailable"


def test_los_seis_estados_existen_en_el_esquema():
    """Sin ellos no se puede distinguir "aun no" de "ya nunca"."""
    from app.models import DOWNLOAD_STATUSES

    assert set(DOWNLOAD_STATUSES) >= {
        "pending",
        "downloading",
        "downloaded",
        "unavailable",
        "expired",
        "failed",
    }


# ---------------------------------------------------------------------------
# El reintento es de UN archivo
# ---------------------------------------------------------------------------


def test_el_reintento_no_toca_el_backfill():
    """Una imagen rota no puede reextraer una conversacion entera.

    Se mira el ARBOL del endpoint: los nombres aparecen en la prosa que
    explica justamente que NO se llaman, y buscarlos como texto daria un
    falso positivo.
    """
    import ast
    import inspect
    import textwrap

    import app.api.routes as routes

    fuente = textwrap.dedent(inspect.getsource(routes.media_retry))
    arbol = ast.parse(fuente)
    llamadas = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call):
            nombre = getattr(nodo.func, "id", None) or getattr(nodo.func, "attr", None)
            if nombre:
                llamadas.append(nombre)

    for prohibido in (
        "run",
        "start",
        "_process_chat",
        "ingest_history_sync",
        "notify_history",
    ):
        assert prohibido not in llamadas, (
            f"el reintento de un adjunto no puede llamar a {prohibido}"
        )


def test_el_reintento_solo_encola_ese_archivo():
    """Un UPDATE, y con el ``id`` en el WHERE."""
    import ast
    import inspect
    import textwrap

    import app.api.routes as routes

    fuente = textwrap.dedent(inspect.getsource(routes.media_retry))
    assert "MediaFile.id == media_id" in fuente
    arbol = ast.parse(fuente)
    actualizaciones = [
        n
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) or getattr(n.func, "attr", None)) == "update"
    ]
    assert len(actualizaciones) == 1, "solo se toca una fila"
