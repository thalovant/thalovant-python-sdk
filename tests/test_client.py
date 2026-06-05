import asyncio
from dataclasses import dataclass
import threading
import time
from typing import Any, Callable

import pytest

from thalovant import (
    AsyncThalovantClient,
    ThalovantClient,
    ThalovantConnectionError,
    ThalovantHealth,
    ThalovantIdentity,
    ThalovantTimeoutError,
)
from thalovant.client import _runtime_bus_context, _runtime_crypto_key


@dataclass
class FakeMessage:
    data: dict[str, Any]
    context: dict[str, Any] | None = None
    msg_type: str | None = None


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

    def push(
        self,
        event_name: str,
        data: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        for handler in self.handlers.get(event_name, []):
            handler(FakeMessage(data or {}, context=context or {}, msg_type=event_name))

    def healthcheck(self) -> ThalovantHealth:
        return ThalovantHealth(
            connected=self.connected,
            handshake_complete=self.connected,
            transport_alive=self.connected,
        )

    def is_connected(self) -> bool:
        return self.connected

    def last_error(self) -> BaseException | None:
        return None


class FlakyTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_emit = True
        self.connect_count = 0

    def connect(self) -> None:
        self.connect_count += 1
        super().connect()

    def emit_event(
        self,
        event_type: str,
        data: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        if self.fail_next_emit:
            self.fail_next_emit = False
            self.connected = False
            raise ThalovantConnectionError("lost connection")
        super().emit_event(event_type, data, context)


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


def test_on_receives_normalized_events_and_unsubscribes():
    transport = FakeTransport()
    client = ThalovantClient(identity(), transport=transport)
    events = []

    subscription = client.on("custom.event", events.append)
    transport.push("custom.event", {"value": 1}, {"source": "hub"})
    subscription.close()
    transport.push("custom.event", {"value": 2}, {"source": "hub"})

    assert len(events) == 1
    assert events[0].name == "custom.event"
    assert events[0].data == {"value": 1}
    assert events[0].context == {"source": "hub"}
    assert transport.handlers["custom.event"] == []


def test_wait_for_event_blocks_until_predicate_matches():
    transport = FakeTransport()
    client = ThalovantClient(identity(), transport=transport)

    def publish() -> None:
        time.sleep(0.02)
        transport.push("custom.event", {"value": 1})
        transport.push("custom.event", {"value": 2})

    thread = threading.Thread(target=publish)
    thread.start()
    event = client.wait_for_event(
        "custom.event",
        timeout=1,
        predicate=lambda candidate: candidate.data.get("value") == 2,
    )
    thread.join(timeout=1)

    assert event.data == {"value": 2}


def test_listen_yields_until_max_events():
    transport = FakeTransport()
    client = ThalovantClient(identity(), transport=transport)

    def publish() -> None:
        time.sleep(0.02)
        transport.push("custom.event", {"value": 1})
        transport.push("custom.event", {"value": 2})

    thread = threading.Thread(target=publish)
    thread.start()
    events = list(client.listen("custom.event", timeout=1, max_events=2))
    thread.join(timeout=1)

    assert [event.data["value"] for event in events] == [1, 2]


def test_healthcheck_returns_transport_state():
    transport = FakeTransport()
    client = ThalovantClient(identity(), transport=transport)

    health = client.healthcheck()

    assert health.ok
    assert health.connected


def test_emit_reconnects_once_after_transport_failure():
    transport = FlakyTransport()
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0)

    client.emit("skillmanager.list", {"x": 1})

    assert transport.connect_count == 2
    assert transport.emitted == [("skillmanager.list", {"x": 1}, {})]


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


def test_async_client_supports_ask():
    async def run() -> str:
        transport = FakeTransport(answer="async hello")
        client = AsyncThalovantClient(
            identity(),
            transport=transport,
            reply_settle_seconds=0,
        )
        reply = await client.ask("hello")
        await client.close()
        return reply.text

    assert asyncio.run(run()) == "async hello"


def test_async_client_supports_event_handlers():
    async def run() -> list[int]:
        transport = FakeTransport()
        client = AsyncThalovantClient(identity(), transport=transport)
        await client.connect()
        values: list[int] = []

        async def handler(event):
            values.append(event.data["value"])

        subscription = client.on("custom.event", handler)
        transport.push("custom.event", {"value": 7})
        await asyncio.sleep(0.02)
        subscription.close()
        await client.close()
        return values

    assert asyncio.run(run()) == [7]


def test_async_client_supports_listen():
    async def run() -> list[int]:
        transport = FakeTransport()
        client = AsyncThalovantClient(identity(), transport=transport)

        async def publish() -> None:
            await asyncio.sleep(0.02)
            transport.push("custom.event", {"value": 1})
            transport.push("custom.event", {"value": 2})

        asyncio.create_task(publish())
        values = [
            event.data["value"]
            async for event in client.listen("custom.event", timeout=1, max_events=2)
        ]
        await client.close()
        return values

    assert asyncio.run(run()) == [1, 2]
