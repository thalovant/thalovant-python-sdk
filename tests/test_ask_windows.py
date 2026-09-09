import threading
import time

import pytest

from thalovant import ThalovantClient, ThalovantConnectionError, ThalovantRuntimeError, ThalovantTimeoutError
from test_query_semantics import QueryTransport, client


@pytest.mark.parametrize('first', ['ovos.utterance.handled', 'complete_intent_failure', 'ovos.intent.unmatched'])
def test_ask_delayed_fallback_recovers_handled_or_soft_miss(first):
    def script(transport):
        transport.bus(first)
        time.sleep(0.02)
        transport.bus('speak', {'utterance': 'fallback'})
        time.sleep(0.02)
        transport.bus('ovos.utterance.speak', {'utterance': 'second'})
        time.sleep(0.07)
        transport.bus('speak', {'utterance': 'past the fixed settle window'})

    transport = QueryTransport(script)
    sdk = client(transport, settle=0.06)
    try:
        reply = sdk.ask('hello', timeout=0.5)
        assert reply.text == 'fallback second'
        assert reply.handled and reply.failure_event is None
        assert [event.name for event in reply.events] == [first, 'speak', 'ovos.utterance.speak']
    finally:
        sdk.close()


def test_ask_first_speech_needs_no_handled_and_later_fragments_do_not_extend_window():
    def script(transport):
        transport.bus('speak', {'utterance': 'first'})
        time.sleep(0.04)
        transport.bus('speak', {'utterance': 'second'})
        time.sleep(0.07)
        transport.bus('speak', {'utterance': 'late'})

    transport = QueryTransport(script)
    sdk = client(transport, settle=0.08)
    try:
        assert sdk.ask('hello', timeout=0.5).text == 'first second'
    finally:
        sdk.close()


@pytest.mark.parametrize('first', ['ovos.utterance.handled', 'complete_intent_failure', 'ovos.intent.unmatched'])
def test_ask_empty_window_is_fixed_and_never_returns_empty_success(first):
    def script(transport):
        transport.bus(first)
        time.sleep(0.03)
        transport.bus(first)
        time.sleep(0.04)
        transport.bus('speak', {'utterance': 'too late'})

    transport = QueryTransport(script)
    sdk = ThalovantClient(client(transport).identity, transport=transport, empty_reply_wait_seconds=0.05)
    try:
        expected = ThalovantTimeoutError if first == 'ovos.utterance.handled' else ThalovantRuntimeError
        with pytest.raises(expected):
            sdk.ask('hello', timeout=0.5)
    finally:
        sdk.close()


@pytest.mark.parametrize('speech', [False, True])
def test_ask_windows_are_clipped_to_original_budget(speech):
    def script(transport):
        transport.bus('ovos.utterance.handled')
        if speech:
            transport.bus('speak', {'utterance': 'answer'})

    transport = QueryTransport(script)
    sdk = client(transport, settle=10)
    started = time.monotonic()
    try:
        if speech:
            assert sdk.ask('hello', timeout=0.04).text == 'answer'
        else:
            with pytest.raises(ThalovantTimeoutError):
                sdk.ask('hello', timeout=0.04)
        assert time.monotonic() - started < 0.3
    finally:
        sdk.close()


@pytest.mark.parametrize('name', ['ask', 'query', 'emit', 'send_utterance', 'send_action', 'send_code'])
def test_application_publication_is_never_repeated_after_send_error(name):
    def fail_after_publication(transport):
        transport.connected = False
        raise ThalovantConnectionError('peer disconnected after publication')

    transport = QueryTransport(fail_after_publication)
    sdk = ThalovantClient(client(transport).identity, transport=transport, auto_reconnect=True, reconnect_attempts=3)
    try:
        with pytest.raises(ThalovantConnectionError):
            getattr(sdk, name)('application action')
        assert transport.sent == 1
        assert transport.dials == 1
    finally:
        sdk.close()


@pytest.mark.parametrize('name', ['ask', 'query'])
def test_application_publication_is_never_repeated_after_reply_disconnect(name):
    transport = QueryTransport(lambda transport: setattr(transport, 'connected', False))
    sdk = ThalovantClient(client(transport).identity, transport=transport, auto_reconnect=True, reconnect_attempts=3)
    try:
        with pytest.raises(ThalovantConnectionError):
            getattr(sdk, name)('application action', timeout=0.5)
        assert transport.sent == 1 and transport.dials == 1
    finally:
        sdk.close()


@pytest.mark.parametrize('name', ['ask', 'emit'])
def test_reconnect_option_still_recovers_prepublication_connection_failure(name):
    class Preflight(QueryTransport):
        def connect(self):
            self.dials += 1
            if self.dials == 1:
                raise ThalovantConnectionError('prepublication connection failed')
            self.connected = True

    transport = Preflight(lambda transport: transport.bus('speak', {'utterance': 'answer'}))
    sdk = ThalovantClient(client(transport).identity, transport=transport, reply_settle_seconds=0, auto_reconnect=True)
    try:
        result = getattr(sdk, name)('hello')
        if name == 'ask':
            assert result.text == 'answer'
        assert transport.dials == 2 and transport.sent == 1
    finally:
        sdk.close()


@pytest.mark.parametrize('blocked_predicate', [False, True])
def test_paused_listener_retires_at_deadline_even_when_transport_monitor_blocks(blocked_predicate):
    class Monitor(QueryTransport):
        def __init__(self):
            super().__init__()
            self.gate = threading.Event()
            self.monitor = False

        def is_connected(self):
            if self.monitor and blocked_predicate:
                self.gate.wait(5)
            return self.connected

        def on_mycroft(self, name, handler):
            super().on_mycroft(name, handler)
            self.bus(name, {'index': 1})
            self.monitor = True

    transport = Monitor()
    sdk = client(transport)
    stream = sdk.listen('event', timeout=0.05)
    try:
        assert next(stream).data['index'] == 1
        time.sleep(0.1)
        assert not any(transport.bus_handlers.values()), 'retirement cannot wait for next()'
        with pytest.raises(StopIteration):
            next(stream)
    finally:
        transport.gate.set()
        stream.close()
        sdk.close()


def test_ask_requires_request_id_and_accepts_runtime_replaced_session():
    def script(transport):
        transport.bus('speak', {'utterance': 'ambient'}, context={})
        transport.bus('hive.policy.denied', context={'request_id': 'foreign'})
        transport.bus('speak', {'utterance': 'wrong request'}, context={'request_id': 'foreign'})
        transport.bus('speak', {'utterance': 'answer'}, context={
            'request_id': 'wanted', 'session': {'session_id': 'runtime-replaced'},
        })

    transport = QueryTransport(script)
    sdk = client(transport, settle=0)
    try:
        reply = sdk.ask('hello', timeout=0.5, request_id='wanted', session_id='client-session')
        assert reply.text == 'answer' and reply.session_id == 'runtime-replaced'
        assert reply.request_id == 'wanted' and len(reply.events) == 1
    finally:
        sdk.close()


@pytest.mark.parametrize('context', [{}, {'request_id': 'foreign'}])
def test_uncorrelated_ask_events_cannot_satisfy_or_fail_request(context):
    def script(transport):
        transport.bus('speak', {'utterance': 'ambient'}, context=context)
        transport.bus('hive.policy.denied', context=context)
        transport.bus('ovos.utterance.handled', context=context)

    transport = QueryTransport(script)
    sdk = client(transport, settle=0)
    try:
        with pytest.raises(ThalovantTimeoutError):
            sdk.ask('hello', timeout=0.03, request_id='wanted')
    finally:
        sdk.close()
