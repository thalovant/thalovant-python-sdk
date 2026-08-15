import asyncio
from dataclasses import dataclass
import json
import threading
import time
from typing import Any, Callable

import pytest

from thalovant import (
    AsyncThalovantClient,
    EVENT_SPEAK,
    HubDataPlaneEndpoints,
    HubProtocolSettings,
    MqttBrokerCredentials,
    ThalovantAgent,
    ThalovantClient,
    ThalovantConnectionInfo,
    ThalovantConnectionError,
    ThalovantHealth,
    ThalovantIdentity,
    ThalovantTimeoutError,
    ThalovantUnsupportedProtocolError,
    build_client_context,
)
from thalovant.client import _runtime_bus_context, _runtime_crypto_key
from thalovant.transport import HiveMindHTTPTransport, HiveMindWSSTransport
from thalovant.transport import _mqtt_default_port, _mqtt_tls_enabled
from thalovant.transport import mqtt_topics_for_identity


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
        self.hive_handlers: dict[str, list[Callable[[Any], None]]] = {}
        self.hive_messages: list[dict[str, Any]] = []

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

    def on_hive_message(self, msg_type: str, handler: Callable[[Any], None]) -> None:
        self.hive_handlers.setdefault(msg_type, []).append(handler)

    def remove_hive_message(self, msg_type: str, handler: Callable[[Any], None]) -> None:
        self.hive_handlers[msg_type] = [
            entry for entry in self.hive_handlers.get(msg_type, []) if entry is not handler
        ]

    def send_hive_message(self, message: dict[str, Any], *, encrypt: bool = True) -> None:
        self.hive_messages.append(message)

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
        connection = self.connection_info()
        return ThalovantHealth(
            connected=self.connected,
            handshake_complete=self.connected,
            transport_alive=self.connected,
            connection=connection,
        )

    def connection_info(self) -> ThalovantConnectionInfo:
        return ThalovantConnectionInfo(phase="ready" if self.connected else "idle")

    def is_connected(self) -> bool:
        return self.connected

    def last_error(self) -> BaseException | None:
        return None


class QueryTransport(FakeTransport):
    def send_hive_message(self, message: dict[str, Any], *, encrypt: bool = True) -> None:
        super().send_hive_message(message, encrypt=encrypt)
        query_id = message["metadata"]["query_id"]
        context = message["payload"]["payload"]["context"]
        for handler in tuple(self.hive_handlers.get("query", [])):
            handler(
                {
                    "msg_type": "query",
                    "metadata": {"query_id": query_id},
                    "payload": {
                        "msg_type": "bus",
                        "payload": {
                            "type": "speak",
                            "data": {"utterance": "direct answer"},
                            "context": context,
                        },
                    },
                }
            )
            handler(
                {
                    "msg_type": "query",
                    "metadata": {"query_id": query_id},
                    "payload": {
                        "type": "hive.query.complete",
                        "data": {},
                        "context": context,
                    },
                }
            )


class HangingTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.disconnect_count = 0
        self.started = threading.Event()

    def connect(self) -> None:
        self.started.set()
        threading.Event().wait(timeout=10)

    def disconnect(self) -> None:
        self.disconnect_count += 1
        super().disconnect()


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


def identity_with_wss() -> ThalovantIdentity:
    return ThalovantIdentity(
        access_key="key",
        password="password",
        crypto_key="crypto",
        site_id="site",
        default_master="https://hub.local",
        default_port=443,
        data_plane_endpoints=HubDataPlaneEndpoints(
            https="https://hub.local/hivemind/public",
            wss="wss://hub.local/hivemind/public",
        ),
        protocols=HubProtocolSettings(wss=True, http=True),
    )


def identity_with_mqtt() -> ThalovantIdentity:
    return ThalovantIdentity(
        access_key="key",
        password="password",
        crypto_key="0123456789abcdef",
        site_id="site",
        default_master="https://hub.local",
        default_port=443,
        data_plane_endpoints=HubDataPlaneEndpoints(
            https="https://hub.local/hivemind/public",
            wss="wss://hub.local/hivemind/public",
            mqtt="mqtts://mqtt.example.com:8883",
        ),
        protocols=HubProtocolSettings(wss=True, http=True, mqtt=True),
        mqtt=MqttBrokerCredentials(
            endpoint="mqtts://mqtt.example.com:8883",
            username="key",
            password="broker-password",
            topic_prefix="hivemind/hub/key",
        ),
    )


def test_ask_emits_utterance_and_collects_speak():
    transport = FakeTransport(answer="The answer")
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0)

    reply = client.ask("what is up?", context={"source": "test"})

    assert reply.text == "The answer"
    assert reply.utterances == ("The answer",)
    assert reply.handled is True
    assert reply.ok is True
    assert reply.request_id
    assert len(transport.emitted) == 1
    event_type, payload, context = transport.emitted[0]
    assert event_type == "recognizer_loop:utterance"
    assert payload == {"utterances": ["what is up?"], "lang": "en-us"}
    assert context["source"] == "test"
    assert context["request_id"] == reply.request_id
    assert context["thalovant_request_id"] == reply.request_id
    assert context["session"] == {
        "site_id": "site",
        "lang": "en-us",
        "request_id": reply.request_id,
    }
    assert transport.handlers["speak"] == []
    assert transport.handlers["ovos.utterance.handled"] == []


def test_ask_includes_identity_metadata():
    transport = FakeTransport(answer="The answer")
    sdk_identity = ThalovantIdentity(
        access_key="key",
        password="password",
        crypto_key="crypto",
        site_id="site",
        default_master="http://hub.local",
        default_port=5679,
        metadata={"thalovant_owner_id": "owner-1", "plan": "paid"},
    )
    client = ThalovantClient(sdk_identity, transport=transport, reply_settle_seconds=0)

    client.ask("what is up?", context={"metadata": {"channel": "test"}})

    _, _, context = transport.emitted[0]
    assert context["metadata"] == {
        "thalovant_owner_id": "owner-1",
        "plan": "paid",
        "channel": "test",
    }


def test_query_uses_direct_hivemind_query_frame():
    transport = QueryTransport(answer=None)
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0)

    reply = client.query("what is up?", session_id="query-session")

    assert reply.text == "direct answer"
    assert reply.request_id
    assert reply.session_id == "query-session"
    assert len(transport.hive_messages) == 1
    frame = transport.hive_messages[0]
    assert frame["msg_type"] == "query"
    assert frame["metadata"]["query_id"] == reply.request_id
    inner = frame["payload"]
    assert inner["msg_type"] == "bus"
    assert inner["payload"]["type"] == "recognizer_loop:utterance"
    assert inner["payload"]["data"] == {"utterances": ["what is up?"], "lang": "en-us"}
    assert inner["payload"]["context"]["session"]["session_id"] == "query-session"
    assert transport.hive_handlers["query"] == []
    assert transport.hive_handlers["cascade"] == []


def test_connect_enforces_hard_timeout_and_disconnects_transport():
    transport = HangingTransport()
    client = ThalovantClient(identity(), transport=transport)

    with pytest.raises(ThalovantConnectionError, match="did not complete"):
        client.connect(timeout=0.02)

    assert transport.started.is_set()
    assert transport.disconnect_count == 1


def test_identity_preserves_metadata_from_mapping():
    sdk_identity = ThalovantIdentity.from_mapping(
        {
            "access_key": "key",
            "password": "password",
            "site_id": "site",
            "default_master": "https://hub.local",
            "metadata": {"thalovant_owner_id": "owner-1"},
        }
    )

    assert sdk_identity.metadata == {"thalovant_owner_id": "owner-1"}
    assert sdk_identity.as_dict()["metadata"] == {"thalovant_owner_id": "owner-1"}


def test_emit_sends_low_level_event():
    transport = FakeTransport()
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0)

    client.emit("skillmanager.list", {"x": 1}, {"source": "test"})

    assert transport.emitted == [("skillmanager.list", {"x": 1}, {"source": "test"})]


def test_connect_with_info_returns_transport_connection_snapshot():
    transport = FakeTransport()
    client = ThalovantClient(identity(), transport=transport)

    info = client.connect_with_info()

    assert info.phase == "ready"
    assert client.connection_info().phase == "ready"
    assert client.healthcheck().connection == info


def test_async_connect_with_info_returns_transport_connection_snapshot():
    async def run() -> None:
        transport = FakeTransport()
        client = AsyncThalovantClient(identity(), transport=transport)

        info = await client.connect_with_info()

        assert info.phase == "ready"
        assert (await client.connection_info()).phase == "ready"

    asyncio.run(run())


def test_client_rejects_unsupported_runtime_protocol_without_custom_transport():
    with pytest.raises(ThalovantUnsupportedProtocolError, match="MQTT"):
        ThalovantClient(identity(), protocol="mqtt")


def test_client_uses_mqtt_transport(monkeypatch):
    created: dict[str, Any] = {}

    class FakeMQTTTransport(FakeTransport):
        def __init__(self, identity: ThalovantIdentity, **kwargs: Any):
            super().__init__()
            created["identity"] = identity
            created["kwargs"] = kwargs

    monkeypatch.setattr("thalovant.client.HiveMindMQTTTransport", FakeMQTTTransport)

    client = ThalovantClient(identity_with_mqtt(), protocol="mqtt")
    client.connect()

    assert created["identity"].mqtt is not None
    assert created["kwargs"]["useragent"].startswith("ThalovantPythonSDK/")
    assert mqtt_topics_for_identity(identity_with_mqtt()) == (
        "hivemind/hub/c2s/key",
        "hivemind/hub/s2c/key",
        "hivemind/hub/status/key",
    )


def test_client_prefers_wss_when_no_protocol_is_forced():
    client = ThalovantClient(identity_with_wss())

    assert isinstance(client._transport, HiveMindWSSTransport)


def test_client_falls_back_to_https_when_wss_endpoint_is_missing():
    client = ThalovantClient(identity())

    assert isinstance(client._transport, HiveMindHTTPTransport)


def test_mqtt_topics_include_hub_id_when_prefix_is_generic():
    identity = ThalovantIdentity(
        access_key="key",
        password="password",
        crypto_key="0123456789abcdef",
        site_id="site",
        default_master="https://hub.local",
        default_port=443,
        protocols=HubProtocolSettings(wss=True, http=True, mqtt=True),
        mqtt=MqttBrokerCredentials(
            endpoint="mqtts://mqtt.example.com:8883",
            username="key",
            password="broker-password",
            topic_prefix="hivemind",
            hub_id="hub",
        ),
    )

    assert mqtt_topics_for_identity(identity) == (
        "hivemind/hub/c2s/key",
        "hivemind/hub/s2c/key",
        "hivemind/hub/status/key",
    )


def test_mqtt_tls_flag_controls_default_port():
    credentials = MqttBrokerCredentials(
        endpoint="mqtt://mqtt.example.com",
        username="key",
        password="broker-password",
        topic_prefix="hivemind/hub",
        tls=True,
    )

    assert _mqtt_tls_enabled(credentials, "mqtt") is True
    assert _mqtt_default_port(tls_enabled=True) == 8883
    assert _mqtt_default_port(tls_enabled=False) == 1883


def test_client_uses_wss_transport(monkeypatch):
    created: dict[str, Any] = {}

    class FakeWSSTransport(FakeTransport):
        def __init__(self, identity: ThalovantIdentity, **kwargs: Any):
            super().__init__()
            created["identity"] = identity
            created["kwargs"] = kwargs

    monkeypatch.setattr("thalovant.client.HiveMindWSSTransport", FakeWSSTransport)

    client = ThalovantClient(identity_with_wss(), protocol="wss")
    client.connect()

    assert created["identity"].endpoint_for("wss") == "wss://hub.local/hivemind/public"
    assert created["kwargs"]["useragent"].startswith("ThalovantPythonSDK/")


def test_wss_transport_authorized_url_preserves_endpoint_path_and_query():
    transport = HiveMindWSSTransport(
        ThalovantIdentity(
            access_key="key",
            password="password",
            site_id="site",
            default_master="https://hub.local",
            default_port=443,
            data_plane_endpoints=HubDataPlaneEndpoints(
                wss="wss://hub.local/hivemind/public?x=1&authorization=old"
            ),
            protocols=HubProtocolSettings(wss=True),
        ),
        useragent="ua",
    )

    assert (
        transport._authorized_wss_url(key="key", useragent="ua")
        == "wss://hub.local/hivemind/public?x=1&authorization=dWE6a2V5"
    )


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
    assert events[0].text == ""
    assert transport.handlers["custom.event"] == []


def test_on_filters_by_session_when_event_context_is_available():
    transport = FakeTransport()
    client = ThalovantClient(identity(), transport=transport)
    events = []

    subscription = client.on("custom.event", events.append, session_id="wanted")
    transport.push("custom.event", {"value": 1}, {"session": {"session_id": "other"}})
    transport.push("custom.event", {"value": 2}, {"session": {"session_id": "wanted"}})
    subscription.close()

    assert [event.data["value"] for event in events] == [2]


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


def test_send_utterance_adds_correlation_context():
    transport = FakeTransport()
    client = ThalovantClient(identity(), transport=transport)

    client.send_utterance("hello there", session_id="session-1", request_id="request-1")

    event_type, payload, context = transport.emitted[0]
    assert event_type == "recognizer_loop:utterance"
    assert payload == {"utterances": ["hello there"], "lang": "en-us"}
    assert context["request_id"] == "request-1"
    assert context["session"]["session_id"] == "session-1"
    assert context["session"]["site_id"] == "site"


def test_send_action_preserves_payload_metadata():
    transport = FakeTransport()
    client = ThalovantClient(identity(), transport=transport)

    client.send_action('/choose{"id":"42"}', title="Choose item", session_id="session-1")

    event_type, payload, context = transport.emitted[0]
    assert event_type == "recognizer_loop:utterance"
    assert payload["utterances"] == ['/choose{"id":"42"}']
    assert context["input"] == {
        "kind": "action",
        "title": "Choose item",
        "payload": '/choose{"id":"42"}',
    }
    assert context["session"]["session_id"] == "session-1"


def test_send_code_marks_exact_machine_input():
    transport = FakeTransport()
    client = ThalovantClient(identity(), transport=transport)

    client.send_code("SN-001-XYZ", kind="qr", label="serial", request_id="request-1")

    _, payload, context = transport.emitted[0]
    expected = {"kind": "qr", "label": "serial", "value": "SN-001-XYZ", "exact": True}
    assert payload["utterances"] == ["SN-001-XYZ"]
    assert payload["input"] == expected
    assert context["input"] == expected
    assert context["request_id"] == "request-1"


def test_build_client_context_is_provider_neutral():
    context = build_client_context(
        user_id="u-1",
        user_name="Ada",
        auth_token="token",
        auth_provider="oidc",
        roles=["operator"],
        platform="mobile",
        source="device-1",
        destination="hive_mind",
        channel="chat",
        device_id="phone-1",
        metadata={"shift": "night"},
    )

    assert context["user"] == {"id": "u-1", "name": "Ada", "roles": ["operator"]}
    assert context["auth"] == {"token": "token", "provider": "oidc"}
    assert context["device"] == {"id": "phone-1", "platform": "mobile"}
    assert context["metadata"] == {"shift": "night"}


def test_conversation_reuses_session_context():
    transport = FakeTransport(answer="conversation hello")
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0)

    with client.conversation(session_id="conversation-1", context={"source": "test"}) as convo:
        reply = convo.ask("hello", request_id="request-1")

    assert reply.text == "conversation hello"
    assert reply.session_id == "conversation-1"
    assert reply.request_id == "request-1"
    _, _, context = transport.emitted[0]
    assert context["source"] == "test"
    assert context["session"]["session_id"] == "conversation-1"
    assert context["session"]["request_id"] == "request-1"


def test_healthcheck_returns_transport_state():
    transport = FakeTransport()
    client = ThalovantClient(identity(), transport=transport)

    health = client.healthcheck()

    assert health.ok
    assert health.connected


def test_transport_error_strings_redact_url_query():
    """Stored and surfaced error text must not carry the data-plane access key,
    which requests-style errors embed as ``?authorization=...`` in the URL."""

    leaky = ConnectionError(
        "HTTPSConnectionPool(host='hub.local', port=5679): Max retries exceeded "
        "with url: /connect?authorization=QWdlbnQ6c2VjcmV0LWtleQ== "
        "(Caused by NewConnectionError)"
    )
    transport = HiveMindHTTPTransport(identity(), useragent="agent")
    transport._begin_connection()
    transport._fail_connection(leaky)

    health = transport.healthcheck()
    surfaced = json.dumps(health.as_dict())
    assert "QWdlbnQ6c2VjcmV0LWtleQ" not in surfaced
    assert "?<redacted>" in health.last_error
    assert "?<redacted>" in health.connection.last_error
    assert "hub.local" in health.last_error  # the useful part survives
    assert transport.last_error() is leaky  # the raw exception object is untouched

    wss = HiveMindWSSTransport(identity(), useragent="agent")
    wss._last_error = leaky
    assert "QWdlbnQ6c2VjcmV0LWtleQ" not in json.dumps(wss.healthcheck().as_dict())


def test_transport_stopped_error_message_redacts_url_query():
    leaky = ConnectionError("boom with url: /send_message?authorization=U0VDUkVU x")

    class StoppedTransport(FakeTransport):
        def is_connected(self) -> bool:
            return False

        def last_error(self) -> BaseException | None:
            return leaky

    client = ThalovantClient(identity(), transport=StoppedTransport())

    with pytest.raises(ThalovantConnectionError) as excinfo:
        client._raise_if_transport_stopped()

    assert "U0VDUkVU" not in str(excinfo.value)
    assert "authorization=" not in str(excinfo.value)
    assert "?<redacted>" in str(excinfo.value)


def test_doctor_reports_identity_and_transport_checks():
    transport = FakeTransport()
    client = ThalovantClient(identity(), transport=transport)

    report = client.doctor()

    assert report.ok
    assert report.identity == {
        "site_id": "site",
        "default_master": "http://hub.local",
        "default_port": 5679,
        "default_path": "",
    }
    assert [check.name for check in report.checks] == [
        "identity",
        "endpoint",
        "connect",
        "transport",
    ]


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


def test_runtime_bus_context_preserves_explicit_session():
    context = _runtime_bus_context(
        {"session": {"session_id": "conversation-1"}},
        useragent="SDK",
        session_id="runtime-session",
        site_id="Office",
    )

    assert context["session"]["session_id"] == "conversation-1"


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


def test_async_client_supports_conversation():
    async def run() -> tuple[str, str | None]:
        transport = FakeTransport(answer="async conversation")
        client = AsyncThalovantClient(
            identity(),
            transport=transport,
            reply_settle_seconds=0,
        )
        async with client.conversation(session_id="async-session") as convo:
            reply = await convo.ask("hello")
        await client.close()
        return reply.text, reply.session_id

    assert asyncio.run(run()) == ("async conversation", "async-session")


def test_rich_reply_display_items_include_text_table_image_and_choices():
    transport = FakeTransport(answer=None)
    client = ThalovantClient(identity(), transport=transport, reply_settle_seconds=0)

    def emit_event(event_type, data, context):
        transport.emitted.append((event_type, data, context))
        rich = {
            "table": '[{"name":"part","status":"ok"}]',
            "attachment": {"type": "image", "payload": {"src": "https://example.com/image.png"}},
            "quick_replies": [{"title": "Continue", "payload": "/continue"}],
        }
        for handler in transport.handlers.get("speak", []):
            handler(
                FakeMessage(
                    {
                        "utterance": "<speak>Hello</speak>",
                        "rich_media_data": json.dumps(rich),
                    },
                    context=context,
                    msg_type="speak",
                )
            )
        for handler in transport.handlers.get("ovos.utterance.handled", []):
            handler(FakeMessage({}, context=context, msg_type="ovos.utterance.handled"))

    transport.emit_event = emit_event
    reply = client.ask("show rich output")

    assert reply.display_text == "Hello"
    items = reply.display_items()
    assert [item.kind for item in items] == ["text", "table", "image", "choices"]
    assert items[0].text == "Hello"
    assert items[2].url == "https://example.com/image.png"
    assert items[3].data[0]["payload"] == "/continue"


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


def test_agent_runs_registered_handler_until_stopped():
    transport = FakeTransport()
    agent = ThalovantAgent(identity(), transport=transport)
    values = []

    @agent.on_speak
    def handle_speak(event):
        values.append(event.text)
        agent.stop()

    thread = threading.Thread(target=lambda: agent.run_forever(poll_interval=0.01))
    thread.start()
    deadline = time.monotonic() + 1
    while not transport.handlers.get(EVENT_SPEAK) and time.monotonic() < deadline:
        time.sleep(0.01)
    transport.push(EVENT_SPEAK, {"utterance": "hello agent"})
    thread.join(timeout=1)

    assert values == ["hello agent"]
    assert not thread.is_alive()
