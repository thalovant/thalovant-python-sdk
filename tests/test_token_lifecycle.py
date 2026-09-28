"""Which token an SDK revokes by default, and what revoking twice does.

``token_id`` must always name the token in ``access_token``: every sign-in
sets it from its own answer. Otherwise a password sign-in after a device
login leaves the device token's id behind, and the next default revoke
revokes a token the client no longer uses while the one it does use lives on.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from thalovant import AsyncThalovantControlPlane, ThalovantAPIError, ThalovantAuthError, ThalovantControlPlane

DEVICE_TOKEN = {"access_token": "synthetic-device-token", "token_type": "bearer", "token_id": "device-token-id",
                "scopes": ["hubs:read"]}
NATIVE_TOKEN = {"access_token": "synthetic-native-token", "token_type": "bearer", "token_id": "native-token-id"}
SESSION_TOKEN = {"access_token": "synthetic-session-token", "token_type": "bearer"}


class Api:
    def __init__(self, revoke_status: int = 204) -> None:
        self.revoke_status = revoke_status
        self.revoked: list[tuple[str, str | None]] = []
        self.server: TestServer | None = None

    async def __aenter__(self) -> Api:
        def answer(token: dict[str, Any]) -> Any:
            async def handler(_request: web.Request) -> web.Response:
                return web.json_response(token)
            return handler

        app = web.Application()
        app.router.add_post("/v1/auth/device/token", answer(DEVICE_TOKEN))
        app.router.add_post("/v1/auth/token", answer(SESSION_TOKEN))
        app.router.add_post("/v1/auth/native/token", answer(NATIVE_TOKEN))
        async def revoke(request: web.Request) -> web.Response:
            return await self.revoke(request)  # looked up per request, so a test can replace it

        app.router.add_delete("/v1/auth/api-tokens/{token_id}", revoke)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()
        return self

    async def __aexit__(self, *_: Any) -> None:
        assert self.server is not None
        await self.server.close()

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.port}"

    async def revoke(self, request: web.Request) -> web.Response:
        self.revoked.append((request.match_info["token_id"], request.headers.get("Authorization")))
        if self.revoke_status == 204:
            return web.Response(status=204)
        body = {"type": "about:blank", "title": "HTTPException", "status": self.revoke_status,
                "detail": "Could not validate credentials" if self.revoke_status == 401 else "API token not found"}
        return web.Response(status=self.revoke_status, text=json.dumps(body), content_type="application/problem+json")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_a_password_sign_in_clears_the_device_tokens_id():
    async def exercise():
        async with Api() as api, AsyncThalovantControlPlane(api.url) as plane:
            await plane.poll_device_login("synthetic-device-code")
            assert plane.token_id == "device-token-id"
            await plane.login("someone@example.invalid", "synthetic-password")
            assert plane.access_token == "synthetic-session-token"
            assert plane.token_id is None
            # The device token is not what this client holds any more: no
            # default revoke may reach it.
            with pytest.raises(ThalovantAPIError, match="No API token id"):
                await plane.revoke_api_token()
            assert api.revoked == []

    run(exercise())


def test_a_native_sign_in_sets_its_own_tokens_id():
    async def exercise():
        async with Api() as api, AsyncThalovantControlPlane(api.url) as plane:
            await plane.poll_device_login("synthetic-device-code")
            await plane.complete_native_sign_in("code", "verifier", "client", "app://callback")
            assert plane.access_token == "synthetic-native-token"
            assert plane.token_id == "native-token-id"
            await plane.revoke_api_token()
            assert api.revoked == [("native-token-id", "Bearer synthetic-native-token")]

    run(exercise())


def test_the_sync_control_plane_resets_the_id_too():
    async def exercise():
        async with Api() as api:
            def sync_part():
                with ThalovantControlPlane(api.url) as plane:
                    plane.poll_device_login("synthetic-device-code")
                    assert plane.token_id == "device-token-id"
                    plane.login("someone@example.invalid", "synthetic-password")
                    assert plane.token_id is None

            await asyncio.to_thread(sync_part)

    run(exercise())


def test_revoking_ones_own_dead_token_succeeds_and_forgets_it():
    async def exercise():
        async with Api(revoke_status=401) as api, AsyncThalovantControlPlane(api.url) as plane:
            await plane.poll_device_login("synthetic-device-code")
            await plane.revoke_api_token()
            assert plane.access_token is None and plane.token_id is None
            await plane.revoke_api_token()  # again: nothing to send, nothing wrong
            assert api.revoked == [("device-token-id", "Bearer synthetic-device-token")]
            # A new sign-in ends the no-op: there is a token to revoke again.
            await plane.login("someone@example.invalid", "synthetic-password")
            with pytest.raises(ThalovantAPIError, match="No API token id"):
                await plane.revoke_api_token()

    run(exercise())


@pytest.mark.parametrize("status", [401, 404])
def test_revoking_another_token_keeps_the_apis_answer(status):
    async def exercise():
        async with Api(revoke_status=status) as api, AsyncThalovantControlPlane(api.url) as plane:
            await plane.poll_device_login("synthetic-device-code")
            with pytest.raises(ThalovantAPIError) as caught:
                await plane.revoke_api_token("some-other-token-id")
            assert caught.value.status_code == status
            assert (status == 401) == isinstance(caught.value, ThalovantAuthError)
            assert plane.token_id == "device-token-id" and plane.access_token == "synthetic-device-token"

    run(exercise())


def test_a_sign_in_during_a_revoke_keeps_its_new_token():
    """The revoke of the old token must not forget the one a sign-in installed meanwhile."""

    async def exercise():
        async with Api() as api:
            gate = asyncio.Event()
            original = api.revoke

            async def slow_revoke(request):
                await gate.wait()
                return await original(request)

            api.revoke = slow_revoke  # type: ignore[method-assign]
            async with AsyncThalovantControlPlane(api.url) as plane:
                await plane.poll_device_login("synthetic-device-code")
                revoking = asyncio.ensure_future(plane.revoke_api_token())
                await asyncio.sleep(0.05)  # the DELETE is on its way
                await plane.complete_native_sign_in("code", "verifier", "client", "app://callback")
                gate.set()
                await revoking
                assert api.revoked == [("device-token-id", "Bearer synthetic-device-token")]
                assert plane.access_token == "synthetic-native-token"
                assert plane.token_id == "native-token-id"

    run(exercise())


@pytest.mark.parametrize("body", ["[1, 2]", '"text"', '{"token_type": "bearer"}', "not json"])
def test_an_unusable_2xx_token_answer_reads_the_same_whatever_it_holds(body):
    """A 2xx the SDK cannot use is a local failure with no status, whether its
    body is not an object or is an object with no token (device-login-vectors
    records the second with status null)."""

    async def exercise():
        async def answer(_request):
            return web.Response(status=200, text=body, content_type="application/json")

        app = web.Application()
        app.router.add_post("/v1/auth/device/token", answer)
        server = TestServer(app, host="127.0.0.1")
        await server.start_server()
        try:
            async with AsyncThalovantControlPlane(f"http://127.0.0.1:{server.port}") as plane:
                with pytest.raises(ThalovantAPIError) as caught:
                    await plane.poll_device_login("synthetic-device-code")
                return caught.value
        finally:
            await server.close()

    error = run(exercise())
    assert error.status_code is None and error.problem is None
