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


def close_or_explain(sdk, timeout=10):
    """Close, and when that does not finish, say what the client was waiting on.

    This test once saw close() wait out its whole budget in CI, and never since
    in thousands of runs on one or two cores. If it happens again the failure
    carries the lifecycle state and every task on the client's loop.
    """
    try:
        sdk.close(timeout=timeout)
    except ThalovantConnectionError as error:
        import io

        core, lines = sdk._core, io.StringIO()
        done = threading.Event()

        def dump():
            print(f"lock held={core._connection_lock.locked()} closing={core._closing} "
                  f"automatic_cleanups={core._automatic_cleanups} connected={core._connected} "
                  f"cancel_connect={core._cancel_connect!r}", file=lines)
            for task in asyncio.all_tasks():
                print(f"task {task!r}", file=lines)
                task.print_stack(file=lines)
            done.set()

        sdk._runner.loop().call_soon_threadsafe(dump)
        done.wait(5)
        raise AssertionError(f"{error}\n{lines.getvalue()}") from error


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
        with pytest.raises(ThalovantConnectionError):
            sdk.connect(timeout=0.02)
        assert transport.dials == 1
        # The connect this abandoned is owned by a worker, and the caller does
        # not wait for it -- that is the whole point of the assert above. So
        # the worker is still inside `Held.connect` here, and it retires the
        # connection when that returns: this budget has to outlast the gate
        # above, not the scheduler.
        #
        # Measured at 0.3s it was a coin toss, because it was really timing how
        # fast a thread got scheduled in the gap. Two identical CI runs of one
        # commit disagreed.
        transport.gate.set()
        assert transport.cleaned.wait(30)
        sdk.connect(timeout=1)
        assert not transport.bus_handlers and transport.sent == 0
    finally:
        transport.gate.set()
        close_or_explain(sdk)


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
    """A producer that outruns the consumer by one event overflows the buffer.

    The consumer takes the first event and then stops asking while the hub
    sends a burst one larger than the buffer: the situation the bound exists
    for -- a consumer slower than the hub -- rather than a race with it.
    """

    class Flood(QueryTransport):
        def on_mycroft(self, name, handler):
            super().on_mycroft(name, handler)
            self.bus(name, {'index': 0})

        def flood(self):
            for i in range(1, limit + 2):
                self.bus('event', {'index': i})

    transport = Flood()
    sdk = client(transport)
    try:
        stream = sdk.listen('event', timeout=5, max_buffered_events=limit)
        assert next(stream).data['index'] == 0
        transport.flood()
        with pytest.raises(ThalovantRuntimeError, match='overflow'):
            list(stream)
        # Flood delivers its first event from inside on_mycroft, so the
        # consumer can overflow while the registering thread has not yet
        # marked the registration landed; that thread then takes the handler
        # back off itself. Retired, then, but not necessarily by the time the
        # error reaches the consumer.
        deadline = time.monotonic() + 5
        while any(transport.bus_handlers.values()):
            assert time.monotonic() < deadline, transport.bus_handlers
            time.sleep(0.005)
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
        # The registration that delivered the burst is still returning on the
        # transport's thread when the first event is handed back; it takes the
        # retired listener off as soon as it does.
        deadline = time.monotonic() + 1
        while any(transport.bus_handlers.values()) and time.monotonic() < deadline:
            time.sleep(0.001)
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
            # Set the moment registration is actually blocked. The assertion
            # below is about ownership being *retained* while it is; without
            # this the test raced the registration thread's own start-up
            # against a 20ms budget and failed whenever the machine was busy
            # enough to lose it -- adding one test file elsewhere in the suite
            # was enough, which is not something this test means to measure.
            self.entered = threading.Event()
            self.once = True

        def hold(self):
            if self.once:
                self.once = False
                self.entered.set()
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
        # Enough budget to reach the transport, not enough to finish. `setup()`
        # computes its connect deadline when the worker thread actually runs,
        # so with 20ms a thread that starts late gets a deadline already in the
        # past: _connect raises before it ever calls register, nothing is held,
        # and the ownership this test is about never exists. The assertion
        # below still bounds promptness -- this only makes the precondition
        # reachable on a machine that is busy.
        with pytest.raises(ThalovantTimeoutError):
            operation(sdk, name, timeout=0.15)
        assert time.monotonic() - started < 0.3
        assert transport.entered.wait(5), 'registration never reached the transport'
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
    """Force retirement before the setup reports its failure."""
    import thalovant.client as client_module

    transport = QueryTransport()
    sdk = client(transport)

    def expire_first(loop, delay, callback):
        callback()
        return loop.call_later(3600, lambda: None)

    async def fail_setup(*args, **kwargs):
        raise ThalovantTimeoutError("initial setup failed after retirement")

    monkeypatch.setattr(sdk._core, "_connect", fail_setup)
    monkeypatch.setattr(client_module, "_schedule_expiry", expire_first)
    with pytest.raises(ThalovantTimeoutError, match="initial setup failed after retirement"):
        next(sdk.listen("event", timeout=0.02))
    assert not any(transport.bus_handlers.values())
