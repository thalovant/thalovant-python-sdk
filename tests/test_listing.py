"""A registered pattern, set the way a person reads it -- and every word a
rule turns on read from the `thalovant-languages` package, never a
constant here.

What the data files contain is the languages package's business and is
gated there. These tests pin what a client may rely on: the sentences the
listing prints for the languages the hub speaks, the order phrases are
shown in, that a language invented in a temporary tree gets every rule its
file states with no code change, and that a client without the language
data prints bare lines rather than guessed marks.
"""
from __future__ import annotations

import pytest
import thalovant_languages

from thalovant import HubIntent, as_sentence, listing, speakable


@pytest.fixture(autouse=True)
def _installed_data(monkeypatch):
    monkeypatch.delenv(thalovant_languages.ENV_OVERRIDE, raising=False)
    thalovant_languages.refresh()
    listing._question_pattern.cache_clear()
    yield
    thalovant_languages.refresh()
    listing._question_pattern.cache_clear()


@pytest.fixture
def invented(tmp_path, monkeypatch):
    """A language nothing in the code has heard of, described only by a file."""
    tree = tmp_path / "languages"
    (tree / "xq").mkdir(parents=True)
    (tree / "xq" / "language.yaml").write_text(
        "trailing_words: [nef]\n"
        "question_openers: [vark]\n"
        "question_words_anywhere: [plim]\n"
        "question_patterns: ['\\bglo[- ]ta\\b']\n"
        "written_forms: {o: O}\n"
        "slot_examples: {thing: the widget}\n",
        encoding="utf-8",
    )
    (tree / "scripts.yaml").write_text(
        (thalovant_languages.DATA_ROOT / "scripts.yaml").read_text(encoding="utf-8"),
        encoding="utf-8")
    monkeypatch.setenv(thalovant_languages.ENV_OVERRIDE, str(tree))
    thalovant_languages.refresh()
    listing._question_pattern.cache_clear()
    return tree


def test_the_language_data_is_installed_for_the_tests():
    assert listing.available()
    assert listing.language_data("fr-CA")["question_openers"]
    assert listing.language_data("zh-CN") == {} and listing.language_data(None) == {}


# -- a pattern read aloud ---------------------------------------------------------

def test_a_slot_reads_as_the_language_s_own_example():
    assert speakable("volume [to] {level} percent", lang="en-US") == "volume fifty percent"
    assert speakable("volume [à] {level} pour cent", lang="fr-FR") == "volume cinquante pour cent"
    # The caller's example wins; a language nothing describes keeps the name.
    assert speakable("weather in {location}", {"location": "Sherbrooke"}, "en") == (
        "weather in Sherbrooke")
    assert speakable("weather in {location}", lang="zh") == "weather in location"
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
    # The installed languages are not there any more: this tree is the whole world.
    assert as_sentence("what time is it", "en-US") == "What time is it"


# -- a client without the language data ---------------------------------------

def test_without_the_languages_package_lines_are_bare_and_slots_keep_their_names(monkeypatch):
    monkeypatch.setattr(listing, "_languages", None)
    listing._question_pattern.cache_clear()
    assert not listing.available()
    assert as_sentence("do i need a jacket", "en-US") == "Do i need a jacket"
    assert as_sentence("quelle heure est-il?", "fr") == "Quelle heure est-il?"
    assert speakable("volume [to] {level} percent", lang="en-US") == "volume level percent"
    assert listing.rank(("weather in", "what is the weather"), "en") == (
        "what is the weather", "weather in")  # by length and slots alone



def test_examples_without_language_keep_the_selected_registration_locale():
    intent = HubIntent(skill_id="s", name="n", engine="padatious", phrases={
        "fr-FR": ("volume {level} pour cent",),
        "en-US": ("volume {level} percent",),
    })
    assert intent.examples(speakable=True) == ("volume cinquante pour cent",)
    assert intent.examples(sentence=True) == ("Volume cinquante pour cent.",)


def test_question_patterns_keep_leading_global_flags(invented):
    (invented / "xq" / "language.yaml").write_text(
        "question_openers: [vark]\nquestion_patterns: ['(?i)^is it', '(?m)^can it']\n",
        encoding="utf-8",
    )
    assert listing.asks("IS IT ready", "xq")
    assert listing.asks("CAN IT work", "xq")
    assert as_sentence("is it ready", "xq") == "Is it ready?"


@pytest.mark.parametrize("rules,question", [
    ("question_patterns: ['^can it']\n", "can it work"),
    ("question_words_anywhere: [plim]\n", "go plim now"),
])
def test_optional_question_categories_work_without_openers(invented, rules, question):
    (invented / "xq" / "language.yaml").write_text(rules, encoding="utf-8")
    assert listing.asks(question, "xq")
    assert as_sentence(question, "xq").endswith("?")
    assert as_sentence("go home", "xq") == "Go home."
