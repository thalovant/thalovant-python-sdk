"""SDK exception hierarchy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class ThalovantError(Exception):
    """Base exception for Thalovant SDK failures."""


class ThalovantIdentityError(ThalovantError):
    """Raised when identity material is missing or invalid."""


class ThalovantConnectionError(ThalovantError):
    """Raised when the HiveMind HTTP connection cannot be established."""


class ThalovantTimeoutError(ThalovantError):
    """Raised when a hub does not answer before the configured timeout."""


class ThalovantRuntimeError(ThalovantError):
    """Raised when the hub reports that a request could not be handled."""


@dataclass(frozen=True)
class ThalovantQuota:
    """The numbers behind a refusal that is a spent allowance, not a policy.

    The intent-quota policy denies with ``intent_quota_exceeded`` and sends
    which counter ran out (``daily``, ``monthly``), what it allows, how much
    was used, and how many seconds until it resets. Without them a caller can
    only say "refused", which is what an app showed somebody who had simply
    used up the day.
    """

    period: str
    limit: int
    used: int
    reset_after: int
    """Seconds until the counter resets, or 0 when the hub did not say."""


MAX_COUNT = 2**53 - 1
"""The largest count the wire can carry, being the largest whole number every JSON decoder holds exactly."""


def _count(value: Any) -> int:
    """A whole, non-negative count from the wire, or 0 -- never a bool, never a guess.

    A negative limit, usage or reset time is not something a policy can mean,
    and passing one through would have an app say "-1 of -5 questions used".
    """
    if isinstance(value, bool):
        return 0
    number: int
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        try:
            number = int(value.strip())
        except ValueError:
            return 0
    else:
        return 0
    # Whole, non-negative, and no larger than every JSON decoder carries
    # exactly. Above 2**53-1 a decoder backed by a double can no longer tell
    # one whole number from the next, so two SDKs would report different
    # allowances for the same denial -- and a count nobody can agree on is
    # worse than none.
    return number if 0 <= number <= MAX_COUNT else 0


class ThalovantPolicyDeniedError(ThalovantRuntimeError):
    """Raised when the hub refuses a message, the instant it does.

    The hub sends ``hive.policy.denied`` as soon as it refuses, with no
    request id, naming the type it refused. Three different things arrive
    under that one name, and each needs something different said about it:

    * an allow-list refusal (``acl_disallowed_type``) -- ask whoever manages
      the connection to allow the type; ``allowed`` lists what it may send;
    * a spent allowance (``intent_quota_exceeded``) -- wait, or raise the
      limit; ``quota`` carries the numbers;
    * a hub whose agent bus is down (``backend_unavailable``) -- nothing the
      caller can fix; try again later.
    """

    QUOTA_EXCEEDED = "intent_quota_exceeded"
    BACKEND_UNAVAILABLE = "backend_unavailable"

    def __init__(
        self,
        denied_type: str,
        *,
        code: str = "",
        reason: str = "",
        allowed: tuple[str, ...] = (),
        quota: ThalovantQuota | None = None,
    ) -> None:
        self.denied_type = denied_type
        self.code = code
        self.reason = reason
        self.allowed = allowed
        self.quota = quota
        super().__init__(_refusal_message(denied_type, code, reason, quota))

    @classmethod
    def from_event(cls, event: "Any") -> "ThalovantPolicyDeniedError":
        raw = getattr(event, "data", None)
        data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        # The policy's own detail rides nested under data.data
        # (hivemind-core _send_policy_denied: "data": verdict.data).
        nested = data.get("data")
        inner: Mapping[str, Any] = nested if isinstance(nested, dict) else {}
        allowed = inner.get("allowed")
        code = str(data.get("code") or "")
        quota = None
        if code == cls.QUOTA_EXCEEDED:
            period = inner.get("period")
            quota = ThalovantQuota(
                period=period if isinstance(period, str) else "",
                limit=_count(inner.get("limit")),
                used=_count(inner.get("used")),
                reset_after=_count(inner.get("reset_after")),
            )
        return cls(
            str(data.get("denied_type") or ""),
            code=code,
            reason=str(data.get("reason") or ""),
            # Only non-empty strings: a number, a null or a blank in the list
            # is not a message type, and stringifying one would put "3" or
            # "None" in front of an operator reading which types to allow.
            allowed=tuple(item.strip() for item in allowed if isinstance(item, str) and item.strip())
            if isinstance(allowed, list)
            else (),
            quota=quota,
        )


def _refusal_message(denied_type: str, code: str, reason: str, quota: ThalovantQuota | None) -> str:
    # Advice follows the kind of refusal. Telling somebody who used up their
    # day to "allow this connection to publish recognizer_loop:utterance" sent
    # them to a settings page that could not help.
    if quota is not None:
        if not (quota.limit or quota.used or quota.reset_after or quota.period):
            # The hub refused on a quota and sent none of the numbers. "All
            # questions used" would be inventing one.
            return f"The hub refused {denied_type!r}: a quota has run out."
        used = f"{quota.used} of {quota.limit}" if quota.limit else "all"
        period = f" {quota.period}" if quota.period else ""
        when = f"; it resets in {quota.reset_after}s" if quota.reset_after else ""
        return f"The hub refused {denied_type!r}: {used}{period} questions used{when}."
    if code == ThalovantPolicyDeniedError.BACKEND_UNAVAILABLE:
        detail = f": {reason}" if reason else ""
        return f"The hub could not reach its assistant{detail}. Try again shortly."
    detail = reason or code or "refused by the hub's policy"
    return (
        f"The hub refused {denied_type!r}: {detail}. Allow this connection to "
        f"publish {denied_type!r} in the dashboard's connection settings."
    )


class ThalovantUnansweredError(ThalovantRuntimeError):
    """Raised when the hub understood a question and has nothing for it.

    ``ovos.intent.unmatched`` (``complete_intent_failure`` from older hubs)
    is neither a refusal nor a fault: nothing went wrong, the question is
    outside what this hub can do. Flattened into a runtime error, a caller
    could only report that something failed.
    """

    def __init__(self, said: str = "") -> None:
        self.said = said
        super().__init__(said or "The hub has no skill that answers this.")


class ThalovantAPIError(ThalovantError):
    """Raised when a control-plane request fails.

    ``status_code`` is the HTTP status when the API answered. Everything else
    the API said rides beside the message rather than inside it:

    * ``problem`` is the whole error body, parsed, when it is a JSON object --
      the Problem+JSON document every Thalovant API refusal is. A structured
      field the API adds is reachable here without a new SDK release:
      ``refused_images``, ``allowed_images`` and ``allowed_repositories`` on a
      ``platform_image_required`` refusal, ``resource``, ``limit`` and ``used``
      on a ``plan_limit`` one.
    * ``code`` is the body's machine-readable code, for branching without
      reading the prose.
    * ``detail`` is the API's own sentence, whole, exactly as sent.

    The message is a single bounded line for display; it can be shortened, so
    it is never where to read what the API said. A value the body echoed back
    from the request never reaches the message, only ``problem``.

    Passing ``problem`` alone derives ``code`` and ``detail`` from it; an
    explicit ``code`` or ``detail`` wins. All three are ``None`` for a local
    failure, such as a missing token or an unexpected response shape.
    """

    def __init__(
        self,
        *args: object,
        status_code: int | None = None,
        code: str | None = None,
        detail: str | None = None,
        problem: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(*args)
        self.status_code = status_code
        self.problem: dict[str, Any] | None = dict(problem) if isinstance(problem, Mapping) else None
        read_code, read_detail = _problem_fields(self.problem)
        self.code = code if code is not None else read_code
        self.detail = detail if detail is not None else read_detail


def _problem_text(value: Any) -> str | None:
    """A string with something in it, exactly as sent; anything else is absent."""
    return value if isinstance(value, str) and value.strip() else None


def _problem_fields(problem: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    """The ``code`` and ``detail`` of an API error body.

    Read from the body's own members first. When ``detail`` is itself an
    object, it is FastAPI's envelope around a structured refusal -- what the
    API sends when its Problem+JSON handler has not lifted that object's
    members to the top -- so the code and the sentence are read from inside it.
    Nothing is trimmed or shortened: ``detail`` is the whole sentence.
    """
    if problem is None:
        return None, None
    member = problem.get("detail")
    nested: Mapping[str, Any] = member if isinstance(member, Mapping) else {}
    code = _problem_text(problem.get("code")) or _problem_text(nested.get("code"))
    detail = _problem_text(member) or _problem_text(nested.get("detail"))
    return code, detail


class ThalovantAPIUnreachableError(ThalovantAPIError, ThalovantConnectionError):
    """Raised when the control plane could not be reached at all.

    DNS, the TCP connection, TLS, a proxy, or the request's own timeout: the
    API never answered, so there is no ``status_code``, ``code``, ``detail`` or
    ``problem``. Both an API error, which is what 0.8 raised here, so an
    ``except ThalovantAPIError`` still catches it, and a connection error, so
    a caller can tell "the API is out of reach, try again later" from "the API
    answered no" without reading the message.
    """


class ThalovantUnsupportedProtocolError(ThalovantError):
    """Raised when a requested data-plane protocol is not supported locally."""


class ThalovantHubRefusedError(ThalovantConnectionError):
    """Raised when a hub turns this connection's credentials away.

    A hub closes the socket without a status for an access key it does not
    know, with 1008 for a malformed authorization, and aborts the Noise
    handshake for a wrong password. None of those clears up on its own the way
    a dropped network does: the connection was deleted, or its secret changed.
    A caller that reconnects forever on this is dialling a door that is shut.
    """


class ThalovantAuthError(ThalovantAPIError):
    """Raised when the control plane rejects the API token itself.

    A 401: the token is unknown, expired or revoked. Signing in again is the
    fix, which is not true of any other refusal.
    """


class ThalovantPlanError(ThalovantAPIError):
    """Raised when the account's plan does not allow the request.

    A 402, or a 403 whose code is ``plan_limit``. ``problem`` carries the
    ``resource``, ``limit`` and ``used`` the API reported.
    """


class ThalovantAlreadyLinkedError(ThalovantAPIError):
    """Raised when a hub already has the one connection of this kind it allows.

    A 409 ``home_assistant_already_linked``: a hub takes one Home Assistant
    connection. ``client_id`` names the connection that holds the link when
    the API said which.
    """

    def __init__(self, *args: object, client_id: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.client_id = client_id


class ThalovantUnsupportedConnectionTypeError(ThalovantAPIError):
    """Raised when the API does not know the connection type asked for.

    A 422 naming ``connection_type``, or a created connection whose type did
    not come back as asked: an API that silently ignores the field would hand
    out an ordinary connection with the grants of one. The SDK deletes such a
    connection before raising.
    """


class ThalovantDeviceLoginPending(ThalovantAPIError):
    """Raised by one device-login poll while the person has not decided yet.

    ``interval`` is how many seconds to wait before the next poll, already
    lengthened when the API asked to slow down.
    """

    def __init__(self, *args: object, interval: float = 5.0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.interval = interval


class ThalovantDeviceLoginExpired(ThalovantAPIError):
    """Raised when the device code expired before anybody approved it."""


class ThalovantDeviceLoginDenied(ThalovantAPIError):
    """Raised when the person declined the sign-in."""


class ThalovantAdmissionTimeoutError(ThalovantConnectionError, ThalovantTimeoutError):
    """Raised when a hub has not admitted a new connection within the wait.

    Both a connection error and a timeout: the connection exists and may still
    be admitted, so waiting longer, or connecting later, can succeed.
    """


class ThalovantAdmissionFailedError(ThalovantConnectionError):
    """Raised when the operation that admits a new connection failed or timed out.

    ``error_code`` is the operation's own code, when it had one.
    """

    def __init__(self, *args: object, error_code: str | None = None) -> None:
        super().__init__(*args)
        self.error_code = error_code
