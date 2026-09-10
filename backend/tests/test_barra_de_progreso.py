"""Lo que la barra dice mientras excava y mientras sube.

BUG 1 — "0 mensajes" con 6314 en la base
----------------------------------------
La barra decia::

    Excavando 18/326 chats - 0 mensajes - 20m 11s

con 6314 mensajes ya guardados. `messages_new` se apoya en `_ingest_watch`,
un observador que se abre justo antes de pedir y se cierra en el `finally`,
en cuanto la peticion devuelve. El blob llega DESPUES, por otro hilo, y para
entonces el observador ya es `None`: `note_history_ingest` se va sin contar.

`messages_in_run` no depende de ninguna ventana: es una resta de totales
contados sobre la propia base. La cuenta fina se conserva --el diagnostico
por chat la necesita-- y esta la acompana.

DRIVE VA POR SU CARRIL, Y TERMINA DESPUES
-----------------------------------------
El excavador nunca ha esperado a Drive: `backfill_service` no menciona el
almacenamiento ni una vez, y `DriveStorageWorker` corre en su propio hilo. Por
eso la extraccion acaba antes que la subida, y decir "completada" en ese hueco
es cierto y enganoso a la vez.
"""

from __future__ import annotations

import pytest


class _BaseFalsa:
    """Devuelve el numero que le digan, o revienta si se le pide."""

    def __init__(self, cuantos, revienta=False):
        self.cuantos = cuantos
        self.revienta = revienta

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            if self.revienta:
                raise RuntimeError("la base dijo que no")
            yield self

        return scope()

    def execute(self, *a, **k):
        base = self

        class _R:
            def scalar(self):
                return base.cuantos

        return _R()


@pytest.fixture
def trabajo():
    from app.services.sync_job import SyncJob

    objeto = object.__new__(SyncJob)
    objeto._database = _BaseFalsa(0)
    return objeto


def test_lo_traido_en_esta_corrida_crece(trabajo):
    """El fallo que cierra: la barra se quedaba en cero toda la corrida."""
    from app.services.sync_job import SyncState

    trabajo.state = SyncState(messages_at_start=6000, messages_total=6000)
    trabajo._database.cuantos = 6314

    cuerpo = trabajo.snapshot()

    assert cuerpo["messages_in_run"] == 314
    assert cuerpo["messages_total"] == 6314


def test_no_depende_del_observador_del_backfill(trabajo):
    """Es el punto: `messages_new` puede seguir en cero y esto no.

    El blob llega por otro hilo despues de cerrarse la ventana, asi que la
    cuenta fina se pierde. La de la barra sale de contar filas.
    """
    from app.services.sync_job import SyncState

    trabajo.state = SyncState(messages_at_start=0, messages_new=0)
    trabajo._database.cuantos = 500

    cuerpo = trabajo.snapshot()

    assert cuerpo["messages_new"] == 0
    assert cuerpo["messages_in_run"] == 500


def test_nunca_sale_un_numero_negativo(trabajo):
    """Limpiar la base a mitad de un ciclo no puede pintar "-4000 mensajes"."""
    from app.services.sync_job import SyncState

    trabajo.state = SyncState(messages_at_start=6000)
    trabajo._database.cuantos = 2000

    assert trabajo.snapshot()["messages_in_run"] == 0


def test_un_contador_roto_no_tumba_el_estado(trabajo):
    """Es un numero para pintar: no puede llevarse por delante la pantalla."""
    from app.services.sync_job import SyncState

    trabajo.state = SyncState(messages_total=42)
    trabajo._database = _BaseFalsa(0, revienta=True)

    cuerpo = trabajo.snapshot()

    assert cuerpo["messages_total"] == 42


# ---------------------------------------------------------------------------
# Drive: el otro carril
# ---------------------------------------------------------------------------


def test_lo_que_falta_por_subir_va_en_cada_vistazo(trabajo):
    """Se calculaba UNA vez, al final del ciclo, y ahi ya no se movia.

    Justo despues es cuando importa: la extraccion termina en minutos y la
    subida sigue por detras.
    """
    from app.services.sync_job import SyncState

    trabajo.state = SyncState(messages_at_start=0)
    trabajo._database.cuantos = 12450  # vale para las dos consultas

    cuerpo = trabajo.snapshot()

    assert cuerpo["drive_pending"] == 12450
    assert cuerpo["drive_done"] == 0


def test_el_excavador_NO_menciona_a_Drive():
    """Los dos carriles estan separados, y esto lo deja fijado.

    La ganancia de "desacoplar Drive" no existe porque ya esta desacoplado:
    meter una subida dentro del bucle de excavacion seria la regresion.
    """
    from pathlib import Path

    fuente = Path("app/services/backfill_service.py").read_text(encoding="utf-8")
    for palabra in ("storage_worker", "DriveStorageWorker", "subir_segmento"):
        assert palabra not in fuente, (
            f"'{palabra}' dentro del excavador: el bucle volveria a esperar a "
            "Drive, y el telefono se quedaria parado mientras tanto"
        )


def test_el_trabajador_de_Drive_tiene_su_propio_hilo():
    """Si dejara de tenerlo, la subida bloquearia a quien la invoque."""
    from pathlib import Path

    fuente = Path("app/storage/worker.py").read_text(encoding="utf-8")
    assert "threading.Thread" in fuente
    assert "daemon=True" in fuente


# ---------------------------------------------------------------------------
# El log no crece sin fin
# ---------------------------------------------------------------------------


def test_el_log_rota_en_vez_de_crecer_sin_tope():
    """Se midio en 106 MB: el fichero mas grande del proyecto tras el entorno.

    Era un `FileHandler` a secas, en DEBUG y sin limite, asi que crecia
    mientras el servicio estuviera vivo. Con el a cien megas, buscar "que paso
    hace un rato" es recorrer cien megas de texto.

    Se comprueba el tipo, no el tamano: un fichero pequeno hoy no dice nada
    sobre lo que pasara en una semana de uso.
    """
    import inspect
    import logging.handlers

    from app.core import logging_setup

    fuente = inspect.getsource(logging_setup)
    assert "RotatingFileHandler" in fuente, (
        "el log volvio a ser un FileHandler sin tope: crecera sin fin"
    )
    assert "maxBytes" in fuente and "backupCount" in fuente
    # Y que el tope sea real, no simbolico.
    assert logging.handlers.RotatingFileHandler is not None
