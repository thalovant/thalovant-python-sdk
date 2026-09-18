"""SDK exception hierarchy."""

from dataclasses import dataclass
from typing import Any


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


def _count(value: Any) -> int:
    """A whole count from the wire, or 0 -- never a bool, never a guess."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


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
        data = getattr(event, "data", None) or {}
        # The policy's own detail rides nested under data.data
        # (hivemind-core _send_policy_denied: "data": verdict.data).
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
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
    """Raised when the control-plane request fails, with its HTTP status if known."""

    def __init__(self, *args: object, status_code: int | None = None) -> None:
        super().__init__(*args)
        self.status_code = status_code


class ThalovantUnsupportedProtocolError(ThalovantError):
    """Raised when a requested data-plane protocol is not supported locally."""
