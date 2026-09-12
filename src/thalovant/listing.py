"""What a person can say, set the way a person reads it.

A skill registers patterns for a matcher: ``(clear|erase|delete) (your|the)
memory``, ``volume [to] {level} percent``, ``did (i|we) (already |)ask``.
The hub hands them back as written, and a listing whose whole job is to
tell somebody what they can say used to show them a grammar. Two clients
then turned the patterns into sentences, each in its own way and each with
its own word lists, and printed different things for the same hub. This
module is the one way.

Everything that depends on the language is data, next to this file::

    locale/<lang>/language.yaml   the words a rule turns on (LANGUAGE_KEYS)
    locale/scripts.yaml           which marks close a sentence, per script

A language is found by the same matcher the rest of OVOS uses
(``ovos_spec_tools.language``): ``fr-CA`` reads the French file. A language
nothing describes gets no rule at all rather than another language's --
"Coupe le son?" reads as a defect, and so does a Spanish question closed
with a full stop; a bare line is the honest answer.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml
from ovos_spec_tools.language import closest_lang

LOCALE_ROOT = Path(__file__).resolve().parent / "locale"

#: The keys a ``language.yaml`` may carry, and what the listing does with each.
LANGUAGE_KEYS: dict[str, str] = {
    "trailing_words": "a phrase ending here is a prefix waiting for an entity, not a sentence",
    "question_openers": "a phrase opening here is a question",
    "question_words_anywhere": "a phrase holding one of these anywhere is a question",
    "question_patterns": "regular expressions (case-insensitive) that make a phrase a question",
    "written_forms": "words spelled their own way once set as a sentence",
    "slot_examples": "what a slot becomes when a pattern is read aloud",
}

# Past this many words a registered phrase stops being an example and
# becomes a recital; among phrases at least this long, the shorter wins.
_FULL_ENOUGH_WORDS = 8


@lru_cache(maxsize=1)
def described() -> tuple[str, ...]:
    """The languages with a ``language.yaml``."""
    if not LOCALE_ROOT.is_dir():
        return ()
    return tuple(sorted(p.name for p in LOCALE_ROOT.iterdir() if (p / "language.yaml").is_file()))


@lru_cache(maxsize=32)
def language_data(lang: str | None) -> dict:
    """What the listing knows about a language: its ``language.yaml``.

    Regions read their language's file (``fr-CA`` gets ``fr-FR``'s). A
    language nothing describes, or none at all, is an empty mapping.
    """
    languages = described()
    if not lang or not languages:
        return {}
    match = closest_lang(lang, languages)
    if match is None:
        return {}
    loaded = yaml.safe_load((LOCALE_ROOT / match / "language.yaml").read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


@lru_cache(maxsize=1)
def scripts() -> dict:
    """What the code knows about writing systems: ``locale/scripts.yaml``."""
    path = LOCALE_ROOT / "scripts.yaml"
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def marks(kind: str, spacing: str) -> str:
    """The punctuation ``scripts.yaml`` lists under ``kind`` (``sentence_ends``,
    ``clause_breaks``) for ``spaced`` or ``unspaced`` scripts, as one string."""
    return str((scripts().get(kind) or {}).get(spacing) or "")


def sentence_ends() -> str:
    """Every mark a sentence ends on, in any script."""
    return marks("sentence_ends", "spaced") + marks("sentence_ends", "unspaced")


def slot_examples(lang: str | None) -> dict[str, str]:
    """What each slot becomes when a pattern in ``lang`` is read aloud."""
    examples = language_data(lang).get("slot_examples") or {}
    return {str(name): str(value) for name, value in examples.items()}


def _words(lang: str | None, key: str) -> frozenset[str]:
    """One word list of the language's file, lower-cased.

    With no language at all, every described language's list together: a
    listing with no language ranks phrases it cannot place, and a trailing
    word in any language still marks a prefix rather than a sentence.
    """
    languages = (lang,) if lang else described()
    return frozenset(
        str(word).lower()
        for tag in languages
        for word in language_data(tag).get(key) or ())


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
