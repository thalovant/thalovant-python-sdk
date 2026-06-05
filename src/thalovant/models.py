"""Public SDK data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .events import ThalovantEvent


@dataclass(frozen=True)
class ThalovantHealth:
    """Snapshot of the SDK's live HiveMind HTTP transport state."""

    connected: bool
    handshake_complete: bool
    transport_alive: bool
    last_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.connected and self.handshake_complete and self.transport_alive and not self.last_error

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "connected": self.connected,
            "handshake_complete": self.handshake_complete,
            "transport_alive": self.transport_alive,
            "last_error": self.last_error,
        }


@dataclass(frozen=True)
class ThalovantReply:
    """A normalized response from a hub utterance request."""

    text: str
    utterances: tuple[str, ...] = ()
    handled: bool = False
    session_id: str | None = None
    request_id: str | None = None
    raw_messages: tuple[Any, ...] = field(default_factory=tuple)
    events: tuple[ThalovantEvent, ...] = field(default_factory=tuple)
    failure_event: ThalovantEvent | None = None

    @property
    def ok(self) -> bool:
        return self.handled and self.failure_event is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "utterances": list(self.utterances),
            "handled": self.handled,
            "ok": self.ok,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "failure_event": self.failure_event.as_dict() if self.failure_event else None,
            "events": [event.as_dict() for event in self.events],
        }


@dataclass(frozen=True)
class ThalovantDoctorCheck:
    """One preflight diagnostic result."""

    name: str
    ok: bool
    detail: str
    duration_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "duration_ms": round(self.duration_ms, 3),
        }


@dataclass(frozen=True)
class ThalovantDoctorReport:
    """Preflight diagnostics for an identity and hub connection."""

    identity: dict[str, Any]
    checks: tuple[ThalovantDoctorCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "identity": self.identity,
            "checks": [check.as_dict() for check in self.checks],
        }

    def format(self) -> str:
        lines = [f"Thalovant doctor: {'ok' if self.ok else 'failed'}"]
        for check in self.checks:
            status = "ok" if check.ok else "failed"
            duration = f" ({check.duration_ms:.0f} ms)" if check.duration_ms else ""
            lines.append(f"- {check.name}: {status}{duration} - {check.detail}")
        return "\n".join(lines)
