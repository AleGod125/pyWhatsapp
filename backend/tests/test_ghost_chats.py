"""Una conversación que falta en una foto NO es una conversación borrada.

LO QUE SE MIDIO
---------------
Dos arranques de la MISMA cuenta trajeron 41 y 39 conversaciones. Las fotos de
WhatsApp vienen incompletas a veces, sin avisar y sin motivo aparente.

Si faltar en una foto bastara para borrar, dos pasadas seguidas se llevarían
por delante historial que costó horas recuperar. Por eso una ausencia sólo se
anota, y hacen falta varias seguidas para llamar dudosa a una conversación.

LO QUE PROTEGEN ESTAS PRUEBAS
-----------------------------
Sobre todo la última: **que nada borre una conversación**. Marcar es
reversible; borrar no se deshace.
"""

from __future__ import annotations

import ast
import pathlib
import uuid

import pytest
from sqlalchemy import delete

from app.models import Chat, WhatsAppAccount
from app.services.ghost_chats import (
    AUSENCIAS,
    AUSENCIAS_PARA_DUDOSA,
    DUDOSA,
    anotar_snapshot,
    es_dudosa,
)

CLAVE = "Contrasena-De-Prueba-1"


@pytest.fixture
def escenario(runtime, session):
    session.execute(delete(WhatsAppAccount))
    session.flush()
    runtime._montar_cuentas()
    inicio = runtime.auth.register(
        email=f"gh-{uuid.uuid4().hex[:10]}@example.com", password=CLAVE
    )
    cuenta = WhatsAppAccount(
        user_id=inicio.user_id,
        session_status="linked",
        session_storage_key=f"users/{inicio.user_id}",
    )
    session.add(cuenta)
    session.flush()

    jids = [f"3460000{n:04d}@s.whatsapp.net" for n in range(3)]
    for jid in jids:
        session.add(
            Chat(jid=jid, chat_type="individual", whatsapp_account_id=cuenta.id)
        )
    session.flush()
    return {"session": session, "cuenta": cuenta, "jids": jids}


def _chat(escenario, jid: str) -> Chat:
    return (
        escenario["session"].query(Chat).filter(Chat.jid == jid).one()
    )


def _anotar(escenario, vistos, veces: int = 1):
    for _ in range(veces):
        anotar_snapshot(
            escenario["session"], vistos, account_id=escenario["cuenta"].id
        )
        escenario["session"].flush()


# ---------------------------------------------------------------------------
# Una ausencia no basta
# ---------------------------------------------------------------------------


def test_una_sola_ausencia_no_marca_nada(escenario):
    """LA REGLA. Una foto incompleta es un suceso normal."""
    jids = escenario["jids"]
    _anotar(escenario, jids[:2])  # falta el tercero

    ausente = _chat(escenario, jids[2])
    assert not es_dudosa(ausente)
    assert (ausente.raw_metadata or {}).get(AUSENCIAS) == 1


def test_hacen_falta_varias_seguidas(escenario):
    jids = escenario["jids"]
    for vuelta in range(1, AUSENCIAS_PARA_DUDOSA):
        _anotar(escenario, jids[:2])
        assert not es_dudosa(_chat(escenario, jids[2])), f"vuelta {vuelta}"

    _anotar(escenario, jids[:2])
    assert es_dudosa(_chat(escenario, jids[2]))


def test_volver_a_aparecer_quita_la_duda(escenario):
    """La duda se quita con la misma facilidad con la que se puso."""
    jids = escenario["jids"]
    _anotar(escenario, jids[:2], veces=AUSENCIAS_PARA_DUDOSA)
    assert es_dudosa(_chat(escenario, jids[2]))

    _anotar(escenario, jids)  # aparece otra vez
    recuperado = _chat(escenario, jids[2])
    assert not es_dudosa(recuperado)
    assert (recuperado.raw_metadata or {}).get(AUSENCIAS) == 0


def test_una_foto_vacia_no_castiga_a_nadie(escenario, cuenta):
    """Si la pasada no trajo nada, lo que fallo fue la pasada.

    Castigar a las 41 conversaciones por eso es exactamente el error que esta
    capa existe para evitar.
    """
    jids = escenario["jids"]
    resultado = anotar_snapshot(
        escenario["session"], [], account_id=escenario["cuenta"].id
    )
    escenario["session"].flush()

    assert resultado.ausentes == 0
    for jid in jids:
        assert (_chat(escenario, jid).raw_metadata or {}).get(AUSENCIAS) in (None, 0)


def test_las_que_aparecen_se_anotan_como_vistas(escenario, cuenta):
    jids = escenario["jids"]
    resultado = anotar_snapshot(
        escenario["session"], jids, account_id=escenario["cuenta"].id
    )
    escenario["session"].flush()
    assert resultado.vistas == 3
    assert resultado.ausentes == 0


def test_el_recuento_cuadra(escenario, cuenta):
    jids = escenario["jids"]
    _anotar(escenario, jids[:2], veces=AUSENCIAS_PARA_DUDOSA)
    resultado = anotar_snapshot(
        escenario["session"], jids[:2], account_id=escenario["cuenta"].id
    )
    assert resultado.vistas == 2
    assert resultado.ausentes == 1
    assert resultado.dudosas_totales == 1
    assert resultado.to_json()["stale_total"] == 1


# ---------------------------------------------------------------------------
# LO QUE NO SE HACE
# ---------------------------------------------------------------------------


def test_NADIE_BORRA_UNA_CONVERSACION(escenario, cuenta):
    """La prueba que mas importa.

    Diez fotos seguidas sin una conversacion: sigue ahi, con su historial.
    Marcar es reversible; borrar no se deshace.
    """
    jids = escenario["jids"]
    _anotar(escenario, jids[:2], veces=10)

    quedan = (
        escenario["session"]
        .query(Chat)
        .filter(Chat.whatsapp_account_id == escenario["cuenta"].id)
        .count()
    )
    assert quedan == 3
    assert _chat(escenario, jids[2]) is not None


def test_el_modulo_no_puede_borrar_nada():
    """Guardia de codigo: aqui no entra un `delete` ni por descuido."""
    arbol = ast.parse(
        pathlib.Path("app/services/ghost_chats.py").read_text(encoding="utf-8")
    )
    # Se quitan los textos: los comentarios hablan de borrar para explicar por
    # que NO se borra.
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
    codigo = ast.unparse(arbol).lower()
    for prohibido in ("delete(", ".delete", "drop ", "truncate"):
        assert prohibido not in codigo, f"prohibido: {prohibido}"
