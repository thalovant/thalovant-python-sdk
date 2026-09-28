"""The Home Assistant link: a hub asks a home, and the home always answers.

A home skill on the hub sends ``thalovant.home.request`` to the account's Home
Assistant connection; the integration hands the utterance to Home Assistant's
conversation agent and answers with ``thalovant.home.response``. The rules
every SDK keeps (``home-link-vectors.json``):

- every request gets exactly one answer, within the hub's 10 seconds;
- the answer is a reply (OVOS-MSG-1 §5.2), so it goes back the way the request
  came;
- ``speech`` is plain text, never markup;
- ``response_type`` is ``action_done``, ``query_answer`` or ``error``, and an
  ``error`` names one ``error_code``. When the SDK has to answer for a handler
  -- it raised, it was too slow, it answered outside the contract -- the speech
  is empty: the hub speaks its own sentence for the code, in the device's
  language, which the SDK does not know.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
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

_SPACE = re.compile(r"\s+")


def plain_speech(text: str | None) -> str:
    """Speech a device can say as it is: markup removed, entities decoded, whitespace collapsed."""
    if not text:
        return ""
    return _SPACE.sub(" ", html.unescape(strip_ssml(str(text)))).strip()


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


async def answer_home_request(
    client: Any,
    event: ThalovantEvent | Any,
    handler: Handler,
    *,
    timeout: float = DEFAULT_HANDLER_TIMEOUT,
) -> dict[str, Any]:
    """Answer one request: run *handler*, then reply whatever happened.

    *client* is anything with ``async reply(event, msg_type, data)``: an
    :class:`~thalovant.AsyncThalovantClient` or an
    :class:`~thalovant.AsyncHubSession`. Returns the payload sent.
    """
    request = HomeRequest.from_event(event)
    answer: HandlerResult
    try:
        result = handler(request)
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            answer = await asyncio.wait_for(result, timeout)
        else:
            answer = result  # type: ignore[assignment]
    except asyncio.TimeoutError:
        log.debug("home request %s: the handler did not answer within %gs", request.request_id, timeout)
        answer = HomeAnswer(response_type="error", error_code="timeout")
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("home request %s: the handler raised", request.request_id)
        answer = HomeAnswer(response_type="error", error_code="failed_to_handle")
    payload = home_response(request, answer)
    await client.reply(request.event, HOME_RESPONSE, payload)
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
