"""What a voice client needs from the SDK, and used to build for itself.

Measured on the Custos appliance's satellite before these moved here: a
transport built by hand for one certificate flag, a request context assembled
by hand for the three hints the hub reads, a second subscription per request
for the sounds a skill embeds in its reply, and a pattern renderer for the
listing. Each is exercised here the way the satellite used it.
"""
from __future__ import annotations

from typing import Any

import pytest
from test_client import FakeMessage, FakeTransport, identity, identity_with_wss

from thalovant import (
    EVENT_AUDIO_QUEUE,
    ThalovantClient,
    ThalovantEvent,
    build_location,
    request_context,
    speakable,
)
from thalovant.events import MAX_AUDIO_CLIP_BYTES
from thalovant.intents import HubIntent


# -- the certificate flag -----------------------------------------------------

def test_the_certificate_flag_reaches_the_transport_without_building_one(monkeypatch):
    """The satellite built its own transport for one reason: the client
    factory passed no `self_signed`. Now the client takes it, and the default
    is still to check the certificate."""
    created: dict[str, Any] = {}

    class FakeWSS(FakeTransport):
        def __init__(self, ident, **kwargs):
            super().__init__()
            created.update(kwargs)

    monkeypatch.setattr("thalovant.client.HiveMindWSSTransport", FakeWSS)
    ThalovantClient(identity_with_wss(), protocol="wss")
    assert created["self_signed"] is False
    ThalovantClient(identity_with_wss(), protocol="wss", self_signed=True, noise_state_dir="/k")
    assert created["self_signed"] is True
    assert created["noise_state_dir"] == "/k"


# -- the hints a hub reads ------------------------------------------------------

def test_ask_carries_the_hints_a_hub_reads():
    transport = FakeTransport(answer="Il est midi.")
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0)
    where = build_location(city="Montréal", region="QC", country="ca",
                           latitude=45.5, longitude=-73.6, timezone="America/Toronto")

    client.ask("quelle heure est-il", lang="fr-fr", stt_lang="fr-fr",
               pipeline=("converse", " padatious_high", "", "fallback_high"), location=where)

    _, payload, context = transport.emitted[0]
    assert payload["lang"] == "fr-fr"
    # ovos-core reads stt_lang before request_lang and detected_lang.
    assert context["stt_lang"] == "fr-fr"
    assert context["session"]["pipeline"] == ["converse", "padatious_high", "fallback_high"]
    assert context["session"]["lang"] == "fr-fr"
    # Request level, not inside the session: that is what outranks the hub's
    # own configured place.
    assert context["location"] == {
        "city": "Montréal", "region": "QC", "country_code": "CA",
        "timezone": {"code": "America/Toronto"},
        "coordinate": {"latitude": 45.5, "longitude": -73.6},
    }


def test_ask_sends_nothing_extra_when_no_hint_is_given():
    transport = FakeTransport(answer="ok")
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0)
    client.ask("what is up?")
    _, _, context = transport.emitted[0]
    assert not {"stt_lang", "location"} & set(context)
    assert "pipeline" not in context["session"]


def test_a_location_needs_a_city_and_a_real_coordinate():
    """0,0 is what an unconfigured GPS reports, not where anybody lives."""
    assert build_location() is None
    assert build_location(city="  ") is None
    assert build_location(city="Paris", latitude=0, longitude=0) == {"city": "Paris"}
    assert build_location(city="Paris", latitude="x", longitude=2) == {"city": "Paris"}
    assert build_location(city="Paris", latitude=91, longitude=2) == {"city": "Paris"}
    assert build_location(city="Paris", latitude="48.8", longitude="2.3")["coordinate"] == {
        "latitude": 48.8, "longitude": 2.3}


def test_request_context_leaves_what_it_was_given_alone():
    assert request_context(None) is None
    assert request_context({"source": "kiosk"}) == {"source": "kiosk"}
    merged = request_context({"session": {"site_id": "s"}}, pipeline=["converse"], stt_lang=" en-us ")
    assert merged == {"session": {"site_id": "s", "pipeline": ["converse"]}, "stt_lang": "en-us"}
    assert request_context(None, pipeline=["", "  "]) is None


# -- skill sounds -----------------------------------------------------------------

class SoundingTransport(FakeTransport):
    """A hub whose skill speaks, plays a clip, and delivers the punchline."""

    def __init__(self, clip_hex: str = "52494646", *, repeat: bool = False,
                 foreign: bool = False):
        super().__init__(answer=None)
        self.clip_hex, self.repeat, self.foreign = clip_hex, repeat, foreign

    def emit_event(self, event_type, data, context):
        self.emitted.append((event_type, data, context))

        def push(name, payload, ctx=context, message=None):
            message = message or FakeMessage(payload, context=ctx, msg_type=name)
            for handler in self.handlers.get(name, []):
                handler(message)

        if self.foreign:  # somebody else's clip, on the same bus
            push(EVENT_AUDIO_QUEUE, {"binary_data": "ff"}, ctx={"request_id": "someone-else"})
        push("speak", {"utterance": "Pulling your finger.", "lang": "en-us"})
        clip = FakeMessage({"binary_data": self.clip_hex}, context=context, msg_type=EVENT_AUDIO_QUEUE)
        push(EVENT_AUDIO_QUEUE, {}, message=clip)
        if self.repeat:  # the bus can deliver one message object twice
            push(EVENT_AUDIO_QUEUE, {}, message=clip)
        push("speak", {"utterance": "Ha! Got you!"})
        push("ovos.utterance.handled", {})


def test_skill_sounds_arrive_in_order_with_the_speech():
    transport = SoundingTransport()
    # A skill's burst -- speech, clip, punchline -- lands within the settle
    # window; at zero the first speech would close collection on the rest.
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0.1)

    reply = client.ask("pull my finger")

    assert reply.text == "Pulling your finger. Ha! Got you!"
    assert [event.name for event in reply.media_events] == ["speak", EVENT_AUDIO_QUEUE, "speak"]
    assert reply.has_audio and reply.media_events[1].audio_bytes() == b"RIFF"
    assert reply.lang == "en-us"  # the first event that names one
    assert reply.dropped_media == 0
    assert reply.ok
    assert reply.as_dict()["lang"] == "en-us"
    assert transport.handlers[EVENT_AUDIO_QUEUE] == []  # unsubscribed with the rest


def test_a_clip_delivered_twice_is_kept_once_and_a_foreign_one_not_at_all():
    transport = SoundingTransport(repeat=True, foreign=True)
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0.1)
    reply = client.ask("pull my finger")
    assert [event.name for event in reply.media_events] == ["speak", EVENT_AUDIO_QUEUE, "speak"]


def test_an_oversized_clip_is_counted_and_left_out():
    transport = SoundingTransport(clip_hex="00" * (MAX_AUDIO_CLIP_BYTES + 1))
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0.1)
    reply = client.ask("pull my finger")
    assert not reply.has_audio
    assert reply.dropped_media == 1
    assert reply.text == "Pulling your finger. Ha! Got you!"


@pytest.mark.parametrize("data, name, reason", [
    ({}, EVENT_AUDIO_QUEUE, "no embedded audio"),
    ({"binary_data": ""}, EVENT_AUDIO_QUEUE, "no embedded audio"),
    ({"binary_data": "zz"}, EVENT_AUDIO_QUEUE, "not hexadecimal"),
    ({"binary_data": "00" * 3}, "speak", "no embedded audio"),
])
def test_audio_bytes_refuses_what_is_not_a_clip(data, name, reason):
    event = ThalovantEvent(name=name, data=data, context={}, raw=None)
    with pytest.raises(ValueError, match=reason):
        event.audio_bytes()


def test_audio_bytes_is_bounded_before_it_decodes():
    event = ThalovantEvent(name=EVENT_AUDIO_QUEUE, data={"binary_data": "00" * 4}, context={}, raw=None)
    assert event.has_audio and event.audio_bytes() == b"\x00\x00\x00\x00"
    with pytest.raises(ValueError, match="clip limit"):
        event.audio_bytes(max_bytes=3)


# -- a pattern read aloud ---------------------------------------------------------

@pytest.mark.parametrize("pattern, spoken", [
    ("did (i|we) (already |)ask", "did i ask"),
    ("(repeat|say) that (again|)", "repeat that"),
    ("did (i|we) ask (about|for|to|) {query}", "did i ask about the garage door"),
    ("mute it [for a (second|bit|minute)]", "mute it"),
    ("volume [level] [to] high [level]", "volume high"),
    ("volume [to] {level} percent", "volume fifty percent"),
    ("set the {gadget_name} going", "set the gadget name going"),
    ("[please]", ""),
    ("(already |)", ""),
    ("(yes|no|)", "yes"),
])
def test_a_pattern_is_printed_as_a_sentence(pattern, spoken):
    assert speakable(pattern, {"query": "the garage door", "level": "fifty"}) == spoken


def test_request_hints_copy_session_without_pipeline():
    base = {"session": {"session_id": "kept"}}
    result = request_context(base, stt_lang="fr")
    result["session"]["session_id"] = "changed"
    assert base["session"]["session_id"] == "kept"


def test_examples_can_be_rendered_speakable():
    intent = HubIntent(skill_id="s", name="n", engine="padatious", phrases={
        "en-us": ("[please] (repeat|say) that (again|)", "volume [to] {level} percent", "[please]"),
    })
    assert intent.examples("en-us", 0) == (
        "[please] (repeat|say) that (again|)", "volume [to] {level} percent", "[please]")
    assert intent.examples("en-us", 0, speakable=True, slots={"level": "fifty"}) == (
        "repeat that", "volume fifty percent")
    assert intent.examples("en-us", 1, speakable=True) == ("repeat that",)


@pytest.mark.parametrize("patterns, expected", [
    (("{query}", "what time is it"), "what time is it"),
    # Rendered the same from a slot pattern and from a literal: it is a whole
    # phrase, and the slot pattern behind it does not demote it.
    (("{query}", "query"), "query"),
    # Among whole phrases the fullest wins, not the shortest ("aqi" was the
    # one example the weather skill got to show).
    (("{query}", "say hello", "query"), "say hello"),
])
def test_speakable_examples_preserve_source_slot_priority(patterns, expected):
    intent = HubIntent(skill_id="s", name="n", engine="padatious",
                       phrases={"en-us": patterns})
    assert intent.examples("en-us", 1, speakable=True) == (expected,)


# -- the hub's session shape is not the client's to warn about ----------------

def test_the_upstream_location_deprecation_is_dropped_and_nothing_else_is(caplog):
    import logging

    from thalovant.transport import quiet_upstream_location_deprecation

    quiet_upstream_location_deprecation()
    quiet_upstream_location_deprecation()  # idempotent: one filter, not two
    logger = logging.getLogger("OVOS")
    assert sum(type(f).__name__ == "_QuietUpstreamLocationDeprecation" for f in logger.filters) == 1
    with caplog.at_level(logging.WARNING, logger="OVOS"):
        logger.warning("Deprecation version=3.0.0. Caller=hivemind_bus_client.protocol:853. "
                       "the nested mycroft.conf 'location' shape (city/coordinate/timezone) "
                       "on session.location is deprecated")
        logger.warning("something else the library has to say")
    assert [r.getMessage() for r in caplog.records] == ["something else the library has to say"]
