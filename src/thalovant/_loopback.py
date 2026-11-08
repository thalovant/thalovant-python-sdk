"""The loopback listener a browser sign-in waits on (RFC 8252 section 7.3).

A program that signs in through the browser binds ``127.0.0.1`` on a port the
system picks, sends the browser to the authorization page with
``http://127.0.0.1:<port>/callback`` as its redirect, and waits here for the
browser to come back. Only ``GET /callback`` is served; anything else is a
404. A callback whose ``state`` is not this sign-in's is a 400 and changes
nothing: a stray tab -- an old attempt's, another program's, a page probing
local ports -- must not end a real sign-in. The first callback with the right
``state`` is accepted, the port stops listening, and its tab is answered once
the caller says how the sign-in ended.

Everything that decides what a request means is in :func:`classify_request`,
which touches no socket, so it can be checked against the shared vectors on
its own. The listener is plain asyncio streams: it writes nothing to a log, so
the code and the state in a request line never reach one.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import secrets
import socket
import time
from typing import Any, Literal
from urllib.parse import parse_qsl

__all__ = [
    "Callback",
    "LoopbackListener",
    "NOT_COMPLETED_TEXT",
    "OAUTH_ERRORS",
    "PAGE_HEADERS",
    "SIGNED_IN_TEXT",
    "classify_request",
    "response_bytes",
]

#: The one path served.
CALLBACK_PATH = "/callback"
#: What the tab shows when the sign-in worked.
SIGNED_IN_TEXT = "Signed in to Thalovant. You can close this tab."
#: What the tab shows when it did not: denied, failed, or the exchange refused.
NOT_COMPLETED_TEXT = "Sign-in was not completed."
#: What a tab whose answer belongs to another sign-in shows.
WRONG_STATE_TEXT = "This answer does not belong to the sign-in in progress."
#: What anything other than ``GET /callback`` shows.
NOT_FOUND_TEXT = "Not found."
#: Sent with every answer. The pages load nothing, and nothing they show may
#: be cached or leak the callback's address (code and state) as a referrer.
PAGE_HEADERS = (
    ("Content-Type", "text/html; charset=utf-8"),
    ("Cache-Control", "no-store"),
    ("Referrer-Policy", "no-referrer"),
    ("Content-Security-Policy", "default-src 'none'"),
    ("X-Content-Type-Options", "nosniff"),
)
_REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found"}
#: The largest request head read; a callback's is a few hundred bytes.
MAX_REQUEST_HEAD = 16 * 1024
#: How long one connection may take to send its request head.
REQUEST_READ_TIMEOUT = 10.0
#: The ``error`` values RFC 6749 section 4.1.2.1 defines. Only these are kept
#: from a callback: anything else is a value somebody else chose, and it could
#: be the state or a code, which must never reach an error message.
OAUTH_ERRORS = frozenset({
    "invalid_request", "unauthorized_client", "access_denied", "unsupported_response_type",
    "invalid_scope", "server_error", "temporarily_unavailable",
})
#: How long closing waits for an accepted tab's page to be written.
CLOSE_GRACE = 1.0


@dataclass(frozen=True)
class Callback:
    """An accepted callback: the browser came back with this sign-in's state.

    ``code`` when the person approved; ``denied`` for ``error=access_denied``;
    ``failed`` for any other ``error``, or for neither an error nor exactly one
    code. ``error`` is the ``error`` the page sent when it is one of
    :data:`OAUTH_ERRORS`, and ``None`` otherwise.
    """

    outcome: Literal["code", "denied", "failed"]
    code: str | None = field(default=None, repr=False)
    error: str | None = None


def classify_request(method: str, target: str, state: str) -> tuple[int, Callback | None]:
    """What the listener answers one request with, and the callback it accepts.

    ``(404, None)`` for anything but ``GET`` on exactly ``/callback``;
    ``(400, None)`` when ``state`` is missing, given more than once, or not
    this sign-in's (compared in constant time, as bytes); otherwise ``(200,
    callback)``. An ``error`` wins over a ``code`` sent with it: a refusal that
    also carries a code is still a refusal. An ``error`` outside
    :data:`OAUTH_ERRORS` is not kept. Query values are decoded once, as a form
    decodes them.
    """

    path, _, query = target.partition("?")
    if method != "GET" or path != CALLBACK_PATH:
        return 404, None
    values: dict[str, list[str]] = {}
    for key, value in parse_qsl(query, keep_blank_values=True):
        values.setdefault(key, []).append(value)
    given = values.get("state", [])
    if len(given) != 1 or not secrets.compare_digest(given[0].encode("utf-8"), state.encode("utf-8")):
        return 400, None
    errors = values.get("error", [])
    if errors:
        if errors == ["access_denied"]:
            return 200, Callback("denied", error="access_denied")
        return 200, Callback("failed", error=errors[0] if errors[0] in OAUTH_ERRORS else None)
    codes = values.get("code", [])
    if len(codes) == 1 and codes[0]:
        return 200, Callback("code", code=codes[0])
    return 200, Callback("failed")


def _page(text: str) -> bytes:
    """A page that loads nothing: no script, no style sheet, no image, no link."""

    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Thalovant</title></head><body><p>{text}</p></body></html>\n"
    ).encode("utf-8")


def response_bytes(status: int, text: str, *, head: bool = False) -> bytes:
    """A whole HTTP/1.1 answer: the status, :data:`PAGE_HEADERS`, and a small page.

    The answer to a ``HEAD`` carries no body (RFC 9110 section 9.3.2); its
    ``Content-Length`` is the length the page would have had.
    """

    body = _page(text)
    lines = [f"HTTP/1.1 {status} {_REASONS[status]}"]
    lines += [f"{name}: {value}" for name, value in PAGE_HEADERS]
    lines += [f"Content-Length: {len(body)}", "Connection: close", "", ""]
    return "\r\n".join(lines).encode("ascii") + (b"" if head else body)


def _request_line(head: bytes) -> tuple[str, str] | None:
    """The method and target of a request head, or ``None`` when it has no request line."""

    line = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = line.split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        return None
    return parts[0], parts[1]


class LoopbackListener:
    """Waits on ``127.0.0.1`` for the browser to bring this sign-in's answer back.

    :meth:`start` binds the port (before any URL is built), :meth:`wait`
    returns the accepted :class:`Callback` or ``None`` at the deadline,
    :meth:`finish` says which page its tab gets, and :meth:`close` stops
    everything. Never binds anything but the loopback address.
    """

    def __init__(self, state: str) -> None:
        self._state = state
        self._server: asyncio.AbstractServer | None = None
        self._accepted: asyncio.Future[Callback] | None = None
        self._answer: asyncio.Future[bool] | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        # The connection that brought the accepted callback, whose tab is
        # still owed its page.
        self._accepted_task: asyncio.Task[Any] | None = None

    async def start(self) -> int:
        """Bind ``127.0.0.1`` on a port the system picks; return the port."""

        loop = asyncio.get_running_loop()
        self._accepted = loop.create_future()
        self._answer = loop.create_future()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:  # pragma: no cover - Windows: no other socket may share the port
                sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            sock.bind(("127.0.0.1", 0))
            sock.listen(8)
            sock.setblocking(False)
            port = int(sock.getsockname()[1])
            self._server = await asyncio.start_server(self._handle, sock=sock, limit=MAX_REQUEST_HEAD)
        except BaseException:
            sock.close()
            raise
        return port

    async def wait(self, timeout: float) -> Callback | None:
        """The first accepted callback, or ``None`` once ``timeout`` seconds have passed.

        Measured on the monotonic clock and never cut short: a timer that
        wakes early waits again for what is left.
        """

        accepted = self._accepted
        if accepted is None:
            raise RuntimeError("The loopback listener was not started.")
        deadline = time.monotonic() + timeout
        while not accepted.done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.wait({accepted}, timeout=remaining)
        return accepted.result()

    def finish(self, signed_in: bool) -> None:
        """Answer the accepted tab: signed in, or not completed."""

        if self._answer is not None and not self._answer.done():
            self._answer.set_result(signed_in)

    async def close(self) -> None:
        """Stop listening, let an accepted tab have its page, close every connection."""

        self.finish(False)
        server, self._server = self._server, None
        if server is not None:
            server.close()
        if self._accepted_task is not None and not self._accepted_task.done():
            # Only the accepted tab is waited for. A browser also opens
            # connections it never sends anything on; those are just closed.
            await asyncio.wait({self._accepted_task}, timeout=CLOSE_GRACE)
        tasks = set(self._tasks)
        for writer in list(self._writers):
            writer.close()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(server.wait_closed(), CLOSE_GRACE)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One connection, kept track of so that :meth:`close` can end it."""

        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._writers.add(writer)
        try:
            await self._serve(reader, writer)
        except Exception:  # noqa: BLE001 - one connection's failure is that connection's alone
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            if task is not None:
                self._tasks.discard(task)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Read one request head, answer it, and hand an accepted callback to :meth:`wait`."""

        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), REQUEST_READ_TIMEOUT)
        except asyncio.LimitOverrunError:
            writer.write(response_bytes(400, WRONG_STATE_TEXT))
            await writer.drain()
            return
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            return
        request = _request_line(head)
        if request is None:
            writer.write(response_bytes(400, WRONG_STATE_TEXT))
            await writer.drain()
            return
        status, callback = classify_request(request[0], request[1], self._state)
        accepted, answer = self._accepted, self._answer
        if callback is None or accepted is None or answer is None or accepted.done():
            # A second answer with the right state, after the first was
            # accepted, is not this sign-in's any more either.
            if callback is not None:
                status = 400
            writer.write(response_bytes(status, NOT_FOUND_TEXT if status == 404 else WRONG_STATE_TEXT,
                                        head=request[0] == "HEAD"))
            await writer.drain()
            return
        accepted.set_result(callback)
        self._accepted_task = asyncio.current_task()
        if self._server is not None:
            # The first accepted callback ends the listening; a tab already
            # connected is still answered.
            self._server.close()
        # Until the sign-in has ended, however long the exchange takes: the
        # page says how it ended, so it cannot be written before it has.
        # close() always answers, so this never outlives the sign-in.
        signed_in = await answer
        writer.write(response_bytes(200, SIGNED_IN_TEXT if signed_in else NOT_COMPLETED_TEXT))
        await writer.drain()
