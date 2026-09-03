"""A hub substitutes its own session id; the request id is what correlates.

Observed against a live hub on 2026-09-03: a client that declares
``session_id="observe-me"`` gets every reply back carrying
``session_id="71048b7f-e7b0-4360-8fb5-a03816f78617"`` -- the hub's own id, not
the declared one and not a derivative of it. Comparing session ids therefore
rejected replies the request id had already identified as ours, and ``ask()``
waited out its whole timeout while the hub had answered and emitted
``ovos.utterance.handled``. Verified across a matched skill, an unmatched
fallback and a French utterance: the request id is present and equal on every
``speak`` and every ``handled`` event in all three.
"""

import pytest

from thalovant import ThalovantEvent
from thalovant.events import _event_matches_context


def _event(session_id=None, request_id=None):
    context = {}
    if session_id is not None:
        context["session"] = {"session_id": session_id}
    if request_id is not None:
        context["request_id"] = request_id
    return ThalovantEvent(
        name="ovos.utterance.handled", data={}, context=context, raw=None
    )


def _asked(session_id=None, request_id=None):
    asked = {}
    if session_id is not None:
        asked["session"] = {"session_id": session_id}
    if request_id is not None:
        asked["request_id"] = request_id
    return asked


# -- the regression ----------------------------------------------------------

def test_a_matching_request_id_wins_over_a_substituted_session():
    """The exact shape a live hub returns."""
    event = _event(session_id="71048b7f-e7b0-4360-8fb5-a03816f78617", request_id="req-1")
    assert _event_matches_context(event, _asked(session_id="observe-me", request_id="req-1"))


def test_a_wrong_request_id_is_rejected_even_if_sessions_agree():
    """The point of correlating must survive the fix."""
    event = _event(session_id="same", request_id="req-2")
    assert not _event_matches_context(event, _asked(session_id="same", request_id="req-1"))


def test_a_reply_without_a_request_id_falls_back_to_the_session():
    """Deliberately lenient, and unchanged by this fix.

    A reply carrying no request id is not evidence of anything either way, and
    hubs and test fakes exist that never send one. Rejecting it here would
    break them for no gain, so the session check still decides.
    """
    unlabelled = _event(session_id="s1", request_id=None)
    assert _event_matches_context(unlabelled, _asked(session_id="s1", request_id="req-1"))
    assert not _event_matches_context(
        _event(session_id="other", request_id=None), _asked(session_id="s1", request_id="req-1")
    )


# -- the pre-request-id fallback still behaves --------------------------------

def test_without_request_ids_the_session_still_decides():
    assert _event_matches_context(_event(session_id="s1"), _asked(session_id="s1"))
    assert not _event_matches_context(_event(session_id="s2"), _asked(session_id="s1"))


def test_asking_for_nothing_accepts_anything():
    assert _event_matches_context(_event(session_id="x", request_id="y"), None)
    assert _event_matches_context(_event(session_id="x", request_id="y"), {})


@pytest.mark.parametrize("session_id", ["observe-me", "nonce:observe-me", None])
def test_the_session_id_never_vetoes_a_matching_request(session_id):
    """Whatever a hub does to the session id -- echo it, namespace it, or
    replace it -- a matching request id is authoritative."""
    event = _event(session_id=session_id, request_id="req-1")
    assert _event_matches_context(event, _asked(session_id="observe-me", request_id="req-1"))
