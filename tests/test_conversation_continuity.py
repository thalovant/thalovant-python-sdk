"""A follow-up reaches the skill that answered the turn before.

A hub keeps nothing between the turns of a *named* session: OVOS-SESSION-2
§2.2 makes the orchestrator stateless for those, so the carrier a client sends
is the whole snapshot and whatever the last turn activated is discarded the
moment it ends. The client is the only thing that remembers.

Measured against the production hub (ovos-core v1.3.0, 2026-09-15): "Fais un
prout" was claimed by the fart skill, and "Encore un" one minute later was not
-- it went to the fallback, because the second utterance arrived with an empty
converse list and the converse pipeline had nobody to poll. Sending the same
session back makes the hub answer it with `skill.converse.response` from the
fart skill instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from thalovant.client import ThalovantClient
from thalovant.events import CONVERSATION_SESSION_FIELDS, carry_conversation
from thalovant.identity import ThalovantIdentity

from test_client import FakeMessage, FakeTransport


def identity() -> ThalovantIdentity:
    return ThalovantIdentity(
        access_key="key", password="password", site_id="site",
        default_master="http://hub.local", default_port=5679,
    )


@dataclass
class HubTurn:
    """What the hub reports the session to be once a turn has been handled."""

    session: dict[str, Any]


class HubTransport(FakeTransport):
    """Answers each ask with the session state a real hub hands back.

    The shapes are the ones an ovos-core v1.3.0 hub actually emitted on
    ``ovos.utterance.handled`` -- captured from the runtime bus, not invented.
    """

    def __init__(self, turns: list[HubTurn]):
        super().__init__(answer=None, handled=False)
        self.turns = list(turns)
        self.sent_sessions: list[dict[str, Any]] = []

    def emit_event(self, event_type, data, context):
        self.emitted.append((event_type, data, context))
        self.sent_sessions.append(dict(context.get("session") or {}))
        turn = self.turns.pop(0) if self.turns else HubTurn(session={})
        reply_context = dict(context)
        reply_context["session"] = {**(context.get("session") or {}), **turn.session}
        # The skill speaks its punchline over the sound it queued; a reply with
        # no speech at all is a separate case this fixture is not about.
        for handler in self.handlers.get("speak", []):
            handler(FakeMessage({"utterance": "Pfffft."}, context=reply_context))
        for handler in self.handlers.get("ovos.utterance.handled", []):
            handler(FakeMessage({}, context=reply_context))


def _fart_handlers(at: float = 1789500246.72) -> dict[str, Any]:
    return {
        "converse_handlers": [{"skill_id": "thalovant-skill-fart.thalovant",
                               "activated_at": at}],
        "active_handlers": [{"skill_id": "thalovant-skill-fart.thalovant",
                             "activated_at": at}],
        "active_skills": [["thalovant-skill-fart.thalovant", at]],
    }


def test_the_next_utterance_carries_what_the_last_one_activated():
    transport = HubTransport([HubTurn(session=_fart_handlers())])
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0,
                             empty_reply_wait_seconds=0)

    client.ask("Fais un prout", lang="fr-FR", session_id="sat-1")
    client.ask("Encore un", lang="fr-FR", session_id="sat-1")

    first, second = transport.sent_sessions
    assert "converse_handlers" not in first
    assert second["converse_handlers"] == [
        {"skill_id": "thalovant-skill-fart.thalovant", "activated_at": 1789500246.72}
    ]
    assert second["active_handlers"] == second["converse_handlers"]
    assert second["active_skills"] == [["thalovant-skill-fart.thalovant", 1789500246.72]]


def test_the_language_this_turn_heard_outranks_the_last_one():
    # The satellite decides the language per utterance, by transcribing in it.
    # A remembered `lang` would quietly pin the conversation to whichever
    # language it opened in -- the bilingual case this fleet is built for.
    transport = HubTransport([HubTurn(session={"lang": "fr-FR", **_fart_handlers()})])
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0,
                             empty_reply_wait_seconds=0)

    client.ask("Fais un prout", lang="fr-FR", session_id="sat-1")
    client.ask("do another one", lang="en-US", session_id="sat-1")

    assert transport.sent_sessions[1]["lang"] == "en-US"


def test_live_device_state_is_not_replayed():
    # `is_speaking` describes a moment that has passed by the time the next
    # utterance is sent; handing it back asserts something untrue about now.
    transport = HubTransport([
        HubTurn(session={"is_speaking": True, "is_recording": True, **_fart_handlers()}),
    ])
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0,
                             empty_reply_wait_seconds=0)

    client.ask("Fais un prout", lang="fr-FR", session_id="sat-1")
    client.ask("Encore un", lang="fr-FR", session_id="sat-1")

    assert "is_speaking" not in transport.sent_sessions[1]
    assert "is_recording" not in transport.sent_sessions[1]


def test_a_turn_that_ends_with_nothing_active_clears_the_memory():
    # A skill deactivating is state, not an absence of it: keeping the old
    # entry would put the conversation back after it ended.
    transport = HubTransport([
        HubTurn(session=_fart_handlers()),
        HubTurn(session={"converse_handlers": [], "active_handlers": [],
                         "active_skills": []}),
    ])
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0,
                             empty_reply_wait_seconds=0)

    client.ask("Fais un prout", lang="fr-FR", session_id="sat-1")
    client.ask("Encore un", lang="fr-FR", session_id="sat-1")
    client.ask("Quelle heure est-il", lang="fr-FR", session_id="sat-1")

    assert "converse_handlers" in transport.sent_sessions[1]
    assert "converse_handlers" not in transport.sent_sessions[2]


def test_conversations_do_not_bleed_across_session_ids():
    transport = HubTransport([HubTurn(session=_fart_handlers())])
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0,
                             empty_reply_wait_seconds=0)

    client.ask("Fais un prout", lang="fr-FR", session_id="kitchen")
    client.ask("Encore un", lang="fr-FR", session_id="bedroom")

    assert "converse_handlers" not in transport.sent_sessions[1]


def test_the_number_of_remembered_conversations_is_bounded():
    cap = ThalovantClient.MAX_REMEMBERED_CONVERSATIONS
    transport = HubTransport([HubTurn(session=_fart_handlers()) for _ in range(cap + 1)])
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0,
                             empty_reply_wait_seconds=0)

    for index in range(cap + 1):
        client.ask("Fais un prout", lang="fr-FR", session_id=f"sat-{index}")

    assert len(client._conversations) == cap
    assert "sat-0" not in client._conversations


def test_this_turn_keeps_its_own_values():
    carried = carry_conversation(
        {"converse_handlers": [{"skill_id": "old", "activated_at": 1.0}]},
        {"converse_handlers": [{"skill_id": "new", "activated_at": 2.0}]},
    )
    assert carried["converse_handlers"] == [{"skill_id": "new", "activated_at": 2.0}]


def test_only_the_conversation_fields_travel():
    carried = carry_conversation(
        {field: [{"skill_id": "x", "activated_at": 1.0}]
         for field in CONVERSATION_SESSION_FIELDS} | {"pipeline": ["a"], "site_id": "other"},
        {"session_id": "sat-1"},
    )
    assert set(carried) == {"session_id", *CONVERSATION_SESSION_FIELDS}
