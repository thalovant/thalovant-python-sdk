"""A hub rewrites a declared session id; replies must still be recognised.

hivemind-core derives a Layer-1 identity for every client-declared session as
``f"{conn_nonce}:{declared}"`` so two clients cannot collide on the same name
(HIVEMIND-BRIDGE-1 §4). Comparing the returned id to the sent one for equality
rejected every reply: ``ask()`` timed out while the hub had already answered
and emitted ``ovos.utterance.handled``. Reproduced against a live hub on
2026-09-03 -- 480 ms with no session id, a full timeout with one.
"""

import pytest

from thalovant import ThalovantEvent
from thalovant.events import _event_matches_context, _session_ids_match


def _event(session_id):
    return ThalovantEvent(
        name="ovos.utterance.handled",
        data={},
        context={"session": {"session_id": session_id}},
        raw=None,
    )


def _asked(session_id):
    return {"session": {"session_id": session_id}}


# -- the regression itself ----------------------------------------------------

def test_a_nat_rewritten_reply_is_recognised():
    """The exact shape a non-admin client gets back."""
    assert _event_matches_context(_event("d41d8cd98f00b204:my-session"), _asked("my-session"))


def test_an_unrewritten_reply_is_still_recognised():
    """Admin connections skip the NAT, so the id comes back untouched."""
    assert _event_matches_context(_event("my-session"), _asked("my-session"))


def test_a_reply_for_a_different_session_is_still_rejected():
    """The point of the check must survive the fix."""
    assert not _event_matches_context(_event("nonce:other-session"), _asked("my-session"))
    assert not _event_matches_context(_event("other-session"), _asked("my-session"))


def test_no_session_asked_accepts_anything():
    """Omitting the id is how every caller worked around this; keep it working."""
    assert _event_matches_context(_event("nonce:whatever"), None)
    assert _event_matches_context(_event("nonce:whatever"), {})


# -- the suffix rule is a prefix-strip, not a bare endswith -------------------

@pytest.mark.parametrize(
    "actual, expected, match",
    [
        ("nonce:abc", "abc", True),
        ("abc", "abc", True),
        # a bare endswith would wrongly accept these
        ("nonce:xabc", "abc", False),
        ("nonce:abc:def", "abc", False),
        # the declared half may itself contain colons only if it matches whole
        ("nonce:a:b", "a:b", True),
        ("", "abc", False),
        ("nonce:", "abc", False),
    ],
)
def test_only_the_declared_half_after_the_first_colon_matches(actual, expected, match):
    assert _session_ids_match(expected, actual) is match


def test_a_session_id_is_never_confused_with_a_request_id():
    """Both are compared; a matching session must not excuse a wrong request."""
    event = ThalovantEvent(
        name="ovos.utterance.handled",
        data={},
        context={"session": {"session_id": "nonce:mine"}, "request_id": "req-2"},
        raw=None,
    )
    asked = {"session": {"session_id": "mine"}, "request_id": "req-1"}
    assert not _event_matches_context(event, asked)
