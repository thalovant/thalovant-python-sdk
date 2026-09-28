"""The WebSocket data plane end to end, against an in-process HiveMind hub.

No mocks between the client and the socket: the real handshake, the Noise
session, the hub's framing, the reconnect on a dropped link, the refusal a hub
gives an unknown key.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import thalovant.client as client_module
from fake_hub import FakeHub, HubThread, speak_back
from thalovant import (
    AsyncThalovantClient,
    HubSession,
    ThalovantClient,
    ThalovantConnectionError,
    ThalovantTimeoutError,
)
from thalovant._wire import HiveMessage, encode_binary_frame
from thalovant.errors import ThalovantHubRefusedError
from thalovant.session import alive, preferred_origin


@pytest.fixture
def hub():
    running = HubThread()
    running.hub.responder = speak_back
    yield running
    running.close()


def _client(hub, tmp_path, record=None, **kwargs):
    record = record or hub.hub.register()
    kwargs.setdefault("reply_settle_seconds", 0.05)
    return ThalovantClient(hub.hub.identity(record), noise_state_dir=str(tmp_path / "noise"), **kwargs)


def test_ask_answers_over_a_real_noise_session_and_pins_the_hub(hub, tmp_path):
    record = hub.hub.register()
    client = _client(hub, tmp_path, record)
    try:
        reply = client.ask("hello there", timeout=5)
        assert reply.text == "You said hello there"
        assert reply.handled
        assert client.connection_info().phase == "ready"
        assert client.healthcheck().ok
        client.close()
        # Pinned both ways: the second session is KK, with no password stretch.
        reply = client.ask("again", timeout=5)
        assert reply.text == "You said again"
        assert hub.hub.patterns_chosen == ["XXpsk2", "KKpsk0"]
    finally:
        client.close()


def test_the_async_client_is_the_same_conversation(tmp_path):
    async def exercise():
        hub = FakeHub()
        hub.responder = speak_back
        await hub.start()
        record = hub.register()
        client = AsyncThalovantClient(
            hub.identity(record), noise_state_dir=str(tmp_path / "noise"), reply_settle_seconds=0.05
        )
        try:
            reply = await client.ask("from a loop", timeout=5)
            assert reply.text == "You said from a loop"
            seen: list[str] = []
            client.on("hub.says", lambda event: seen.append(event.data["word"]))
            session = hub.sessions[0]
            await session.send_bus("hub.says", {"word": "hi"})
            for _ in range(100):
                if seen:
                    break
                await asyncio.sleep(0.01)
            assert seen == ["hi"]
        finally:
            await client.close()
            await hub.stop()

    asyncio.run(exercise())


def test_a_sync_handler_runs_off_the_loop_and_may_call_the_client_mid_turn(hub, tmp_path):
    """thalovant-voice sets the volume from a handler while its main thread
    waits in ask(). The handler's own emit must not wait for the ask."""
    client = _client(hub, tmp_path, reply_settle_seconds=0.3)
    handled = threading.Event()
    threads: list[str] = []
    try:
        client.connect()

        def on_volume(event):
            threads.append(threading.current_thread().name)
            client.emit("mycroft.volume.set.confirm", {"level": event.data["level"]})
            handled.set()

        client.on("mycroft.volume.set", on_volume)

        async def mid_turn(session, message):
            payload = message.get("payload") or {}
            if payload.get("type") == "recognizer_loop:utterance":
                context = payload.get("context") or {}
                await session.send_bus("mycroft.volume.set", {"level": 7}, context)
                await session.send_bus("speak", {"utterance": "Volume set."}, context)
                await session.send_bus("ovos.utterance.handled", {}, context)

        hub.hub.responder = mid_turn
        started = time.monotonic()
        reply = client.ask("louder", timeout=5)
        assert reply.text == "Volume set."
        assert handled.wait(1)
        assert time.monotonic() - started < 1.5
        assert threads == ["thalovant-handlers"]
        session = hub.hub.sessions[0]

        def confirmed():
            items = []
            while not session.received.empty():
                items.append(session.received.get_nowait())
            return items

        for _ in range(100):
            messages = hub.call(lambda: asyncio.sleep(0, result=confirmed()))
            if any(m["payload"].get("type") == "mycroft.volume.set.confirm" for m in messages):
                break
            time.sleep(0.01)
        else:
            pytest.fail("the handler's emit never reached the hub")
    finally:
        client.close()


def test_an_unknown_key_is_a_refusal_not_a_network_fault(hub, tmp_path):
    record = hub.hub.register()
    identity = hub.hub.identity(record)
    hub.hub.clients.clear()
    client = ThalovantClient(identity, noise_state_dir=str(tmp_path / "noise"), auto_reconnect=False)
    try:
        with pytest.raises(ThalovantHubRefusedError) as caught:
            client.connect(timeout=5)
        # Still a connection error, and still says "connect": callers that
        # classify by message keep working.
        assert isinstance(caught.value, ThalovantConnectionError)
        assert "connect" in str(caught.value)
    finally:
        client.close()


def test_a_hub_that_never_says_hello_times_out_and_leaves_nothing_behind(hub, tmp_path):
    hub.hub.silent = True
    client = _client(hub, tmp_path, handshake_timeout=0.3, connect_timeout=1)
    try:
        started = time.monotonic()
        with pytest.raises((ThalovantTimeoutError, ThalovantConnectionError)) as caught:
            client.connect()
        assert time.monotonic() - started < 2
        assert "handshake" in str(caught.value) or "did not complete" in str(caught.value)
        client.wait_closed(timeout=2)
        transport = client._core._link.transport
        assert transport._carrier is None and not transport._connecting
    finally:
        client.close()


def test_close_during_a_silent_handshake_returns_promptly(hub, tmp_path):
    hub.hub.silent = True
    client = _client(hub, tmp_path, handshake_timeout=30, connect_timeout=30)
    errors: list[BaseException] = []

    def connect():
        try:
            client.connect()
        except BaseException as error:  # checked below
            errors.append(error)

    thread = threading.Thread(target=connect)
    thread.start()
    time.sleep(0.3)
    started = time.monotonic()
    client.close(timeout=5)
    thread.join(5)
    assert not thread.is_alive()
    assert time.monotonic() - started < 2
    assert errors and isinstance(errors[0], ThalovantConnectionError)


def test_a_dropped_link_reads_as_dead_and_the_next_ask_reconnects(hub, tmp_path):
    client = _client(hub, tmp_path)
    try:
        assert client.ask("one", timeout=5).text == "You said one"
        hub.call(hub.hub.drop_all)
        for _ in range(200):
            if not alive(client):
                break
            time.sleep(0.01)
        assert not alive(client)
        assert client.ask("two", timeout=5).text == "You said two"
        assert hub.hub.attempts == 2
    finally:
        client.close()


def test_a_listener_gets_its_link_back_after_the_hub_drops_it(hub, tmp_path, monkeypatch):
    """0.8.7's WebSocket library redialled a dropped link by itself, and a
    client that only listens relied on that after a hub restart."""
    monkeypatch.setattr(client_module, "_REDIAL_FIRST_SECONDS", 0.05)
    client = _client(hub, tmp_path)
    seen: list[str] = []
    got = threading.Event()
    try:
        client.connect()
        client.on("hub.says", lambda event: (seen.append(event.data["word"]), got.set()))
        hub.call(hub.hub.drop_all)
        deadline = time.monotonic() + 5
        while not got.is_set():
            assert time.monotonic() < deadline, "the link never came back"
            if hub.hub.attempts >= 2 and hub.hub.sessions:
                hub.call(hub.hub.sessions[-1].send_bus, "hub.says", {"word": "back"})
            got.wait(0.05)
        assert set(seen) == {"back"}
        assert alive(client)
    finally:
        client.close()


def test_close_stops_the_redial(hub, tmp_path, monkeypatch):
    monkeypatch.setattr(client_module, "_REDIAL_FIRST_SECONDS", 0.1)
    client = _client(hub, tmp_path)
    client.connect()
    hub.call(hub.hub.drop_all)
    client.close()
    time.sleep(0.5)
    assert hub.hub.attempts == 1


def test_without_auto_reconnect_a_dropped_link_stays_down(hub, tmp_path, monkeypatch):
    monkeypatch.setattr(client_module, "_REDIAL_FIRST_SECONDS", 0.05)
    client = _client(hub, tmp_path, auto_reconnect=False)
    try:
        client.connect()
        hub.call(hub.hub.drop_all)
        time.sleep(0.5)
        assert hub.hub.attempts == 1
        assert not alive(client)
    finally:
        client.close()


def test_hub_session_rebuilds_after_a_drop(hub, tmp_path):
    record = hub.hub.register()
    identity = hub.hub.identity(record)

    def build():
        client = ThalovantClient(identity, noise_state_dir=str(tmp_path / "noise"), reply_settle_seconds=0.05)
        client.connect(timeout=5)
        return client

    session = HubSession(build, warm=False)
    try:
        assert session.ask("first", timeout=5).text == "You said first"
        hub.call(hub.hub.drop_all)
        time.sleep(0.2)
        session.probe()
        assert session.ask("second", timeout=5).text == "You said second"
    finally:
        session.close()


def test_binary_frames_arrive_whole(hub, tmp_path):
    client = _client(hub, tmp_path)
    seen = []
    arrived = threading.Event()
    try:
        client.connect()
        client.on_binary(lambda frame: (seen.append(frame), arrived.set()))
        session = hub.session()
        meta = b'{"utterance": "Pfffft.", "lang": "fr-FR", "file_name": "a.wav"}'
        audio = bytes(range(256)) * 400  # chunked by Noise: over 64 KiB
        bits = "0110" + "".join(f"{byte:08b}" for byte in audio) + "0000"
        frame = bytes((0x80 | (12 << 1), len(meta))) + meta + int(bits, 2).to_bytes(len(bits) // 8, "big")
        hub.call(session.send_binary, frame)
        assert arrived.wait(5)
        assert seen[0].kind == "tts_audio"
        assert seen[0].utterance == "Pfffft." and seen[0].data == audio
    finally:
        client.close()


def test_a_json_frame_in_the_binary_encoding_is_read_too(hub, tmp_path):
    client = _client(hub, tmp_path)
    got = threading.Event()
    try:
        client.connect()
        client.on("hub.bits", lambda event: got.set())
        session = hub.session()
        encoded = encode_binary_frame(HiveMessage("bus", {"type": "hub.bits", "data": {}, "context": {}}))
        hub.call(session.send_binary, encoded)
        assert got.wait(5)
    finally:
        client.close()


def test_a_reply_goes_back_the_way_the_request_came(hub, tmp_path):
    client = _client(hub, tmp_path)
    try:
        client.connect()
        session = hub.session()
        requests = []
        arrived = threading.Event()
        client.on("thalovant.home.request", lambda event: (requests.append(event), arrived.set()))
        hub.call(session.send_bus, "thalovant.home.request", {"request_id": "r1", "utterance": "lights off"},
                 {"source": "thalovant-skill-home", "destination": ["ha-peer"], "session": {"session_id": "kitchen"}})
        assert arrived.wait(5)
        client.reply(requests[0], "thalovant.home.response", {"request_id": "r1", "speech": "Done."})
        message = hub.call(lambda: asyncio.wait_for(session.received.get(), 5))
        payload = message["payload"]
        assert payload["type"] == "thalovant.home.response"
        assert payload["data"] == {"request_id": "r1", "speech": "Done."}
        assert payload["context"]["destination"] == "thalovant-skill-home"
        assert payload["context"]["source"] == "ha-peer"
        assert payload["context"]["session"]["session_id"] == "kitchen"
    finally:
        client.close()


def test_the_preferred_origin_override_decides_where_the_hub_is_dialled(hub, tmp_path):
    record = hub.hub.register()
    identity = hub.hub.identity(record)
    from dataclasses import replace

    from thalovant.protocols import HubDataPlaneEndpoints

    named = replace(
        identity,
        data_plane_endpoints=HubDataPlaneEndpoints(wss=f"ws://hub.thalovant.invalid:{hub.hub.port}/"),
    )
    client = ThalovantClient(named, noise_state_dir=str(tmp_path / "noise"), reply_settle_seconds=0.05)
    try:
        with pytest.raises(ThalovantConnectionError):
            client.connect(timeout=3)
        client.close()
        with preferred_origin("hub.thalovant.invalid", "127.0.0.1"):
            client.connect(timeout=5)
        assert client.ask("via the LAN", timeout=5).text == "You said via the LAN"
    finally:
        client.close()
