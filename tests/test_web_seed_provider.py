"""El proveedor auxiliar solo produce semillas. Nunca historial.

POR QUE EXISTE ESTE COMPONENTE
------------------------------
33 conversaciones llegaron sin un solo identificador de mensaje, y sin uno
``HISTORY_SYNC_ON_DEMAND`` no puede pedir nada. Se agotaron las fuentes
nativas, con medidas::

    INITIAL_BOOTSTRAP            sin identificador (auditado campo a campo)
    blobs de History Sync        sin identificador
    PostgreSQL                   sin identificador
    alias PN/LID                 sin identificador
    app-state incremental        0 claves en 61 mutaciones
    app-state snapshot COMPLETO  0 claves en 93 mutaciones

QUE FIJAN ESTAS PRUEBAS
-----------------------
La frontera. Este componente vincula un dispositivo adicional a la cuenta del
usuario, asi que lo que puede y no puede hacer no es una cuestion de estilo:

* NO escribe mensajes ni multimedia;
* NO comparte criptografia con pywhats;
* su sesion se borra sin tocar la principal;
* y una clave que no se pueda demostrar del chat correcto se rechaza.

Si un dia sobra, se elimina entero y el sistema sigue igual.
"""

from __future__ import annotations

import pytest

from app.experimental.web_seed_provider import WebSeed, WebSeedProvider

ISAAC = "64940106866902@lid"
ISAAC_PN = "573243116421@s.whatsapp.net"
OTRO = "99911122233@lid"
ID_REAL = "3A1F8BDD4678EB6DE395"


@pytest.fixture
def proveedor(settings, tmp_path):
    import dataclasses

    aislado = dataclasses.replace(settings, session_dir=tmp_path / "session")
    (tmp_path / "session").mkdir(parents=True, exist_ok=True)
    return WebSeedProvider(aislado)


def _crudo(**cambios):
    base = {
        "remote_jid": ISAAC,
        "message_id": ID_REAL,
        "from_me": False,
        "timestamp": 1_788_400_000,
        "participant": None,
    }
    base.update(cambios)
    return base


# ---------------------------------------------------------------------------
# Validacion de la semilla
# ---------------------------------------------------------------------------


def test_una_clave_real_se_acepta(proveedor):
    semilla = proveedor.validate(_crudo(), {ISAAC})
    assert semilla is not None
    assert semilla.remote_jid == ISAAC
    assert semilla.message_id == ID_REAL
    assert semilla.source == "web_bootstrap"


@pytest.mark.parametrize("falso", ["opaque-1", "sintetico", "", "1788400000", "xx"])
def test_un_identificador_que_no_sirve_de_ancla_se_rechaza(proveedor, falso):
    """El filtro es el MISMO que usa el backfill para anclarse."""
    assert proveedor.validate(_crudo(message_id=falso), {ISAAC}) is None


def test_una_clave_de_otro_chat_se_rechaza(proveedor):
    """Anclar un chat con el mensaje de otro corromperia su historial."""
    assert proveedor.validate(_crudo(remote_jid=OTRO), {ISAAC}) is None


def test_el_mismo_contacto_por_telefono_se_acepta(proveedor):
    """Telefono y LID son el mismo contacto, no dos chats."""
    semilla = proveedor.validate(_crudo(remote_jid=ISAAC_PN), {ISAAC_PN, ISAAC})
    assert semilla is not None


@pytest.mark.parametrize(
    "jid", ["status@broadcast", "123@broadcast", "abc@newsletter"]
)
def test_estados_y_canales_se_rechazan(proveedor, jid):
    """No son conversaciones y su historial no se pide igual."""
    assert proveedor.validate(_crudo(remote_jid=jid), {jid}) is None


def test_sin_marca_de_tiempo_real_se_rechaza(proveedor):
    """``ON_DEMAND`` necesita id Y timestamp: uno solo no ancla nada."""
    assert proveedor.validate(_crudo(timestamp=0), {ISAAC}) is None
    assert proveedor.validate(_crudo(timestamp=None), {ISAAC}) is None


def test_from_me_y_timestamp_se_conservan(proveedor):
    semilla = proveedor.validate(
        _crudo(from_me=True, timestamp=1_787_000_123), {ISAAC}
    )
    assert semilla.from_me is True
    assert semilla.timestamp == 1_787_000_123


def test_no_se_registra_el_identificador_completo():
    """Se registra una huella corta, nunca el identificador."""
    semilla = WebSeed(
        remote_jid=ISAAC, message_id=ID_REAL, from_me=False, timestamp=1
    )
    cuerpo = semilla.to_json()
    assert ID_REAL not in str(cuerpo)
    assert cuerpo["message_id_fp"] == semilla.huella
    assert len(semilla.huella) == 8
    # Y el JID va enmascarado: un identificador completo es un telefono.
    assert "64940106866902" not in str(cuerpo)


# ---------------------------------------------------------------------------
# La frontera: solo semillas
# ---------------------------------------------------------------------------


def test_el_proveedor_no_escribe_mensajes_ni_multimedia():
    """La condicion que impide que esto crezca hasta ser otro backup."""
    import ast
    from pathlib import Path

    arbol = ast.parse(
        Path("app/experimental/web_seed_provider.py").read_text(encoding="utf-8")
    )
    nombres = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Attribute):
            nombres.append(nodo.attr)
        elif isinstance(nodo, ast.Name):
            nombres.append(nodo.id)

    for prohibido in (
        "Message",
        "MediaFile",
        "bulk_upsert_messages",
        "ingest_history_sync",
        "insert",
        "commit",
    ):
        assert prohibido not in nombres, (
            f"el proveedor auxiliar no puede tocar {prohibido}: solo produce "
            f"semillas"
        )


def test_el_componente_node_no_guarda_historial():
    """Tampoco del lado Node: una clave por chat y se apaga."""
    from pathlib import Path

    fuente = Path("web_bootstrap/seed.js").read_text(encoding="utf-8")
    # Se apaga solo en cuanto no queda nada que buscar.
    assert "pendientes.size === 0" in fuente
    assert "terminar(" in fuente
    # No descarga contenido.
    for prohibido in ("downloadMediaMessage", "downloadContentFromMessage"):
        assert prohibido not in fuente
    # Y no manda nada: es un oyente.
    assert "sendMessage" not in fuente


def test_el_auxiliar_no_marca_nada_como_leido():
    """Se conecta a la cuenta real del usuario: no puede alterar sus chats."""
    from pathlib import Path

    fuente = Path("web_bootstrap/seed.js").read_text(encoding="utf-8")
    assert "markOnlineOnConnect: false" in fuente
    for prohibido in ("readMessages", "chatModify", "sendReceipt"):
        assert prohibido not in fuente


# ---------------------------------------------------------------------------
# Sesion aislada
# ---------------------------------------------------------------------------


def test_la_sesion_auxiliar_es_otra(proveedor, settings):
    """Son dos vinculaciones distintas: mezclarlas corrompe las dos."""
    principal = proveedor._settings.session_file
    assert proveedor.session_dir != principal.parent
    assert proveedor.session_dir.name == "web_bootstrap"
    assert str(principal) not in str(proveedor.session_dir)


def test_borrar_la_auxiliar_no_toca_la_principal(proveedor):
    """Tiene que poder quitarse sin consecuencias."""
    import json

    principal = proveedor._settings.session_file
    principal.parent.mkdir(parents=True, exist_ok=True)
    principal.write_text(json.dumps({"jid": {"user": "x"}}), encoding="utf-8")

    proveedor.session_dir.mkdir(parents=True, exist_ok=True)
    (proveedor.session_dir / "creds.json").write_text("{}", encoding="utf-8")
    assert proveedor.linked() is True

    assert proveedor.forget() is True
    assert not proveedor.session_dir.exists()
    assert principal.exists(), "la sesion principal NO puede tocarse"


def test_borrar_cuando_no_hay_nada_no_falla(proveedor):
    assert proveedor.forget() is False


def test_no_se_copia_criptografia_entre_sesiones():
    """Son sesiones Signal distintas: copiar entre ellas corrompe ambas."""
    import ast
    from pathlib import Path

    arbol = ast.parse(
        Path("app/experimental/web_seed_provider.py").read_text(encoding="utf-8")
    )
    llamadas = [
        getattr(n.func, "attr", None) or getattr(n.func, "id", None)
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
    ]
    for prohibido in ("copy", "copy2", "copytree", "copyfile"):
        assert prohibido not in llamadas


# ---------------------------------------------------------------------------
# Disponibilidad y errores
# ---------------------------------------------------------------------------


def test_sin_dependencias_lo_dice_en_vez_de_reventar(proveedor, tmp_path):
    disponible, motivo = proveedor.available()
    assert disponible is False
    assert motivo and ("seed.js" in motivo or "npm install" in motivo)


def test_el_componente_esta_instalado_de_verdad(settings):
    """En el proyecto real, no en el temporal de la prueba."""
    proveedor = WebSeedProvider(settings)
    disponible, motivo = proveedor.available()
    assert disponible is True, motivo


def test_un_fallo_del_auxiliar_no_lanza(proveedor):
    """El servicio principal no puede caerse porque el auxiliar falle.

    Aqui falta ``seed.js`` a proposito (el proveedor apunta a un directorio
    temporal), asi que esto ejerce la ruta de "no se pudo ni arrancar".
    """
    resultado = proveedor.run([{"chat_id": 1, "jids": [ISAAC]}])
    assert resultado == {}


def test_se_buscan_todos_los_chats_en_UNA_conexion(proveedor):
    """Un proceso por chat serian treinta vinculaciones seguidas.

    El historial llega junto de todas formas: se aprovecha esa unica entrega.
    """
    import inspect

    firma = inspect.signature(WebSeedProvider.run)
    assert "targets" in firma.parameters, "recibe la lista entera, no un chat"

    fuente = inspect.getsource(WebSeedProvider.run)
    assert "--targets" in fuente


def test_el_proceso_se_cierra_siempre():
    """Un auxiliar que se queda vivo deja de ser efimero."""
    import inspect

    fuente = inspect.getsource(WebSeedProvider.run)
    assert "finally" in fuente
    assert "_cerrar" in fuente
    assert "Timer" in fuente, "hace falta un vigilante por si no termina solo"
