"""OVOS-INTENT-2 language matching, without the OVOS stack.

``ovos_spec_tools.language.closest_lang`` measures with ``langcodes``, which
brings a large CLDR database along. This is the same policy over the subset of
that data the policy reads -- likely subtags, aliases, default scripts,
macrolanguages, the language and script distance tables and five region
groups -- taken from langcodes 3.5.1. The tuple distance is adapted from
langcodes (MIT; see LICENSE-langcodes) by way of the Go SDK, and the shared
``language-matching-vectors.json`` cases hold every SDK to the same answers.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from functools import lru_cache
from importlib import resources
from typing import Any, NamedTuple

__all__ = ["closest_lang", "lang_distance", "usual_form"]

#: A distance up to this is a usable regional match (OVOS-INTENT-2 §2.2).
DEFAULT_MAX_LANGUAGE_DISTANCE = 10

_SCRIPT = re.compile(r"^[a-z]{4}$")
_REGION = re.compile(r"^(?:[a-z]{2}|[0-9]{3})$")


class _Tag(NamedTuple):
    language: str
    script: str
    region: str


@lru_cache(maxsize=1)
def _data() -> dict[str, Any]:
    text = resources.files("thalovant").joinpath("data/language-matching.json").read_text(
        encoding="utf-8"
    )
    data = json.loads(text)
    if not isinstance(data, dict):  # pragma: no cover - the file ships with the package
        raise RuntimeError("invalid embedded language-matching data")
    return data


def _parse(value: str, aliases: bool) -> _Tag:
    data = _data()
    languages: dict[str, str] = data["languages"]
    value = value.strip().replace("_", "-").lower()
    if aliases and languages.get(value):
        value = languages[value].lower()
    tokens = value.split("-")
    primary = tokens[0] or "und"
    language, script, region = primary, "", ""
    if aliases and languages.get(primary):
        language, script, region = _parse(languages[primary], False)
    only_script = True
    for token in tokens[1:]:
        if not _SCRIPT.match(token):
            only_script = False
        if len(token) == 1:
            break
        if _SCRIPT.match(token):
            script = data["scripts"].get(token) or token[:1].upper() + token[1:]
        elif _REGION.match(token):
            region = data["territories"].get(token) or token.upper()
    if script == data["default_scripts"].get(language, "\0"):
        script = ""
    if language == "pt" and not script and not region and only_script:
        region = "PT"
    return _Tag(language, script, region)


def _maximize(tag: _Tag) -> _Tag:
    data = _data()
    language, script, region = tag
    if language == "und" and not script and not region:
        return _Tag("und", "Zzzz", "ZZ")
    language = data["macrolanguages"].get(language) or language

    def join(*parts: str) -> str:
        return "-".join(part for part in parts if part)

    probes = [join(language, script, region), join(language, region), join(language, script), language]
    if script:
        probes.append(f"und-{script}")
    probes.append("und")
    likely: dict[str, str] = data["likely"]
    for probe in probes:
        found = likely.get(probe)
        if found:
            parts = found.split("-")
            return _Tag(
                parts[0] if language == "und" else language,
                script or parts[1],
                region or parts[2],
            )
    raise RuntimeError("invalid embedded likely-subtag data")  # pragma: no cover


def _inside(group: str, region: str) -> bool:
    members: list[str] = _data()["regions"].get(group, [])
    return region in members


def lang_distance(wanted: str, candidate: str) -> int:
    """How far apart two BCP-47 tags are: 0 identical, above 10 not a match."""
    a = _maximize(_parse(wanted, True))
    b = _maximize(_parse(candidate, True))
    distances: dict[str, dict[str, int]] = _data()["distances"]

    def lookup(source: str, target: str, fallback: int) -> int:
        return distances.get(source, {}).get(target, fallback)

    result = 0
    if a.language != b.language:
        result += lookup(a.language, b.language, 80)
    pair_a, pair_b = f"{a.language}_{a.script}", f"{b.language}_{b.script}"
    if a.script != b.script:
        result += lookup(pair_a, pair_b, 50)
    if a.region == b.region:
        return result
    region_distance = 4
    if pair_a == pair_b:
        if a.language == "ar":
            if _inside("MAGHREB", a.region) != _inside("MAGHREB", b.region):
                region_distance = 5
        elif a.language == "en":
            if (a.region == "GB" and not _inside("US", b.region)) or (
                not _inside("US", a.region) and b.region == "GB"
            ):
                region_distance = 3
            elif _inside("US", a.region) != _inside("US", b.region):
                region_distance = 5
        elif _inside("LATIN_AMERICA", a.region) and b.region == "419":
            region_distance = 1
        elif a.language in ("es", "pt"):
            if _inside("AMERICAS", a.region) != _inside("AMERICAS", b.region):
                region_distance = 5
        elif pair_a == "zh_Hant":
            if _inside("CNSAR", a.region) != _inside("CNSAR", b.region):
                region_distance = 5
    return result + region_distance


def closest_lang(
    target: str,
    available: Sequence[str] | Iterable[str],
    max_distance: int = DEFAULT_MAX_LANGUAGE_DISTANCE,
) -> str | None:
    """The entry of ``available`` nearest to ``target``, or ``None``.

    Ties go to the first candidate. The winner is returned verbatim, so a
    caller can map it straight back to a directory or a manifest key.
    """
    best: str | None = None
    best_distance: int | None = None
    for candidate in available:
        distance = lang_distance(target, candidate)
        if best_distance is None or distance < best_distance:
            best, best_distance = candidate, distance
    if best is None or best_distance is None or best_distance > max_distance:
        return None
    return best


def usual_form(tag: str) -> str | None:
    """The form a language is usually written in, lower case, when that differs.

    ``en-CA`` -> ``en-us``, ``fr-BE`` -> ``fr-fr``, ``pt-AO`` -> ``pt-br``, from
    CLDR's likely subtags. ``None`` when there is nothing different to try.

    A language CLDR has no likely subtags for -- Klingon, a private-use code --
    is ``None`` too. langcodes answered those with its root locale's guess,
    ``tlh-us``, which is the confident United States the check below exists
    to refuse.
    """
    text = tag.strip()
    if not text or "" in text.replace("_", "-").split("-"):
        return None
    base = _parse(text, True).language
    if not base or base == "und":
        return None
    data = _data()
    # "und" is "no idea", and CLDR's guess for an unknown language is English.
    # A direct entry in the likely table -- or the macrolanguage it belongs to
    # -- is what says CLDR knows the language: maximizing "zzz" would
    # otherwise answer a confident "zzz-us".
    if not data["likely"].get(base) and not data["likely"].get(data["macrolanguages"].get(base, "")):
        return None
    likely = _maximize(_Tag(base, "", ""))
    # The language keeps its own code: "arb" is usually written "arb-eg", not
    # its macrolanguage's "ar-eg".
    usual = (f"{base}-{likely.region}" if likely.region else base).lower()
    # Byte comparison, not same-language: en-US and en-us are the same
    # language, and the retry exists precisely for that spelling.
    return None if usual == text else usual
