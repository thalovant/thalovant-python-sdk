"""The HiveMind envelope and the WIRE-1 binary frame, against the reference library.

The frames in ``binary-frames.json`` were written by hivemind-bus-client's own
encoder; every SDK that decodes these bits itself tests against the same file.
The envelope checks round-trip through hivemind-bus-client where it is
installed, because a hub parses what this SDK sends with that library.
"""

from __future__ import annotations

import base64
import json
import zlib
from pathlib import Path

import pytest

from thalovant._wire import (
    BINARY_KINDS,
    BusMessage,
    HiveMessage,
    binary_kind_name,
    decode_binary_frame,
    encode_binary_frame,
    hive_message_from_json,
)

VECTORS = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts" / "conformance" / "binary-frames.json")
    .read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
def test_every_reference_frame_decodes(case):
    message = decode_binary_frame(base64.b64decode(case["frame"]))
    assert message.msg_type == "bin"
    assert message.payload == base64.b64decode(case["expected_payload"])
    assert message.metadata == case["expected_metadata"]
    assert binary_kind_name(message.bin_type) == case["expected_kind"]


def test_a_binarized_bus_frame_is_json_not_bytes():
    message = decode_binary_frame(base64.b64decode(VECTORS["bus_frame"]))
    assert message.msg_type == "bus"
    assert isinstance(message.payload, BusMessage)
    assert message.payload.msg_type == "speak"
    assert message.payload.data == {"utterance": "Pfffft."}


def test_the_payload_names_are_the_wire_s_own():
    assert BINARY_KINDS == {
        1: "raw_audio", 2: "numpy_image", 3: "file", 4: "stt_transcribe", 5: "stt_handle", 6: "tts_audio",
    }
    assert binary_kind_name(0) == "binary:0" and binary_kind_name(99) == "binary:99"


def test_a_truncated_or_versioned_frame_is_refused():
    frame = base64.b64decode(VECTORS["cases"][0]["frame"])
    with pytest.raises(ValueError):
        decode_binary_frame(frame[:3])
    # Version 2: a marker bit, then a version byte this decoder does not know.
    with pytest.raises(ValueError, match="version"):
        decode_binary_frame(bytes((0b11000000, 0b10000000)) + frame)
    with pytest.raises(ValueError):
        decode_binary_frame(b"")


def test_metadata_must_be_an_object():
    meta = b"[1]"
    with pytest.raises(ValueError, match="metadata"):
        decode_binary_frame(bytes((0x80 | (12 << 1), len(meta))) + meta + b"\x00")


def test_compressed_json_frames_inflate():
    body = zlib.compress(json.dumps({"type": "speak", "data": {}, "context": {}}).encode())
    meta = zlib.compress(b"{}")
    frame = bytes((0x80 | (1 << 1) | 1, len(meta))) + meta + body
    message = decode_binary_frame(frame)
    assert message.msg_type == "bus" and message.payload.msg_type == "speak"


def test_encoded_json_frames_decode_back():
    original = HiveMessage("bus", BusMessage("speak", {"utterance": "hi"}, {"lang": "en"}), metadata={"a": 1})
    decoded = decode_binary_frame(encode_binary_frame(original))
    assert decoded.payload == original.payload
    assert decoded.metadata == {"a": 1}
    with pytest.raises(ValueError, match="255"):
        encode_binary_frame(HiveMessage("bus", {}, metadata={"x": "y" * 300}))


def test_envelopes_parse_with_nested_payloads():
    raw = {
        "msg_type": "propagate",
        "payload": {"msg_type": "bus", "payload": {"type": "ping", "data": {"n": 1}, "context": {}}},
        "metadata": {"k": "v"}, "route": [{"source": "a", "targets": ["b"]}],
        "node": "n", "target_site_id": "s", "target_pubkey": None, "source_peer": "p",
    }
    message = hive_message_from_json(raw)
    assert isinstance(message.payload, HiveMessage)
    assert message.payload.payload == BusMessage("ping", {"n": 1}, {})
    assert message.as_dict["payload"]["payload"]["type"] == "ping"
    assert HiveMessage.deserialize(message.serialize()) == message
    assert BusMessage.deserialize(message.payload.payload.serialize()) == message.payload.payload
    with pytest.raises(ValueError):
        hive_message_from_json("[]")
    with pytest.raises(ValueError, match="bytes"):
        HiveMessage("bin", b"x").as_dict  # noqa: B018 - the property raises


def test_what_this_sdk_sends_is_what_the_hub_s_library_reads():
    hivemind = pytest.importorskip("hivemind_bus_client.message")
    inner = HiveMessage("bus", BusMessage("thalovant.ping", {"n": 1}, {"session": {"session_id": "s"}}))
    for kind in ("propagate", "escalate", "broadcast"):
        ours = HiveMessage(kind, inner)
        theirs = hivemind.HiveMessage.deserialize(ours.serialize())
        assert str(theirs.msg_type) == kind
        assert theirs.payload.payload.msg_type == "thalovant.ping"
        assert hive_message_from_json(theirs.serialize()) == ours


def test_frames_this_decoder_reads_match_the_reference_decoder():
    serialization = pytest.importorskip("hivemind_bus_client.serialization")
    for case in VECTORS["cases"]:
        frame = base64.b64decode(case["frame"])
        reference = serialization.decode_bitstring(frame)
        ours = decode_binary_frame(frame)
        assert ours.payload == reference.payload
        assert ours.metadata == reference.metadata
        assert ours.bin_type == int(reference.bin_type)
    frame = encode_binary_frame(HiveMessage("bus", BusMessage("speak", {"utterance": "x"}, {})))
    reference = serialization.decode_bitstring(frame)
    assert reference.payload.msg_type == "speak"
