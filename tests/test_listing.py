"""A registered pattern, set the way a person reads it -- and every word a
rule turns on read from the language's file, never a constant in the code.

Two gates. Every `language.yaml` in the tree is checked for shape, so a
misspelt key or a pattern that does not compile fails here rather than on
the first listing in that language. And a language that exists only as a
directory in a temporary tree gets every rule its file states, which is the
whole point: adding a language is adding a file.
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

from thalovant import HubIntent, as_sentence, speakable
from thalovant import listing

LOCALE = pathlib.Path(listing.__file__).resolve().parent / "locale"
LANGUAGE_FILES = sorted(LOCALE.glob("*/language.yaml"))
LIST_KEYS = {"trailing_words", "question_openers", "question_words_anywhere", "question_patterns"}
MAP_KEYS = {"written_forms", "slot_examples"}


def _fresh(monkeypatch, root: pathlib.Path) -> None:
    monkeypatch.setattr(listing, "LOCALE_ROOT", root)
    for cached in (listing.described, listing.language_data, listing.scripts,
                   listing._question_pattern):
        cached.cache_clear()


@pytest.fixture
def invented(tmp_path, monkeypatch):
    """A language nothing in the code has heard of, described only by a file."""
    directory = tmp_path / "locale" / "xq"
    directory.mkdir(parents=True)
    (directory / "language.yaml").write_text(
        "trailing_words: [nef]\n"
        "question_openers: [vark]\n"
        "question_words_anywhere: [plim]\n"
        "question_patterns: ['\\bglo[- ]ta\\b']\n"
        "written_forms: {o: O}\n"
        "slot_examples: {thing: the widget}\n",
        encoding="utf-8",
    )
    (tmp_path / "locale" / "scripts.yaml").write_text(
        (LOCALE / "scripts.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    _fresh(monkeypatch, tmp_path / "locale")
    yield directory
    _fresh(monkeypatch, LOCALE)


# -- every file in the tree is well formed ------------------------------------

def test_the_languages_the_hub_speaks_are_described():
    assert {"en-US", "fr-FR"} <= set(listing.described())


@pytest.mark.parametrize("path", LANGUAGE_FILES, ids=lambda p: p.parent.name)
def test_a_language_file_names_only_keys_the_code_reads(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data, path
    unknown = sorted(set(data) - set(listing.LANGUAGE_KEYS))
    assert not unknown, unknown


@pytest.mark.parametrize("path", LANGUAGE_FILES, ids=lambda p: p.parent.name)
def test_a_language_file_has_the_shape_each_key_needs(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key in LIST_KEYS & set(data):
        assert isinstance(data[key], list) and data[key], key
        # A bare on, off, yes or no is a boolean to YAML, not a word; quote it.
        odd = [word for word in data[key] if not isinstance(word, str) or not word.strip()]
        assert not odd, f"{key}: {odd!r} -- quote a word YAML reads as something else"
        assert len(set(data[key])) == len(data[key]), f"{key} repeats a word"
    for key in MAP_KEYS & set(data):
        assert isinstance(data[key], dict) and data[key], key
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in data[key].items()), key
    for pattern in data.get("question_patterns", ()):
        re.compile(pattern, re.IGNORECASE)
    assert "trailing_words" in data and "question_openers" in data and "slot_examples" in data


def test_the_script_table_closes_sentences_in_every_script():
    assert "?" in listing.marks("sentence_ends", "spaced")
    assert "。" in listing.marks("sentence_ends", "unspaced")
    assert "," in listing.marks("clause_breaks", "spaced")
    assert listing.marks("no-such-kind", "spaced") == ""


# -- a language is found by the matcher the rest of OVOS uses -------------------

def test_a_region_reads_its_language_file():
    french = listing.language_data("fr-FR")
    assert french["trailing_words"]
    assert listing.language_data("fr-CA") == french
    assert listing.language_data("fr") == french
    assert listing.language_data("en-GB") == listing.language_data("en-US")


def test_a_language_nothing_describes_gets_no_rule_rather_than_another_language_s():
    assert listing.language_data("es-ES") == {}
    assert listing.language_data(None) == {}
    assert listing.language_data("") == {}


# -- a pattern read aloud ---------------------------------------------------------

def test_a_slot_reads_as_the_language_s_own_example():
    assert speakable("volume [to] {level} percent", lang="en-US") == "volume fifty percent"
    assert speakable("volume [à] {level} pour cent", lang="fr-FR") == "volume cinquante pour cent"
    # The caller's example wins; a language nothing describes keeps the name.
    assert speakable("weather in {location}", {"location": "Sherbrooke"}, "en") == (
        "weather in Sherbrooke")
    assert speakable("weather in {location}", lang="es") == "weather in location"
    assert speakable("set the {gadget_name} going", lang="en") == "set the gadget name going"


# -- set as a sentence ------------------------------------------------------------

@pytest.mark.parametrize("phrase", [
    "quelle heure est-il", "y a t il de la neige", "ai je besoin d une veste",
    "qu'est-ce que tu sais faire", "il est quelle heure", "on est quel mois",
    "combien de jours avant noel", "quand est la fete du Canada",
])
def test_french_questions_get_their_mark(phrase):
    assert as_sentence(phrase, "fr-FR").endswith("?"), phrase


@pytest.mark.parametrize("phrase", [
    "prends rendez-vous avec le docteur", "oublie ce que j'ai dit", "coupe le son",
])
def test_french_orders_get_a_full_stop(phrase):
    said = as_sentence(phrase, "fr-FR")
    assert said.endswith("."), said
    assert said[0].isupper()


def test_english_first_person_is_written_as_a_capital():
    assert as_sentence("what did i ask you to do", "en-US") == "What did I ask you to do?"
    assert as_sentence("forget what i said", "en-US") == "Forget what I said."
    assert as_sentence("tell me what happened", "en-US") == "Tell me what happened."
    assert as_sentence("do i need a jacket", "en-US") == "Do I need a jacket?"
    assert as_sentence("is it raining", "en-US") == "Is it raining?"


def test_a_capital_already_there_is_kept():
    assert as_sentence("quand est la fete du Canada", "fr") == "Quand est la fete du Canada?"
    assert as_sentence("weather in Toronto", "en") == "Weather in Toronto."


def test_a_language_without_known_interrogatives_is_left_bare():
    assert as_sentence("como esta el tiempo", "es-ES") == "Como esta el tiempo"
    assert as_sentence("what time is it", None) == "What time is it"


def test_a_prefix_waiting_for_an_entity_is_not_punctuated():
    assert as_sentence("current conditions in", "en") == "Current conditions in"
    assert as_sentence("quelles sont les conditions actuelles a", "fr") == (
        "Quelles sont les conditions actuelles a")


def test_setting_a_sentence_twice_changes_nothing():
    once = as_sentence("quelle heure est-il", "fr")
    assert once == "Quelle heure est-il?"
    assert as_sentence(once, "fr") == once
    assert as_sentence("", "fr") == ""


# -- the order worth showing them in --------------------------------------------

def test_the_fullest_sentence_comes_first_and_a_prefix_last():
    ranked = listing.rank(("aqi", "air quality", "weather in", "what is the weather in {location}",
                           "what is the air quality like today"), "en-US")
    assert ranked[0] == "what is the air quality like today"
    assert ranked[-1] == "weather in"
    assert ranked.index("air quality") < ranked.index("aqi")
    assert ranked.index("what is the weather in {location}") > ranked.index("aqi")


def test_examples_count_what_is_shown_not_what_was_tried():
    intent = HubIntent(skill_id="s", name="n", engine="padatious", phrases={
        "en-us": ("[please]", "(repeat|say) that (again|)", "[please] repeat that",
                  "volume [to] {level} percent"),
    })
    assert intent.examples("en-us", 2, speakable=True) == ("repeat that", "volume fifty percent")
    assert intent.examples("en-US", 2, sentence=True) == ("Repeat that.", "Volume fifty percent.")
    assert intent.examples("fr", 1, sentence=True) == ()


def test_phrases_are_found_by_the_language_s_nearest_registration():
    intent = HubIntent(skill_id="s", name="n", engine="padatious", phrases={
        "en-us": ("what time is it",), "fr-fr": ("quelle heure est-il",),
    })
    assert intent.phrases_for("fr") == ("quelle heure est-il",)
    assert intent.phrases_for("fr-CA") == ("quelle heure est-il",)
    assert intent.phrases_for("en-GB") == ("what time is it",)
    assert intent.phrases_for("de") == ()
    assert intent.examples("fr-CA", 1, sentence=True) == ("Quelle heure est-il?",)


# -- a language that is only a file gets every rule it states -----------------

def test_a_language_invented_as_a_file_gets_its_rules(invented):
    assert as_sentence("vark o there", "xq") == "Vark O there?"
    assert as_sentence("go plim now", "xq") == "Go plim now?"
    assert as_sentence("we glo-ta go", "xq") == "We glo-ta go?"
    assert as_sentence("go home", "xq") == "Go home."
    assert as_sentence("go to nef", "xq") == "Go to nef"
    assert speakable("open {thing}", lang="xq-ZZ") == "open the widget"
    assert listing.rank(("go to nef", "go home now"), "xq") == ("go home now", "go to nef")
