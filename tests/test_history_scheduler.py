"""El orden en que se recuperan las conversaciones. Lo abierto va primero.

EL PROBLEMA QUE RESUELVE
------------------------
La excavación atendía por actividad, de la más nueva a la más vieja, y punto.
Abrir una conversación no cambiaba nada: si estaba en la posición treinta, le
tocaba en la treinta, y cada puesto puede costar hasta 45 segundos. El usuario
abría un chat y no pasaba nada.

LO QUE PROTEGEN ESTAS PRUEBAS
-----------------------------
* que lo interactivo gane SIEMPRE, incluso sobre un reintento;
* que las que esperan referencia **no** ocupen turno — sin ancla no se puede ni
  formular la petición, así que darles uno es regalarlo;
* que el fondo no se muera de hambre aunque el usuario navegue sin parar;
* que abrir la misma conversación diez veces no produzca diez trabajos.
"""

from __future__ import annotations

import time

import pytest

from app.history.scheduler import (
    UNO_DE_FONDO_CADA,
    VIGENCIA_INTERACTIVA,
    HistoryScheduler,
    Prioridad,
)


def _candidato(jid: str, *, ultimo: float | None = None, reintentando: bool = False):
    return {
        "chat_id": abs(hash(jid)) % 100000,
        "chat_jid": jid,
        "ultimo_mensaje": ultimo,
        "reintentando": reintentando,
    }


@pytest.fixture
def planificador() -> HistoryScheduler:
    return HistoryScheduler()


# ---------------------------------------------------------------------------
# Prioridades
# ---------------------------------------------------------------------------


def test_lo_que_el_usuario_abre_va_primero(planificador):
    """LA REGLA. Es lo único que alguien está esperando de verdad."""
    candidatos = [
        _candidato("a@lid", ultimo=3000),
        _candidato("b@lid", ultimo=2000),
        _candidato("c@lid", ultimo=1000),
    ]
    planificador.marcar_interactiva("c@lid")
    orden = [t.chat_jid for t in planificador.ordenar(candidatos)]
    assert orden[0] == "c@lid"


def test_sin_nadie_mirando_manda_la_actividad(planificador):
    candidatos = [
        _candidato("viejo@lid", ultimo=1000),
        _candidato("nuevo@lid", ultimo=3000),
    ]
    orden = [t.chat_jid for t in planificador.ordenar(candidatos)]
    assert orden == ["nuevo@lid", "viejo@lid"]


def test_lo_interactivo_gana_incluso_a_un_reintento(planificador):
    """Un reintento cuesta y se sabe. Lo que el usuario mira, no espera."""
    planificador.marcar_interactiva("mirando@lid")
    prioridad = planificador.clasificar("mirando@lid", reintentando=True)
    assert prioridad == Prioridad.INTERACTIVA


def test_los_reintentos_van_al_final(planificador):
    """Ya se sabe que cuestan; adelantarlos retrasa a las que sí responden."""
    candidatos = [
        _candidato("falla@lid", ultimo=9000, reintentando=True),
        _candidato("normal@lid", ultimo=1000),
    ]
    orden = [t.chat_jid for t in planificador.ordenar(candidatos)]
    assert orden == ["normal@lid", "falla@lid"]


def test_lo_reciente_va_antes_que_el_fondo(planificador):
    """El umbral sale de los propios datos, no de un numero de dias fijo.

    Una cuenta con conversaciones de hoy y otra cuya ultima es de hace un anio
    no tienen el mismo «reciente», y una constante haria que en la segunda no
    fuese reciente ninguna.
    """
    candidatos = [
        _candidato("d@lid", ultimo=100),
        _candidato("c@lid", ultimo=200),
        _candidato("b@lid", ultimo=900),
        _candidato("a@lid", ultimo=1000),
    ]
    trabajos = planificador.ordenar(candidatos)
    porJid = {t.chat_jid: t.prioridad for t in trabajos}
    assert porJid["a@lid"] == Prioridad.RECIENTE
    assert porJid["d@lid"] == Prioridad.FONDO


def test_sin_marca_de_tiempo_se_trata_como_fondo(planificador):
    """Lo que no se sabe NO se asciende: nunca se inventa urgencia."""
    assert planificador.clasificar("x@lid", ultimo_mensaje=None) == Prioridad.FONDO


# ---------------------------------------------------------------------------
# Marcar y soltar
# ---------------------------------------------------------------------------


def test_abrir_el_mismo_chat_muchas_veces_no_lo_duplica(planificador):
    """§F14/§F35. Sube la prioridad una vez, no encola diez trabajos."""
    for _ in range(5):
        planificador.marcar_interactiva("c@lid")
    trabajos = planificador.ordenar([_candidato("c@lid", ultimo=1)])
    assert len(trabajos) == 1
    assert planificador.resumen()["interactivas"] == ["c@lid"]


def test_soltarla_la_devuelve_a_su_sitio(planificador):
    planificador.marcar_interactiva("c@lid")
    assert planificador.es_interactiva("c@lid")
    planificador.soltar_interactiva("c@lid")
    assert not planificador.es_interactiva("c@lid")


def test_la_marca_caduca_sola(planificador, monkeypatch):
    """Si dejo de mirarla y nadie lo dice, deja de ser urgente igualmente."""
    planificador.marcar_interactiva("c@lid")
    ahora = time.monotonic()
    monkeypatch.setattr(
        "app.history.scheduler.time.monotonic",
        lambda: ahora + VIGENCIA_INTERACTIVA + 1,
    )
    assert not planificador.es_interactiva("c@lid")


def test_un_jid_vacio_no_marca_nada(planificador):
    planificador.marcar_interactiva("")
    assert planificador.resumen()["interactivas"] == []


# ---------------------------------------------------------------------------
# Equidad (F19)
# ---------------------------------------------------------------------------


def test_el_fondo_no_se_muere_de_hambre(planificador):
    """Con prioridad estricta y un usuario navegando, el fondo no avanza nunca.

    Cada `UNO_DE_FONDO_CADA` interactivas se cede un turno al fondo. Se
    comprueba que en las primeras posiciones aparece al menos una de fondo.
    """
    candidatos = [_candidato(f"i{n}@lid", ultimo=1000 + n) for n in range(10)]
    candidatos.append(_candidato("fondo@lid", ultimo=1))
    for n in range(10):
        planificador.marcar_interactiva(f"i{n}@lid")

    orden = [t.chat_jid for t in planificador.ordenar(candidatos)]
    posicion = orden.index("fondo@lid")
    assert posicion <= UNO_DE_FONDO_CADA, (
        f"el fondo no deberia esperar a las 10 interactivas; salio en {posicion}"
    )
    assert planificador.cedidos_al_fondo >= 1


def test_sin_trabajo_de_fondo_no_se_inventa_ninguno(planificador):
    candidatos = [_candidato(f"i{n}@lid", ultimo=n) for n in range(5)]
    for n in range(5):
        planificador.marcar_interactiva(f"i{n}@lid")
    orden = planificador.ordenar(candidatos)
    assert len(orden) == 5
    assert planificador.cedidos_al_fondo == 0


def test_no_se_pierde_ni_se_duplica_ningun_trabajo(planificador):
    """La cesion reordena; nunca anade ni quita."""
    candidatos = [_candidato(f"c{n}@lid", ultimo=n) for n in range(12)]
    for n in range(6):
        planificador.marcar_interactiva(f"c{n}@lid")
    orden = [t.chat_jid for t in planificador.ordenar(candidatos)]
    assert sorted(orden) == sorted(c["chat_jid"] for c in candidatos)
    assert len(orden) == len(set(orden))


def test_una_lista_vacia_no_revienta(planificador):
    assert planificador.ordenar([]) == []


def test_un_candidato_sin_jid_se_ignora(planificador):
    """Un trabajo sin conversacion no se puede pedir. No se cuela."""
    orden = planificador.ordenar([{"chat_id": 1, "chat_jid": ""}])
    assert orden == []
