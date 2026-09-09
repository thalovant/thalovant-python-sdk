"""Direct query behavior through the public client and synthetic transports."""

import threading
import time
from types import SimpleNamespace

import pytest

from thalovant import (
    ThalovantClient,
    ThalovantConnectionError,
    ThalovantIdentity,
    ThalovantRuntimeError,
    ThalovantTimeoutError,
)


class QueryTransport:
    def __init__(self, script=lambda transport: None):
        self.connected = False
        self.handlers = {"query": [], "cascade": []}
        self.script = script
        self.dials = 0
        self.sent = 0
        self.bus_handlers = {}

    def connect(self):
        self.dials += 1
        self.connected = True

    def disconnect(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    def last_error(self):
        return None

    def on_hive_message(self, kind, handler):
        self.handlers[kind].append(handler)

    def remove_hive_message(self, kind, handler):
        self.handlers[kind].remove(handler)

    def send_hive_message(self, frame, *, encrypt):
        assert encrypt
        self.sent += 1
        self.frame = frame
        self.script(self)

    def on_mycroft(self, name, handler):
        self.bus_handlers.setdefault(name, []).append(handler)

    def remove_mycroft(self, name, handler):
        self.bus_handlers[name].remove(handler)

    def emit_event(self, name, data, context):
        self.sent += 1
        self.script(self)

    def bus(self, name, data=None, context=None):
        for handler in tuple(self.bus_handlers.get(name, [])):
            handler(SimpleNamespace(msg_type=name, data=data or {}, context=context or {}))

    def reply(self, event, text=None, *, channel="query", query_id="fixture"):
        frame = {
            "msg_type": channel,
            "metadata": {"query_id": query_id},
            "payload": {"msg_type": "bus", "payload": {
                "type": event,
                "data": {"utterance": text} if text else {},
                "context": {},
            }},
        }
        for handler in tuple(self.handlers[channel]):
            handler(frame)


def client(transport, *, settle=0):
    identity = ThalovantIdentity(
        access_key="fixture", password="fixture", default_master="https://fixture.invalid", site_id="fixture",
    )
    return ThalovantClient(identity, transport=transport, reply_settle_seconds=settle)


@pytest.mark.parametrize("channel", ["query", "cascade"])
@pytest.mark.parametrize("miss", ["complete_intent_failure", "ovos.intent.unmatched"])
def test_query_soft_miss_recovers_with_later_speech_and_correlation(channel, miss):
    def script(transport):
        transport.reply("hive.policy.denied", query_id="foreign", channel=channel)
        transport.reply(miss, channel=channel)
        transport.reply("speak", "answer", channel=channel)
        transport.reply(miss, channel=channel)
        transport.reply("hive.query.complete", channel=channel)
        transport.reply("speak", "too late", channel=channel)

    transport = QueryTransport(script)
    sdk = client(transport)
    try:
        reply = sdk.query("hello", query_id="fixture", request_id="request", session_id="session")
        assert reply.text == "answer"
        assert reply.handled and reply.failure_event is None
        assert reply.request_id == "request" and reply.session_id == "session"
        assert [event.name for event in reply.events] == [miss, "speak", miss, "hive.query.complete"]
        assert all(not handlers for handlers in transport.handlers.values())
    finally:
        sdk.close()


@pytest.mark.parametrize("miss", ["complete_intent_failure", "ovos.intent.unmatched"])
def test_query_unrecovered_soft_miss_requires_completion(miss):
    transport = QueryTransport(lambda transport: transport.reply(miss))
    sdk = client(transport)
    try:
        with pytest.raises(ThalovantTimeoutError):
            sdk.query("hello", timeout=0.02, query_id="fixture")
        transport.script = lambda transport: (
            transport.reply(miss), transport.reply("hive.query.complete"),
        )
        with pytest.raises(ThalovantRuntimeError):
            sdk.query("hello", timeout=1, query_id="fixture")
    finally:
        sdk.close()


@pytest.mark.parametrize("terminal", ["hive.policy.denied", "hive.query.timeout"])
@pytest.mark.parametrize("partial", [False, True])
def test_hard_query_failure_freezes_partial_reply(terminal, partial):
    def script(transport):
        if partial:
            transport.reply("speak", "partial")
        transport.reply(terminal)
        transport.reply("speak", "forbidden late fragment")
        transport.reply("hive.query.complete")

    transport = QueryTransport(script)
    sdk = client(transport, settle=10)
    started = time.monotonic()
    try:
        if partial:
            reply = sdk.query("hello", timeout=1, query_id="fixture")
            assert reply.text == "partial" and not reply.handled
            assert reply.failure_event.name == terminal
            assert [event.name for event in reply.events] == ["speak", terminal]
        else:
            with pytest.raises(ThalovantRuntimeError):
                sdk.query("hello", timeout=1, query_id="fixture")
        assert time.monotonic() - started < 0.5
        assert all(not handlers for handlers in transport.handlers.values())
    finally:
        sdk.close()


@pytest.mark.parametrize("terminal", ["hive.query.complete", "hive.policy.denied"])
def test_terminal_reply_precedes_later_send_failure(terminal):
    def script(transport):
        transport.reply("speak", "terminal answer")
        transport.reply(terminal)
        raise RuntimeError("write rejected after terminal reply")

    sdk = client(QueryTransport(script))
    try:
        reply = sdk.query("hello", timeout=1, query_id="fixture")
        assert reply.text == "terminal answer"
        assert reply.handled == (terminal == "hive.query.complete")
    finally:
        sdk.close()


def test_query_settle_is_capped_by_original_deadline():
    transport = QueryTransport(lambda transport: (
        transport.reply("speak", "answer"), transport.reply("hive.query.complete"),
    ))
    sdk = client(transport, settle=10)
    started = time.monotonic()
    try:
        assert sdk.query("hello", timeout=0.03, query_id="fixture").text == "answer"
        assert time.monotonic() - started < 0.3
    finally:
        sdk.close()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_query_budget_performs_no_io(timeout):
    transport = QueryTransport()
    with pytest.raises(ThalovantTimeoutError):
        client(transport).query("hello", timeout=timeout)
    assert transport.dials == 0 and transport.sent == 0


@pytest.mark.parametrize("stage", ["connect", "send"])
@pytest.mark.parametrize("late_failure", [False, True])
def test_query_deadline_retains_blocked_work_and_cleanup_before_reuse(stage, late_failure):
    class HeldTransport(QueryTransport):
        def __init__(self):
            super().__init__()
            self.gate = threading.Event()
            self.cleanup_gate = threading.Event()
            self.cleanup_started = threading.Event()
            self.ready_before_replacement = None

        def connect(self):
            self.dials += 1
            if self.dials == 1 and stage == "connect":
                self.gate.wait(5)
                if late_failure:
                    raise RuntimeError("late connect failure")
            if self.dials > 1:
                self.ready_before_replacement = self.connected
            self.connected = True

        def send_hive_message(self, frame, *, encrypt):
            self.sent += 1
            if self.sent == 1 and stage == "send":
                self.gate.wait(5)
                if late_failure:
                    raise RuntimeError("late send failure")
            self.reply("speak", "answer")
            self.reply("hive.query.complete")

        def disconnect(self):
            self.cleanup_started.set()
            self.cleanup_gate.wait(5)
            self.connected = False

    transport = HeldTransport()
    sdk = client(transport)
    try:
        started = time.monotonic()
        with pytest.raises(ThalovantTimeoutError):
            sdk.query("hello", timeout=0.02, query_id="fixture")
        assert time.monotonic() - started < 0.3
        assert transport.cleanup_started.wait(0.5)
        assert all(not handlers for handlers in transport.handlers.values())
        with pytest.raises(ThalovantConnectionError):
            sdk.connect(timeout=0.02)
        assert transport.dials == 1
        transport.gate.set()
        with pytest.raises(ThalovantConnectionError):
            sdk.connect(timeout=0.02)
        assert transport.dials == 1, "pending cleanup must retain session ownership"
        assert transport.sent == (1 if stage == "send" else 0), "expired connect must never send later"
        transport.cleanup_gate.set()
        sdk.connect(timeout=1)
        assert transport.dials == 2 and transport.ready_before_replacement is False
        assert sdk.query("hello", timeout=1, query_id="fixture").text == "answer"
        assert all(not handlers for handlers in transport.handlers.values())
    finally:
        transport.gate.set()
        transport.cleanup_gate.set()
        sdk.close()
