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


def _matches(pkmsg: Any, recorded: tuple[bytes, bytes] | None) -> bool:
    """El mensaje pertenece al establecimiento registrado."""
    if recorded is None:
        return False
    base_key, identity_key = recorded
    return pkmsg.base_key == base_key and pkmsg.identity_key == identity_key


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
    global _registry

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

    original = Receiver._decrypt_enc
    if getattr(original, _MARKER, False):
        return True

    def _decrypt_enc(self: Any, sender: Any, enc_type: str, ciphertext: bytes) -> bytes:
        if enc_type != "pkmsg" or _registry is None:
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
            log.info(
                "Reutilizando ratchet existente (sender=%s opk_id=%s base_key_fp=%s); "
                "decrypt success",
                sid,
                opk_id,
                _fingerprint(pkmsg.base_key),
            )
            return plaintext

        # --- Caso 2: base key nueva o sin registro -> X3DH original ----------
        plaintext = original(self, sender, enc_type, ciphertext)
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

    log.info("Adaptacion de reutilizacion de ratchet (PKMSG) aplicada")
    return True
