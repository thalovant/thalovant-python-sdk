"""Hexadecimal spelling does not change an authenticated Noise identity."""
import json
from pathlib import Path
import threading

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from thalovant import ThalovantConnectionError
from thalovant._noise_runtime import noise_identity
from thalovant.transport import HiveMindHTTPTransport
from test_noise_transports import http_peer, identity  # noqa: F401


@pytest.mark.parametrize("stored,submitted", [
    ("AB" * 32, "ab" * 32), ("ab" * 32, "AB" * 32), ("AB" * 32, "AB" * 32),
])
def test_equivalent_pin_setter_preserves_verified_file(tmp_path, monkeypatch, stored, submitted):
    store = noise_identity(str(tmp_path))
    store.pin_noise_key("hub", stored)
    path = Path(store.IDENTITY_FILE.path)
    before = path.read_bytes()

    def unexpected_write(_):
        pytest.fail("an already trusted key must not rewrite the identity file")

    monkeypatch.setattr(store, "_write_private", unexpected_write)
    store.pin_noise_key("hub", submitted)
    assert path.read_bytes() == before
    assert noise_identity(str(tmp_path)).get_pinned_noise_key("hub") == stored
    with pytest.raises(ThalovantConnectionError, match="key changed"):
        store.pin_noise_key("hub", "cd" * 32)
    assert path.read_bytes() == before


@pytest.mark.parametrize("changed_server", [False, True])
def test_https_uppercase_pin_authenticates_kk_and_rejects_changed_key(http_peer, tmp_path, changed_server):
    peer, endpoint = http_peer
    transport = HiveMindHTTPTransport(
        identity(endpoint), useragent="conformance", noise_state_dir=str(tmp_path / "client"),
        handshake_poll_interval=0.01,
    )
    try:
        transport.connect()
        path = Path(transport._client.channel.store.IDENTITY_FILE.path)
        pin_id = transport._client.channel.pin_id
        transport.disconnect()
        data = json.loads(path.read_text())
        pin = data["pinned_noise_keys"][pin_id]
        assert pin.upper() != pin, "fixture key must exercise hex letter case"
        data["pinned_noise_keys"][pin_id] = pin.upper()
        before = json.dumps(data, indent=2).encode()
        path.write_bytes(before)

        if changed_server:
            # Authenticate a different real key through XX to exercise the
            # final pin comparison, not an earlier KK transcript failure.
            key = X25519PrivateKey.generate().private_bytes(
                serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption(),
            )
            (tmp_path / "server.key").write_text(key.hex())
            peer.offer["noise"]["patterns"] = ["XXpsk2"]
            with pytest.raises(ThalovantConnectionError) as caught:
                transport.connect()
            assert isinstance(caught.value.__cause__, ThalovantConnectionError)
            assert "key changed" in str(caught.value.__cause__)
            assert not transport.healthcheck().ok
            assert peer.patterns == ["XXpsk2", "XXpsk2"]
        else:
            transport.connect()
            assert transport.healthcheck().ok
            received = threading.Event()
            transport.on_mycroft("speak", lambda _: received.set())
            transport.emit_event("ovos.intent.list", {}, {"request_id": "uppercase-pin"})
            assert received.wait(5), "authenticated KK session must exchange encrypted replies"
            assert peer.patterns == ["XXpsk2", "KKpsk0"]
        assert path.read_bytes() == before
    finally:
        transport.disconnect()
