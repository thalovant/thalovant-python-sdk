"""What a person can say, set the way a person reads it.

A skill registers patterns for a matcher: ``(clear|erase|delete) (your|the)
memory``, ``volume [to] {level} percent``, ``did (i|we) (already |)ask``.
The hub hands them back as written, and a listing whose whole job is to
tell somebody what they can say used to show them a grammar. Two clients
then turned the patterns into sentences, each in its own way and each with
its own word lists, and printed different things for the same hub. This
module is the one way.

Nothing here knows a word of any language. What makes a phrase a question,
what a slot reads as, which marks close a sentence: all of it is the
``thalovant-languages`` package (the ``listing`` extra), one file per
language, found by the matcher the rest of OVOS uses so ``fr-CA`` reads the
French file. Without that package a listing still prints, capitalised and
bare -- no rule, rather than a guessed one, because "Coupe le son?" reads
as a defect and so does a Spanish question closed with a full stop.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable

try:
    import thalovant_languages as _languages
except ImportError:  # pragma: no cover - exercised by monkeypatching below
    _languages = None  # type: ignore[assignment]


def available() -> bool:
    """Whether the language data is installed (``pip install thalovant[listing]``)."""
    return _languages is not None


def language_data(lang: str | None) -> dict:
    """What is known about a language: its file in ``thalovant-languages``,
    by the closest tag. An empty mapping for a language nothing describes,
    for none at all, and for a client without the language data."""
    return _languages.language(lang) if _languages is not None else {}


def sentence_ends() -> str:
    """Every mark a sentence ends on, in any script."""
    if _languages is None:
        return ""
    return _languages.marks("sentence_ends", "spaced") + _languages.marks("sentence_ends", "unspaced")


def slot_examples(lang: str | None) -> dict[str, str]:
    """What each slot becomes when a pattern in ``lang`` is read aloud."""
    examples = language_data(lang).get("slot_examples") or {}
    return {str(name): str(value) for name, value in examples.items()}


def _words(lang: str | None, key: str) -> frozenset[str]:
    """One word list of the language, lower-cased; with no language at all,
    every described language's list together, so a listing with no language
    still ranks a prefix in any language after a whole sentence."""
    return _languages.words(lang, key) if _languages is not None else frozenset()


# Past this many words a registered phrase stops being an example and
# becomes a recital; among phrases at least this long, the shorter wins.
_FULL_ENOUGH_WORDS = 8


def dangling(text: str, lang: str | None) -> bool:
    """Whether a phrase ends on one of the language's ``trailing_words``: a
    keyword prefix waiting for an entity ("weather in"), not something
    anybody says on its own."""
    words = text.rstrip(sentence_ends() + " ").split()
    return bool(words) and words[-1].lower() in _words(lang, "trailing_words")


@lru_cache(maxsize=16)
def _question_pattern(lang: str | None) -> re.Pattern[str] | None:
    patterns = language_data(lang).get("question_patterns") or ()
    if not patterns:
        return None
    return re.compile("|".join(f"(?:{pattern})" for pattern in patterns), re.IGNORECASE)


def asks(text: str, lang: str | None) -> bool:
    """Whether a registered phrase is asking something, by the language's
    own ``question_openers``, ``question_words_anywhere`` and
    ``question_patterns``."""
    pattern = _question_pattern(lang)
    if pattern is not None and pattern.search(text):
        return True
    words = [word.strip(",;:!?.’'\"()").lower() for word in text.split()]
    words = [word for word in words if word]
    if not words:
        return False
    if words[0] in _words(lang, "question_openers"):
        return True
    return bool(_words(lang, "question_words_anywhere").intersection(words))


def as_sentence(text: str, lang: str | None = None) -> str:
    """A phrase set the way somebody reads one rather than matches it.

    Capitalised, and closed with a question mark where it asks and a full
    stop where it tells the hub to do something. Punctuated only in a
    language whose interrogatives are known: guessing wrong is worse than
    leaving the line bare. A phrase that trails off ("les conditions
    actuelles a") is a prefix waiting for an entity rather than a sentence,
    so it is capitalised and left unpunctuated.
    """
    text = text.strip()
    if not text:
        return text
    # Not str.capitalize(), which lowercases the rest and would spell the
    # answer to "quand est la fete du Canada" with a small c.
    text = text[0].upper() + text[1:]
    if text.endswith(tuple(sentence_ends())) or dangling(text, lang):
        return text
    if not lang or not _words(lang, "question_openers"):
        return text
    # Locale files are lower case throughout, so English first person
    # arrives as "do i need a jacket"; ``written_forms`` spells it back.
    for word, written in (language_data(lang).get("written_forms") or {}).items():
        text = re.sub(rf"\b{re.escape(str(word))}\b", str(written), text)
    return text + ("?" if asks(text, lang) else ".")


def rank(phrases: Iterable[str], lang: str | None) -> tuple[str, ...]:
    """Registered phrases in the order worth showing them: whole sentences
    before prefixes, sentences before patterns with a slot, and the fullest
    phrasing first.

    The listing shows one example per intent, so that example is the whole
    of what a reader learns about it -- and the shortest phrasing is usually
    the worst advertisement for what the intent does. The weather skill
    registers both "aqi" and "air quality"; the date skill both "current
    time" and "what time is it". Shortest-first showed the fragment in each
    pair. Capped at eight words, because past that a phrase is a recital;
    among equals the shorter string wins, so the choice stays stable.
    """
    def key(text: str) -> tuple:
        words = len(text.split())
        return (dangling(text, lang), "{" in text, -min(words, _FULL_ENOUGH_WORDS), len(text))

    return tuple(sorted(phrases, key=key))
