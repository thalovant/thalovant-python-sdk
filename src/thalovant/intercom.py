"""Intercom: say a short text aloud on a household hub's speakers.

An announcement is a text of at most 280 characters, spoken by each chosen
speaker's own text-to-speech. It is announce-only: there is no two-way audio.
Intercom does not record or store speech; while an urgent alert sounds, the hub
only checks whether a reply is an acknowledgement. **It is not a certified
safety system** and does not replace smoke or CO alarms, medical alert devices
or emergency services.

The API does not store the text: it keeps a keyed hash of it (HMAC-SHA256,
server-side secret) and its length, the targets, each speaker's result and the
actor. (The hub's own message bus and each speaker's text-to-speech are outside
the platform's control.) These are the typed answers of the routes under
``/v1/hubs/{hub_id}/intercom``, and the request builders. The calls live on
:class:`~thalovant.control.AsyncThalovantControlPlane` and
:class:`~thalovant.control.ThalovantControlPlane` (``announce``,
``cancel_announcement``, ``get_announcement``, ``list_announcements``,
``list_intercom_speakers``, ``update_intercom_speaker``,
``get_intercom_settings`` and ``set_intercom_settings``). The names say
*announce* and *intercom_*, never plain ``intercom``: the HiveMind mesh already
has an ``intercom`` frame (:mod:`thalovant.client`), which is a different thing.

Scopes: ``intercom:read`` for the reads, ``intercom:send`` for ``announce`` and
``cancel_announcement``, ``intercom:urgent`` **in addition to** ``intercom:send``
for an urgent announcement (``priority="urgent"``, and so every ``repeat``), and
``hubs:write`` (owner or admin of the hub) to change settings or a speaker's
policy. A token without ``intercom:urgent`` is refused before anything is
spent (403 ``urgent_scope_required``, :class:`~thalovant.errors.ThalovantUrgentScopeRequiredError`).

Idempotency: pass a fresh unique ``idempotency_key`` (a ``uuid4``) for each
announcement you mean to send. The same key replays the stored answer
(``Idempotency-Replayed: true``) and **never sends again**, whatever its status;
a replayed ``failed`` announcement stays ``failed``. To send the same text again
on purpose, use a new key.

Every status is an open enum: a value this SDK does not know yet is kept as
the string the API sent, never refused.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Literal, Mapping, Sequence, Union
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ThalovantAPIError

#: Why intercom cannot run on a hub: the platform's own switch is off (``platform``,
#: which wins over every other reason), the owner's plan, a kid hub, or a public hub.
IntercomUnavailableReason = Literal["platform", "plan", "kid_hub", "public_hub"]
#: ``normal`` waits for the hub's answer; ``urgent`` may repeat until acknowledged.
IntercomPriority = Literal["normal", "urgent"]
#: ``sending`` while an urgent announcement is still repeating; ``cancelled`` after ``cancel_announcement``.
AnnouncementStatus = Literal["completed", "partial", "failed", "sending", "cancelled"]
#: What happened for one speaker.
DeliveryStatus = Literal[
    "delivered", "acked", "offline", "skipped_dnd", "skipped_not_receiving", "failed"
]

#: Longest announcement, in characters.
MAX_ANNOUNCEMENT_TEXT = 280
#: Most speakers one announcement may name.
MAX_ANNOUNCEMENT_SPEAKERS = 50
#: Most repeats, and the interval range in seconds, of an urgent announcement.
MAX_REPEAT_TIMES = 10
MIN_REPEAT_INTERVAL_SECONDS = 5
MAX_REPEAT_INTERVAL_SECONDS = 120
#: Limits of a speaker's policy.
MAX_ROOM_LENGTH = 64
MAX_GROUP_LENGTH = 32
MAX_GROUPS = 8
#: Longest name in a ``speakers``, ``rooms`` or ``groups`` target, and most names in one list.
MAX_TARGET_NAME_LENGTH = 64
MAX_TARGET_NAMES = 50
#: Longest ``Idempotency-Key`` the API takes.
MAX_IDEMPOTENCY_KEY_LENGTH = 128
#: Longest IANA timezone name the API takes.
MAX_TIMEZONE_LENGTH = 64

#: The scopes of the intercom routes. ``intercom:urgent`` is needed IN ADDITION to
#: ``intercom:send`` for an urgent announcement; it is not implied by anything and
#: implies nothing. Changing settings or a speaker needs ``hubs:write``.
INTERCOM_READ_SCOPE = "intercom:read"
INTERCOM_SEND_SCOPE = "intercom:send"
INTERCOM_URGENT_SCOPE = "intercom:urgent"

#: What a delivery's ``detail`` can be (a fixed code, never free text). Treat a value
#: that is not in this set as opaque: it is kept as sent.
DELIVERY_DETAIL_CODES = frozenset({
    "disabled", "sender_not_allowed", "rate_limited", "invalid_text", "text_too_long", "invalid_priority",
    "too_many_targets", "no_targets", "internal_error", "duplicate_id", "busy", "not_running", "invalid_id",
    "policy_changed", "dnd_unreadable", "cancelled", "intercom_disabled", "intercom_disabled_platform",
    "no_result", "unknown_status", "no_matching_speakers", "hub_no_response", "hub_unreachable", "hub_not_ready",
    "hub_transport_error", "skill_not_installed", "stopped",
})

#: Unicode categories an announcement may not hold: controls (Cc), format characters
#: (Cf: bidirectional overrides and isolates, zero-width spaces, tag characters), lone
#: surrogates (Cs), private use (Co), and line and paragraph separators (Zl, Zp).
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})
#: The two format characters scripts and emoji sequences need: allowed, as the API allows them.
_JOINERS = frozenset("\u200c\u200d")
_MARKUP = frozenset("<>")
_LANG = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$")
#: The shape of an IANA timezone name, used only where this machine has no timezone database.
_ZONE_SHAPE = re.compile(r"^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$")
#: Characters an id may not hold: they would change a request path.
_ID_UNSAFE = re.compile(r"[/?#\\\x00-\x1f\x7f-\x9f]")
_CLOCK = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


class _Unset:
    """The type of :data:`UNSET`."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


#: "Leave this field as it is" in :meth:`~thalovant.control.AsyncThalovantControlPlane.update_intercom_speaker`,
#: where ``None`` is a value (``room=None`` clears the room).
UNSET = _Unset()


@dataclass(frozen=True)
class IntercomSettings:
    """A hub's intercom switch (``GET .../intercom/settings``).

    ``plan_allowed`` is false on a plan without intercom;
    ``unavailable_reason`` says why it cannot run (``platform``: the platform's
    own switch is off, which wins over the others; ``plan``; ``kid_hub``;
    ``public_hub``) and is ``None`` when it can. ``enabled`` is true only when
    the hub's switch is on AND ``unavailable_reason`` is ``None``.
    """

    enabled: bool
    plan_allowed: bool
    unavailable_reason: IntercomUnavailableReason | str | None = None

    @classmethod
    def from_dict(cls, payload: Any) -> IntercomSettings:
        body = _mapping(payload, "intercom settings")
        reason = body.get("unavailable_reason")
        return cls(
            enabled=body.get("enabled") is True,
            plan_allowed=body.get("plan_allowed") is True,
            unavailable_reason=reason if isinstance(reason, str) else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IntercomQuietHours:
    """Local quiet hours: ``HH:MM`` start and end, in an IANA ``timezone``.

    Quiet hours that are switched on (``IntercomDnd(enabled=True)``) need a real
    ``timezone`` and a ``start`` that differs from the ``end``: the API refuses a
    window it would have to guess the zone of (422), so the SDK refuses it first.
    """

    start: str
    end: str
    timezone: str | None = None

    @classmethod
    def from_dict(cls, payload: Any) -> IntercomQuietHours | None:
        if not isinstance(payload, Mapping):
            return None
        start, end, zone = payload.get("start"), payload.get("end"), payload.get("timezone")
        if not isinstance(start, str) or not isinstance(end, str):
            return None
        return cls(start=start, end=end, timezone=zone if isinstance(zone, str) else None)

    def as_dict(self) -> dict[str, Any]:
        _clock(self.start, "quiet_hours.start")
        _clock(self.end, "quiet_hours.end")
        if self.timezone is not None:
            _timezone(self.timezone, "quiet_hours.timezone")
        return {"start": self.start, "end": self.end, "timezone": self.timezone}


@dataclass(frozen=True)
class IntercomDnd:
    """Do not disturb: a normal announcement is skipped inside quiet hours; an urgent one is not."""

    enabled: bool = False
    quiet_hours: IntercomQuietHours | None = None

    @classmethod
    def from_dict(cls, payload: Any) -> IntercomDnd:
        if not isinstance(payload, Mapping):
            return cls()
        return cls(
            enabled=payload.get("enabled") is True,
            quiet_hours=IntercomQuietHours.from_dict(payload.get("quiet_hours")),
        )

    def as_dict(self) -> dict[str, Any]:
        _flag(self.enabled, "dnd.enabled")
        out: dict[str, Any] = {"enabled": self.enabled}
        if self.quiet_hours is not None:
            out["quiet_hours"] = self.quiet_hours.as_dict()
        if self.enabled:
            # The API refuses an enabled window with no zone (UTC would put a
            # Toronto bedroom's night in the afternoon) or with start == end.
            hours = self.quiet_hours
            if hours is None or hours.timezone is None:
                raise ValueError("dnd.quiet_hours with a timezone is required when dnd is enabled.")
            if hours.start == hours.end:
                raise ValueError("dnd.quiet_hours start and end must differ when dnd is enabled.")
        return out


@dataclass(frozen=True)
class IntercomPolicy:
    """A speaker's intercom policy. ``receive`` is false until someone turns it on."""

    receive: bool = False
    room: str | None = None
    groups: tuple[str, ...] = ()
    dnd: IntercomDnd = field(default_factory=IntercomDnd)

    @classmethod
    def from_dict(cls, payload: Any) -> IntercomPolicy:
        if not isinstance(payload, Mapping):
            return cls()
        room = payload.get("room")
        groups = payload.get("groups")
        return cls(
            receive=payload.get("receive") is True,
            room=room if isinstance(room, str) else None,
            groups=tuple(g for g in groups if isinstance(g, str)) if isinstance(groups, list) else (),
            dnd=IntercomDnd.from_dict(payload.get("dnd")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"receive": self.receive, "room": self.room, "groups": list(self.groups), "dnd": self.dnd.as_dict()}


@dataclass(frozen=True)
class IntercomSpeaker:
    """A connection of the hub that may be spoken to, with its policy and presence.

    ``kind`` is the connection kind (``voice_satellite``, ``web_chat``,
    ``developer``, ``embedded``, ``home_assistant``; any other reads
    ``voice_satellite``, the API's own rule). ``can_play`` is the EFFECTIVE
    answer to "can this connection play speech at all": true by default for a
    ``voice_satellite`` and ``web_chat``, false for ``home_assistant``,
    ``developer`` and ``embedded`` unless its owner declared
    ``spec.intercom.can_play: true`` on the connection. One that cannot is listed
    (grey it out) but cannot be given ``receive=True`` (422 ``not_a_speaker``),
    is never part of ``all``, a room or a group, and naming it in an
    announcement is refused. ``can_play`` is declared on the connection
    (``PATCH /v1/clients/{id}``), never on the speaker.

    ``policy_synced`` is set on the answer to
    :meth:`~thalovant.control.AsyncThalovantControlPlane.update_intercom_speaker`:
    ``False`` while the hub may hold an out-of-date consent map (the push failed
    and is retried in the background), so show "Changes may take a moment to reach
    the home". ``None`` when the API did not say (a list item: the list carries
    its own, see :attr:`IntercomSpeakerList.policy_synced`).
    """

    client_id: str
    slug: str | None = None
    name: str | None = None
    online: bool = False
    last_seen_at: str | None = None
    intercom: IntercomPolicy = field(default_factory=IntercomPolicy)
    kind: str = "voice_satellite"
    can_play: bool = True
    policy_synced: bool | None = None

    @classmethod
    def from_dict(cls, payload: Any) -> IntercomSpeaker:
        body = _mapping(payload, "intercom speaker")
        kind = body.get("kind")
        synced = body.get("policy_synced")
        return cls(
            client_id=_required(body, "client_id", "intercom speaker"),
            slug=_str_or_none(body.get("slug")),
            name=_str_or_none(body.get("name")),
            online=body.get("online") is True,
            last_seen_at=_str_or_none(body.get("last_seen_at")),
            intercom=IntercomPolicy.from_dict(body.get("intercom")),
            kind=kind if isinstance(kind, str) and kind else "voice_satellite",
            # The API's own default is true; only an explicit false says "cannot play".
            can_play=body.get("can_play") is not False,
            policy_synced=synced if isinstance(synced, bool) else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "slug": self.slug,
            "name": self.name,
            "kind": self.kind,
            "can_play": self.can_play,
            "online": self.online,
            "last_seen_at": self.last_seen_at,
            "intercom": self.intercom.as_dict(),
            "policy_synced": self.policy_synced,
        }


@dataclass(frozen=True)
class IntercomDelivery:
    """What happened for one speaker. ``status`` keeps a value this SDK does not know.

    ``delivered`` means the hub handed the speech to that speaker's connection;
    there is no playback receipt, so it does not prove the sound came out or that
    anybody heard it. Only ``acked`` (an urgent announcement somebody answered)
    is a person's answer, and ``failed`` is not proof that nothing was spoken.
    ``detail`` is one of :data:`DELIVERY_DETAIL_CODES` or ``None``: a fixed code,
    never free text; a value not in the set is kept as sent.
    """

    client_id: str | None = None
    slug: str | None = None
    name: str | None = None
    status: DeliveryStatus | str = "failed"
    at: str | None = None
    detail: str | None = None

    @classmethod
    def from_dict(cls, payload: Any) -> IntercomDelivery:
        body = _mapping(payload, "intercom delivery")
        return cls(
            client_id=_str_or_none(body.get("client_id")),
            slug=_str_or_none(body.get("slug")),
            name=_str_or_none(body.get("name")),
            status=_required(body, "status", "intercom delivery"),
            at=_str_or_none(body.get("at")),
            detail=_str_or_none(body.get("detail")),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Announcement:
    """An announcement and its deliveries. The text is never part of it: the platform keeps none.

    ``hub_confirmed`` is set only by ``cancel_announcement``: ``False`` means
    the hub could not confirm the cancel (the record is ``cancelled`` anyway),
    so speech already queued on the hub may still play. ``None`` when the API
    did not say, as on every other answer.

    ``replayed`` is ``True`` when this answer is the STORED announcement for an
    ``idempotency_key`` the hub had seen before (the API's response header
    ``Idempotency-Replayed: true``): nothing new was sent, whatever its status. It is
    ``False`` on a first answer and on every read. Do not clear the text a person typed,
    or say "sent", on a replayed answer without saying nothing new was sent.
    ``acked_by`` is the display name of a client of the hub, or ``"unknown"``.
    """

    id: str
    status: AnnouncementStatus | str
    priority: IntercomPriority | str = "normal"
    created_at: str | None = None
    deliveries: tuple[IntercomDelivery, ...] = ()
    acked_by: str | None = None
    hub_confirmed: bool | None = None
    replayed: bool = False

    @property
    def sending(self) -> bool:
        """True while an urgent announcement is still repeating."""
        return self.status == "sending"

    @classmethod
    def from_dict(cls, payload: Any) -> Announcement:
        body = _mapping(payload, "announcement")
        rows = body.get("deliveries")
        if rows is not None and not isinstance(rows, list):
            raise ThalovantAPIError("Thalovant API returned an announcement with unexpected deliveries.")
        confirmed = body.get("hub_confirmed")
        return cls(
            id=_required(body, "id", "announcement"),
            status=_required(body, "status", "announcement"),
            priority=_str_or_none(body.get("priority")) or "normal",
            created_at=_str_or_none(body.get("created_at")),
            deliveries=tuple(IntercomDelivery.from_dict(row) for row in rows or []),
            acked_by=_str_or_none(body.get("acked_by")),
            hub_confirmed=confirmed if isinstance(confirmed, bool) else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "priority": self.priority,
            "created_at": self.created_at,
            "deliveries": [d.as_dict() for d in self.deliveries],
            "acked_by": self.acked_by,
            "hub_confirmed": self.hub_confirmed,
            "replayed": self.replayed,
        }


@dataclass(frozen=True)
class AnnouncementList:
    """A page of announcements, newest first. ``next_cursor`` is ``None`` on the last page."""

    items: tuple[Announcement, ...] = ()
    next_cursor: str | None = None

    @classmethod
    def from_dict(cls, payload: Any) -> AnnouncementList:
        body = _mapping(payload, "announcement list")
        rows = body.get("items", body.get("data"))
        if not isinstance(rows, list):
            raise ThalovantAPIError("Thalovant API returned an unexpected announcement listing shape.")
        cursor = body.get("next_cursor", body.get("cursor"))
        return cls(
            items=tuple(Announcement.from_dict(row) for row in rows),
            next_cursor=cursor if isinstance(cursor, str) and cursor else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {"items": [a.as_dict() for a in self.items], "next_cursor": self.next_cursor}


@dataclass(frozen=True)
class IntercomSpeakerList:
    """Every connection of the hub, as speakers (the platform's own Intercom client is not listed).

    ``policy_synced`` is ``False`` while the hub may hold an out-of-date consent
    map (a push failed and is retried in the background): a speaker whose
    ``receive`` was just turned off can still be spoken to by a routine until it
    clears. Show a quiet "Changes may take a moment to reach the home". ``True``
    when nothing is pending, and while intercom is off; ``None`` when the API did
    not say. ``unavailable_reason`` is the same value as
    :attr:`IntercomSettings.unavailable_reason` (a list read while intercom is
    off platform-wide says ``platform``).
    """

    items: tuple[IntercomSpeaker, ...] = ()
    policy_synced: bool | None = None
    unavailable_reason: IntercomUnavailableReason | str | None = None

    @classmethod
    def from_dict(cls, payload: Any) -> IntercomSpeakerList:
        body = _mapping(payload, "intercom speaker list")
        rows = body.get("items")
        if not isinstance(rows, list):
            raise ThalovantAPIError("Thalovant API returned an unexpected intercom speaker listing shape.")
        synced = body.get("policy_synced")
        return cls(
            items=tuple(IntercomSpeaker.from_dict(row) for row in rows),
            policy_synced=synced if isinstance(synced, bool) else None,
            unavailable_reason=_str_or_none(body.get("unavailable_reason")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": [s.as_dict() for s in self.items],
            "policy_synced": self.policy_synced,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class IntercomRepeat:
    """How an urgent announcement repeats: ``times`` 1-10, ``interval_seconds`` 5-120."""

    times: int
    interval_seconds: int

    def as_dict(self) -> dict[str, Any]:
        for name, value, low, high in (
            ("times", self.times, 1, MAX_REPEAT_TIMES),
            ("interval_seconds", self.interval_seconds, MIN_REPEAT_INTERVAL_SECONDS, MAX_REPEAT_INTERVAL_SECONDS),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"repeat {name} must be an integer from {low} to {high}.")
        return {"times": self.times, "interval_seconds": self.interval_seconds}


@dataclass(frozen=True)
class IntercomTargets:
    """Whom to speak to: all speakers, named speakers, rooms or groups. Exactly one."""

    all: bool = False
    speakers: tuple[str, ...] = ()
    rooms: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()

    @classmethod
    def everyone(cls) -> IntercomTargets:
        """Every speaker that receives: ``{"all": true}``."""
        return cls(all=True)

    @classmethod
    def for_speakers(cls, *speakers: str) -> IntercomTargets:
        """Speakers by client id or slug (at most 50)."""
        return cls(speakers=tuple(speakers))

    @classmethod
    def for_rooms(cls, *rooms: str) -> IntercomTargets:
        return cls(rooms=tuple(rooms))

    @classmethod
    def for_groups(cls, *groups: str) -> IntercomTargets:
        return cls(groups=tuple(groups))

    def as_dict(self) -> dict[str, Any]:
        _flag(self.all, "targets.all")
        names = {
            "speakers": _target_names(self.speakers, "speakers"),
            "rooms": _target_names(self.rooms, "rooms"),
            "groups": _target_names(self.groups, "groups"),
        }
        chosen = [name for name, value in (("all", self.all), *names.items()) if value]
        if len(chosen) != 1:
            raise ValueError("Choose exactly one of all, speakers, rooms or groups as targets.")
        if self.all:
            return {"all": True}
        name = chosen[0]
        items = names[name]
        for item in items:
            _require_text(item, f"a {name[:-1]} target")
            if len(item) > MAX_TARGET_NAME_LENGTH:
                raise ValueError(f"A {name[:-1]} target is at most {MAX_TARGET_NAME_LENGTH} characters.")
        if len(items) > MAX_TARGET_NAMES:
            # 50 names of any kind; "at most 50 speakers" is the API's rule for explicit speakers.
            raise ValueError(f"An announcement names at most {MAX_TARGET_NAMES} {name}.")
        return {name: list(items)}


#: Targets as :meth:`~thalovant.control.AsyncThalovantControlPlane.announce` takes them.
TargetsInput = Union[IntercomTargets, Mapping[str, Any]]
#: A repeat as ``announce`` takes it.
RepeatInput = Union[IntercomRepeat, Mapping[str, Any]]
#: A speaker's do-not-disturb setting as ``update_intercom_speaker`` takes it.
DndInput = Union[IntercomDnd, Mapping[str, Any]]


def announcement_text(text: str) -> str:
    """The text as it is sent: NFC-normalised, trimmed, 1-280 characters, or ``ValueError`` before any request.

    The API's own rule, applied before anything is sent (one rule everywhere): no
    ``<`` or ``>`` (a speaker's text-to-speech may read them as markup), no
    control characters (a newline or a tab included), no bidirectional overrides
    or isolates or other invisible format characters (the zero-width joiner and
    non-joiner, which scripts and emoji need, are allowed), no lone surrogates,
    no private-use characters and no line or paragraph separators. The text is
    refused rather than rewritten, and an error never echoes it.
    """
    if not isinstance(text, str):
        raise ValueError("An announcement's text must be a string.")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("An announcement's text must be valid Unicode (no lone surrogates).") from None
    trimmed = unicodedata.normalize("NFC", text).strip()
    if not trimmed:
        raise ValueError("An announcement's text must not be empty.")
    if len(trimmed) > MAX_ANNOUNCEMENT_TEXT:
        raise ValueError(f"An announcement's text is at most {MAX_ANNOUNCEMENT_TEXT} characters.")
    for char in trimmed:
        if char in _MARKUP:
            raise ValueError("An announcement's text must not contain markup characters (< or >).")
        if char not in _JOINERS and unicodedata.category(char) in _REFUSED_CATEGORIES:
            raise ValueError(
                "An announcement's text must not contain control, bidirectional, format or private-use characters."
            )
    return trimmed


def idempotency_key(value: Any) -> str:
    """An ``Idempotency-Key`` fit to send: a string of 1-128 printable ASCII characters, or ``ValueError``.

    Use a fresh unique key (a ``uuid4``) for each announcement you mean to send:
    the same key replays the stored answer and never sends again.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("idempotency_key must be a non-empty string when given.")
    if len(value) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError(f"idempotency_key is at most {MAX_IDEMPOTENCY_KEY_LENGTH} characters.")
    if not all(" " <= char <= "~" for char in value):
        raise ValueError("idempotency_key must be printable ASCII.")
    return value


def intercom_id(value: Any, name: str) -> str:
    """An id (hub, client or announcement) fit to be one path segment, or ``ValueError`` before any request.

    Refused: anything but a string, an empty or blank one, ``.`` and ``..``
    (dot segments, which a URL resolver would collapse into another path), and
    any id holding ``/``, ``?``, ``#``, a backslash or a control character.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    if value in (".", "..") or _ID_UNSAFE.search(value):
        raise ValueError(
            f"{name} is not a valid id: it is a dot segment or holds '/', '?', '#', a backslash or a control character."
        )
    return value


def announce_body(
    text: str,
    targets: TargetsInput,
    *,
    priority: IntercomPriority | str = "normal",
    repeat: RepeatInput | None = None,
    lang: str | None = None,
) -> dict[str, Any]:
    """The ``POST .../intercom/announce`` body. It holds the text: never log it."""
    if priority not in ("normal", "urgent"):
        raise ValueError('priority must be "normal" or "urgent".')
    body: dict[str, Any] = {"text": announcement_text(text), "targets": _targets(targets), "priority": priority}
    if repeat is not None:
        if priority != "urgent":
            raise ValueError("repeat is only for urgent announcements.")
        body["repeat"] = repeat.as_dict() if isinstance(repeat, IntercomRepeat) else IntercomRepeat(
            times=repeat.get("times"), interval_seconds=repeat.get("interval_seconds")  # type: ignore[arg-type]
        ).as_dict()
    if lang is not None:
        if not isinstance(lang, str) or not _LANG.match(lang):
            raise ValueError('lang must be a language tag such as "en-US" or "fr".')
        body["lang"] = lang
    return body


def speaker_update_body(
    *,
    receive: bool | _Unset = UNSET,
    room: str | None | _Unset = UNSET,
    groups: Sequence[str] | _Unset = UNSET,
    dnd: DndInput | _Unset = UNSET,
) -> dict[str, Any]:
    """The ``PATCH .../intercom/speakers/{id}`` body: only what is passed. ``room=None`` clears the room."""
    body: dict[str, Any] = {}
    if not isinstance(receive, _Unset):
        if not isinstance(receive, bool):
            raise ValueError("receive must be true or false.")
        body["receive"] = receive
    if not isinstance(room, _Unset):
        if room is not None:
            _require_text(room, "room")
            if len(room) > MAX_ROOM_LENGTH:
                raise ValueError(f"room is at most {MAX_ROOM_LENGTH} characters.")
        body["room"] = room
    if not isinstance(groups, _Unset):
        if isinstance(groups, (str, bytes, bytearray)) or not isinstance(groups, Sequence) or len(groups) > MAX_GROUPS:
            raise ValueError(f"groups is a list of at most {MAX_GROUPS} names.")
        for group in groups:
            _require_text(group, "a group")
            if len(group) > MAX_GROUP_LENGTH:
                raise ValueError(f"A group name is at most {MAX_GROUP_LENGTH} characters.")
        body["groups"] = list(groups)
    if not isinstance(dnd, _Unset):
        body["dnd"] = _dnd(dnd)
    if not body:
        raise ValueError("Pass at least one of receive, room, groups or dnd.")
    return body


def settings_body(enabled: bool) -> dict[str, Any]:
    """The ``PUT .../intercom/settings`` body."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false.")
    return {"enabled": enabled}


def _targets(targets: TargetsInput) -> dict[str, Any]:
    if isinstance(targets, IntercomTargets):
        return targets.as_dict()
    if not isinstance(targets, Mapping):
        raise ValueError("targets must be IntercomTargets or a mapping.")
    known = {"all", "speakers", "rooms", "groups"}
    unknown = sorted(set(targets) - known)
    if unknown:
        raise ValueError(f"Unknown target kind: {unknown[0]}.")
    everyone = targets.get("all", False)
    _flag(everyone, "targets.all")
    return IntercomTargets(
        all=everyone,
        speakers=_target_names(targets.get("speakers"), "speakers"),
        rooms=_target_names(targets.get("rooms"), "rooms"),
        groups=_target_names(targets.get("groups"), "groups"),
    ).as_dict()


def _target_names(value: Any, kind: str) -> tuple[str, ...]:
    """A target list as a tuple of names. A bare string is refused, never split into characters."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(f"targets.{kind} must be a list of names, not a single value.")
    return tuple(value)


def _dnd(dnd: DndInput) -> dict[str, Any]:
    if isinstance(dnd, IntercomDnd):
        return dnd.as_dict()
    if not isinstance(dnd, Mapping):
        raise ValueError("dnd must be IntercomDnd or a mapping.")
    unknown = sorted(str(key) for key in set(dnd) - {"enabled", "quiet_hours"})
    if unknown:
        raise ValueError(f"Unknown dnd field: {unknown[0]}.")
    hours = dnd.get("quiet_hours")
    parsed = None
    if hours is not None:
        if not isinstance(hours, Mapping):
            raise ValueError("quiet_hours must be a mapping.")
        unknown = sorted(str(key) for key in set(hours) - {"start", "end", "timezone"})
        if unknown:
            raise ValueError(f"Unknown quiet_hours field: {unknown[0]}.")
        zone = hours.get("timezone")
        if zone is not None and not isinstance(zone, str):
            raise ValueError("quiet_hours.timezone must be an IANA name or null.")
        parsed = IntercomQuietHours(start=hours.get("start"), end=hours.get("end"), timezone=zone)  # type: ignore[arg-type]
    if "enabled" not in dnd:
        # The API replaces the whole do-not-disturb object, and a missing
        # ``enabled`` is stored as false: say so rather than switch it off.
        raise ValueError("dnd.enabled is required: true or false.")
    return IntercomDnd(enabled=dnd["enabled"], quiet_hours=parsed).as_dict()


def _flag(value: Any, name: str) -> None:
    """Refuse anything but a real boolean: ``"true"`` or ``1`` is never read as a choice."""
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false.")


def _clock(value: Any, name: str) -> None:
    if not isinstance(value, str) or not _CLOCK.match(value):
        raise ValueError(f"{name} must be HH:MM.")


def _timezone(value: Any, name: str) -> None:
    """An IANA timezone name the API will accept: at most 64 characters, untrimmed, and one the tz database knows.

    Where this machine has no timezone database at all (Windows without the
    ``tzdata`` package) every name would look unknown, so only the shape is checked
    and the API decides.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_TIMEZONE_LENGTH or value != value.strip():
        raise ValueError(f"{name} must be an IANA timezone name.")
    try:
        ZoneInfo(value)
        return
    except (ZoneInfoNotFoundError, ValueError, OSError):
        pass
    try:
        ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        if _ZONE_SHAPE.match(value):
            return  # no timezone database here: the shape is all that can be checked
    raise ValueError(f"{name} must be an IANA timezone name.")


def _require_text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")


def _mapping(payload: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ThalovantAPIError(f"Thalovant API returned an unexpected {what} shape.")
    return payload


def _required(payload: Mapping[str, Any], key: str, what: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ThalovantAPIError(f"Thalovant API returned an {what} without {key}.")
    return value


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


__all__ = [
    "DELIVERY_DETAIL_CODES",
    "INTERCOM_READ_SCOPE",
    "INTERCOM_SEND_SCOPE",
    "INTERCOM_URGENT_SCOPE",
    "MAX_ANNOUNCEMENT_SPEAKERS",
    "MAX_ANNOUNCEMENT_TEXT",
    "MAX_GROUPS",
    "MAX_GROUP_LENGTH",
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "MAX_REPEAT_INTERVAL_SECONDS",
    "MAX_REPEAT_TIMES",
    "MAX_ROOM_LENGTH",
    "MAX_TARGET_NAMES",
    "MAX_TARGET_NAME_LENGTH",
    "MAX_TIMEZONE_LENGTH",
    "MIN_REPEAT_INTERVAL_SECONDS",
    "UNSET",
    "Announcement",
    "AnnouncementList",
    "AnnouncementStatus",
    "DeliveryStatus",
    "DndInput",
    "IntercomDelivery",
    "IntercomDnd",
    "IntercomPolicy",
    "IntercomPriority",
    "IntercomQuietHours",
    "IntercomRepeat",
    "IntercomSettings",
    "IntercomSpeaker",
    "IntercomSpeakerList",
    "IntercomTargets",
    "IntercomUnavailableReason",
    "RepeatInput",
    "TargetsInput",
    "announce_body",
    "announcement_text",
    "idempotency_key",
    "intercom_id",
    "settings_body",
    "speaker_update_body",
]
