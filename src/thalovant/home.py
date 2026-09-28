"""The Home Assistant link: a hub asks a home, and the home always answers.

A home skill on the hub sends ``thalovant.home.request`` to the account's Home
Assistant connection; the integration hands the utterance to Home Assistant's
conversation agent and answers with ``thalovant.home.response``. The rules
every SDK keeps (``home-link-vectors.json``):

- every request gets at most one answer, and never after the hub's 10
  seconds: the handler's time (9 s by default) and the reply's own sending
  both come out of that bound, and a reply that could only arrive late is not
  sent at all;
- the answer is a reply (OVOS-MSG-1 §5.2), so it goes back the way the request
  came;
- ``speech`` is plain text, never markup: tags, comments and processing
  instructions removed; numeric character references, the five XML entities
  and ``&nbsp;`` decoded and nothing else; runs of Unicode white space
  collapsed to one space;
- ``response_type`` is ``action_done``, ``query_answer`` or ``error``, and an
  ``error`` names one ``error_code``. When the SDK has to answer for a handler
  -- it raised, it was too slow, it answered outside the contract -- the speech
  is empty: the hub speaks its own sentence for the code, in the device's
  language, which the SDK does not know.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Union

from .events import ThalovantEvent, _event_from_message
from .rich import strip_ssml

__all__ = [
    "ERROR_CODES",
    "HOME_REQUEST",
    "HOME_RESPONSE",
    "HOME_REQUEST_TIMEOUT",
    "RESPONSE_TYPES",
    "HomeAnswer",
    "HomeRequest",
    "answer_home_request",
    "answer_home_requests",
    "decode_references",
    "home_response",
    "plain_speech",
]

log = logging.getLogger("thalovant.home")

HOME_REQUEST = "thalovant.home.request"
HOME_RESPONSE = "thalovant.home.response"
#: The hub treats silence after this many seconds as ``timeout``.
HOME_REQUEST_TIMEOUT = 10.0
#: How long a handler has by default: a second inside the hub's bound, so the
#: SDK's own ``timeout`` answer still lands before the hub gives up.
DEFAULT_HANDLER_TIMEOUT = HOME_REQUEST_TIMEOUT - 1.0

RESPONSE_TYPES = ("action_done", "query_answer", "error")
ERROR_CODES = (
    "no_intent_match",
    "no_valid_targets",
    "failed_to_handle",
    "unknown",
    "timeout",
    "agent_unavailable",
)

#: The Unicode White_Space property, spelled out so every SDK collapses the
#: same characters (a regex ``\\s`` differs between languages).
_SPACE = re.compile("[\t\n\v\f\r \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+")
#: The one small set of references every SDK decodes: numeric (decimal and
#: hexadecimal), the five XML entities, and ``&nbsp;``. Any other named
#: reference -- ``&eacute;``, ``&copy;`` -- is left as it is written: HTML's
#: list of named references differs between the libraries SDKs use.
_REFERENCE = re.compile(r"&(?:#([0-9]{1,7})|#[xX]([0-9A-Fa-f]{1,6})|(amp|lt|gt|quot|apos|nbsp));")
_NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "nbsp": "\xa0"}


def _decode_reference(match: re.Match[str]) -> str:
    decimal, hexadecimal, name = match.groups()
    if name:
        return _NAMED[name]
    value = int(decimal) if decimal else int(hexadecimal, 16)
    if value == 0 or 0xD800 <= value <= 0xDFFF or value > 0x10FFFF:
        return match.group(0)  # not a character: left as written
    return chr(value)


def decode_references(text: str) -> str:
    """Decode the portable set of character references, once, left to right."""
    return _REFERENCE.sub(_decode_reference, text)


def plain_speech(text: str | None) -> str:
    """Speech a device can say as it is: markup removed, references decoded, white space collapsed.

    In that order, so ``&lt;b&gt;`` stays the text "<b>". See
    ``home-link-vectors.json`` for the exact rules every SDK follows.
    """
    if not text:
        return ""
    return _SPACE.sub(" ", decode_references(strip_ssml(str(text)))).strip()


@dataclass(frozen=True)
class HomeRequest:
    """One ``thalovant.home.request``: what was said, in which language."""

    request_id: str
    utterance: str
    lang: str | None = None
    conversation_id: str | None = None
    #: The event it arrived as; the answer is a reply to it.
    event: Any = field(default=None, repr=False, compare=False)

    @classmethod
    def from_event(cls, event: ThalovantEvent | Any) -> HomeRequest:
        """Read a request from a :class:`ThalovantEvent` or a raw bus message."""
        if not isinstance(event, ThalovantEvent):
            event = _event_from_message(HOME_REQUEST, event)
        data = event.data if isinstance(event.data, Mapping) else {}

        def text(key: str) -> str | None:
            value = data.get(key)
            return value if isinstance(value, str) and value else None

        return cls(
            request_id=text("request_id") or "",
            utterance=text("utterance") or "",
            lang=text("lang"),
            conversation_id=text("conversation_id"),
            event=event,
        )


@dataclass(frozen=True)
class HomeAnswer:
    """What a handler says back. ``speech`` may carry markup; it is sent as plain text."""

    speech: str = ""
    response_type: str = "action_done"
    error_code: str | None = None
    continue_conversation: bool = False
    conversation_id: str | None = None


HandlerResult = Union[HomeAnswer, Mapping[str, Any], str, None]
Handler = Callable[[HomeRequest], Union[HandlerResult, Awaitable[HandlerResult]]]


def home_response(request: HomeRequest, answer: HomeAnswer | Mapping[str, Any] | str | None) -> dict[str, Any]:
    """The ``thalovant.home.response`` payload for *answer*, held to the contract.

    A plain string is a spoken ``action_done``. An answer outside the contract
    -- an unknown ``response_type`` or ``error_code`` -- becomes ``error`` /
    ``unknown``, keeping its speech.
    """
    if answer is None:
        answer = HomeAnswer(response_type="error", error_code="unknown")
    elif isinstance(answer, str):
        answer = HomeAnswer(speech=answer)
    elif isinstance(answer, Mapping):
        answer = HomeAnswer(
            speech=str(answer.get("speech") or ""),
            response_type=str(answer.get("response_type") or "action_done"),
            error_code=answer.get("error_code") if isinstance(answer.get("error_code"), str) else None,
            continue_conversation=answer.get("continue_conversation") is True,
            conversation_id=answer.get("conversation_id") if isinstance(answer.get("conversation_id"), str) else None,
        )
    response_type = answer.response_type
    error_code = answer.error_code if response_type == "error" else None
    if response_type not in RESPONSE_TYPES or (
        response_type == "error" and error_code not in ERROR_CODES
    ):
        response_type, error_code = "error", "unknown"
    payload: dict[str, Any] = {
        "request_id": request.request_id,
        "speech": plain_speech(answer.speech),
        "response_type": response_type,
        "continue_conversation": bool(answer.continue_conversation),
    }
    if error_code:
        payload["error_code"] = error_code
    conversation_id = answer.conversation_id or request.conversation_id
    if conversation_id:
        payload["conversation_id"] = conversation_id
    return payload


async def _within(awaitable: Awaitable[HandlerResult], seconds: float) -> HandlerResult:
    """The handler's answer, or ``TimeoutError`` the moment *seconds* are up.

    Not ``asyncio.wait_for``: on timeout it cancels the handler and then
    waits for it to finish, so a handler that ignores cancellation -- one
    stuck in a call that cannot be interrupted, or that swallows
    ``CancelledError`` -- would hold the answer back past the hub's bound.
    The handler is cancelled and left to finish on its own; what it returns
    later is dropped.
    """
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait({task}, timeout=seconds)
    if task in done:
        return task.result()
    task.cancel()
    task.add_done_callback(_ignore_late)
    raise asyncio.TimeoutError


def _ignore_late(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.debug("a home handler that had timed out failed afterwards: %r", task.exception())


async def answer_home_request(
    client: Any,
    event: ThalovantEvent | Any,
    handler: Handler,
    *,
    timeout: float = DEFAULT_HANDLER_TIMEOUT,
    hub_timeout: float = HOME_REQUEST_TIMEOUT,
) -> dict[str, Any] | None:
    """Answer one request: run *handler*, then reply whatever happened.

    *client* is anything with ``async reply(event, msg_type, data)``: an
    :class:`~thalovant.AsyncThalovantClient` or an
    :class:`~thalovant.AsyncHubSession`.

    Everything happens inside *hub_timeout*, counted from the call: the hub
    gives up on a request after that, and an answer it has given up on only
    confuses the next one. The handler gets *timeout* or what is left of the
    bound, whichever is less; the reply gets whatever the handler left. A
    reply is never started after the bound, and one still queued behind other
    frames when it passes is withdrawn; a frame already being written is
    finished, since half of one would break the Noise stream. Returns the
    payload when it was sent in time, ``None`` when there was no time left.
    """
    started = time.monotonic()

    def remaining() -> float:
        return hub_timeout - (time.monotonic() - started)

    request = HomeRequest.from_event(event)
    answer: HandlerResult
    handler_timeout = min(timeout, remaining())
    try:
        result = handler(request)
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            answer = await _within(result, max(0.0, handler_timeout))
        else:
            answer = result  # type: ignore[assignment]
    except asyncio.TimeoutError:
        log.debug("home request %s: the handler did not answer within %.3gs", request.request_id, handler_timeout)
        answer = HomeAnswer(response_type="error", error_code="timeout")
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("home request %s: the handler raised", request.request_id)
        answer = HomeAnswer(response_type="error", error_code="failed_to_handle")
    payload = home_response(request, answer)
    left = remaining()
    if left <= 0:
        log.debug("home request %s: no time left to answer; the hub has given up on it", request.request_id)
        return None
    try:
        await asyncio.wait_for(client.reply(request.event, HOME_RESPONSE, payload), left)
    except asyncio.TimeoutError:
        log.debug("home request %s: the reply could not be sent before the hub gave up", request.request_id)
        return None
    return payload


def answer_home_requests(
    client: Any,
    handler: Handler,
    *,
    timeout: float = DEFAULT_HANDLER_TIMEOUT,
) -> Callable[[], None]:
    """Answer every ``thalovant.home.request`` *client* receives. Returns an unsubscriber.

    Each request is answered on a task of its own, so a slow one does not hold
    up the next. Unsubscribing cancels the answers still running.
    """
    tasks: set[asyncio.Task[Any]] = set()

    def on_request(event: ThalovantEvent) -> None:
        task = asyncio.ensure_future(answer_home_request(client, event, handler, timeout=timeout))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        task.add_done_callback(_log_failure)

    subscription = client.on(HOME_REQUEST, on_request)

    def unsubscribe() -> None:
        close = getattr(subscription, "close", subscription)
        close()
        for task in tuple(tasks):
            task.cancel()

    return unsubscribe


def _log_failure(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.warning("could not send a home response: %s", error)
