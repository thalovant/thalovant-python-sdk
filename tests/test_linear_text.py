"""Text that comes off the network is processed in linear time, whatever it holds.

``strip_ssml`` runs on every reply and every home request; its tag regex
backtracked cubically on an unclosed tag followed by spaces (1,600 of them
took six seconds). ``speakable`` runs on the hub's intent patterns; it took
one pass per level of nesting. Both are single passes now, and pinned here
to the rules they implement.
"""

from __future__ import annotations

import random
import re
import time

import pytest

from thalovant import intents
from thalovant.home import plain_speech
from thalovant.rich import strip_ssml

#: The documented tag rule as a regex -- fine as a reference on short input,
#: where backtracking cannot cost anything.
TAG_RULE = re.compile(r"<!--.*?-->|<\?.*?\?>|</?[A-Za-z](?:[^>\"']|\"[^\"]*\"|'[^']*')*>", re.DOTALL)

PATHOLOGICAL = {
    "an unclosed tag and spaces": "<b" + " " * 50_000,
    "an unclosed tag and nested quotes": "<b " + "\"'" * 25_000,
    "quoted greater-than signs": ("<a \"" + ">\"") * 10_000,
    "quotes that never close": "<a '" * 20_000,
    "comments that never close": "<!--" * 20_000,
    "instructions that never close": "<?" * 30_000,
    "tags that never close": "<b <i <u " * 10_000,
    "nothing but less-than signs": "<" * 60_000,
    "closing tags that never close": "</a" * 20_000,
}


@pytest.mark.parametrize("text", PATHOLOGICAL.values(), ids=PATHOLOGICAL.keys())
def test_markup_removal_is_linear(text: str) -> None:
    started = time.perf_counter()
    strip_ssml(text)
    plain_speech(text)
    # Each is tens of milliseconds; the old regex took hours on the first.
    assert time.perf_counter() - started < 1.0


def test_markup_removal_is_the_documented_rule() -> None:
    rng = random.Random(20260928)
    alphabet = list("<>/!?-\"' ab=")
    for _ in range(20_000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 14)))
        assert strip_ssml(text) == TAG_RULE.sub("", text), text


@pytest.mark.parametrize(
    "pattern",
    ["[" * 50_000 + "x" + "]" * 50_000, "(" * 50_000 + "a|b" + ")" * 50_000, "(" * 50_000, "[(" * 50_000],
    ids=["deep optional parts", "deep groups", "groups that never close", "both, unclosed"],
)
def test_intent_patterns_are_resolved_in_linear_time(pattern: str) -> None:
    started = time.perf_counter()
    intents.speakable(pattern)
    assert time.perf_counter() - started < 1.0


def test_intent_patterns_resolve_as_innermost_first_did() -> None:
    optional = re.compile(r"\[[^\[\]]*\]")
    group = re.compile(r"\(([^()]*)\)")

    def innermost_first(text: str) -> str:
        while True:
            text, changed = optional.subn("", text)
            if not changed:
                break
        while True:
            text, changed = group.subn(lambda match: intents._choose_branch(match.group(1)), text)
            if not changed:
                return text

    rng = random.Random(20260928)
    alphabet = list("[]()| ab")
    for _ in range(20_000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 16)))
        resolved = intents._resolve_nested(intents._drop_nested(text, "[", "]"), "(", ")", intents._choose_branch)
        assert resolved == innermost_first(text), text


def test_speech_trims_white_space_only() -> None:
    # U+001C..U+001F are separators to str.strip() but not Unicode White_Space.
    assert plain_speech("\x1c Hi \x1f") == "\x1c Hi \x1f"
    assert plain_speech(" 　 Hi  ") == "Hi"
