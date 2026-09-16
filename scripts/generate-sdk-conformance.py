#!/usr/bin/env python3
"""Regenerate reviewable public behavior vectors; never update acceptance records.

Run in an installed Python SDK environment and copy the resulting JSON files to
managed SDK test fixtures. --check executes the current dependency graph against
the committed expectations, including scheduled fresh dependency resolutions.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import unicodedata

from thalovant import Intent, Inventory, InventoryCache, Skill, listing
from thalovant.events import ThalovantEvent
from thalovant.models import ThalovantReply


def reply_claim_vectors():
    cases=[
     ('legacy',True,False,[{}]),('empty',True,False,[]),('unhandled',False,False,[{}]),('failed',True,True,[{'pipeline_id':'intent'}]),
     ('intent',True,False,[{'pipeline_id':'ovos-padatious-pipeline-plugin','skill_id':'weather'}]),
     ('fallback',True,False,[{'pipeline_id':'ovos-fallback-pipeline-plugin','skill_id':'fallback.skill'}]),
     ('fallback-then-converse',True,False,[{'pipeline_id':'ovos-fallback-pipeline-plugin','skill_id':'first'},{'pipeline_id':'ovos-converse-pipeline-plugin','skill_id':'second'}]),
     ('ordered-duplicates',True,False,[{'pipeline_id':'z-stage','skill_id':'z-skill'},{'pipeline_id':'a-stage','skill_id':'a-skill'},{'pipeline_id':'z-stage','skill_id':'z-skill'}]),
     ('empty-stamps',True,False,[{'pipeline_id':'','skill_id':''},{'pipeline_id':None,'skill_id':None}]),
     ('malformed-stamps',True,False,[{'pipeline_id':True,'skill_id':123},{'pipeline_id':['fallback'],'skill_id':{'id':'x'}}]),
     ('malformed-plus-fallback',True,False,[{'pipeline_id':123},{'pipeline_id':'fallback','skill_id':'real'}]),
     ('case-sensitive-stage',True,False,[{'pipeline_id':'FALLBACK','skill_id':'Skill'},{'pipeline_id':'fallback','skill_id':'skill'}]),
     ('substring',True,False,[{'pipeline_id':'prefix-fallback-suffix'}]),
     ('whitespace-preserved',True,False,[{'pipeline_id':' stage ','skill_id':' skill '}]),
     ('unicode',True,False,[{'pipeline_id':'段階','skill_id':'技能'},{'pipeline_id':'段階','skill_id':'技能'}]),
     ('failed-without-stamps',True,True,[{}]),
    ]
    rows=[]
    for name,handled,failed,contexts in cases:
     events=tuple(ThalovantEvent('speak',{'utterance':'reply'},context,None) for context in contexts)
     reply=ThalovantReply(text='reply',handled=handled,events=events,failure_event=ThalovantEvent('failure',{}, {},None) if failed else None)
     rows.append({'name':name,'handled':handled,'failed':failed,'contexts':contexts,'expected':{'pipeline_ids':list(reply.pipeline_ids),'skill_ids':list(reply.skill_ids),'claimed':reply.claimed}})
    return {"schema_version": 1, "contract": "String IDs, first-seen unique order; case-sensitive fallback substring; legacy success remains claimed; advisory, not authentication.", "cases": rows}


def vectors():
    marks = [chr(i) for i in range(0x110000) if "QUESTION MARK" in unicodedata.name(chr(i), "")]
    languages = [None, "en", "fr-CA", "ar", "xq", "ja", ""]
    texts = ["", "  ", "go home", "is it ready", "weather in", "ai je besoin d une veste", "y a t il de la neige", "hello!", "?hello"]
    texts += ["hello" + mark + "  " for mark in marks]
    texts += ["hello" + chr(i) for i in [0x10441, 0x11144, 0x1e960, 0x1fbc5, 0xe0040]]
    # What the expectations actually depend on, and nothing else. This SDK's own
    # version was in here and did two kinds of harm: read from the installed
    # distribution rather than this tree, it stamped whatever happened to be in
    # the virtualenv -- and being stamped at all, it changed the vectors' digest
    # on every release, sending nine repositories to re-vendor a file whose
    # expectations had not moved.
    source = {"thalovant-languages": importlib.metadata.version("thalovant-languages"),
              "unicode_version": unicodedata.unidata_version}
    question = {"source": source, "cases": [{"text": text, "lang": language, "expected": listing.asks(text, language)} for language in languages for text in texts]}
    inventory = Inventory("hub", "Kitchen", "hub", "2026-09-13T00:00:00Z", (
        Skill("weather", "Weather", ("en-us",), (
            Intent("weather.now", "weather.now", "weather", "padatious", {"fr-fr": ("météo",), "en-us": ("weather in", "what is the weather")}),
        )), Skill("unknown", "Unknown", (), ()),
    ))
    # Sorting deliberately destroys JSON object order. The explicit language
    # list must preserve omitted-language behavior across every decoder.
    raw = json.loads(json.dumps(inventory.as_dict(), sort_keys=True))
    queries = [(None, 0), ("en-gb", 0), ("en-gb", 1), ("fr-ca", 2), ("de", 3)]
    inventory_vectors = {"source": source, "cache_key": InventoryCache.key("hub", None), "inventory": raw, "examples": [
        {"language": lang, "limit": limit, "expected": list(inventory.intents[0].examples(lang, limit))}
        for lang, limit in queries
    ], "speaks": [{"language": lang, "expected": inventory.skills[0].speaks(lang)} for lang in ["en-gb", "fr", "de"]]}
    return {"question-vectors.json": question, "inventory-vectors.json": inventory_vectors,
            "reply-claim-vectors.json": reply_claim_vectors(), "binary-frames.json": binary_frames()}


def binary_frames():
    """Real WIRE-1 BINARY frames, produced by the reference encoder.

    Every SDK that decodes these bits itself needs frames it did not build to
    test against; one written by hand tests a reading of the specification
    rather than the wire a hub puts out. So these come from
    ``hivemind-bus-client``'s own ``get_bitstring``, base64'd, and every SDK
    vendors the same file.
    """

    import base64

    from hivemind_bus_client.message import HiveMessageType, HiveMindBinaryPayloadType as Kind
    from hivemind_bus_client.serialization import get_bitstring

    clip = bytes(range(256))
    vectors = json.loads((Path(__file__).resolve().parents[1]
                          / "contracts/conformance/binary-vectors.json").read_text())
    cases = []
    for row in vectors["cases"]:
        frame = get_bitstring(hive_type=HiveMessageType.BINARY, payload=clip,
                              compressed=False, hivemeta=row["metadata"],
                              binary_type=Kind(row["bin_type"]), versioned=True)
        cases.append({"name": row["name"],
                      "frame": base64.b64encode(frame.bytes).decode(),
                      "expected_kind": row["expected"]["kind"],
                      "expected_metadata": row["metadata"],
                      "expected_payload": base64.b64encode(clip).decode()})
    for wire, name in sorted(vectors["payload_kinds"].items(), key=lambda kv: int(kv[0])):
        frame = get_bitstring(hive_type=HiveMessageType.BINARY, payload=clip,
                              compressed=False, hivemeta={"file_name": f"{name}.bin"},
                              binary_type=Kind(int(wire)), versioned=True)
        cases.append({"name": f"payload type {wire} is {name}",
                      "frame": base64.b64encode(frame.bytes).decode(),
                      "expected_kind": name,
                      "expected_metadata": {"file_name": f"{name}.bin"},
                      "expected_payload": base64.b64encode(clip).decode()})
    # A frame whose metadata is worth compressing. The encoder chooses per
    # frame -- whichever of the two is shorter -- so a hub really does send
    # these, and an SDK that cannot inflate loses the utterance while the clip
    # still arrives. Without a case here that gap ships untested in every
    # language that had to write its own inflate.
    talkative = {"lang": "fr-FR", "file_name": "prout.wav",
                 "utterance": "Pfffft. " * 40}
    compressed = get_bitstring(hive_type=HiveMessageType.BINARY, payload=clip,
                               compressed=True, hivemeta=talkative,
                               binary_type=Kind.TTS_AUDIO, versioned=True)
    cases.append({"name": "compressed metadata still reads, and the clip is never inflated",
                  "frame": base64.b64encode(compressed.bytes).decode(),
                  "expected_kind": "tts_audio",
                  "expected_metadata": talkative,
                  "expected_payload": base64.b64encode(clip).decode()})
    bus = get_bitstring(hive_type=HiveMessageType.BUS,
                        payload=json.dumps({"type": "speak", "data": {"utterance": "Pfffft."}}),
                        compressed=False, hivemeta={}, versioned=True)
    return {"contract": "Frames from hivemind-bus-client's own encoder. A BINARY "
                        "frame's payload is bytes; every other type binarized on "
                        "the wire is still JSON.",
            "cases": cases,
            "bus_frame": base64.b64encode(bus.bytes).decode()}


def check_binary_frames(data):
    import base64

    from hivemind_bus_client.serialization import decode_bitstring

    for row in data["cases"]:
        message = decode_bitstring(base64.b64decode(row["frame"]))
        assert message.payload == base64.b64decode(row["expected_payload"]), row["name"]
        assert message.metadata == row["expected_metadata"], row["name"]
    bus = decode_bitstring(base64.b64decode(data["bus_frame"]))
    # A binarized BUS frame decodes to a Message, not to bytes: only BINARY
    # carries a payload the library leaves alone.
    assert bus.payload.msg_type == "speak"
    assert bus.payload.data == {"utterance": "Pfffft."}


def check_questions(question):
    for row in question["cases"]:
        assert listing.asks(row["text"], row["lang"]) == row["expected"], row


def check_inventory(data):
    assert InventoryCache.key("hub", None) == data["cache_key"]
    inventory = Inventory.from_dict(data["inventory"])
    for row in data["examples"]:
        assert list(inventory.intents[0].examples(row["language"], row["limit"])) == row["expected"], row
    for row in data["speaks"]:
        assert inventory.skills[0].speaks(row["language"]) == row["expected"], row
    assert inventory.skills[1].speaks("en") is None


def check_reply_claims(claims):
    for row in claims["cases"]:
        reply = ThalovantReply(text="reply", handled=row["handled"],
            events=tuple(ThalovantEvent("speak", {}, context, None) for context in row["contexts"]),
            failure_event=ThalovantEvent("failure", {}, {}, None) if row["failed"] else None)
        assert list(reply.pipeline_ids) == row["expected"]["pipeline_ids"], row["name"]
        assert list(reply.skill_ids) == row["expected"]["skill_ids"], row["name"]
        assert reply.claimed == row["expected"]["claimed"], row["name"]


# One entry per committed vector file, so a file with nobody to execute it is a
# visible hole rather than a quiet one: the parity contract asks which test runs
# each vector, and this is the answer for the three generated here.
CHECKS = {
    "binary-frames.json": check_binary_frames,
    "question-vectors.json": check_questions,
    "inventory-vectors.json": check_inventory,
    "reply-claim-vectors.json": check_reply_claims,
}


def check(directory):
    # Execute the committed inputs, so Python Unicode database additions do not
    # spuriously invalidate existing vectors on a supported interpreter.
    for name, run in CHECKS.items():
        run(json.loads((directory / name).read_text()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parents[1] / "contracts/conformance")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        check(args.directory)
        print("Public SDK conformance passed")
    else:
        args.directory.mkdir(parents=True, exist_ok=True)
        for name, data in vectors().items():
            (args.directory / name).write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n")
