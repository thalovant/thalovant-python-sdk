"""Nothing the SDK does on an application's event loop blocks it.

Home Assistant watches its loop for exactly this: a file opened, a sleep, a
DNS lookup or a slow callback on the loop stalls every integration at once.
A whole conversation -- the first connection with its fresh identity files and
argon2id key, an ask, a reconnect, a close -- runs here on a debug loop whose
blocking calls are recorded when they happen on the loop's own thread.
"""

from __future__ import annotations

import asyncio
import builtins
import io
import logging
import os
import socket
import ssl
import sys
import threading
import time

import pytest

from fake_hub import HubThread, speak_back
from thalovant import AsyncHubSession, AsyncThalovantClient, AsyncThalovantControlPlane, HubSessionPolicy


class LoopWatch:
    """Records the blocking calls made on one thread."""

    def __init__(self) -> None:
        self.thread: int | None = None
        self.calls: list[str] = []
        self.inside = False

    #: Frames that are not the SDK's doing: asyncio's debug mode reading
    #: source lines for its own tracebacks.
    IGNORED = ("linecache.py", "traceback.py", "asyncio/format_helpers.py")

    def wrap(self, name: str, original):
        watch = self

        def guarded(*args, **kwargs):
            if threading.get_ident() == watch.thread and not watch.inside:
                watch.inside = True
                try:
                    frame = sys._getframe(1)
                    stack = []
                    while frame is not None:
                        stack.append((frame.f_code.co_filename, frame.f_lineno))
                        frame = frame.f_back
                    if not any(path.endswith(watch.IGNORED) for path, _ in stack):
                        origin = next(
                            (f"{path.rsplit('/', 1)[-1]}:{line}" for path, line in stack
                             if "/thalovant/" in path or "/aiohttp/" in path),
                            stack[0][0],
                        )
                        watch.calls.append(f"{name} from {origin}")
                finally:
                    watch.inside = False
            return original(*args, **kwargs)

        return guarded


@pytest.fixture
def watch(monkeypatch):
    watched = LoopWatch()
    for owner, name in (
        (builtins, "open"), (io, "open"), (os, "open"), (os, "stat"), (os, "listdir"),
        (time, "sleep"), (socket, "getaddrinfo"), (ssl.SSLContext, "load_verify_locations"),
    ):
        monkeypatch.setattr(owner, name, watched.wrap(f"{getattr(owner, '__name__', owner)}.{name}", getattr(owner, name)))
    return watched


def test_a_conversation_never_blocks_the_loop(watch, tmp_path, caplog):
    hub = HubThread()
    hub.hub.responder = speak_back
    record = hub.hub.register()
    identity = hub.hub.identity(record)

    async def exercise():
        loop = asyncio.get_running_loop()
        loop.set_debug(True)
        loop.slow_callback_duration = 0.1
        watch.thread = threading.get_ident()
        client = AsyncThalovantClient(identity, noise_state_dir=str(tmp_path / "noise"), reply_settle_seconds=0.05)
        try:
            assert (await client.ask("first", timeout=10)).text == "You said first"
            await client.close()
            assert (await client.ask("second", timeout=10)).text == "You said second"
            session = AsyncHubSession.for_identity(
                identity, noise_state_dir=str(tmp_path / "noise"), settle_seconds=0.05,
                policy=HubSessionPolicy(retry_seconds=0.05, retry_ceiling_seconds=0.1,
                                        probe_seconds=0.05, probe_down_seconds=0.05),
            )
            await session.connect()
            await session.close()
        finally:
            await client.close()
            watch.thread = None

    try:
        with caplog.at_level(logging.WARNING, logger="asyncio"):
            asyncio.run(exercise())
    finally:
        hub.close()
    assert watch.calls == [], f"blocking calls on the loop: {sorted(set(watch.calls))}"
    slow = [r.getMessage() for r in caplog.records if r.name == "asyncio" and "took" in r.getMessage()]
    assert slow == [], slow


def test_the_control_plane_never_blocks_the_loop(watch):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def exercise():
        async def hubs(_request):
            return web.json_response({"data": []})

        app = web.Application()
        app.router.add_get("/v1/hubs", hubs)
        server = TestServer(app, host="127.0.0.1")
        await server.start_server()
        loop = asyncio.get_running_loop()
        loop.set_debug(True)
        watch.thread = threading.get_ident()
        plane = AsyncThalovantControlPlane(f"http://127.0.0.1:{server.port}", access_token="synthetic-token")
        try:
            assert await plane.list_hubs() == {"data": []}
        finally:
            watch.thread = None
            await plane.aclose()
            await server.close()

    asyncio.run(exercise())
    assert watch.calls == [], f"blocking calls on the loop: {sorted(set(watch.calls))}"
