"""No read in an admission wait runs past the wait's own deadline."""

from __future__ import annotations

import asyncio
import time

from aiohttp import web
from aiohttp.test_utils import TestServer

from thalovant import AsyncThalovantControlPlane, ThalovantAdmissionTimeoutError


def test_a_slow_operation_read_is_cut_at_the_deadline() -> None:
    async def exercise() -> float:
        async def slow(_request: web.Request) -> web.Response:
            await asyncio.sleep(5)
            return web.json_response({"id": "op-1", "status": "ready"})

        app = web.Application()
        app.router.add_get("/v1/operations/{operation_id}", slow)
        server = TestServer(app, host="127.0.0.1")
        await server.start_server()
        try:
            # The per-request timeout (10 s) is longer than the whole wait.
            async with AsyncThalovantControlPlane(f"http://127.0.0.1:{server.port}", access_token="synthetic") as api:
                started = time.monotonic()
                try:
                    await api.wait_for_admission({"id": "op-1"}, timeout=0.3, poll_interval=0.05)
                except ThalovantAdmissionTimeoutError as error:
                    assert str(error).endswith("it may still admit it later.")
                    return time.monotonic() - started
                raise AssertionError("the wait did not time out")
        finally:
            await server.close()

    assert asyncio.run(exercise()) < 1.0
