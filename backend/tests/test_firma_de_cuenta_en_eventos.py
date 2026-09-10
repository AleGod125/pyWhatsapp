"""Cada evento del worker dice de que cuenta es, y se comprueba.

LA RAIZ QUE ESTO CIERRA
-----------------------
El worker de Baileys no firmaba nada. Python decidia el dueno de cada mensaje
con ``runtime_owner_account_id``, un campo EN MEMORIA del runtime que posee el
proceso. Mientras ese campo este bien, todo cuadra. Cuando se equivoca, **nada
lo detecta**: lo que acaba en PostgreSQL es un ``chat_id`` perfectamente valido
de la cuenta equivocada, y ninguna restriccion de la base puede cuestionarlo
porque la base no sabe que socket escribio.

Se midio: 195 conversaciones de un telefono entraron bajo la cuenta de otro,
catorce segundos despues de vincular el segundo movil. La fila afectada acabo
ademas con el ``wa_pn`` y el ``wa_lid`` del telefono ajeno.

CON LA FIRMA
------------
El dato dice de quien es. Si no coincide con lo que el runtime cree, se
descarta y se anota. Ese mismo fallo habria dejado 195 lineas
``ATRIBUCION_MISMATCH`` en el log y cero datos cruzados.

Se firma en ``protocol.js/emitir``, que es **la unica funcion que escribe en
stdout**, y no en cada llamada repartida por el worker: asi no existe la
posibilidad de anadir un evento nuevo y olvidarse de firmarlo.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest


RAIZ_WORKER = Path(__file__).resolve().parents[1] / "wa_baileys"


# ---------------------------------------------------------------------------
# 1. El worker firma, y sin cuenta no arranca
# ---------------------------------------------------------------------------


def _node_disponible() -> bool:
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=20)
        return True
    except Exception:  # noqa: BLE001
        return False


sin_node = pytest.mark.skipif(not _node_disponible(), reason="Node no disponible")


@sin_node
def test_cada_evento_sale_firmado():
    """La firma la pone el unico sitio que escribe en stdout."""
    guion = (
        "process.env.WA_ACCOUNT_ID='cuenta-de-prueba';"
        "const {emitir}=require('./protocol.js');"
        "emitir({event:'uno'});"
        "emitir({event:'dos', dato:42});"
    )
    salida = subprocess.run(
        ["node", "-e", guion],
        cwd=str(RAIZ_WORKER),
        capture_output=True,
        text=True,
        timeout=60,
    )

    lineas = [json.loads(l) for l in salida.stdout.splitlines() if l.strip()]
    assert len(lineas) == 2
    for evento in lineas:
        assert evento["wa_account_id"] == "cuenta-de-prueba", (
            "un evento salio sin firmar: el supervisor tendria que suponer "
            "de quien es"
        )


@sin_node
def test_la_firma_no_pisa_el_contenido():
    """Firmar no puede cambiar lo que el evento ya decia."""
    guion = (
        "process.env.WA_ACCOUNT_ID='c1';"
        "const {emitir}=require('./protocol.js');"
        "emitir({event:'client', name:'messages.upsert', payload:{a:1}});"
    )
    salida = subprocess.run(
        ["node", "-e", guion],
        cwd=str(RAIZ_WORKER),
        capture_output=True,
        text=True,
        timeout=60,
    )

    evento = json.loads(salida.stdout.splitlines()[0])
    assert evento["event"] == "client"
    assert evento["name"] == "messages.upsert"
    assert evento["payload"] == {"a": 1}
    assert evento["wa_account_id"] == "c1"


@sin_node
def test_sin_cuenta_el_worker_se_niega_a_arrancar():
    """Arrancar sin firma obligaria a elegir entre perder datos o creerselos."""
    # El entorno ENTERO con la variable vacia, no uno minimo: en Windows,
    # Node sin `SystemRoot` revienta con 134 antes de llegar a comprobar nada,
    # y esta prueba estaria pasando por el motivo equivocado.
    import os

    entorno = {**os.environ, "WA_ACCOUNT_ID": ""}
    salida = subprocess.run(
        ["node", "worker.js"],
        cwd=str(RAIZ_WORKER),
        capture_output=True,
        text=True,
        timeout=60,
        env=entorno,
        stdin=subprocess.DEVNULL,
    )

    assert salida.returncode == 1, "arranco sin saber de que cuenta es"
    assert "WA_ACCOUNT_ID" in salida.stderr


def test_la_firma_se_pone_en_UN_solo_sitio():
    """Si hubiera dos, uno acabaria olvidandose.

    `emitir` es la unica funcion que escribe en stdout --lo dice su propio
    comentario-- y por eso es el sitio correcto para firmar.
    """
    fuente = (RAIZ_WORKER / "protocol.js").read_text(encoding="utf-8")

    assert "wa_account_id: WA_ACCOUNT_ID" in fuente
    assert fuente.count("process.stdout.write") == 1, (
        "hay mas de un sitio escribiendo en stdout: uno puede salir sin firmar"
    )


# ---------------------------------------------------------------------------
# 2. Python comprueba la firma antes de procesar
# ---------------------------------------------------------------------------


@pytest.fixture
def cliente_falso(settings):
    """Un `BaileysClient` sin proceso, solo para la comprobacion de firma."""
    from app.wa.baileys_client import BaileysClient

    c = object.__new__(BaileysClient)
    c._settings = settings
    c.whatsapp_account_id = uuid.uuid4()
    return c


def test_un_evento_de_OTRA_cuenta_se_descarta(cliente_falso, caplog):
    """El caso medido: 195 conversaciones bajo la cuenta equivocada."""
    ajena = str(uuid.uuid4())

    with caplog.at_level("ERROR"):
        pasa = cliente_falso._firma_correcta(
            {"event": "client", "wa_account_id": ajena}
        )

    assert pasa is False
    assert "ATRIBUCION_MISMATCH" in caplog.text


def test_el_evento_propio_pasa(cliente_falso):
    assert (
        cliente_falso._firma_correcta(
            {"event": "client", "wa_account_id": str(cliente_falso.whatsapp_account_id)}
        )
        is True
    )


def test_un_evento_SIN_firmar_se_descarta(cliente_falso, caplog):
    """Un worker anterior a la firma. Antes que atribuirlo a ciegas, fuera."""
    with caplog.at_level("ERROR"):
        pasa = cliente_falso._firma_correcta({"event": "client"})

    assert pasa is False
    assert "ATRIBUCION_SIN_FIRMA" in caplog.text


def test_sin_cuenta_en_el_cliente_no_se_bloquea_nada(cliente_falso):
    """No se inventa un dueno: se deja pasar y decide quien atribuye.

    `start()` ya impide lanzar un worker sin cuenta, asi que esto solo cubre
    un cliente antiguo que siguiera vivo.
    """
    cliente_falso.whatsapp_account_id = None

    assert cliente_falso._firma_correcta({"event": "client"}) is True


def test_la_comprobacion_esta_ANTES_de_procesar():
    """Si estuviera despues, el evento ajeno ya se habria escrito."""
    import inspect

    from app.wa.baileys_client import BaileysClient

    fuente = inspect.getsource(BaileysClient._leer_salida)

    assert "_firma_correcta" in fuente
    assert fuente.index("_firma_correcta") < fuente.index("self._procesar")


# ---------------------------------------------------------------------------
# 3. La cuenta llega al worker
# ---------------------------------------------------------------------------


def test_la_cuenta_viaja_en_el_entorno(settings):
    from app.wa.baileys_client import BaileysClient

    c = object.__new__(BaileysClient)
    c._settings = settings
    ident = uuid.uuid4()
    c.whatsapp_account_id = ident

    assert c._entorno()["WA_ACCOUNT_ID"] == str(ident)


def test_sin_cuenta_no_se_lanza_el_worker(settings, caplog, tmp_path):
    """Mejor un mensaje que explique que un proceso que muere al nacer."""
    import threading

    from app.wa.baileys_client import BaileysClient

    c = object.__new__(BaileysClient)
    c._settings = settings
    c._candado = threading.Lock()
    c._proceso = None
    c.whatsapp_account_id = None

    with caplog.at_level("ERROR"):
        c.start()

    assert c._proceso is None, "se lanzo un worker cuya salida no se puede atribuir"
    assert "no se sabe de que cuenta" in caplog.text
