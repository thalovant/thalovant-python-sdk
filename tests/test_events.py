"""Classification of terminal failure events.

Regression coverage for issue #22: OVOS renamed the "no intent matched" bus
event from the legacy Mycroft ``complete_intent_failure`` to
``ovos.intent.unmatched``. Both names must be treated as terminal so that
``ask()`` returns promptly with the failure set instead of waiting out its
full timeout.
"""

import pytest

from thalovant import ThalovantEvent
from thalovant.events import EVENT_INTENT_FAILURE, EVENT_INTENT_UNMATCHED


def _event(name: str) -> ThalovantEvent:
    return ThalovantEvent(name=name, data={}, context={}, raw=None)


@pytest.mark.parametrize(
    "name",
    [
        EVENT_INTENT_UNMATCHED,  # current OVOS name
        EVENT_INTENT_FAILURE,  # legacy Mycroft name, kept for older runtimes
    ],
)
def test_intent_unmatched_names_are_failures(name):
    assert _event(name).is_failure is True


def test_ovos_intent_unmatched_literal_is_a_failure():
    assert _event("ovos.intent.unmatched").is_failure is True


def test_legacy_complete_intent_failure_literal_is_a_failure():
    assert _event("complete_intent_failure").is_failure is True


def test_intent_matched_is_not_a_failure():
    assert _event("ovos.intent.matched").is_failure is False
