"""Anclas que ya teníamos guardadas y no estábamos usando.

LO QUE SE MIDIO ANTES DE ESCRIBIR EL RESOLUTOR
----------------------------------------------
Sobre la base real, contando sólo lo que NO trajo el segundo dispositivo::

    conversaciones                                        51
    con ancla de fuente propia                             9
    SIN ancla propia pero CON mensaje real guardado       23   <- gratis
    SIN ancla propia y sin ningún mensaje real            20

Esas 23 conversaciones tienen mensajes reales, autenticados, guardados — y
figuraban sin ancla porque nadie había promovido ninguno de ellos a referencia.

QUE PROTEGEN ESTAS PRUEBAS
--------------------------
Sobre todo las prohibiciones. Un ancla fabricada recibe confirmación del
servidor y después silencio, que es el fallo más caro de diagnosticar del
proyecto — así que aquí se comprueba, una por una, que no se inventa nada:
ni identificadores, ni marcas, ni referencias prestadas de otra conversación.

Y que **no se mira el navegador**: si el resolutor pudiera consultarlo, la
mejora que mide dejaría de significar «lo que consigue la principal sola».
"""

from __future__ import annotations

import ast
import pathlib
import uuid

import pytest
from sqlalchemy import delete

from app.discovery.primary_seed_resolver import (
    FUENTES_DEL_NAVEGADOR,
    ORIGEN,
    PrimarySeedResolver,
)
from app.history.seed_collector import RecentSeedCollector
from app.models import Chat, ChatHistoryState, HistorySeed, Message, WhatsAppAccount

CLAVE = "Contrasena-De-Prueba-1"
ID_REAL = "3EB0C767D82B0F2B1234"


def _correo() -> str:
    return f"psr-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
def escenario(runtime, session):
    """Una cuenta con un chat, listo para recibir mensajes y anclas."""
    session.execute(delete(WhatsAppAccount))
    session.flush()
    runtime._montar_cuentas()

    inicio = runtime.auth.register(email=_correo(), password=CLAVE)
    cuenta = WhatsAppAccount(
        user_id=inicio.user_id,
        session_status="linked",
        session_storage_key=f"users/{inicio.user_id}",
    )
    session.add(cuenta)
    session.flush()

    class _Cola:
        def __init__(self):
            self.encolados = []

        def enqueue(self, jids):
            self.encolados.extend(jids)

    cola = _Cola()
    colector = RecentSeedCollector(
        runtime.database,
        user_id=inicio.user_id,
        account_id=cuenta.id,
        seed_queue=cola,
    )
    return {
        "runtime": runtime,
        "session": session,
        "cuenta": cuenta,
        "user_id": inicio.user_id,
        "colector": colector,
        "cola": cola,
    }


def _chat(escenario, *, jid=None, esperando=True):
    session, cuenta = escenario["session"], escenario["cuenta"]
    chat = Chat(
        jid=jid or f"5735{uuid.uuid4().hex[:8]}@s.whatsapp.net",
        chat_type="group" if (jid or "").endswith("@g.us") else "individual",
        whatsapp_account_id=cuenta.id,
    )
    session.add(chat)
    session.flush()
    if esperando:
        session.add(
            ChatHistoryState(
                chat_id=chat.id, chat_jid=chat.jid, history_status="waiting_seed"
            )
        )
        session.flush()
    return chat


def _mensaje(escenario, chat, *, wamid=ID_REAL, ts=1_760_000_000, source="on_demand"):
    session = escenario["session"]
    mensaje = Message(
        chat_id=chat.id,
        chat_jid=chat.jid,
        whatsapp_message_id=wamid,
        message_type="text",
        timestamp=ts,
        from_me=False,
        source=source,
    )
    session.add(mensaje)
    session.flush()
    return mensaje


def _ancla(escenario, chat, *, source, wamid=None):
    session = escenario["session"]
    session.add(
        HistorySeed(
            user_id=escenario["user_id"],
            whatsapp_account_id=escenario["cuenta"].id,
            chat_id=chat.id,
            chat_jid=chat.jid,
            wa_msg_id=wamid or f"3EB0{uuid.uuid4().hex[:16].upper()}",
            timestamp=1_759_000_000,
            from_me=False,
            source=source,
        )
    )
    session.flush()


def _resolutor(escenario):
    return PrimarySeedResolver(
        escenario["runtime"].database, account_id=escenario["cuenta"].id
    )


# ---------------------------------------------------------------------------
# Qué se considera pendiente
# ---------------------------------------------------------------------------


def test_un_chat_con_ancla_propia_no_es_pendiente(escenario):
    chat = _chat(escenario)
    _ancla(escenario, chat, source="initial_bootstrap")
    escenario["session"].commit()
    pendientes = _resolutor(escenario).chats_sin_ancla_propia(escenario["session"])
    assert chat.jid not in [j for _i, j in pendientes]


@pytest.mark.parametrize("fuente", sorted(FUENTES_DEL_NAVEGADOR))
def test_un_ancla_del_navegador_NO_cuenta_como_propia(escenario, fuente):
    """LA DISTINCION QUE DECIDE LA FASE.

    Una conversacion cuya unica referencia la trajo el segundo dispositivo
    sigue dependiendo de el. Contarla como resuelta escondería justo lo que se
    quiere dejar de necesitar.
    """
    chat = _chat(escenario)
    _ancla(escenario, chat, source=fuente)
    escenario["session"].commit()
    pendientes = _resolutor(escenario).chats_sin_ancla_propia(escenario["session"])
    assert chat.jid in [j for _i, j in pendientes]


# ---------------------------------------------------------------------------
# Qué se acepta como referencia
# ---------------------------------------------------------------------------


def test_se_encuentra_el_mensaje_real_guardado(escenario):
    chat = _chat(escenario)
    _mensaje(escenario, chat)
    escenario["session"].commit()
    candidato = _resolutor(escenario).referencia_de(escenario["session"], chat.jid)
    assert candidato is not None
    assert candidato.wa_msg_id == ID_REAL
    assert candidato.timestamp == 1_760_000_000
    assert candidato.source == ORIGEN


def test_se_elige_el_mensaje_MAS_RECIENTE(escenario):
    """La excavacion va hacia atras: anclar en el mas nuevo deja todo delante.

    Anclar en el mas viejo dejaria fuera justo lo que todavia no se tiene.
    """
    chat = _chat(escenario)
    _mensaje(escenario, chat, wamid="3EB0VIEJO000000000AA", ts=1_700_000_000)
    _mensaje(escenario, chat, wamid="3EB0NUEVO000000000BB", ts=1_760_000_000)
    escenario["session"].commit()
    candidato = _resolutor(escenario).referencia_de(escenario["session"], chat.jid)
    assert candidato.wa_msg_id == "3EB0NUEVO000000000BB"


def test_sin_mensajes_reales_no_hay_referencia(escenario):
    chat = _chat(escenario)
    escenario["session"].commit()
    assert _resolutor(escenario).referencia_de(escenario["session"], chat.jid) is None


def test_un_mensaje_sin_identificador_real_no_sirve(escenario):
    """PROHIBIDO inventar. Sin WAMID no hay ancla, y punto."""
    chat = _chat(escenario)
    _mensaje(escenario, chat, wamid=None)
    escenario["session"].commit()
    assert _resolutor(escenario).referencia_de(escenario["session"], chat.jid) is None


def test_un_identificador_fabricado_por_nosotros_no_sirve(escenario):
    """Recibe confirmacion del servidor y despues silencio. Se rechaza aqui."""
    from app.services.repository import SYNTHETIC_PREFIXES

    for prefijo in SYNTHETIC_PREFIXES:
        chat = _chat(escenario)
        _mensaje(escenario, chat, wamid=f"{prefijo}{uuid.uuid4().hex[:10]}")
        escenario["session"].commit()
        assert _resolutor(escenario).referencia_de(escenario["session"], chat.jid) is None


def test_una_marca_invalida_no_sirve(escenario):
    chat = _chat(escenario)
    _mensaje(escenario, chat, ts=0)
    escenario["session"].commit()
    assert _resolutor(escenario).referencia_de(escenario["session"], chat.jid) is None


def test_un_mensaje_de_fuente_no_autenticada_no_sirve(escenario):
    """Solo historial inicial, excavacion y tiempo real. Nada mas."""
    chat = _chat(escenario)
    _mensaje(escenario, chat, source="unknown")
    escenario["session"].commit()
    assert _resolutor(escenario).referencia_de(escenario["session"], chat.jid) is None


def test_la_referencia_es_del_chat_que_se_pide(escenario):
    """PROHIBIDO prestar una referencia de otra conversacion."""
    uno, otro = _chat(escenario), _chat(escenario)
    _mensaje(escenario, uno, wamid="3EB0DELUNO0000000AAA")
    escenario["session"].commit()
    assert _resolutor(escenario).referencia_de(escenario["session"], otro.jid) is None


def test_un_grupo_se_ancla_con_su_propio_mensaje(escenario):
    grupo = _chat(escenario, jid=f"12036{uuid.uuid4().hex[:12]}@g.us")
    _mensaje(escenario, grupo, wamid="3EB0DELGRUPO00000AAA")
    escenario["session"].commit()
    candidato = _resolutor(escenario).referencia_de(escenario["session"], grupo.jid)
    assert candidato.chat_jid == grupo.jid
    assert candidato.wa_msg_id == "3EB0DELGRUPO00000AAA"


# ---------------------------------------------------------------------------
# La promoción, por el camino de siempre
# ---------------------------------------------------------------------------


def test_lo_encontrado_se_promueve_y_despierta_el_chat(escenario):
    """§20 y §21: mismo colector, misma cola, sin pulsar nada."""
    chat = _chat(escenario)
    _mensaje(escenario, chat)
    escenario["session"].commit()

    resultado = _resolutor(escenario).resolver(escenario["colector"])

    assert resultado.encontrados_en_mensajes == 1
    assert resultado.promovidos == 1
    assert resultado.esperando_despues < resultado.esperando_antes
    assert chat.jid in escenario["cola"].encolados


def test_no_se_crean_filas_por_un_camino_paralelo(escenario, session):
    """La fila la escribe el colector, con su fuente. No hay insert propio."""
    chat = _chat(escenario)
    _mensaje(escenario, chat)
    session.commit()

    _resolutor(escenario).resolver(escenario["colector"])
    session.expire_all()

    anclas = session.query(HistorySeed).filter(HistorySeed.chat_jid == chat.jid).all()
    assert len(anclas) == 1
    assert anclas[0].wa_msg_id == ID_REAL
    assert anclas[0].source == ORIGEN


def test_resolver_dos_veces_no_duplica(escenario, session):
    chat = _chat(escenario)
    _mensaje(escenario, chat)
    session.commit()

    resolutor = _resolutor(escenario)
    resolutor.resolver(escenario["colector"])
    segundo = resolutor.resolver(escenario["colector"])
    session.expire_all()

    assert segundo.promovidos == 0
    assert session.query(HistorySeed).filter(HistorySeed.chat_jid == chat.jid).count() == 1


def test_un_chat_que_ya_tenia_ancla_propia_no_se_toca(escenario, session):
    chat = _chat(escenario, esperando=False)
    _ancla(escenario, chat, source="initial_bootstrap", wamid="3EB0YAESTABA00000AAA")
    _mensaje(escenario, chat, wamid="3EB0OTRODISTINTO0AAA")
    session.commit()

    _resolutor(escenario).resolver(escenario["colector"])
    session.expire_all()

    anclas = session.query(HistorySeed).filter(HistorySeed.chat_jid == chat.jid).all()
    assert [a.wa_msg_id for a in anclas] == ["3EB0YAESTABA00000AAA"]


def test_el_recuento_cuadra(escenario, session):
    """§25: lo que habia, lo que se encontro y lo que quedo."""
    con_mensaje = _chat(escenario)
    _mensaje(escenario, con_mensaje)
    _chat(escenario)  # sin nada: no se puede resolver
    session.commit()

    resultado = _resolutor(escenario).resolver(escenario["colector"])

    assert resultado.conocidos == 2
    assert resultado.esperando_antes == 2
    assert resultado.encontrados_en_mensajes == 1
    assert resultado.esperando_despues == 1
    assert resultado.to_json()["waiting_after"] == 1


# ---------------------------------------------------------------------------
# Guardias de código (§19)
# ---------------------------------------------------------------------------


def _sin_docstrings(ruta: str) -> ast.Module:
    arbol = ast.parse(pathlib.Path(ruta).read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        cuerpo = getattr(nodo, "body", None)
        if (
            isinstance(cuerpo, list)
            and cuerpo
            and isinstance(cuerpo[0], ast.Expr)
            and isinstance(cuerpo[0].value, ast.Constant)
            and isinstance(cuerpo[0].value.value, str)
        ):
            cuerpo[0].value.value = ""
    return arbol


def test_el_resolutor_NO_puede_mirar_al_navegador():
    """LA REGLA QUE HACE QUE LA MEDIDA SIGNIFIQUE ALGO.

    Si pudiera consultar el almacen del navegador, «lo que consigue la sesion
    principal sola» dejaria de ser lo que dice medir. Las fuentes web solo
    pueden aparecer para EXCLUIRLAS.
    """
    arbol = _sin_docstrings("app/discovery/primary_seed_resolver.py")
    llamadas = {
        nodo.func.attr
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)
    }
    nombres = {
        nodo.id for nodo in ast.walk(arbol) if isinstance(nodo, ast.Name)
    } | llamadas
    for prohibido in ("WebCompanionSupervisor", "web_inventory", "enviar", "probe"):
        assert prohibido not in nombres, f"el resolutor no puede usar {prohibido}"


def test_el_resolutor_no_fabrica_identificadores():
    """Nada de uuid, hash ni cadenas construidas donde va un WAMID."""
    arbol = _sin_docstrings("app/discovery/primary_seed_resolver.py")
    nombres = {nodo.id for nodo in ast.walk(arbol) if isinstance(nodo, ast.Name)}
    for prohibido in ("uuid", "uuid4", "hashlib", "secrets", "random"):
        assert prohibido not in nombres, f"el resolutor no puede usar {prohibido}"


def test_el_resolutor_no_escribe_en_la_base():
    """Escribe el colector, que es quien valida. Aqui no hay ni un insert."""
    codigo = ast.unparse(
        _sin_docstrings("app/discovery/primary_seed_resolver.py")
    ).lower()
    for prohibido in ("session.add", "sesion.add", "insert(", "update(", "delete("):
        assert prohibido not in codigo, f"el resolutor no puede hacer {prohibido}"
