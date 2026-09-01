"""Event names, event models, and context helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from uuid import uuid4

from .rich import ThalovantDisplayItem, display_items_from_event_data, rich_media_from_data, strip_ssml


EVENT_RECOGNIZER_LOOP_UTTERANCE = "recognizer_loop:utterance"
EVENT_SPEAK = "speak"
EVENT_OVOS_UTTERANCE_SPEAK = "ovos.utterance.speak"
EVENT_UTTERANCE_HANDLED = "ovos.utterance.handled"
# Legacy Mycroft name for an utterance that matched no intent.
EVENT_INTENT_FAILURE = "complete_intent_failure"
# Current OVOS name for the same terminal "no intent matched" event.
EVENT_INTENT_UNMATCHED = "ovos.intent.unmatched"
EVENT_POLICY_DENIED = "hive.policy.denied"
EVENT_QUERY_TIMEOUT = "hive.query.timeout"
FAILURE_EVENTS = (
    EVENT_INTENT_FAILURE,
    EVENT_INTENT_UNMATCHED,
    EVENT_POLICY_DENIED,
    EVENT_QUERY_TIMEOUT,
)

EventHandler = Callable[["ThalovantEvent"], Any]
EventPredicate = Callable[["ThalovantEvent"], bool]


@dataclass(frozen=True)
class ThalovantEvent:
    """A normalized event received from a hub."""

    name: str
    data: dict[str, Any]
    context: dict[str, Any]
    raw: Any

    @property
    def text(self) -> str:
        """Best-effort text extracted from common OVOS/HiveMind payloads."""

        utterance = self.data.get("utterance") or self.data.get("text")
        if isinstance(utterance, str):
            return utterance
        utterances = self.utterances
        return utterances[0] if utterances else ""

    @property
    def display_text(self) -> str:
        """Text suitable for visual display with simple SSML/XML tags removed."""

        return strip_ssml(self.text)

    @property
    def utterances(self) -> tuple[str, ...]:
        """Return normalized utterance strings from the event payload."""

        raw = self.data.get("utterances")
        if isinstance(raw, str):
            return (raw,)
        if isinstance(raw, (list, tuple)):
            return tuple(item for item in raw if isinstance(item, str))
        utterance = self.data.get("utterance")
        return (utterance,) if isinstance(utterance, str) else ()

    @property
    def session_id(self) -> str | None:
        return _session_id_from_context(self.context)

    @property
    def site_id(self) -> str | None:
        session = _session_from_context(self.context)
        site_id = session.get("site_id") or self.context.get("site_id")
        return str(site_id) if site_id is not None else None

    @property
    def request_id(self) -> str | None:
        return _request_id_from_context(self.context) or _request_id_from_mapping(self.data)

    @property
    def lang(self) -> str | None:
        session = _session_from_context(self.context)
        lang = self.data.get("lang") or self.context.get("lang") or session.get("lang")
        return str(lang) if lang is not None else None

    @property
    def is_policy_denied(self) -> bool:
        return self.name == EVENT_POLICY_DENIED

    @property
    def is_failure(self) -> bool:
        return self.name in FAILURE_EVENTS

    @property
    def rich_media(self) -> dict[str, Any]:
        """Return normalized rich media data from the event payload."""

        return rich_media_from_data(self.data)

    def display_items(self, *, max_text_chars: int | None = None) -> tuple[ThalovantDisplayItem, ...]:
        """Return UI-friendly text/media/table/choice items for this event."""

        return display_items_from_event_data(
            self.data,
            event_name=self.name,
            max_text_chars=max_text_chars,
        )

    def matches_context(
        self,
        context: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> bool:
        """Return whether this event belongs to the requested session/request.

        Missing event context is treated as compatible for older hubs that do
        not echo session metadata on every reply.
        """

        expected = _context_with_correlation(
            context,
            session_id=session_id,
            request_id=request_id,
        )
        return _event_matches_context(self, expected)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "data": self.data,
            "context": self.context,
            "text": self.text,
            "display_text": self.display_text,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "display_items": [item.as_dict() for item in self.display_items()],
        }


def _new_session_id() -> str:
    return f"thalovant-session-{uuid4().hex}"


def _new_request_id() -> str:
    return f"thalovant-request-{uuid4().hex}"


def _utterance_payload(text: str, lang: str) -> dict[str, Any]:
    return {"utterances": [text], "lang": lang}


def _merge_context(
    base: dict[str, Any] | None,
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(base or {})
    for key, value in (extra or {}).items():
        if key == "session" and isinstance(value, dict):
            merged["session"] = {**_session_from_context(merged), **value}
        else:
            merged[key] = value
    return merged


def _context_with_correlation(
    raw_context: dict[str, Any] | None,
    *,
    session_id: str | None = None,
    site_id: str | None = None,
    lang: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    context = dict(raw_context or {})
    session = _session_from_context(context)

    if session_id:
        session["session_id"] = session_id
    if site_id:
        session.setdefault("site_id", site_id)
    if lang:
        session.setdefault("lang", lang)
    if request_id:
        context["request_id"] = request_id
        context["thalovant_request_id"] = request_id
        session["request_id"] = request_id

    if session:
        context["session"] = session
    return context


def _event_matches_context(
    event: ThalovantEvent,
    expected_context: dict[str, Any] | None,
) -> bool:
    expected_session_id = _session_id_from_context(expected_context)
    actual_session_id = event.session_id
    if expected_session_id and actual_session_id and actual_session_id != expected_session_id:
        return False

    expected_request_id = _request_id_from_context(expected_context)
    actual_request_id = event.request_id
    if expected_request_id and actual_request_id and actual_request_id != expected_request_id:
        return False

    return True


def _session_from_context(context: dict[str, Any] | None) -> dict[str, Any]:
    raw_session = (context or {}).get("session")
    return dict(raw_session) if isinstance(raw_session, dict) else {}


def _session_id_from_context(context: dict[str, Any] | None) -> str | None:
    session = _session_from_context(context)
    session_id = session.get("session_id") or (context or {}).get("session_id")
    return str(session_id) if session_id is not None else None


def _request_id_from_mapping(values: dict[str, Any] | None) -> str | None:
    if not isinstance(values, dict):
        return None
    request_id = (
        values.get("request_id")
        or values.get("thalovant_request_id")
        or values.get("correlation_id")
    )
    return str(request_id) if request_id is not None else None


def _request_id_from_context(context: dict[str, Any] | None) -> str | None:
    request_id = _request_id_from_mapping(context)
    if request_id:
        return request_id
    return _request_id_from_mapping(_session_from_context(context))


def _message_data(message: Any) -> dict[str, Any]:
    data = getattr(message, "data", None)
    return data if isinstance(data, dict) else {}


def _message_context(message: Any) -> dict[str, Any]:
    context = getattr(message, "context", None)
    return context if isinstance(context, dict) else {}


def _message_name(message: Any) -> str | None:
    name = getattr(message, "msg_type", None)
    return str(name) if name is not None else None


def _event_from_message(event_name: str, message: Any) -> ThalovantEvent:
    return ThalovantEvent(
        name=_message_name(message) or event_name,
        data=_message_data(message),
        context=_message_context(message),
        raw=message,
    )


def _failure_reason(event: ThalovantEvent | None) -> str:
    if event is None:
        return "Hub reported that the utterance could not be handled."
    reason = event.data.get("reason") or event.data.get("error") or event.data.get("code")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return f"Hub reported {event.name}."


def _runtime_crypto_key(raw_crypto_key: str | None) -> str | None:
    if not isinstance(raw_crypto_key, str):
        return None
    normalized = raw_crypto_key.strip()
    if not normalized:
        return None
    return normalized[:16]


def _runtime_bus_context(
    raw_context: dict[str, Any] | None,
    *,
    useragent: str,
    session_id: str,
    site_id: str | None,
) -> dict[str, Any]:
    context = dict(raw_context or {})
    context.setdefault("source", useragent)
    context.setdefault("platform", useragent)
    context.setdefault("destination", "HiveMind")

    session = _session_from_context(context)
    current_session_id = str(session.get("session_id") or "")
    if not current_session_id or current_session_id == "default":
        session["session_id"] = session_id
    session.setdefault("site_id", site_id)
    context["session"] = session
    return context
