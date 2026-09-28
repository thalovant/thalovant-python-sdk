"""The client side of the v3 negotiation, and the files it shares with hivemind-bus-client.

A device that upgrades from an SDK built on hivemind-bus-client keeps its
static key -- the one its hub pinned -- its own pins, and its PSK cache. The
store here reads and writes the same files the same way; the tests below
prove it against the library itself where it is installed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thalovant import ThalovantConnectionError, _noise
from thalovant._noise_runtime import (
    NoiseClientProtocol,
    NoiseChannel,
    hello_message,
    load_cached_psk,
    load_noise_key,
    noise_identity,
    prepare_noise_key,
    save_cached_psk,
    forget_cached_psk,
)

PASSWORD = "runtime-password"


class Responder:
    """The hub's half of the negotiation, on the SDK's own Noise."""

    def __init__(self, *, node_id="runtime-hub", patterns=("KKpsk0", "XXpsk2"), password=PASSWORD):
        self.node_id = node_id
        self.static = _noise.generate_private_key()
        self.patterns = list(patterns)
        self.password = password
        self.pinned: bytes | None = None
        self.chosen: list[str] = []

    def opening(self):
        self.hello = {"node_id": self.node_id, "pubkey": "", "peer": "p"}
        patterns = self.patterns if self.pinned is not None else ["XXpsk2"]
        self.offer = {"max_protocol_version": 3, "noise": {"patterns": patterns, "suites": list(_noise.SUITES)}}
        return [json.dumps({"msg_type": "hello", "payload": self.hello}),
                json.dumps({"msg_type": "shake", "payload": self.offer})]

    def answer(self, frame):
        params = json.loads(frame)["payload"]["noise"]
        if "pattern" in params:
            self.chosen.append(params["pattern"])
            name = _noise.protocol_name(params["pattern"], params["suite"])
            self.handshake = _noise.NoiseHandshake(
                params["pattern"], params["suite"], _noise.derive_psk(self.password, self.node_id),
                _noise.build_prologue(self.hello, self.offer, name), self.static,
                remote_static=self.pinned if params["pattern"] == "KKpsk0" else None, initiator=False,
            )
            self.handshake.read_message(bytes.fromhex(params["msg"]))
            reply = self.handshake.write_message(b"{}")
            return json.dumps({"msg_type": "shake", "payload": {"noise": {"msg": reply.hex()}}})
        self.handshake.read_message(bytes.fromhex(params["msg"]))
        return None

    def session(self):
        session = self.handshake.into_session()
        self.pinned = session.remote_static
        return session


def negotiate(protocol: NoiseClientProtocol, hub: Responder):
    """Run one negotiation to the end; return the hub's session."""
    for frame in hub.opening():
        step = protocol.receive(frame)
        if step.need_psk:
            step = protocol.provide_psk(protocol.derive_psk())
        for out in step.send:
            if isinstance(out, str):
                answer = hub.answer(out)
                if answer is not None:
                    follow = protocol.receive(answer)
                    for final in follow.send:
                        if isinstance(final, str):
                            hub.answer(final)
                    return hub.session(), follow
    raise AssertionError("the negotiation did not finish")


def _protocol(tmp_path, pin_id="wss://hub:443"):
    return NoiseClientProtocol(
        store=noise_identity(str(tmp_path)), pin_id=pin_id,
        hello=hello_message("session-1", "site"), password=PASSWORD, access_key="key-1",
    )


def test_xx_then_kk_with_the_pin_and_the_cached_psk(tmp_path):
    hub = Responder()
    protocol = _protocol(tmp_path)
    session, step = negotiate(protocol, hub)
    assert protocol.ready and hub.chosen == ["XXpsk2"]
    # The encrypted HELLO is the first transport message.
    hello = json.loads(session.decrypt_frame(step.send[-1]).payload)
    assert hello["msg_type"] == "hello" and hello["payload"]["session"]["session_id"] == "session-1"
    assert protocol.store.get_pinned_noise_key("wss://hub:443") == _noise.public_key(hub.static).hex()
    key_path = prepare_noise_key(protocol.store)
    assert load_cached_psk(key_path, hub.node_id, "key-1") == _noise.derive_psk(PASSWORD, hub.node_id)

    protocol.reset()
    # Second time round the offer names KK, the pin allows it, and the PSK
    # comes from the cache: no need_psk step.
    frames = hub.opening()
    protocol.receive(frames[0])
    step = protocol.receive(frames[1])
    assert step.need_psk is None and step.send
    hub.answer(step.send[0])
    hub.session()
    assert hub.chosen == ["XXpsk2", "KKpsk0"]


def test_a_failed_kk_retries_once_as_xx_and_forgets_the_psk(tmp_path):
    hub = Responder()
    protocol = _protocol(tmp_path)
    negotiate(protocol, hub)
    protocol.reset()
    # The hub's password changed: KK fails at message two.
    hub.password = "rotated"
    frames = hub.opening()
    protocol.receive(frames[0])
    step = protocol.receive(frames[1])
    with pytest.raises(Exception):
        hub.answer(step.send[0])
    # The client learns nothing from a hub that closed; drive its side of a
    # failed message two instead: a response it cannot authenticate.
    bogus = json.dumps({"msg_type": "shake", "payload": {"noise": {"msg": "00" * 96}}})
    with pytest.raises(ThalovantConnectionError, match="authentication failed"):
        protocol.receive(bogus)
    key_path = prepare_noise_key(protocol.store)
    assert load_cached_psk(key_path, hub.node_id, "key-1") is None
    assert protocol.store.get_pinned_noise_key("wss://hub:443"), "a failure never drops the pin"
    protocol.reset()
    hub.password = PASSWORD
    negotiate(protocol, hub)
    assert hub.chosen[-1] == "XXpsk2"


def test_a_changed_hub_key_is_refused(tmp_path):
    hub = Responder(patterns=("XXpsk2",))
    protocol = _protocol(tmp_path)
    negotiate(protocol, hub)
    protocol.reset()
    hub.static = _noise.generate_private_key()
    with pytest.raises(ThalovantConnectionError, match="key changed"):
        negotiate(protocol, hub)


@pytest.mark.parametrize(("frames", "match"), [
    (["not json"], "Malformed"),
    ([json.dumps({"msg_type": "hello", "payload": {}})], "node_id"),
    ([json.dumps({"msg_type": "bus", "payload": {}})], "before Noise"),
    ([json.dumps({"msg_type": "hello", "payload": {"node_id": "n"}}),
      json.dumps({"msg_type": "shake", "payload": {"preshared_key": True}})], "did not offer"),
    ([json.dumps({"msg_type": "hello", "payload": {"node_id": "n"}}),
      json.dumps({"msg_type": "shake", "payload": {"noise": {"patterns": ["NN"], "suites": []}}})], "No supported"),
    ([json.dumps({"msg_type": "shake", "payload": {"noise": {"patterns": ["XXpsk2"], "suites": []}}})], "Out-of-order"),
])
def test_a_broken_negotiation_is_refused(tmp_path, frames, match):
    protocol = _protocol(tmp_path)
    with pytest.raises(ThalovantConnectionError, match=match):
        for frame in frames:
            protocol.receive(frame)
    assert protocol.failed
    with pytest.raises(ThalovantConnectionError, match="reconnect required"):
        protocol.receive(frames[0])


def test_binary_before_authentication_and_text_after_are_refused(tmp_path):
    protocol = _protocol(tmp_path)
    with pytest.raises(ThalovantConnectionError, match="before Noise"):
        protocol.receive(b"\x00\x01")
    protocol = _protocol(tmp_path)
    negotiate(protocol, Responder())
    with pytest.raises(ThalovantConnectionError, match="Plaintext"):
        protocol.receive('{"msg_type": "bus"}')
    with pytest.raises(ThalovantConnectionError, match="not established"):
        protocol.seal({"msg_type": "bus"})


def test_the_mqtt_channel_reads_its_negotiation_as_bytes(tmp_path):
    hub = Responder()
    written = []

    class Identity:
        password = PASSWORD
        access_key = "key-1"

    channel = NoiseChannel(Identity(), state_dir=str(tmp_path), pin_id="mqtt-hub",
                           hello=hello_message("s", "site"), write=written.append)
    for frame in hub.opening():
        channel.receive(frame.encode())
    answer = hub.answer(written[0])
    channel.receive(answer.encode())
    hub.answer(written[1])  # XX message three
    session = hub.session()
    assert channel.ready
    assert json.loads(session.decrypt_frame(written[-1]).payload)["msg_type"] == "hello"
    channel.send({"msg_type": "bus", "payload": {"type": "x", "data": {}, "context": {}}})
    assert json.loads(session.decrypt_frame(written[-1]).payload)["payload"]["type"] == "x"


# -- the files, against hivemind-bus-client ---------------------------------------


def test_hivemind_bus_client_reads_what_this_store_writes(tmp_path):
    identity_module = pytest.importorskip("hivemind_bus_client.identity")
    pytest.importorskip("json_database")
    from json_database import JsonStorage

    store = noise_identity(str(tmp_path))
    store.pin_noise_key("wss://hub:443", "ab" * 32)
    key_path = prepare_noise_key(store)
    theirs = identity_module.NodeIdentity(identity_file=JsonStorage(store.IDENTITY_FILE.path))
    assert theirs.get_pinned_noise_key("wss://hub:443") == "ab" * 32
    assert Path(theirs.noise_key) == Path(key_path)


def test_this_store_reads_what_hivemind_bus_client_wrote(tmp_path):
    identity_module = pytest.importorskip("hivemind_bus_client.identity")
    noise_module = pytest.importorskip("poorman_handshake.noise")
    from json_database import JsonStorage

    path = tmp_path / "_identity.json"
    theirs = identity_module.NodeIdentity(identity_file=JsonStorage(str(path)))
    theirs.pin_noise_key("wss://hub:443", "cd" * 32)
    key_path = theirs.noise_key
    handshake = noise_module.NoiseHandShake(initiator=True, path=key_path, psk=bytes(32))
    ours = noise_identity(str(tmp_path))
    assert ours.get_pinned_noise_key("wss://hub:443") == "cd" * 32
    assert Path(prepare_noise_key(ours)) == Path(key_path)
    assert _noise.public_key(load_noise_key(ours)).hex() == handshake.pubkey


def test_the_psk_cache_is_one_file_for_both(tmp_path):
    hivemind_noise = pytest.importorskip("hivemind_bus_client.noise")
    key_path = str(tmp_path / "unnamed-node_noise.key")
    psk = _noise.derive_psk("pw", "node")
    save_cached_psk(key_path, "node", psk, "access")
    assert hivemind_noise.load_cached_psk(key_path, "node", "access") == psk
    other = bytes(range(32))
    hivemind_noise.save_cached_psk(key_path, "hub-2", other, "access")
    assert load_cached_psk(key_path, "hub-2", "access") == other
    forget_cached_psk(key_path, "node", "access")
    assert hivemind_noise.load_cached_psk(key_path, "node", "access") is None
