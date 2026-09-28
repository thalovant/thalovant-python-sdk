"""The Noise layer against the reference vectors and the reference library.

Ported with the prototype that became this module (thalovant/aiothalovant,
tests/test_noise.py); the vectors come from the Node and Go SDKs, which took
them from poorman-handshake, hivemind-bus-client and noiseprotocol.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from noise.connection import Keypair, NoiseConnection

from thalovant import _noise

VECTORS = json.loads(
    (Path(__file__).resolve().parent / "vectors" / "noise-vectors.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize(
    "case", VECTORS["psk"]["cases"], ids=lambda case: case["node_id"] or "empty"
)
def test_psk_matches_the_reference(case: dict[str, str]) -> None:
    assert _noise.derive_psk(case["password"], case["node_id"]).hex() == case["psk"]


def test_psk_is_salted_by_the_node_id() -> None:
    assert _noise.derive_psk("same", "hub-one") != _noise.derive_psk("same", "hub-two")


@pytest.mark.parametrize(
    "case",
    VECTORS["transport"]["cases"],
    ids=lambda case: f"{case['suite']}@{case['counter']}",
)
def test_transport_ciphertexts_match_the_reference(case: dict[str, Any]) -> None:
    little_endian = case["suite"] == _noise.SUITE_CHACHA
    assert _noise._nonce(case["counter"], little_endian=little_endian).hex() == case["nonce"]
    aead = _noise._Aead(case["suite"])
    key = bytes.fromhex(VECTORS["transport"]["key"])
    plaintext = VECTORS["transport"]["plaintext_utf8"].encode()
    sealed = aead.encrypt(key, case["counter"], b"", plaintext)
    assert sealed.hex() == case["ciphertext"]
    assert aead.decrypt(key, case["counter"], b"", sealed) == plaintext


@pytest.mark.parametrize("case", VECTORS["canonical_json"], ids=lambda case: case["output"][:24])
def test_canonical_json_matches_the_reference(case: dict[str, Any]) -> None:
    assert _noise.canonical_json(case["input"]).decode("utf-8") == case["output"]


def test_prologue_matches_the_reference() -> None:
    case = VECTORS["prologue"]
    name = _noise.protocol_name(case["pattern"], case["suite"])
    assert name == case["protocol_name"]
    assert _noise.build_prologue(case["hello"], case["handshake"], name).hex() == case["bytes"]


@pytest.mark.parametrize("case", VECTORS["selection"], ids=str)
def test_selection_matches_the_contract(case: dict[str, Any]) -> None:
    chosen = _noise.select_options(case["patterns"], case["suites"], pinned=case["pinned"])
    assert chosen == (tuple(case["expect"]) if case["expect"] else None)


def _reference(
    pattern: str,
    suite: str,
    psk: bytes,
    prologue: bytes,
    static: bytes,
    remote: bytes | None,
    *,
    initiator: bool,
) -> Any:
    """``noiseprotocol``, the library the hub's poorman-handshake wraps."""
    peer = NoiseConnection.from_name(_noise.protocol_name(pattern, suite).encode())
    peer.set_keypair_from_private_bytes(Keypair.STATIC, static)
    if remote is not None:
        peer.set_keypair_from_public_bytes(Keypair.REMOTE_STATIC, remote)
    peer.set_psks(psk)
    peer.set_prologue(prologue)
    if initiator:
        peer.set_as_initiator()
    else:
        peer.set_as_responder()
    peer.start_handshake()
    return peer


PAIRS = [
    (pattern, suite)
    for pattern in (_noise.PATTERN_XX, _noise.PATTERN_KK)
    for suite in _noise.SUITES
]


@pytest.mark.parametrize(("pattern", "suite"), PAIRS)
def test_our_initiator_against_the_reference_responder(pattern: str, suite: str) -> None:
    psk, prologue = os.urandom(32), b"hello+offer+name"
    ours, theirs = _noise.generate_private_key(), _noise.generate_private_key()
    kk = pattern == _noise.PATTERN_KK
    hub = _reference(
        pattern,
        suite,
        psk,
        prologue,
        theirs,
        _noise.public_key(ours) if kk else None,
        initiator=False,
    )
    client = _noise.NoiseHandshake(
        pattern, suite, psk, prologue, ours, remote_static=_noise.public_key(theirs) if kk else None
    )

    first = _noise.canonical_json({"binarize": False, "encodings": []})
    assert bytes(hub.read_message(client.write_message(first))) == first
    assert client.read_message(bytes(hub.write_message(b'{"encoding":"JSON-HEX"}'))) == (
        b'{"encoding":"JSON-HEX"}'
    )
    if not client.finished:
        hub.read_message(client.write_message())
    assert hub.handshake_finished
    session = client.into_session()
    assert session.remote_static == _noise.public_key(theirs)

    for n in range(4):  # past counter zero, where the two nonce layouts differ
        message = json.dumps({"n": n}).encode()
        (frame,) = session.encrypt_message(message)
        assert bytes(hub.decrypt(frame)) == b"\x00" + message
        answer = session.decrypt_frame(bytes(hub.encrypt(b"\x00" + message)))
        assert answer == _noise.Frame(message, True)


@pytest.mark.parametrize(("pattern", "suite"), PAIRS)
def test_our_responder_against_the_reference_initiator(pattern: str, suite: str) -> None:
    """The fake hub's responder is a genuine peer, not one that agrees with anything."""
    psk, prologue = os.urandom(32), b"prologue"
    ours, theirs = _noise.generate_private_key(), _noise.generate_private_key()
    kk = pattern == _noise.PATTERN_KK
    client = _reference(
        pattern,
        suite,
        psk,
        prologue,
        theirs,
        _noise.public_key(ours) if kk else None,
        initiator=True,
    )
    hub = _noise.NoiseHandshake(
        pattern,
        suite,
        psk,
        prologue,
        ours,
        remote_static=_noise.public_key(theirs) if kk else None,
        initiator=False,
    )
    assert hub.read_message(bytes(client.write_message(b"one"))) == b"one"
    assert bytes(client.read_message(hub.write_message(b"two"))) == b"two"
    if not hub.finished:
        assert hub.read_message(bytes(client.write_message(b""))) == b""
    session = hub.into_session()
    assert session.remote_static == _noise.public_key(theirs)
    (frame,) = session.encrypt_message(b"{}")
    assert bytes(client.decrypt(frame)) == b"\x00{}"


def test_a_wrong_psk_fails_the_handshake() -> None:
    ours, theirs = _noise.generate_private_key(), _noise.generate_private_key()
    hub = _reference(
        _noise.PATTERN_XX, _noise.SUITE_CHACHA, os.urandom(32), b"p", theirs, None, initiator=False
    )
    client = _noise.NoiseHandshake(
        _noise.PATTERN_XX, _noise.SUITE_CHACHA, os.urandom(32), b"p", ours
    )
    hub.read_message(client.write_message(b""))
    with pytest.raises(_noise.NoiseError, match="authentication"):
        client.read_message(bytes(hub.write_message(b"")))


def test_a_different_prologue_fails_the_handshake() -> None:
    psk = os.urandom(32)
    ours, theirs = _noise.generate_private_key(), _noise.generate_private_key()
    hub = _noise.NoiseHandshake(
        _noise.PATTERN_XX, _noise.SUITE_CHACHA, psk, b"offer A", theirs, initiator=False
    )
    client = _noise.NoiseHandshake(_noise.PATTERN_XX, _noise.SUITE_CHACHA, psk, b"offer B", ours)
    with pytest.raises(_noise.NoiseError):
        hub.read_message(client.write_message(b"payload"))


def test_a_kk_peer_with_another_key_is_refused() -> None:
    psk = os.urandom(32)
    ours, theirs, impostor = (_noise.generate_private_key() for _ in range(3))
    hub = _noise.NoiseHandshake(
        _noise.PATTERN_KK,
        _noise.SUITE_CHACHA,
        psk,
        b"p",
        impostor,
        remote_static=_noise.public_key(ours),
        initiator=False,
    )
    client = _noise.NoiseHandshake(
        _noise.PATTERN_KK,
        _noise.SUITE_CHACHA,
        psk,
        b"p",
        ours,
        remote_static=_noise.public_key(theirs),
    )
    with pytest.raises(_noise.NoiseError):
        hub.read_message(client.write_message(b""))


def _pair(suite: str = _noise.SUITE_CHACHA) -> tuple[_noise.NoiseSession, _noise.NoiseSession]:
    psk = os.urandom(32)
    ours, theirs = _noise.generate_private_key(), _noise.generate_private_key()
    client = _noise.NoiseHandshake(_noise.PATTERN_XX, suite, psk, b"p", ours)
    hub = _noise.NoiseHandshake(_noise.PATTERN_XX, suite, psk, b"p", theirs, initiator=False)
    hub.read_message(client.write_message())
    client.read_message(hub.write_message())
    hub.read_message(client.write_message())
    return client.into_session(), hub.into_session()


def test_an_oversize_message_is_chunked_and_reassembled() -> None:
    client, hub = _pair()
    original = b"x" * (_noise.CHUNK_SIZE * 2 + 1024)
    frames = client.encrypt_message(original)
    assert len(frames) == 3
    assert hub.decrypt_frame(frames[0]) is None
    assert hub.decrypt_frame(frames[1]) is None
    assert hub.decrypt_frame(frames[2]) == _noise.Frame(original, True)


def test_binary_frames_keep_their_marker() -> None:
    client, hub = _pair()
    (frame,) = client.encrypt_message(b"\x0c\xff", is_json=False)
    assert hub.decrypt_frame(frame) == _noise.Frame(b"\x0c\xff", False)
    big = client.encrypt_message(b"b" * (_noise.CHUNK_SIZE + 1), is_json=False)
    assert hub.decrypt_frame(big[0]) is None
    assert hub.decrypt_frame(big[1]) == _noise.Frame(b"b" * (_noise.CHUNK_SIZE + 1), False)


def test_tampered_and_replayed_frames_are_refused() -> None:
    client, hub = _pair()
    first, second = client.encrypt_message(b"{}")[0], client.encrypt_message(b"{}")[0]
    tampered = bytearray(first)
    tampered[-1] ^= 1
    with pytest.raises(_noise.NoiseError):
        hub.decrypt_frame(bytes(tampered))
    client2, hub2 = _pair()
    frame = client2.encrypt_message(b"{}")[0]
    hub2.decrypt_frame(frame)
    with pytest.raises(_noise.NoiseError):
        hub2.decrypt_frame(frame)
    del second


@pytest.mark.parametrize(
    ("markers", "match"),
    [
        ([_noise.FRAME_FIRST_JSON, _noise.FRAME_JSON], "inside a chunked"),
        ([_noise.FRAME_FIRST_JSON, _noise.FRAME_FIRST_BINARY], "inside another"),
        ([_noise.FRAME_MORE], "no chunked message open"),
        ([_noise.FRAME_LAST], "no chunked message open"),
        ([0x09], "Unknown v3 frame marker"),
    ],
)
def test_malformed_chunk_sequences_are_refused(markers: list[int], match: str) -> None:
    client, hub = _pair()
    *leading, last = markers
    for marker in leading:
        hub.decrypt_frame(client._seal(marker, b"chunk"))
    with pytest.raises(_noise.NoiseError, match=match):
        hub.decrypt_frame(client._seal(last, b"chunk"))


def test_an_empty_or_oversize_frame_is_refused() -> None:
    client, hub = _pair()
    with pytest.raises(_noise.NoiseError, match="Empty"):
        hub.decrypt_frame(client._send.encrypt_with_ad(b"", b""))
    with pytest.raises(_noise.NoiseError, match="65535"):
        hub.decrypt_frame(b"\x00" * (_noise.MAX_MESSAGE + 1))


def test_reassembly_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_noise, "MAX_REASSEMBLY", 10)
    client, hub = _pair()
    assert hub.decrypt_frame(client._seal(_noise.FRAME_FIRST_JSON, b"12345")) is None
    with pytest.raises(_noise.NoiseError, match="cap"):
        hub.decrypt_frame(client._seal(_noise.FRAME_MORE, b"123456"))


def test_misuse_is_refused() -> None:
    key = _noise.generate_private_key()
    with pytest.raises(_noise.NoiseError, match="pattern"):
        _noise.NoiseHandshake("NNpsk0", _noise.SUITE_CHACHA, bytes(32), b"", key)
    with pytest.raises(_noise.NoiseError, match="suite"):
        _noise.NoiseHandshake(_noise.PATTERN_XX, "448_AESGCM_BLAKE2b", bytes(32), b"", key)
    with pytest.raises(_noise.NoiseError, match="32 bytes"):
        _noise.NoiseHandshake(_noise.PATTERN_XX, _noise.SUITE_CHACHA, b"short", b"", key)
    with pytest.raises(_noise.NoiseError, match="KKpsk0"):
        _noise.NoiseHandshake(_noise.PATTERN_KK, _noise.SUITE_CHACHA, bytes(32), b"", key)

    handshake = _noise.NoiseHandshake(_noise.PATTERN_XX, _noise.SUITE_CHACHA, bytes(32), b"", key)
    with pytest.raises(_noise.NoiseError, match="not finished"):
        handshake.into_session()
    with pytest.raises(_noise.NoiseError, match="Truncated"):
        _noise.NoiseHandshake(
            _noise.PATTERN_XX, _noise.SUITE_CHACHA, bytes(32), b"", key, initiator=False
        ).read_message(b"short")


def test_a_finished_handshake_has_nothing_left() -> None:
    psk = os.urandom(32)
    client = _noise.NoiseHandshake(
        _noise.PATTERN_XX, _noise.SUITE_CHACHA, psk, b"", _noise.generate_private_key()
    )
    hub = _noise.NoiseHandshake(
        _noise.PATTERN_XX,
        _noise.SUITE_CHACHA,
        psk,
        b"",
        _noise.generate_private_key(),
        initiator=False,
    )
    hub.read_message(client.write_message())
    client.read_message(hub.write_message())
    hub.read_message(client.write_message())
    with pytest.raises(_noise.NoiseError, match="left to write"):
        client.write_message()
    with pytest.raises(_noise.NoiseError, match="left to read"):
        client.read_message(b"")


def test_a_truncated_static_key_is_refused() -> None:
    psk = os.urandom(32)
    client = _noise.NoiseHandshake(
        _noise.PATTERN_XX, _noise.SUITE_CHACHA, psk, b"", _noise.generate_private_key()
    )
    hub = _noise.NoiseHandshake(
        _noise.PATTERN_XX,
        _noise.SUITE_CHACHA,
        psk,
        b"",
        _noise.generate_private_key(),
        initiator=False,
    )
    hub.read_message(client.write_message())
    response = hub.write_message()
    with pytest.raises(_noise.NoiseError, match="Truncated"):
        client.read_message(response[:40])


def test_a_low_order_point_is_refused() -> None:
    # An all-zero public key makes X25519 produce an all-zero secret.
    with pytest.raises(_noise.NoiseError, match="key agreement"):
        _noise._dh(_noise.generate_private_key(), bytes(32))


def test_a_token_without_its_keys_is_refused() -> None:
    handshake = _noise.NoiseHandshake(
        _noise.PATTERN_XX, _noise.SUITE_CHACHA, bytes(32), b"", _noise.generate_private_key()
    )
    with pytest.raises(_noise.NoiseError, match="without both keys"):
        handshake._dh("ee")


def test_the_nonce_space_is_bounded() -> None:
    cipher = _noise.CipherState(_noise._Aead(_noise.SUITE_CHACHA), bytes(32))
    cipher._counter = 2**64 - 1
    with pytest.raises(_noise.NoiseError, match="exhausted"):
        cipher.encrypt_with_ad(b"", b"x")
    with pytest.raises(_noise.NoiseError, match="exhausted"):
        cipher.decrypt_with_ad(b"", b"x" * 17)
