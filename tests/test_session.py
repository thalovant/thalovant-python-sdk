"""The hub session policy, as the appliance measured it, now in the SDK."""
from __future__ import annotations

import contextlib
import socket
import threading
from types import SimpleNamespace

import pytest

from thalovant import HubSession, HubSessionPolicy, OriginPreference, hub_hostname, preferred_origin
from thalovant.errors import ThalovantPolicyDeniedError, ThalovantRuntimeError, ThalovantTimeoutError
from thalovant.session import alive


def _client(phase="ready", **kw):
    client = SimpleNamespace(connection_info=lambda: SimpleNamespace(phase=phase), closed=0,
                             subscriptions=[], asks=[], emits=[])
    client.close = lambda: setattr(client, "closed", client.closed + 1)
    client.on = lambda name, handler: client.subscriptions.append((name, handler))
    client.ask = lambda text, **kwargs: client.asks.append((text, kwargs)) or SimpleNamespace(text="ok")
    client.emit = lambda event, data=None, context=None: client.emits.append((event, data)) or None
    for key, value in kw.items():
        setattr(client, key, value)
    return client


def _session(connect, clock=None, **kw):
    return HubSession(connect, warm=False, clock=clock or (lambda: 0.0), **kw)


def test_a_dead_session_is_read_from_the_public_connection_info():
    assert alive(_client("ready")) and alive(_client("handshake"))
    assert not alive(_client("error")) and not alive(_client("closed"))
    # anything unexpected reads as alive: a probe that guessed "dead" would
    # tear down a working session on every check
    assert alive(SimpleNamespace()) and alive(_client(None))
    assert not alive(None)


def test_an_ambiguous_ask_is_not_replayed_and_the_next_call_rebuilds():
    calls = []
    def accepted_then_lost(text, **kwargs):
        calls.append(text)
        raise ConnectionError("socket closed after remote acceptance")
    first, second = _client(ask=accepted_then_lost), _client()
    clients = [first, second]
    session = _session(lambda: clients.pop(0))
    with pytest.raises(ConnectionError):
        session.ask("install a skill")
    assert calls == ["install a skill"] and second.asks == []
    assert first.closed == 1 and not session.held
    assert session.ask("status").text == "ok"
    assert second.asks == [("status", {})]


def test_a_hub_blip_is_retried_within_seconds_not_minutes():
    """Measured 2026-09-12: two 502s from the gateway, then two minutes of
    silence, because the retry window opened between two minute-mark probes."""
    clock = [1000.0]
    attempts = []

    def connect():
        attempts.append(clock[0])
        if len(attempts) < 3:
            raise ConnectionRefusedError("502 Bad Gateway")
        return _client()

    session = _session(connect, clock=lambda: clock[0])
    policy = session.policy

    def ensure_quietly():
        with contextlib.suppress(Exception):
            session._ensure()

    session.warm = ensure_quietly
    session.probe()
    assert len(attempts) == 1
    assert session.retry_at == 1000.0 + policy.retry_seconds
    assert session.probe_delay() == policy.probe_down_seconds
    clock[0] = 1005.0
    session.probe()
    assert len(attempts) == 1, "the window is still shut"
    clock[0] = 1010.0
    session.probe()
    assert len(attempts) == 2
    assert session.retry_at == 1010.0 + 2 * policy.retry_seconds
    clock[0] = 1030.0
    session.probe()
    assert len(attempts) == 3 and session.held
    assert session.probe_delay() == policy.probe_seconds
    assert session.retry_wait == policy.retry_seconds, "a success resets the ladder"


def test_the_retry_ladder_reaches_its_ceiling_against_a_refusing_hub():
    policy = HubSessionPolicy()
    wait, waits = policy.retry_seconds, []
    for _ in range(6):
        waits.append(wait)
        wait = policy.next_wait(wait)
    assert waits == [10.0, 20.0, 40.0, 80.0, 120.0, 120.0]


def test_a_refusal_keeps_the_session_and_a_timeout_drops_it():
    def refusing(text, **kwargs):
        raise ThalovantPolicyDeniedError("recognizer_loop:utterance", code="quota")

    client = _client(ask=refusing)
    session = _session(lambda: client)
    with pytest.raises(ThalovantRuntimeError):
        session.ask("hello")
    assert session.held and client.closed == 0, "a refusal arrives on a live socket"

    def timing_out(text, **kwargs):
        raise ThalovantTimeoutError("no answer")

    quiet = _client(ask=timing_out)
    session = _session(lambda: quiet)
    with pytest.raises(ThalovantTimeoutError):
        session.ask("hello")
    assert not session.held and quiet.closed == 1, "a quiet death is not kept"


def test_subscriptions_are_wired_on_every_client_the_session_builds():
    first, second = _client("error"), _client()
    clients = [first, second]
    session = _session(lambda: clients.pop(0))
    handler = lambda event: None  # noqa: E731
    session.on("custos.shadow.request", handler)
    assert session.ask("hi").text == "ok"
    assert first.subscriptions == [("custos.shadow.request", handler)]
    # the first client reads dead on the next call: replaced, and the
    # replacement carries the subscription without anyone re-wiring it
    assert session.ask("again").text == "ok"
    assert first.closed == 1
    assert second.subscriptions == [("custos.shadow.request", handler)]
    assert second.asks == [("again", {})]


def test_emit_drops_a_dead_socket_but_never_publishes_twice():
    """An event can reach the hub before the response that says so reaches
    the client, and carries nothing a hub could deduplicate on; the caller's
    outbox owns the retry. The dead socket is still dropped."""
    dead = _client(emit=lambda *a, **k: (_ for _ in ()).throw(ConnectionError("closed")))
    live = _client()
    clients = [dead, live]
    session = _session(lambda: clients.pop(0))
    with pytest.raises(ConnectionError):
        session.emit("custos.shadow.event", {"seq": 1})
    assert dead.closed == 1 and not session.held
    session.emit("custos.shadow.event", {"seq": 2})
    assert live.emits == [("custos.shadow.event", {"seq": 2})]


def test_a_subscription_the_client_refuses_is_not_queued_and_a_bad_client_is_not_kept():
    refusing = _client(on=lambda name, handler: (_ for _ in ()).throw(RuntimeError("no such event")))
    session = _session(lambda: refusing)
    session._ensure()
    with pytest.raises(RuntimeError):
        session.on("custos.shadow.request", lambda event: None)
    assert session._subscriptions == [], "a refused registration is not replayed on every client after"
    # and a replay that fails on a fresh client closes it and backs off
    session = _session(lambda: refusing)
    session._subscriptions.append(("custos.shadow.request", lambda event: None))
    with pytest.raises(RuntimeError):
        session._ensure()
    assert refusing.closed == 1 and not session.held and session.retry_at > 0


def test_overlapping_origin_blocks_leave_the_resolver_as_it_was():
    import threading
    real = socket.getaddrinfo
    inside = threading.Event()
    release = threading.Event()

    def first():
        with preferred_origin("hub.example.invalid", "127.0.0.1"):
            inside.set()
            release.wait(5)

    worker = threading.Thread(target=first, daemon=True)
    worker.start()
    assert inside.wait(5)
    # the second block waits for the first rather than stacking a wrapper
    with_second = []

    def second():
        with preferred_origin("hub.example.invalid", "127.0.0.2"):
            with_second.append(socket.getaddrinfo("hub.example.invalid", 443)[0][4][0])

    other = threading.Thread(target=second, daemon=True)
    other.start()
    other.join(0.3)
    assert other.is_alive(), "the second block is queued behind the first"
    release.set()
    worker.join(5)
    other.join(5)
    assert with_second == ["127.0.0.2"]
    assert socket.getaddrinfo is real


def test_a_stale_client_is_replaced_before_the_call_not_after_the_timeout():
    stale, fresh = _client("closed"), _client()
    clients = [stale, fresh]
    session = _session(lambda: clients.pop(0))
    session._ensure()
    assert session.ask("hi").text == "ok"
    assert stale.asks == [] and fresh.asks == [("hi", {})] and stale.closed == 1


def test_hub_hostname_reads_a_url_or_a_bare_host():
    assert hub_hostname("wss://abc.thalovant.io/") == "abc.thalovant.io"
    assert hub_hostname("abc.thalovant.io:443/x") == "abc.thalovant.io"
    assert hub_hostname("") == "" and hub_hostname(None) == ""


def test_preferred_origin_redirects_one_host_for_the_block_only():
    resolved = socket.getaddrinfo("localhost", 80)
    with preferred_origin("hub.example.invalid", "127.0.0.1"):
        inside = socket.getaddrinfo("hub.example.invalid", 443)
        assert inside and inside[0][4][0] == "127.0.0.1"
        assert socket.getaddrinfo("localhost", 80) == resolved, "other hosts are untouched"
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("hub.example.invalid", 443)


def test_the_origin_is_tried_first_and_cooled_down_after_it_fails():
    clock = [0.0]
    built = []

    def build(handshake):
        client = _client(handshake=handshake, connects=[])
        client.connect = lambda timeout=None: client.connects.append(timeout) or (
            (_ for _ in ()).throw(TimeoutError("no HELLO")) if client.handshake == 1.5 else None)
        built.append(client)
        return client

    origin = OriginPreference("10.0.0.5", handshake_seconds=1.5, cooldown_seconds=300.0, clock=lambda: clock[0])
    client = origin.connect(build, host="hub.example.invalid", connect_timeout=6.0, handshake_seconds=3.0)
    assert [c.handshake for c in built] == [1.5, 3.0], "the short leg first, its own budget, then the public path"
    assert built[0].closed == 1 and client is built[1] and origin.cooling_down
    # inside the cooldown the short path is not even tried
    client = origin.connect(build, host="hub.example.invalid", connect_timeout=6.0, handshake_seconds=3.0)
    assert [c.handshake for c in built] == [1.5, 3.0, 3.0]
    clock[0] = 301.0
    assert not origin.cooling_down


def test_a_session_survives_a_connect_that_cannot_be_built(caplog):
    def broken():
        raise TypeError("bad transport kwargs")

    session = _session(broken)
    with pytest.raises(TypeError):
        session._ensure()
    assert session.retry_at > 0, "even a wiring fault backs off the probe"


@pytest.mark.parametrize("kwargs", [dict(retry_seconds=0), dict(probe_seconds=-1),
    dict(retry_ceiling_seconds=float("inf")), dict(probe_down_seconds=float("nan")),
    dict(retry_seconds=20, retry_ceiling_seconds=10)])
def test_policy_rejects_unbounded_or_busy_loop_values(kwargs):
    with pytest.raises(ValueError):
        HubSessionPolicy(**kwargs)


def test_closed_session_cannot_be_reopened_by_a_queued_warm_or_probe():
    client = _client()
    session = _session(lambda: client)
    session.ask("hi")
    session.close()
    session.warm()
    session.probe()
    assert not session.held and client.closed == 1
    with pytest.raises(Exception, match="Hub session is closed"):
        session.ask("again")
    with pytest.raises(Exception, match="Hub session is closed"):
        session.on("event", lambda _: None)


def test_close_waits_for_the_admitted_call_without_dropping_its_client():
    entered, release, closing = threading.Event(), threading.Event(), threading.Event()
    client = _client()
    def ask(text, **kwargs):
        entered.set()
        assert release.wait(5)
        assert client.closed == 0
        return "done"
    client.ask = ask
    session = _session(lambda: client)
    replies = []
    caller = threading.Thread(target=lambda: replies.append(session.ask("hi")))
    caller.start()
    assert entered.wait(5)
    def close():
        closing.set()
        session.close()
    closer = threading.Thread(target=close)
    closer.start()
    assert closing.wait(5)
    release.set()
    caller.join(5); closer.join(5)
    assert not caller.is_alive() and not closer.is_alive()
    assert replies == ["done"] and client.closed == 1 and not session.held


def test_failed_cleanup_retains_the_old_client_before_another_connect():
    attempts = []
    old = _client(ask=lambda *_a, **_k: (_ for _ in ()).throw(ConnectionError("lost response")))
    closes = []
    def close():
        closes.append(None)
        if len(closes) < 3:
            raise ConnectionError("cleanup refused")
    old.close = close
    fresh = _client()
    def connect():
        attempts.append(None)
        return old if len(attempts) == 1 else fresh
    session = _session(connect)
    with pytest.raises(ConnectionError):
        session.ask("action")
    with pytest.raises(ConnectionError):
        session.ask("status")
    assert len(attempts) == 1, "failed retirement cannot authorize a replacement"
    assert session.ask("status").text == "ok"
    assert len(attempts) == 2 and len(closes) == 3
    session.close()


def test_shutdown_rejects_new_subscriptions_before_the_active_call_finishes():
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    client = _client()
    client.ask = lambda *_a, **_k: entered.set() or release.wait(5)
    session = _session(lambda: client)
    real = session._busy
    class Admission:
        def __enter__(self):
            if threading.current_thread().name == "session-closer":
                waiting.set()
            real.acquire()
        def __exit__(self, *_args):
            real.release()
    session._busy = Admission()
    caller = threading.Thread(target=lambda: session.ask("active"))
    closer = threading.Thread(target=session.close, name="session-closer")
    try:
        caller.start(); assert entered.wait(5)
        closer.start(); assert waiting.wait(5)
        with pytest.raises(Exception, match="Hub session is closed"):
            session.on("new", lambda *_: None)
        assert client.closed == 0
    finally:
        release.set(); caller.join(5); closer.join(5)
    assert client.closed == 1


@pytest.mark.parametrize("address", ["", "127.0.0.1"])
def test_origin_public_failures_retire_clients_and_failed_cleanup_blocks_replacement(address):
    origin = OriginPreference(address)
    old = _client()
    old.connect = lambda **_: (_ for _ in ()).throw(ConnectionError("failed public dial"))
    attempts, closes = [], []
    def close():
        closes.append(None)
        if len(closes) < 3:
            raise OSError("retirement pending")
    old.close = close
    def build(_handshake):
        attempts.append(None)
        return old
    with pytest.raises(OSError):
        origin.connect(build, host="hub.invalid", connect_timeout=1)
    with pytest.raises(OSError):
        origin.connect(build, host="hub.invalid", connect_timeout=1)
    assert len(attempts) == 1
    origin.close()
    assert len(closes) == 3


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_origin_rejects_unbounded_or_busy_loop_budgets(value):
    with pytest.raises(ValueError):
        OriginPreference("127.0.0.1", handshake_seconds=value)
    with pytest.raises(ValueError):
        OriginPreference("127.0.0.1", cooldown_seconds=value)


def test_hub_hostname_handles_ipv6_and_invalid_urls():
    assert hub_hostname("[::1]:443/path") == "::1"
    assert hub_hostname("wss://[broken") == ""
