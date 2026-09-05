"""The intent inventory, against a hub that behaves like the one observed.

Shapes copied from a live runtime on 2026-09-05: ``ovos.intent.list.response``
rows, ``ovos.intent.describe.response`` definitions carrying ``samples`` as the
skill's locale files wrote them, ``hive.policy.denied`` for a type the
connection may not publish, and every reply delivered twice.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from thalovant import (
    AsyncThalovantClient,
    HubIntentInventory,
    ThalovantClient,
    ThalovantIdentity,
    ThalovantPolicyDeniedError,
    ThalovantTimeoutError,
)
from thalovant.intents import SOURCE_ENGINES, SOURCE_MANIFEST, same_language
from thalovant.models import ThalovantConnectionInfo, ThalovantHealth

WEATHER = "thalovant-skill-weather.thalovant"
SHADOW = "thalovant-skill-custos-shadow.thalovant"

# What the hub registered: per language, per intent, the sentences. Weather
# speaks both languages; the shadow skill only English.
REGISTRATIONS: dict[str, dict[tuple[str, str], list[str]]] = {
    "en-us": {
        (WEATHER, "current.weather"): [
            "what is the weather",
            "what is the weather in {location}",
            "how is it outside",
        ],
        (SHADOW, "custos.incidents"): ["are there incidents", "any incidents"],
    },
    "fr-fr": {
        (WEATHER, "current.weather"): [
            "quel temps fait-il",
            "quelle est la météo à {location}",
            "quelle est la météo",
        ],
    },
}
ALLOWED = ["recognizer_loop:utterance", "speak"]


@dataclass
class FakeMessage:
    data: dict[str, Any]
    context: dict[str, Any] | None = None
    msg_type: str | None = None


class FakeHubTransport:
    """A hub session: answers the manifest, or refuses it, twice over."""

    def __init__(
        self,
        *,
        registrations: dict[str, dict[tuple[str, str], list[str]]] | None = REGISTRATIONS,
        refuse: tuple[str, ...] = (),
        silent: tuple[str, ...] = (),
        definitions_in_list: bool = False,
        echo_request_id: bool = True,
        repeats: int = 2,
    ) -> None:
        self.registrations = registrations
        self.refuse = refuse
        self.silent = silent
        self.definitions_in_list = definitions_in_list
        self.echo_request_id = echo_request_id
        self.repeats = repeats
        self.connected = False
        self.emitted: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.handlers: dict[str, list[Callable[[Any], None]]] = {}

    # -- transport surface -------------------------------------------------

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def on_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self.handlers.setdefault(event_name, []).append(handler)

    def remove_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self.handlers[event_name] = [
            entry for entry in self.handlers.get(event_name, []) if entry is not handler
        ]

    def healthcheck(self) -> ThalovantHealth:
        return ThalovantHealth(
            connected=self.connected,
            handshake_complete=self.connected,
            transport_alive=self.connected,
            connection=self.connection_info(),
        )

    def connection_info(self) -> ThalovantConnectionInfo:
        return ThalovantConnectionInfo(phase="ready" if self.connected else "idle")

    def is_connected(self) -> bool:
        return self.connected

    def last_error(self) -> BaseException | None:
        return None

    # -- the hub ------------------------------------------------------------

    def _deliver(self, event_name: str, data: dict[str, Any], context: dict[str, Any]) -> None:
        reply_context = dict(context)
        if not self.echo_request_id:
            reply_context.pop("request_id", None)
        for _ in range(self.repeats):
            for handler in list(self.handlers.get(event_name, [])):
                handler(FakeMessage(data, context=reply_context, msg_type=event_name))

    def emit_event(self, event_type: str, data: dict[str, Any], context: dict[str, Any]) -> None:
        self.emitted.append((event_type, dict(data), dict(context)))
        if event_type in self.refuse:
            self._deliver(
                "hive.policy.denied",
                {
                    "denied_type": event_type,
                    "code": "acl_disallowed_type",
                    "reason": f"{event_type} not in allowed_types",
                    "data": {"msg_type": event_type, "allowed": ALLOWED},
                },
                context,
            )
            return
        if event_type in self.silent:
            return
        lang = str(data.get("lang") or "")
        if event_type == "ovos.intent.list":
            rows = []
            for (skill_id, intent_name), samples in (self.registrations or {}).get(lang, {}).items():
                row = {
                    "skill_id": skill_id, "intent_name": intent_name, "lang": lang.upper()
                    if lang == "fr-fr" else lang,
                    "method": "template", "enabled": True, "session_id": "default",
                }
                if self.definitions_in_list and data.get("include_definitions"):
                    row["definition"] = {
                        "skill_id": skill_id, "intent_name": intent_name,
                        "lang": lang, "samples": samples,
                    }
                rows.append(row)
            self._deliver("ovos.intent.list.response", {"ok": True, "intents": rows}, context)
        elif event_type == "ovos.intent.describe":
            key = (str(data["skill_id"]), str(data["intent_name"]))
            samples = (self.registrations or {}).get(lang, {}).get(key)
            if samples is None:
                payload: dict[str, Any] = {"ok": False, "error": "unknown intent"}
            else:
                payload = {"ok": True, "definitions": [{
                    "method": "template",
                    "definition": {"skill_id": key[0], "intent_name": key[1],
                                   "lang": lang, "samples": samples,
                                   "blacklist": [], "slot_blacklist": {}},
                }]}
            self._deliver("ovos.intent.describe.response", payload, context)
        elif event_type == "intent.service.adapt.manifest.get":
            self._deliver("intent.service.adapt.manifest", {"intents": []}, context)
        elif event_type == "intent.service.padatious.manifest.get":
            names = sorted({
                f"{skill}:{name}"
                for per_language in (self.registrations or {}).values()
                for (skill, name) in per_language
            })
            self._deliver("intent.service.padatious.manifest", {"intents": names}, context)


def identity() -> ThalovantIdentity:
    return ThalovantIdentity(
        access_key="key", password="password", crypto_key="crypto",
        site_id="site", default_master="http://hub.local", default_port=5679,
    )


def client(transport: FakeHubTransport) -> ThalovantClient:
    return ThalovantClient(identity(), transport=transport, reply_settle_seconds=0)


def test_inventory_carries_the_sentences_per_language() -> None:
    hub = FakeHubTransport()
    inventory = client(hub).intents(["en-us", "fr-fr"])

    assert isinstance(inventory, HubIntentInventory)
    assert inventory.source == SOURCE_MANIFEST and not inventory.denied
    assert inventory.languages == ("en-us", "fr-fr")
    assert [skill.skill_id for skill in inventory.skills] == [SHADOW, WEATHER]
    weather = inventory.skills[1].intents[0]
    assert weather.id == f"{WEATHER}:current.weather" and weather.engine == "padatious"
    assert weather.phrases_for("fr-FR") == (
        "quel temps fait-il", "quelle est la météo à {location}", "quelle est la météo",
    )
    assert inventory.skills[1].languages == ("en-us", "fr-fr")
    shadow = inventory.skills[0]
    assert shadow.languages == ("en-us",), "the hub said the skill has no French"
    assert shadow.intents[0].phrases_for("fr-fr") == ()


def test_examples_prefer_whole_sentences_and_respect_the_limit() -> None:
    weather = client(FakeHubTransport()).intents(["en-us"]).skills[1].intents[0]
    assert weather.examples("en-us", 2) == ("how is it outside", "what is the weather")
    assert weather.examples("en-us", 0) == weather.phrases_for("en-us")
    assert weather.examples(limit=1) == ("how is it outside",)


def test_every_registration_is_described_at_once_and_repeats_are_dropped() -> None:
    hub = FakeHubTransport(repeats=3)
    inventory = client(hub).intents(["en-us", "fr-fr"])

    describes = [(d["skill_id"], d["intent_name"], d["lang"]) for t, d, _ in hub.emitted
                 if t == "ovos.intent.describe"]
    assert len(describes) == 3 and len(set(describes)) == 3
    assert len(inventory.intents) == 2
    assert all(
        "request_id" in ctx for t, _, ctx in hub.emitted
        if t in ("ovos.intent.list", "ovos.intent.describe")
    ), "every query is correlated by request id"


def test_definitions_attached_to_the_listing_skip_the_describes() -> None:
    hub = FakeHubTransport(definitions_in_list=True)
    inventory = client(hub).intents(["fr-fr"])
    assert not any(t == "ovos.intent.describe" for t, _, _ in hub.emitted)
    assert inventory.intents[0].phrases_for("fr-fr")[0] == "quel temps fait-il"
    assert hub.emitted[0][1] == {"lang": "fr-fr", "include_definitions": True}


def test_a_refusal_is_an_error_naming_the_type_not_a_timeout() -> None:
    hub = FakeHubTransport(refuse=("ovos.intent.list",))
    with pytest.raises(ThalovantPolicyDeniedError) as caught:
        client(hub).intents(["en-us"], fallback=False, timeout=5.0)
    error = caught.value
    assert error.denied_type == "ovos.intent.list"
    assert error.code == "acl_disallowed_type"
    assert error.allowed == tuple(ALLOWED)
    assert "ovos.intent.list" in str(error) and "connection" in str(error)


def test_the_fallback_lists_names_and_says_what_was_refused() -> None:
    hub = FakeHubTransport(refuse=("ovos.intent.list",))
    inventory = client(hub).intents(["en-us", "fr-fr"])

    assert inventory.source == SOURCE_ENGINES
    assert inventory.denied == ("ovos.intent.list",)
    assert not inventory.has_phrases
    assert [intent.id for intent in inventory.intents] == [
        f"{SHADOW}:custos.incidents", f"{WEATHER}:current.weather",
    ]
    # Names carry no language, so the engines are asked once, not per language.
    assert sum(1 for t, _, _ in hub.emitted if t == "intent.service.padatious.manifest.get") == 1


def test_a_hub_refusing_everything_raises_even_with_the_fallback() -> None:
    hub = FakeHubTransport(refuse=("ovos.intent.list", "intent.service.adapt.manifest.get"))
    with pytest.raises(ThalovantPolicyDeniedError) as caught:
        client(hub).intents(["en-us"])
    assert caught.value.denied_type == "intent.service.adapt.manifest.get"


def test_a_silent_hub_times_out_on_the_listing() -> None:
    hub = FakeHubTransport(silent=("ovos.intent.list",))
    with pytest.raises(ThalovantTimeoutError, match="ovos.intent.list"):
        client(hub).intents(["en-us"], timeout=0.2)


def test_a_describe_that_never_comes_leaves_that_intent_without_sentences() -> None:
    class HalfDeaf(FakeHubTransport):
        def emit_event(self, event_type, data, context):
            if event_type == "ovos.intent.describe" and data["skill_id"] == SHADOW:
                self.emitted.append((event_type, dict(data), dict(context)))
                return
            super().emit_event(event_type, data, context)

    inventory = client(HalfDeaf()).intents(["en-us"], timeout=0.3)
    by_id = {intent.id: intent for intent in inventory.intents}
    assert by_id[f"{WEATHER}:current.weather"].phrases_for("en-us")
    assert by_id[f"{SHADOW}:custos.incidents"].phrases_for("en-us") == ()


def test_a_reply_without_a_request_id_is_still_taken() -> None:
    """A hub that does not echo the request id is not evidence of anything."""
    hub = FakeHubTransport(echo_request_id=False, repeats=1)
    inventory = client(hub).intents(["en-us"])
    assert inventory.has_phrases


def test_low_level_calls_expose_the_manifest_rows_and_definitions() -> None:
    hub = FakeHubTransport()
    rows = client(hub).list_intents("fr-fr")
    assert [(row.skill_id, row.intent_name, row.engine, row.enabled) for row in rows] == [
        (WEATHER, "current.weather", "padatious", True),
    ]
    assert same_language(rows[0].lang, "fr-fr")
    definitions = client(hub).describe_intent(WEATHER, "current.weather", "fr-fr")
    assert definitions[0].samples[0] == "quel temps fait-il"
    assert definitions[0].raw["blacklist"] == []
    assert client(hub).describe_intent(SHADOW, "custos.incidents", "fr-fr") == []


def test_as_dict_is_json_ready_and_complete() -> None:
    inventory = client(FakeHubTransport()).intents(["en-us", "fr-fr"])
    payload = inventory.as_dict()
    assert payload["source"] == SOURCE_MANIFEST and payload["languages"] == ["en-us", "fr-fr"]
    weather = next(s for s in payload["skills"] if s["skill_id"] == WEATHER)
    assert weather["languages"] == ["en-us", "fr-fr"]
    assert weather["intents"][0]["phrases"]["fr-fr"][0] == "quel temps fait-il"


def test_the_async_client_has_the_same_surface() -> None:
    async def scenario() -> None:
        hub = FakeHubTransport()
        async_client = AsyncThalovantClient(identity(), transport=hub, reply_settle_seconds=0)
        inventory = await async_client.intents(["en-us"])
        assert inventory.has_phrases
        rows = await async_client.list_intents("en-us")
        assert len(rows) == 2
        definitions = await async_client.describe_intent(WEATHER, "current.weather", "en-us")
        assert definitions[0].samples

    asyncio.run(scenario())


def test_languages_default_to_english() -> None:
    hub = FakeHubTransport()
    client(hub).intents()
    assert hub.emitted[0][1]["lang"] == "en-us"
    hub.emitted.clear()
    client(hub).intents([])
    assert hub.emitted[0][1]["lang"] == "en-us"
