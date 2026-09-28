"""A bus message from the hub is taken in once, the way a HiveMind node takes it.

hivemind-bus-client's slave protocol did this for the SDK until 0.9: strip any
verified-origin claim (nothing on this path verifies one), and read
``destination`` as ``source`` (HIVEMIND-BRIDGE-1 §3.1). The SDK's own transport
does it now, and keeps the context as it arrived so a reply can be routed
back the way the message came.
"""

from thalovant import ThalovantIdentity
from thalovant._hive import VERIFIED_SOURCE_PEER_KEY, AsyncHiveMindWSSTransport
from thalovant._wire import BusMessage, HiveMessage, hive_message_from_json
from thalovant.client import _reply_to
from thalovant.events import _event_from_message


def _transport():
    identity = ThalovantIdentity(
        access_key="synthetic", password="synthetic", site_id="conformance",
        default_master="wss://hub.local", default_port=443,
    )
    transport = AsyncHiveMindWSSTransport(identity, useragent="conformance")
    transport._carrier = object()  # a session is open: registrations are allowed
    return transport


def _frame(context):
    return hive_message_from_json({
        "msg_type": "bus",
        "payload": {"type": "conformance.event", "data": {"verb": "one-frame"}, "context": context},
    })


def test_bus_delivery_once_after_protocol_processing():
    transport = _transport()
    delivered = []
    transport.on_mycroft("conformance.event", delivered.append)
    for _ in range(2):
        transport._deliver(_frame({
            "source": "skills", "destination": "receiver", VERIFIED_SOURCE_PEER_KEY: "forged",
            "session": {"session_id": "hub-session"},
        }))
    # Equal payloads in distinct frames remain distinct deliveries.
    assert len(delivered) == 2
    for message in delivered:
        assert message.context["source"] == "receiver"
        assert "destination" not in message.context
        assert VERIFIED_SOURCE_PEER_KEY not in message.context
        assert message.context["session"] == {"session_id": "hub-session"}
        # What the hub sent is kept, for the reply.
        assert message.wire_context["destination"] == "receiver"
        assert message.wire_context["source"] == "skills"


def test_a_message_without_a_destination_keeps_its_source():
    transport = _transport()
    delivered = []
    transport.on_mycroft("conformance.event", delivered.append)
    transport._deliver(_frame({"source": "skills"}))
    assert delivered[0].context == {"source": "skills"}


def test_a_broadcast_aimed_at_this_site_is_taken_in_and_others_are_not():
    transport = _transport()
    delivered, frames = [], []
    transport.on_mycroft("conformance.event", delivered.append)
    transport.on_hive_message("broadcast", frames.append)

    def broadcast(site):
        inner = HiveMessage("bus", BusMessage("conformance.event", {}, {"destination": "receiver"}))
        return HiveMessage("broadcast", inner, target_site_id=site)

    transport._deliver(broadcast("conformance"))
    transport._deliver(broadcast("elsewhere"))
    assert len(delivered) == 1 and delivered[0].context["source"] == "receiver"
    # The mesh frame itself still reaches a hive subscriber, both times.
    assert len(frames) == 2


def test_a_reply_is_routed_from_the_context_as_it_arrived():
    transport = _transport()
    delivered = []
    transport.on_mycroft("thalovant.home.request", delivered.append)
    transport._deliver(hive_message_from_json({
        "msg_type": "bus",
        "payload": {
            "type": "thalovant.home.request",
            "data": {"request_id": "r1"},
            "context": {"source": "skill-home", "destination": ["ha-peer", "other"],
                        "session": {"session_id": "s1"}, "request_id": "r1"},
        },
    }))
    event = _event_from_message("thalovant.home.request", delivered[0])
    context = _reply_to(event, None)
    assert context["destination"] == "skill-home"
    assert context["source"] == "ha-peer"
    assert context["session"] == {"session_id": "s1"} and context["request_id"] == "r1"
    # A deep copy: the reply never edits the request it answers.
    context["session"]["session_id"] = "changed"
    assert delivered[0].wire_context["session"]["session_id"] == "s1"
