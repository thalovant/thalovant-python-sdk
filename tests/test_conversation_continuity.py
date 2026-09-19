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

import threading
import time
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


def test_a_hub_that_answers_under_its_own_id_still_continues():
    # A hub is free to answer under an id of its own: HiveMind NATs a declared
    # session to a per-connection identity and undoes it on the way out, and
    # older hubs substituted a uuid outright. The next turn can only look the
    # conversation up by the id it is about to send.
    transport = HubTransport([
        HubTurn(session={"session_id": "71048b7f-e7b0-4360", **_fart_handlers()}),
    ])
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0,
                             empty_reply_wait_seconds=0)

    client.ask("Fais un prout", lang="fr-FR", session_id="sat-1")
    client.ask("Encore un", lang="fr-FR", session_id="sat-1")

    assert transport.sent_sessions[1]["session_id"] == "sat-1"
    assert transport.sent_sessions[1]["converse_handlers"]


def test_a_caller_that_declares_no_session_still_continues():
    # Without a declared id the hub keeps the connection's own session, which
    # is stable for the life of the connection -- so there is a conversation
    # to carry even though neither side named it.
    transport = HubTransport([HubTurn(session=_fart_handlers())])
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0,
                             empty_reply_wait_seconds=0)

    client.ask("Fais un prout", lang="fr-FR")
    client.ask("Encore un", lang="fr-FR")

    assert transport.sent_sessions[1]["converse_handlers"]


def test_a_top_level_session_id_names_the_same_conversation():
    # A session id is accepted in three places: the ask() argument, the
    # context's session, and a top-level context["session_id"]. The turn is
    # filed under whatever the request resolves to, so the lookup has to
    # resolve it the same way or a top-level caller stores under one key and
    # reads under another -- carrying nothing, silently.
    transport = HubTransport([HubTurn(session=_fart_handlers())])
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0,
                             empty_reply_wait_seconds=0)

    client.ask("Fais un prout", lang="fr-FR", context={"session_id": "kitchen"})
    client.ask("Encore un", lang="fr-FR", context={"session_id": "kitchen"})

    assert list(client._conversations) == ["kitchen"]
    assert transport.sent_sessions[1]["converse_handlers"]


class NattingHubTransport(HubTransport):
    """A hub that answers under an id of its own, as HiveMind NATs one.

    HIVEMIND-BRIDGE-1 §4 maps a declared id to a per-connection identity and
    undoes it outbound; older hubs substituted a uuid outright.
    """

    def __init__(self, turns: list[HubTurn], answered_with: str):
        super().__init__(turns)
        self.answered_with = answered_with

    def emit_event(self, event_type, data, context):
        self.emitted.append((event_type, data, context))
        self.sent_sessions.append(dict(context.get("session") or {}))
        turn = self.turns.pop(0) if self.turns else HubTurn(session={})
        reply_context = dict(context)
        reply_context["session"] = {
            **(context.get("session") or {}), **turn.session,
            "session_id": self.answered_with,
        }
        for handler in self.handlers.get("speak", []):
            handler(FakeMessage({"utterance": "Pfffft."}, context=reply_context))
        for handler in self.handlers.get("ovos.utterance.handled", []):
            handler(FakeMessage({}, context=reply_context))


def test_the_carry_survives_whichever_session_id_the_caller_sends_back():
    """`reply.session_id` is the hub's, and a caller may well send it back.

    It is the first non-empty *event* session id, so when a hub answers under
    an id of its own the reply hands the caller an id the carry used to be
    filed under nothing. Both are remembered now: the request's, which is what
    a satellite reuses, and the hub's, which is what `reply.session_id` offers.
    """

    transport = NattingHubTransport([HubTurn(session=_fart_handlers())],
                                    answered_with="hub-namespace:sat-1")
    client = ThalovantClient(identity(), transport=transport)

    reply = client.ask("Fais un prout", session_id="sat-1")
    assert reply.session_id == "hub-namespace:sat-1"

    # The caller does the natural thing with what the reply handed back.
    client.ask("Encore un", session_id=reply.session_id)
    carried = transport.sent_sessions[-1]
    assert carried.get("converse_handlers"), carried

    # And the satellite's path -- reusing its own id -- still works.
    transport.turns.append(HubTurn(session=_fart_handlers()))
    client.ask("Encore un", session_id="sat-1")
    assert transport.sent_sessions[-1].get("converse_handlers"), transport.sent_sessions[-1]


class SplitIdHubTransport(HubTransport):
    """A hub whose speak and handled events disagree about the session id.

    `ThalovantReply.session_id` is the first non-blank id from *any* event, so
    the id the caller is handed comes from the speak here -- not from the
    handled event the carry is read out of.
    """

    def __init__(self, turns: list[HubTurn], speak_id: str, handled_id: str | None):
        super().__init__(turns)
        self.speak_id = speak_id
        self.handled_id = handled_id

    def emit_event(self, event_type, data, context):
        self.emitted.append((event_type, data, context))
        self.sent_sessions.append(dict(context.get("session") or {}))
        turn = self.turns.pop(0) if self.turns else HubTurn(session={})
        base = {**(context.get("session") or {}), **turn.session}
        for handler in self.handlers.get("speak", []):
            handler(FakeMessage({"utterance": "Pfffft."},
                                context={**context, "session": {**base, "session_id": self.speak_id}}))
        handled = dict(base)
        if self.handled_id is None:
            handled.pop("session_id", None)
        else:
            handled["session_id"] = self.handled_id
        for handler in self.handlers.get("ovos.utterance.handled", []):
            handler(FakeMessage({}, context={**context, "session": handled}))


def test_the_carry_is_filed_under_the_id_the_reply_actually_returns():
    """The speak decides `reply.session_id`; the handled event decides the carry.

    Filing only under the handled event's id returned an id nothing was filed
    under whenever the two disagreed, and the next turn sent no carried state.
    """

    for handled_id in ("hub-handled", None):
        transport = SplitIdHubTransport([HubTurn(session=_fart_handlers())],
                                        speak_id="hub-speak", handled_id=handled_id)
        client = ThalovantClient(identity(), transport=transport)

        reply = client.ask("Fais un prout", session_id="sat-1")
        assert reply.session_id == "hub-speak", (handled_id, reply.session_id)

        transport.turns.append(HubTurn(session=_fart_handlers()))
        client.ask("Encore un", session_id=reply.session_id)
        carried = transport.sent_sessions[-1]
        assert carried.get("converse_handlers"), (handled_id, carried)


def test_a_translated_conversation_holds_one_place_in_the_bound():
    """Two ids reaching one conversation are one entry, evicted together.

    Filed separately they aged and were evicted separately, so a caller using
    the evicted alias lost the carry while one using its partner kept it -- and
    the bound counted names rather than conversations, so a NAT-translated
    client remembered half as many.
    """

    client = ThalovantClient(identity(), transport=HubTransport([]))
    cap = client.MAX_REMEMBERED_CONVERSATIONS
    handlers = _fart_handlers()

    for index in range(cap):
        client._remember_conversation([f"sat-{index}", f"hub:sat-{index}"], handlers)

    # `cap` conversations under 2 * cap names, and every name still resolves.
    assert client._remembered_conversations() == cap
    assert len(client._conversations) == 2 * cap
    for index in range(cap):
        for name in (f"sat-{index}", f"hub:sat-{index}"):
            assert client._continue_conversation(None, name), name

    # One more evicts the oldest conversation -- both of its names, together.
    client._remember_conversation(["sat-new", "hub:sat-new"], handlers)
    assert client._remembered_conversations() == cap
    assert client._continue_conversation(None, "sat-0") is None
    assert client._continue_conversation(None, "hub:sat-0") is None
    assert client._continue_conversation(None, "sat-1")
    assert client._continue_conversation(None, "hub:sat-1")


class LateHandledHubTransport(HubTransport):
    """A hub whose `ovos.utterance.handled` arrives after the reply settled."""

    def __init__(self, turns: list[HubTurn], delay: float = 0.05):
        super().__init__(turns)
        self.delay = delay
        self.threads: list[threading.Thread] = []
        # Set once the late handled has actually been delivered. Waiting on the
        # thread list instead is a race: the send runs on a worker, so ask() can
        # return before the thread is even in the list, and joining an empty
        # list waits for nothing -- the next turn then goes out before the hub
        # has said what the conversation is.
        self.delivered = threading.Event()

    def emit_event(self, event_type, data, context):
        self.emitted.append((event_type, data, context))
        self.sent_sessions.append(dict(context.get("session") or {}))
        turn = self.turns.pop(0) if self.turns else HubTurn(session={})
        reply_context = dict(context)
        reply_context["session"] = {**(context.get("session") or {}), **turn.session}
        for handler in self.handlers.get("speak", []):
            handler(FakeMessage({"utterance": "Pfffft."}, context=reply_context))

        def _late() -> None:
            time.sleep(self.delay)
            for handler in self.handlers.get("ovos.utterance.handled", []):
                handler(FakeMessage({}, context=reply_context))
            self.delivered.set()

        thread = threading.Thread(target=_late, daemon=True)
        thread.start()
        self.threads.append(thread)


def test_a_handled_event_that_arrives_after_the_reply_still_records_the_carry():
    """A zero settle window finishes the reply before the hub says what changed.

    Dropping the subscription there lost the carry entirely: the next turn sent
    no `converse_handlers` and the follow-up reached the fallback.
    """

    transport = LateHandledHubTransport([HubTurn(session=_fart_handlers())])
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0.0, empty_reply_wait_seconds=0.0)

    client.ask("Fais un prout", session_id="sat-1")
    assert transport.delivered.wait(5), "the hub never said what the conversation was"

    transport.turns.append(HubTurn(session=_fart_handlers()))
    client.ask("Encore un", session_id="sat-1")
    assert transport.sent_sessions[-1].get("converse_handlers"), transport.sent_sessions[-1]
