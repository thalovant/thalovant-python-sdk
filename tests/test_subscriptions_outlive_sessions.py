"""A subscription made with `on()` outlives the transport session it was made on.

The transports register handlers on the client object they hold, and a
reconnect replaces that object. `emit()`, `ask()` and `_with_reconnect()`
reconnect on their own when the hub link drops, so without the client
putting its subscriptions back, a responder wired once went quiet at the
first hiccup and nothing said so (the custos node's shadow responder,
2026-09-13: requests received, none handled, for twelve hours).
"""
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import pytest

from thalovant import ThalovantClient, ThalovantConnectionError
from test_query_semantics import QueryTransport, client


class SessionSwappingTransport(QueryTransport):
    """Every connect() is a fresh session, with nothing registered on it."""

    def connect(self):
        super().connect()
        self.bus_handlers = {}

    def remove_mycroft(self, name, handler):
        handlers = self.bus_handlers.get(name, [])
        if handler in handlers:
            handlers.remove(handler)


def _client(transport):
    return ThalovantClient(client(transport).identity, transport=transport,
                           auto_reconnect=True, reconnect_attempts=1)


def test_a_subscription_is_back_on_the_session_a_reconnect_opens():
    transport = SessionSwappingTransport()
    sdk = _client(transport)
    seen = []
    try:
        sdk.on('custos.shadow.request', lambda event: seen.append(event.data['verb']))
        transport.bus('custos.shadow.request', {'verb': 'before'})
        # the link drops; the next emit() reconnects by itself
        transport.connected = False
        sdk.emit('custos.shadow.event', {'seq': 1})
        assert transport.dials == 2
        transport.bus('custos.shadow.request', {'verb': 'after'})
        assert seen == ['before', 'after']
        # once on the new session, not once per reconnect so far
        assert len(transport.bus_handlers['custos.shadow.request']) == 1
    finally:
        sdk.close()


def test_a_closed_subscription_stays_closed_across_a_reconnect():
    transport = SessionSwappingTransport()
    sdk = _client(transport)
    seen = []
    try:
        subscription = sdk.on('custos.shadow.request', lambda event: seen.append(event.data['verb']))
        subscription.close()
        transport.connected = False
        sdk.emit('custos.shadow.event', {'seq': 1})
        transport.bus('custos.shadow.request', {'verb': 'after'})
        assert seen == [] and not transport.bus_handlers.get('custos.shadow.request')
    finally:
        sdk.close()


class PausedRegistrationTransport(SessionSwappingTransport):
    def __init__(self):
        super().__init__()
        self.pause_next = False
        self.registering = threading.Event()
        self.release_registration = threading.Event()

    def on_mycroft(self, name, handler):
        if self.pause_next:
            self.pause_next = False
            self.registering.set()
            assert self.release_registration.wait(5), "registration was not released"
        super().on_mycroft(name, handler)


def test_close_during_reconnect_cannot_restore_a_closed_handler():
    transport = PausedRegistrationTransport()
    sdk = _client(transport)
    seen = []
    subscription = sdk.on("event", lambda event: seen.append(event))
    transport.connected = False
    transport.pause_next = True
    with ThreadPoolExecutor(max_workers=2) as workers:
        reconnect = workers.submit(sdk.connect)
        try:
            assert transport.registering.wait(5)
            closed = workers.submit(subscription.close)
            # Removal must serialize with the in-flight registration. In 0.6.9
            # it completed first, and the subsequent registration revived it.
            with pytest.raises(TimeoutError):
                closed.result(timeout=0.1)
        finally:
            transport.release_registration.set()
        reconnect.result(timeout=5)
        closed.result(timeout=5)
    try:
        transport.bus("event")
        assert seen == []
        assert not transport.bus_handlers.get("event")
    finally:
        sdk.close()


def test_new_subscription_racing_reconnect_is_registered_once():
    transport = PausedRegistrationTransport()
    sdk = _client(transport)
    sdk.connect()
    seen = []
    transport.pause_next = True
    with ThreadPoolExecutor(max_workers=2) as workers:
        added = workers.submit(sdk.on, "event", lambda event: seen.append(event))
        try:
            assert transport.registering.wait(5)
            transport.connected = False
            reconnect = workers.submit(sdk.connect)
            try:
                reconnect.result(timeout=0.1)
            except TimeoutError:
                pass  # Fixed clients wait for the old session registration.
        finally:
            transport.release_registration.set()
        subscription = added.result(timeout=5)
        reconnect.result(timeout=5)
    try:
        transport.bus("event")
        assert len(seen) == 1
        assert len(transport.bus_handlers["event"]) == 1
        subscription.close()
        transport.connected = False
        sdk.connect()
        transport.bus("event")
        assert len(seen) == 1
    finally:
        sdk.close()


def test_failed_registration_does_not_create_a_future_subscription():
    class FailingRegistrationTransport(SessionSwappingTransport):
        def on_mycroft(self, name, handler):
            if self.dials == 1:
                raise ThalovantConnectionError("registration failed")
            super().on_mycroft(name, handler)

    transport = FailingRegistrationTransport()
    sdk = _client(transport)
    seen = []
    try:
        with pytest.raises(ThalovantConnectionError, match="registration failed"):
            sdk.on("event", lambda event: seen.append(event))
        transport.connected = False
        sdk.connect()
        transport.bus("event")
        assert seen == []
        assert not transport.bus_handlers.get("event")
    finally:
        sdk.close()


@pytest.mark.parametrize("close_pending", [False, True])
@pytest.mark.parametrize("preserve_session", [False, True])
def test_subscription_handoff_while_connection_is_pending(monkeypatch, close_pending, preserve_session):
    class PausedConnectTransport(SelfHealingTransport):
        def __init__(self):
            super().__init__()
            self.connecting = threading.Event()
            self.release_connect = threading.Event()

        def connect(self):
            if self.dials:
                self.connecting.set()
                assert self.release_connect.wait(5)
            super().connect()

    transport = PausedConnectTransport()
    sdk = _client(transport)
    seen = []
    existing = sdk.on("existing", lambda event: seen.append(event))
    adding = threading.Event()
    release_add = threading.Event()
    original_add = sdk._add_subscription

    def paused_add(name, handler):
        adding.set()
        assert release_add.wait(5)
        original_add(name, handler)

    monkeypatch.setattr(sdk, "_add_subscription", paused_add)
    with ThreadPoolExecutor(max_workers=2) as workers:
        added = workers.submit(sdk.on, "event", lambda event: seen.append(event))
        try:
            assert adding.wait(5)
            sdk._connected = False
            if not preserve_session:
                transport.connected = False
            reconnect = workers.submit(sdk.connect)
            assert transport.connecting.wait(5)
            existing.close()
            release_add.set()
            subscription = added.result(timeout=5)
            if close_pending:
                subscription.close()
        finally:
            release_add.set()
            transport.release_connect.set()
        reconnect.result(timeout=5)
    try:
        transport.bus("event")
        assert len(seen) == (0 if close_pending else 1)
        transport.bus("existing")
        assert len(seen) == (0 if close_pending else 1)
        subscription.close()
    finally:
        sdk.close()


class SelfHealingTransport(QueryTransport):
    """The library reopened the socket by itself: connect() finds the session
    open, keeps what is registered on it, and reports the same token."""

    def __init__(self):
        super().__init__()
        self.generation = 0

    def connect(self):
        self.dials += 1
        if self.connected:
            return
        self.connected = True
        self.generation += 1
        self.bus_handlers = {}

    def session_token(self):
        return self.generation


def test_a_session_found_already_open_keeps_one_handler_per_subscription():
    transport = SelfHealingTransport()
    sdk = _client(transport)
    seen = []
    try:
        sdk.on('custos.shadow.request', lambda event: seen.append(event.data['verb']))
        # a connect that timed out leaves the client believing it is down
        # (`cancel()`), while the transport finishes opening on its own
        sdk._connected = False
        sdk.emit('custos.shadow.event', {'seq': 1})
        assert transport.dials == 2 and transport.generation == 1
        transport.bus('custos.shadow.request', {'verb': 'once'})
        assert seen == ['once']
        assert len(transport.bus_handlers['custos.shadow.request']) == 1
        # and a session this client opens itself gets it back, still once
        transport.connected = False
        sdk.emit('custos.shadow.event', {'seq': 2})
        assert transport.generation == 2
        transport.bus('custos.shadow.request', {'verb': 'again'})
        assert seen == ['once', 'again']
        assert len(transport.bus_handlers['custos.shadow.request']) == 1
    finally:
        sdk.close()


def test_custom_transport_without_token_reuses_handlers_without_duplicates():
    transport = QueryTransport()
    sdk = _client(transport)
    seen = []
    try:
        subscription = sdk.on("event", lambda event: seen.append(event))
        sdk._connected = False
        sdk.connect()
        transport.bus("event")
        assert len(seen) == 1
        subscription.close()
        sdk._connected = False
        sdk.connect()
        transport.bus("event")
        assert len(seen) == 1
    finally:
        sdk.close()
