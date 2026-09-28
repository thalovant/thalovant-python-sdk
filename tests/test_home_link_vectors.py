"""The Home Assistant link, against the four shared vector files.

``device-login``, ``connection-kinds`` and ``connection-admission`` are HTTP
exchanges: each case's answers are served by a loopback API, in order, and
every request the SDK sends is checked against the one the case expects. What
the SDK produced is recorded for the conformance record and compared with the
case. ``home-link`` holds the reply routing and the request/response rules.
Every SDK runs the same files.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from conformance_record import record
from thalovant import (
    AsyncThalovantControlPlane,
    ThalovantAdmissionFailedError,
    ThalovantAdmissionTimeoutError,
    ThalovantAlreadyLinkedError,
    ThalovantAPIError,
    ThalovantAPIUnreachableError,
    ThalovantAuthError,
    ThalovantConnectionError,
    ThalovantControlPlane,
    ThalovantDeviceLoginDenied,
    ThalovantDeviceLoginExpired,
    ThalovantDeviceLoginPending,
    ThalovantPlanError,
    ThalovantTimeoutError,
    ThalovantUnsupportedConnectionTypeError,
)
from thalovant.client import reply_context
from thalovant.control import DeviceAuthorization, OperationResource
from thalovant.events import ThalovantEvent
from thalovant.home import HOME_REQUEST, HOME_RESPONSE, HomeAnswer, answer_home_request, plain_speech

CONFORMANCE = Path(__file__).resolve().parents[1] / "contracts" / "conformance"


def vectors(name: str) -> dict[str, Any]:
    return json.loads((CONFORMANCE / name).read_text(encoding="utf-8"))


DEVICE = vectors("device-login-vectors.json")
KINDS = vectors("connection-kinds-vectors.json")
ADMISSION = vectors("connection-admission-vectors.json")
HOME = vectors("home-link-vectors.json")


class ScriptedApi:
    """Serves a case's exchanges in order and checks each request against its own."""

    def __init__(self, exchanges: list[dict[str, Any]]) -> None:
        self.exchanges = list(exchanges)
        self.index = 0
        self.sent: list[str] = []
        self.mismatches: list[str] = []
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

    async def handle(self, request: web.Request) -> web.Response:
        if_match = request.headers.get("If-Match")
        self.sent.append(f"{request.method} {request.path}" + (f" If-Match={if_match}" if if_match else ""))
        if self.index >= len(self.exchanges):
            self.mismatches.append(f"unexpected {request.method} {request.path}")
            return web.Response(status=599, text="{}")
        exchange = self.exchanges[self.index]
        if not exchange.get("repeat"):
            self.index += 1
        expected = exchange["request"]
        if (request.method, request.path) != (expected["method"], expected["path"]):
            self.mismatches.append(f"{request.method} {request.path} != {expected['method']} {expected['path']}")
        raw = await request.text()
        body = json.loads(raw) if raw else None
        if "json" in expected and body != expected["json"]:
            self.mismatches.append(f"body {body!r} != {expected['json']!r}")
        if "json_subset" in expected and not _contains(body, expected["json_subset"]):
            self.mismatches.append(f"body {body!r} lacks {expected['json_subset']!r}")
        if "if_match" in expected and if_match != expected["if_match"]:
            self.mismatches.append(f"If-Match {if_match!r} != {expected['if_match']!r}")
        if "authorization" in expected and request.headers.get("Authorization") != expected["authorization"]:
            self.mismatches.append("wrong Authorization header")
        response = exchange["response"]
        headers = dict(response.get("headers") or {})
        if response["body"]:
            headers["Content-Type"] = response["content_type"]
        return web.Response(status=response["status"], body=response["body"].encode("utf-8"), headers=headers or None)


def _contains(value: Any, subset: Any) -> bool:
    if isinstance(subset, dict):
        return isinstance(value, dict) and all(key in value and _contains(value[key], item) for key, item in subset.items())
    return bool(value == subset)


def _number(value: float) -> float | int:
    return int(value) if float(value).is_integer() else value


def _api_fields(error: ThalovantAPIError) -> dict[str, Any]:
    return {"status": error.status_code, "code": error.code, "detail": error.detail}


def _excluded(error: BaseException, spec: dict[str, Any]) -> None:
    for text in spec.get("message_excludes", ()):
        assert text not in str(error) and text not in repr(error)


# -- device login -------------------------------------------------------------


async def _device_case(case: dict[str, Any], plane_cls: type) -> tuple[list[dict[str, Any]], ScriptedApi]:
    call = case["call"]
    async with ScriptedApi(case["exchanges"]) as api:
        plane = plane_cls(api.url)
        run = _runner(plane)
        produced: list[dict[str, Any]] = []
        try:
            if call["op"] == "begin":
                try:
                    grant = await run(plane.begin_device_login, scopes=call.get("scopes"), client_name=call.get("client_name"))
                except ThalovantAPIError as error:
                    _excluded(error, DEVICE)
                    produced.append({"outcome": "error", "status": error.status_code})
                else:
                    produced.append({"outcome": "started", "user_code": grant.user_code,
                                     "verification_uri": grant.verification_uri,
                                     "verification_uri_complete": grant.verification_uri_complete,
                                     "interval": _number(grant.interval), "expires_in": grant.expires_in})
            else:
                authorization = DeviceAuthorization(
                    device_code=call["authorization"]["device_code"], user_code="", verification_uri="https://x",
                    verification_uri_complete=None, interval=float(call["authorization"]["interval"]), expires_in=900,
                )
                for _ in range(call.get("times", 1)):
                    produced.append(await _poll_once(run, plane, authorization))
                if call["op"] == "revoke":
                    await run(plane.revoke_api_token)
                    assert plane.access_token is None and plane.token_id is None
                    produced = [{"outcome": "revoked"}]
                    # Idempotent: revoking again sends nothing and succeeds.
                    await run(plane.revoke_api_token)
        finally:
            await _close(plane)
        return produced, api


async def _poll_once(run: Any, plane: Any, authorization: DeviceAuthorization) -> dict[str, Any]:
    try:
        token = await run(plane.poll_device_login, authorization)
    except ThalovantDeviceLoginPending as pending:
        return {"outcome": "pending", "interval": _number(pending.interval)}
    except ThalovantDeviceLoginExpired as error:
        _excluded(error, DEVICE)
        return {"outcome": "expired", "status": error.status_code}
    except ThalovantDeviceLoginDenied as error:
        _excluded(error, DEVICE)
        return {"outcome": "denied", "status": error.status_code}
    except ThalovantAPIError as error:
        _excluded(error, DEVICE)
        produced = {"outcome": "error", "status": error.status_code}
        if error.status_code is not None:
            produced.update(code=error.code, detail=error.detail)
        return produced
    assert plane.access_token == token.access_token and plane.token_id == token.token_id
    return {"outcome": "approved", "token_type": token.token_type, "scopes": list(token.scopes),
            "expires_at": token.expires_at.isoformat().replace("+00:00", "Z") if token.expires_at else None,
            "token_id": token.token_id}


def _runner(plane: Any) -> Any:
    """Call a method of either class from a coroutine."""
    async def run(method: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(plane, AsyncThalovantControlPlane):
            return await method(*args, **kwargs)
        return await asyncio.to_thread(method, *args, **kwargs)
    return run


async def _close(plane: Any) -> None:
    if isinstance(plane, AsyncThalovantControlPlane):
        await plane.aclose()
    else:
        await asyncio.to_thread(plane.close)


@pytest.mark.parametrize("plane_cls", [AsyncThalovantControlPlane, ThalovantControlPlane], ids=["async", "sync"])
@pytest.mark.parametrize("case", DEVICE["cases"], ids=lambda case: case["name"])
def test_device_login_vectors(case: dict[str, Any], plane_cls: type) -> None:
    produced, api = asyncio.run(_device_case(case, plane_cls))
    if plane_cls is AsyncThalovantControlPlane:
        record("device-login-vectors.json", case["name"], produced)
    assert not api.mismatches, api.mismatches
    assert api.index == len(case["exchanges"]), "not every exchange was used"
    assert produced == case["expect"]


# -- connection kinds -------------------------------------------------------


async def _kinds_case(case: dict[str, Any], plane_cls: type) -> tuple[dict[str, Any], ScriptedApi]:
    call = case["call"]
    async with ScriptedApi(case["exchanges"]) as api:
        plane = plane_cls(api.url, access_token="synthetic-token")
        run = _runner(plane)
        try:
            if call["op"] == "create":
                try:
                    result = await run(plane.create_client_identity, call["hub"], name=call["name"],
                                       connection_type=call["connection_type"])
                except ThalovantUnsupportedConnectionTypeError as error:
                    _excluded(error, KINDS)
                    produced: dict[str, Any] = {"outcome": "unsupported"}
                    if error.status_code is not None:
                        produced.update(_api_fields(error))
                    else:
                        produced["deleted"] = any(line.startswith("DELETE ") for line in api.sent)
                except ThalovantAPIError as error:
                    _excluded(error, KINDS)
                    produced = {"outcome": _kind_outcome(error), **_api_fields(error)}
                    if isinstance(error, ThalovantAlreadyLinkedError):
                        produced["client_id"] = error.client_id
                else:
                    produced = {"outcome": "created", "client_id": result.client_id,
                                "connection_type": result.connection_type,
                                "operation_id": result.operation.id if result.operation else None}
            else:
                try:
                    await run(plane.delete_client, call["client_id"], etag=call.get("etag"))
                except ThalovantAPIError as error:
                    produced = {"outcome": _kind_outcome(error), **_api_fields(error)}
                else:
                    produced = {"outcome": "deleted"}
        finally:
            await _close(plane)
        produced["requests"] = list(api.sent)
        return produced, api


def _kind_outcome(error: ThalovantAPIError) -> str:
    if isinstance(error, ThalovantPlanError):
        return "plan"
    if isinstance(error, ThalovantAlreadyLinkedError):
        return "already_linked"
    if isinstance(error, ThalovantAuthError):
        return "auth"
    return "error"


@pytest.mark.parametrize("plane_cls", [AsyncThalovantControlPlane, ThalovantControlPlane], ids=["async", "sync"])
@pytest.mark.parametrize("case", KINDS["cases"], ids=lambda case: case["name"])
def test_connection_kinds_vectors(case: dict[str, Any], plane_cls: type) -> None:
    produced, api = asyncio.run(_kinds_case(case, plane_cls))
    if plane_cls is AsyncThalovantControlPlane:
        record("connection-kinds-vectors.json", case["name"], produced)
    assert not api.mismatches, api.mismatches
    assert produced == case["expect"]


# -- admission ----------------------------------------------------------------


class ResettingListener:
    """A port that accepts a connection and resets it at once.

    What the vectors' "unreachable" means, reached the same way on every
    platform: a closed port is refused at once on Linux and macOS, but Windows
    retries the SYN for about two seconds first.
    """

    def __init__(self) -> None:
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self._socket.settimeout(0.05)
        self.port = int(self._socket.getsockname()[1])
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        import struct

        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except OSError:
                continue
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            connection.close()  # RST

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)
        self._socket.close()


def _placed(value: Any, host: str, port: int) -> Any:
    """The case's operation with {api_host} and {api_port} filled in."""
    if isinstance(value, str):
        return value.replace("{api_host}", host).replace("{api_port}", str(port))
    if isinstance(value, dict):
        return {key: _placed(item, host, port) for key, item in value.items()}
    return value


async def _admission_case(case: dict[str, Any]) -> tuple[dict[str, Any], ScriptedApi]:
    call = case["call"]
    expect = case["expect"]
    async with ScriptedApi(case["exchanges"]) as api:
        assert api.server is not None
        resetting = ResettingListener() if call.get("api") == "unreachable" else None
        url = f"http://127.0.0.1:{resetting.port}" if resetting is not None else api.url
        plane = AsyncThalovantControlPlane(url, access_token="synthetic-token")
        operation = _placed(call["operation"], "127.0.0.1", api.server.port)
        started = time.monotonic()
        try:
            await plane.wait_for_admission(
                OperationResource.from_dict(operation) if operation else None,
                timeout=call["timeout_ms"] / 1000, poll_interval=call["poll_interval_ms"] / 1000,
            )
        except ThalovantAdmissionTimeoutError as error:
            assert isinstance(error, ThalovantConnectionError) and isinstance(error, ThalovantTimeoutError)
            assert str(error).endswith("it may still admit it later.")
            produced: dict[str, Any] = {"outcome": "timeout"}
            if "polls" in expect:
                produced["polls"] = len(api.sent)
        except ThalovantAdmissionFailedError as error:
            produced = {"outcome": "failed", "error_code": error.error_code, "status": error.status_code}
            if error.status_code is not None:
                produced.update(code=error.code, detail=error.detail)
            produced["polls"] = len(api.sent)
        except ThalovantAPIUnreachableError:
            produced = {"outcome": "unreachable", "polls": len(api.sent)}
        except ThalovantAuthError as error:
            produced = {"outcome": "auth", "status": error.status_code, "polls": len(api.sent)}
        except ThalovantAPIError:
            produced = {"outcome": "error", "polls": len(api.sent)}
        else:
            produced = {"outcome": "admitted", "polls": len(api.sent)}
        finally:
            await plane.aclose()
            if resetting is not None:
                resetting.close()
        if "waited_at_least_ms" in expect:
            # Recorded as the bound it met, so every SDK records the same value.
            waited_ms = (time.monotonic() - started) * 1000
            bound = expect["waited_at_least_ms"]
            produced["waited_at_least_ms"] = bound if waited_ms >= bound else int(waited_ms)
        return produced, api


@pytest.mark.parametrize("case", ADMISSION["cases"], ids=lambda case: case["name"])
def test_connection_admission_vectors(case: dict[str, Any]) -> None:
    produced, api = asyncio.run(_admission_case(case))
    record("connection-admission-vectors.json", case["name"], produced)
    assert not api.mismatches, api.mismatches
    assert produced == case["expect"]


# -- the home link --------------------------------------------------------------


class Replies:
    def __init__(self) -> None:
        self.sent: list[tuple[Any, str, dict[str, Any]]] = []

    async def reply(self, event: Any, msg_type: str, data: dict[str, Any]) -> None:
        self.sent.append((event, msg_type, data))


def _handler(spec: dict[str, Any]) -> Any:
    async def handle(_request: Any) -> HomeAnswer:
        if spec.get("raises"):
            raise RuntimeError("the conversation agent is gone")
        if spec.get("sleep_ms"):
            await asyncio.sleep(spec["sleep_ms"] / 1000)
        return HomeAnswer(
            speech=spec.get("speech", ""),
            response_type=spec.get("response_type", "action_done"),
            error_code=spec.get("error_code"),
            continue_conversation=spec.get("continue_conversation", False),
        )

    return handle


class SlowReplies(Replies):
    """A transport that takes ``send_ms`` to put a reply on the wire."""

    def __init__(self, send_ms: int) -> None:
        super().__init__()
        self.send_ms = send_ms

    async def reply(self, event: Any, msg_type: str, data: dict[str, Any]) -> None:
        await asyncio.sleep(self.send_ms / 1000)
        await super().reply(event, msg_type, data)


@pytest.mark.parametrize("case", HOME["cases"], ids=lambda case: case["name"])
def test_home_link_vectors(case: dict[str, Any]) -> None:
    if case["kind"] == "reply_context":
        produced: Any = reply_context(case["context"])
    elif case["kind"] == "speech":
        produced = plain_speech(case["text"])
    elif case["kind"] == "deadline":
        replies = SlowReplies(case["send_ms"])
        event = ThalovantEvent(name=HOME_REQUEST, data=case["request"], context={"source": "skill"}, raw=None)
        hub_timeout = case["hub_timeout_ms"] / 1000

        async def answer() -> Any:
            started = time.monotonic()
            sent = await answer_home_request(
                replies, event, _handler(case["handler"]),
                timeout=case["timeout_ms"] / 1000, hub_timeout=hub_timeout,
            )
            # Never past the hub's bound, whatever the handler or the transport did.
            assert time.monotonic() - started <= hub_timeout + 0.1
            return sent

        sent = asyncio.run(answer())
        produced = {"replied": sent is not None}
        if sent is not None:
            assert [(msg_type, data) for _, msg_type, data in replies.sent] == [(HOME_RESPONSE, sent)]
            produced["response"] = sent
    else:
        replies = Replies()
        event = ThalovantEvent(name=HOME_REQUEST, data=case["request"], context={"source": "skill"}, raw=None)
        produced = asyncio.run(answer_home_request(
            replies, event, _handler(case["handler"]), timeout=case.get("timeout_ms", 9000) / 1000,
        ))
        assert [(msg_type, data) for _, msg_type, data in replies.sent] == [(HOME_RESPONSE, produced)]
    record("home-link-vectors.json", case["name"], produced)
    assert produced == case["expect"]


def test_the_contract_lists_match_the_sdk() -> None:
    from thalovant import home

    assert list(home.RESPONSE_TYPES) == HOME["response_types"]
    assert list(home.ERROR_CODES) == HOME["error_codes"]
    assert home.HOME_REQUEST == HOME["request_type"] and home.HOME_RESPONSE == HOME["response_type"]
    assert home.HOME_REQUEST_TIMEOUT * 1000 == HOME["reply_timeout_ms"]
    assert home.DEFAULT_HANDLER_TIMEOUT * 1000 == 9000
    from thalovant import HOME_ASSISTANT_SCOPES

    assert list(HOME_ASSISTANT_SCOPES) == DEVICE["home_assistant_scopes"]


def test_a_handler_that_ignores_cancellation_does_not_hold_the_answer_back() -> None:
    """``asyncio.wait_for`` waits for a cancelled task to finish; the answer must not."""

    async def stubborn(_request: Any) -> HomeAnswer:
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            await asyncio.sleep(2)  # swallows the cancellation and carries on
        return HomeAnswer(speech="Too late.")

    async def exercise() -> tuple[Any, float, Replies]:
        replies = Replies()
        event = ThalovantEvent(name=HOME_REQUEST, data={"request_id": "s1", "utterance": "x"}, context={}, raw=None)
        started = time.monotonic()
        sent = await answer_home_request(replies, event, stubborn, timeout=0.1, hub_timeout=1.0)
        return sent, time.monotonic() - started, replies

    sent, took, replies = asyncio.run(exercise())
    assert took < 0.5
    assert sent is not None and sent["error_code"] == "timeout"
    assert len(replies.sent) == 1


def test_a_wait_the_api_asked_for_is_never_cut_short(monkeypatch) -> None:
    from thalovant import control

    naps: list[float] = []
    real_sleep = asyncio.sleep

    async def early_sleep(seconds: float) -> None:
        naps.append(seconds)
        await real_sleep(max(0.0, seconds - 0.03))  # wakes 30 ms early, as a coarse clock can

    monkeypatch.setattr(control.asyncio, "sleep", early_sleep)

    async def exercise() -> float:
        started = time.monotonic()
        await control._sleep_at_least(0.2)
        return time.monotonic() - started

    assert asyncio.run(exercise()) >= 0.2
    assert len(naps) >= 2
