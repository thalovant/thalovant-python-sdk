"""A subscription made with `on()` outlives the transport session it was made on.

The transports register handlers on the client object they hold, and a
reconnect replaces that object. `emit()`, `ask()` and `_with_reconnect()`
reconnect on their own when the hub link drops, so without the client
putting its subscriptions back, a responder wired once went quiet at the
first hiccup and nothing said so (the custos node's shadow responder,
2026-09-13: requests received, none handled, for twelve hours).
"""
from thalovant import ThalovantClient
from test_query_semantics import QueryTransport, client


class SessionSwappingTransport(QueryTransport):
    """Every connect() is a fresh session, with nothing registered on it."""

    def connect(self):
        super().connect()
        self.bus_handlers = {}


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
