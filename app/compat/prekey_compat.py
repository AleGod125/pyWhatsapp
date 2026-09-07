"""Reutilizacion del ratchet ante un PreKeySignalMessage repetido.

BUG VERIFICADO (pywhats 0.2.0, ``pywhats/messaging/receiver.py:762-800``).
La rama ``pkmsg`` de ``Receiver._decrypt_enc`` ejecuta X3DH SIEMPRE::

    if pkmsg.one_time_pre_key_id is not None:
        opk = self._identity.get_one_time_pre_key(pkmsg.one_time_pre_key_id)
        if opk is None:
            raise ValueError(f"unknown one-time pre-key id {...}")
    ...
    result = x3dh_responder(identity, spk, opk, pkmsg.identity_key, pkmsg.base_key)
    state = ratchet_init_bob(...)

No comprueba en ningun momento si esa base key YA establecio una sesion. La
secuencia real que rompe:

    1. Llega el primer PreKeySignalMessage -> la OPK existe -> X3DH -> sesion
       creada -> descifrado correcto -> la OPK se consume (correctamente).
    2. WhatsApp envia mas PreKeySignalMessage del MISMO establecimiento: misma
       base key, misma identidad, mismo OPK id.
    3. pywhats vuelve a pedir esa OPK, que ya no existe porque se consumio
       bien, y lanza "unknown one-time pre-key id".

SEMANTICA APLICADA (la de libsignal, no un atajo):

  * Si el PreKeySignalMessage corresponde a un establecimiento YA REGISTRADO
    para esa (session_id, base_key, identity_key) y la sesion sigue viva:
    NO se ejecuta X3DH, NO se pide OPK, NO se consume OPK. Se reutiliza el
    ratchet existente y se descifra el SignalMessage interno.
  * Si la base key es DISTINTA, es un handshake/rekey nuevo: se delega en el
    codigo original, que hara X3DH. No se reutiliza sesion a ciegas.

Lo que este parche NO hace, deliberadamente:

  * No debilita nada. El MAC se sigue verificando con exactamente la misma
    llamada (``pkmsg.message.verify_mac``) que usa pywhats.
  * No aplica la regla insegura "si hay cualquier sesion, ignora la OPK".
    Sin coincidencia de base key se delega en el original.

ESTADO PROPIO: el registro (session_id -> base_key) vive en un SQLite aparte,
``session/compat_prekey.db``, que es NUESTRO. No se toca ni se duplica el
Signal Store de pywhats (``device.json.signal.db``).
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("SIGNAL")

_MARKER = "_whatsapp_backup_prekey_patch"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prekey_establishments (
    session_id          TEXT PRIMARY KEY,
    base_key            BLOB NOT NULL,
    identity_key        BLOB NOT NULL,
    one_time_pre_key_id INTEGER,
    established_at      INTEGER NOT NULL
)
"""


def _fingerprint(data: bytes) -> str:
    """Hash parcial NO reversible, solo para correlacionar en los logs.

    Nunca se loguea la clave. 8 hex chars bastan para seguir un
    establecimiento dentro de una ejecucion.
    """
    return hashlib.sha256(data).hexdigest()[:8]


class EstablishmentRegistry:
    """Registro persistente de que base key establecio cada sesion."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: el receiver de pywhats corre en el event
        # loop, que puede vivir en otro hilo que el que construye esto.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def get(self, session_id: str) -> tuple[bytes, bytes] | None:
        """``(base_key, identity_key)`` del establecimiento registrado."""
        row = self._conn.execute(
            "SELECT base_key, identity_key FROM prekey_establishments WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return (row[0], row[1]) if row is not None else None

    def record(
        self, session_id: str, base_key: bytes, identity_key: bytes, opk_id: int | None
    ) -> None:
        self._conn.execute(
            "INSERT INTO prekey_establishments "
            "(session_id, base_key, identity_key, one_time_pre_key_id, established_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "base_key=excluded.base_key, identity_key=excluded.identity_key, "
            "one_time_pre_key_id=excluded.one_time_pre_key_id, "
            "established_at=excluded.established_at",
            (session_id, base_key, identity_key, opk_id, int(time.time())),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


_registry: EstablishmentRegistry | None = None

#: La ruta del registro, recordada para poder reabrirlo solo.
#:
#: EL FALLO QUE ESTO CIERRA, MEDIDO EN UNA VINCULACION NUEVA
#: ---------------------------------------------------------
#: ``archive_session`` cierra el registro y lo deja en ``None`` --hace falta
#: para poder mover el archivo en Windows-- y ``apply()`` solo corre una vez,
#: en ``prepare_pywhats``, ANTES de crear el cliente. Si la sesion se archiva
#: despues (un 401, un re-emparejamiento) nadie lo vuelve a abrir.
#:
#: Y el cuerpo del parche, con ``_registry`` en ``None``, cae de largo al
#: camino original SIN DECIR NADA. La compatibilidad queda muerta para el
#: resto de la vida del proceso.
#:
#: Se midio exactamente eso::
#:
#:     22:06:06  Adaptaciones activas: ... prekey_replay ...
#:     22:06:29  Sesion archivada (revoked-401)      <- _registry = None
#:     22:07:03  vinculacion nueva
#:     22:07:04  uploaded 50 one-time prekeys (1..50)
#:     22:07:26  primer mensaje de 855390@lid        -> OK, consume la OPK 21
#:     22:07:52  segundo mensaje del MISMO dispositivo
#:               unknown one-time pre-key id 21      -> y ya no hay quien lo salve
#:
#: Cero lineas ``PKMSG sender=`` en toda la ventana, y ``compat_prekey.db``
#: sin llegar a existir: la prueba de que el parche no se ejecuto ni una vez.
_ruta_del_registro: Path | None = None


# ---------------------------------------------------------------------------
# Establecimientos que NO se pueden completar
# ---------------------------------------------------------------------------
#
# EL CASO, MEDIDO
# ---------------
# Un dispositivo de un contacto (``206566***:44@lid``) lleva desde el 3 de
# septiembre mandando PreKeySignalMessage con la MISMA base key
# (``b7331e3e``) y la MISMA clave de un solo uso (``17``). Esa clave se
# consumio, correctamente, en otro establecimiento anterior, y su parte
# privada ya no existe.
#
# Sin esa privada no se puede completar el X3DH. No es que falte codigo: el
# secreto compartido no se puede derivar. El mensaje es indescifrable, y va a
# seguir siendolo.
#
# LO QUE SE HACIA, Y POR QUE NO SERVIA
# ------------------------------------
# Por cada intento se mandaba un acuse de reintento, y a partir del segundo
# con material publico para que el emisor rehiciera el saludo. Se midio: 69
# acuses a ese dispositivo, 17 de ellos CON material, 180 intentos de
# descifrado. La base key nunca cambio. El emisor no rehace el saludo.
#
# Asi que a partir de cierto punto insistir solo gasta: una clave de un solo
# uso por acuse con material, y ruido en el registro. Se sigue intentando
# descifrar cada mensaje --si el emisor rehace el saludo se vera al instante,
# porque la base key sera otra-- pero se deja de pedir lo que no llega.
#
# NO se descarta ningun mensaje, NO se marca nada como leido y NO se toca
# Signal. Lo unico que se corta es el acuse.

#: Acuses por establecimiento antes de dejar de pedir. Cinco: bastante para
#: que un emisor que SI reacciona tenga ocasion de hacerlo.
ACUSES_ANTES_DE_DESISTIR = 5

_imposibles: dict[str, dict[str, Any]] = {}
MAXIMO_DE_IMPOSIBLES = 200


def _anotar_imposible(sid: str, base_key: bytes, opk_id: int | None) -> None:
    """Anota que este establecimiento no se puede completar.

    Si la base key cambia, el emisor rehizo el saludo: se empieza de cero,
    porque ese intento SI puede salir bien.
    """
    huella = _fingerprint(base_key)
    anotado = _imposibles.get(sid)
    if anotado is None or anotado.get("base_key_fp") != huella:
        if len(_imposibles) >= MAXIMO_DE_IMPOSIBLES:
            _imposibles.pop(next(iter(_imposibles)))
        _imposibles[sid] = {
            "base_key_fp": huella,
            "opk_id": opk_id,
            "fallos": 1,
            "desde": time.time(),
        }
        return
    anotado["fallos"] = int(anotado.get("fallos", 0)) + 1


def insistir_es_inutil(sid: str) -> bool:
    """Si ya se pidio bastante por un establecimiento que no puede completarse."""
    anotado = _imposibles.get(sid)
    if anotado is None:
        return False
    return int(anotado.get("fallos", 0)) > ACUSES_ANTES_DE_DESISTIR


def establecimientos_imposibles() -> dict[str, dict[str, Any]]:
    """Copia de lo anotado. Para diagnostico; no se modifica desde fuera."""
    return {sid: dict(datos) for sid, datos in _imposibles.items()}


def olvidar_imposibles() -> None:
    """Para las pruebas y para un re-emparejamiento."""
    _imposibles.clear()


def _matches(pkmsg: Any, recorded: tuple[bytes, bytes] | None) -> bool:
    """El mensaje pertenece al establecimiento registrado."""
    if recorded is None:
        return False
    base_key, identity_key = recorded
    return pkmsg.base_key == base_key and pkmsg.identity_key == identity_key


def _reabrir_registro() -> bool:
    """Vuelve a abrir el registro tras un archivado. ``False`` si no se puede.

    No inventa la ruta: usa la que dejo ``apply()``. Si nunca se aplico la
    compatibilidad no hay nada que reabrir y se contesta que no.
    """
    global _registry

    if _ruta_del_registro is None:
        return False
    try:
        _registry = EstablishmentRegistry(_ruta_del_registro)
    except Exception:  # noqa: BLE001 - no poder abrirlo no rompe la recepcion
        log.warning(
            "[SIGNAL] no se pudo reabrir el registro de establecimientos: los "
            "PreKeySignalMessage repetidos volveran a pedir una clave de un "
            "solo uso ya consumida",
            exc_info=True,
        )
        return False
    log.info(
        "[SIGNAL] registro de establecimientos reabierto tras archivar la "
        "sesion; la reutilizacion de ratchet vuelve a estar activa"
    )
    return True


def apply(store_path: Path | None = None) -> bool:
    """Instala el parche sobre ``Receiver._decrypt_enc``. Idempotente.

    El parche se pone una sola vez, pero el REGISTRO se reabre siempre. Son
    dos cosas con vidas distintas y confundirlas costo caro:

    ``archive_session`` cierra el registro y lo deja en ``None`` para poder
    mover el archivo en Windows. Si al arrancar el cliente nuevo esta funcion
    saliera pronto por estar ya parcheada, ``_registry`` se quedaria en
    ``None`` para siempre y el cuerpo del parche caeria de largo al camino
    original. Resultado: tras un re-pairing en el mismo proceso, la
    reutilizacion de ratchet deja de existir en silencio y vuelve
    ``unknown one-time pre-key id N`` en cada PKMSG reenviado.
    """
    global _registry, _ruta_del_registro

    from pywhats.messaging.addressing import session_id
    from pywhats.messaging.receiver import Receiver
    from pywhats.signal.experimental.ratchet import ratchet_decrypt
    from pywhats.signal.experimental.types import PreKeySignalMessage

    if store_path is None:
        raise ValueError("prekey_compat.apply() necesita la ruta del registro")

    # Primero el registro, este parcheado o no: es lo que la sesion nueva
    # necesita, y apunta al archivo de la sesion nueva.
    if _registry is not None and getattr(_registry, "_path", None) != store_path:
        # Cambio de sesion: el registro anterior describe otra identidad.
        try:
            _registry.close()
        except Exception:  # noqa: BLE001 - cerrar no puede impedir seguir
            log.debug("No se pudo cerrar el registro anterior")
        _registry = None
    if _registry is None:
        _registry = EstablishmentRegistry(store_path)
    _ruta_del_registro = store_path

    original = Receiver._decrypt_enc
    if getattr(original, _MARKER, False):
        return True

    def _decrypt_enc(self: Any, sender: Any, enc_type: str, ciphertext: bytes) -> bytes:
        if enc_type != "pkmsg":
            return original(self, sender, enc_type, ciphertext)

        # Si el registro se cerro al archivar la sesion, se reabre AQUI.
        #
        # Antes se caia de largo al original en silencio y la compatibilidad
        # quedaba muerta hasta reiniciar el proceso. Reabrirlo solo es lo unico
        # que no depende de que alguien se acuerde de rearmarla despues de cada
        # archivado, que es justo lo que no ocurrio.
        if _registry is None and not _reabrir_registro():
            return original(self, sender, enc_type, ciphertext)

        # Misma normalizacion de direccion que hace el original antes de
        # calcular el session id. Es idempotente (receiver.py:855 sale pronto
        # si ya hay sesion en la clave LID).
        self._migrate_known_lid_sender(sender)
        sid = session_id(sender)

        pkmsg = PreKeySignalMessage.decode(ciphertext)
        opk_id = pkmsg.one_time_pre_key_id
        recorded = _registry.get(sid)
        existing_session = self._sessions.load(sid)
        matching = _matches(pkmsg, recorded)

        log.debug(
            "PKMSG sender=%s opk_id=%s existing_session=%s matching_prekey_session=%s "
            "base_key_fp=%s",
            sid,
            opk_id,
            "yes" if existing_session is not None else "no",
            "yes" if matching else "no",
            _fingerprint(pkmsg.base_key),
        )

        # --- Caso 1: reenvio del mismo establecimiento -> reutilizar ratchet --
        if matching and existing_session is not None:
            peer_identity = self._identity_store.load(sid) or pkmsg.identity_key
            ad = peer_identity + self._identity.identity_public
            # El MAC se verifica igual que en el camino original: no se
            # relaja ninguna comprobacion, solo se evita rehacer X3DH.
            plaintext = ratchet_decrypt(
                existing_session,
                pkmsg.message.header,
                pkmsg.message.ciphertext,
                ad,
                verify_mac=lambda mac_key: pkmsg.message.verify_mac(
                    peer_identity,
                    self._identity.identity_public,
                    mac_key,
                ),
            )
            self._sessions.save(sid, existing_session)
            # Por mensaje. En INFO llenaria la consola en cuanto hay
            # trafico; lo que interesa en INFO es que la compat este puesta.
            log.debug(
                "Reutilizando ratchet existente (sender=%s opk_id=%s base_key_fp=%s); "
                "decrypt success",
                sid,
                opk_id,
                _fingerprint(pkmsg.base_key),
            )
            return plaintext

        # --- Caso 1b: hay sesion pero NO hay registro -> lo dice el MAC ------
        #
        # EL CASO, MEDIDO
        # ---------------
        # El establecimiento de un contacto quedo HUERFANO: su sesion existe
        # en el Signal Store, pero el registro no lo tiene porque cuando se
        # creo, la compatibilidad estaba muerta (el registro se habia cerrado
        # al archivar la sesion y nadie lo reabrio)::
        #
        #     sesiones Signal:   85539055239413:0@lid   <- existe
        #     establecimientos:  (ninguno para ese sid)  <- se perdio
        #     OPK 21:            consumida
        #
        # Resultado: cada reenvio cae al X3DH original, que pide la OPK 21 y
        # falla. Para siempre, porque la privada de esa clave ya no existe y
        # el registro no se puede reconstruir --las base key no se guardan en
        # ningun otro sitio.
        #
        # POR QUE ESTO NO RELAJA NADA
        # ---------------------------
        # El registro solo sirve para DECIDIR que camino intentar. Quien
        # autentica es el MAC, y aqui se verifica con la misma llamada de
        # siempre. Si el mensaje pertenece de verdad a ese ratchet, el MAC
        # cuadra y queda demostrado --prueba criptografica, mas fuerte que
        # nuestra contabilidad--. Si no pertenece, el MAC falla y se sigue por
        # el camino original, que es exactamente lo que pasa hoy.
        #
        # Ademas se exige que la identidad del emisor sea la MISMA que quedo
        # fijada al crear la sesion: no se prueba nada contra un tercero.
        if not matching and existing_session is not None:
            fijada = self._identity_store.load(sid)
            if fijada is not None and fijada == pkmsg.identity_key:
                ad = fijada + self._identity.identity_public
                try:
                    plaintext = ratchet_decrypt(
                        existing_session,
                        pkmsg.message.header,
                        pkmsg.message.ciphertext,
                        ad,
                        verify_mac=lambda mac_key: pkmsg.message.verify_mac(
                            fijada,
                            self._identity.identity_public,
                            mac_key,
                        ),
                    )
                except Exception:  # noqa: BLE001 - no era ese ratchet: sigue igual
                    log.debug(
                        "Sesion sin registro: el ratchet existente NO descifra "
                        "(sender=%s base_key_fp=%s); se delega en el original",
                        sid,
                        _fingerprint(pkmsg.base_key),
                    )
                else:
                    # El MAC cuadro: este mensaje pertenece a esa sesion. Se
                    # anota el establecimiento para no repetir la prueba.
                    self._sessions.save(sid, existing_session)
                    _registry.record(
                        sid, pkmsg.base_key, pkmsg.identity_key, opk_id
                    )
                    log.info(
                        "[SIGNAL] establecimiento huerfano recuperado por MAC "
                        "(sender=%s base_key_fp=%s opk_id=%s): la sesion existia "
                        "pero no estaba registrada",
                        sid,
                        _fingerprint(pkmsg.base_key),
                        opk_id,
                    )
                    return plaintext

        # --- Caso 2: base key nueva o sin registro -> X3DH original ----------
        try:
            plaintext = original(self, sender, enc_type, ciphertext)
        except Exception as exc:  # noqa: BLE001 - se clasifica y se RELANZA
            # Una clave de un solo uso que ya no existe no vuelve a existir:
            # se anota para dejar de pedir por ese establecimiento, y el
            # error sigue su camino igual que antes.
            if "unknown one-time pre-key" in str(exc):
                _anotar_imposible(sid, pkmsg.base_key, opk_id)
                # Y se anota el AGUJERO: ese mensaje existe, tiene su hora, y
                # no se va a poder leer nunca por esta via. Lo que falta esta
                # en el borde reciente y se cierra pidiendo historial, que
                # llega firmado y por el camino de siempre.
                try:
                    from app.services.missed_live import anotar_perdido

                    # La huella del texto cifrado identifica el mensaje sin
                    # revelar nada: aqui el WAMID todavia va dentro de lo que
                    # no se ha podido descifrar.
                    anotar_perdido(sender, _fingerprint(ciphertext), str(exc))
                except Exception:  # noqa: BLE001 - anotar no puede cortar nada
                    log.debug("No se pudo anotar el agujero del borde")
            raise
        # Solo se registra tras un descifrado correcto: si el original hubiera
        # fallado, no habria establecimiento que anotar.
        _registry.record(sid, pkmsg.base_key, pkmsg.identity_key, opk_id)
        log.debug(
            "Establecimiento registrado sender=%s base_key_fp=%s opk_id=%s",
            sid,
            _fingerprint(pkmsg.base_key),
            opk_id,
        )
        return plaintext

    setattr(_decrypt_enc, _MARKER, True)
    Receiver._decrypt_enc = _decrypt_enc  # type: ignore[method-assign]

    log.debug("Adaptacion de reutilizacion de ratchet (PKMSG) aplicada")
    return True
