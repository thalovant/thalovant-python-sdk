"""The shared vectors, executed against the implementation they describe.

A capability's contract is only a contract if something runs it. These files
are what every other SDK is asked to satisfy, so they have to be true of the
reference first -- otherwise the gate spreads a fiction to eight repositories.

`contracts/conformance/*.json` is hashed into the parity reference, so a change
here is a change every consumer must accept before it can ship.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from thalovant.events import (
    HIVE_KINDS,
    ThalovantBinary,
    carry_conversation,
)

VECTORS = Path(__file__).resolve().parents[1] / "contracts" / "conformance"


def vectors(name: str) -> dict[str, Any]:
    return json.loads((VECTORS / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# conversation-vectors.json


def test_the_carry_matches_its_vectors():
    spec = vectors("conversation-vectors.json")
    for case in spec["cases"]:
        carried = carry_conversation(case["previous"], case["session"])
        assert carried == case["expected"], case["name"]


def test_the_carried_field_list_is_the_one_the_vectors_name():
    from thalovant.events import CONVERSATION_SESSION_FIELDS

    spec = vectors("conversation-vectors.json")
    assert sorted(CONVERSATION_SESSION_FIELDS) == sorted(spec["carried_fields"])
    # The other half of the promise: what must never travel. A remembered
    # `lang` would pin a bilingual conversation to whichever language it
    # opened in, which is the failure this list exists to prevent.
    for field in spec["never_carried"]:
        assert field not in CONVERSATION_SESSION_FIELDS


# --------------------------------------------------------------------------
# binary-vectors.json


def test_binary_frames_match_their_vectors():
    from thalovant.transport import _BINARY_KINDS, _unnamed_binary

    spec = vectors("binary-vectors.json")
    for case in spec["cases"]:
        wire = case["bin_type"]
        kind = _BINARY_KINDS.get(wire)
        if kind is None:
            class _Frame:
                bin_type = wire

            kind = _unnamed_binary(_Frame())
        frame = ThalovantBinary(kind=kind, data=b"", metadata=case["metadata"])
        expected = case["expected"]
        assert frame.kind == expected["kind"], case["name"]
        assert frame.utterance == expected["utterance"], case["name"]
        assert frame.lang == expected["lang"], case["name"]
        assert frame.file_name == expected["file_name"], case["name"]


def test_the_payload_numbers_are_the_ones_the_vectors_name():
    from thalovant.transport import _BINARY_KINDS

    spec = vectors("binary-vectors.json")
    assert {str(k): v for k, v in _BINARY_KINDS.items()} == spec["payload_kinds"]


# --------------------------------------------------------------------------
# mesh-vectors.json


def test_the_mesh_kinds_are_the_ones_the_vectors_name():
    spec = vectors("mesh-vectors.json")
    assert sorted(HIVE_KINDS) == sorted(spec["kinds"])


def test_subscription_matches_the_mesh_vectors():
    from thalovant.client import ThalovantClient
    from thalovant.identity import ThalovantIdentity

    from test_client import FakeTransport

    class _Transport(FakeTransport):
        def on_hive_message(self, msg_type, handler):
            pass

        def remove_hive_message(self, msg_type, handler):
            pass

    client = ThalovantClient(
        ThalovantIdentity(
            access_key="key", password="password", site_id="site",
            default_master="http://hub.local", default_port=5679,
        ),
        transport=_Transport(answer=None, handled=False),
    )
    for case in vectors("mesh-vectors.json")["cases"]:
        if case["expected"]["accepted"]:
            assert client.on_hive(case["kind"], lambda _f: None), case["name"]
        else:
            with pytest.raises(ValueError):
                client.on_hive(case["kind"], lambda _f: None)


def test_the_envelope_is_the_shape_the_vectors_describe():
    spec = vectors("mesh-vectors.json")
    envelope = spec["envelope"]
    # Nested, because a hub reads message.payload as a HiveMessage of its own
    # and re-stamps the route on it. A flat frame loses the route.
    assert envelope["payload"]["msg_type"] == "bus"
    assert set(envelope["payload"]["payload"]) == {"type", "data", "context"}
