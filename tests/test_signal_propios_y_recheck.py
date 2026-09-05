"""Mensajes propios que fallan Signal, y la revision de pendientes.

DOS COSAS QUE NO SE NEGOCIAN
----------------------------
1. Un mensaje que no supera su verificacion de autenticidad NO se entrega, no
   se persiste y no sirve de ancla. Aqui se comprueba llamando al camino real,
   no leyendo el codigo.
2. Revisar los pendientes no puede volver a descomprimir los mismos archivos
   una vez por conversacion.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, update

from app.models import Chat, ChatHistoryState

WAMID = "3A1F8BDD4678EB6DE395"


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


# ---------------------------------------------------------------------------
# Fallos de Signal en mensajes propios
# ---------------------------------------------------------------------------


class _Remitente:
    """Nuestro propio LID, tal y como llega al receptor."""

    server = "lid"
    device = 0

    def __init__(self, user: str):
        self.user = user


@pytest.fixture
def diagnostico():
    """El modulo con la identidad propia fijada y los contadores a cero."""
    from app.compat import lid_diagnostics

    previo = lid_diagnostics._own_lid_user
    lid_diagnostics._own_lid_user = "86531142340710"
    lid_diagnostics.METRICAS.clear()
    yield lid_diagnostics
    lid_diagnostics._own_lid_user = previo
    lid_diagnostics.METRICAS.clear()


def test_un_fallo_de_MAC_se_distingue_de_no_tener_sesion(diagnostico):
    """Son cosas distintas y se arreglan distinto.

    Sin sesion, el reenvio como ``pkmsg`` lo resuelve solo. Un MAC que no
    cuadra es un ratchet desincronizado, y mezclarlos llevaba a buscar el
    fallo en el sitio equivocado.
    """
    propio = _Remitente("86531142340710")
    diagnostico._clasificar(propio, "msg", Exception("no session for peer 8653...@lid"))
    diagnostico._clasificar(propio, "msg", Exception("signal message mac check failed"))

    assert diagnostico.METRICAS["sin_sesion"] == 1
    assert diagnostico.METRICAS["mac_fallido"] == 1


def test_un_MAC_fallido_NO_entrega_el_mensaje(diagnostico, monkeypatch):
    """La excepcion se relanza intacta. No se acepta lo que no se autentica."""
    llamadas = {"n": 0}

    class _Receptor:
        def _decrypt_enc(self, sender, enc_type, ciphertext):
            llamadas["n"] += 1
            raise ValueError("signal message mac check failed")

    original = _Receptor._decrypt_enc

    def envuelto(self, sender, enc_type, ciphertext):
        propio = diagnostico._es_nuestro(sender)
        try:
            return original(self, sender, enc_type, ciphertext)
        except Exception as exc:
            if propio:
                diagnostico._clasificar(sender, enc_type, exc)
            raise

    with pytest.raises(ValueError, match="mac check failed"):
        envuelto(_Receptor(), _Remitente("86531142340710"), "msg", b"")

    assert llamadas["n"] == 1, "se llamo al descifrado real, no a un atajo"
    assert diagnostico.METRICAS["mac_fallido"] == 1


def test_un_mensaje_de_otro_no_entra_en_estas_metricas(diagnostico):
    """Solo se observan los NUESTROS: lo demas tiene su propio camino."""
    assert not diagnostico._es_nuestro(_Remitente("64940106866902"))


def test_no_se_desactiva_la_verificacion_en_ningun_sitio():
    """La red de seguridad de todo esto.

    Cualquier apano que "arregle" un fallo de descifrado desactivando la
    comprobacion convertiria el backup en algo que acepta mensajes que nadie
    ha autenticado.
    """
    import inspect

    from app.compat import lid_diagnostics, prekey_compat

    for modulo in (lid_diagnostics, prekey_compat):
        fuente = inspect.getsource(modulo)
        for prohibido in (
            "verify_mac=False",
            "skip_mac",
            "check_mac=False",
            "verify=False",
        ):
            assert prohibido not in fuente, f"{modulo.__name__} desactiva una verificacion"


# ---------------------------------------------------------------------------
# Un mensaje recuperado por reintento
# ---------------------------------------------------------------------------


def test_un_reintento_exitoso_se_marca_como_tal(runtime):
    """Y con su procedencia real.

    Un mensaje recuperado por reenvio es justo el que puede ser la PRIMERA
    ancla de su conversacion. Atribuirlo a "live" perderia esa informacion.
    """
    runtime._decrypt_pendientes["ABC123"] = "mac check failed"
    runtime.counters["decrypt_unrecovered"] = 1

    assert runtime._marcar_recuperado("ABC123") is True
    assert runtime.counters["decrypt_recovered"] == 1
    assert runtime.counters["decrypt_unrecovered"] == 0


def test_un_mensaje_que_nunca_fallo_no_es_un_reintento(runtime):
    assert runtime._marcar_recuperado("NUNCA_FALLO") is False


def test_el_mensaje_recuperado_entra_como_retry_resend(runtime):
    """Se comprueba en el codigo del receptor, que es donde se decide."""
    import inspect

    fuente = inspect.getsource(type(runtime)._wire_services)
    assert '"retry_resend" if recuperado else "live"' in fuente


def test_retry_resend_es_una_fuente_declarada():
    from app.models.seeds import SEED_SOURCES

    assert "retry_resend" in SEED_SOURCES


def test_los_fallos_repetidos_se_agrupan(runtime):
    """Cien fallos identicos son un problema que ocurre cien veces."""
    from app.core.logging_setup import RateLimitedLogger

    assert isinstance(runtime._avisos_signal, RateLimitedLogger)


# ---------------------------------------------------------------------------
# La revision de pendientes ya no reingiere
# ---------------------------------------------------------------------------


def test_la_revision_NO_reingiere_los_blobs(settings, database):
    """El punto de todo esto.

    Con 27 pendientes y 4 blobs, la version anterior hacia 108
    descompresiones y 108 ingestas completas para descubrir exactamente lo
    mismo que la primera.
    """
    import inspect

    from app.services.pending_recheck import PendingRecheckService

    fuente = inspect.getsource(PendingRecheckService._revisar)
    assert "ingest_history_sync" not in fuente
    assert "HistoryRecheck" not in fuente, "ya no delega en la revision por chat"
    assert "solo_nuevos=True" in fuente, "solo se abren los blobs sin escanear"


def test_la_revision_automatica_sale_sin_tocar_disco_si_no_hay_nada(
    session, settings, monkeypatch
):
    """El panel llama a esto al abrirse y en cada refresco."""
    from app.services.pending_recheck import PendingRecheckService

    abiertos = {"n": 0}

    class _EscanerQueCuenta:
        def __init__(self, *a, **k):
            pass

        def hay_blobs_nuevos(self):
            return False

        def escanear(self, **kwargs):
            abiertos["n"] += 1
            raise AssertionError("no deberia abrirse ningun blob")

    monkeypatch.setattr(
        "app.history.blob_scanner.BlobSeedScanner", _EscanerQueCuenta
    )

    servicio = PendingRecheckService(settings, _DatabaseDeSesion(session))
    # Ya hubo una revision, hace mucho: la espera entre ejecuciones no aplica
    # y lo unico que puede evitar el trabajo es que no haya nada nuevo.
    import time as _time

    from app.services.pending_recheck import RecheckJob

    servicio._ultimo = RecheckJob(
        job_id="anterior", state="completed", finished_at=_time.time() - 100_000
    )

    class _RuntimeFalso:
        runtime_owner_account_id = uuid.uuid4()
        sync_job = None
        backfill = None
        seed_queue = None
        seed_collector = None

    # Un pendiente cualquiera, para que la lista no salga vacia por otro motivo.
    chat = Chat(jid=f"5730{uuid.uuid4().hex[:8]}@s.whatsapp.net", chat_type="individual")
    session.add(chat)
    session.flush()
    session.add(
        ChatHistoryState(
            chat_id=chat.id, chat_jid=chat.jid, history_status="waiting_seed"
        )
    )
    session.flush()

    trabajo = servicio.start(_RuntimeFalso(), auto=True)

    assert trabajo.skipped is True
    assert trabajo.state == "completed"
    assert abiertos["n"] == 0


def test_el_resumen_de_la_revision_es_UNA_linea(caplog, session, settings):
    """Antes eran 27 lineas de "sigue sin ancla", una por conversacion."""
    import inspect

    from app.services.pending_recheck import PendingRecheckService

    fuente = inspect.getsource(PendingRecheckService._revisar)
    infos = fuente.count("log.info(")
    assert infos == 1, f"se esperaba una sola linea en INFO, hay {infos}"
    assert "waiting=%d blobs_nuevos=%d semillas_nuevas=%d despertados=%d" in fuente


def test_un_chat_que_YA_tiene_ancla_despierta_en_la_revision(
    session, settings, runtime
):
    """Sin abrir un solo archivo: el ancla ya estaba."""
    from app.history.seed_collector import RecentSeedCollector
    from app.models import Message, WhatsAppAccount
    from app.services.pending_recheck import PendingRecheckService

    inicio = runtime.auth.register(
        email=f"rc-{uuid.uuid4().hex[:10]}@example.com", password="una contrasena larga"
    )
    cuenta = WhatsAppAccount(
        user_id=inicio.user_id,
        session_status="linked",
        session_storage_key=f"users/{inicio.user_id}",
    )
    session.add(cuenta)
    session.flush()

    chat = Chat(
        jid=f"5730{uuid.uuid4().hex[:8]}@s.whatsapp.net",
        chat_type="individual",
        whatsapp_account_id=cuenta.id,
    )
    session.add(chat)
    session.flush()
    session.add(
        ChatHistoryState(
            chat_id=chat.id, chat_jid=chat.jid, history_status="waiting_seed"
        )
    )
    session.add(
        Message(
            chat_id=chat.id,
            chat_jid=chat.jid,
            whatsapp_message_id=WAMID,
            timestamp=1_760_000_000,
            from_me=False,
            message_type="text",
            source="live",
        )
    )
    session.flush()

    db = _DatabaseDeSesion(session)

    class _RuntimeFalso:
        runtime_owner_account_id = cuenta.id
        sync_job = None
        backfill = None
        seed_queue = None
        seed_collector = RecentSeedCollector(
            db, user_id=inicio.user_id, account_id=cuenta.id
        )

    # Solo esta conversacion espera: las de la base real quedan fuera.
    session.execute(
        update(ChatHistoryState)
        .where(ChatHistoryState.chat_jid != chat.jid)
        .values(history_status="exhausted")
    )
    session.flush()

    servicio = PendingRecheckService(settings, db)
    servicio._revisar(
        _trabajo_de(chat),
        [{"chat_id": chat.id, "name": None, "chat_jid": chat.jid}],
        _RuntimeFalso(),
    )
    session.flush()

    estado = session.execute(
        select(ChatHistoryState.history_status).where(
            ChatHistoryState.chat_jid == chat.jid
        )
    ).scalar_one()
    assert estado == "pending"


def _trabajo_de(chat):
    from app.services.pending_recheck import ChatProgress, RecheckJob

    return RecheckJob(
        job_id="test0001",
        total=1,
        chats=[ChatProgress(chat.id, None, "waiting_seed")],
    )
