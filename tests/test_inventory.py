"""The presentable inventory, its cache, and the listing helpers."""
from __future__ import annotations

import json
import os
import time

from thalovant import Intent, Inventory, InventoryCache, Skill, common_affix, friendly_title, humanize, languages_present, sort_key, strip_affix
from thalovant.inventory import identity_host


def _inventory():
    weather = Skill(
        id="thalovant-skill-weather.thalovant", title="Weather", locales=("en-US", "fr-FR"),
        intents=(
            Intent(id="a", name="current.weather", skill_id="thalovant-skill-weather.thalovant", engine="padatious",
                   phrases={"en-US": ("weather in", "what is the weather"), "fr-FR": ("quel temps fait-il",)}),
            Intent(id="b", name="high_low.weather", skill_id="thalovant-skill-weather.thalovant", engine="padatious"),
        ),
    )
    return Inventory(hub_id="hub-1", hub_name="Custos", source="hub", generated_at="2026-09-13T00:00:00Z",
                     skills=(weather,), notes=("from the hub",))


def test_the_inventory_round_trips_through_the_cache_and_ages_out(tmp_path):
    cache = InventoryCache(tmp_path / "cache", ttl=3600)
    key = InventoryCache.key("hub", None)
    assert cache.load(key) is None
    cache.store(key, _inventory())
    stored = cache.path(key)
    assert oct(stored.stat().st_mode & 0o777) == "0o600"
    again = cache.load(key)
    assert again == _inventory() and again.live and again.has_phrases
    old = time.time() - 7200
    os.utime(stored, (old, old))
    assert cache.load(key) is None, "an hour is what a listing stays true for"


def test_a_cache_from_another_release_is_refused_not_misread(tmp_path):
    cache = InventoryCache(tmp_path)
    path = cache.path("k")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cache_version": 99, "skills": []}), encoding="utf-8")
    assert cache.load("k") is None


def test_the_key_names_the_hub_and_separates_identities(tmp_path):
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"default_master": "wss://abc.thalovant.io", "access_key": "x"}), encoding="utf-8")
    assert identity_host(identity) == "abc.thalovant.io"
    key = InventoryCache.key("hub", identity)
    assert key.startswith("hub-abc.thalovant.io-") and len(key.rsplit("-", 1)[1]) == 8
    assert InventoryCache.key("hub", identity) != InventoryCache.key("control", identity)
    assert InventoryCache.key("hub", None).startswith("hub-local-")


def test_examples_prefer_whole_sentences_in_the_asked_language():
    intent = _inventory().skills[0].intents[0]
    assert intent.examples("en-GB", 1) == ("what is the weather",)
    assert intent.examples("fr-CA", 5) == ("quel temps fait-il",)
    assert intent.examples(None, 0) == ("weather in", "what is the weather")


def test_a_skill_says_whether_it_speaks_a_language_or_says_nothing():
    weather = _inventory().skills[0]
    assert weather.speaks("fr-CA") is True and weather.speaks("de-DE") is False
    assert Skill(id="x", title="X", locales=(), intents=()).speaks("fr-FR") is None
    assert languages_present(_inventory()) == ("en-US", "fr-FR")


def test_titles_and_intent_names_read_the_way_a_person_says_them():
    assert friendly_title("thalovant-skill-custos-query.thalovant") == "Custos Query"
    assert friendly_title("ovos-skill-date-time.openvoiceos") == "Date Time"
    assert friendly_title("weird") == "Weird"
    assert humanize("high_low.weather") == "high low weather"
    assert common_affix(["current.weather", "high_low.weather", "forecast.weather"]) == ("suffix", "weather")
    assert common_affix(["custos.status", "custos.incidents"]) == ("prefix", "custos")
    assert common_affix(["weather"]) == (None, "")
    assert common_affix(["weather", "forecast.weather"]) == (None, ""), "nothing is stripped when a name would go blank"
    assert strip_affix("high_low.weather", "suffix", "weather") == "high low"
    assert strip_affix("custos.status", "prefix", "custos") == "status"
    assert sorted(["intent10", "intent2", "Intent1"], key=sort_key) == ["Intent1", "intent2", "intent10"]



def test_cache_rejects_keys_that_escape_its_directory(tmp_path):
    cache = InventoryCache(tmp_path / "cache")
    for key in ("/../../outside", "x/../../../outside", "x\\outside", "x" * 161):
        cache.store(key, _inventory())
        assert cache.load(key) is None
    assert not list(tmp_path.rglob("*.json"))


def test_cache_does_not_follow_a_predictable_scratch_symlink(tmp_path):
    cache = InventoryCache(tmp_path / "cache")
    cache.directory.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("keep this")
    cache.path("safe").with_suffix(".partial").symlink_to(outside)
    cache.store("safe", _inventory())
    assert outside.read_text() == "keep this"
    assert cache.load("safe") == _inventory()
    assert cache.path("safe").stat().st_mode & 0o777 == 0o600


def test_concurrent_cache_writers_leave_a_complete_inventory(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    cache = InventoryCache(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: cache.store("shared", _inventory()), range(32)))
    assert cache.load("shared") == _inventory()
    assert not list(tmp_path.glob(".intents-*.partial"))


def test_empty_affix_names_remain_empty():
    assert strip_affix("", "suffix", "token") == ""
    assert strip_affix("._", "prefix", "token") == "._"


def test_corrupt_cache_shapes_are_refused_without_silent_field_loss(tmp_path):
    import copy
    import pytest
    for patch in ({"cache_version": True}, {"cache_version": 1.0}, {"skills": "wrong"}, {"notes": "wrong"}):
        raw = _inventory().as_dict() | patch
        with pytest.raises(ValueError):
            Inventory.from_dict(raw)
    raw = copy.deepcopy(_inventory().as_dict())
    del raw["skills"][0]["intents"][0]["engine"]
    with pytest.raises(ValueError):
        Inventory.from_dict(raw)
    cache = InventoryCache(tmp_path)
    cache.path("bad").write_text(json.dumps(raw))
    assert cache.load("bad") is None
    for ttl in (-1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            InventoryCache(tmp_path, ttl=ttl)


def test_cache_keys_normalize_hosts_and_hash_the_full_hostname(tmp_path):
    identity = tmp_path / "identity.json"
    first, second = "a" * 40 + "one.example", "a" * 40 + "two.example"
    identity.write_text(json.dumps({"default_master": first}))
    assert identity_host(identity) == first
    key = InventoryCache.key("hub", identity)
    identity.write_text(json.dumps({"default_master": second}))
    assert InventoryCache.key("hub", identity) != key
