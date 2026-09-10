import asyncio
import threading
import time

import pytest

from thalovant import AsyncThalovantClient, ThalovantConnectionError, ThalovantRuntimeError, ThalovantTimeoutError
from test_query_semantics import QueryTransport, client


async def eventually(predicate):
    deadline = asyncio.get_running_loop().time() + 1
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.005)


def operation(sdk, name, *, timeout=0.02):
    if name == 'listen':
        return next(sdk.listen('event', timeout=timeout))
    if name == 'wait_for_event':
        return sdk.wait_for_event('event', timeout=timeout)
    return getattr(sdk, name)('hello', timeout=timeout)


@pytest.mark.parametrize('name', ['ask', 'wait_for_event', 'listen'])
def test_event_operations_bound_connect_and_never_subscribe_after_expiry(name):
    class Held(QueryTransport):
        def __init__(self):
            super().__init__()
            self.gate = threading.Event()
            self.cleaned = threading.Event()

        def connect(self):
            self.dials += 1
            self.gate.wait(5)
            self.connected = True

        def disconnect(self):
            super().disconnect()
            self.cleaned.set()

    transport = Held()
    sdk = client(transport)
    try:
        started = time.monotonic()
        with pytest.raises(ThalovantTimeoutError):
            operation(sdk, name)
        assert time.monotonic() - started < 0.3
        assert transport.cleaned.wait(0.3)
        with pytest.raises(ThalovantConnectionError):
            sdk.connect(timeout=0.02)
        assert transport.dials == 1
        transport.gate.set()
        sdk.connect(timeout=1)
        assert not transport.bus_handlers and transport.sent == 0
    finally:
        transport.gate.set()
        sdk.close()


@pytest.mark.parametrize('name', ['query', 'ask', 'wait_for_event', 'listen'])
def test_cancelled_async_waiter_preserves_another_pending_owner(name):
    class Held(QueryTransport):
        def __init__(self):
            super().__init__()
            self.gate = threading.Event()
            self.started = threading.Event()
            self.closed = 0

        def connect(self):
            self.dials += 1
            self.started.set()
            self.gate.wait(5)
            self.connected = True

        def disconnect(self):
            self.closed += 1
            super().disconnect()

    async def exercise():
        transport = Held()
        sdk = AsyncThalovantClient(client(transport).identity, transport=transport, reply_settle_seconds=0)
        owner = asyncio.create_task(sdk.connect(timeout=2))
        try:
            assert await asyncio.to_thread(transport.started.wait, 1)
            if name == 'listen':
                stream = sdk.listen('event', timeout=2)
                request = asyncio.create_task(anext(stream))
            else:
                request = asyncio.create_task(getattr(sdk, name)('event', timeout=2))
            await asyncio.sleep(0.03)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            await asyncio.sleep(0.05)
            assert transport.closed == 0, 'a cancelled queued waiter must not close its predecessor'
            assert transport.sent == 0 and not transport.bus_handlers
            assert not any(transport.handlers.values())
            transport.gate.set()
            await owner
            assert transport.dials == 1 and transport.connected
        finally:
            transport.gate.set()
            await asyncio.gather(owner, return_exceptions=True)
            await sdk.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('name', ['query', 'ask'])
def test_async_request_cancellation_retires_active_write_before_replacement(name):
    class Held(QueryTransport):
        def __init__(self):
            super().__init__(self.hold)
            self.gate = threading.Event()
            self.started = threading.Event()
            self.cleaned = threading.Event()

        def hold(self, transport):
            self.started.set()
            self.gate.wait(5)
            raise RuntimeError('late write rejection')

        def disconnect(self):
            super().disconnect()
            self.cleaned.set()

    async def exercise():
        transport = Held()
        sdk = AsyncThalovantClient(client(transport).identity, transport=transport, reply_settle_seconds=0)
        try:
            request = asyncio.create_task(getattr(sdk, name)('hello', timeout=2))
            assert await asyncio.to_thread(transport.started.wait, 1)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert await asyncio.to_thread(transport.cleaned.wait, 1)
            await eventually(lambda: not any(transport.handlers.values()) and not any(transport.bus_handlers.values()))
            with pytest.raises(ThalovantConnectionError):
                await sdk.connect(timeout=0.02)
            assert transport.dials == 1
            transport.gate.set()
            await sdk.connect(timeout=1)
            assert transport.dials == 2 and transport.connected
        finally:
            transport.gate.set()
            await sdk.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('name', ['query', 'ask', 'wait_for_event', 'listen'])
def test_async_cancellation_removes_active_reply_or_event_listeners(name):
    async def exercise():
        transport = QueryTransport()
        sdk = AsyncThalovantClient(client(transport).identity, transport=transport)
        try:
            if name == 'listen':
                stream = sdk.listen('event', timeout=5)
                task = asyncio.create_task(anext(stream))
            else:
                task = asyncio.create_task(getattr(sdk, name)('event', timeout=5))
            await eventually(lambda: any(transport.handlers.values()) or any(transport.bus_handlers.values()))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await eventually(lambda: not any(transport.handlers.values()) and not any(transport.bus_handlers.values()))
        finally:
            await sdk.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('limit', [1, 3, 256])
def test_listen_overflow_is_explicit_and_retires_subscription(limit):
    class Flood(QueryTransport):
        def on_mycroft(self, name, handler):
            super().on_mycroft(name, handler)
            for i in range(limit + 1):
                self.bus(name, {'index': i})

    transport = Flood()
    sdk = client(transport)
    try:
        with pytest.raises(ThalovantRuntimeError, match='overflow'):
            list(sdk.listen('event', timeout=1, max_buffered_events=limit))
        assert not any(transport.bus_handlers.values())
    finally:
        sdk.close()


def test_wait_for_event_keeps_first_matching_event_during_burst():
    class Flood(QueryTransport):
        def on_mycroft(self, name, handler):
            super().on_mycroft(name, handler)
            self.bus(name, {'index': -1}, {'request_id': 'foreign'})
            for i in range(300):
                self.bus(name, {'index': i}, {'request_id': 'wanted'})

    transport = Flood()
    sdk = client(transport)
    try:
        assert sdk.wait_for_event('event', timeout=1, request_id='wanted').data['index'] == 0
        assert not any(transport.bus_handlers.values())
    finally:
        sdk.close()


def test_async_listen_uses_bounded_buffer_without_unbounded_loop_callbacks():
    async def exercise():
        transport = QueryTransport()
        sdk = AsyncThalovantClient(client(transport).identity, transport=transport)
        stream = sdk.listen('event', max_buffered_events=2)
        try:
            first = asyncio.create_task(anext(stream))
            await eventually(lambda: bool(transport.bus_handlers.get('event')))
            transport.bus('event', {'index': 0})
            assert (await first).data['index'] == 0
            for i in range(1, 4):
                transport.bus('event', {'index': i})
            with pytest.raises(ThalovantRuntimeError, match='overflow'):
                await anext(stream)
            assert not any(transport.bus_handlers.values())
        finally:
            await stream.aclose()
            await sdk.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('limit', [0, -1, 1.5, True])
def test_invalid_event_buffer_performs_no_connection(limit):
    transport = QueryTransport()
    with pytest.raises(ValueError):
        next(client(transport).listen('event', max_buffered_events=limit))
    assert transport.dials == 0


@pytest.mark.parametrize('name', ['query', 'ask', 'wait_for_event', 'listen'])
def test_blocking_transport_predicate_cannot_extend_request_deadline(name):
    class BlockPredicate(QueryTransport):
        def __init__(self):
            super().__init__()
            self.gate = threading.Event()
            self.monitor = False

        def is_connected(self):
            if self.monitor:
                self.gate.wait(5)
            return self.connected

        def send_hive_message(self, frame, *, encrypt):
            self.monitor = True

        def emit_event(self, name, data, context):
            self.monitor = True

        def on_mycroft(self, name, handler):
            super().on_mycroft(name, handler)
            if name == 'event':
                self.monitor = True

    transport = BlockPredicate()
    sdk = client(transport)
    try:
        started = time.monotonic()
        with pytest.raises((ThalovantTimeoutError, StopIteration)):
            operation(sdk, name, timeout=0.07)
        assert time.monotonic() - started < 0.4
    finally:
        transport.gate.set()
        sdk.close()


def test_ask_hard_failure_freezes_partial_reply_and_ignores_later_write_error():
    def respond(transport):
        transport.bus('speak', {'utterance': 'answer'})
        transport.bus('hive.policy.denied')
        transport.bus('speak', {'utterance': 'late'})
        raise RuntimeError('late write failure')

    transport = QueryTransport(respond)
    sdk = client(transport, settle=10)
    try:
        started = time.monotonic()
        reply = sdk.ask('hello', timeout=0.03)
        assert time.monotonic() - started < 0.3
        assert reply.text == 'answer' and not reply.handled
        assert [event.name for event in reply.events] == ['speak', 'hive.policy.denied']
    finally:
        sdk.close()


@pytest.mark.parametrize('name', ['query', 'ask', 'wait_for_event', 'listen'])
def test_blocked_registration_retains_ownership_and_removes_late_listener(name):
    class HeldRegistration(QueryTransport):
        def __init__(self):
            super().__init__()
            self.gate = threading.Event()
            self.once = True

        def hold(self):
            if self.once:
                self.once = False
                self.gate.wait(5)

        def on_mycroft(self, name, handler):
            self.hold()
            super().on_mycroft(name, handler)

        def on_hive_message(self, name, handler):
            self.hold()
            super().on_hive_message(name, handler)

    transport = HeldRegistration()
    sdk = client(transport)
    try:
        started = time.monotonic()
        with pytest.raises(ThalovantTimeoutError):
            operation(sdk, name)
        assert time.monotonic() - started < 0.3
        with pytest.raises(ThalovantConnectionError):
            sdk.connect(timeout=0.02)
        transport.gate.set()
        sdk.connect(timeout=1)
        assert transport.sent == 0
        assert not any(transport.handlers.values()) and not any(transport.bus_handlers.values())
    finally:
        transport.gate.set()
        sdk.close()


def test_no_timeout_listener_still_bounds_initial_connection():
    class Held(QueryTransport):
        def __init__(self):
            super().__init__()
            self.gate = threading.Event()

        def connect(self):
            self.gate.wait(5)
            self.connected = True

    transport = Held()
    sdk = client(transport)
    sdk._hard_connect_timeout = 0.02
    try:
        started = time.monotonic()
        with pytest.raises(ThalovantTimeoutError):
            next(sdk.listen('event'))
        assert time.monotonic() - started < 0.3
    finally:
        transport.gate.set()
        sdk.close(timeout=1)


def test_ask_deadline_retires_held_send_before_reconnect():
    gate = threading.Event()
    transport = QueryTransport(lambda transport: gate.wait(5))
    sdk = client(transport)
    try:
        started = time.monotonic()
        with pytest.raises(ThalovantTimeoutError):
            sdk.ask('hello', timeout=0.02)
        assert time.monotonic() - started < 0.3
        with pytest.raises(ThalovantConnectionError):
            sdk.connect(timeout=0.02)
        assert transport.sent == 1 and transport.dials == 1
        gate.set()
        sdk.connect(timeout=1)
        assert transport.dials == 2
    finally:
        gate.set()
        sdk.close()


def test_listener_keeps_initial_setup_failure_when_expiry_retires_first(monkeypatch):
    """Force timer retirement before the setup worker reports its failure."""
    import thalovant.client as client_module

    transport = QueryTransport()
    sdk = client(transport)

    class ExpireFirst:
        def __init__(self, interval, callback):
            self.callback = callback

        def start(self):
            self.callback()

        def cancel(self):
            pass

    class InlineSetup:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            self.target()

    def fail_setup(*args, **kwargs):
        raise ThalovantTimeoutError("initial setup failed after retirement")

    monkeypatch.setattr(sdk, "_connect", fail_setup)
    monkeypatch.setattr(client_module.threading, "Timer", ExpireFirst)
    monkeypatch.setattr(client_module.threading, "Thread", InlineSetup)
    with pytest.raises(ThalovantTimeoutError, match="initial setup failed after retirement"):
        next(sdk.listen("event", timeout=0.02))
    assert not any(transport.bus_handlers.values())
