"""Semantica de la reutilizacion de ratchet ante PKMSG repetido.

Estos tests construyen una sesion Signal REAL con las primitivas de pywhats
(x3dh_initiator / ratchet_encrypt / PreKeySignalMessage.encode) y la hacen
pasar por el ``Receiver._decrypt_enc`` autentico. No hay mocks de criptografia:
si el parche debilitara una verificacion, estos tests lo verian.

Escenario reproducido (el que rompe en pywhats 0.2.0):

    1. Alice envia el primer PreKeySignalMessage -> Bob hace X3DH, consume la
       OPK y descifra.
    2. Alice reenvia PreKeySignalMessages del MISMO establecimiento.
    3. Sin el parche: "unknown one-time pre-key id". Con el parche: se
       reutiliza el ratchet y se descifra.
"""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from pywhats.events import JID
from pywhats.signal.experimental import (
    IdentityKeyPair,
    InMemoryIdentityStore,
    InMemorySessionStore,
    PreKeyBundle,
    PreKeySignalMessage,
    SignalMessage,
    SignedPreKey,
    generate_pre_key,
    ratchet_encrypt,
    ratchet_init_alice,
    x3dh_initiator,
    xeddsa_sign,
)
from pywhats.signal.experimental.keys import signal_pubkey
from pywhats.socket.crypto import generate_keypair

SENDER = JID(user="34600111222", server="s.whatsapp.net", device=0)
SESSION_ID = f"{SENDER.user}:{SENDER.device}@{SENDER.server}"


# ---------------------------------------------------------------------------
# Utillaje: un "Bob" (nosotros) y una "Alice" (el peer) reales
# ---------------------------------------------------------------------------


class FakeIdentityProvider:
    """Implementa el ResponderIdentityProvider que consume el Receiver."""

    def __init__(self) -> None:
        priv, pub = generate_keypair()
        self.identity_private = priv
        self.identity_public = pub

        spk_priv, spk_pub = generate_keypair()
        identity = IdentityKeyPair(private=priv, public=pub)
        self._spk = SignedPreKey(
            key_id=1,
            private=spk_priv,
            public=spk_pub,
            signature=xeddsa_sign(identity.private, signal_pubkey(spk_pub)),
        )
        self._opks: dict[int, object] = {}
        self.consumed: list[int] = []

    def add_one_time_pre_key(self, key_id: int):
        opk = generate_pre_key(key_id)
        self._opks[key_id] = opk
        return opk

    # -- API que usa el receiver --
    def get_signed_pre_key(self, key_id: int):
        return self._spk

    def get_one_time_pre_key(self, key_id: int):
        return self._opks.get(key_id)

    def consume_one_time_pre_key(self, key_id: int) -> None:
        self.consumed.append(key_id)
        self._opks.pop(key_id, None)

    @property
    def signed_pre_key(self):
        return self._spk


def make_receiver(identity: FakeIdentityProvider) -> SimpleNamespace:
    """``self`` minimo con exactamente los atributos que toca _decrypt_enc."""
    return SimpleNamespace(
        _identity=identity,
        _sessions=InMemorySessionStore(),
        _identity_store=InMemoryIdentityStore(),
        _atomic=nullcontext,
        _migrate_known_lid_sender=lambda sender: None,
    )


class AliceSession:
    """Lado iniciador: hace X3DH una vez y CONSERVA su ratchet.

    Es el detalle que reproduce el bug real. Mientras Alice no recibe
    respuesta de Bob no sabe que su sesion ya llego, asi que sigue envolviendo
    cada mensaje en un PreKeySignalMessage con la MISMA base key y el MISMO
    opk id, pero avanzando el contador del ratchet (n = 0, 1, 2, ...).

    Un test que rehiciera X3DH en cada mensaje generaria siempre n=0 y no
    reproduciria nada realista.
    """

    def __init__(
        self,
        identity: IdentityKeyPair,
        bob: FakeIdentityProvider,
        opk_id: int | None,
        ephemeral: IdentityKeyPair | None = None,
    ) -> None:
        self.identity = identity
        self.bob = bob
        self.opk_id = opk_id

        opk = bob.get_one_time_pre_key(opk_id) if opk_id is not None else None
        bundle = PreKeyBundle(
            identity_key=bob.identity_public,
            signed_pre_key_id=bob.signed_pre_key.key_id,
            signed_pre_key_public=bob.signed_pre_key.public,
            signed_pre_key_signature=bob.signed_pre_key.signature,
            one_time_pre_key_id=opk_id,
            one_time_pre_key_public=opk.public if opk is not None else None,
        )

        if ephemeral is None:
            eph_priv, eph_pub = generate_keypair()
            ephemeral = IdentityKeyPair(private=eph_priv, public=eph_pub)
        self.ephemeral = ephemeral

        result = x3dh_initiator(identity, bundle, ephemeral)
        self.state = ratchet_init_alice(result.shared_secret, bob.signed_pre_key.public)

    def encrypt(self, plaintext: bytes) -> bytes:
        """Siguiente mensaje del mismo establecimiento, envuelto en PKMSG."""
        ad = self.identity.public + self.bob.identity_public
        header, ciphertext, mac_key = ratchet_encrypt(self.state, plaintext, ad)
        pkmsg = PreKeySignalMessage(
            registration_id=1,
            one_time_pre_key_id=self.opk_id,
            signed_pre_key_id=self.bob.signed_pre_key.key_id,
            base_key=self.ephemeral.public,
            identity_key=self.identity.public,
            message=SignalMessage(header=header, ciphertext=ciphertext),
        )
        return pkmsg.encode(self.identity.public, self.bob.identity_public, mac_key)


@pytest.fixture
def alice() -> IdentityKeyPair:
    priv, pub = generate_keypair()
    return IdentityKeyPair(private=priv, public=pub)


@pytest.fixture
def bob() -> FakeIdentityProvider:
    provider = FakeIdentityProvider()
    provider.add_one_time_pre_key(77)
    return provider


@pytest.fixture
def patched(tmp_path: Path):
    """Aplica el parche sobre un registro limpio y lo revierte al terminar."""
    from pywhats.messaging.receiver import Receiver

    from app.compat import prekey_compat

    original = Receiver._decrypt_enc
    prekey_compat.apply(tmp_path / "compat_prekey.db")
    yield prekey_compat
    Receiver._decrypt_enc = original
    if prekey_compat._registry is not None:
        prekey_compat._registry.close()
        prekey_compat._registry = None


# ---------------------------------------------------------------------------
# El bug, sin parche
# ---------------------------------------------------------------------------


def test_bug_reproducido_sin_parche(alice, bob):
    """Sin parche, el segundo PKMSG del mismo establecimiento revienta."""
    from pywhats.messaging.receiver import Receiver

    receiver = make_receiver(bob)
    session = AliceSession(alice, bob, opk_id=77)
    assert Receiver._decrypt_enc(receiver, SENDER, "pkmsg", session.encrypt(b"primero")) == b"primero"
    assert bob.consumed == [77], "la OPK debe consumirse en el primer mensaje"

    # Mismo establecimiento: misma base key, misma identidad, mismo opk id.
    with pytest.raises(ValueError, match="unknown one-time pre-key id"):
        Receiver._decrypt_enc(receiver, SENDER, "pkmsg", session.encrypt(b"segundo"))


# ---------------------------------------------------------------------------
# El parche
# ---------------------------------------------------------------------------


def test_primer_pkmsg_usa_y_consume_opk(alice, bob, patched):
    """Con parche, el primer mensaje sigue haciendo X3DH y consumiendo la OPK."""
    from pywhats.messaging.receiver import Receiver

    receiver = make_receiver(bob)
    session = AliceSession(alice, bob, opk_id=77)

    assert Receiver._decrypt_enc(receiver, SENDER, "pkmsg", session.encrypt(b"hola")) == b"hola"
    assert bob.consumed == [77]
    assert receiver._sessions.load(SESSION_ID) is not None
    # El establecimiento queda registrado para poder reconocer reenvios.
    assert patched._registry.get(SESSION_ID) is not None


def test_pkmsg_repetido_reutiliza_ratchet(alice, bob, patched):
    """El caso que rompe en pywhats: mismo establecimiento, OPK ya consumida."""
    from pywhats.messaging.receiver import Receiver

    receiver = make_receiver(bob)
    session = AliceSession(alice, bob, opk_id=77)
    assert Receiver._decrypt_enc(receiver, SENDER, "pkmsg", session.encrypt(b"primero")) == b"primero"

    assert bob.get_one_time_pre_key(77) is None, "la OPK ya no existe"

    # Varios reenvios seguidos del mismo establecimiento.
    assert Receiver._decrypt_enc(receiver, SENDER, "pkmsg", session.encrypt(b"segundo")) == b"segundo"
    assert Receiver._decrypt_enc(receiver, SENDER, "pkmsg", session.encrypt(b"tercero")) == b"tercero"

    # No se volvio a consumir ninguna OPK: solo la del primer mensaje.
    assert bob.consumed == [77]


def test_base_key_distinta_no_reutiliza_sesion(alice, bob, patched):
    """Una base key nueva es un handshake nuevo: debe ejecutarse X3DH otra vez.

    Esta es la diferencia entre la semantica de libsignal y el atajo inseguro
    "si hay cualquier sesion, ignora la OPK".
    """
    from pywhats.messaging.receiver import Receiver

    receiver = make_receiver(bob)
    first = AliceSession(alice, bob, opk_id=77)
    assert Receiver._decrypt_enc(receiver, SENDER, "pkmsg", first.encrypt(b"primero")) == b"primero"

    # Rekey: efimera nueva y OPK nueva disponible.
    bob.add_one_time_pre_key(88)
    second = AliceSession(alice, bob, opk_id=88)

    assert Receiver._decrypt_enc(receiver, SENDER, "pkmsg", second.encrypt(b"rekey")) == b"rekey"
    # Se consumio la OPK nueva: hubo X3DH de verdad, no reutilizacion.
    assert bob.consumed == [77, 88]


def test_mac_invalido_sigue_fallando_en_reenvio(alice, bob, patched):
    """El parche no relaja la verificacion del MAC en el camino reutilizado."""
    from pywhats.messaging.receiver import Receiver
    from pywhats.signal.experimental.types import SignalCryptoError

    receiver = make_receiver(bob)
    session = AliceSession(alice, bob, opk_id=77)
    Receiver._decrypt_enc(receiver, SENDER, "pkmsg", session.encrypt(b"primero"))

    # Se corrompe el ultimo byte, que cae dentro del MAC del SignalMessage.
    corrupted = bytearray(session.encrypt(b"segundo"))
    corrupted[-1] ^= 0xFF

    with pytest.raises(SignalCryptoError):
        Receiver._decrypt_enc(receiver, SENDER, "pkmsg", bytes(corrupted))


def test_registro_sobrevive_a_reinicio(alice, bob, tmp_path):
    """El registro es persistente: tras reiniciar el proceso sigue reconociendo.

    Sin persistencia, un reenvio posterior a un reinicio volveria a fallar.
    """
    from pywhats.messaging.receiver import Receiver

    from app.compat import prekey_compat

    original = Receiver._decrypt_enc
    db = tmp_path / "compat_prekey.db"
    try:
        prekey_compat.apply(db)
        receiver = make_receiver(bob)
        session = AliceSession(alice, bob, opk_id=77)
        Receiver._decrypt_enc(receiver, SENDER, "pkmsg", session.encrypt(b"primero"))
        sessions = receiver._sessions
        identities = receiver._identity_store

        # "Reinicio": se cierra el registro y se reaplica sobre el mismo archivo.
        prekey_compat._registry.close()
        prekey_compat._registry = None
        Receiver._decrypt_enc = original
        prekey_compat.apply(db)

        # La sesion Signal sobrevive (en produccion la conserva el SqliteStore).
        receiver2 = make_receiver(bob)
        receiver2._sessions = sessions
        receiver2._identity_store = identities

        assert (
            Receiver._decrypt_enc(receiver2, SENDER, "pkmsg", session.encrypt(b"tras-reinicio"))
            == b"tras-reinicio"
        )
    finally:
        Receiver._decrypt_enc = original
        if prekey_compat._registry is not None:
            prekey_compat._registry.close()
            prekey_compat._registry = None
