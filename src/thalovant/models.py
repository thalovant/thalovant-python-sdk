"""Public SDK data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .events import EVENT_AUDIO_QUEUE, MEDIA_EVENTS, ThalovantEvent
from .rich import ThalovantDisplayItem, strip_ssml

ThalovantConnectionPhase = Literal[
    "idle",
    "connecting",
    "open",
    "handshake",
    "ready",
    "closed",
    "error",
]


@dataclass(frozen=True)
class ThalovantConnectionInfo:
    """Timing snapshot for the current or most recent transport connection."""

    phase: ThalovantConnectionPhase = "idle"
    started_at: str | None = None
    connected_at: str | None = None
    transport_open_ms: float | None = None
    socket_open_ms: float | None = None
    handshake_ms: float | None = None
    connect_ms: float | None = None
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "started_at": self.started_at,
            "connected_at": self.connected_at,
            "transport_open_ms": self.transport_open_ms,
            "socket_open_ms": self.socket_open_ms,
            "handshake_ms": self.handshake_ms,
            "connect_ms": self.connect_ms,
            "last_error": self.last_error,
        }


@dataclass(frozen=True)
class ThalovantHealth:
    """Snapshot of the SDK's live HiveMind transport state."""

    connected: bool
    handshake_complete: bool
    transport_alive: bool
    last_error: str | None = None
    connection: ThalovantConnectionInfo | None = None

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
            "connection": self.connection.as_dict() if self.connection else None,
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
    #: Skill sounds the hub sent that were over the clip or reply budget and
    #: were left out of ``events`` rather than kept in memory.
    dropped_media: int = 0

    @property
    def ok(self) -> bool:
        return self.handled and self.failure_event is None

    @property
    def lang(self) -> str | None:
        """The language the hub answered in: the first event that names one.

        Taken from the reply rather than the request because the hub is
        entitled to disagree -- a skill with no locale for the session answers
        in its own language, and a client that renders that sentence with the
        requested language's voice is the one outcome that sounds broken rather
        than untranslated. ``None`` when no event says.
        """
        for event in self.events:
            if event.lang:
                return event.lang
        return None

    @property
    def media_events(self) -> tuple[ThalovantEvent, ...]:
        """Speech and embedded skill sounds, in the order the hub sent them.

        ``text`` is the speech joined; this is the same speech with the clips
        between the sentences where a skill put them, for a client that plays
        a reply rather than prints it.
        """
        return tuple(event for event in self.events if event.name in MEDIA_EVENTS)

    @property
    def has_audio(self) -> bool:
        return any(event.name == EVENT_AUDIO_QUEUE for event in self.events)

    @property
    def display_text(self) -> str:
        """Reply text suitable for visual display."""

        return strip_ssml(self.text)

    def display_items(self, *, max_text_chars: int | None = None) -> tuple[ThalovantDisplayItem, ...]:
        """Aggregate UI-friendly items from the reply's events."""

        items: list[ThalovantDisplayItem] = []
        for event in self.events:
            items.extend(event.display_items(max_text_chars=max_text_chars))
        if not items and self.text:
            items.append(ThalovantDisplayItem(kind="text", text=self.display_text))
        return tuple(items)

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "display_text": self.display_text,
            "utterances": list(self.utterances),
            "handled": self.handled,
            "ok": self.ok,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "lang": self.lang,
            "display_items": [item.as_dict() for item in self.display_items()],
            "failure_event": self.failure_event.as_dict() if self.failure_event else None,
            "events": [event.as_dict() for event in self.events],
            "dropped_media": self.dropped_media,
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
