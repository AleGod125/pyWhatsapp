"""Recuperar el historial que ya estaba en disco.

EL HALLAZGO QUE LO MOTIVA, MEDIDO EN LA INSTALACION REAL
--------------------------------------------------------
    mensajes en los blobs archivados = 6504
    mensajes en PostgreSQL           =  537

WhatsApp SI habia entregado el historial. Se archivo en ``data/history/*.pb``
y la base se quedo sin el. Una conversacion con 1994 mensajes en disco tenia
31 guardados, y encima marcada ``exhausted``: el telefono habia contestado
"no queda mas" --con razon-- asi que ningun boton que pregunte al telefono
podia arreglarlo.

``BlobSeedScanner`` recorria esos mismos archivos, les sacaba el identificador
para usarlo de ancla y tiraba el cuerpo del mensaje. Su docstring lo decia:
"los mensajes de esos blobs ya estan donde tienen que estar". Era cierto hasta
que alguien vacio PostgreSQL.

LO QUE SE PRUEBA
----------------
Lo dificil no es leer los archivos: es no meterle a una cuenta las
conversaciones de otra. Los blobs se archivan en una carpeta COMUN y el
nombre del archivo no dice de quien es.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from app.wa.historial import FullHistorySync, HistoryConversation
from app.models import Chat, Message, User, WhatsAppAccount
from app.services.blob_reingest import _es_mia, reingerir_blobs


class _Db:
    def __init__(self, session):
        self._session = session

    def transaction(self):
        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


class _Ajustes:
    def __init__(self, carpeta):
        self.data_dir = carpeta


def _blob(tmp_path, nombre: str, conversaciones):
    """Un archivo con contenido cualquiera; ``parse_full`` se sustituye."""
    carpeta = tmp_path / "history"
    carpeta.mkdir(exist_ok=True)
    ruta = carpeta / nombre
    ruta.write_bytes(b"da igual: parse_full esta sustituido")
    return FullHistorySync(
        sync_type="ON_DEMAND",
        chunk_order=0,
        progress=100,
        conversations=conversaciones,
        pushnames=[],
    )


def _conversacion(jid: str) -> HistoryConversation:
    return HistoryConversation(
        jid=jid, name=None, last_message_timestamp=None, unread_count=0, messages=[]
    )


def _ya_conocida(session, cuenta, jid: str) -> int:
    """Da de alta ese chat en la cuenta.

    Hace falta desde que un lote huerfano ya NO se adopta: reingerir sirve
    para recuperar el CONTENIDO de conversaciones que la cuenta ya conoce, no
    para descubrir de quien es un fichero suelto. Adoptar lo suelto es como se
    ingirieron 6613 mensajes de otra persona.
    """
    from app.services import repository as repo

    chat_id = repo.upsert_chat(session, jid=jid, whatsapp_account_id=cuenta.id)
    session.flush()
    return chat_id


def _otra_cuenta(session) -> WhatsAppAccount:
    usuario = User(email=f"o-{uuid.uuid4().hex[:8]}@x.com", password_hash="x")
    session.add(usuario)
    session.flush()
    ident = uuid.uuid4()
    cuenta = WhatsAppAccount(
        id=ident,
        user_id=usuario.id,
        session_status="linked",
        session_storage_key=f"accounts/{ident}",
    )
    session.add(cuenta)
    session.flush()
    return cuenta


# ---------------------------------------------------------------------------
# La regla de atribucion, aislada
# ---------------------------------------------------------------------------


def test_una_conversacion_que_ya_es_mia_se_ingiere():
    duenos = {"a@s.whatsapp.net": {"yo"}}

    assert _es_mia("a@s.whatsapp.net", "yo", duenos, sola=False) is True


def test_una_conversacion_de_OTRA_cuenta_no_se_toca_nunca():
    """Ni siquiera siendo la unica cuenta conectada ahora mismo."""
    duenos = {"a@s.whatsapp.net": {"otra"}}

    assert _es_mia("a@s.whatsapp.net", "yo", duenos, sola=False) is False
    assert _es_mia("a@s.whatsapp.net", "yo", duenos, sola=True) is False


def test_una_desconocida_NO_se_adopta_ni_siendo_la_unica_cuenta():
    """Esta prueba decia lo contrario, y lo contrario produjo una fuga.

    Decia: "con una sola cuenta no hay ninguna otra a la que pudiera
    pertenecer". El razonamiento da por hecho que el lote lo produjo una cuenta
    que TODAVIA existe. Se vacio la base --las cuentas desaparecieron, los 256
    lotes se quedaron en disco--, se vinculo otro telefono de otra persona, y
    "no hay ninguna otra" resulto cierto y falso a la vez: se adoptaron 326
    conversaciones y 6613 mensajes ajenos.

    Perder una reingesta se ve y se repite. Ingerir la conversacion de otro no
    se ve.
    """
    assert _es_mia("nueva@s.whatsapp.net", "yo", {}, sola=True) is None


def test_una_desconocida_con_VARIAS_cuentas_no_se_adivina():
    """Devolver `None` es decir "no lo se". Adivinar seria repartir historial."""
    assert _es_mia("nueva@s.whatsapp.net", "yo", {}, sola=False) is None


def test_una_conversacion_compartida_por_dos_cuentas_es_mia_si_estoy_dentro():
    """El mismo grupo lo tienen las dos: la fila de cada una es suya."""
    duenos = {"g@g.us": {"yo", "otra"}}

    assert _es_mia("g@g.us", "yo", duenos, sola=False) is True


# ---------------------------------------------------------------------------
# El recorrido completo
# ---------------------------------------------------------------------------


def test_se_recupera_el_historial_que_estaba_en_disco(
    session, cuenta, tmp_path, monkeypatch
):
    jid = f"{uuid.uuid4().int % 10**12}@s.whatsapp.net"
    _ya_conocida(session, cuenta, jid)
    sync = _blob(tmp_path, "uno.pb", [_conversacion(jid)])

    llamadas = []

    def _falso_parse(_crudo):
        return sync

    monkeypatch.setattr("app.wa.historial.parse_full", _falso_parse)

    def _falsa_ingesta(sesion, s, **kwargs):
        llamadas.append(kwargs)
        from app.services.history_service import IngestResult

        r = IngestResult()
        r.conversations = len(s.conversations)
        r.messages_seen = 10
        r.messages_inserted = 7
        r.new_chat_jids = [c.jid for c in s.conversations]
        return r

    monkeypatch.setattr("app.services.history_service.ingest_history_sync", _falsa_ingesta)

    resultado = reingerir_blobs(
        _Db(session), _Ajustes(tmp_path), account_id=cuenta.id
    )

    assert resultado.blobs == 1
    assert resultado.mensajes_nuevos == 7
    # Y con dueno: sin esto los mensajes entran y no los ve nadie.
    assert llamadas[0]["whatsapp_account_id"] == cuenta.id


def test_NO_se_ingiere_la_conversacion_de_otra_cuenta(
    session, cuenta, tmp_path, monkeypatch
):
    """El fallo que habria: darle a una persona la copia de otra.

    Los blobs se archivan en una carpeta comun, sin nada que diga de quien
    son. Es exactamente el sitio donde se cuela un cruce de cuentas.
    """
    ajena = _otra_cuenta(session)
    jid_ajeno = f"{uuid.uuid4().int % 10**12}@s.whatsapp.net"
    session.add(
        Chat(jid=jid_ajeno, chat_type="individual", whatsapp_account_id=ajena.id)
    )
    session.flush()

    sync = _blob(tmp_path, "uno.pb", [_conversacion(jid_ajeno)])
    monkeypatch.setattr("app.wa.historial.parse_full", lambda _c: sync)

    ingeridas = []

    def _falsa_ingesta(sesion, s, **kwargs):
        ingeridas.extend(c.jid for c in s.conversations)
        from app.services.history_service import IngestResult

        return IngestResult()

    monkeypatch.setattr("app.services.history_service.ingest_history_sync", _falsa_ingesta)

    resultado = reingerir_blobs(_Db(session), _Ajustes(tmp_path), account_id=cuenta.id)

    assert ingeridas == [], "no se puede ingerir la conversacion de otra cuenta"
    assert resultado.ajenas == 1
    assert resultado.mensajes_nuevos == 0


def test_con_varias_cuentas_una_conversacion_desconocida_se_deja_fuera(
    session, cuenta, tmp_path, monkeypatch
):
    _otra_cuenta(session)  # ya no soy la unica
    sync = _blob(tmp_path, "uno.pb", [_conversacion("nadie@s.whatsapp.net")])
    monkeypatch.setattr("app.wa.historial.parse_full", lambda _c: sync)

    ingeridas = []
    monkeypatch.setattr(
        "app.services.history_service.ingest_history_sync",
        lambda sesion, s, **k: ingeridas.extend(c.jid for c in s.conversations),
    )

    resultado = reingerir_blobs(_Db(session), _Ajustes(tmp_path), account_id=cuenta.id)

    assert ingeridas == []
    assert resultado.sin_atribuir == 1


def test_un_blob_roto_no_para_a_los_demas(session, cuenta, tmp_path, monkeypatch):
    jid = f"{uuid.uuid4().int % 10**12}@s.whatsapp.net"
    bueno = _blob(tmp_path, "b-bueno.pb", [_conversacion(jid)])
    _blob(tmp_path, "a-roto.pb", [])

    def _parse(crudo):
        raise ValueError("protobuf corrupto")

    estado = {"primero": True}

    def _parse_selectivo(crudo):
        if estado["primero"]:
            estado["primero"] = False
            raise ValueError("protobuf corrupto")
        return bueno

    monkeypatch.setattr("app.wa.historial.parse_full", _parse_selectivo)

    from app.services.history_service import IngestResult

    monkeypatch.setattr(
        "app.services.history_service.ingest_history_sync",
        lambda sesion, s, **k: IngestResult(),
    )

    resultado = reingerir_blobs(_Db(session), _Ajustes(tmp_path), account_id=cuenta.id)

    assert resultado.blobs_ilegibles == 1
    assert resultado.blobs == 1, "el segundo se leyo igual"
    assert _parse is not None


def test_sin_carpeta_no_pasa_nada(session, cuenta, tmp_path):
    resultado = reingerir_blobs(
        _Db(session), _Ajustes(tmp_path / "no-existe"), account_id=cuenta.id
    )

    assert resultado.blobs == 0


# ---------------------------------------------------------------------------
# El blob de Baileys (JSON) se recupera igual que el de pywhats (.pb)
# ---------------------------------------------------------------------------
#
# MEDIDO EN UN ENLACE FRESCO: un History Sync de 156 conversaciones y 4887
# mensajes llego, se archivo en ``data/history_baileys/*.json`` y el INSERT
# fallo entero por el limite de parametros de PostgreSQL (ver
# ``test_bulk_upsert_no_supera_el_limite_de_parametros`` en
# ``test_proveedor_de_whatsapp.py``). El bootstrap completo se habria perdido
# sin este camino: el blob ya estaba en casa, solo habia que releerlo.


def test_se_recupera_un_blob_de_baileys_en_json(session, cuenta, tmp_path, monkeypatch):
    carpeta = tmp_path / "history_baileys"
    carpeta.mkdir()
    (carpeta / "bootstrap.json").write_text("{}", encoding="utf-8")

    jid = f"{uuid.uuid4().int % 10**12}@lid"
    _ya_conocida(session, cuenta, jid)
    sync = FullHistorySync(
        sync_type="RECENT", chunk_order=0, progress=100,
        conversations=[_conversacion(jid)], pushnames=[],
    )
    monkeypatch.setattr("app.wa.historial.parse_full_json", lambda _d: sync)

    llamadas = []

    def _falsa_ingesta(sesion, s, **kwargs):
        llamadas.append(kwargs)
        from app.services.history_service import IngestResult

        r = IngestResult()
        r.conversations = len(s.conversations)
        r.messages_inserted = 4887
        r.new_chat_jids = [c.jid for c in s.conversations]
        return r

    monkeypatch.setattr("app.services.history_service.ingest_history_sync", _falsa_ingesta)

    resultado = reingerir_blobs(_Db(session), _Ajustes(tmp_path), account_id=cuenta.id)

    assert resultado.blobs == 1
    assert resultado.mensajes_nuevos == 4887
    assert llamadas[0]["whatsapp_account_id"] == cuenta.id


def test_recorre_las_DOS_carpetas_a_la_vez(session, cuenta, tmp_path, monkeypatch):
    """Un blob viejo (.pb) y uno nuevo (Baileys) pueden convivir en disco."""
    (tmp_path / "history").mkdir()
    (tmp_path / "history" / "viejo.pb").write_bytes(b"x")
    (tmp_path / "history_baileys").mkdir()
    (tmp_path / "history_baileys" / "nuevo.json").write_text("{}", encoding="utf-8")

    jid_pb = f"{uuid.uuid4().int % 10**12}@s.whatsapp.net"
    jid_json = f"{uuid.uuid4().int % 10**12}@lid"
    _ya_conocida(session, cuenta, jid_pb)
    _ya_conocida(session, cuenta, jid_json)
    monkeypatch.setattr(
        "app.wa.historial.parse_full",
        lambda _c: FullHistorySync(
            sync_type="ON_DEMAND", chunk_order=0, progress=100,
            conversations=[_conversacion(jid_pb)], pushnames=[],
        ),
    )
    monkeypatch.setattr(
        "app.wa.historial.parse_full_json",
        lambda _d: FullHistorySync(
            sync_type="RECENT", chunk_order=0, progress=100,
            conversations=[_conversacion(jid_json)], pushnames=[],
        ),
    )

    vistos = []

    def _falsa_ingesta(sesion, s, **kwargs):
        vistos.extend(c.jid for c in s.conversations)
        from app.services.history_service import IngestResult

        return IngestResult()

    monkeypatch.setattr("app.services.history_service.ingest_history_sync", _falsa_ingesta)

    resultado = reingerir_blobs(_Db(session), _Ajustes(tmp_path), account_id=cuenta.id)

    assert resultado.blobs == 2
    assert set(vistos) == {jid_pb, jid_json}


def test_un_json_ilegible_no_para_a_los_demas(session, cuenta, tmp_path):
    carpeta = tmp_path / "history_baileys"
    carpeta.mkdir()
    (carpeta / "roto.json").write_text("esto no es json", encoding="utf-8")

    resultado = reingerir_blobs(_Db(session), _Ajustes(tmp_path), account_id=cuenta.id)

    assert resultado.blobs_ilegibles == 1
    assert resultado.blobs == 0


# ---------------------------------------------------------------------------
# Idempotencia: la garantia de que se puede pulsar el boton dos veces
# ---------------------------------------------------------------------------


def test_reingerir_dos_veces_no_duplica_ni_un_mensaje(session, cuenta):
    """Lo garantiza el upsert por (chat_id, whatsapp_message_id).

    Se comprueba de verdad contra la base, no leyendo el codigo: es la
    propiedad de la que depende que el boton se pueda pulsar sin miedo.
    """
    from app.services.history_service import ingest_history_sync

    jid = f"{uuid.uuid4().int % 10**12}@s.whatsapp.net"
    chat = Chat(jid=jid, chat_type="individual", whatsapp_account_id=cuenta.id)
    session.add(chat)
    session.flush()

    from app.services.repository import bulk_upsert_messages
    from app.services.history_service import IncomingMessage

    wamid = f"WAMID{uuid.uuid4().hex[:16].upper()}"
    mensaje = IncomingMessage(
        chat_jid=jid,
        whatsapp_message_id=wamid,
        timestamp=1_700_000_000,
        from_me=False,
        message_type="text",
        text="hola",
        source="on_demand",
    )

    primera = bulk_upsert_messages(session, {jid: chat.id}, [mensaje])
    segunda = bulk_upsert_messages(session, {jid: chat.id}, [mensaje])
    session.flush()

    assert primera == 1
    assert segunda == 0, "la segunda pasada no puede insertar nada"

    cuantos = session.execute(
        Message.__table__.select().where(Message.whatsapp_message_id == wamid)
    ).all()
    assert len(cuantos) == 1
    assert ingest_history_sync is not None


# ---------------------------------------------------------------------------
# El ciclo YA NO la llama, y esa es la garantia
# ---------------------------------------------------------------------------
#
# Aqui habia una prueba que exigia lo contrario::
#
#     assert PHASES.index("archive") < PHASES.index("backfill")
#
# El razonamiento era "pedirle al telefono lo que ya esta en casa gasta la
# unica ranura de peticiones". Suena bien, y era exactamente el camino de la
# fuga: un ciclo automatico que lee ficheros del disco y mete lo que encuentra
# en la cuenta que se este mirando. Con la base recien vaciada, esos ficheros
# no se podian atribuir a nadie -- y entraron 326 conversaciones y 6613
# mensajes de otra persona, con su telefono apagado y sin vincular.
#
# Excavar es pedirle cosas al TELEFONO. La fase se quito de `PHASES` y del
# ciclo; el metodo `_fase_archivo` sigue escrito pero no lo invoca nadie.
#
# La garantia vive en `test_nada_personal_en_disco.py`
# (`test_el_ciclo_de_sincronizacion_no_relee_el_disco` y
# `test_el_ciclo_no_llama_a_la_reingesta`). Lo que queda aqui son las
# propiedades del metodo, por si alguna vez se vuelve a enchufar a mano.


def test_la_fase_archivo_sigue_sin_estar_enchufada():
    """Si vuelve a `PHASES`, vuelve la fuga. Que falle aqui tambien."""
    from app.services.sync_job import PHASES

    assert "archive" not in PHASES


def test_la_fase_archivo_solo_correria_en_la_revision_completa():
    """En la busqueda rapida seria releer 500 archivos para no encontrar nada."""
    import inspect

    from app.services.sync_job import SyncJob

    fuente = inspect.getsource(SyncJob._fase_archivo)
    assert 'self.state.mode != "full"' in fuente
    assert "reingerir_blobs" in fuente


def test_la_fase_se_acotaria_a_la_cuenta_que_pulsa():
    """Sin esto, reingerir seria repartir a ojo lotes que no dicen de quien son."""
    import inspect

    from app.services.sync_job import SyncJob

    assert "runtime_owner_account_id" in inspect.getsource(SyncJob._fase_archivo)


# ---------------------------------------------------------------------------
# Reabrir las agotadas: solo con evidencia, nunca por pulsar
# ---------------------------------------------------------------------------


def _agotada(session, cuenta, *, ancla_ahora: int, cursor_respondido: int | None):
    """Un chat `exhausted` con su ultima peticion respondida."""
    from app.models import ChatHistoryState, HistoryRequest

    chat = Chat(
        jid=f"{uuid.uuid4().int % 10**12}@s.whatsapp.net",
        chat_type="individual",
        whatsapp_account_id=cuenta.id,
    )
    session.add(chat)
    session.flush()
    session.add(
        ChatHistoryState(
            chat_id=chat.id,
            chat_jid=chat.jid,
            history_status="exhausted",
            oldest_message_timestamp=ancla_ahora,
        )
    )
    if cursor_respondido is not None:
        session.add(
            HistoryRequest(
                chat_id=chat.id,
                chat_jid=chat.jid,
                cursor_timestamp=cursor_respondido,
                requested_count=500,
                status="received",
            )
        )
    session.flush()
    return chat


def _estado_de(session, chat):
    from app.models import ChatHistoryState

    session.expire_all()
    return session.execute(
        ChatHistoryState.__table__.select().where(ChatHistoryState.chat_id == chat.id)
    ).one()


def test_una_agotada_con_ancla_MAS_VIEJA_vuelve_a_la_cola(session, cuenta):
    """El telefono contesto "no queda nada antes del 8 de septiembre".

    Al releer el archivo aparecieron mensajes de agosto, asi que el ancla se
    movio atras. "¿Queda algo antes del 9 de agosto?" es otra pregunta, y esa
    no la ha contestado nunca.
    """
    from app.services.blob_reingest import reabrir_agotados_con_ancla_mas_vieja

    chat = _agotada(
        session,
        cuenta,
        ancla_ahora=1_754_000_000,       # agosto: lo que hay AHORA
        cursor_respondido=1_757_000_000,  # septiembre: lo que se pregunto
    )

    assert reabrir_agotados_con_ancla_mas_vieja(_Db(session), cuenta.id) == 1
    assert _estado_de(session, chat).history_status == "pending"


def test_una_agotada_cuyo_ancla_NO_se_movio_sigue_agotada(session, cuenta):
    """Sin evidencia nueva no se reabre. Insistir gasta una peticion para nada."""
    from app.services.blob_reingest import reabrir_agotados_con_ancla_mas_vieja

    chat = _agotada(
        session, cuenta, ancla_ahora=1_757_000_000, cursor_respondido=1_757_000_000
    )

    assert reabrir_agotados_con_ancla_mas_vieja(_Db(session), cuenta.id) == 0
    assert _estado_de(session, chat).history_status == "exhausted"


def test_una_agotada_a_la_que_nunca_se_le_pidio_nada_no_se_reabre(session, cuenta):
    """Sin peticion respondida no hay con que comparar: no es evidencia."""
    from app.services.blob_reingest import reabrir_agotados_con_ancla_mas_vieja

    chat = _agotada(session, cuenta, ancla_ahora=1_754_000_000, cursor_respondido=None)

    assert reabrir_agotados_con_ancla_mas_vieja(_Db(session), cuenta.id) == 0
    assert _estado_de(session, chat).history_status == "exhausted"


def test_no_se_reabre_la_agotada_de_OTRA_cuenta(session, cuenta):
    from app.services.blob_reingest import reabrir_agotados_con_ancla_mas_vieja

    ajena = _otra_cuenta(session)
    mia = _agotada(
        session, cuenta, ancla_ahora=1_754_000_000, cursor_respondido=1_757_000_000
    )
    suya = _agotada(
        session, ajena, ancla_ahora=1_754_000_000, cursor_respondido=1_757_000_000
    )

    assert reabrir_agotados_con_ancla_mas_vieja(_Db(session), cuenta.id) == 1

    assert _estado_de(session, mia).history_status == "pending"
    assert _estado_de(session, suya).history_status == "exhausted"


def test_reabrir_agotadas_solo_ocurre_si_el_archivo_aporto_algo():
    """Sin mensajes nuevos el ancla no se ha movido: no hay nada que revisar."""
    import inspect

    from app.services.sync_job import SyncJob

    fuente = inspect.getsource(SyncJob._fase_archivo)
    assert "if resultado.mensajes_nuevos:" in fuente
    assert "reabrir_agotados_con_ancla_mas_vieja" in fuente
