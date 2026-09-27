"""Language matching without the OVOS stack, held to the shared vectors.

``language-matching-vectors.json`` was recorded from this SDK when it still
used ovos-spec-tools and langcodes (see its ``source``); Go and Node run the
same file. Where those libraries are installed, the matcher is also compared
with them directly over a larger sweep.
"""

from __future__ import annotations

import itertools
import json
import random
from pathlib import Path

import pytest

from thalovant._language import closest_lang, lang_distance, usual_form

VECTORS = json.loads(
    (Path(__file__).resolve().parent / "vectors" / "language-matching-vectors.json").read_text(encoding="utf-8")
)


def test_every_shared_case():
    failures = [
        case for case in VECTORS["cases"]
        if closest_lang(case["target"], case["available"]) != case["expected"]
    ]
    assert not failures, failures[:5]
    assert len(VECTORS["cases"]) > 900


def test_the_distance_scale():
    assert lang_distance("en-US", "en-us") == 0
    assert lang_distance("en", "en-US") == 0
    assert lang_distance("fr-CA", "fr-FR") <= 10
    assert lang_distance("en", "fr") > 10
    assert closest_lang("fr", []) is None
    assert closest_lang("fr-CA", ["fr-FR", "fr-CA"]) == "fr-CA"
    # Ties go to the first candidate, returned verbatim.
    assert closest_lang("pt", ["pt_BR", "pt-PT"]) in {"pt_BR", "pt-PT"}


@pytest.mark.parametrize(("tag", "expected"), [
    ("en-CA", "en-us"), ("fr-BE", "fr-fr"), ("pt-AO", "pt-br"), ("en-US", "en-us"),
    ("en-us", None), ("fr-fr", None), ("", None), ("   ", None), ("zzz", None), ("en-", None),
    ("arb", "arb-eg"), ("cmn", "cmn-cn"),
])
def test_usual_form(tag, expected):
    assert usual_form(tag) == expected


def test_the_intents_module_answers_the_same():
    from thalovant import intents

    assert intents.usual_form("en-CA") == "en-us"
    assert intents.same_language("fr_FR", "fr-fr")


def test_matching_agrees_with_ovos_spec_tools_over_a_sweep():
    spec_tools = pytest.importorskip("ovos_spec_tools.language")
    tags = sorted({case["target"] for case in VECTORS["cases"]}
                  | {tag for case in VECTORS["cases"] for tag in case["available"]})
    rng = random.Random(20260927)
    for _ in range(3000):
        target = rng.choice(tags)
        available = rng.sample(tags, rng.randint(1, 4))
        assert closest_lang(target, available) == spec_tools.closest_lang(target, available), (target, available)


def test_usual_form_agrees_with_langcodes_except_where_cldr_has_nothing():
    langcodes = pytest.importorskip("langcodes")

    def reference(tag):
        try:
            base = langcodes.Language.get(tag).language
            if not base or not langcodes.Language.get(base).is_valid():
                return None
            likely = langcodes.Language.get(base).maximize()
            usual = (f"{likely.language}-{likely.territory}" if likely.territory else likely.language).lower()
        except Exception:
            return None
        return None if usual == tag.strip() else usual

    languages = ["en", "fr", "de", "es", "pt", "it", "nl", "zh", "ar", "ja", "sr", "nb", "no", "iw", "in", "ms", "fa"]
    regions = ["US", "GB", "CA", "FR", "BE", "BR", "PT", "AO", "MX", "419", "CN", "TW", "DE", "CH", "IN"]
    tags = {f"{lang}-{region}" for lang, region in itertools.product(languages, regions)} | set(languages)
    # Languages CLDR has no likely subtags for: langcodes guessed "-us" from
    # its root locale, and this SDK says it does not know instead.
    known_differences = {"tlh", "art", "qaa", "i-klingon"}
    for tag in sorted(tags - known_differences):
        assert usual_form(tag) == reference(tag), tag
    for tag in known_differences:
        assert usual_form(tag) is None
