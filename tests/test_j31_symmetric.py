"""La vara con la que se mide el Plan J3.1. Si esto falla, la prueba no vale.

QUE PROTEGE
-----------
Tres fases del Plan J se sostuvieron sobre una comparación que no lo era: ocho
conversaciones con ancla de un instante contra treinta y siete acumuladas
durante días. El error no estaba en los datos, estaba en que cada lado se
medía con un criterio distinto y nadie comparó las edades.

Estas pruebas cierran esa puerta. Cada una fija una regla que, si se relaja,
devuelve exactamente aquel fallo:

* una conversación se clasifica igual venga de donde venga;
* un ancla es válida por lo que es, no por quién la trajo;
* lo que consiguió el segundo dispositivo NO cuenta como arranque del primero;
* dos fotos de edades distintas no producen veredicto;
* durante la fase A, el segundo dispositivo no arranca por ninguna puerta.
"""

from __future__ import annotations

import json

import pytest

from app.discovery import symmetric_snapshot as snap


# ---------------------------------------------------------------------------
# Clasificación: la misma tabla para los dos lados (§14)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "jid, esperado",
    [
        ("34600111222@s.whatsapp.net", snap.INDIVIDUAL),
        ("12345678901234@lid", snap.INDIVIDUAL),
        ("34600111222@c.us", snap.INDIVIDUAL),
        ("120363000000000000@g.us", snap.GRUPO),
        ("120363000000000000@newsletter", snap.BOLETIN),
        ("status@broadcast", snap.ESTADOS),
        ("34600111222-1234567890@broadcast", snap.DIFUSION),
        ("13135550002@bot", snap.BOT),
        ("", snap.SISTEMA),
        (None, snap.SISTEMA),
        ("sin-arroba", snap.SISTEMA),
    ],
)
def test_cada_jid_cae_en_su_cajon(jid, esperado):
    assert snap.clasificar(jid) == esperado


def test_los_indicadores_afinan_pero_no_se_inventan():
    """Sin indicador, un individual es individual. No se supone archivado."""
    jid = "34600111222@s.whatsapp.net"
    assert snap.clasificar(jid) == snap.INDIVIDUAL
    assert snap.clasificar(jid, archivado=True) == snap.INDIVIDUAL_ARCHIVADO
    assert snap.clasificar(jid, es_negocio=True) == snap.NEGOCIO
    assert snap.clasificar(jid, archivado=None) == snap.INDIVIDUAL


def test_una_comunidad_no_es_una_conversacion():
    """El nodo padre de una comunidad contiene grupos; no es un chat."""
    grupo = "120363000000000000@g.us"
    assert snap.es_de_usuario(snap.clasificar(grupo))
    assert not snap.es_de_usuario(snap.clasificar(grupo, es_comunidad=True))


def test_lo_que_cuenta_como_conversacion_de_usuario():
    de_usuario = {c for c in snap.CLASES_DE_USUARIO}
    especiales = {c for c in snap.CLASES_ESPECIALES}
    # Ni una clase puede estar en los dos sitios: si lo estuviera, la
    # cobertura dependeria de por que lista se pregunte primero.
    assert not de_usuario & especiales
    assert snap.BOLETIN in especiales
    assert snap.ESTADOS in especiales
    assert snap.INDIVIDUAL in de_usuario


# ---------------------------------------------------------------------------
# Anclas: qué vale y qué no (§16)
# ---------------------------------------------------------------------------


def test_un_ancla_necesita_las_tres_cosas():
    valida = dict(
        chat_jid="34600111222@s.whatsapp.net",
        wa_msg_id="3EB0C767D82B0F2B1234",
        timestamp=1757000000,
    )
    assert snap.ancla_valida(**valida)

    assert not snap.ancla_valida(**{**valida, "chat_jid": None})
    assert not snap.ancla_valida(**{**valida, "chat_jid": "sin-arroba"})
    assert not snap.ancla_valida(**{**valida, "wa_msg_id": None})
    assert not snap.ancla_valida(**{**valida, "wa_msg_id": ""})
    assert not snap.ancla_valida(**{**valida, "timestamp": 0})
    assert not snap.ancla_valida(**{**valida, "timestamp": None})
    assert not snap.ancla_valida(**{**valida, "timestamp": "ayer"})


def test_un_identificador_nuestro_no_es_un_ancla():
    """Un ancla fabricada recibe confirmacion del servidor y luego silencio.

    Es el fallo mas caro de diagnosticar del proyecto, asi que se rechaza aqui
    y no mas adelante.
    """
    from app.services.repository import SYNTHETIC_PREFIXES

    for prefijo in SYNTHETIC_PREFIXES:
        assert not snap.ancla_valida(
            chat_jid="34600111222@s.whatsapp.net",
            wa_msg_id=f"{prefijo}abc123",
            timestamp=1757000000,
        )


# ---------------------------------------------------------------------------
# Origen de las anclas: la separación que decide la fase (§17, §18)
# ---------------------------------------------------------------------------


def test_los_origenes_se_normalizan_a_una_sola_taxonomia():
    assert snap.normalizar_origen("web_fetch1") == snap.ORIGEN_FETCH_WEB
    assert snap.normalizar_origen("web_local_fetch") == snap.ORIGEN_FETCH_WEB
    assert snap.normalizar_origen("retry_resend") == snap.ORIGEN_LIVE
    assert snap.normalizar_origen("recent_history") == snap.ORIGEN_BOOTSTRAP
    assert snap.normalizar_origen("  ON_DEMAND  ") == snap.ORIGEN_ON_DEMAND
    assert snap.normalizar_origen("algo_que_no_existe") == snap.ORIGEN_OTRO
    assert snap.normalizar_origen(None) == snap.ORIGEN_OTRO


def test_on_demand_no_cuenta_como_arranque():
    """Necesita un ancla previa para poder pedirse: contarlo es contar dos veces."""
    assert not snap.cuenta_como_bootstrap("on_demand")


def test_live_no_cuenta_como_arranque():
    """Mide la actividad de la cuenta, no lo que trajo vincular."""
    assert not snap.cuenta_como_bootstrap("live")
    assert not snap.cuenta_como_bootstrap("retry_resend")


def test_lo_del_navegador_no_cuenta_como_arranque_de_la_principal():
    """LA REGLA QUE DECIDE LA FASE.

    Si un ancla que trajo el segundo dispositivo cuenta como cobertura de
    arranque del primero, la principal aparece con cuarenta anclas cuando de
    verdad tiene ocho. Se midio sobre la sesion real: exactamente ese error.
    """
    assert not snap.cuenta_como_bootstrap("web_store")
    assert not snap.cuenta_como_bootstrap("web_fetch1")
    assert snap.cuenta_como_bootstrap("initial_bootstrap")


# ---------------------------------------------------------------------------
# La foto del navegador
# ---------------------------------------------------------------------------


def _respuesta_del_worker(chats):
    return {"chats": chats, "store_msg_total": sum(c.get("msgs_in_memory", 0) for c in chats)}


def test_la_foto_del_navegador_cuenta_lo_mismo_que_la_de_la_principal():
    foto = snap.fotografiar_navegador(
        _respuesta_del_worker(
            [
                {
                    "id": "34600111222@s.whatsapp.net",
                    "name": True,
                    "last_activity": 1757000000,
                    "msgs_in_memory": 4,
                    "newest": {"wa_msg_id": "3EB0AAA", "t": 1757000000, "from_me": False},
                },
                {
                    "id": "120363000000000000@g.us",
                    "name": False,
                    "last_activity": None,
                    "msgs_in_memory": 0,
                    "newest": None,
                },
                {
                    "id": "status@broadcast",
                    "name": False,
                    "last_activity": 1757000000,
                    "msgs_in_memory": 9,
                    "newest": {"wa_msg_id": "3EB0BBB", "t": 1757000001, "from_me": False},
                },
            ]
        ),
        t0=1000.0,
        etiqueta="t120",
        ahora=1120.0,
    )
    assert foto.raw_chat_count == 3
    # Los estados no son una conversacion, aunque tengan mensajes.
    assert foto.user_chat_count == 2
    assert foto.special_entity_count == 1
    assert foto.chats_with_valid_seed == 2
    assert foto.user_chats_with_valid_seed == 1
    assert foto.session_age_seconds == 120.0


def test_el_modo_distingue_almacen_natural_de_sondeo():
    """§57: las dos mediciones no se pueden mezclar."""
    natural = snap.fotografiar_navegador(
        _respuesta_del_worker([]), t0=0.0, etiqueta="t120", ahora=120.0
    )
    sondeado = snap.fotografiar_navegador(
        _respuesta_del_worker([]),
        t0=0.0,
        etiqueta="probes",
        ahora=200.0,
        modo="after_probes",
    )
    assert natural.to_json()["mode"] == "native_store"
    assert sondeado.to_json()["mode"] == "after_probes"


def test_un_mensaje_sin_identificador_no_da_ancla():
    foto = snap.fotografiar_navegador(
        _respuesta_del_worker(
            [
                {
                    "id": "34600111222@s.whatsapp.net",
                    "name": True,
                    "last_activity": 1757000000,
                    "msgs_in_memory": 3,
                    "newest": {"wa_msg_id": None, "t": 1757000000, "from_me": False},
                }
            ]
        ),
        t0=0.0,
        etiqueta="t000",
        ahora=0.0,
    )
    assert foto.chats_with_real_message == 0
    assert foto.chats_with_valid_seed == 0


# ---------------------------------------------------------------------------
# La comparación (§66-§72)
# ---------------------------------------------------------------------------


def _foto(lado, *, chats, anclas, edad, especiales=0):
    filas = []
    for i in range(chats):
        filas.append(
            snap.Fila(
                chat=f"{lado}{i:04d}",
                clase=snap.INDIVIDUAL,
                tiene_nombre=True,
                tiene_actividad=True,
                tiene_mensaje_real=i < anclas,
                tiene_ancla=i < anclas,
                mensajes_en_memoria=1 if i < anclas else 0,
            )
        )
    for i in range(especiales):
        filas.append(
            snap.Fila(
                chat=f"{lado}x{i:03d}",
                clase=snap.BOLETIN,
                tiene_nombre=False,
                tiene_actividad=True,
                tiene_mensaje_real=False,
                tiene_ancla=False,
                mensajes_en_memoria=0,
            )
        )
    return snap.Foto(
        lado=lado, etiqueta="t120", t0_epoch=0.0, capturado_epoch=edad, filas=filas
    )


def test_edades_distintas_no_producen_veredicto():
    """LA REGLA QUE ORIGINA ESTA FASE.

    Comparar un arranque de dos minutos contra un almacen de dias es lo que
    se venia haciendo. Ahora se detecta y se dice, en vez de concluir.
    """
    principal = _foto("p", chats=41, anclas=8, edad=120.0)
    navegador = _foto("w", chats=50, anclas=37, edad=259_200.0)  # tres dias
    resultado = snap.comparar(principal, navegador)
    assert resultado["symmetric"] is False
    assert resultado["case"] == snap.CASO_INCONCLUSO


def test_caso_a_el_navegador_recien_nacido_saca_mucho_mas():
    principal = _foto("p", chats=41, anclas=8, edad=120.0)
    navegador = _foto("w", chats=50, anclas=32, edad=120.0)
    resultado = snap.comparar(principal, navegador)
    assert resultado["symmetric"] is True
    assert resultado["case"] == snap.CASO_DIFERENCIA_REAL


def test_caso_b_a_la_misma_edad_se_parecen():
    """Si sale esto, la premisa de las fases anteriores era falsa."""
    principal = _foto("p", chats=41, anclas=8, edad=120.0)
    navegador = _foto("w", chats=42, anclas=9, edad=120.0)
    resultado = snap.comparar(principal, navegador)
    assert resultado["case"] == snap.CASO_PREMISA_FALSA


def test_caso_d_ve_mas_conversaciones_pero_no_mas_anclas():
    principal = _foto("p", chats=30, anclas=10, edad=120.0)
    navegador = _foto("w", chats=50, anclas=11, edad=120.0)
    assert snap.comparar(principal, navegador)["case"] == snap.CASO_HUECO_DE_DESCUBRIMIENTO


def test_caso_e_mismas_conversaciones_pero_muchas_mas_anclas():
    principal = _foto("p", chats=41, anclas=8, edad=120.0)
    navegador = _foto("w", chats=41, anclas=33, edad=120.0)
    assert snap.comparar(principal, navegador)["case"] == snap.CASO_HUECO_DE_ANCLAS


def test_si_la_principal_va_por_delante_no_se_canta_victoria():
    """Una diferencia al reves no confirma la hipotesis: es inconcluso."""
    principal = _foto("p", chats=50, anclas=40, edad=120.0)
    navegador = _foto("w", chats=20, anclas=4, edad=120.0)
    assert snap.comparar(principal, navegador)["case"] == snap.CASO_INCONCLUSO


def test_la_cobertura_normalizada_descuenta_lo_que_no_es_conversacion():
    """§74: si lo que solo ve el navegador son boletines, la cobertura sube."""
    principal = _foto("p", chats=10, anclas=5, edad=120.0)
    navegador = _foto("p", chats=10, anclas=5, edad=120.0, especiales=9)
    normalizada = snap.cobertura_normalizada(principal, navegador)
    assert normalizada["web_only_total"] == 9
    assert normalizada["web_only_user_visible"] == 0
    assert normalizada["normalized_user_chat_coverage"] == 1.0


def test_las_conversaciones_que_solo_ve_el_navegador_se_listan():
    principal = _foto("p", chats=2, anclas=2, edad=120.0)
    navegador = _foto("p", chats=5, anclas=5, edad=120.0)
    solo_web = snap.solo_del_navegador(principal, navegador)
    assert len(solo_web) == 3
    assert all(fila["user_visible"] for fila in solo_web)


# ---------------------------------------------------------------------------
# Privacidad
# ---------------------------------------------------------------------------


def test_de_la_foto_no_sale_ni_un_identificador():
    foto = snap.fotografiar_navegador(
        _respuesta_del_worker(
            [
                {
                    "id": "34600111222@s.whatsapp.net",
                    "name": True,
                    "last_activity": 1757000000,
                    "msgs_in_memory": 2,
                    "newest": {"wa_msg_id": "3EB0SECRETO", "t": 1757000000},
                }
            ]
        ),
        t0=0.0,
        etiqueta="t000",
        ahora=0.0,
    )
    texto = json.dumps(foto.to_json())
    assert "34600111222" not in texto
    assert "3EB0SECRETO" not in texto
    assert "@s.whatsapp.net" not in texto


def test_el_hash_es_estable_y_no_reversible():
    a = snap.hash_de("34600111222@s.whatsapp.net")
    b = snap.hash_de("34600111222@s.whatsapp.net")
    assert a == b and len(a) == 8
    assert a != snap.hash_de("34600111223@s.whatsapp.net")


# ---------------------------------------------------------------------------
# La bandera de fase A (§11, §12)
# ---------------------------------------------------------------------------


class _Ajustes:
    def __init__(self, **campos):
        self.web_companion_enabled = True
        self.plan_j31_primary_only = False
        self.plan_j31_freeze_history = False
        for clave, valor in campos.items():
            setattr(self, clave, valor)


def test_primary_only_bloquea_el_segundo_dispositivo(tmp_path):
    from app.web_companion.supervisor import WebCompanionSupervisor

    supervisor = WebCompanionSupervisor(_Ajustes(plan_j31_primary_only=True), raiz=tmp_path)
    assert supervisor.permitido() is False


def test_sin_la_bandera_manda_la_sesion_principal(tmp_path):
    """La bandera se anade a la regla de siempre; no la sustituye."""
    from app.web_companion.supervisor import WebCompanionSupervisor

    supervisor = WebCompanionSupervisor(_Ajustes(), raiz=tmp_path)
    assert supervisor.permitido() is True  # sin runtime cableado


def test_primary_only_cierra_tambien_la_puerta_de_start(tmp_path):
    """No basta con no reintentar: `start` se llama desde la API y las tools."""
    from app.web_companion.supervisor import WebCompanionSupervisor

    supervisor = WebCompanionSupervisor(_Ajustes(plan_j31_primary_only=True), raiz=tmp_path)
    assert supervisor.start() is False
    assert supervisor.snapshot()["state"] == "blocked_by_primary"


# ---------------------------------------------------------------------------
# El congelado de historial (§24)
# ---------------------------------------------------------------------------


def test_congelar_historial_corta_on_demand():
    """Excavar mientras se mide el arranque mezcla anclas de dos fuentes."""
    from app.services.backfill_service import BackfillService

    servicio = object.__new__(BackfillService)
    servicio._settings = _Ajustes(plan_j31_freeze_history=True)
    assert servicio._historia_congelada("34600111222@s.whatsapp.net") is True

    servicio._settings = _Ajustes(plan_j31_freeze_history=False)
    assert servicio._historia_congelada("34600111222@s.whatsapp.net") is False


# ---------------------------------------------------------------------------
# Huellas Signal: sin material de clave (§84)
# ---------------------------------------------------------------------------


def test_la_huella_signal_no_expone_claves(tmp_path):
    import sqlite3
    import sys

    sys.path.insert(0, str(tmp_path))
    from tools.j31_signal_fingerprint import tomar_huella

    ruta = tmp_path / "device.json.signal.db"
    conexion = sqlite3.connect(ruta)
    conexion.execute("CREATE TABLE sessions (session_id TEXT, state BLOB)")
    conexion.execute("CREATE TABLE identities (session_id TEXT, identity BLOB)")
    conexion.execute("CREATE TABLE prekeys (key_id INTEGER, private BLOB, public BLOB)")
    conexion.execute("CREATE TABLE lid_map (pn_user TEXT, lid_user TEXT)")
    conexion.execute(
        "INSERT INTO sessions VALUES (?, ?)",
        ("34600111222.0@s.whatsapp.net", b"RATCHET-SECRETO-NO-DEBE-SALIR"),
    )
    conexion.execute(
        "INSERT INTO identities VALUES (?, ?)",
        ("34600111222.0@s.whatsapp.net", b"IDENTIDAD-SECRETA"),
    )
    conexion.commit()
    conexion.close()

    huella = tomar_huella(ruta, "prueba")
    texto = json.dumps(huella)
    assert "RATCHET-SECRETO-NO-DEBE-SALIR" not in texto
    assert "IDENTIDAD-SECRETA" not in texto
    assert "34600111222" not in texto
    assert huella["session_count"] == 1
    assert huella["pn_sessions"] == 1
    assert huella["sessions"][0]["kind"] == "PN"


def test_la_huella_signal_detecta_que_algo_cambio():
    from tools.j31_signal_fingerprint import comparar as comparar_signal

    antes = {
        "sessions": [{"address": "...1222@s", "record_fingerprint": "aaa"}],
        "prekeys": 40,
    }
    igual = {
        "sessions": [{"address": "...1222@s", "record_fingerprint": "aaa"}],
        "prekeys": 40,
    }
    distinto = {
        "sessions": [
            {"address": "...1222@s", "record_fingerprint": "bbb"},
            {"address": "...9999@lid", "record_fingerprint": "ccc"},
        ],
        "prekeys": 38,
    }
    assert comparar_signal(antes, igual)["changed"] is False
    resultado = comparar_signal(antes, distinto)
    assert resultado["changed"] is True
    assert resultado["changed_sessions"] == ["...1222@s"]
    assert resultado["new_sessions"] == ["...9999@lid"]


# ---------------------------------------------------------------------------
# La escalera (§19, §21, §43, §44)
# ---------------------------------------------------------------------------


def test_la_escalera_no_pisa_una_captura_existente(tmp_path):
    """§19: cada captura es un registro aparte e inmutable."""
    from app.experimental.j31_recorder import RegistradorJ31

    registrador = RegistradorJ31(runtime=None, carpeta=tmp_path)
    registrador._guardar("primary_t000.json", {"v": 1})
    registrador._guardar("primary_t000.json", {"v": 2})
    guardado = json.loads((tmp_path / "primary_t000.json").read_text(encoding="utf-8"))
    assert guardado == {"v": 1}


def test_se_alarga_solo_si_seguia_moviendose(tmp_path):
    """§43: si esta quieto entre t060 y t120, no hace falta t300."""
    from app.experimental.j31_recorder import RegistradorJ31

    quieta = _foto("p", chats=10, anclas=5, edad=60.0)
    igual = _foto("p", chats=10, anclas=5, edad=120.0)
    creciendo = _foto("p", chats=14, anclas=9, edad=120.0)

    assert RegistradorJ31._se_movio(quieta, igual) is False
    assert RegistradorJ31._se_movio(quieta, creciendo) is True
    # Sin captura previa se alarga: no saber no es lo mismo que estar quieto.
    assert RegistradorJ31._se_movio(None, igual) is True


def test_lo_congelado_no_se_vuelve_a_escribir(tmp_path):
    """§44: despues del congelado pueden entrar mensajes en vivo, y da igual."""
    from app.experimental.j31_recorder import RegistradorJ31

    registrador = RegistradorJ31(runtime=None, carpeta=tmp_path)
    primera = {"t120": _foto("p", chats=10, anclas=5, edad=120.0)}
    segunda = {"t120": _foto("p", chats=99, anclas=99, edad=120.0)}
    registrador._congelar("primary", 0.0, primera, alargado=False)
    registrador._congelar("primary", 0.0, segunda, alargado=False)
    congelado = registrador.congelado["PRIMARY_BOOTSTRAP_FINAL"]
    assert congelado["final"]["metrics"]["user_chat_count"] == 10


def test_el_registrador_no_hace_nada_sin_la_bandera():
    from app.experimental.j31_recorder import arrancar_si_procede

    class _Runtime:
        settings = _Ajustes()

    assert arrancar_si_procede(_Runtime()) is None
