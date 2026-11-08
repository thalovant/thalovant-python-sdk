"""Browser sign-in against ``contracts/conformance/browser-login-vectors.json``.

The pure sections (PKCE, the redirect, the authorization URL, the
configuration, the browser rule, what a callback means) are run against the
functions that decide them. ``sign_ins`` and ``chooser`` are run for real: a
loopback API serves each case's exchanges in order and checks every request
the SDK sends, and a raw HTTP client plays the browser against the port the
SDK bound. What the SDK produced is recorded for the conformance record and
compared with the case. Every SDK runs the same file.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import webbrowser
from hashlib import sha256
from base64 import urlsafe_b64encode
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from conformance_record import record
from thalovant import (
    AsyncThalovantControlPlane,
    NativeSignInConfig,
    ThalovantAPIError,
    ThalovantControlPlane,
    ThalovantDeviceLoginDenied,
    ThalovantDeviceLoginExpired,
    browser_authorization_url,
    can_open_browser,
    challenge_for,
    loopback_redirect_uri,
)
from thalovant import native_auth
from thalovant._loopback import LoopbackListener, classify_request

CONFORMANCE = Path(__file__).resolve().parents[1] / "contracts" / "conformance"


def vectors(name: str) -> dict[str, Any]:
    return dict(json.loads((CONFORMANCE / name).read_text(encoding="utf-8")))


BROWSER = vectors("browser-login-vectors.json")
VECTOR_FILE = "browser-login-vectors.json"
UNRESERVED = re.compile(r"[A-Za-z0-9\-._~]{43,128}")
STATE_ALPHABET = re.compile(r"[A-Za-z0-9\-_]{22,}")
PLANES = [AsyncThalovantControlPlane, ThalovantControlPlane]


def cases(section: str) -> list[dict[str, Any]]:
    return list(BROWSER[section])


def ids(case: dict[str, Any]) -> str:
    return str(case["name"])


def _record(section: str, case: dict[str, Any], produced: Any) -> None:
    record(VECTOR_FILE, f"{section}/{case['name']}", produced)


# -- the pure sections ------------------------------------------------------


@pytest.mark.parametrize("case", cases("pkce"), ids=ids)
def test_pkce(case: dict[str, Any]) -> None:
    produced = {"challenge": challenge_for(case["verifier"])}
    _record("pkce", case, produced)
    assert produced == case["expect"]


@pytest.mark.parametrize("case", cases("redirect_uris"), ids=ids)
def test_redirect_uris(case: dict[str, Any]) -> None:
    try:
        produced: dict[str, Any] = {"redirect_uri": loopback_redirect_uri(case["port"])}
    except ValueError:
        produced = {"error": "invalid_port"}
    _record("redirect_uris", case, produced)
    assert produced == case["expect"]


@pytest.mark.parametrize("case", cases("authorization_urls"), ids=ids)
def test_authorization_urls(case: dict[str, Any]) -> None:
    given = case["input"]
    produced = {"url": browser_authorization_url(
        given["authorization_endpoint"], client_id=given["client_id"],
        redirect_uri=loopback_redirect_uri(given["port"]), code_challenge=given["code_challenge"],
        state=given["state"], scopes=given["scopes"], device_name=given["device_name"],
    )}
    _record("authorization_urls", case, produced)
    assert produced == case["expect"]


@pytest.mark.parametrize("case", cases("config"), ids=ids)
def test_config(case: dict[str, Any]) -> None:
    try:
        config = NativeSignInConfig.from_dict(case["body"])
    except ValueError:
        produced: dict[str, Any] = {"usable": False}
    else:
        produced = {"usable": True, "authorization_endpoint": config.authorization_endpoint,
                    "token_endpoint": config.token_endpoint, "sdk_client_id": config.sdk_client_id}
    _record("config", case, produced)
    assert produced == case["expect"]


@pytest.mark.parametrize("case", cases("environment"), ids=ids)
def test_environment(case: dict[str, Any]) -> None:
    produced = {"browser": can_open_browser(case["env"], case["platform"])}
    _record("environment", case, produced)
    assert produced == case["expect"]


SYNTHETIC_STATE = "Xx0-_yLq7m3Vw9ZtR2cKp4HdN8sFjB6a"


def _callback_outcome(status: int, callback: Any) -> dict[str, Any]:
    if callback is None:
        return {"status": status, "outcome": "ignored"}
    if callback.outcome == "code":
        return {"status": status, "outcome": "code", "code": callback.code}
    if callback.outcome == "denied":
        return {"status": status, "outcome": "denied"}
    return {"status": status, "outcome": "failed", "error": callback.error}


@pytest.mark.parametrize("case", cases("callbacks"), ids=ids)
def test_callbacks(case: dict[str, Any]) -> None:
    request = case["request"]
    status, callback = classify_request(
        request["method"], request["target"].replace("{state}", SYNTHETIC_STATE), SYNTHETIC_STATE)
    produced = _callback_outcome(status, callback)
    _record("callbacks", case, produced)
    assert produced == case["expect"]


@pytest.mark.parametrize("case", cases("callbacks"), ids=ids)
def test_callbacks_over_the_listener(case: dict[str, Any]) -> None:
    """The same cases through a real socket: the listener answers as the rule says."""

    async def run() -> dict[str, Any]:
        listener = LoopbackListener(SYNTHETIC_STATE)
        port = await listener.start()
        try:
            request = case["request"]
            answer = await asyncio.wait_for(
                http(port, request["method"], request["target"].replace("{state}", SYNTHETIC_STATE),
                     before_reading=lambda: listener.finish(False)), 5)
            _check_page_headers(answer)
            accepted = await listener.wait(0)
            return _callback_outcome(answer.status, accepted)
        finally:
            await listener.close()

    assert asyncio.run(run()) == case["expect"]


# -- a browser, and an API, on loopback ---------------------------------------


class Answer:
    def __init__(self, status: int, headers: dict[str, str], body: str) -> None:
        self.status, self.headers, self.body = status, headers, body

    @property
    def page(self) -> str | None:
        for key, text in BROWSER["pages"].items():
            if text in self.body:
                return key
        return None


async def http(port: int, method: str, target: str, before_reading: Callable[[], None] | None = None) -> Answer:
    """One request on its own connection, as a browser tab sends it."""

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
                     .encode("utf-8"))
        await writer.drain()
        if before_reading is not None:
            await asyncio.sleep(0.05)
            before_reading()
        raw = await reader.read()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip()] = value.strip()
    return Answer(status, headers, body.decode("utf-8"))


def _check_page_headers(answer: Answer) -> None:
    for name, value in BROWSER["page_headers"].items():
        assert answer.headers.get(name) == value, (name, answer.headers)
    # Self-contained: nothing for the page to load.
    assert "src=" not in answer.body and "href=" not in answer.body and "http" not in answer.body


async def refused(port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return True
    writer.close()
    return False


class Signing:
    """What the test learnt from the URL the SDK built."""

    def __init__(self, url: str) -> None:
        parts = urlsplit(url)
        self.endpoint = f"{parts.scheme}://{parts.netloc}{parts.path}"
        self.pairs = parse_qsl(parts.query, keep_blank_values=True)
        values = dict(self.pairs)
        redirect = urlsplit(values["redirect_uri"])
        assert redirect.port is not None
        self.port = redirect.port
        self.state = values["state"]
        self.challenge = values["code_challenge"]
        assert STATE_ALPHABET.fullmatch(self.state), "state must be at least 128 bits of base64url"
        assert re.fullmatch(r"[A-Za-z0-9\-_]{43}", self.challenge)
        assert values["redirect_uri"] == f"http://127.0.0.1:{self.port}/callback"
        assert 1024 <= self.port <= 65535

    def normalized(self, text: str) -> str:
        return (text.replace(f"http://127.0.0.1:{self.port}/callback", "http://127.0.0.1:{port}/callback")
                .replace(self.state, "{state}").replace(self.challenge, "{challenge}"))

    def url_record(self) -> dict[str, Any]:
        return {"endpoint": self.endpoint, "query": [[key, self.normalized(value)] for key, value in self.pairs]}


class ScriptedApi:
    """Serves a case's exchanges in order and checks each request against its own."""

    def __init__(self, exchanges: list[dict[str, Any]]) -> None:
        self.exchanges = list(exchanges)
        self.index = 0
        self.sent: list[str] = []
        self.mismatches: list[str] = []
        self.signing: Signing | None = None
        self.exchange: dict[str, Any] | None = None
        self.verifier: str | None = None
        self.server: TestServer | None = None
        self.url = ""

    async def __aenter__(self) -> ScriptedApi:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()
        self.url = f"http://127.0.0.1:{self.server.port}"
        return self

    async def __aexit__(self, *_: Any) -> None:
        assert self.server is not None
        await self.server.close()

    def _token_body(self, body: Any) -> Any:
        """The exchange body with the generated values put back as placeholders, once proven."""

        if not isinstance(body, dict) or self.signing is None:
            return body
        if set(body) != {"code", "code_verifier", "client_id", "redirect_uri"}:
            self.mismatches.append(f"the exchange must carry exactly four fields, not {sorted(body)}")
        verifier = body.get("code_verifier")
        clean = {key: self.signing.normalized(value) if isinstance(value, str) else value
                 for key, value in body.items()}
        if isinstance(verifier, str) and UNRESERVED.fullmatch(verifier) and \
                urlsafe_b64encode(sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode() == \
                self.signing.challenge:
            clean["code_verifier"] = "{verifier}"
            self.verifier = verifier
        else:
            self.mismatches.append("the code_verifier is not the one the challenge was made from")
        self.exchange = clean
        return clean

    async def handle(self, request: web.Request) -> web.Response:
        self.sent.append(f"{request.method} {request.path}")
        if self.index >= len(self.exchanges):
            self.mismatches.append(f"unexpected {request.method} {request.path}")
            return web.Response(status=599, text="{}")
        exchange = self.exchanges[self.index]
        self.index += 1
        expected = exchange["request"]
        if (request.method, request.path) != (expected["method"], expected["path"]):
            self.mismatches.append(f"{request.method} {request.path} != {expected['method']} {expected['path']}")
        raw = await request.text()
        body = json.loads(raw) if raw else None
        if request.path == expected["path"] == "/v1/auth/native/token" or "code_verifier" in (body or {}):
            body = self._token_body(body)
        if "json" in expected and body != expected["json"]:
            self.mismatches.append(f"body {body!r} != {expected['json']!r}")
        if "authorization" in expected and request.headers.get("Authorization") != expected["authorization"]:
            self.mismatches.append("wrong Authorization header")
        response = exchange["response"]
        headers = {"Content-Type": response["content_type"]} if response["body"] else None
        return web.Response(status=response["status"], body=response["body"].encode("utf-8"), headers=headers)


def _runner(plane: Any) -> Callable[..., Any]:
    async def run(function: Any, /, *args: Any, **kwargs: Any) -> Any:
        if isinstance(plane, AsyncThalovantControlPlane):
            return await function(*args, **kwargs)
        return await asyncio.to_thread(function, *args, **kwargs)
    return run


async def _close(plane: Any) -> None:
    if isinstance(plane, AsyncThalovantControlPlane):
        await plane.aclose()
    else:
        await asyncio.to_thread(plane.close)


def _result(plane: Any, outcome: Any) -> dict[str, Any]:
    if isinstance(outcome, ThalovantDeviceLoginDenied):
        return {"outcome": "denied", "status": outcome.status_code}
    if isinstance(outcome, ThalovantDeviceLoginExpired):
        return {"outcome": "expired", "status": outcome.status_code}
    if isinstance(outcome, ThalovantAPIError):
        produced = {"outcome": "error", "status": outcome.status_code, "code": outcome.code}
        if outcome.status_code is not None:
            produced["detail"] = outcome.detail
        return produced
    assert isinstance(outcome, dict)
    assert plane.access_token == outcome["access_token"] and plane.token_id == outcome["token_id"]
    return {"outcome": "approved", "token_type": outcome["token_type"], "scopes": outcome["scopes"],
            "expires_at": outcome["expires_at"], "token_id": outcome["token_id"]}


def _no_secrets(error: BaseException, api: ScriptedApi) -> None:
    hidden = list(BROWSER["message_excludes"])
    hidden += [value for value in (api.verifier, api.signing.state if api.signing else None) if value]
    shown = [str(error), repr(error), repr(getattr(error, "problem", None))]
    for value in hidden:
        assert not any(value in text for text in shown), "a secret reached the error"


async def _sign_in(case: dict[str, Any], plane_cls: type, start: Callable[[Any, Any, Callable[[str], None]], Any],
                   ) -> tuple[dict[str, Any], ScriptedApi]:
    async with ScriptedApi(case["exchanges"]) as api:
        plane = plane_cls(api.url)
        loop = asyncio.get_running_loop()
        opened: asyncio.Future[str] = loop.create_future()

        def hand_over(url: str) -> None:
            # From whichever thread the SDK shows the URL on.
            def settle() -> None:
                if not opened.done():
                    opened.set_result(url)
            loop.call_soon_threadsafe(settle)

        produced: dict[str, Any] = {}
        task = asyncio.ensure_future(start(plane, _runner(plane), hand_over))
        try:
            done, _ = await asyncio.wait({opened, task}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
            assert done, "the sign-in neither opened a URL nor ended"
            if opened.done():
                signing = api.signing = Signing(opened.result())
                produced["authorization_url"] = signing.url_record()
                browser = []
                for request in case["requests"]:
                    answer = await http(signing.port, request["method"],
                                        request["target"].replace("{state}", signing.state))
                    _check_page_headers(answer)
                    step: dict[str, Any] = {"status": answer.status}
                    if answer.status == 200:
                        step["page"] = answer.page
                    browser.append(step)
                produced["browser"] = browser
            try:
                outcome: Any = await asyncio.wait_for(task, 10)
            except (ThalovantAPIError, ThalovantDeviceLoginDenied, ThalovantDeviceLoginExpired) as error:
                _no_secrets(error, api)
                outcome = error
            if api.signing is not None:
                produced["closed"] = await refused(api.signing.port)
            if api.exchange is not None:
                produced["exchange"] = api.exchange
            produced["result"] = _result(plane, outcome)
            if case.get("call", {}).get("then") == "revoke" and produced["result"]["outcome"] == "approved":
                run = _runner(plane)
                await run(plane.revoke_api_token)
                assert plane.access_token is None and plane.token_id is None
                await run(plane.revoke_api_token)  # idempotent: sends nothing
                produced["result"] = {"outcome": "revoked"}
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await _close(plane)
        return produced, api


def _timeout(call: dict[str, Any]) -> float | None:
    return call["timeout_ms"] / 1000 if "timeout_ms" in call else None


@pytest.mark.parametrize("plane_cls", PLANES, ids=["async", "sync"])
@pytest.mark.parametrize("case", cases("sign_ins"), ids=ids)
def test_sign_ins(case: dict[str, Any], plane_cls: type) -> None:
    call = case["call"]

    def start(plane: Any, run: Any, hand_over: Callable[[str], None]) -> Any:
        timeout = _timeout(call)
        return run(plane.browser_login, scopes=call["scopes"], device_name=call["device_name"],
                   open_browser=False, prompt=hand_over, **({"timeout": timeout} if timeout else {}))

    produced, api = asyncio.run(_sign_in(case, plane_cls, start))
    if plane_cls is AsyncThalovantControlPlane:
        _record("sign_ins", case, produced)
    assert not api.mismatches, api.mismatches
    assert api.index == len(case["exchanges"]), f"not every exchange was used: {api.sent}"
    assert produced == case["expect"]


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, Any]], None]:
    """This process as a case's machine: its platform and the variables the browser rule reads."""

    def become(case: dict[str, Any]) -> None:
        for name in ("CI", "SSH_CONNECTION", "SSH_TTY", "DISPLAY", "WAYLAND_DISPLAY"):
            monkeypatch.delenv(name, raising=False)
        for name, value in case["env"].items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(native_auth, "_platform_name", lambda: case["platform"])

    return become


@pytest.mark.parametrize("plane_cls", PLANES, ids=["async", "sync"])
@pytest.mark.parametrize("case", cases("chooser"), ids=ids)
def test_chooser(case: dict[str, Any], plane_cls: type, machine: Callable[[dict[str, Any]], None],
                 monkeypatch: pytest.MonkeyPatch) -> None:
    machine(case)
    call = case["call"]
    receivers: list[Callable[[str], None]] = []
    lock = threading.Lock()

    def fake_browser(url: str, *_: Any, **__: Any) -> bool:
        # The browser the SDK opens. Device login opens its verification
        # page too; only the consent page is this test's to answer.
        if "response_type=code" in url:
            with lock:
                for receive in receivers:
                    receive(url)
        return True

    monkeypatch.setattr(webbrowser, "open", fake_browser)

    def start(plane: Any, run: Any, hand_over: Callable[[str], None]) -> Any:
        receivers.append(hand_over)
        timeout = _timeout(call)
        return run(plane.interactive_login, method=call["method"], device_name=call["device_name"],
                   **({"timeout": timeout} if timeout else {}))

    produced, api = asyncio.run(_sign_in(case, plane_cls, start))
    result = {"method": "device" if "POST /v1/auth/device/authorize" in api.sent else "browser",
              "result": produced["result"]}
    if plane_cls is AsyncThalovantControlPlane:
        _record("chooser", case, result)
    assert not api.mismatches, api.mismatches
    assert api.index == len(case["exchanges"]), f"not every exchange was used: {api.sent}"
    assert result == case["expect"]
    if result["method"] == "browser" and "browser" in produced:
        assert produced.get("closed") is True


def test_every_section_is_run() -> None:
    """A section nobody runs is behaviour written down and never checked."""

    sections = {"pkce", "redirect_uris", "authorization_urls", "config", "environment", "callbacks",
                "sign_ins", "chooser"}
    assert sections == {key for key, value in BROWSER.items() if isinstance(value, list)
                        and value and isinstance(value[0], dict) and "name" in value[0]}


def test_the_rfc_7636_vector_is_the_first_pkce_case() -> None:
    first = BROWSER["pkce"][0]
    assert first["verifier"] == "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert first["expect"]["challenge"] == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_the_listener_binds_loopback_only() -> None:
    async def run() -> None:
        listener = LoopbackListener("s" * 32)
        await listener.start()
        try:
            assert listener._server is not None
            assert {sock.getsockname()[0] for sock in listener._server.sockets} == {"127.0.0.1"}
        finally:
            await listener.close()

    asyncio.run(run())
