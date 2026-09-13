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
