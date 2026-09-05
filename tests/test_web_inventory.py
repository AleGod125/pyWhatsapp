"""WhatsApp Web indexa; la sesión principal excava.

EL CAMBIO QUE FIJAN ESTAS PRUEBAS
---------------------------------
Antes el universo de conversaciones lo definía la sesión principal, y a
WhatsApp Web se le preguntaba sólo por las que ya estaban en la base esperando
ancla. Medido, eso dejaba fuera dos cosas: lo que la sesión principal nunca
descubrió, y de lo que sí conocía, todo lo que no estuviera ya materializado.

Ahora Web contesta a la pregunta de verdad — qué conversaciones existen y cuál
es el último mensaje real de cada una — y este servicio lo reconcilia.

LO QUE HAY QUE PROTEGER
-----------------------
Que Web **proponga** y Python **decida**. Web no sabe de quién es la cuenta,
ni de alias entre teléfono y LID. Si esa frontera se rompe aparecen
conversaciones duplicadas, y un historial partido en dos mitades que nunca se
juntan es peor que no tenerlo.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import Chat, ChatHistoryState, HistorySeed
from app.web_companion.inventory import (
    InventarioNoDisponible,
    WebInventoryService,
)

ANCLA = "3A1F8BDD4678EB6DE395"


class _Database:
    def __init__(self, session):
        self._session = session

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


class _Supervisor:
    def __init__(self, respuesta, *, listo=True, vivo=True, habilitado=True):
        self.respuesta = respuesta
        self.habilitado = habilitado
        self.vivo = vivo
        self._listo = listo
        self.enviados: list[dict] = []

    def enviar(self, comando, *, timeout=None):
        self.enviados.append(comando)
        return self.respuesta

    def snapshot(self):
        return {"state": "connected", "web_client_ready": self._listo}


class _Cola:
    def __init__(self):
        self.encolados: list[str] = []

    def enqueue(self, jids):
        nuevos = [j for j in jids if j not in self.encolados]
        self.encolados.extend(nuevos)
        return nuevos


@pytest.fixture
def cuenta(session):
    from app.models import User, WhatsAppAccount

    usuario = User(email=f"a{uuid.uuid4().hex[:8]}@x.test", password_hash="x")
    session.add(usuario)
    session.flush()
    wa = WhatsAppAccount(user_id=usuario.id, session_storage_key=uuid.uuid4().hex)
    session.add(wa)
    session.flush()
    return usuario.id, wa.id


def _runtime(session, respuesta, cuenta, *, principal_lista=True, **extra):
    from app.core.session_state import AppState

    usuario_id, cuenta_id = cuenta
    # El indice no reconcilia nada sin conexion principal: crearia filas y
    # cambiaria estados de una cuenta que ahora mismo no se puede excavar.
    return SimpleNamespace(
        database=_Database(session),
        web_companion=_Supervisor(respuesta, **extra),
        seed_queue=_Cola(),
        seed_collector=None,
        runtime_owner_user_id=usuario_id,
        runtime_owner_account_id=cuenta_id,
        bus=None,
        state=SimpleNamespace(
            state=AppState.CONNECTED if principal_lista else AppState.NO_SESSION
        ),
        client=SimpleNamespace(
            _client=SimpleNamespace(
                device=SimpleNamespace(jid=SimpleNamespace(user="34600111222"))
            )
        ),
        settings=SimpleNamespace(
            signal_store_file=SimpleNamespace(exists=lambda: True)
        ),
    )


def _fila(jid, *, nombre=None, grupo=False, con_ancla=True, indice=0, **cambios):
    candidato = None
    if con_ancla:
        candidato = {
            "chat_jid": jid,
            "wa_msg_id": f"{ANCLA[:-2]}{indice:02X}",
            "timestamp": 1_760_000_000 + indice,
            "from_me": False,
            "source": "web_last_message",
        }
        candidato.update(cambios)
    return {
        "chat_jid": jid,
        "is_group": grupo,
        "name": nombre,
        "last_activity": 1_760_000_500 + indice,
        "candidate": candidato,
        "via": "last_message" if con_ancla else None,
    }


def _respuesta(filas, **metricas):
    base = {
        "total": len(filas),
        "individual": sum(1 for f in filas if not f["is_group"]),
        "group": sum(1 for f in filas if f["is_group"]),
        "con_last_message": sum(1 for f in filas if f["candidate"]),
        "sin_candidato": sum(1 for f in filas if not f["candidate"]),
    }
    base.update(metricas)
    return {"event": "web_inventory", "metrics": base, "chats": filas}


def _existente(session, cuenta_id, jid, *, estado="exhausted", nombre=None):
    chat = Chat(jid=jid, chat_type="individual", name=nombre, whatsapp_account_id=cuenta_id)
    session.add(chat)
    session.flush()
    session.add(ChatHistoryState(chat_id=chat.id, chat_jid=jid, history_status=estado))
    session.flush()
    return chat


# ---------------------------------------------------------------------------
# Descubrimiento: lo que Web ve y aquí no existía
# ---------------------------------------------------------------------------


def test_una_conversacion_que_solo_ve_Web_SI_se_importa(session, cuenta):
    """La regla anterior las descartaba, y asi se perdian de verdad."""
    _, cuenta_id = cuenta
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    rt = _runtime(session, _respuesta([_fila(jid, nombre="Ana")]), cuenta)

    salida = WebInventoryService(rt).refrescar().to_json()

    assert salida["web_inventory_new"] == 1
    creado = session.execute(select(Chat).where(Chat.jid == jid)).scalar_one()
    assert creado.name == "Ana"


def test_nace_esperando_ancla_si_no_trae_ninguna(session, cuenta):
    """Existir no es estar sincronizado. Esa mentira costo dos fases."""
    _, cuenta_id = cuenta
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    rt = _runtime(session, _respuesta([_fila(jid, con_ancla=False)]), cuenta)

    WebInventoryService(rt).refrescar()

    estado = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == jid)
    ).scalar_one()
    assert estado.history_status == "waiting_seed"


def test_con_ancla_pasa_directamente_a_poder_pedir_historial(session, cuenta):
    _, cuenta_id = cuenta
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    rt = _runtime(session, _respuesta([_fila(jid)]), cuenta)

    salida = WebInventoryService(rt).refrescar()

    assert salida.promovidos == 1
    session.expire_all()
    estado = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == jid)
    ).scalar_one()
    assert estado.history_status == "pending"
    assert rt.seed_queue.encolados == [jid]


def test_una_conversacion_que_ya_existia_no_se_duplica(session, cuenta):
    _, cuenta_id = cuenta
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    _existente(session, cuenta_id, jid)
    rt = _runtime(session, _respuesta([_fila(jid)]), cuenta)

    salida = WebInventoryService(rt).refrescar().to_json()

    assert salida["web_existing_chats"] == 1
    assert salida["web_inventory_new"] == 0
    cuantos = session.execute(
        select(func.count()).select_from(Chat).where(Chat.jid == jid)
    ).scalar()
    assert cuantos == 1


def test_un_grupo_se_crea_como_grupo(session, cuenta):
    _, cuenta_id = cuenta
    jid = f"1203630{uuid.uuid4().hex[:10]}@g.us"
    rt = _runtime(
        session, _respuesta([_fila(jid, grupo=True, nombre="Familia")]), cuenta
    )

    WebInventoryService(rt).refrescar()

    chat = session.execute(select(Chat).where(Chat.jid == jid)).scalar_one()
    assert chat.chat_type == "group"
    assert chat.name == "Familia"


# ---------------------------------------------------------------------------
# Web propone, Python decide
# ---------------------------------------------------------------------------


def test_un_LID_se_resuelve_al_chat_que_ya_existe(session, cuenta):
    """El mismo contacto por telefono y por LID es UNA conversacion.

    Crear la segunda dejaria el historial partido en dos mitades que nunca se
    juntan.
    """
    from app.models import Contact

    _, cuenta_id = cuenta
    telefono = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    lid = f"649401{uuid.uuid4().hex[:8]}@lid"
    _existente(session, cuenta_id, telefono, estado="waiting_seed")
    session.add(Contact(jid=telefono, lid=lid))
    session.flush()

    rt = _runtime(session, _respuesta([_fila(lid)]), cuenta)
    salida = WebInventoryService(rt).refrescar().to_json()

    assert salida["web_inventory_new"] == 0
    assert salida["web_alias_resolved"] == 1
    assert (
        session.execute(
            select(func.count()).select_from(Chat).where(Chat.jid == lid)
        ).scalar()
        == 0
    ), "no se crea una segunda conversacion para el mismo contacto"


def test_el_ancla_pasa_por_el_recolector_de_siempre():
    """No hay un segundo sistema de anclas."""
    import inspect

    from app.web_companion import inventory

    fuente = inspect.getsource(inventory)
    assert "RecentSeedCollector" in fuente
    # Ni validacion propia, ni escritura de anclas a mano, ni cursor propio.
    assert "HistorySeed(" not in fuente
    assert "persist_cursor" not in fuente


def test_el_cursor_solo_se_LEE_y_solo_para_repartir_la_cuota_de_red():
    """La regla no es "no mirar el cursor": es no decidir anclas con el.

    El indice necesita saber cuales ya tienen con que excavar para no gastar
    en ellas una peticion de red que le hace falta a otra. Eso es una LECTURA
    y no toca ningun ancla.

    Lo que no puede pasar es que el camino que produce anclas —``_procesar``,
    ``_asegurar_chat``— empiece a decidir por su cuenta que es un cursor
    valido: dos definiciones de "tiene cursor" es exactamente el fallo que
    costo la fase anterior.
    """
    import ast
    import inspect

    from app.web_companion import inventory

    arbol = ast.parse(inspect.getsource(inventory))
    donde = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.FunctionDef):
            continue
        cuerpo = ast.unparse(nodo)
        if "get_valid_history_cursor" in cuerpo:
            donde.add(nodo.name)

    assert donde <= {"_reparto"}, (
        f"el cursor se mira fuera del reparto de la cuota de red: {donde}"
    )
    # Y ahi solo se lee: escribirlo seria el segundo sistema de anclas.
    assert "persist_cursor" not in inspect.getsource(inventory)


def test_un_ancla_inventada_se_rechaza(session, cuenta):
    _, cuenta_id = cuenta
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    rt = _runtime(session, _respuesta([_fila(jid, wa_msg_id="temp-abc")]), cuenta)

    salida = WebInventoryService(rt).refrescar()

    assert salida.validos == 0
    assert salida.rechazados == 1
    session.expire_all()
    estado = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == jid)
    ).scalar_one()
    assert estado.history_status == "waiting_seed"


def test_una_marca_en_milisegundos_se_rechaza(session, cuenta):
    _, cuenta_id = cuenta
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    rt = _runtime(
        session, _respuesta([_fila(jid, timestamp=1_760_000_000_000)]), cuenta
    )
    assert WebInventoryService(rt).refrescar().validos == 0


def test_aplicar_el_indice_dos_veces_no_duplica_anclas(session, cuenta):
    _, cuenta_id = cuenta
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    respuesta = _respuesta([_fila(jid)])
    rt = _runtime(session, respuesta, cuenta)

    WebInventoryService(rt).refrescar()
    antes = session.execute(select(func.count()).select_from(HistorySeed)).scalar()
    WebInventoryService(rt).refrescar()
    despues = session.execute(select(func.count()).select_from(HistorySeed)).scalar()

    assert antes == despues


def test_una_conversacion_terminada_no_se_reabre(session, cuenta):
    """Que Web vea actividad reciente no obliga a volver a excavar."""
    _, cuenta_id = cuenta
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    _existente(session, cuenta_id, jid, estado="exhausted")
    rt = _runtime(session, _respuesta([_fila(jid)]), cuenta)

    WebInventoryService(rt).refrescar()

    session.expire_all()
    estado = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == jid)
    ).scalar_one()
    assert estado.history_status == "exhausted"


def test_se_refresca_el_nombre_pero_no_se_pisa_el_que_hay(session, cuenta):
    _, cuenta_id = cuenta
    con_nombre = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    sin_nombre = f"5731{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    _existente(session, cuenta_id, con_nombre, nombre="El de siempre")
    _existente(session, cuenta_id, sin_nombre)

    rt = _runtime(
        session,
        _respuesta(
            [
                _fila(con_nombre, nombre="Otro nombre", indice=1),
                _fila(sin_nombre, nombre="Ana", indice=2),
            ]
        ),
        cuenta,
    )
    WebInventoryService(rt).refrescar()

    session.expire_all()
    assert (
        session.execute(select(Chat.name).where(Chat.jid == con_nombre)).scalar()
        == "El de siempre"
    )
    assert (
        session.execute(select(Chat.name).where(Chat.jid == sin_nombre)).scalar()
        == "Ana"
    )


# ---------------------------------------------------------------------------
# Cobertura y condiciones
# ---------------------------------------------------------------------------


def test_la_cobertura_se_mide_sobre_lo_que_ve_Web(session, cuenta):
    _, cuenta_id = cuenta
    filas = [
        _fila(f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net", indice=i)
        for i in range(8)
    ]
    filas += [
        _fila(f"5731{uuid.uuid4().hex[:10]}@s.whatsapp.net", con_ancla=False, indice=i)
        for i in range(2)
    ]
    rt = _runtime(session, _respuesta(filas), cuenta)

    salida = WebInventoryService(rt).refrescar().to_json()

    assert salida["web_inventory_total"] == 10
    assert salida["web_seed_valid"] == 8
    assert salida["web_seed_coverage_percent"] == 80


def test_sin_el_indice_listo_no_se_toca_nada(session, cuenta):
    _, cuenta_id = cuenta
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    rt = _runtime(session, _respuesta([_fila(jid)]), cuenta, listo=False)

    antes = session.execute(select(func.count()).select_from(Chat)).scalar()
    with pytest.raises(InventarioNoDisponible) as fallo:
        WebInventoryService(rt).refrescar()

    assert fallo.value.code == "WEB_COMPANION_NOT_READY"
    assert session.execute(select(func.count()).select_from(Chat)).scalar() == antes
    assert rt.web_companion.enviados == []


def test_apagado_tampoco(session, cuenta):
    rt = _runtime(session, _respuesta([]), cuenta, habilitado=False)
    with pytest.raises(InventarioNoDisponible) as fallo:
        WebInventoryService(rt).refrescar()
    assert fallo.value.code == "WEB_COMPANION_DISABLED"


def test_se_pide_el_indice_completo_no_los_que_esperan(session, cuenta):
    """La pregunta cambio: ya no es "de estos, cuales ves"."""
    _, cuenta_id = cuenta
    rt = _runtime(session, _respuesta([]), cuenta)
    WebInventoryService(rt).refrescar()
    assert [c["cmd"] for c in rt.web_companion.enviados] == ["web_inventory"]


def test_el_indice_no_pide_historial():
    """Web indexa; excavar es de la sesion principal."""
    import ast
    import inspect

    from app.web_companion import inventory

    # Sobre el CODIGO, no sobre la prosa: la documentacion habla de ON_DEMAND
    # justamente para explicar que eso lo hace la otra sesion.
    arbol = ast.parse(inspect.getsource(inventory))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Expr) and isinstance(nodo.value, ast.Constant):
            if isinstance(nodo.value.value, str):
                nodo.value.value = ""
    codigo = ast.unparse(arbol)

    for prohibido in (
        "ON_DEMAND",
        "build_on_demand_message",
        "_process_chat",
        "run_canary",
        "BackfillService",
    ):
        assert prohibido not in codigo, f"el indice pide historial: {prohibido}"


# ---------------------------------------------------------------------------
# UNA LLAVE POR CONVERSACION
# ---------------------------------------------------------------------------
#
# No buscamos historial completo con WhatsApp Web. Buscamos UN mensaje real
# por conversacion: con su identificador, su marca y su direccion, el motor de
# siempre puede pedir el resto.
#
# Se midio el fallo que motiva estas pruebas: el indice reportaba
# `chats=50 seeds=0 cobertura=0%` mientras el sondeo antiguo encontraba 14
# referencias sobre LAS MISMAS conversaciones. No era que WhatsApp Web no
# tuviera mensajes: era que por ese camino no se llegaba a preguntar.


def test_una_conversacion_que_solo_ve_Web_con_referencia_queda_lista(session, cuenta):
    """El caso completo: no existia aqui, Web la ve, trae mensaje, se excava."""
    _, cuenta_id = cuenta
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    rt = _runtime(session, _respuesta([_fila(jid)]), cuenta)

    salida = WebInventoryService(rt).refrescar()

    assert salida.creados == 1
    assert salida.validos == 1
    assert salida.promovidos == 1
    session.expire_all()
    estado = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == jid)
    ).scalar_one()
    assert estado.history_status == "pending", "ya se puede excavar"
    assert rt.seed_queue.encolados == [jid]


def test_una_que_solo_ve_Web_y_no_da_mensaje_espera_referencia(session, cuenta):
    """No se inventa nada. Existe, y se sabe por que no se puede excavar."""
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    respuesta = _respuesta([_fila(jid, con_ancla=False)])
    respuesta["chats"][0]["no_seed_reason"] = "WEB_NO_MATERIALIZED_MESSAGE"
    rt = _runtime(session, respuesta, cuenta)

    salida = WebInventoryService(rt).refrescar()

    assert salida.creados == 1
    assert salida.validos == 0
    assert salida.motivos_sin_referencia == {"WEB_NO_MATERIALIZED_MESSAGE": 1}
    session.expire_all()
    estado = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == jid)
    ).scalar_one()
    assert estado.history_status == "waiting_seed"


def test_un_fallo_de_red_no_marca_la_conversacion_como_rota(session, cuenta):
    """Se vuelve a intentar en otra ronda: no es un error permanente."""
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    respuesta = _respuesta([_fila(jid, con_ancla=False)])
    respuesta["chats"][0]["no_seed_reason"] = "WEB_FETCH1_FAILED"
    rt = _runtime(session, respuesta, cuenta)

    salida = WebInventoryService(rt).refrescar()

    assert salida.motivos_sin_referencia == {"WEB_FETCH1_FAILED": 1}
    session.expire_all()
    estado = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == jid)
    ).scalar_one()
    assert estado.history_status == "waiting_seed", "sigue esperando, no en error"


def test_la_referencia_de_la_red_pasa_por_el_recolector_de_siempre(session, cuenta):
    """Venga de donde venga, la valida el mismo recolector."""
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    rt = _runtime(session, _respuesta([_fila(jid, source="web_fetch1")]), cuenta)

    salida = WebInventoryService(rt).refrescar()

    assert salida.validos == 1
    session.expire_all()
    ancla = session.execute(
        select(HistorySeed).where(HistorySeed.chat_jid == jid)
    ).scalar_one()
    assert ancla.source == "web_fetch1", "y se conserva por que via llego"


def test_la_misma_referencia_dos_veces_es_una_sola(session, cuenta):
    """El sondeo antiguo y el indice pueden ver el MISMO mensaje."""
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    rt = _runtime(session, _respuesta([_fila(jid, source="web_store")]), cuenta)
    WebInventoryService(rt).refrescar()
    session.expire_all()

    # Y ahora el mismo WAMID, por la otra via.
    rt2 = _runtime(session, _respuesta([_fila(jid, source="web_fetch1")]), cuenta)
    WebInventoryService(rt2).refrescar()
    session.expire_all()

    assert (
        session.execute(
            select(func.count()).select_from(HistorySeed).where(HistorySeed.chat_jid == jid)
        ).scalar()
        == 1
    ), "una conversacion, una referencia"


# ---------------------------------------------------------------------------
# Donde se gasta la cuota de red
# ---------------------------------------------------------------------------


def test_se_le_dice_a_Node_cuales_esperan_y_cuales_ya_estan(session, cuenta):
    """Node no puede saberlo, y sin eso gasta la cuota donde no desbloquea nada."""
    _, cuenta_id = cuenta
    esperando = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    agotada = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    for jid, estado in ((esperando, "waiting_seed"), (agotada, "exhausted")):
        chat = Chat(jid=jid, chat_type="individual", whatsapp_account_id=cuenta_id)
        session.add(chat)
        session.flush()
        session.add(
            ChatHistoryState(chat_id=chat.id, chat_jid=jid, history_status=estado)
        )
    session.flush()

    rt = _runtime(session, _respuesta([]), cuenta)
    WebInventoryService(rt).refrescar()

    enviado = rt.web_companion.enviados[0]
    assert esperando in enviado["priority_chat_jids"]
    assert agotada in enviado["skip_chat_jids"], "su historial ya termino"
    assert esperando in enviado["known_chat_jids"]
    assert agotada in enviado["known_chat_jids"]


def test_una_agotada_no_se_reabre_por_que_Web_la_vea_activa(session, cuenta):
    """Ver actividad reciente no es motivo para volver a excavar."""
    _, cuenta_id = cuenta
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    chat = Chat(jid=jid, chat_type="individual", whatsapp_account_id=cuenta_id)
    session.add(chat)
    session.flush()
    session.add(
        ChatHistoryState(chat_id=chat.id, chat_jid=jid, history_status="exhausted")
    )
    session.flush()

    rt = _runtime(session, _respuesta([_fila(jid)]), cuenta)
    WebInventoryService(rt).refrescar()
    session.expire_all()

    estado = session.execute(
        select(ChatHistoryState).where(ChatHistoryState.chat_jid == jid)
    ).scalar_one()
    assert estado.history_status == "exhausted"


def test_la_telemetria_separa_ver_un_mensaje_de_tener_una_referencia(session, cuenta):
    """Con una sola cifra, "0 referencias" no dice donde esta el problema."""
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    respuesta = _respuesta([_fila(jid, con_ancla=False)])
    respuesta["metrics"].update(
        {
            "fetch1_attempted": 9,
            "fetch1_success": 7,
            "fetch1_empty": 2,
            "fetch1_error": 0,
            "messages_found": 7,
            "seed_invalid": 7,
        }
    )
    rt = _runtime(session, respuesta, cuenta)

    datos = WebInventoryService(rt).refrescar().to_json()

    assert datos["fetch1_attempted"] == 9
    assert datos["messages_found"] == 7, "WhatsApp Web SI daba mensajes"
    assert datos["valid_seeds"] == 0, "y aun asi ninguno servia"
    assert datos["seed_invalid"] == 7, "el problema esta en el filtro"


# ---------------------------------------------------------------------------
# QUE LA PANTALLA SE ENTERE SOLA
# ---------------------------------------------------------------------------
#
# El fallo medido: el índice creaba conversaciones en PostgreSQL y no avisaba
# a nadie. El único que publicaba el resumen era la ruta HTTP, y esa no la
# llama nadie -- el índice lo lanza el vigilante automático cada treinta
# segundos. Así que las conversaciones nuevas existían en la base y la pantalla
# seguía enseñando la lista del momento en que se cargó. La única salida era F5.
#
# La regla que no se rompe: **primero PostgreSQL, después el aviso**. Nunca al
# revés, o la pantalla enseñaría una conversación que todavía no existe.


class _Bus:
    def __init__(self):
        self.eventos: list[tuple[str, object]] = []

    def publish(self, nombre, carga=None, **extra):
        self.eventos.append((nombre, carga))

    def de(self, nombre):
        return [c for n, c in self.eventos if n == nombre]


def test_una_conversacion_nueva_se_anuncia_con_su_fila_entera(session, cuenta):
    """Con la fila dentro, la pantalla la inserta sin pedir nada.

    El aviso escueto obligaba a pedir la lista, y descubrir cincuenta
    conversaciones serían cincuenta peticiones contra el mismo endpoint.
    """
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    rt = _runtime(session, _respuesta([_fila(jid, nombre="Ana")]), cuenta)
    rt.bus = _Bus()

    WebInventoryService(rt).refrescar()

    avisos = rt.bus.de("web_chat_created")
    assert len(avisos) == 1
    fila = avisos[0]["chat"]
    assert avisos[0]["chat_id"]
    assert fila["jid"] == jid
    assert "display_name" in fila and "history_status" in fila


def test_no_se_anuncia_nada_que_no_este_ya_en_la_base(session, cuenta):
    """La regla: primero PostgreSQL, después el aviso."""
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    rt = _runtime(session, _respuesta([_fila(jid)]), cuenta)
    rt.bus = _Bus()

    WebInventoryService(rt).refrescar()

    for carga in rt.bus.de("web_chat_created"):
        existe = session.execute(
            select(func.count()).select_from(Chat).where(Chat.id == carga["chat_id"])
        ).scalar()
        assert existe == 1, "se anunció una conversación que no está guardada"


def test_una_que_ya_existia_se_anuncia_como_actualizacion(session, cuenta):
    """Se actualiza en el sitio: ni se duplica ni se recarga la lista."""
    _, cuenta_id = cuenta
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    chat = Chat(jid=jid, chat_type="individual", whatsapp_account_id=cuenta_id)
    session.add(chat)
    session.flush()
    session.add(
        ChatHistoryState(chat_id=chat.id, chat_jid=jid, history_status="waiting_seed")
    )
    session.flush()

    rt = _runtime(session, _respuesta([_fila(jid, nombre="Ana")]), cuenta)
    rt.bus = _Bus()
    WebInventoryService(rt).refrescar()

    assert rt.bus.de("web_chat_created") == []
    actualizados = rt.bus.de("web_chat_updated")
    assert len(actualizados) == 1
    assert actualizados[0]["chat_id"] == chat.id


def test_el_resumen_de_la_tanda_sale_del_servicio_no_de_la_ruta(session, cuenta):
    """La causa exacta del F5.

    El índice normal lo lanza el vigilante automático, no una petición HTTP.
    Publicarlo sólo desde la ruta dejaba mudo justo el camino que se usa.
    """
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    rt = _runtime(session, _respuesta([_fila(jid)]), cuenta)
    rt.bus = _Bus()

    WebInventoryService(rt).refrescar()

    resumen = rt.bus.de("web_inventory_done")
    assert len(resumen) == 1
    assert resumen[0]["web_inventory_new"] == 1


def test_una_tanda_de_veinte_no_produce_veinte_resumenes(session, cuenta):
    """Un aviso por conversación y UNO de tanda. Ni uno más."""
    filas = [
        _fila(f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net", indice=i) for i in range(20)
    ]
    rt = _runtime(session, _respuesta(filas), cuenta)
    rt.bus = _Bus()

    WebInventoryService(rt).refrescar()

    assert len(rt.bus.de("web_chat_created")) == 20
    assert len(rt.bus.de("web_inventory_done")) == 1


def test_sin_bus_el_indice_funciona_igual(session, cuenta):
    """Avisar es un extra: si no hay a quién, se guarda igual."""
    jid = f"5730{uuid.uuid4().hex[:10]}@s.whatsapp.net"
    rt = _runtime(session, _respuesta([_fila(jid)]), cuenta)
    rt.bus = None

    salida = WebInventoryService(rt).refrescar()

    assert salida.creados == 1
