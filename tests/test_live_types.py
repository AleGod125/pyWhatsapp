"""``_store`` atraviesa TODOS los tipos de mensaje sin reventar.

POR QUE EXISTE ESTE ARCHIVO
---------------------------
Habia un ``TypeError`` en el camino de todo mensaje en vivo::

    message_type in _MEDIA_KINDS.values() | {"voice_note", "gif"}

``dict_values`` no admite la union con un ``set``. Y lo grave no era el error
en si, sino donde estaba: DENTRO de la transaccion, y en la rama que se evalua
cuando pywhats NO trae adjunto, o sea en cada mensaje de texto. El
``except`` de ``handle()`` lo capturaba, la transaccion hacia rollback y el
mensaje se perdia dejando solo una traza en el log.

Las pruebas que ya habia no lo vieron por un motivo concreto: ninguna ponia
``raw_proto``, y el ``and`` cortaba antes de llegar a la expresion rota. Aqui
se pone SIEMPRE, que es lo que pasa de verdad desde que el receptor lo expone.

Estas pruebas NO tocan Signal, ni pairing, ni History Sync, ni ON_DEMAND, ni
los cursores, ni la deduplicacion.
"""

from __future__ import annotations

import pytest

from pywhats.events import JID, Message  # noqa: E402

from app.services import repository as repo  # noqa: E402
from app.services.live_service import LiveMessageService  # noqa: E402

CHAT = JID(user="34600888777", server="s.whatsapp.net")
CHAT_JID = "34600888777@s.whatsapp.net"
GRUPO = JID(user="120363111222333444", server="g.us")
GRUPO_JID = "120363111222333444@g.us"
PARTICIPANTE = JID(user="34600555444", server="s.whatsapp.net")


class FakeDatabase:
    """Reutiliza la sesion transaccional del test en vez de abrir otra."""

    def __init__(self, session):
        self._session = session

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


class MediaFalsa:
    """Adjunto tal y como lo entrega pywhats."""

    def __init__(self, kind: str, *, ptt: bool = False, mimetype: str = "") -> None:
        self.kind = kind
        self.ptt = ptt
        self.mimetype = mimetype
        self.caption = ""
        self.filename = ""
        self.file_length = 1024
        self.direct_path = "/v/t62.7118-24/abc"
        self.media_key = b"k" * 32
        self.file_sha256 = b"s" * 32
        self.file_enc_sha256 = b"e" * 32


def mensaje(**kwargs) -> Message:
    defaults = dict(
        id="3EB0LIVETYPES001",
        chat=CHAT,
        sender=CHAT,
        text="hola",
        timestamp=1_788_100_000,
        from_me=False,
        media=None,
        quoted=None,
    )
    defaults.update(kwargs)
    return Message(**defaults)


@pytest.fixture
def con_raw_proto(monkeypatch):
    """Hace que ``last_raw_message()`` devuelva bytes, como en produccion.

    Es la pieza que faltaba: sin ella el ``and`` corta antes de evaluar la
    expresion que estaba rota, y el fallo pasa desapercibido.
    """
    import app.compat.protocol_flag as protocol_flag

    # Un ``Message`` E2E minimo: campo 1 (conversation) con texto.
    crudo = bytes([0x0A, 0x04]) + b"hola"
    monkeypatch.setattr(protocol_flag, "last_raw_message", lambda: crudo)
    return crudo


@pytest.fixture
def servicio(session):
    return LiveMessageService(FakeDatabase(session), own_jid="34600999888@s.whatsapp.net")


def _fila(session, wamid: str):
    from sqlalchemy import select

    from app.models import Message as MessageRow

    return session.execute(
        select(MessageRow).where(MessageRow.whatsapp_message_id == wamid)
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Todos los tipos atraviesan _store
# ---------------------------------------------------------------------------


CASOS = [
    ("text", None, "texto suelto"),
    ("image", MediaFalsa("image", mimetype="image/jpeg"), ""),
    ("video", MediaFalsa("video", mimetype="video/mp4"), ""),
    ("audio", MediaFalsa("audio", mimetype="audio/ogg"), ""),
    ("voice_note", MediaFalsa("audio", ptt=True, mimetype="audio/ogg"), ""),
    ("sticker", MediaFalsa("sticker", mimetype="image/webp"), ""),
    ("document", MediaFalsa("document", mimetype="application/pdf"), ""),
    ("gif", MediaFalsa("video", mimetype="image/gif"), ""),
]


@pytest.mark.parametrize(
    "esperado,media,texto", CASOS, ids=[c[0] for c in CASOS]
)
def test_cada_tipo_atraviesa_store_sin_typeerror(
    servicio, session, con_raw_proto, esperado, media, texto
):
    """Ningun tipo puede reventar dentro de la transaccion.

    Se comprueba que la fila EXISTE, no solo que no hubo excepcion: el fallo
    original quedaba tragado por el ``except`` de ``handle()`` y lo unico
    observable era que el mensaje no aparecia.
    """
    wamid = f"3EBLIVE{esperado.upper()[:8]}"
    resultado = servicio.handle(
        mensaje(id=wamid, text=texto, media=media)
    )

    assert resultado is not None, f"{esperado}: handle() se trago un error"
    fila = _fila(session, wamid)
    assert fila is not None, f"{esperado}: la fila no llego a PostgreSQL"
    assert fila.chat_jid == CHAT_JID
    assert resultado["message_id"] == fila.id
    assert resultado["new"] is True


def test_un_mensaje_de_texto_se_guarda(servicio, session, con_raw_proto):
    """El caso que el bug rompia: texto normal, sin adjunto de pywhats."""
    resultado = servicio.handle(
        mensaje(id="3EBLIVETXT001", text="prueba live 001")
    )

    assert resultado is not None
    fila = _fila(session, "3EBLIVETXT001")
    assert fila is not None
    assert fila.text == "prueba live 001"
    assert fila.message_type == "text"
    assert fila.source == "live"


def test_un_mensaje_de_grupo_se_guarda(servicio, session, con_raw_proto):
    resultado = servicio.handle(
        mensaje(
            id="3EBLIVEGRP001",
            chat=GRUPO,
            sender=PARTICIPANTE,
            text="mensaje al grupo",
        )
    )

    assert resultado is not None
    fila = _fila(session, "3EBLIVEGRP001")
    assert fila is not None
    assert fila.chat_jid == GRUPO_JID
    assert fila.sender_jid == "34600555444@s.whatsapp.net"


def test_un_adjunto_de_tipo_desconocido_no_pierde_el_mensaje(
    servicio, session, con_raw_proto
):
    """Un ``kind`` que pywhats no traduce NO puede tirar la transaccion.

    El adjunto se registra como 'unknown', que SI es un valor valido de
    ``media_files.media_type``. Guardarlo con el tipo del mensaje violaria el
    CHECK de la columna y se perderia el mensaje entero, no solo el adjunto.
    """
    from sqlalchemy import select

    from app.models import MediaFile

    resultado = servicio.handle(
        mensaje(id="3EBLIVEUNK001", text="", media=MediaFalsa("marciano"))
    )

    assert resultado is not None
    fila = _fila(session, "3EBLIVEUNK001")
    assert fila is not None

    adjunto = session.execute(
        select(MediaFile).where(MediaFile.message_id == fila.id)
    ).scalar_one_or_none()
    assert adjunto is not None, "el adjunto tenia que registrarse igualmente"
    assert adjunto.media_type == "unknown"


def test_un_evento_de_sistema_no_rompe_el_pipeline(servicio, session, con_raw_proto):
    """Un mensaje sin texto ni adjunto se guarda; no se descarta."""
    resultado = servicio.handle(mensaje(id="3EBLIVESYS001", text=""))

    assert resultado is not None
    assert _fila(session, "3EBLIVESYS001") is not None


# ---------------------------------------------------------------------------
# La constante, y el fallo concreto que se corrigio
# ---------------------------------------------------------------------------


def test_los_tipos_descargables_son_un_conjunto_inmutable():
    from app.services.live_service import DOWNLOADABLE_MEDIA_TYPES

    assert isinstance(DOWNLOADABLE_MEDIA_TYPES, frozenset)
    # La union con un set tiene que funcionar: era exactamente lo que fallaba.
    assert DOWNLOADABLE_MEDIA_TYPES | {"otro"}


def test_los_tipos_descargables_coinciden_con_la_columna():
    """Un tipo que la base no acepta romperia el INSERT del adjunto."""
    from app.models import MEDIA_TYPES
    from app.services.live_service import DOWNLOADABLE_MEDIA_TYPES

    assert DOWNLOADABLE_MEDIA_TYPES <= set(MEDIA_TYPES)
    assert "unknown" not in DOWNLOADABLE_MEDIA_TYPES
    for tipo in ("image", "video", "gif", "audio", "voice_note", "sticker", "document"):
        assert tipo in DOWNLOADABLE_MEDIA_TYPES


def test_ya_no_se_hace_la_union_sobre_dict_values():
    """``dict_values | set`` es un TypeError. No puede volver."""
    import inspect

    import app.services.live_service as modulo

    fuente = inspect.getsource(modulo)
    assert ".values() |" not in fuente, (
        "dict_values no admite la union con un set: fue un TypeError dentro de "
        "la transaccion y cada mensaje de texto en vivo se perdia por rollback"
    )


def test_si_store_lanza_no_se_avisa_de_nada(servicio, session, con_raw_proto, monkeypatch):
    """Un fallo dentro de ``_store`` no puede producir un evento.

    Asi era el bug: ``handle()`` capturaba el ``TypeError``, la transaccion
    real hacia rollback y el mensaje se perdia. Devolver ``None`` es lo que
    impide que ademas se emita un ``message.created`` de un mensaje que no
    esta en la base.

    (Aqui la transaccion es la del test y no revierte de verdad; lo que se
    comprueba es el contrato observable: sin resultado no hay evento.)
    """
    import app.services.live_service as modulo

    def explota(*_args, **_kwargs):
        raise TypeError("simula el fallo original")

    monkeypatch.setattr(modulo.repo, "refresh_history_state", explota)

    resultado = servicio.handle(mensaje(id="3EBLIVEBOOM01", text="boom"))

    assert resultado is None, (
        "sin resultado no hay evento: es lo que evita anunciar un mensaje "
        "que la transaccion acaba de revertir"
    )


# ---------------------------------------------------------------------------
# Orden del pipeline: persistir ANTES de avisar
# ---------------------------------------------------------------------------


def test_el_resultado_llega_con_el_mensaje_ya_en_la_base(
    servicio, session, con_raw_proto
):
    """``handle()`` solo devuelve algo cuando la fila ya existe.

    Es lo que garantiza el orden que pide el pipeline: primero PostgreSQL,
    luego el evento. Al reves, el frontend podria pedir un mensaje que aun no
    esta y llevarse un 404.
    """
    resultado = servicio.handle(mensaje(id="3EBLIVEORD001", text="orden"))

    assert resultado is not None
    fila = _fila(session, "3EBLIVEORD001")
    assert fila is not None
    assert resultado["message_id"] == fila.id
    assert resultado["chat_id"] is not None


def test_un_duplicado_se_marca_como_no_nuevo(servicio, session, con_raw_proto):
    """La deduplicacion por wamid sigue mandando; el evento la respeta."""
    primero = servicio.handle(mensaje(id="3EBLIVEDUP001", text="una vez"))
    segundo = servicio.handle(mensaje(id="3EBLIVEDUP001", text="una vez"))

    assert primero["new"] is True
    assert segundo["new"] is False
    # Y apuntan a la MISMA fila: no se creo otra.
    assert primero["message_id"] == segundo["message_id"]
    assert repo.count_messages(session, CHAT_JID) >= 1
