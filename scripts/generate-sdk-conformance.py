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


def vectors():
    marks = [chr(i) for i in range(0x110000) if "QUESTION MARK" in unicodedata.name(chr(i), "")]
    languages = [None, "en", "fr-CA", "ar", "xq", "ja", ""]
    texts = ["", "  ", "go home", "is it ready", "weather in", "ai je besoin d une veste", "y a t il de la neige", "hello!", "?hello"]
    texts += ["hello" + mark + "  " for mark in marks]
    texts += ["hello" + chr(i) for i in [0x10441, 0x11144, 0x1e960, 0x1fbc5, 0xe0040]]
    source = {"thalovant": importlib.metadata.version("thalovant"),
              "thalovant-languages": importlib.metadata.version("thalovant-languages"),
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
    return {"question-vectors.json": question, "inventory-vectors.json": inventory_vectors}


def check(directory):
    # Execute the committed inputs, so Python Unicode database additions do not
    # spuriously invalidate existing vectors on a supported interpreter.
    question = json.loads((directory / "question-vectors.json").read_text())
    for row in question["cases"]:
        assert listing.asks(row["text"], row["lang"]) == row["expected"], row
    data = json.loads((directory / "inventory-vectors.json").read_text())
    assert InventoryCache.key("hub", None) == data["cache_key"]
    inventory = Inventory.from_dict(data["inventory"])
    for row in data["examples"]:
        assert list(inventory.intents[0].examples(row["language"], row["limit"])) == row["expected"], row
    for row in data["speaks"]:
        assert inventory.skills[0].speaks(row["language"]) == row["expected"], row
    assert inventory.skills[1].speaks("en") is None


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
