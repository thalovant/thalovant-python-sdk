"""What an ask does when the hub refuses it, against the shared vectors.

`contracts/conformance/refusal-vectors.json` is part of the parity reference,
so every SDK runs the same cases: a refusal becomes a typed error carrying the
hub's code and, for a spent quota, its numbers; an unmatched intent is an
unanswered question rather than a failure; and a denial with no request id is
taken only by the ask that can be the one it refused.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from thalovant import (
    ThalovantConnectionError,
    ThalovantPolicyDeniedError,
    ThalovantQuota,
    ThalovantTimeoutError,
    ThalovantUnansweredError,
)
from thalovant.events import (
    UNTRACKED_UTTERANCE_GRACE_SECONDS,
    ThalovantEvent,
    failure_error,
    refusal_belongs_to_ask,
)
from test_query_semantics import QueryTransport, client

CONFORMANCE = Path(__file__).resolve().parents[1] / "contracts" / "conformance"


def load_vectors(name: str) -> dict[str, Any]:
    return json.loads((CONFORMANCE / name).read_text(encoding="utf-8"))


VECTORS = load_vectors("refusal-vectors.json")


def _event(case: dict[str, Any]) -> ThalovantEvent:
    wire = case["event"]
    return ThalovantEvent(name=wire["type"], data=wire.get("data", {}), context=wire.get("context", {}), raw=wire)


@pytest.mark.parametrize("case", VECTORS["classification"], ids=lambda case: case["name"])
def test_a_failure_event_becomes_the_error_its_vector_names(case):
    error = failure_error(_event(case))
    expect = case["expect"]
    if expect["kind"] == "unanswered":
        assert isinstance(error, ThalovantUnansweredError), error
        # What the person said, which is what a caller shows.
        assert error.said == expect["said"]
        return
    assert expect["kind"] == "refused"
    assert isinstance(error, ThalovantPolicyDeniedError), error
    produced = {
        "kind": "refused",
        "denied_type": error.denied_type,
        "code": error.code,
        "reason": error.reason,
        "allowed": list(error.allowed),
        "quota": None if error.quota is None else {
            "period": error.quota.period,
            "limit": error.quota.limit,
            "used": error.quota.used,
            "reset_after": error.quota.reset_after,
        },
    }
    assert produced == expect


@pytest.mark.parametrize("case", VECTORS["correlation"], ids=lambda case: case["name"])
def test_a_denial_is_taken_only_by_the_ask_it_can_belong_to(case):
    request_id = {"own": "req-own", "other": "req-other", None: None}[case["request_id"]]
    assert refusal_belongs_to_ask(
        request_id=request_id,
        own_request_id="req-own",
        denied_type=case["denied_type"],
        asks_in_flight=case["asks_in_flight"],
        queries_in_flight=case["queries_in_flight"],
        sends_in_flight=case["sends_in_flight"],
    ) is case["taken"]


def test_the_grace_window_is_the_one_the_vectors_name():
    assert UNTRACKED_UTTERANCE_GRACE_SECONDS == VECTORS["untracked_grace_seconds"]


def test_the_vectors_cover_every_kind_of_refusal():
    """A vector set that quietly lost its quota or its unanswered case would still pass."""
    codes = {case["expect"].get("code") for case in VECTORS["classification"]}
    kinds = {case["expect"]["kind"] for case in VECTORS["classification"]}
    assert {"acl_disallowed_type", "intent_quota_exceeded", "backend_unavailable"} <= codes
    assert kinds == {"refused", "unanswered"}
    assert {True, False} == {case["taken"] for case in VECTORS["correlation"]}


def _denial(**data: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return "hive.policy.denied", {"denied_type": "recognizer_loop:utterance", **data}, {"source": "hivemind-core"}


def test_an_uncorrelated_quota_refusal_ends_the_ask_at_once_with_its_numbers():
    # The production shape: denied at once, no request id, the numbers nested.
    name, data, context = _denial(
        code="intent_quota_exceeded", reason="daily intent quota exceeded",
        data={"period": "daily", "limit": 50, "used": 50, "reset_after": 36120},
    )
    transport = QueryTransport(lambda t: t.bus(name, data, context=context))
    sdk = client(transport, settle=0.1)
    try:
        started = time.monotonic()
        with pytest.raises(ThalovantPolicyDeniedError) as refused:
            sdk.ask("what time is it", timeout=5.0)
        assert time.monotonic() - started < 2.0, "it waited out the deadline instead of taking the refusal"
        assert refused.value.quota == ThalovantQuota(period="daily", limit=50, used=50, reset_after=36120)
        # The advice fits the refusal: a spent day, not an allow-list to edit.
        assert "dashboard" not in str(refused.value)
    finally:
        sdk.close()


def test_an_unmatched_intent_is_an_unanswered_question_not_a_failure():
    def script(transport):
        transport.bus("ovos.intent.unmatched", {"utterance": "book me a flight to the moon"})

    transport = QueryTransport(script)
    sdk = client(transport, settle=0.05)
    try:
        with pytest.raises(ThalovantUnansweredError):
            sdk.ask("book me a flight to the moon", timeout=1.0)
    finally:
        sdk.close()


def test_with_two_asks_in_flight_an_uncorrelated_denial_fails_neither():
    # Either ask could be the one refused; ending the wrong one fails a
    # question the hub never refused, so both are left to their deadlines.
    # The fake runs its script inside each ask's send, after that ask has
    # reserved its id and subscribed -- so on the second send both are out.
    sends = []
    lock = threading.Lock()
    first_out = threading.Event()

    def script(transport):
        with lock:
            sends.append(None)
            count = len(sends)
        if count == 1:
            first_out.set()
            return
        name, data, context = _denial(code="intent_quota_exceeded", data={})
        transport.bus(name, data, context=context)

    transport = QueryTransport(script)
    sdk = client(transport, settle=0.05)
    outcomes: dict[str, BaseException | None] = {}

    def ask(label: str) -> None:
        try:
            sdk.ask(label, timeout=0.6)
            outcomes[label] = None
        except BaseException as error:  # noqa: BLE001 -- the type is the assertion
            outcomes[label] = error

    first = threading.Thread(target=ask, args=("first",))
    second = threading.Thread(target=ask, args=("second",))
    try:
        first.start()
        assert first_out.wait(timeout=2)
        second.start()
        first.join(timeout=5)
        second.join(timeout=5)
        assert len(sends) == 2
        for label in ("first", "second"):
            assert isinstance(outcomes.get(label), ThalovantTimeoutError), f"{label}: {outcomes.get(label)!r}"
    finally:
        sdk.close()


def test_an_ask_does_not_take_a_denial_a_fire_and_forget_send_could_own():
    # send_utterance() has no reply and no id, but the hub can refuse it, and
    # that refusal names only the type. Arriving while an ask waits, it could
    # be either message's -- so the ask is left to its own deadline.
    def script(transport):
        if transport.sent_utterances == 2:
            name, data, context = _denial(code="intent_quota_exceeded", data={})
            transport.bus(name, data, context=context)

    class CountingTransport(QueryTransport):
        sent_utterances = 0

        def emit_event(self, name, data, context):
            if name == "recognizer_loop:utterance":
                CountingTransport.sent_utterances += 1
            return super().emit_event(name, data, context)

    transport = CountingTransport(script)
    sdk = client(transport, settle=0.05)
    try:
        sdk.send_utterance("turn the lights off")
        with pytest.raises(ThalovantTimeoutError):
            sdk.ask("what time is it", timeout=0.4)
    finally:
        sdk.close()


def test_a_send_that_never_connected_is_not_in_flight():
    # A connect that fails publishes nothing, so there is nothing for the hub
    # to refuse -- and a phantom would suppress a real refusal for the whole
    # grace window.
    class Unreachable(QueryTransport):
        def connect(self):
            raise ThalovantConnectionError("no route to the hub")

    transport = Unreachable(lambda _: None)
    sdk = client(transport, settle=0)
    try:
        with pytest.raises(ThalovantConnectionError):
            sdk.send_utterance("turn the lights off")
        assert sdk._utterances_in_flight()["sends_in_flight"] == 0
    finally:
        sdk.close()


def test_a_publish_that_errored_still_counts_because_the_hub_may_hold_it():
    # The transport can fail after the hub already has the frame, and the hub
    # refuses what it holds. Forgetting the send would leave the next ask as
    # the only candidate for a denial that was never its own.
    class Lossy(QueryTransport):
        def emit_event(self, name, data, context):
            raise ThalovantConnectionError("the write reported a failure")

    transport = Lossy(lambda _: None)
    sdk = client(transport, settle=0)
    try:
        with pytest.raises(ThalovantConnectionError):
            sdk.send_utterance("turn the lights off")
        assert sdk._utterances_in_flight()["sends_in_flight"] == 1
    finally:
        sdk.close()
