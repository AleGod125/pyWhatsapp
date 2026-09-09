"""Lo que antes arreglaban los parches de pywhats, ahora comprobado en Baileys.

DE DONDE VIENE ESTE FICHERO
---------------------------
Aqui habia diez ficheros de pruebas sobre el mismo problema: la
implementacion de Signal de pywhats no descifraba los mensajes que el usuario
escribia desde SU PROPIO telefono. Llegaban dirigidos a nuestro LID, la sesion
existia pero guardada bajo el numero, y morian con::

    no session for peer 86531142340710@lid

Alrededor de eso crecieron ``own_lid_map``, ``self_session_guard``,
``own_lid_recovery``, ``retry_keys``, ``retry_observer`` y ``prekey_compat``,
con sus monkeypatches sobre atributos privados de la libreria.

Baileys hace la capa de Signal el mismo y resuelve la equivalencia
LID<->numero por dentro, asi que ese problema --y los parches-- desaparecieron.
Lo que NO desaparece es lo que el proyecto necesitaba de todo aquello, y es lo
que se comprueba aqui:

* que la identidad propia (numero Y LID) se resuelva de verdad;
* que la huella de sesion sea la misma mirando el disco o el dispositivo vivo;
* que nuestro par PN<->LID acabe en ``contacts``, que es de donde salen los
  nombres del panel;
* que una vinculacion conseguida no se pueda tirar abajo sola;
* que el historial tenga camino hasta PostgreSQL.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

from app.core.session_state import AppState


# ---------------------------------------------------------------------------
# Identidad propia
# ---------------------------------------------------------------------------


def _creds(carpeta, *, pn="573002389304", lid="86531142340710", ranura=38, reg=125):
    """Unos ajustes que apuntan a un ``creds.json`` como el que escribe Baileys."""
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / "creds.json"
    cuerpo = {
        "me": {"id": f"{pn}:{ranura}@s.whatsapp.net", "name": "Prueba"},
        "registrationId": reg,
    }
    if lid:
        cuerpo["me"]["lid"] = f"{lid}:{ranura}@lid"
    ruta.write_text(json.dumps(cuerpo), encoding="utf-8")
    return types.SimpleNamespace(session_file=ruta)


def test_la_identidad_propia_sale_del_creds_de_baileys(tmp_path):
    """El criterio de aceptacion: pn=True lid=True.

    Con la forma de pywhats esto devolvia ``(None, None)`` y el log decia
    ``Identidad propia: pn=False lid=False``. Sin identidad, el backfill no
    sabe a quien pedirle el historial.
    """
    from app.core.identity import own_identity

    pn, lid = own_identity(_creds(tmp_path))

    assert pn == "573002389304@s.whatsapp.net"
    assert lid == "86531142340710@lid"


def test_la_ranura_de_dispositivo_no_entra_en_la_identidad(tmp_path):
    """Baileys guarda el jid propio como ``573...:38@s.whatsapp.net``.

    Esa parte identifica al COMPANION, no a la cuenta. Si se colara, el numero
    propio no coincidiria con el de ningun mensaje y los salientes se
    atribuirian a otra persona.
    """
    from app.core.identity import own_identity

    pn, lid = own_identity(_creds(tmp_path, ranura=99))

    assert ":" not in pn and ":" not in lid
    assert pn.endswith("@s.whatsapp.net") and lid.endswith("@lid")


def test_sin_lid_todavia_no_se_inventa(tmp_path):
    """El LID no llega en el pair-success, llega en el <success>.

    Entre uno y otro pasa un segundo. Deducirlo del numero seria inventar: son
    espacios de identificadores distintos y no se convierten.
    """
    from app.core.identity import own_identity

    pn, lid = own_identity(_creds(tmp_path, lid=None))

    assert pn is not None
    assert lid is None


def test_sin_credenciales_no_hay_identidad(tmp_path):
    from app.core.identity import own_identity

    ajustes = types.SimpleNamespace(session_file=tmp_path / "no-existe")
    assert own_identity(ajustes) == (None, None)


# ---------------------------------------------------------------------------
# La huella de sesion: el disco y el dispositivo vivo tienen que coincidir
# ---------------------------------------------------------------------------


def test_la_huella_distingue_dos_vinculaciones_en_la_misma_ranura(tmp_path):
    """El servidor REUTILIZA el numero de ranura al desvincular todo.

    Si la huella dependiera solo de la ranura, la segunda vinculacion daria
    por confirmado el historial inicial de la primera y se saltaria el
    bootstrap sin que nada lo delatara.
    """
    from app.core.identity import session_fingerprint

    a = session_fingerprint(_creds(tmp_path / "a", reg=111))
    b = session_fingerprint(_creds(tmp_path / "b", reg=222))

    assert a and b and a != b


def test_la_huella_del_disco_y_la_del_dispositivo_vivo_coinciden(tmp_path):
    """Si divergieran, la espera de 180 s volveria en CADA arranque.

    El historial inicial se confirmaria bajo una huella y se buscaria bajo
    otra, y nada en el log lo delataria: solo tres minutos perdidos cada vez.
    """
    from app.core.identity import session_fingerprint
    from app.services.backfill_service import BackfillService
    from app.wa.tipos import Identidad, jid_desde

    ajustes = _creds(tmp_path, ranura=38, reg=125)

    servicio = object.__new__(BackfillService)
    servicio._client = types.SimpleNamespace(
        device=Identidad(
            jid=jid_desde("573002389304@s.whatsapp.net"),
            lid="86531142340710@lid",
            device_id="38",
            registration_id="125",
        )
    )

    assert session_fingerprint(ajustes) == servicio.session_fingerprint()


# ---------------------------------------------------------------------------
# El par PN<->LID propio acaba en `contacts`
# ---------------------------------------------------------------------------


def _orquestador():
    from app.core.orchestrator import Orchestrator

    orq = object.__new__(Orchestrator)
    orq._database = object()
    return orq


def test_el_par_propio_se_registra_desde_el_dispositivo_vivo(monkeypatch):
    """De ``contacts.lid`` salen los nombres del panel.

    Se lee del dispositivo CONECTADO y no del disco: el LID llega en el
    ``<success>`` y el fichero va un paso por detras.
    """
    import app.services.contacts_service as contactos

    from app.wa.tipos import Identidad, jid_desde

    escritos = []
    monkeypatch.setattr(
        contactos,
        "guardar_par_lid",
        lambda db, lid, pn: bool(escritos.append((lid, pn))) or True,
    )

    sesion = types.SimpleNamespace(
        device=Identidad(
            jid=jid_desde("573002389304@s.whatsapp.net"), lid="86531142340710@lid"
        )
    )
    _orquestador()._registrar_par_propio(sesion)

    assert escritos == [("86531142340710@lid", "573002389304@s.whatsapp.net")]


def test_sin_lid_todavia_no_se_registra_nada(monkeypatch):
    """Registrar media fila seria peor que no registrar ninguna."""
    import app.services.contacts_service as contactos

    from app.wa.tipos import Identidad, jid_desde

    escritos = []
    monkeypatch.setattr(
        contactos,
        "guardar_par_lid",
        lambda db, lid, pn: bool(escritos.append((lid, pn))) or True,
    )

    sesion = types.SimpleNamespace(
        device=Identidad(jid=jid_desde("573002389304@s.whatsapp.net"), lid=None)
    )
    _orquestador()._registrar_par_propio(sesion)

    assert escritos == []


def test_registrar_el_par_propio_no_puede_tumbar_la_conexion():
    """Corre dentro de post_connect: si lanzara, no habria sesion."""

    class _Roto:
        @property
        def device(self):
            raise RuntimeError("sin dispositivo")

    _orquestador()._registrar_par_propio(_Roto())  # no lanza


# ---------------------------------------------------------------------------
# La vinculacion no se tira abajo sola
# ---------------------------------------------------------------------------


def test_session_valid_es_parte_del_contrato():
    """Con pywhats lo producia un parche sobre ``SessionActivator.on_success``.

    Al soltar la libreria dejo de existir, y con el la transicion a CONNECTED:
    la vinculacion funcionaba pero la pantalla se quedaba en el codigo QR,
    pidiendo ``/session/pair`` en bucle.
    """
    from app.wa.port import EVENTOS

    assert "session_valid" in EVENTOS


def test_el_worker_emite_session_valid_al_abrirse_la_conexion():
    fuente = Path("wa_baileys/worker.js").read_text(encoding="utf-8")
    abierto = fuente[fuente.index("u.connection === 'open'") :]
    corte = abierto.index("if (u.connection === 'close')")
    assert "session_valid" in abierto[:corte]


def test_pedir_vinculacion_no_reabre_una_ya_escaneada(settings, tmp_path):
    """El bucle medido: pair-success -> CONNECTING -> /session/pair -> PAIRING.

    El frontend seguia en la pantalla del codigo y volvia a pedir vinculacion;
    esto la devolvia a PAIRING y tiraba abajo la que acababa de conseguirse.
    """
    import dataclasses

    from app.core.runtime import AppRuntime

    aislado = dataclasses.replace(
        settings, session_dir=tmp_path / "s", diagnostics_dir=tmp_path / "d"
    )
    rt = AppRuntime(aislado, owner="pytest", configure_logging=False)
    rt.pairing._on_renew = lambda: None

    rt.pairing.commit()
    rt.state.set(AppState.CONNECTING, reason="dispositivo vinculado")

    rt.iniciar_vinculacion(user_id=None, account_id=None)

    assert rt.state.state is AppState.CONNECTING, (
        "una peticion de vinculacion no puede deshacer un pair-success"
    )


def test_el_vigilante_no_revive_tras_el_pair_success():
    """Cada ronda del vigilante pedia un codigo nuevo contra el servidor."""
    from app.core.pairing import PairingManager

    gestor = PairingManager()
    gestor._on_renew = lambda: None

    gestor.commit()
    gestor.start_watchdog()

    assert gestor._watchdog is None or not gestor._watchdog.is_alive()


# ---------------------------------------------------------------------------
# El historial tiene camino hasta la base
# ---------------------------------------------------------------------------


def test_el_historial_entra_por_un_sink():
    """El fallo mas caro de la migracion, y el mas silencioso.

    El unico camino a PostgreSQL era ``history_compat.set_callback``, un
    parche a pywhats. Sin el, los blobs llegaban, se archivaban en disco y no
    entraba ni un mensaje -- sin un solo error por ninguna parte.
    """
    import inspect

    from app.core.runtime import AppRuntime

    fuente = inspect.getsource(AppRuntime._wire_history_ingestion)
    # Sin los comentarios: uno de ellos NOMBRA el camino viejo para explicar
    # por que se fue, y eso no es una llamada.
    codigo = "".join(
        linea for linea in fuente.splitlines(True) if not linea.strip().startswith("#")
    )
    assert 'sinks["history_sync"] = ingest' in codigo
    assert "set_callback(" not in codigo


def test_el_worker_deja_a_baileys_reintentar_el_descifrado():
    """Los acuses de reintento los llevaba un parche que espiaba el emisor.

    Baileys lo hace por dentro, con el contador REAL y el bloque ``<keys>``
    que pywhats no mandaba.
    """
    fuente = Path("wa_baileys/worker.js").read_text(encoding="utf-8")
    assert "maxMsgRetryCount" in fuente


def test_el_par_lid_se_cosecha_de_la_clave_del_mensaje():
    """La via mas barata que hay: llega sola, sin gastar una peticion de red.

    56 de 57 conversaciones individuales vienen identificadas por ``@lid``;
    sin la correspondencia el panel muestra numeros.
    """
    fuente = Path("wa_baileys/traducir.js").read_text(encoding="utf-8")
    assert "remoteJidAlt" in fuente
    assert "participantAlt" in fuente
