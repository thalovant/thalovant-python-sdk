"""HiveMind protocol v3: the Noise handshake and its transport framing.

A HiveMind-core 5.x hub accepts one key exchange, a Noise handshake whose
pre-shared key is stretched from the connection password, and nothing else.

The state machine below is written out against the Noise Protocol Framework
(revision 34) for the two patterns the protocol registers, ``XXpsk2`` and
``KKpsk0``. Every primitive underneath it (X25519, ChaCha20-Poly1305,
AES-GCM, argon2id) comes from ``cryptography``; SHA-256 and HMAC come from the
standard library. What is hand-written is the sequencing, and the tests check
it against the reference implementation's vectors and against ``noiseprotocol``
itself, which is what the hub runs.

Everything here is synchronous and does no I/O. The one expensive call,
:func:`derive_psk`, is CPU-bound (argon2id at 64 MiB); callers run it off the
event loop.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

PATTERN_KK: Final = "KKpsk0"
"""Both static keys are known before the handshake starts."""
PATTERN_XX: Final = "XXpsk2"
"""First contact: static keys are exchanged, and the hub's is pinned."""

SUITE_CHACHA: Final = "25519_ChaChaPoly_SHA256"
SUITE_AESGCM: Final = "25519_AESGCM_SHA256"
SUITES: Final = (SUITE_CHACHA, SUITE_AESGCM)
"""Our suite preference. Selection walks this list, not the hub's."""

# argon2id parameters for the password -> PSK derivation. They are part of the
# wire contract: a peer deriving with other parameters gets another key, and
# the handshake then fails exactly as it does on a wrong password.
_PSK_TIME_COST: Final = 3
_PSK_MEMORY_KIB: Final = 64 * 1024
_PSK_LANES: Final = 1
_PSK_LENGTH: Final = 32

# The first plaintext byte of every transport message tags its framing.
FRAME_JSON: Final = 0x00
FRAME_BINARY: Final = 0x01
FRAME_FIRST_JSON: Final = 0x02
FRAME_FIRST_BINARY: Final = 0x03
FRAME_MORE: Final = 0x04
FRAME_LAST: Final = 0x05

MAX_MESSAGE: Final = 65535
"""A Noise transport message never exceeds this many bytes."""
CHUNK_SIZE: Final = 65000
"""Plaintext per chunk, leaving room for the AEAD tag and the marker."""
MAX_REASSEMBLY: Final = 32 * 1024 * 1024
"""A chunked message is dropped once it buffers more than this."""

_HASH_LEN: Final = 32
_KEY_LEN: Final = 32
_TAG_LEN: Final = 16
_MAX_NONCE: Final = 2**64 - 1  # reserved by the spec; never used as a nonce

Token = Literal["e", "s", "ee", "es", "se", "ss", "psk"]


class NoiseError(Exception):
    """The handshake or the transport failed. Always fatal for the session."""


def derive_psk(password: str, node_id: str) -> bytes:
    """Stretch the connection password into the 32-byte pre-shared key.

    Salted with SHA-256 of the *hub's* node id. Costs 64 MiB and about a tenth
    of a second of CPU, and the answer is fixed for a ``(password, node_id)``
    pair, so callers derive it once, off the event loop, and keep it.
    """
    kdf = Argon2id(
        salt=hashlib.sha256(node_id.encode("utf-8")).digest(),
        length=_PSK_LENGTH,
        iterations=_PSK_TIME_COST,
        lanes=_PSK_LANES,
        memory_cost=_PSK_MEMORY_KIB,
    )
    return kdf.derive(password.encode("utf-8"))


def protocol_name(pattern: str, suite: str) -> str:
    """The full Noise protocol name for a pattern and suite selection."""
    return f"Noise_{pattern}_{suite}"


def select_options(
    patterns: Sequence[str], suites: Sequence[str], *, pinned: bool
) -> tuple[str, str] | None:
    """Pick a pattern and suite from what the hub offers.

    ``KKpsk0`` only when we already hold the hub's static key and the hub
    offers it; otherwise ``XXpsk2``. ``None`` when there is nothing in common.
    """
    suite = next((candidate for candidate in SUITES if candidate in suites), None)
    if suite is None:
        return None
    if pinned and PATTERN_KK in patterns:
        return PATTERN_KK, suite
    if PATTERN_XX in patterns:
        return PATTERN_XX, suite
    return None


def canonical_json(value: Any) -> bytes:
    """Serialize a JSON value exactly as the reference implementation does.

    Sorted keys, no whitespace, no ASCII escaping. Both peers hash these bytes
    into the prologue, so one byte of difference fails the handshake.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def build_prologue(hello: Mapping[str, Any], handshake: Mapping[str, Any], name: str) -> bytes:
    """The hub's cleartext HELLO and HANDSHAKE payloads, then the protocol name.

    This is the downgrade protection: a peer that saw a different negotiation
    derives a different transcript and the handshake aborts.
    """
    return canonical_json(hello) + canonical_json(handshake) + name.encode("utf-8")


def _nonce(counter: int, *, little_endian: bool) -> bytes:
    # Four zero bytes, then the 64-bit counter. The byte order is per cipher
    # (Noise rev 34 section 12): ChaChaPoly little-endian, AESGCM big-endian.
    # The two agree only at zero, so the wrong one fails on the second message.
    return b"\x00" * 4 + counter.to_bytes(8, "little" if little_endian else "big")


class _Aead:
    """One suite's AEAD, keyed per call."""

    def __init__(self, suite: str) -> None:
        if suite == SUITE_CHACHA:
            self._chacha = True
        elif suite == SUITE_AESGCM:
            self._chacha = False
        else:
            raise NoiseError(f"Unsupported Noise cipher suite {suite}.")

    def encrypt(self, key: bytes, counter: int, ad: bytes, plaintext: bytes) -> bytes:
        nonce = _nonce(counter, little_endian=self._chacha)
        if self._chacha:
            return ChaCha20Poly1305(key).encrypt(nonce, plaintext, ad)
        return AESGCM(key).encrypt(nonce, plaintext, ad)

    def decrypt(self, key: bytes, counter: int, ad: bytes, ciphertext: bytes) -> bytes:
        nonce = _nonce(counter, little_endian=self._chacha)
        if self._chacha:
            return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, ad)
        return AESGCM(key).decrypt(nonce, ciphertext, ad)


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _hkdf(chaining_key: bytes, material: bytes, outputs: int) -> list[bytes]:
    temp = _hmac(chaining_key, material)
    first = _hmac(temp, b"\x01")
    second = _hmac(temp, first + b"\x02")
    if outputs == 2:
        return [first, second]
    return [first, second, _hmac(temp, second + b"\x03")]


class CipherState:
    """One direction: a key and a strictly sequential nonce counter."""

    def __init__(self, aead: _Aead, key: bytes | None = None) -> None:
        self._aead = aead
        self._key = key
        self._counter = 0

    @property
    def has_key(self) -> bool:
        return self._key is not None

    def encrypt_with_ad(self, ad: bytes, plaintext: bytes) -> bytes:
        if self._key is None:
            return plaintext
        if self._counter >= _MAX_NONCE:
            raise NoiseError("Noise nonce space exhausted.")
        sealed = self._aead.encrypt(self._key, self._counter, ad, plaintext)
        self._counter += 1
        return sealed

    def decrypt_with_ad(self, ad: bytes, ciphertext: bytes) -> bytes:
        if self._key is None:
            return ciphertext
        if self._counter >= _MAX_NONCE:
            raise NoiseError("Noise nonce space exhausted.")
        try:
            plaintext = self._aead.decrypt(self._key, self._counter, ad, ciphertext)
        except InvalidTag as exc:
            raise NoiseError("Noise message failed authentication.") from exc
        self._counter += 1
        return plaintext


class _SymmetricState:
    """The chaining key and the running transcript hash."""

    def __init__(self, aead: _Aead, name: str) -> None:
        encoded = name.encode("utf-8")
        if len(encoded) <= _HASH_LEN:
            self.hash = encoded.ljust(_HASH_LEN, b"\x00")
        else:
            self.hash = hashlib.sha256(encoded).digest()
        self.chaining_key = self.hash
        self._aead = aead
        self.cipher = CipherState(aead)

    def mix_hash(self, data: bytes) -> None:
        self.hash = hashlib.sha256(self.hash + data).digest()

    def mix_key(self, material: bytes) -> None:
        self.chaining_key, temp_key = _hkdf(self.chaining_key, material, 2)
        self.cipher = CipherState(self._aead, temp_key[:_KEY_LEN])

    def mix_key_and_hash(self, material: bytes) -> None:
        self.chaining_key, temp_hash, temp_key = _hkdf(self.chaining_key, material, 3)
        self.mix_hash(temp_hash)
        self.cipher = CipherState(self._aead, temp_key[:_KEY_LEN])

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ciphertext = self.cipher.encrypt_with_ad(self.hash, plaintext)
        self.mix_hash(ciphertext)
        return ciphertext

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        plaintext = self.cipher.decrypt_with_ad(self.hash, ciphertext)
        self.mix_hash(ciphertext)
        return plaintext

    def split(self) -> tuple[CipherState, CipherState]:
        first, second = _hkdf(self.chaining_key, b"", 2)
        return (
            CipherState(self._aead, first[:_KEY_LEN]),
            CipherState(self._aead, second[:_KEY_LEN]),
        )


@dataclass(frozen=True)
class _Shape:
    initiator_pre: tuple[Token, ...]
    responder_pre: tuple[Token, ...]
    messages: tuple[tuple[Token, ...], ...]


# The psk modifier is already applied: psk0 opens message 1, psk2 closes
# message 2. In every psk handshake the ephemeral key is also mixed into the
# chaining key, so the psk cannot be attacked offline.
_SHAPES: Final[dict[str, _Shape]] = {
    PATTERN_XX: _Shape((), (), (("e",), ("e", "ee", "s", "es", "psk"), ("s", "se"))),
    PATTERN_KK: _Shape(("s",), ("s",), (("psk", "e", "es", "ss"), ("e", "ee", "se"))),
}


def generate_private_key() -> bytes:
    """A fresh X25519 private key, raw."""
    return X25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


def public_key(private_key: bytes) -> bytes:
    """The raw X25519 public key of a raw private key."""
    return (
        X25519PrivateKey.from_private_bytes(private_key)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )


def _dh(private_key: bytes, remote_public: bytes) -> bytes:
    try:
        return X25519PrivateKey.from_private_bytes(private_key).exchange(
            X25519PublicKey.from_public_bytes(remote_public)
        )
    except ValueError as exc:  # a low-order point gives an all-zero secret
        raise NoiseError("Noise key agreement failed.") from exc


class NoiseHandshake:
    """One side of a v3 handshake.

    The library only ever initiates. The responder side exists so the tests
    can stand up a genuine peer instead of a stub that agrees with anything.
    """

    def __init__(
        self,
        pattern: str,
        suite: str,
        psk: bytes,
        prologue: bytes,
        static_private: bytes,
        *,
        remote_static: bytes | None = None,
        initiator: bool = True,
    ) -> None:
        shape = _SHAPES.get(pattern)
        if shape is None:
            raise NoiseError(f"Unsupported Noise pattern {pattern}.")
        if len(psk) != _KEY_LEN:
            raise NoiseError("The Noise pre-shared key must be 32 bytes.")
        if pattern == PATTERN_KK and (remote_static is None or len(remote_static) != _KEY_LEN):
            raise NoiseError("KKpsk0 needs the peer's 32-byte static key.")
        self.pattern = pattern
        self._initiator = initiator
        self._psk = psk
        self._messages = shape.messages
        self._index = 0
        self._static = static_private
        self._static_public = public_key(static_private)
        self._ephemeral: bytes | None = None
        self._remote_static = remote_static if pattern == PATTERN_KK else None
        self._remote_ephemeral: bytes | None = None
        self._transport: tuple[CipherState, CipherState] | None = None

        self._symmetric = _SymmetricState(_Aead(suite), protocol_name(pattern, suite))
        self._symmetric.mix_hash(prologue)
        # Pre-message keys enter the transcript initiator first, whichever
        # side we are.
        local, remote = self._static_public, self._remote_static
        ordered = (
            ((shape.initiator_pre, local), (shape.responder_pre, remote))
            if initiator
            else ((shape.initiator_pre, remote), (shape.responder_pre, local))
        )
        for tokens, key in ordered:
            for token in tokens:
                if token == "s" and key is not None:
                    self._symmetric.mix_hash(key)

    @property
    def finished(self) -> bool:
        return self._transport is not None

    @property
    def remote_static(self) -> bytes | None:
        """The peer's static public key, once it is known."""
        return self._remote_static

    def write_message(self, payload: bytes = b"") -> bytes:
        """Produce the next outgoing handshake message."""
        if self._index >= len(self._messages):
            raise NoiseError("The Noise handshake has no message left to write.")
        tokens = self._messages[self._index]
        self._index += 1
        out = bytearray()
        for token in tokens:
            if token == "e":
                self._ephemeral = generate_private_key()
                ephemeral_public = public_key(self._ephemeral)
                out += ephemeral_public
                self._symmetric.mix_hash(ephemeral_public)
                self._symmetric.mix_key(ephemeral_public)
            elif token == "s":
                out += self._symmetric.encrypt_and_hash(self._static_public)
            elif token == "psk":
                self._symmetric.mix_key_and_hash(self._psk)
            else:
                self._symmetric.mix_key(self._dh(token))
        out += self._symmetric.encrypt_and_hash(payload)
        self._finish_if_done()
        return bytes(out)

    def read_message(self, message: bytes) -> bytes:
        """Consume an incoming handshake message and return its payload.

        Any failure is authentication failing: a wrong password, a tampered
        negotiation, or a static key that contradicts the pinned one.
        """
        if self._index >= len(self._messages):
            raise NoiseError("The Noise handshake has no message left to read.")
        tokens = self._messages[self._index]
        self._index += 1
        rest = bytes(message)
        for token in tokens:
            if token == "e":
                if len(rest) < _KEY_LEN:
                    raise NoiseError("Truncated Noise handshake message.")
                self._remote_ephemeral, rest = rest[:_KEY_LEN], rest[_KEY_LEN:]
                self._symmetric.mix_hash(self._remote_ephemeral)
                self._symmetric.mix_key(self._remote_ephemeral)
            elif token == "s":
                size = _KEY_LEN + _TAG_LEN if self._symmetric.cipher.has_key else _KEY_LEN
                if len(rest) < size:
                    raise NoiseError("Truncated Noise handshake message.")
                learned = self._symmetric.decrypt_and_hash(rest[:size])
                rest = rest[size:]
                if self._remote_static is not None and not hmac.compare_digest(
                    self._remote_static, learned
                ):
                    raise NoiseError("The peer's static key contradicts the pinned one.")
                self._remote_static = learned
            elif token == "psk":
                self._symmetric.mix_key_and_hash(self._psk)
            else:
                self._symmetric.mix_key(self._dh(token))
        payload = self._symmetric.decrypt_and_hash(rest)
        self._finish_if_done()
        return payload

    def into_session(self) -> NoiseSession:
        """The completed transport. Split() yields the initiator's send state first."""
        if self._transport is None:
            raise NoiseError("The Noise handshake is not finished.")
        first, second = self._transport
        send, receive = (first, second) if self._initiator else (second, first)
        return NoiseSession(send, receive, self._remote_static)

    def _finish_if_done(self) -> None:
        if self._index >= len(self._messages) and self._transport is None:
            self._transport = self._symmetric.split()

    def _dh(self, token: Token) -> bytes:
        # "es" is always initiator-ephemeral with responder-static and "se" the
        # reverse, so which local key a token names flips with the role.
        local: bytes | None
        remote: bytes | None
        if token == "ee":
            local, remote = self._ephemeral, self._remote_ephemeral
        elif token == "ss":
            local, remote = self._static, self._remote_static
        elif (token == "es") == self._initiator:
            local, remote = self._ephemeral, self._remote_static
        else:
            local, remote = self._static, self._remote_ephemeral
        if local is None or remote is None:
            raise NoiseError(f"The Noise handshake reached {token!r} without both keys.")
        return _dh(local, remote)


@dataclass(frozen=True, slots=True)
class Frame:
    """One complete decrypted message."""

    payload: bytes
    is_json: bool


class NoiseSession:
    """A completed session: the two cipher states and the frame markers."""

    def __init__(
        self, send: CipherState, receive: CipherState, remote_static: bytes | None
    ) -> None:
        self._send = send
        self._receive = receive
        self.remote_static = remote_static
        self._buffer: bytearray | None = None
        self._buffer_is_json = False

    def encrypt_message(self, payload: bytes, *, is_json: bool = True) -> list[bytes]:
        """Encrypt one message into the transport messages that carry it.

        The frames must reach the wire contiguously and in order: the nonce
        counter is strictly sequential, and the receiver refuses a complete
        frame arriving in the middle of a chunked one.
        """
        single = FRAME_JSON if is_json else FRAME_BINARY
        first = FRAME_FIRST_JSON if is_json else FRAME_FIRST_BINARY
        if len(payload) <= CHUNK_SIZE:
            return [self._seal(single, payload)]
        frames: list[bytes] = []
        last_offset = len(payload) - CHUNK_SIZE
        for offset in range(0, len(payload), CHUNK_SIZE):
            if offset == 0:
                marker = first
            elif offset >= last_offset:
                marker = FRAME_LAST
            else:
                marker = FRAME_MORE
            frames.append(self._seal(marker, payload[offset : offset + CHUNK_SIZE]))
        return frames

    def decrypt_frame(self, data: bytes) -> Frame | None:
        """Decrypt one transport message.

        Returns the message once it is complete, ``None`` while a chunked one
        is still arriving. Every error is fatal for the session: a message
        that does not decrypt at the current counter means tampering, replay
        or reordering, and the connection is dropped rather than the frame.
        """
        if len(data) > MAX_MESSAGE:
            raise NoiseError("Noise transport message exceeds 65535 bytes.")
        plaintext = self._receive.decrypt_with_ad(b"", bytes(data))
        if not plaintext:
            raise NoiseError("Empty Noise transport message.")
        marker, body = plaintext[0], plaintext[1:]
        if marker in (FRAME_JSON, FRAME_BINARY):
            if self._buffer is not None:
                self._buffer = None
                raise NoiseError("A complete frame arrived inside a chunked message.")
            return Frame(body, marker == FRAME_JSON)
        if marker in (FRAME_FIRST_JSON, FRAME_FIRST_BINARY):
            if self._buffer is not None:
                self._buffer = None
                raise NoiseError("A chunked message started inside another one.")
            self._buffer = bytearray(body)
            self._buffer_is_json = marker == FRAME_FIRST_JSON
            self._check_cap()
            return None
        if marker in (FRAME_MORE, FRAME_LAST):
            if self._buffer is None:
                raise NoiseError("A continuation chunk arrived with no chunked message open.")
            self._buffer += body
            self._check_cap()
            if marker == FRAME_MORE:
                return None
            finished, self._buffer = bytes(self._buffer), None
            return Frame(finished, self._buffer_is_json)
        raise NoiseError(f"Unknown v3 frame marker 0x{marker:02x}.")

    def _seal(self, marker: int, body: bytes) -> bytes:
        return self._send.encrypt_with_ad(b"", bytes((marker,)) + body)

    def _check_cap(self) -> None:
        if self._buffer is not None and len(self._buffer) > MAX_REASSEMBLY:
            self._buffer = None
            raise NoiseError("A chunked message exceeded the reassembly cap.")
