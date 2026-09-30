"""A reply says which stage answered, and whether anybody claimed it.

The hub stamps each reply message with the pipeline stage that matched; the
fallback stage answers whatever nothing else matched. A satellite that opened
its microphone without a wake word needs that difference: a fallback answer
to a fragment the room produced is not a conversation to keep open.
"""

from __future__ import annotations

from thalovant.events import ThalovantEvent
from thalovant.models import THALOVANT_CLAIMED_META_KEY, ThalovantReply


def _event(
    name: str,
    pipeline: str | None,
    skill: str | None,
    meta: dict | None = None,
) -> ThalovantEvent:
    context = {}
    if pipeline is not None:
        context["pipeline_id"] = pipeline
    if skill is not None:
        context["skill_id"] = skill
    data: dict = {"utterance": "x"}
    if meta is not None:
        data["meta"] = meta
    return ThalovantEvent(name=name, data=data, context=context, raw=None)


def test_an_intent_match_is_claimed():
    reply = ThalovantReply(
        text="Il fait 22 degrés.",
        handled=True,
        events=(
            _event(
                "speak",
                "ovos-padatious-pipeline-plugin",
                "thalovant-skill-weather.thalovant",
            ),
            _event(
                "ovos.utterance.handled",
                "ovos-padatious-pipeline-plugin",
                "thalovant-skill-weather.thalovant",
            ),
        ),
    )
    assert reply.claimed is True
    assert reply.pipeline_ids == ("ovos-padatious-pipeline-plugin",)
    assert reply.skill_ids == ("thalovant-skill-weather.thalovant",)


def test_a_fallback_answer_is_not_a_claim():
    reply = ThalovantReply(
        text="Je ne peux pas répondre à cela.",
        handled=True,
        events=(
            _event(
                "speak",
                "ovos-fallback-pipeline-plugin",
                "thalovant-skill-custos-fallback.thalovant",
            ),
            _event(
                "ovos.utterance.handled",
                "ovos-fallback-pipeline-plugin",
                "thalovant-skill-custos-fallback.thalovant",
            ),
        ),
    )
    assert reply.claimed is False
    assert reply.ok is True, "handled, and honestly so: the fallback did answer"
    assert reply.skill_ids == ("thalovant-skill-custos-fallback.thalovant",)


def test_a_skill_that_answered_after_a_fallback_stage_still_counts():
    """Converse and fallback stages can both stamp messages in one reply."""
    reply = ThalovantReply(
        text="Done.",
        handled=True,
        events=(
            _event("speak", "ovos-fallback-pipeline-plugin", "a.skill"),
            _event("speak", "ovos-converse-pipeline-plugin", "b.skill"),
        ),
    )
    assert reply.claimed is True
    assert reply.pipeline_ids == (
        "ovos-fallback-pipeline-plugin",
        "ovos-converse-pipeline-plugin",
    )
    assert reply.skill_ids == ("a.skill", "b.skill")


def test_a_hub_that_stamps_nothing_is_taken_at_its_word():
    reply = ThalovantReply(
        text="ok", handled=True, events=(_event("speak", None, None),)
    )
    assert reply.claimed is True
    assert reply.pipeline_ids == ()
    assert reply.skill_ids == ()


def test_a_failed_or_unhandled_reply_is_never_claimed():
    failure = _event("ovos.utterance.timeout", None, None)
    assert ThalovantReply(text="", handled=False).claimed is False
    assert ThalovantReply(text="", handled=True, failure_event=failure).claimed is False


def test_the_dictionary_form_carries_the_claim():
    reply = ThalovantReply(
        text="hi",
        handled=True,
        events=(_event("speak", "ovos-adapt-pipeline-plugin", "hello.skill"),),
    )
    d = reply.as_dict()
    assert d["claimed"] is True
    assert d["pipeline_ids"] == ["ovos-adapt-pipeline-plugin"]
    assert d["skill_ids"] == ["hello.skill"]


def test_non_string_stamps_do_not_turn_a_fallback_into_a_claim():
    reply = ThalovantReply(text="fallback text", handled=True, events=(
        ThalovantEvent("speak", {}, {"pipeline_id": 123, "skill_id": ["forged"]}, None),
        _event("speak", "fallback", "real.skill"),
    ))
    assert reply.pipeline_ids == ("fallback",)
    assert reply.skill_ids == ("real.skill",)
    assert reply.claimed is False
    assert reply.text == "fallback text"
    assert reply.ok is True


def test_a_skill_can_assert_a_genuine_claim_from_the_fallback_tier():
    """thalovant-skill-home's real ("turn the light off") answer, positively marked."""
    reply = ThalovantReply(
        text="D'accord, la lumière du bureau est éteinte.",
        handled=True,
        events=(
            _event(
                "speak",
                "ovos-fallback-pipeline-plugin",
                "thalovant-skill-home.thalovant",
                meta={THALOVANT_CLAIMED_META_KEY: True},
            ),
        ),
    )
    assert reply.claimed is True
    assert reply.pipeline_ids == ("ovos-fallback-pipeline-plugin",)


def test_the_fleets_generic_catch_all_is_still_not_a_claim():
    """Regression: a real fallback-tier skill_id with no assertion stays unclaimed."""
    reply = ThalovantReply(
        text="Je ne peux pas répondre à cela.",
        handled=True,
        events=(
            _event(
                "speak",
                "ovos-fallback-pipeline-plugin",
                "thalovant-skill-custos-fallback.thalovant",
            ),
        ),
    )
    assert reply.claimed is False


def test_the_assertion_does_not_affect_a_non_fallback_reply():
    reply = ThalovantReply(
        text="Il fait 22 degrés.",
        handled=True,
        events=(
            _event(
                "speak",
                "ovos-padatious-pipeline-plugin",
                "thalovant-skill-weather.thalovant",
                meta={THALOVANT_CLAIMED_META_KEY: True},
            ),
        ),
    )
    assert reply.claimed is True


def test_the_assertion_cannot_rescue_a_failed_reply():
    failure = _event("ovos.utterance.timeout", None, None)
    reply = ThalovantReply(
        text="",
        handled=True,
        failure_event=failure,
        events=(
            _event(
                "speak",
                "ovos-fallback-pipeline-plugin",
                "thalovant-skill-home.thalovant",
                meta={THALOVANT_CLAIMED_META_KEY: True},
            ),
        ),
    )
    assert reply.claimed is False


def test_only_a_literal_true_asserts_the_claim():
    for bad_meta in (
        {THALOVANT_CLAIMED_META_KEY: False},
        {THALOVANT_CLAIMED_META_KEY: "true"},
        {THALOVANT_CLAIMED_META_KEY: 1},
        {"unrelated": "value"},
        {},
    ):
        reply = ThalovantReply(
            text="Je ne peux pas répondre à cela.",
            handled=True,
            events=(
                _event(
                    "speak",
                    "ovos-fallback-pipeline-plugin",
                    "thalovant-skill-custos-fallback.thalovant",
                    meta=bad_meta,
                ),
            ),
        )
        assert reply.claimed is False, f"meta={bad_meta!r} must not assert a claim"


def test_only_the_skills_own_speak_event_can_assert_the_claim():
    """A correlated non-speak event -- ovos.utterance.handled, say -- carrying
    the same meta shape must not assert a claim on the skill's behalf. Only
    the skill's own speak_to()/emit_speech() call, on a speak-type event, can
    set this: nothing else the hub happens to stamp alongside it counts."""
    reply = ThalovantReply(
        text="Je ne peux pas répondre à cela.",
        handled=True,
        events=(
            _event(
                "speak",
                "ovos-fallback-pipeline-plugin",
                "thalovant-skill-custos-fallback.thalovant",
            ),
            _event(
                "ovos.utterance.handled",
                "ovos-fallback-pipeline-plugin",
                "thalovant-skill-custos-fallback.thalovant",
                meta={THALOVANT_CLAIMED_META_KEY: True},
            ),
        ),
    )
    assert reply.claimed is False
