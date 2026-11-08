"""Browser sign-in beyond the shared vectors: what is printed and opened, the
generated secrets, the listener's own edges, and what never reaches an error.

``test_browser_login_vectors.py`` runs the contract every SDK shares; these
are the parts that are this SDK's own -- its event loop, its threads, its
stdout -- or that are about what must *not* happen.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import webbrowser
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from thalovant import (
    AsyncThalovantControlPlane,
    ThalovantAPIError,
    ThalovantControlPlane,
    ThalovantDeviceLoginExpired,
    default_device_name,
    native_auth,
    new_state,
    new_verifier,
)
from thalovant._loopback import MAX_REQUEST_HEAD, LoopbackListener

CONFIG = {
    "authorization_endpoint": "https://dash.thalovant.com/authorize",
    "token_endpoint": "/v1/auth/native/token",
    "device_authorization_endpoint": "/v1/auth/device/authorize",
    "sdk_client_id": "thalovant-sdk",
    "loopback_redirect_uri": "http://127.0.0.1:{port}/callback",
    "code_challenge_methods_supported": ["S256"],
}
TOKEN = {"access_token": "tvpat_unit_token", "token_type": "bearer", "scopes": ["hubs:read"],
         "expires_at": "2027-11-08T12:00:00Z", "token_id": "tok-unit", "ai_disclosure": "AI"}


class Api:
    """The config and token routes; the token route answers with *token_answer*."""

    def __init__(self, token_answer: Callable[[dict[str, Any]], web.Response] | None = None) -> None:
        self.token_answer = token_answer or (lambda _: web.json_response(TOKEN))
        self.requests: list[str] = []
        self.server: TestServer | None = None
        self.url = ""

    async def __aenter__(self) -> Api:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()
        self.url = f"http://127.0.0.1:{self.server.port}"
        return self

    async def __aexit__(self, *_: Any) -> None:
        assert self.server is not None
        await self.server.close()

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append(f"{request.method} {request.path}")
        if request.path == "/v1/auth/native/config":
            return web.json_response(CONFIG)
        if request.path == "/v1/auth/native/token":
            return self.token_answer(json.loads(await request.text()))
        return web.Response(status=599)


def parts(url: str) -> tuple[int, str, dict[str, str]]:
    query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    port = urlsplit(query["redirect_uri"]).port
    assert port is not None
    return port, query["state"], query


async def get(port: int, target: str, *, raw: bytes | None = None) -> tuple[int, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(raw if raw is not None else
                     f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        await writer.drain()
        data = await reader.read()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), body.decode()


async def closed(port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return True
    writer.close()
    return False


def opened_url(loop: asyncio.AbstractEventLoop) -> tuple[asyncio.Future[str], Callable[[str], None]]:
    future: asyncio.Future[str] = loop.create_future()

    def hand_over(url: str) -> None:
        loop.call_soon_threadsafe(lambda: future.done() or future.set_result(url))

    return future, hand_over


# -- what the person sees -----------------------------------------------------


def test_the_url_is_printed_and_the_browser_opened_off_the_loop(monkeypatch: pytest.MonkeyPatch,
                                                                capsys: pytest.CaptureFixture[str]) -> None:
    opened: list[tuple[str, str]] = []

    async def run() -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        future, hand_over = opened_url(loop)

        def fake_browser(url: str, *_: Any, **__: Any) -> bool:
            opened.append((url, threading.current_thread().name))
            hand_over(url)
            return True

        monkeypatch.setattr(webbrowser, "open", fake_browser)
        async with Api() as api:
            plane = AsyncThalovantControlPlane(api.url)
            task = asyncio.ensure_future(plane.browser_login(device_name="unit"))
            url = await asyncio.wait_for(future, 5)
            port, state, _ = parts(url)
            assert (await get(port, f"/callback?code=c&state={state}"))[0] == 200
            token = await task
            await plane.aclose()
            return token

    token = asyncio.run(run())
    assert token["access_token"] == "tvpat_unit_token"
    url, thread = opened[0]
    assert thread == "thalovant-browser"
    assert f"Opening your browser to sign in to Thalovant. If it does not open, visit:\n{url}\n" in capsys.readouterr().out


def test_without_open_browser_the_url_is_only_shown(monkeypatch: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(webbrowser, "open", lambda *_a, **_k: pytest.fail("the browser was opened"))

    async def run() -> None:
        # Nobody comes back to the port, so the sign-in expires.
        async with Api() as api:
            plane = AsyncThalovantControlPlane(api.url)
            with pytest.raises(ThalovantDeviceLoginExpired):
                await plane.browser_login(open_browser=False, timeout=0.2)
            await plane.aclose()

    asyncio.run(run())
    out = capsys.readouterr().out
    assert out.startswith("To sign in to Thalovant, visit:\nhttps://dash.thalovant.com/authorize?response_type=code&")


def test_a_browser_that_cannot_open_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: Any, **__: Any) -> bool:
        raise webbrowser.Error("no runnable browser")

    monkeypatch.setattr(webbrowser, "open", broken)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        future, hand_over = opened_url(loop)
        async with Api() as api:
            plane = AsyncThalovantControlPlane(api.url)
            task = asyncio.ensure_future(plane.browser_login(prompt=hand_over))
            port, state, _ = parts(await asyncio.wait_for(future, 5))
            await get(port, f"/callback?code=c&state={state}")
            assert (await task)["token_id"] == "tok-unit"
            await plane.aclose()

    asyncio.run(run())


def test_the_device_name_defaults_to_this_sdk_on_this_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_auth.socket, "gethostname", lambda: "workstation")
    assert default_device_name() == "thalovant-python-sdk on workstation"
    monkeypatch.setattr(native_auth.socket, "gethostname", lambda: "h" * 300)
    assert len(default_device_name()) == 120
    monkeypatch.setattr(native_auth.socket, "gethostname", lambda: "")
    assert default_device_name() == "thalovant-python-sdk"

    def no_name() -> str:
        raise OSError("no hostname")

    monkeypatch.setattr(native_auth.socket, "gethostname", no_name)
    assert default_device_name() == "thalovant-python-sdk"


def test_the_default_device_name_reaches_the_consent_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_auth.socket, "gethostname", lambda: "workstation")

    async def run() -> dict[str, str]:
        loop = asyncio.get_running_loop()
        future, hand_over = opened_url(loop)
        async with Api() as api:
            plane = AsyncThalovantControlPlane(api.url)
            task = asyncio.ensure_future(plane.browser_login(open_browser=False, prompt=hand_over, timeout=0.2))
            _, _, query = parts(await asyncio.wait_for(future, 5))
            with pytest.raises(ThalovantDeviceLoginExpired, match="within 0.2 seconds"):
                await task
            await plane.aclose()
            return query

    assert asyncio.run(run())["device_name"] == "thalovant-python-sdk on workstation"


# -- the secrets ----------------------------------------------------------------


def test_every_verifier_and_state_is_fresh_and_well_formed() -> None:
    verifiers = {new_verifier() for _ in range(50)}
    states = {new_state() for _ in range(50)}
    assert len(verifiers) == len(states) == 50
    assert all(re.fullmatch(r"[A-Za-z0-9\-._~]{43,128}", value) for value in verifiers)
    # 24 random bytes: 192 bits, well over the 128 the contract asks for.
    assert all(re.fullmatch(r"[A-Za-z0-9\-_]{32}", value) for value in states)


def test_an_echoed_code_and_verifier_never_reach_the_error() -> None:
    sent: dict[str, Any] = {}

    def echo(body: dict[str, Any]) -> web.Response:
        # A validation error lists each field's input, as FastAPI's 422 does.
        sent.update(body)
        detail = [{"loc": ["body", key], "msg": "bad", "input": value} for key, value in body.items()]
        return web.json_response({"detail": detail, sent_code_key(body): "x"}, status=422)

    def sent_code_key(body: dict[str, Any]) -> str:
        return f"seen:{body['code']}"  # a secret in a key, too

    async def run() -> ThalovantAPIError:
        loop = asyncio.get_running_loop()
        future, hand_over = opened_url(loop)
        async with Api(echo) as api:
            plane = AsyncThalovantControlPlane(api.url)
            task = asyncio.ensure_future(plane.browser_login(open_browser=False, prompt=hand_over))
            port, state, _ = parts(await asyncio.wait_for(future, 5))
            status, body = await get(port, f"/callback?code=secret-code-value&state={state}")
            assert status == 200 and "Sign-in was not completed." in body
            with pytest.raises(ThalovantAPIError) as raised:
                await task
            await plane.aclose()
            sent["state"] = state
            return raised.value

    error = asyncio.run(run())
    assert error.status_code == 422 and type(error) is ThalovantAPIError
    shown = "\n".join([str(error), repr(error), json.dumps(error.problem), str(error.detail)])
    for secret in (sent["code"], sent["code_verifier"], sent["state"]):
        assert secret not in shown
    assert "[redacted]" in json.dumps(error.problem)
    # Nothing rides along: no cause, and no context holding the unredacted original.
    assert error.__cause__ is None and error.__context__ is None and error.__suppress_context__


def test_a_cleartext_api_is_refused_before_anything_is_asked_or_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webbrowser, "open", lambda *_a, **_k: pytest.fail("the browser was opened"))
    plane = ThalovantControlPlane("http://control.example.test")
    try:
        with pytest.raises(ThalovantAPIError, match="cleartext"):
            plane.browser_login(open_browser=False)
        with pytest.raises(ThalovantAPIError, match="cleartext"):
            plane.interactive_login(method="browser")
    finally:
        plane.close()


def test_interactive_login_refuses_a_method_it_does_not_know() -> None:
    plane = ThalovantControlPlane("http://127.0.0.1:9")
    try:
        with pytest.raises(ValueError, match="auto"):
            plane.interactive_login(method="password")  # type: ignore[arg-type]
    finally:
        plane.close()


# -- the listener's own edges ---------------------------------------------------


def test_cancelling_the_sign_in_closes_the_port() -> None:
    async def run() -> None:
        loop = asyncio.get_running_loop()
        future, hand_over = opened_url(loop)
        async with Api() as api:
            plane = AsyncThalovantControlPlane(api.url)
            task = asyncio.ensure_future(plane.browser_login(open_browser=False, prompt=hand_over))
            port, _, _ = parts(await asyncio.wait_for(future, 5))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await closed(port)
            await plane.aclose()

    asyncio.run(run())


def test_a_connection_that_sends_nothing_does_not_hold_the_sign_in() -> None:
    """A browser opens connections it never uses; the sign-in must not wait on them."""

    async def run() -> float:
        loop = asyncio.get_running_loop()
        future, hand_over = opened_url(loop)
        async with Api() as api:
            plane = AsyncThalovantControlPlane(api.url)
            task = asyncio.ensure_future(plane.browser_login(open_browser=False, prompt=hand_over))
            port, state, _ = parts(await asyncio.wait_for(future, 5))
            _, idle = await asyncio.open_connection("127.0.0.1", port)
            started = time.monotonic()
            assert (await get(port, f"/callback?code=c&state={state}"))[0] == 200
            await task
            took = time.monotonic() - started
            idle.close()
            await plane.aclose()
            return took

    assert asyncio.run(run()) < 0.9


def test_a_request_that_is_not_one_is_400_and_the_wait_goes_on() -> None:
    async def run() -> None:
        loop = asyncio.get_running_loop()
        future, hand_over = opened_url(loop)
        async with Api() as api:
            plane = AsyncThalovantControlPlane(api.url)
            task = asyncio.ensure_future(plane.browser_login(open_browser=False, prompt=hand_over))
            port, state, _ = parts(await asyncio.wait_for(future, 5))
            assert (await get(port, "", raw=b"hello\r\n\r\n"))[0] == 400
            too_big = b"GET /callback?code=" + b"a" * (MAX_REQUEST_HEAD + 10) + b" HTTP/1.1\r\n\r\n"
            assert (await get(port, "", raw=too_big))[0] == 400
            assert not task.done()
            assert (await get(port, f"/callback?code=c&state={state}"))[0] == 200
            assert (await task)["access_token"] == "tvpat_unit_token"
            await plane.aclose()

    asyncio.run(run())


def test_a_second_answer_after_the_first_is_turned_away() -> None:
    async def run() -> None:
        listener = LoopbackListener("the-state-of-this-sign-in")
        port = await listener.start()
        try:
            # Both connected before the first is accepted, so the second still
            # reaches the listener after the port has stopped listening.
            first_reader, first = await asyncio.open_connection("127.0.0.1", port)
            second_reader, second = await asyncio.open_connection("127.0.0.1", port)
            request = b"GET /callback?code=c&state=the-state-of-this-sign-in HTTP/1.1\r\n\r\n"
            first.write(request)
            accepted = await listener.wait(5)
            assert accepted is not None and accepted.code == "c"
            assert await closed(port)
            second.write(request)
            assert (await second_reader.read()).startswith(b"HTTP/1.1 400 ")
            listener.finish(True)
            assert b"Signed in to Thalovant." in await first_reader.read()
            for writer in (first, second):
                writer.close()
        finally:
            await listener.close()

    asyncio.run(run())


def test_the_wait_is_never_cut_short() -> None:
    async def run() -> float:
        listener = LoopbackListener("s")
        await listener.start()
        try:
            started = time.monotonic()
            assert await listener.wait(0.25) is None
            return time.monotonic() - started
        finally:
            await listener.close()

    assert asyncio.run(run()) >= 0.25


def test_waiting_on_a_listener_that_was_never_started_is_an_error() -> None:
    async def run() -> None:
        with pytest.raises(RuntimeError):
            await LoopbackListener("s").wait(0)

    asyncio.run(run())


# -- the remaining edges ----------------------------------------------------------


@pytest.mark.parametrize(("system", "name"), [("darwin", "macos"), ("win32", "windows"), ("linux", "linux"),
                                              ("freebsd14", "freebsd14")])
def test_this_machine_is_named_as_the_browser_rule_names_it(monkeypatch: pytest.MonkeyPatch, system: str,
                                                            name: str) -> None:
    monkeypatch.setattr(native_auth.sys, "platform", system)
    assert native_auth._platform_name() == name


def test_the_browser_rule_reads_this_process_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("CI", "SSH_CONNECTION", "SSH_TTY", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(native_auth, "_platform_name", lambda: "linux")
    assert native_auth.can_open_browser() is False
    monkeypatch.setenv("DISPLAY", ":0")
    assert native_auth.can_open_browser() is True


@pytest.mark.parametrize("endpoint", ["https://dash.thalovant.com/author\x07ize", "https://dash.thalovant.com /authorize",
                                      "https://dash.thalovant.com:99999/authorize", "https://[::1/authorize"])
def test_an_authorization_endpoint_no_browser_can_open_is_unusable(endpoint: str) -> None:
    with pytest.raises(ValueError):
        native_auth.NativeSignInConfig.from_dict({**CONFIG, "authorization_endpoint": endpoint})


def test_a_configuration_that_is_not_an_object_is_unusable() -> None:
    with pytest.raises(ValueError):
        native_auth.NativeSignInConfig.from_dict(["not", "an", "object"])  # type: ignore[arg-type]


@pytest.mark.parametrize("route", ["config", "token"])
def test_an_answer_that_is_not_json_is_an_error_with_no_status(route: str) -> None:
    class Garbled(Api):
        async def handle(self, request: web.Request) -> web.Response:
            if request.path == f"/v1/auth/native/{route}":
                return web.Response(status=200, text="<html>not json</html>", content_type="text/html")
            return await super().handle(request)

    async def run() -> ThalovantAPIError:
        loop = asyncio.get_running_loop()
        future, hand_over = opened_url(loop)
        async with Garbled() as api:
            plane = AsyncThalovantControlPlane(api.url)
            task = asyncio.ensure_future(plane.browser_login(open_browser=False, prompt=hand_over))
            if route == "token":
                port, state, _ = parts(await asyncio.wait_for(future, 5))
                assert (await get(port, f"/callback?code=c&state={state}"))[0] == 200
            with pytest.raises(ThalovantAPIError) as raised:
                await task
            await plane.aclose()
            return raised.value

    error = asyncio.run(run())
    assert error.status_code is None and error.code is None


def test_a_listener_that_cannot_start_leaves_no_socket_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket as sockets

    import thalovant._loopback as loopback

    made: list[sockets.socket] = []

    class Recorded(sockets.socket):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            made.append(self)

    async def refuse(*_: Any, **__: Any) -> None:
        raise OSError("no listening here")

    monkeypatch.setattr(loopback.socket, "socket", Recorded)
    monkeypatch.setattr(loopback.asyncio, "start_server", refuse)

    async def run() -> None:
        with pytest.raises(OSError, match="no listening here"):
            await LoopbackListener("s").start()

    asyncio.run(run())
    # The event loop's own self-pipe is made the same way; ours is the IPv4 one.
    ours = [sock for sock in made if sock.family == sockets.AF_INET]
    assert len(ours) == 1 and ours[0].fileno() == -1


def test_a_connection_that_closes_or_breaks_before_its_answer_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import thalovant._loopback as loopback

    async def run() -> None:
        listener = LoopbackListener("the-state")
        port = await listener.start()
        try:
            # Gone before sending a whole request head.
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /callback?code=c&st")
            writer.close()
            # A failure while one request is handled ends that connection alone.
            original = loopback.classify_request

            def broken(*_: Any) -> Any:
                raise RuntimeError("handler failure")

            monkeypatch.setattr(loopback, "classify_request", broken)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /callback?code=c&state=the-state HTTP/1.1\r\n\r\n")
            assert await reader.read() == b""
            writer.close()
            monkeypatch.setattr(loopback, "classify_request", original)
            assert await listener.wait(0.1) is None
            # Neither was accepted, so this sign-in's own answer still is.
            tab = asyncio.ensure_future(get(port, "/callback?code=c&state=the-state"))
            assert (await listener.wait(5)) is not None
            listener.finish(True)
            status, body = await tab
            assert status == 200 and "Signed in to Thalovant." in body
        finally:
            await listener.close()

    asyncio.run(run())


def test_the_accepted_tab_waits_as_long_as_the_sign_in_takes() -> None:
    """The page says how the sign-in ended, so it is written once it has, however long the exchange took."""

    async def run() -> tuple[str, float]:
        listener = LoopbackListener("the-state")
        port = await listener.start()
        try:
            started = time.monotonic()
            tab = asyncio.ensure_future(get(port, "/callback?code=c&state=the-state"))
            assert (await listener.wait(5)) is not None
            await asyncio.sleep(0.3)
            assert not tab.done()
            listener.finish(True)
            _, body = await tab
            return body, time.monotonic() - started
        finally:
            await listener.close()

    body, took = asyncio.run(run())
    assert "Signed in to Thalovant. You can close this tab." in body and took >= 0.3


def test_closing_the_listener_answers_a_tab_that_is_still_waiting() -> None:
    async def run() -> str:
        listener = LoopbackListener("the-state")
        port = await listener.start()
        tab = asyncio.ensure_future(get(port, "/callback?code=c&state=the-state"))
        assert (await listener.wait(5)) is not None
        await listener.close()
        return (await tab)[1]

    assert "Sign-in was not completed." in asyncio.run(run())


def test_a_head_request_gets_the_headers_and_no_body() -> None:
    async def run() -> bytes:
        listener = LoopbackListener("the-state")
        port = await listener.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"HEAD /callback?code=c&state=the-state HTTP/1.1\r\n\r\n")
            raw = await reader.read()
            writer.close()
            return raw
        finally:
            await listener.close()

    head, _, body = asyncio.run(run()).partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 404 ") and body == b""
    assert b"Content-Type: text/html; charset=utf-8" in head
    length = int(re.search(rb"Content-Length: (\d+)", head).group(1))  # type: ignore[union-attr]
    assert length > 0  # what a GET would have carried


def test_a_message_cut_to_length_keeps_no_part_of_a_secret() -> None:
    """The detail is redacted before the bounded message is made of it."""

    seen: dict[str, str] = {}

    def straddle(body: dict[str, Any]) -> web.Response:
        seen.update(body)
        # The verifier starts ten characters before the message's 200-character cut.
        return web.json_response({"detail": "a" * 190 + body["code_verifier"]}, status=400)

    async def run() -> ThalovantAPIError:
        loop = asyncio.get_running_loop()
        future, hand_over = opened_url(loop)
        async with Api(straddle) as api:
            plane = AsyncThalovantControlPlane(api.url)
            task = asyncio.ensure_future(plane.browser_login(open_browser=False, prompt=hand_over))
            port, state, _ = parts(await asyncio.wait_for(future, 5))
            await get(port, f"/callback?code=c&state={state}")
            with pytest.raises(ThalovantAPIError) as raised:
                await task
            await plane.aclose()
            return raised.value

    error = asyncio.run(run())
    assert error.status_code == 400
    prefix = seen["code_verifier"][:10]
    for shown in (str(error), repr(error), str(error.detail), json.dumps(error.problem)):
        assert prefix not in shown
    assert error.detail == "a" * 190 + "[redacted]"


def test_a_transport_failure_during_the_exchange_carries_nothing_of_the_request() -> None:
    """A requests error keeps the request, body and all; the error raised must not keep it."""

    class Failure(OSError):
        def __init__(self, body: bytes) -> None:
            super().__init__("connection reset")
            self.request_body = body

    class Answer:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.status_code = 200
            self.text = json.dumps(payload)
            self.headers = {"Content-Type": "application/json"}

        def json(self) -> Any:
            return json.loads(self.text)

    class Session:
        headers: dict[str, str] = {}

        def request(self, method: str, url: str, *, json: Any = None, **_: Any) -> Answer:
            if url.endswith("/v1/auth/native/config"):
                return Answer(CONFIG)
            raise Failure(repr(json).encode())

        def close(self) -> None:
            pass

    async def run() -> ThalovantAPIError:
        loop = asyncio.get_running_loop()
        future, hand_over = opened_url(loop)
        plane = ThalovantControlPlane("http://127.0.0.1:9", session=Session())
        task = asyncio.ensure_future(asyncio.to_thread(plane.browser_login, open_browser=False, prompt=hand_over))
        port, state, _ = parts(await asyncio.wait_for(future, 5))
        await get(port, f"/callback?code=transport-code&state={state}")
        with pytest.raises(ThalovantAPIError) as raised:
            await task
        await asyncio.to_thread(plane.close)
        return raised.value

    error = asyncio.run(run())
    assert error.status_code is None
    assert error.__context__ is None and error.__cause__ is None and error.__suppress_context__


def test_the_exchange_never_leaves_the_api_origin() -> None:
    """The configuration refuses such an endpoint; the exchange checks again before sending."""

    async def run() -> tuple[ThalovantAPIError, list[str]]:
        async with Api() as collector, Api() as api:
            plane = AsyncThalovantControlPlane(api.url)
            # Built directly, past the configuration's own check.
            config = native_auth.NativeSignInConfig(
                authorization_endpoint=CONFIG["authorization_endpoint"],
                token_endpoint=f"/{collector.url}/v1/auth/native/token",
                sdk_client_id="thalovant-sdk",
            )
            with pytest.raises(ThalovantAPIError, match="origin") as raised:
                await plane._exchange_browser_code(config, "c", "v" * 43, "http://127.0.0.1:1/callback", ("c",))
            await plane.aclose()
            return raised.value, collector.requests + api.requests

    error, requests = asyncio.run(run())
    assert requests == [] and error.status_code is None


def test_scrubbing_is_one_pass_longest_first_and_leaves_its_marker_alone() -> None:
    from thalovant.control import _scrub

    hidden = ("abc", "abcdef", "c")
    assert _scrub("xx-abc-abcdef-[redacted]", hidden) == "xx-[redacted]-[redacted]-[redacted]"
    once = _scrub({"seen:abcdef": ["c", {"deep": "abc"}]}, hidden)
    assert once == {"seen:[redacted]": ["[redacted]", {"deep": "[redacted]"}]}
    assert _scrub(once, hidden) == once
    assert _scrub("nothing hidden", ()) == "nothing hidden"


@pytest.mark.parametrize("path", ["/https://elsewhere.example/v1/x", "/elsewhere.example:443/v1/x", "/{collector}/v1/x",
                                  "https://elsewhere.example/v1/x"])
def test_no_request_leaves_the_api_origin(path: str) -> None:
    """Every request is joined to the API's address; none may come out somewhere else.

    The join takes the path's leading slashes off, so a path that is a URL --
    or whose first segment reads as a scheme -- would otherwise be one.
    """

    async def run() -> tuple[ThalovantAPIError, list[str]]:
        async with Api() as collector, Api() as api:
            plane = AsyncThalovantControlPlane(api.url, access_token="tvpat_must_stay_home")
            target = path.replace("{collector}", collector.url)
            with pytest.raises(ThalovantAPIError, match="origin") as raised:
                await plane._request("GET", target)
            with pytest.raises(ThalovantAPIError, match="origin"):
                await plane._send("POST", target, json={"code": "c"}, auth=False)
            await plane.aclose()
            return raised.value, collector.requests + api.requests

    error, requests = asyncio.run(run())
    assert requests == [] and error.status_code is None
    assert "tvpat_must_stay_home" not in str(error)


def test_an_ordinary_api_path_still_reaches_the_api() -> None:
    async def run() -> list[str]:
        async with Api() as api:
            plane = AsyncThalovantControlPlane(api.url)
            assert (await plane._request("GET", "/v1/auth/native/config", auth=False))["sdk_client_id"] == "thalovant-sdk"
            await plane.aclose()
            return api.requests

    assert asyncio.run(run()) == ["GET /v1/auth/native/config"]


def _send_and_read(port: int, request: bytes) -> tuple[bytes, str | None]:
    """Send *request* while reading the answer, as a browser does; say how the connection ended."""

    import socket as sockets

    connection = sockets.create_connection(("127.0.0.1", port))
    sender = threading.Thread(target=lambda: _quietly(connection.sendall, request), daemon=True)
    sender.start()
    data, ended = b"", None
    try:
        while chunk := connection.recv(65536):
            data += chunk
    except OSError as error:
        ended = type(error).__name__
    sender.join(5)
    connection.close()
    return data, ended


def _quietly(send: Callable[[bytes], Any], data: bytes) -> None:
    try:
        send(data)
    except OSError:
        pass


@pytest.mark.parametrize("request_bytes", [
    # A head ten times the listener's limit: most of it is never read.
    b"GET /callback?code=" + b"a" * 600_000 + b" HTTP/1.1\r\nHost: x\r\n\r\n",
    # A body nobody asked for, behind a 404.
    b"POST /callback HTTP/1.1\r\nHost: x\r\nContent-Length: 600000\r\n\r\n" + b"b" * 600_000,
], ids=["oversized head", "unread body"])
def test_an_early_answer_is_not_lost_to_a_reset(request_bytes: bytes) -> None:
    """Closing with bytes unread resets the connection, and macOS then drops the
    answer the browser had not read; Linux shows the reset as ECONNRESET."""

    async def run() -> tuple[bytes, str | None]:
        listener = LoopbackListener("the-state")
        port = await listener.start()
        try:
            return await asyncio.to_thread(_send_and_read, port, request_bytes)
        finally:
            await listener.close()

    data, ended = asyncio.run(run())
    assert data.split(b"\r\n", 1)[0] in (b"HTTP/1.1 400 Bad Request", b"HTTP/1.1 404 Not Found")
    assert ended is None, f"the connection ended with {ended}"


def test_draining_a_connection_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A browser that never closes its side cannot keep a connection's task alive."""

    import thalovant._loopback as loopback

    monkeypatch.setattr(loopback, "DRAIN_TIMEOUT", 0.2)

    async def run() -> int:
        listener = LoopbackListener("the-state")
        port = await listener.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /favicon.ico HTTP/1.1\r\n\r\n")
            assert (await reader.read()).startswith(b"HTTP/1.1 404 ")  # the answer ends: our side is shut
            await asyncio.sleep(0.5)  # ours stays open
            left = len(listener._tasks)
            writer.close()
            return left
        finally:
            await listener.close()

    assert asyncio.run(run()) == 0
