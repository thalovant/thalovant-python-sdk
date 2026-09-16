"""The rest of the HiveMind protocol: the mesh, and binary frames.

A hub is a *hive*, not a star. Besides the BUS traffic of one conversation it
relays frames aimed down at every child, walked across the whole hive, sent up
to a parent, addressed node to node, and a mailbox peers use to find each
other through NAT. Our hub implements all thirteen HiveMind message types.
This SDK implemented five, and the transport dropped the rest off the end of
its dispatch with no branch and no log line.

`BINARY` is the other half. It is how a hub answers `speak:synth` -- it renders
the utterance and sends the audio back, so a client with no synthesiser of its
own can still speak -- and how it hands over a file. The library decodes those
frames and calls `bin_callbacks`; nothing was passing one, so every frame met
"Ignoring received binary TTS audio" and was discarded.
"""

from __future__ import annotations

from typing import Any

import pytest

from thalovant.client import ThalovantClient
from thalovant.events import (
    BINARY_FILE,
    BINARY_TTS_AUDIO,
    HIVE_BROADCAST,
    HIVE_ESCALATE,
    HIVE_INTERCOM,
    HIVE_KINDS,
    HIVE_PROPAGATE,
    HIVE_RENDEZVOUS,
    ThalovantBinary,
)
from thalovant.identity import ThalovantIdentity

from test_client import FakeTransport


def identity() -> ThalovantIdentity:
    return ThalovantIdentity(
        access_key="key", password="password", site_id="site",
        default_master="http://hub.local", default_port=5679,
    )


class MeshTransport(FakeTransport):
    """Records hive frames on the way out and replays them on the way in."""

    def __init__(self) -> None:
        super().__init__(answer=None, handled=False)
        self.frames: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        self.hive_subscriptions: dict[str, list[Any]] = {}
        self.binary_handlers: list[Any] = []

    def send_hive_frame(self, kind, event_type, data, context):
        self.frames.append((kind, event_type, data, context))
        return True

    def on_hive_message(self, msg_type, handler):
        self.hive_subscriptions.setdefault(msg_type, []).append(handler)

    def remove_hive_message(self, msg_type, handler):
        self.hive_subscriptions[msg_type] = [
            h for h in self.hive_subscriptions.get(msg_type, []) if h is not handler
        ]

    def on_binary(self, handler):
        self.binary_handlers.append(handler)

    def remove_binary(self, handler):
        self.binary_handlers = [h for h in self.binary_handlers if h is not handler]


def _client(transport: MeshTransport) -> ThalovantClient:
    return ThalovantClient(identity(), transport=transport, reply_settle_seconds=0)


# --------------------------------------------------------------------------
# sending


def test_each_mesh_direction_travels_under_its_own_kind():
    transport = MeshTransport()
    client = _client(transport)

    client.propagate("thalovant.ping", {"n": 1})
    client.escalate("thalovant.ping", {"n": 2})
    client.broadcast("thalovant.ping", {"n": 3})

    assert [frame[0] for frame in transport.frames] == [
        HIVE_PROPAGATE, HIVE_ESCALATE, HIVE_BROADCAST,
    ]
    assert [data for _kind, _event, data, _context in transport.frames] == [
        {"n": 1}, {"n": 2}, {"n": 3},
    ]


def test_a_hive_frame_needs_something_to_carry():
    client = _client(MeshTransport())
    for blank in ("", "   "):
        with pytest.raises(ValueError):
            client.propagate(blank)


def test_the_event_type_is_sent_trimmed():
    transport = MeshTransport()
    _client(transport).escalate("  thalovant.ping  ", {})
    assert transport.frames[0][1] == "thalovant.ping"


def test_identity_metadata_rides_along_as_it_does_on_a_bus_event():
    # A mesh frame is as much this client's traffic as an utterance is; a hub
    # that cannot tell who sent it cannot police who may.
    transport = MeshTransport()
    sdk_identity = ThalovantIdentity(
        access_key="key", password="password", site_id="site",
        default_master="http://hub.local", default_port=5679,
        metadata={"thalovant_owner_id": "owner-1"},
    )
    ThalovantClient(sdk_identity, transport=transport).propagate("thalovant.ping")
    assert transport.frames[0][3]["metadata"]["thalovant_owner_id"] == "owner-1"


# --------------------------------------------------------------------------
# receiving


@pytest.mark.parametrize("kind", HIVE_KINDS)
def test_every_mesh_kind_can_be_listened_to(kind: str):
    transport = MeshTransport()
    client = _client(transport)
    seen: list[Any] = []

    unsubscribe = client.on_hive(kind, seen.append)
    for handler in transport.hive_subscriptions[kind]:
        handler({"msg_type": kind})
    assert seen == [{"msg_type": kind}]

    unsubscribe()
    assert transport.hive_subscriptions[kind] == []


def test_the_client_s_own_traffic_is_not_a_hive_kind():
    # `query` and `cascade` are this client's request/response frames and
    # ask() owns them. Accepting them here would look like it worked and
    # quietly compete for the same replies.
    client = _client(MeshTransport())
    for own in ("query", "cascade", "bus"):
        assert own not in HIVE_KINDS
        with pytest.raises(ValueError):
            client.on_hive(own, lambda _frame: None)


def test_a_misspelled_kind_is_refused_rather_than_never_firing():
    client = _client(MeshTransport())
    with pytest.raises(ValueError, match="broadcasts"):
        client.on_hive("broadcasts", lambda _frame: None)


def test_intercom_and_rendezvous_are_reachable():
    # Neither has ever been reachable from this SDK, and both are how a hive
    # addresses one node and how peers find each other through NAT.
    assert HIVE_INTERCOM in HIVE_KINDS
    assert HIVE_RENDEZVOUS in HIVE_KINDS


# --------------------------------------------------------------------------
# binary frames


def test_binary_frames_reach_a_subscriber_and_stop_when_it_leaves():
    transport = MeshTransport()
    client = _client(transport)
    seen: list[ThalovantBinary] = []

    unsubscribe = client.on_binary(seen.append)
    frame = ThalovantBinary(BINARY_TTS_AUDIO, b"RIFF....", {"utterance": "Pfffft."})
    for handler in transport.binary_handlers:
        handler(frame)
    assert [f.utterance for f in seen] == ["Pfffft."]

    unsubscribe()
    assert transport.binary_handlers == []


def test_a_binary_frame_reads_its_own_metadata():
    frame = ThalovantBinary(
        BINARY_TTS_AUDIO, b"12345",
        {"utterance": "Il fait 22 degrés.", "lang": "fr-FR", "file_name": "a.wav"},
    )
    assert frame.utterance == "Il fait 22 degrés."
    assert frame.lang == "fr-FR"
    assert frame.file_name == "a.wav"
    assert len(frame) == 5


def test_missing_metadata_reads_as_absent_rather_than_as_the_string_none():
    frame = ThalovantBinary(BINARY_FILE, b"", {"file_name": ""})
    assert frame.utterance is None
    assert frame.lang is None
    # An empty name is no name. Rendering it as "" would put a blank filename
    # in front of somebody as though the hub had sent one.
    assert frame.file_name is None


def _delivery() -> Any:
    """The binary-subscriber plumbing on its own.

    `_ConnectionLifecycle` is where it lives, and exercising it there rather
    than through a whole transport keeps this file from constructing live
    objects it never connects -- the suite already has enough of those, and
    one of them is why these tests are cheap on purpose.
    """

    from thalovant.transport import _ConnectionLifecycle

    class _Delivery(_ConnectionLifecycle):
        def __init__(self) -> None:
            self._init_lifecycle()

    return _Delivery()


def test_one_raising_subscriber_does_not_cost_the_others_their_frame():
    # These arrive on the socket's read loop. A subscriber that throws must
    # not take the connection down with it, nor silence the next subscriber.
    transport = _delivery()
    delivered: list[ThalovantBinary] = []

    def angry(_frame: ThalovantBinary) -> None:
        raise RuntimeError("no")

    transport.on_binary(angry)
    transport.on_binary(delivered.append)
    transport._deliver_binary(BINARY_FILE, b"xyz", {"file_name": "n.bin"})

    assert [f.file_name for f in delivered] == ["n.bin"]


def test_a_subscriber_that_leaves_stops_receiving():
    # Held on the transport and not on the upstream client, which a reconnect
    # throws away: kept there they would go quiet after the first dropped
    # socket and nothing would say why.
    transport = _delivery()
    seen: list[ThalovantBinary] = []

    transport.on_binary(seen.append)
    transport._deliver_binary(BINARY_TTS_AUDIO, b"a", {})
    transport.remove_binary(seen.append)  # a different object: still subscribed
    transport._deliver_binary(BINARY_TTS_AUDIO, b"b", {})
    assert len(seen) == 2

    transport._binary_handlers.clear()
    transport._deliver_binary(BINARY_TTS_AUDIO, b"c", {})
    assert len(seen) == 2


# --------------------------------------------------------------------------
# the wire itself


def test_the_mesh_envelope_is_what_a_hub_unpacks():
    """A hub reads `message.payload` of a mesh frame as a HiveMessage of its own.

    `hivemind_core._unpack_message` calls `replace_route`, `update_source_peer`
    and `remove_target_peer` on it before forwarding. A flat frame carrying a
    bare bus message would not answer any of those, so the nesting is the
    contract, not a style.

    Verified against the hub's own library version (hivemind-bus-client
    1.1.1a1, in the pod) as well as the one resolved here -- the two differ,
    and the envelope has to parse on the hub's.
    """

    from hivemind_bus_client.message import HiveMessage, HiveMessageType
    from ovos_bus_client.message import Message

    for kind in (HIVE_PROPAGATE, HIVE_ESCALATE, HIVE_BROADCAST):
        inner = HiveMessage(HiveMessageType.BUS, Message("thalovant.ping", {"n": 1}, {}))
        restored = HiveMessage.deserialize(HiveMessage(kind, inner).serialize())

        assert str(restored.msg_type) == kind
        payload = restored.payload
        assert isinstance(payload, HiveMessage)
        payload.replace_route(restored.route)
        payload.update_source_peer("node-x")
        payload.remove_target_peer("peer-y")
        assert payload.payload.msg_type == "thalovant.ping"


def test_the_binary_payload_numbers_are_the_wire_s_own():
    """Our names are ours; the numbers are the protocol's.

    A hub answering `speak:synth` sends `TTS_AUDIO`, which is 6 on the wire.
    Mapping it to the wrong name would deliver rendered speech as something
    else and only show up as silence.
    """

    from hivemind_bus_client.message import HiveMindBinaryPayloadType as Wire

    from thalovant.transport import _BINARY_KINDS

    assert _BINARY_KINDS[Wire.TTS_AUDIO.value] == BINARY_TTS_AUDIO
    assert _BINARY_KINDS[Wire.FILE.value] == BINARY_FILE
    # Every type the wire defines has a name here except UNDEFINED, which is
    # the absence of one and falls through to `binary:<n>`.
    named = {t.value for t in Wire} - {Wire.UNDEFINED.value}
    assert named <= set(_BINARY_KINDS)


def test_an_unnamed_binary_type_still_arrives():
    from thalovant.transport import _unnamed_binary

    class _Frame:
        bin_type = 99

    # Dropping it is how the TTS audio was lost in the first place.
    assert _unnamed_binary(_Frame()) == "binary:99"


def test_the_mesh_kinds_this_sdk_dispatches_match_the_ones_it_offers():
    from thalovant.transport import _HIVE_DISPATCHED

    # A kind offered by on_hive() but not dispatched would subscribe happily
    # and never fire.
    assert set(HIVE_KINDS) <= _HIVE_DISPATCHED
