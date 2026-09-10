"""Deterministic connection ownership races; no socket or credential access."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import queue
import threading
from types import SimpleNamespace

import pytest

from thalovant import ThalovantIdentity, ThalovantConnectionError
from thalovant.transport import HiveMindHTTPTransport, HiveMindMQTTTransport


def identity():
    return ThalovantIdentity.from_mapping({
        "access_key": "lifecycle-fixture", "password": "lifecycle-fixture", "site_id": "fixture",
        "default_master": "https://fixture.test", "default_port": 443,
        "mqtt": {"endpoint": "mqtts://fixture.test:8883", "username": "fixture", "password": "fixture",
                 "tls": True, "topic_prefix": "hivemind/test/client"},
    })


class Channel:
    def __init__(self, *args, **kwargs):
        self.ready = False
        self.received = []
        self.closes = 0

    def receive(self, value):
        self.received.append(value)

    def close(self):
        self.closes += 1


class Broker:
    def __init__(self, *args, **kwargs):
        self.published = []
        self.disconnects = 0
        self.stops = 0

    def publish(self, *args, **kwargs):
        self.published.append(args)
        return SimpleNamespace(wait_for_publish=lambda **kwargs: None, is_published=lambda: True)

    def disconnect(self):
        self.disconnects += 1

    def loop_stop(self):
        self.stops += 1

    def is_connected(self):
        return True


def mqtt():
    return HiveMindMQTTTransport(identity(), useragent="fixture", send_timeout=0.05)


def test_late_mqtt_callbacks_cannot_change_reconnected_session():
    transport, old, current = mqtt(), Broker(), Broker()
    channel = Channel()
    transport._client, transport._noise = current, channel
    for event in (transport._connected, transport._subscribed, transport._handshake):
        event.set()
    transport._on_disconnect(old, None, None, 1)
    assert all(event.is_set() for event in (transport._connected, transport._subscribed, transport._handshake))
    assert channel.closes == 0 and transport.last_error() is None
    transport._connected.clear(); transport._subscribed.clear()
    transport._on_connect(old, None, None, 0)
    transport._on_subscribe(old, None, 1, [0])
    assert not transport._connected.is_set() and not transport._subscribed.is_set()


def test_mqtt_disconnect_callback_never_waits_for_noise_send_lock():
    transport, client = mqtt(), Broker()
    entered, release = threading.Event(), threading.Event()

    class LockedChannel(Channel):
        def close(self):
            entered.set()
            assert release.wait(1), "network callback tried to take the channel lock"

    transport._client, transport._noise = client, LockedChannel()
    transport._connected.set(); transport._handshake.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        callback = pool.submit(transport._on_disconnect, client, None, None, 1)
        try:
            callback.result(timeout=0.2)
            assert not entered.is_set()
        finally:
            release.set()
    assert not transport._connected.is_set() and not transport._handshake.is_set()
    assert transport._inbound.get_nowait() is None


def test_waiting_old_mqtt_worker_discards_frame_after_reconnect():
    transport, old, current = mqtt(), Broker(), Broker()
    old_channel, current_channel = Channel(), Channel()
    entered, release = threading.Event(), threading.Event()

    class DelayedQueue:
        def get(self, **kwargs):
            entered.set()
            assert release.wait(1)
            return b"old ciphertext"

    transport._client, transport._noise = old, old_channel
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(transport._receive_loop, old, old_channel, DelayedQueue())
        assert entered.wait(1)
        with transport._lifecycle_lock:
            transport._client, transport._noise = current, current_channel
        release.set()
        worker.result(timeout=1)
    assert old_channel.received == [] and current_channel.received == []
    assert old_channel.closes == 1 and current_channel.closes == 0
    assert transport.last_error() is None


def test_old_noise_writer_cannot_publish_on_new_broker():
    transport, old, current = mqtt(), Broker(), Broker()
    transport._client = current
    with pytest.raises(ThalovantConnectionError):
        transport._publish(b"old ciphertext", client=old)
    assert old.published == [] and current.published == []


def test_disconnect_cleans_owned_mqtt_resources_once():
    transport, client, channel = mqtt(), Broker(), Channel()
    client.thalovant_channel = channel
    client.thalovant_inbound = queue.Queue()
    transport._client, transport._noise = client, channel
    transport.disconnect(); transport.disconnect()
    assert client.disconnects == 1 and client.stops == 1 and channel.closes == 1
    assert transport._client is None and transport._noise is None


def test_http_connect_exception_cleans_client_and_reservation(monkeypatch):
    from thalovant import _http_runtime
    created = []

    class Failing:
        def __init__(self, transport):
            self.closes = 0
            created.append(self)

        def connect(self):
            raise OSError("synthetic dial failure")

        def close(self):
            self.closes += 1

    monkeypatch.setattr(_http_runtime, "HTTPNoiseClient", Failing)
    transport = HiveMindHTTPTransport(identity(), useragent="fixture")
    with pytest.raises(ThalovantConnectionError):
        transport.connect()
    assert created[0].closes == 1 and transport._client is None and not transport._connecting
    assert transport.connection_info().phase == "error"


def test_cancelled_old_http_connect_cannot_mark_new_connection_ready_or_close_it(monkeypatch):
    from thalovant import _http_runtime
    entered, release = threading.Event(), threading.Event()
    created = []

    class Client:
        def __init__(self, transport):
            self.connected = threading.Event()
            self.handshake_event = threading.Event()
            self.channel = SimpleNamespace(ready=False)
            self.closes = 0
            self.index = len(created)
            created.append(self)

        def connect(self):
            if self.index == 0:
                entered.set()
                assert release.wait(2)
            self.channel.ready = True
            self.connected.set(); self.handshake_event.set()

        def close(self):
            self.closes += 1
            self.connected.clear(); self.handshake_event.clear()

        def is_alive(self):
            return self.connected.is_set()

    monkeypatch.setattr(_http_runtime, "HTTPNoiseClient", Client)
    transport = HiveMindHTTPTransport(identity(), useragent="fixture")
    with ThreadPoolExecutor(max_workers=1) as pool:
        old_connect = pool.submit(transport.connect)
        assert entered.wait(1)
        with pytest.raises(ThalovantConnectionError, match="already in progress"):
            transport.connect()
        transport.disconnect()
        transport.connect()
        assert transport._client is created[1] and transport.connection_info().phase == "ready"
        release.set()
        with pytest.raises(ThalovantConnectionError):
            old_connect.result(timeout=1)
    assert created[0].closes == 1 and created[1].closes == 0
    assert transport._client is created[1] and transport.connection_info().phase == "ready"
    transport.disconnect()


def test_mqtt_synchronous_dial_failure_stops_worker_and_closes_channel(monkeypatch):
    from thalovant import _noise_runtime
    instances = []

    class FailingBroker(Broker):
        def __init__(self, *args, **kwargs):
            super().__init__()
            instances.append(self)

        def username_pw_set(self, *args): pass
        def tls_set(self): pass
        def will_set(self, *args, **kwargs): pass
        def connect(self, *args, **kwargs): raise OSError("synthetic dial failure")

    monkeypatch.setattr(_noise_runtime, "NoiseChannel", Channel)
    module = SimpleNamespace(Client=FailingBroker, CallbackAPIVersion=SimpleNamespace(VERSION2=2))
    transport = mqtt()
    monkeypatch.setattr(transport, "_load_mqtt_module", lambda: module)
    with pytest.raises(OSError, match="synthetic dial failure"):
        transport.connect()
    client = instances[0]
    assert transport._client is None and not transport._connecting
    assert not client.thalovant_worker.is_alive()
    assert client.disconnects == 1 and client.stops == 1
    assert client.thalovant_channel.closes >= 1


def test_mqtt_dial_finishing_after_cancel_cannot_start_a_network_worker(monkeypatch):
    from thalovant import _noise_runtime
    entered, release = threading.Event(), threading.Event()
    instances = []

    class DelayedBroker(Broker):
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.starts = 0
            instances.append(self)

        def username_pw_set(self, *args): pass
        def tls_set(self): pass
        def will_set(self, *args, **kwargs): pass
        def connect(self, *args, **kwargs):
            entered.set()
            assert release.wait(2)
        def loop_start(self):
            self.starts += 1

    monkeypatch.setattr(_noise_runtime, "NoiseChannel", Channel)
    transport = mqtt()
    monkeypatch.setattr(transport, "_load_mqtt_module", lambda: SimpleNamespace(
        Client=DelayedBroker, CallbackAPIVersion=SimpleNamespace(VERSION2=2)))
    with ThreadPoolExecutor(max_workers=1) as pool:
        connecting = pool.submit(transport.connect)
        assert entered.wait(1)
        transport.disconnect()
        release.set()
        with pytest.raises(ThalovantConnectionError, match="after cancellation"):
            connecting.result(timeout=1)
    assert instances[0].starts == 0
    assert instances[0].disconnects == 2  # initial close, then late returned dial
    assert transport._client is None and transport.connection_info().phase == "closed"


@pytest.mark.parametrize("callback", ["connect", "subscribe"])
def test_mqtt_callback_failure_diagnostics_redact_authorization_queries(callback):
    import json
    transport, broker = mqtt(), Broker()
    transport._client = broker
    secret = "test-only-mqtt-query-secret"

    class Reason:
        def __int__(self): return 128
        def __str__(self): return f"failed /connect?authorization={secret}"

    if callback == "connect":
        transport._on_connect(broker, None, None, Reason())
    else:
        transport._on_subscribe(broker, None, 1, [Reason()])
    assert secret not in json.dumps(transport.healthcheck().as_dict())
    assert secret not in json.dumps(transport.connection_info().as_dict())
    with pytest.raises(ThalovantConnectionError) as caught:
        transport._wait_mqtt_event(broker, threading.Event(), 0.01, callback)
    assert secret not in str(caught.value)


@pytest.mark.parametrize("phase", ["subscription", "handshake"])
def test_mqtt_wait_timeout_reports_the_phase_without_credentials(phase):
    from thalovant import ThalovantTimeoutError
    transport, broker = mqtt(), Broker()
    transport._client = broker
    with pytest.raises(ThalovantTimeoutError, match=f"MQTT {phase} timed out"):
        transport._wait_mqtt_event(broker, threading.Event(), 0, phase)
