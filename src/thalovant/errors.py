"""SDK exception hierarchy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence


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
    * ``fields`` names what a refusal is about -- the settings or secrets a
      chat app refused (``bridge_settings_invalid``, ``bridge_secrets_invalid``,
      ``bridge_url_not_public`` ...) -- by name, never by value. Empty when
      the body names none.

    The message is a single bounded line for display; it can be shortened, so
    it is never where to read what the API said. A value the body echoed back
    from the request never reaches the message, only ``problem``.

    Passing ``problem`` alone derives ``code``, ``detail`` and ``fields`` from
    it; an explicit ``code``, ``detail`` or ``fields`` wins. ``code`` and
    ``detail`` are ``None``, and ``fields`` empty, for a local failure, such as
    a missing token or an unexpected response shape.
    """

    def __init__(
        self,
        *args: object,
        status_code: int | None = None,
        code: str | None = None,
        detail: str | None = None,
        problem: Mapping[str, Any] | None = None,
        fields: Sequence[str] | None = None,
    ) -> None:
        super().__init__(*args)
        self.status_code = status_code
        self.problem: dict[str, Any] | None = dict(problem) if isinstance(problem, Mapping) else None
        read_code, read_detail = _problem_fields(self.problem)
        self.code = code if code is not None else read_code
        self.detail = detail if detail is not None else read_detail
        if isinstance(fields, str):
            # One name, not its characters.
            self.fields: tuple[str, ...] = (fields,)
        elif fields is not None:
            self.fields = tuple(item for item in fields if isinstance(item, str))
        else:
            self.fields = _problem_field_names(self.problem)
        #: How long the API asked the caller to wait before trying again, in
        #: seconds, when it said: a 429's ``retry_after_seconds`` (at the top
        #: of the problem or inside its ``detail`` object), else its
        #: ``Retry-After`` or ``RateLimit-Reset`` header. ``None`` otherwise.
        self.retry_after_seconds: float | None = _retry_after(self.problem)


def _problem_text(value: Any) -> str | None:
    """A string with something in it, exactly as sent; anything else is absent."""
    return value if isinstance(value, str) and value.strip() else None


def _retry_after(problem: Mapping[str, Any] | None) -> float | None:
    """A problem's ``retry_after_seconds``, at its top or inside a ``detail`` object.

    The API's per-token 429 is FastAPI's envelope around a structured refusal,
    so the number sits inside ``detail``, as ``code`` does.
    """
    if not isinstance(problem, Mapping):
        return None
    nested = problem.get("detail")
    for source in (problem, nested if isinstance(nested, Mapping) else {}):
        value = source.get("retry_after_seconds")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value)
    return None


def _problem_field_names(problem: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The names a refusal's ``fields`` lists, at the body's top or inside an object ``detail``.

    Only strings: a name, never a value the request sent.
    """
    if not isinstance(problem, Mapping):
        return ()
    nested = problem.get("detail")
    for source in (problem, nested if isinstance(nested, Mapping) else {}):
        value = source.get("fields")
        if isinstance(value, list):
            return tuple(item for item in value if isinstance(item, str))
    return ()


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


class ThalovantClientKeyRejectedError(ThalovantHubRefusedError):
    """Raised when the hub refuses this client's own Noise key: it pinned a different one.

    A hub pins the first static key a connection presents and refuses any
    other for good, closing the link the moment the handshake that showed it
    ends. Two programs that read the same identity but keep their keys in
    different folders -- a satellite and a command-line tool run by another
    user, say -- each present their own key, and whichever came second is
    locked out. No handshake can recover from this (XX shows the same key
    again), so :meth:`AsyncHubSession.run` stops on it at once.

    ``key_folder`` is the folder this client's key is in, and
    ``other_key_folder``, when there is a likely one, where another program
    reading the same identity keeps its key. The fix is to pair again (a new
    connection pins afresh), or to share the key folder: point every program
    that reads this identity at the folder holding the key the hub trusts
    (``noise_state_dir``; ``hub.noise_state_dir`` in thalovant-voice).
    """

    def __init__(
        self,
        *args: object,
        key_folder: str | None = None,
        other_key_folder: str | None = None,
    ) -> None:
        super().__init__(*args)
        self.key_folder = key_folder
        self.other_key_folder = other_key_folder


class ThalovantHubKeyChangedError(ThalovantConnectionError):
    """Raised when a hub answers with a different Noise key than the one pinned for it.

    The first connection to a hub pins its static key; every later one must
    present the same key. A different one is either the hub being replaced or
    somebody in between, and the SDK cannot tell which, so it never connects
    and never replaces the pin by itself. It is a connection error rather than
    a refusal -- the hub did not turn the credentials away -- but retrying
    changes nothing: :meth:`AsyncHubSession.run` stops on it at once. Remove
    the pin only once the new key is known to be the hub's.
    """


class ThalovantAuthError(ThalovantAPIError):
    """Raised when the control plane will not act on the credential itself.

    Three answers, each with its own way out:

    - **401**: the token is unknown, expired or revoked. Sign in again.
    - **403 "Insufficient scopes"**: the token is valid but was not granted
      the scope this call needs. Sign in again asking for that scope (a
      device login's ``scopes``); a new token with the same scopes will be
      refused the same way.
    - **423**: the account is locked. Signing in again does not help until
      it is unlocked, from the dashboard or by support.

    ``status_code`` says which. Any other 403 is not this error: a plan
    limit is :class:`ThalovantPlanError`, and anything else a plain
    :class:`ThalovantAPIError`.
    """


class ThalovantPlanError(ThalovantAPIError):
    """Raised when the account's plan does not allow the request.

    A 402, or a 403 whose code is ``plan_limit``. ``problem`` carries the
    ``resource``, ``limit`` and ``used`` the API reported.
    """


class IntercomProblemCode:
    """The ``code`` of every refusal the intercom routes (and the connection routes beside them) add.

    The API's answers are ``application/problem+json``: read ``code`` first. The
    typed error for each code is named in its comment. A code not listed here
    follows the ordinary mappings by status (a 401 is
    :class:`ThalovantAuthError`, a 429 :class:`ThalovantRateLimitedError`, ...)
    and is otherwise a plain :class:`ThalovantAPIError`, with ``code`` and
    ``problem`` either way.
    """

    #: 403: ``priority="urgent"`` (and so every ``repeat``) needs ``intercom:urgent`` as well as ``intercom:send``.
    URGENT_SCOPE_REQUIRED = "urgent_scope_required"
    #: 429: the hub (or the platform) already has its urgent announcements repeating; ``scope`` says which.
    URGENT_BUSY = "intercom_urgent_busy"
    #: 409: an urgent announcement on a hub whose runtime has no active Intercom skill.
    SKILL_NOT_INSTALLED = "skill_not_installed"
    #: 422: ``receive: true``, or a ``speakers`` target, on a connection that cannot play speech.
    NOT_A_SPEAKER = "not_a_speaker"
    #: 422: a connection named like the platform's own Intercom client.
    RESERVED_NAME = "reserved_name"
    #: 403: the hub is locked; nothing was changed.
    HUB_LOCKED = "hub_locked"
    #: 403: an impersonated session cannot send announcements.
    IMPERSONATION_NOT_ALLOWED = "impersonation_not_allowed"
    #: 422: ``all``, a room or a group resolved to more than 50 speakers.
    TOO_MANY_TARGETS = "too_many_targets"
    #: 422: intercom is never available on this hub (a kid hub or a public hub); ``reason`` says which.
    UNAVAILABLE = "intercom_unavailable"
    #: 503: intercom is switched off for the whole platform; nothing was sent.
    DISABLED_PLATFORM = "intercom_disabled_platform"
    #: 503: the operator has not configured intercom yet; nothing was sent.
    NOT_CONFIGURED = "intercom_not_configured"
    #: 403: the platform-managed Intercom client cannot be managed or given credentials.
    MANAGED_CLIENT = "managed_client"
    #: 409: only an announcement that is still ``sending`` can be cancelled; ``announcement_status`` is its current status.
    NOT_CANCELLABLE = "not_cancellable"
    #: 502: the hub did not take the announcement; ``announcement_id`` names the failed record.
    HUB_UNREACHABLE = "hub_unreachable"
    #: 403 or 422 (any status): the hub's switch is off.
    DISABLED = "intercom_disabled"


def _problem_member(
    problem: Mapping[str, Any] | None, key: str, usable: Callable[[Any], bool] | None = None
) -> Any:
    """A member of a problem body, at its top or inside an object ``detail`` (FastAPI's envelope).

    The first usable one wins: a ``null`` at the top (or a value of the wrong
    type, when ``usable`` says what the member must be) does not hide the one
    inside ``detail``.
    """
    if not isinstance(problem, Mapping):
        return None
    nested = problem.get("detail")
    for source in (problem, nested if isinstance(nested, Mapping) else {}):
        value = source.get(key)
        if value is not None and (usable is None or usable(value)):
            return value
    return None


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _problem_str(problem: Mapping[str, Any] | None, key: str) -> str | None:
    value = _problem_member(problem, key, _is_text)
    return value if isinstance(value, str) else None


def _problem_strs(problem: Mapping[str, Any] | None, key: str) -> tuple[str, ...]:
    value = _problem_member(problem, key, lambda item: isinstance(item, list))
    return tuple(item for item in value if isinstance(item, str) and item) if isinstance(value, list) else ()


class ThalovantIntercomDisabledError(ThalovantAPIError):
    """Raised when intercom is off for the hub (code ``intercom_disabled``), or cannot run on it at all.

    The hub's feature flag is off: turn it on with ``set_intercom_settings``
    (needs ``hubs:write`` and a plan with intercom). The subclasses say the
    stronger cases, which turning the hub's switch on cannot change:
    :class:`ThalovantIntercomUnavailableError` (a kid hub or a public hub) and
    :class:`ThalovantIntercomPlatformDisabledError` (the platform's own switch).
    """


class ThalovantIntercomUnavailableError(ThalovantIntercomDisabledError):
    """Raised when intercom is never available on the hub (422 ``intercom_unavailable``).

    :attr:`reason` is ``kid_hub`` or ``public_hub``: intercom cannot be turned
    on there for any plan. Not retryable.
    """

    @property
    def reason(self) -> str | None:
        """Why: ``kid_hub`` or ``public_hub`` (an open value), or ``None`` when the body did not say."""
        return _problem_str(self.problem, "reason")


class ThalovantIntercomPlatformDisabledError(ThalovantIntercomDisabledError):
    """Raised when intercom is switched off for the whole platform (503 ``intercom_disabled_platform``).

    Nothing was written, sent or spent. Show an explanation, not an error toast:
    only the platform operator can turn it on, and no hub setting changes that.
    Reads (speakers, history) and ``cancel_announcement`` still work.
    """


class ThalovantIntercomNotConfiguredError(ThalovantAPIError):
    """Raised when intercom is not configured on this platform yet (503 ``intercom_not_configured``).

    The operator has not finished setting it up. Nothing was sent. Disabling,
    reads and cancelling are unaffected. Say "Intercom is not available yet".
    """


class ThalovantUrgentScopeRequiredError(ThalovantAuthError):
    """Raised when an urgent announcement is sent without ``intercom:urgent`` (403 ``urgent_scope_required``).

    ``priority="urgent"`` (and so every ``repeat``) needs ``intercom:urgent`` IN
    ADDITION to ``intercom:send``; nothing implies it. It is checked before the
    idempotency lookup, the rate limit and the hub: nothing was spent. Sign in
    again asking for the scope (the plan must allow it), or send a normal
    announcement.
    """


class ThalovantRateLimitedError(ThalovantAPIError):
    """Raised on a 429: too many requests. ``retry_after_seconds`` says how long to wait.

    Intercom limits a hub to 20 normal announcements a minute and 10 urgent
    ones an hour. A full urgent pool is the subclass
    :class:`ThalovantUrgentBusyError`.
    """


class ThalovantUrgentBusyError(ThalovantRateLimitedError):
    """Raised when the urgent pool is full (429 ``intercom_urgent_busy``).

    A hub may have 2 urgent announcements repeating at once, and the platform
    a fixed number per API replica; :attr:`scope` says which one is full
    (``hub`` or ``platform``). ``retry_after_seconds`` says when to ask again.
    Nothing was stored and no rate token was spent; cancel one that is running
    (``cancel_announcement``) or wait. A non-repeating urgent announcement and
    a normal one are not subject to it.
    """

    @property
    def scope(self) -> str | None:
        """``hub`` or ``platform`` (an open value), or ``None`` when the body did not say."""
        return _problem_str(self.problem, "scope")


class ThalovantSkillNotInstalledError(ThalovantAPIError):
    """Raised when the hub has no Intercom skill yet (409 ``skill_not_installed``).

    An urgent announcement needs the skill active on the hub's runtime. Nothing
    was sent. Switch intercom on for the hub (``set_intercom_settings``), wait
    for the skill to install, and try again.
    """


class ThalovantNotASpeakerError(ThalovantAPIError):
    """Raised when a connection that cannot play speech is asked to receive (422 ``not_a_speaker``).

    Two cases. ``update_intercom_speaker(receive=True)`` on a connection whose
    effective ``can_play`` is false: :attr:`kind` is its connection kind. An
    announcement that names such connections in ``targets.speakers``:
    :attr:`not_speakers` lists them. Its owner declares ``can_play`` on the
    connection (``spec.intercom.can_play``), never on the speaker.
    """

    @property
    def kind(self) -> str | None:
        """The connection kind of a refused ``receive`` (``home_assistant``, ``developer``, ``embedded``...), or ``None``."""
        return _problem_str(self.problem, "kind")

    @property
    def not_speakers(self) -> tuple[str, ...]:
        """The named connections that cannot play speech, for a refused announcement; empty otherwise."""
        return _problem_strs(self.problem, "not_speakers")


class ThalovantReservedNameError(ThalovantAPIError):
    """Raised when a connection is created or renamed onto a name the platform reserves (422 ``reserved_name``).

    The name ``thalovant-intercom-...`` and the slug ``thalovant-intercom``
    belong to the platform's own Intercom client. Choose another name.
    """


class ThalovantHubLockedError(ThalovantAPIError):
    """Raised when the hub is locked (403 ``hub_locked``): nothing was changed or sent.

    An announcement, a switch or a speaker change is refused on a locked hub;
    ``set_intercom_settings(hub, False)`` (by a platform administrator) and
    ``cancel_announcement`` still work. Not retryable until the hub is unlocked.
    """


class ThalovantImpersonationNotAllowedError(ThalovantAPIError):
    """Raised when an impersonated session sends an announcement (403 ``impersonation_not_allowed``).

    A staff member looking through somebody's session cannot enable intercom,
    change a speaker or announce. Switching it off and cancelling stay open.
    """


class ThalovantTooManyTargetsError(ThalovantAPIError):
    """Raised when ``all``, a room or a group resolves to more than 50 speakers (422 ``too_many_targets``).

    :attr:`matched` is how many it resolved to. Naming that same room or group
    resolves to the same speakers: choose a narrower target (a smaller room or
    group) or name at most 50 speakers.
    """

    @property
    def matched(self) -> int | None:
        """How many speakers the targets resolved to, or ``None`` when the body did not say."""
        value = _problem_member(self.problem, "matched", _is_count)
        return value if isinstance(value, int) else None


class ThalovantManagedClientError(ThalovantAPIError):
    """Raised on a connection the platform manages (403 ``managed_client``).

    The platform's own Intercom client cannot be edited, paused, deleted,
    given credentials or named in an access policy by anybody; switch Intercom
    off and on for the hub to repair it. It is identified by
    ``spec.intercomApi == "platform"``. :attr:`refused_client_refs` lists the
    managed connections an access policy named, when it was one. Not retryable.
    """

    @property
    def refused_client_refs(self) -> tuple[str, ...]:
        """The managed connections an access policy named; empty otherwise."""
        return _problem_strs(self.problem, "refused_client_refs")


class ThalovantHubUnreachableError(ThalovantAPIError):
    """Raised when the API could not reach the hub to speak (502 ``hub_unreachable``).

    Whether the text was spoken is not guaranteed either way: the hub may have
    spoken it and lost the answer. The API records the attempt as a ``failed``
    announcement and names it in :attr:`announcement_id`.

    **Do not resend blindly.** Read the announcement first
    (``get_announcement(hub_id, error.announcement_id)``). Reusing the same
    ``idempotency_key`` does *not* retry: the API replays the stored ``failed``
    record and never sends again. A ``failed`` record is not proof that nothing
    was spoken: weigh the risk of a repeated announcement, and use a **new**
    key only when you choose to send a second attempt.
    """

    @property
    def announcement_id(self) -> str | None:
        """The announcement the API recorded for the failed attempt (``announcement_id`` in the problem), or ``None``."""
        return _problem_str(self.problem, "announcement_id")


class ThalovantNotCancellableError(ThalovantAPIError):
    """Raised when an announcement cannot be cancelled (409 ``not_cancellable``).

    Only an announcement that is still ``sending`` can be cancelled; one that
    has completed, failed or been cancelled already cannot. :attr:`announcement_status`
    is its current status: refresh the result (``get_announcement``) instead of
    showing an error.
    """

    @property
    def announcement_status(self) -> str | None:
        """The announcement's current status (``completed``, ``partial``, ``failed`` or ``cancelled``), or ``None``."""
        return _problem_str(self.problem, "announcement_status")


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


class ConnectionProblemCode:
    """The ``code`` of every refusal the connection routes gained with home links and home bridges.

    Read ``code`` first; the typed error for each is named in its comment. A
    code the SDK does not list is a plain :class:`ThalovantAPIError`, with
    ``code`` and ``problem`` either way.
    """

    #: 422: no link on this hub; ``reason`` is ``hub_operated_by_another_account`` (a hub another customer runs). Devices are not affected.
    CONNECTION_TYPE_UNAVAILABLE = "connection_type_unavailable"
    #: 422: the kind is not one the API creates; ``known`` lists the ones it does.
    CONNECTION_TYPE_UNKNOWN = "connection_type_unknown"
    #: 422: ``connection_type`` and ``connectionType`` were both sent and differ.
    CONNECTION_TYPE_AMBIGUOUS = "connection_type_ambiguous"
    #: 422: the home bridge's connector list is empty, repeated or names an id it may not serve; ``reason`` says which.
    CONNECTORS_INVALID = "connectors_invalid"
    #: 403: only the account owner, from their own session, may add a device beside a home link; ``link_client_id`` names the link.
    DEVICE_OWNER_ONLY = "device_owner_only_beside_home_link"
    #: 403: the same, for a device's setup session.
    SETUP_SESSION_OWNER_ONLY = "setup_session_owner_only_beside_home_link"
    #: 403: the same, for downloading a device's identity.
    DEVICE_IDENTITY_OWNER_ONLY = "device_identity_owner_only_beside_home_link"
    #: 409: a hub with links does not scale out; ``links`` counts them. Remove the links first.
    HUB_SCALE_OUT_BREAKS_LINKS = "hub_scale_out_breaks_links"
    #: 409: the ``Idempotency-Key`` made something that was since removed; send a new key.
    IDEMPOTENCY_KEY_SPENT = "idempotency_key_spent"
    #: 422: the setting is not for a public hub (the music skill's direct route); ``settings`` names them.
    SKILL_CONFIG_PRIVATE_HUB_ONLY = "skill_config_private_hub_only"
    #: Not an error code: the ``replay.code`` of a connection answered from an earlier request with the same key.
    IDEMPOTENT_REPLAY = "idempotent_replay"


class ThalovantConnectionTypeUnavailableError(ThalovantUnsupportedConnectionTypeError):
    """Raised when a link cannot be made on this hub (422 ``connection_type_unavailable``).

    A kind of :class:`ThalovantUnsupportedConnectionTypeError`, so code that
    handles that keeps working. :attr:`reason` is ``hub_operated_by_another_account``
    when the hub is run by another account: a link (``home_assistant`` or
    ``connector``) is made only on the caller's own hub or one Thalovant runs.
    Choose one of those; devices are not affected. Not retryable on this hub.
    """

    @property
    def reason(self) -> str | None:
        """Why (an open value), or ``None`` when the body did not say."""
        return _problem_str(self.problem, "reason")


class ThalovantConnectorsInvalidError(ThalovantAPIError):
    """Raised when a home bridge's ``spec.connectors`` is refused (422 ``connectors_invalid``).

    :attr:`reason` is ``duplicate`` (an id named twice), ``own_kind``
    (``home_assistant`` has a connection kind of its own and is never a
    bridge's) or ``unknown_or_malformed``; :attr:`known` lists the ids a bridge
    may serve. Send each known id once.
    """

    @property
    def reason(self) -> str | None:
        """``duplicate``, ``own_kind`` or ``unknown_or_malformed`` (an open value), or ``None``."""
        return _problem_str(self.problem, "reason")

    @property
    def known(self) -> tuple[str, ...]:
        """The connector ids a bridge may serve; empty when the body did not say."""
        return _problem_strs(self.problem, "known")


class ThalovantOwnerOnlyBesideLinkError(ThalovantAPIError):
    """Raised when a device is added beside a home link by anybody but the account owner (403).

    Codes ``device_owner_only_beside_home_link``,
    ``setup_session_owner_only_beside_home_link`` and
    ``device_identity_owner_only_beside_home_link``. :attr:`link_client_id`
    names the link. The owner does it from their own session (never an
    impersonated one); on a hub where the account has no link nothing changes.
    """

    @property
    def link_client_id(self) -> str | None:
        """The link that closes this, or ``None`` when the body did not say."""
        return _problem_str(self.problem, "link_client_id")


class ThalovantHubScaleOutBreaksLinksError(ThalovantAPIError):
    """Raised when a hub's change would scale it out while it carries links (409 ``hub_scale_out_breaks_links``).

    A link needs one listener. :attr:`links` counts the links on the hub.
    There is no override: remove the links first.
    """

    @property
    def links(self) -> int | None:
        """How many links the hub carries, or ``None`` when the body did not say."""
        value = _problem_member(self.problem, "links", _is_count)
        return value if isinstance(value, int) else None


class ThalovantIdempotencyKeySpentError(ThalovantAPIError):
    """Raised when an ``Idempotency-Key`` made something that has since been removed (409 ``idempotency_key_spent``).

    Send the request again with a new key.
    """


class ThalovantPrivateHubOnlyError(ThalovantAPIError):
    """Raised when a setting is for a private hub only (422 ``skill_config_private_hub_only``).

    The music skill's direct route (``transport: direct``, ``url``, ``token``,
    ``allowed_devices``, ``device_players``) is refused while the skill set
    serves a public hub, and a hub cannot be made public over it. Use the home
    bridge instead, or keep the hub private. :attr:`settings` names them.
    """

    @property
    def settings(self) -> tuple[str, ...]:
        """The refused settings by name; empty when the body did not say."""
        return _problem_strs(self.problem, "settings")


class ThalovantCredentialsNotReplayedError(ThalovantError):
    """Raised when a connection create was answered from an earlier request with the same key.

    The connection exists, but its credentials were in the first answer only:
    the platform does not store them, so a replay carries none. The SDK does not
    hand back an identity it generated itself, because the platform issues the
    connection's real credentials. :attr:`client` is the replayed resource (no
    credentials); download the identity from the owner's own session, or delete
    the connection and create it again with a new key.
    """

    def __init__(self, *args: object, client: Mapping[str, Any] | None = None) -> None:
        super().__init__(*args)
        self.client: dict[str, Any] = dict(client) if isinstance(client, Mapping) else {}

    @property
    def client_id(self) -> str | None:
        """The connection's id."""
        value = self.client.get("id")
        return value if isinstance(value, str) else None


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
    """Raised when a new connection will not be admitted.

    Either the operation that admits it failed or timed out on the platform --
    ``error_code`` is the operation's own code -- or the API refused the wait
    itself, and then ``status_code``, ``code``, ``detail`` and ``problem``
    keep what it answered, as a :class:`ThalovantAPIError` does. An
    authentication refusal is not this: it is raised as the API's own error.
    """

    def __init__(
        self,
        *args: object,
        error_code: str | None = None,
        status_code: int | None = None,
        code: str | None = None,
        detail: str | None = None,
        problem: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(*args)
        self.error_code = error_code
        self.status_code = status_code
        self.problem: dict[str, Any] | None = dict(problem) if isinstance(problem, Mapping) else None
        read_code, read_detail = _problem_fields(self.problem)
        self.code = code if code is not None else read_code
        self.detail = detail if detail is not None else read_detail
