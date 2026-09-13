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


def test_the_ask_goes_through_the_client_and_retries_once_on_a_dead_socket():
    calls = []

    class Client:
        def __init__(self, fail_once):
            self.fail_once = fail_once

        def connection_info(self):
            return SimpleNamespace(phase="ready")

        def ask(self, text, **kwargs):
            calls.append((self, text, kwargs))
            if self.fail_once:
                self.fail_once = False
                raise ConnectionError("socket closed")
            return SimpleNamespace(text="ok")

        def on(self, name, handler):
            pass

        def close(self):
            pass

    clients = [Client(fail_once=True), Client(fail_once=False)]
    session = _session(lambda: clients.pop(0))
    reply = session.ask("what time is it", lang="fr-fr", pipeline=["converse"])
    assert reply.text == "ok"
    assert [call[1] for call in calls] == ["what time is it", "what time is it"]
    assert calls[1][2] == {"lang": "fr-fr", "pipeline": ["converse"]}
    assert calls[0][0] is not calls[1][0], "the dead client was replaced, not retried"


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


def test_emit_shares_the_ask_policy():
    dead = _client(emit=lambda *a, **k: (_ for _ in ()).throw(ConnectionError("closed")))
    live = _client()
    clients = [dead, live]
    session = _session(lambda: clients.pop(0))
    session.emit("custos.shadow.event", {"seq": 1})
    assert dead.closed == 1 and live.emits == [("custos.shadow.event", {"seq": 1})]


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
