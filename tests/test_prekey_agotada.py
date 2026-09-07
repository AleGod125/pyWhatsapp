"""Una clave de un solo uso ya consumida NO vuelve a existir.

LOS DOS CASOS, QUE NO SON EL MISMO
----------------------------------
1. **Reenvio del MISMO establecimiento** --misma base key, misma identidad--
   con la clave de un solo uso ya consumida. Es lo que arregla
   ``prekey_compat``: no se rehace el X3DH, se reutiliza el ratchet que ya
   existe. Medido en el registro local: **479 mensajes** rescatados asi.

2. **Establecimiento NUEVO** --base key distinta-- que referencia una clave de
   un solo uso ya consumida. Aqui no hay nada que reutilizar: sin la parte
   privada de esa clave el secreto compartido **no se puede derivar**. No es
   que falte codigo; es que la informacion no existe.

EL CASO 2, MEDIDO
-----------------
El dispositivo ``206566***:44@lid`` lleva desde el 3 de septiembre mandando la
misma base key (``b7331e3e``) con la clave ``17``, consumida hace dias::

    PKMSG sender=206566***:44@lid opk_id=17
          existing_session=yes matching_prekey_session=no base_key_fp=b7331e3e

**69 acuses de reintento. 17 de ellos con material publico. 180 intentos.** La
base key nunca cambio: el emisor no rehace el saludo.

LO QUE SE HACE, Y LO QUE NO
---------------------------
Se deja de pedir despues de unos cuantos acuses, porque cada acuse con
material gasta una clave de un solo uso y no ha servido de nada. Se sigue
intentando descifrar cada mensaje: el dia que el emisor rehaga el saludo, la
base key sera otra y el recuento empieza de cero.

**No se acepta ningun mensaje sin descifrar. No se retiene ninguna clave
privada. No se toca el Signal Store.**
"""

from __future__ import annotations

import pytest

from app.compat import prekey_compat

SID = "206566519222309:44@lid"
OTRO_SID = "170686312136883:0@lid"
BASE_A = b"base-key-de-la-primera-sesion---"
BASE_B = b"base-key-de-un-saludo-distinto--"


@pytest.fixture(autouse=True)
def sin_memoria():
    prekey_compat.olvidar_imposibles()
    yield
    prekey_compat.olvidar_imposibles()


def _fallar(sid=SID, base=BASE_A, opk=17, veces=1):
    for _ in range(veces):
        prekey_compat._anotar_imposible(sid, base, opk)


# ---------------------------------------------------------------------------
# Se insiste primero, y se desiste despues
# ---------------------------------------------------------------------------


def test_al_principio_SI_se_pide():
    """El acuse es la via correcta: hay que darle su oportunidad."""
    _fallar()
    assert prekey_compat.insistir_es_inutil(SID) is False


def test_tras_varios_intentos_se_deja_de_pedir():
    """LA REGLA. Cada acuse con material gasta una clave de un solo uso."""
    _fallar(veces=prekey_compat.ACUSES_ANTES_DE_DESISTIR + 1)
    assert prekey_compat.insistir_es_inutil(SID) is True


def test_el_limite_esta_donde_dice_la_constante():
    _fallar(veces=prekey_compat.ACUSES_ANTES_DE_DESISTIR)
    assert prekey_compat.insistir_es_inutil(SID) is False
    _fallar()
    assert prekey_compat.insistir_es_inutil(SID) is True


def test_UNA_BASE_KEY_NUEVA_EMPIEZA_DE_CERO():
    """La prueba que evita silenciar a alguien para siempre.

    Que el emisor cambie de base key significa que rehizo el saludo, y ese
    intento SI puede completarse. Seria un fallo grave dejar de pedirle nada
    por lo que hizo su establecimiento anterior.
    """
    _fallar(veces=20)
    assert prekey_compat.insistir_es_inutil(SID) is True

    _fallar(base=BASE_B, opk=31)
    assert prekey_compat.insistir_es_inutil(SID) is False


def test_lo_de_un_dispositivo_no_calla_a_otro():
    _fallar(veces=20)
    assert prekey_compat.insistir_es_inutil(OTRO_SID) is False


def test_de_quien_nunca_fallo_no_se_sabe_nada():
    assert prekey_compat.insistir_es_inutil(SID) is False


def test_el_registro_no_crece_sin_fin():
    for n in range(prekey_compat.MAXIMO_DE_IMPOSIBLES + 50):
        prekey_compat._anotar_imposible(f"{n}@lid", BASE_A, 17)
    assert len(prekey_compat.establecimientos_imposibles()) <= (
        prekey_compat.MAXIMO_DE_IMPOSIBLES
    )


def test_lo_anotado_se_puede_mirar_sin_poder_tocarlo():
    """El diagnostico devuelve una copia: nadie muta el estado desde fuera."""
    _fallar(veces=3)
    copia = prekey_compat.establecimientos_imposibles()
    copia[SID]["fallos"] = 999
    assert prekey_compat.establecimientos_imposibles()[SID]["fallos"] == 3


def test_se_anota_la_clave_que_el_emisor_repite():
    """Para poder decir en el informe cual es, sin adivinarla."""
    _fallar(opk=17)
    assert prekey_compat.establecimientos_imposibles()[SID]["opk_id"] == 17


# ---------------------------------------------------------------------------
# LO QUE NO SE HACE
# ---------------------------------------------------------------------------


def test_no_se_guarda_ninguna_clave_privada():
    """Se anota una HUELLA de la base key publica. Nada mas."""
    _fallar()
    anotado = prekey_compat.establecimientos_imposibles()[SID]
    assert BASE_A not in repr(anotado).encode("utf-8", "ignore")
    assert len(anotado["base_key_fp"]) == 8


def test_desistir_del_acuse_NO_ACEPTA_NINGUN_MENSAJE():
    """La prueba que mas importa.

    Dejar de pedir es dejar de pedir. No convierte un mensaje indescifrable en
    uno valido, no lo guarda, y no relaja ninguna comprobacion: el modulo no
    tiene forma de aceptar nada.
    """
    import ast
    import pathlib

    codigo = pathlib.Path("app/compat/prekey_compat.py").read_text(encoding="utf-8")
    arbol = ast.parse(codigo)
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
    sin_texto = ast.unparse(arbol).lower()
    for prohibido in (
        "verify_mac=none",
        "verify_mac=lambda mac_key: true",
        "identity_private",
        "one_time_pre_key_private",
        "del _consume_opk",
    ):
        assert prohibido not in sin_texto, f"prohibido: {prohibido}"
    # El MAC se sigue verificando con la misma llamada de pywhats.
    assert "verify_mac" in sin_texto
    assert "pkmsg.message.verify_mac" in sin_texto


def test_el_reuso_de_ratchet_sigue_exigiendo_que_coincida_la_base_key():
    """Sin coincidencia de base key se delega en el original. Sin atajos."""
    assert prekey_compat._matches(
        type("M", (), {"base_key": BASE_A, "identity_key": b"id"})(), (BASE_A, b"id")
    )
    assert not prekey_compat._matches(
        type("M", (), {"base_key": BASE_B, "identity_key": b"id"})(), (BASE_A, b"id")
    )
    assert not prekey_compat._matches(
        type("M", (), {"base_key": BASE_A, "identity_key": b"id"})(), None
    )


# ---------------------------------------------------------------------------
# Archivar la sesion NO puede matar la compatibilidad
# ---------------------------------------------------------------------------
#
# EL FALLO, MEDIDO EN UNA VINCULACION NUEVA
# -----------------------------------------
#     22:06:06  Adaptaciones activas: ... prekey_replay ...
#     22:06:29  Sesion archivada (revoked-401)   <- _registry = None
#     22:07:03  vinculacion nueva
#     22:07:04  uploaded 50 one-time prekeys (ids 1..50)
#     22:07:26  primer mensaje de 855390@lid     -> OK, consume la OPK 21
#     22:07:52  segundo mensaje, MISMO dispositivo
#               unknown one-time pre-key id 21
#
# `archive_session` cierra el registro para poder mover el archivo en Windows,
# y `apply()` solo corre una vez, antes de crear el cliente. Nadie lo rearmaba.
# Con el registro en None el parche caia de largo al original EN SILENCIO: cero
# lineas `PKMSG sender=` en toda la ventana y `compat_prekey.db` sin llegar a
# existir.


def test_EL_REGISTRO_SE_REABRE_SOLO_TRAS_ARCHIVAR(tmp_path):
    """LA REGLA. Archivar cierra el registro; el siguiente PKMSG lo reabre."""
    ruta = tmp_path / "compat_prekey.db"
    assert prekey_compat.apply(ruta) is True
    assert prekey_compat._registry is not None

    # Lo que hace `archive_session` para poder mover el archivo.
    prekey_compat._registry.close()
    prekey_compat._registry = None

    assert prekey_compat._reabrir_registro() is True
    assert prekey_compat._registry is not None
    assert ruta.exists(), "el registro tiene que volver a existir en disco"


def test_la_ruta_se_recuerda_al_aplicar(tmp_path):
    ruta = tmp_path / "compat_prekey.db"
    prekey_compat.apply(ruta)
    assert prekey_compat._ruta_del_registro == ruta


def test_SIN_HABER_APLICADO_NUNCA_NO_SE_INVENTA_UNA_RUTA(monkeypatch):
    """No se adivina donde vive el registro: si no se aplico, no hay nada."""
    monkeypatch.setattr(prekey_compat, "_registry", None, raising=False)
    monkeypatch.setattr(prekey_compat, "_ruta_del_registro", None, raising=False)
    assert prekey_compat._reabrir_registro() is False


def test_reabrir_empieza_de_cero(tmp_path):
    """Tras archivar, la identidad es otra: los establecimientos viejos no valen.

    El archivo anterior se movio con la sesion, asi que reabrir crea uno nuevo
    y vacio. Es lo correcto: reutilizar establecimientos de otra identidad
    seria justo lo que esta compatibilidad NO debe hacer.
    """
    ruta = tmp_path / "compat_prekey.db"
    prekey_compat.apply(ruta)
    prekey_compat._registry.record("alguien:0@lid", b"base", b"identidad", 21)
    assert prekey_compat._registry.get("alguien:0@lid") is not None

    prekey_compat._registry.close()
    prekey_compat._registry = None
    ruta.unlink()  # lo que hace el archivado: se lleva el fichero

    assert prekey_compat._reabrir_registro() is True
    assert prekey_compat._registry.get("alguien:0@lid") is None


# ---------------------------------------------------------------------------
# El reinicio del proceso
# ---------------------------------------------------------------------------
#
# La ruta del registro NO puede depender de la memoria del proceso anterior:
# un Ctrl+C y un `py service.py` la borrarian. Tiene que salir del
# `session_dir`, que es lo unico que sobrevive.


def _reiniciar_proceso(monkeypatch):
    """Simula un arranque limpio: las globales del modulo se pierden."""
    monkeypatch.setattr(prekey_compat, "_registry", None, raising=False)
    monkeypatch.setattr(prekey_compat, "_ruta_del_registro", None, raising=False)


def test_TRAS_UN_REINICIO_EL_REGISTRO_SIGUE_AHI(tmp_path, monkeypatch):
    """LA REGLA. Lo anotado antes del reinicio se vuelve a leer despues."""
    ruta = tmp_path / "compat_prekey.db"

    # Proceso 1: se aplica y se anota un establecimiento.
    prekey_compat.apply(ruta)
    prekey_compat._registry.record("alguien:0@lid", b"base-key", b"identidad", 21)
    prekey_compat._registry.close()

    _reiniciar_proceso(monkeypatch)

    # Proceso 2: se aplica igual, con la ruta derivada del directorio de sesion.
    prekey_compat.apply(ruta)
    assert prekey_compat._registry is not None
    guardado = prekey_compat._registry.get("alguien:0@lid")
    assert guardado is not None, "el establecimiento tenia que sobrevivir al reinicio"
    assert guardado[0] == b"base-key"


def test_la_ruta_no_depende_de_la_memoria_del_proceso_anterior(tmp_path, monkeypatch):
    """Sale del directorio de sesion, que es lo unico que persiste."""
    _reiniciar_proceso(monkeypatch)
    assert prekey_compat._ruta_del_registro is None

    ruta = tmp_path / "compat_prekey.db"
    prekey_compat.apply(ruta)

    assert prekey_compat._ruta_del_registro == ruta
    assert ruta.exists()


def test_el_arranque_normal_deja_el_registro_listo(tmp_path, monkeypatch):
    """Aplicar tiene que dejarlo utilizable, no solo marcado como activo.

    Que el arranque diga «prekey_replay» solo demuestra que se llamo a
    `apply()`. Lo que importa es que despues haya registro con el que trabajar.
    """
    _reiniciar_proceso(monkeypatch)
    ruta = tmp_path / "compat_prekey.db"

    assert prekey_compat.apply(ruta) is True

    assert prekey_compat._registry is not None
    assert ruta.exists()

