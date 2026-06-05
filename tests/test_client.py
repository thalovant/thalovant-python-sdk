from dataclasses import dataclass
from typing import Any, Callable

import pytest

from thalovant import ThalovantClient, ThalovantIdentity, ThalovantTimeoutError
from thalovant.client import _runtime_bus_context, _runtime_crypto_key


@dataclass
class FakeMessage:
    data: dict[str, Any]


class FakeTransport:
    def __init__(self, *, answer: str | None = "hello", handled: bool = True):
        self.answer = answer
        self.handled = handled
        self.connected = False
        self.emitted: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.handlers: dict[str, list[Callable[[Any], None]]] = {}

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

    def emit_event(
        self,
        event_type: str,
        data: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        self.emitted.append((event_type, data, context))
        if self.answer is not None:
            for handler in self.handlers.get("speak", []):
                handler(FakeMessage({"utterance": self.answer}))
        if self.handled:
            for handler in self.handlers.get("ovos.utterance.handled", []):
                handler(FakeMessage({}))


def identity() -> ThalovantIdentity:
    return ThalovantIdentity(
        access_key="key",
        password="password",
        crypto_key="crypto",
        site_id="site",
        default_master="http://hub.local",
        default_port=5679,
    )


def test_ask_emits_utterance_and_collects_speak():
    transport = FakeTransport(answer="The answer")
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0)

    reply = client.ask("what is up?", context={"source": "test"})

    assert reply.text == "The answer"
    assert reply.utterances == ("The answer",)
    assert reply.handled is True
    assert transport.emitted == [
        (
            "recognizer_loop:utterance",
            {"utterances": ["what is up?"], "lang": "en-us"},
            {"source": "test"},
        )
    ]
    assert transport.handlers["speak"] == []
    assert transport.handlers["ovos.utterance.handled"] == []


def test_emit_sends_low_level_event():
    transport = FakeTransport()
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0)

    client.emit("skillmanager.list", {"x": 1}, {"source": "test"})

    assert transport.emitted == [("skillmanager.list", {"x": 1}, {"source": "test"})]


def test_runtime_crypto_key_matches_hivemind_runtime_truncation():
    assert _runtime_crypto_key("  abcdefghijklmnopqrstuvwxyz  ") == "abcdefghijklmnop"
    assert _runtime_crypto_key("0123456789abcdef") == "0123456789abcdef"
    assert _runtime_crypto_key("   ") is None
    assert _runtime_crypto_key(None) is None


def test_runtime_bus_context_injects_non_default_session():
    context = _runtime_bus_context(
        {"session": {"session_id": "default", "pipeline": ["x"]}},
        useragent="SDK",
        session_id="runtime-session",
        site_id="Office",
    )

    assert context["source"] == "SDK"
    assert context["platform"] == "SDK"
    assert context["destination"] == "HiveMind"
    assert context["session"] == {
        "session_id": "runtime-session",
        "site_id": "Office",
        "pipeline": ["x"],
    }


def test_ask_times_out_without_handled_event():
    transport = FakeTransport(answer=None, handled=False)
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0)

    with pytest.raises(ThalovantTimeoutError):
        client.ask("hello", timeout=0.01)
