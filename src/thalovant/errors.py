"""SDK exception hierarchy."""

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


class ThalovantPolicyDeniedError(ThalovantRuntimeError):
    """Raised when the hub refuses a message type this connection may not publish.

    The hub answers ``hive.policy.denied`` at once, naming the type and the
    list it does allow; raising here saves the caller a timeout and tells the
    operator exactly what to add to the connection's allow-list.
    """

    def __init__(
        self,
        denied_type: str,
        *,
        code: str = "",
        reason: str = "",
        allowed: tuple[str, ...] = (),
    ) -> None:
        self.denied_type = denied_type
        self.code = code
        self.reason = reason
        self.allowed = allowed
        detail = reason or code or "refused by the hub's policy"
        super().__init__(
            f"The hub refused {denied_type!r}: {detail}. Allow this connection to "
            f"publish {denied_type!r} in the dashboard's connection settings."
        )

    @classmethod
    def from_event(cls, event: "Any") -> "ThalovantPolicyDeniedError":
        data = getattr(event, "data", None) or {}
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        allowed = inner.get("allowed") if isinstance(inner, dict) else None
        return cls(
            str(data.get("denied_type") or ""),
            code=str(data.get("code") or ""),
            reason=str(data.get("reason") or ""),
            allowed=tuple(str(item) for item in allowed) if isinstance(allowed, list) else (),
        )


class ThalovantAPIError(ThalovantError):
    """Raised when the Thalovant control-plane API request fails."""


class ThalovantUnsupportedProtocolError(ThalovantError):
    """Raised when a requested data-plane protocol is not supported locally."""
