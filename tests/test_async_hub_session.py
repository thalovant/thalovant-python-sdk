"""AsyncHubSession and the home link, end to end against an in-process hub."""

from __future__ import annotations

import asyncio
import logging

import aiohttp
import pytest

from fake_hub import FakeHub, speak_back
from thalovant import (
    AsyncHubSession,
    HomeAnswer,
    HubSessionPolicy,
    ThalovantConnectionError,
    answer_home_requests,
)
from thalovant.errors import ThalovantHubRefusedError

FAST = HubSessionPolicy(retry_seconds=0.05, retry_ceiling_seconds=0.2, probe_seconds=0.05,
                        probe_down_seconds=0.05, refusal_grace_seconds=0.4)


async def _hub() -> FakeHub:
    hub = FakeHub()
    hub.responder = speak_back
    await hub.start()
    return hub


def _session(hub, record, tmp_path, **kwargs):
    kwargs.setdefault("policy", FAST)
    return AsyncHubSession.for_identity(
        hub.identity(record), noise_state_dir=str(tmp_path / "noise"), reply_settle_seconds=0.05,
        settle_seconds=kwargs.pop("settle_seconds", 0.1), **kwargs,
    )


async def _eventually(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "condition never became true"
        await asyncio.sleep(0.01)


def test_run_keeps_the_link_connect_opened_and_logs_only_at_debug(tmp_path, caplog):
    async def exercise():
        hub = await _hub()
        record = hub.register()
        session = _session(hub, record, tmp_path)
        states: list[bool] = []
        session.on_state_change(states.append)
        try:
            await session.connect()
            assert session.connected and hub.attempts == 1
            runner = asyncio.ensure_future(session.run())
            await asyncio.sleep(0.2)
            # The link connect() opened is the one run() keeps.
            assert hub.attempts == 1 and session.connected
            reply = await session.ask("hello", timeout=5)
            assert reply.text == "You said hello"
            # A drop: run() notices and dials again, on its ladder.
            await hub.drop_all()
            await _eventually(lambda: hub.attempts == 2 and session.connected)
            assert states[:3] == [True, False, True]
            await session.close()
            await asyncio.wait_for(runner, 2)
        finally:
            await session.close()
            await hub.stop()

    with caplog.at_level(logging.DEBUG, logger="thalovant.session"):
        asyncio.run(exercise())
    session_records = [r for r in caplog.records if r.name == "thalovant.session"]
    assert session_records, "every attempt is logged"
    assert all(r.levelno == logging.DEBUG for r in session_records)


def test_a_refusal_is_retried_through_the_grace_then_raised(tmp_path):
    async def exercise():
        hub = await _hub()
        record = hub.register()
        identity = hub.identity(record)
        hub.clients.clear()  # the hub has not admitted this connection
        session = AsyncHubSession.for_identity(identity, noise_state_dir=str(tmp_path / "noise"), policy=FAST)
        try:
            with pytest.raises(ThalovantHubRefusedError):
                await asyncio.wait_for(session.run(), 5)
            # More than one attempt: the first refusals were "not admitted yet".
            assert hub.attempts >= 2
        finally:
            await session.close()
            await hub.stop()

    asyncio.run(exercise())


def test_a_refusal_that_clears_inside_the_grace_connects(tmp_path):
    async def exercise():
        hub = await _hub()
        record = hub.register()
        identity = hub.identity(record)
        hub.admit = False
        session = AsyncHubSession.for_identity(
            identity, noise_state_dir=str(tmp_path / "noise"),
            policy=HubSessionPolicy(retry_seconds=0.05, retry_ceiling_seconds=0.1, probe_seconds=0.05,
                                    probe_down_seconds=0.05, refusal_grace_seconds=30),
        )
        runner = asyncio.ensure_future(session.run())
        try:
            await _eventually(lambda: hub.attempts >= 2)
            hub.admit = True  # the hub admits it
            await _eventually(lambda: session.connected)
        finally:
            await session.close()
            await asyncio.wait_for(runner, 2)
            await hub.stop()

    asyncio.run(exercise())


def test_a_hub_that_closes_right_after_the_handshake_is_a_refusal(tmp_path):
    async def exercise():
        hub = await _hub()
        record = hub.register()
        hub.close_after_handshake = True
        session = _session(hub, record, tmp_path, settle_seconds=0.5)
        try:
            with pytest.raises(ThalovantHubRefusedError):
                await session.connect()
            assert not session.held
            hub.close_after_handshake = False
            await session.connect()
            assert session.connected
        finally:
            await session.close()
            await hub.stop()

    asyncio.run(exercise())


def test_an_unreachable_hub_is_a_connection_error(tmp_path):
    async def exercise():
        hub = await _hub()
        record = hub.register()
        identity = hub.identity(record)
        await hub.stop()
        session = AsyncHubSession.for_identity(identity, noise_state_dir=str(tmp_path / "noise"),
                                              policy=FAST, connect_timeout=1)
        try:
            with pytest.raises(ThalovantConnectionError) as caught:
                await session.connect()
            assert not isinstance(caught.value, ThalovantHubRefusedError)
        finally:
            await session.close()

    asyncio.run(exercise())


def test_home_requests_are_answered_back_along_their_route_over_the_callers_session(tmp_path):
    async def exercise():
        hub = await _hub()
        record = hub.register()
        async with aiohttp.ClientSession() as http:
            session = AsyncHubSession.for_identity(
                hub.identity(record), session=http, noise_state_dir=str(tmp_path / "noise"),
                policy=FAST, settle_seconds=0.05,
            )
            heard: list[str] = []

            async def handler(request):
                heard.append(request.utterance)
                return HomeAnswer(speech="<speak>Turned off the kitchen light.</speak>",
                                  response_type="action_done")

            stop = answer_home_requests(session, handler)
            try:
                await session.connect()
                await _eventually(lambda: hub.sessions)
                hub_session = hub.sessions[0]
                await hub_session.send_bus(
                    "thalovant.home.request",
                    {"request_id": "r1", "utterance": "turn off the kitchen light", "lang": "en-US"},
                    {"source": "thalovant-skill-home", "destination": ["ha-peer"], "session": {"session_id": "kitchen"}},
                )
                message = await asyncio.wait_for(hub_session.received.get(), 5)
                payload = message["payload"]
                assert heard == ["turn off the kitchen light"]
                assert payload["type"] == "thalovant.home.response"
                assert payload["data"] == {"request_id": "r1", "speech": "Turned off the kitchen light.",
                                           "response_type": "action_done", "continue_conversation": False}
                assert payload["context"]["destination"] == "thalovant-skill-home"
                assert payload["context"]["source"] == "ha-peer"
                assert payload["context"]["session"]["session_id"] == "kitchen"
            finally:
                stop()
                await session.close()
            assert not http.closed, "the caller's session is the caller's"
        await hub.stop()

    asyncio.run(exercise())


def test_subscriptions_follow_every_client_the_session_builds(tmp_path):
    async def exercise():
        hub = await _hub()
        record = hub.register()
        session = _session(hub, record, tmp_path)
        seen: list[str] = []
        unsubscribe = session.on("hub.says", lambda event: seen.append(event.data["word"]))
        runner = asyncio.ensure_future(session.run())
        try:
            await _eventually(lambda: session.connected and hub.sessions)
            await hub.sessions[0].send_bus("hub.says", {"word": "one"})
            await _eventually(lambda: seen == ["one"])
            await hub.drop_all()
            await _eventually(lambda: hub.attempts == 2 and session.connected and hub.sessions)
            await hub.sessions[0].send_bus("hub.says", {"word": "two"})
            await _eventually(lambda: seen == ["one", "two"])
            unsubscribe()
            await hub.sessions[0].send_bus("hub.says", {"word": "three"})
            await asyncio.sleep(0.1)
            assert seen == ["one", "two"]
        finally:
            await session.close()
            await asyncio.wait_for(runner, 2)
            await hub.stop()

    asyncio.run(exercise())


def test_a_reply_withdrawn_while_queued_leaves_the_link_up(tmp_path):
    """A home reply still waiting behind another frame when the hub's bound
    passes is withdrawn whole -- never sent late, and the link stays up."""
    from thalovant import AsyncThalovantClient
    from thalovant.home import answer_home_request
    from thalovant.events import ThalovantEvent

    async def exercise():
        hub = await _hub()
        record = hub.register()
        client = AsyncThalovantClient(hub.identity(record), noise_state_dir=str(tmp_path / "noise"),
                                      reply_settle_seconds=0.05)
        try:
            await client.connect()
            await _eventually(lambda: hub.sessions)
            session = hub.sessions[0]
            transport = client._link.transport
            if transport._send_lock is None:
                transport._send_lock = asyncio.Lock()
            await transport._send_lock.acquire()  # another frame is being written
            event = ThalovantEvent(name="thalovant.home.request",
                                   data={"request_id": "q1", "utterance": "lights"},
                                   context={"source": "skill", "destination": "ha"}, raw=None)
            sent = await answer_home_request(client, event, lambda _request: HomeAnswer(speech="Done."),
                                             hub_timeout=0.2)
            assert sent is None  # withdrawn at the bound
            transport._send_lock.release()
            await asyncio.sleep(0.2)
            received = []
            while not session.received.empty():
                received.append(session.received.get_nowait()["payload"].get("type"))
            assert "thalovant.home.response" not in received  # never sent late
            reply = await client.ask("still there", timeout=5)
            assert reply.text == "You said still there"
            assert hub.attempts == 1  # the same link all along
        finally:
            await client.close()
            await hub.stop()

    asyncio.run(exercise())
