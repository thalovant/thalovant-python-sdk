"""Thalovant control-plane helpers.

:class:`AsyncThalovantControlPlane` is the implementation, on aiohttp.
:class:`ThalovantControlPlane` runs it on a private event-loop thread, or --
when given a requests-style ``session``, as it always accepted -- sends through
that session instead.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
import re
import secrets
import time
from typing import Any, Awaitable, Callable, Coroutine, Iterable, Iterator, Literal, Mapping, TypeVar, cast
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from uuid import uuid4
import weakref
import webbrowser

from . import _aiohttp
from ._loop import LoopThread as _LoopThread
from .errors import (
    ThalovantAdmissionFailedError,
    ThalovantAdmissionTimeoutError,
    ThalovantAlreadyLinkedError,
    ThalovantAPIError,
    ThalovantAPIUnreachableError,
    ThalovantAuthError,
    ThalovantDeviceLoginDenied,
    ThalovantDeviceLoginExpired,
    ThalovantDeviceLoginPending,
    ThalovantPlanError,
    ThalovantTimeoutError,
    ThalovantUnsupportedConnectionTypeError,
    ThalovantUnsupportedProtocolError,
)
from .identity import ThalovantIdentity
from .protocols import (
    DEFAULT_PROTOCOL_PREFERENCE,
    HubDataPlaneEndpoints,
    HubProtocol,
    HubProtocolSettings,
    SelectedHubEndpoint,
    endpoint_from_domain,
    select_data_plane_endpoint,
)
from ._version import USER_AGENT

_T = TypeVar("_T")

DEFAULT_CONTROL_API_URL = "https://api.thalovant.com"
DEFAULT_CONTROL_USER_AGENT = USER_AGENT

DEFAULT_DEVICE_POLL_INTERVAL = 5.0
#: Seconds between two reads of an operation the caller is waiting on.
DEFAULT_OPERATION_POLL_INTERVAL = 2.0
#: How long :meth:`AsyncThalovantControlPlane.wait_for_admission` waits by
#: default: a hub admits a new connection in about ninety seconds.
DEFAULT_ADMISSION_TIMEOUT = 180.0

#: The scopes a Home Assistant link asks for, and all a Free plan can approve.
HOME_ASSISTANT_SCOPES = ("hubs:read", "clients:read", "clients:write")
#: ``spec.connection_type`` of a Home Assistant link.
CONNECTION_TYPE_HOME_ASSISTANT = "home_assistant"

OperationStatus = Literal[
    "requested",
    "committed",
    "applied",
    "ready",
    "failed",
    "timed_out",
]


@dataclass(frozen=True)
class OperationResource:
    """Durable progress for an accepted control-plane command."""

    id: str
    kind: str
    aggregate_type: str
    aggregate_id: str | None
    status: OperationStatus
    details: dict[str, Any]
    git_commit_sha: str | None
    error_code: str | None
    error_message: str | None
    created_at: str
    updated_at: str
    committed_at: str | None
    applied_at: str | None
    ready_at: str | None
    terminal_at: str | None
    links: dict[str, str | None]

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> OperationResource:
        """Parse the public operation representation returned by the API."""

        return cls(
            id=_required_str(payload, "id"),
            kind=_required_str(payload, "kind"),
            aggregate_type=_required_str(payload, "aggregate_type"),
            aggregate_id=_optional_str(payload.get("aggregate_id")),
            status=cast(OperationStatus, _required_str(payload, "status")),
            details=dict(payload.get("details") or {}),
            git_commit_sha=_optional_str(payload.get("git_commit_sha")),
            error_code=_optional_str(payload.get("error_code")),
            error_message=_optional_str(payload.get("error_message")),
            created_at=_required_str(payload, "created_at"),
            updated_at=_required_str(payload, "updated_at"),
            committed_at=_optional_str(payload.get("committed_at")),
            applied_at=_optional_str(payload.get("applied_at")),
            ready_at=_optional_str(payload.get("ready_at")),
            terminal_at=_optional_str(payload.get("terminal_at")),
            links={
                str(key): _optional_str(value)
                for key, value in dict(payload.get("links") or {}).items()
            },
        )


# ---------------------------------------------------------------------------
# Hub-scoped skills.
#
# The API contract for these routes is still being finalized. Every path,
# field name, and state value the SDK depends on lives in this block so that a
# change on the server side is a change in one place here.
# ---------------------------------------------------------------------------

#: Seconds between two operation reads while waiting for a hub skill change.
DEFAULT_HUB_SKILL_POLL_INTERVAL = 2.0
#: Default ceiling, in seconds, for ``wait=True`` on the hub skill commands.
DEFAULT_HUB_SKILL_WAIT_TIMEOUT = 120.0

#: Operation statuses after which the API will not move an operation again.
_TERMINAL_OPERATION_STATUSES = frozenset({"ready", "failed", "timed_out"})


def _hub_skills_path(hub_id: str) -> str:
    return f"/v1/hubs/{quote(hub_id, safe='')}/skills"


def _hub_skill_path(hub_id: str, skill: str) -> str:
    return f"{_hub_skills_path(hub_id)}/{quote(skill, safe='')}"


#: State of one skill as the hub reports it. A change in progress shows as
#: ``pending``; ``drifted`` means the running version differs from the
#: requested one, ``quarantined`` that the runtime disabled the skill after
#: repeated failures, and ``unmanaged`` that it runs on the hub but is not
#: managed through this route.
HubSkillState = Literal[
    "pending",
    "installed",
    "failed",
    "removing",
    "drifted",
    "quarantined",
    "unmanaged",
]

HubSkillOperationState = Literal[
    "installing",
    "updating",
    "removing",
    "installed",
    "removed",
    "failed",
]


@dataclass(frozen=True)
class HubSkill:
    """One row of ``GET /v1/hubs/{hub_id}/skills``. Absent strings are ``None``."""

    skill: str
    state: HubSkillState
    title: str | None = None
    marketplace_skill_id: str | None = None
    package_name: str | None = None
    source_type: str | None = None
    install_source: str | None = None
    #: The requested version (``"latest"`` or an exact ``x.y.z``).
    version: str | None = None
    version_pin: str | None = None
    installed_version: str | None = None
    observed_version: str | None = None
    previous_version: str | None = None
    latest_version: str | None = None
    available_version: str | None = None
    update_available: bool = False
    changelog: str | None = None
    active: bool = True
    operator_phase: str | None = None
    operator_message: str | None = None
    operator_last_error: str | None = None
    last_transition_at: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> HubSkill:
        """Parse one row of the hub skill listing."""

        return cls(
            skill=_required_str(payload, "skill"),
            state=cast(HubSkillState, _required_str(payload, "state")),
            title=_optional_str(payload.get("title")),
            marketplace_skill_id=_optional_str(payload.get("marketplace_skill_id")),
            package_name=_optional_str(payload.get("package_name")),
            source_type=_optional_str(payload.get("source_type")),
            install_source=_optional_str(payload.get("install_source")),
            version=_optional_str(payload.get("version")),
            version_pin=_optional_str(payload.get("version_pin")),
            installed_version=_optional_str(payload.get("installed_version")),
            observed_version=_optional_str(payload.get("observed_version")),
            previous_version=_optional_str(payload.get("previous_version")),
            latest_version=_optional_str(payload.get("latest_version")),
            available_version=_optional_str(payload.get("available_version")),
            update_available=payload.get("update_available") is True,
            changelog=_optional_str(payload.get("changelog")),
            active=payload.get("active") is not False,
            operator_phase=_optional_str(payload.get("operator_phase")),
            operator_message=_optional_str(payload.get("operator_message")),
            operator_last_error=_optional_str(payload.get("operator_last_error")),
            last_transition_at=_optional_str(payload.get("last_transition_at")),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HubSkillList:
    """The ``GET /v1/hubs/{hub_id}/skills`` envelope: one hub's skills and where the reading came from.

    Iterating or taking ``len()`` of the listing goes over ``data``.
    """

    hub_id: str
    data: list[HubSkill]
    runtime_group_id: str | None = None
    observed_at: str | None = None
    source: str | None = None
    operator_phase: str | None = None
    operator_message: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> HubSkillList:
        """Parse the listing envelope; the rows live under ``data``."""

        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ThalovantAPIError("Thalovant API returned an unexpected hub skill listing shape.")
        if any(not isinstance(row, Mapping) for row in rows):
            raise ThalovantAPIError("Thalovant API returned an unexpected hub skill row shape.")
        return cls(
            hub_id=_required_str(payload, "hub_id"),
            data=[HubSkill.from_dict(row) for row in rows],
            runtime_group_id=_optional_str(payload.get("runtime_group_id")),
            observed_at=_optional_str(payload.get("observed_at")),
            source=_optional_str(payload.get("source")),
            operator_phase=_optional_str(payload.get("operator_phase")),
            operator_message=_optional_str(payload.get("operator_message")),
        )

    def __iter__(self) -> Iterator[HubSkill]:
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)

    def as_dict(self) -> dict[str, Any]:
        return {
            "hub_id": self.hub_id,
            "runtime_group_id": self.runtime_group_id,
            "observed_at": self.observed_at,
            "source": self.source,
            "operator_phase": self.operator_phase,
            "operator_message": self.operator_message,
            "data": [skill.as_dict() for skill in self.data],
        }


@dataclass(frozen=True)
class HubSkillOperation:
    """An accepted hub skill command, plus where it converged when waited for.

    The install, update, and remove routes answer HTTP 202 with an
    ``operation_id``; ``state`` is what the API said at acceptance
    (``installing``, ``updating``, ``removing``). With ``wait=True`` the SDK
    polls that operation and returns ``installed`` or ``removed`` instead,
    with the last :class:`OperationResource` it read in ``operation``.
    """

    operation_id: str
    skill: str
    #: The requested version; ``None`` for a removal.
    version: str | None
    state: HubSkillOperationState
    hub_id: str | None = None
    runtime_group_id: str | None = None
    #: The version the hub carried before this change, when it carried one.
    previous_version: str | None = None
    operation: OperationResource | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> HubSkillOperation:
        """Parse the HTTP 202 body of a hub skill command."""

        return cls(
            operation_id=_required_str(payload, "operation_id"),
            skill=_required_str(payload, "skill"),
            version=_optional_str(payload.get("version")),
            state=cast(HubSkillOperationState, _required_str(payload, "state")),
            hub_id=_optional_str(payload.get("hub_id")),
            runtime_group_id=_optional_str(payload.get("runtime_group_id")),
            previous_version=_optional_str(payload.get("previous_version")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "hub_id": self.hub_id,
            "runtime_group_id": self.runtime_group_id,
            "skill": self.skill,
            "version": self.version,
            "previous_version": self.previous_version,
            "state": self.state,
            "operation": asdict(self.operation) if self.operation else None,
        }


@dataclass(frozen=True)
class BootstrapIdentityResult:
    """Result returned after provisioning a hub client identity through the API."""

    identity: ThalovantIdentity
    hub: dict[str, Any]
    # The raw /v1/clients response carries credentials (echoed spec secrets,
    # initial_identify, initial_identify_token); keep it out of repr()/str().
    client: dict[str, Any] = field(repr=False)
    endpoint: SelectedHubEndpoint | None
    #: The operation that carries the new client to its hub, when the API
    #: returned one; see ``wait_for_admission``.
    operation: OperationResource | None = None

    @property
    def selected_protocol(self) -> HubProtocol | None:
        return self.endpoint.protocol if self.endpoint else None

    @property
    def client_id(self) -> str | None:
        """The new client's id."""
        value = self.client.get("id") if isinstance(self.client, Mapping) else None
        return value if isinstance(value, str) else None

    @property
    def connection_type(self) -> str | None:
        """The connection type the API recorded for the client."""
        spec = self.client.get("spec") if isinstance(self.client, Mapping) else None
        value = spec.get("connection_type") if isinstance(spec, Mapping) else None
        return value if isinstance(value, str) else None

    def as_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        """Return a serializable result, redacting all secrets by default.

        By default the identity secrets are omitted and the ``client`` resource
        is deep-scrubbed: the API's ``POST /v1/clients`` response echoes the
        request ``spec`` (``apiKey``/``password``/``cryptoKey``) and carries the
        ``initial_identify`` credential block plus ``initial_identify_token``.
        Pass ``include_secrets=True`` to get everything unchanged, for example
        to persist an identity file; never log that output.
        """

        return {
            "identity": self.identity.as_dict(include_secrets=include_secrets),
            "hub": self.hub,
            "client": self.client if include_secrets else _scrub_client_secrets(self.client),
            "selected_protocol": self.selected_protocol,
            "selected_endpoint": self.endpoint.endpoint if self.endpoint else None,
            "operation": asdict(self.operation) if self.operation else None,
        }


@dataclass(frozen=True)
class DeviceAuthorization:
    """A started device sign-in: what to show a person, and what to poll with.

    ``device_code`` is the secret half; it never needs showing. Store the
    whole object with :meth:`as_dict` to resume polling in another process.
    """

    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    interval: float
    expires_in: int

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> DeviceAuthorization:
        """Parse ``POST /v1/auth/device/authorize``; refuse URLs a browser should not open."""

        device_code = payload.get("device_code")
        user_code = payload.get("user_code")
        verification_uri = payload.get("verification_uri")
        for value in (device_code, user_code, verification_uri):
            if not isinstance(value, str) or not value:
                raise ThalovantAPIError("Thalovant API device authorization response was incomplete.")
        complete = payload.get("verification_uri_complete")
        for value in (verification_uri, complete):
            if value is None:
                continue
            if not _safe_browser_url(value):
                raise ThalovantAPIError(
                    "Device verification URLs must use HTTP or HTTPS without embedded credentials."
                )
        raw_interval = payload.get("interval")
        interval = (
            float(raw_interval)
            if isinstance(raw_interval, (int, float)) and not isinstance(raw_interval, bool) and raw_interval >= 0
            else DEFAULT_DEVICE_POLL_INTERVAL
        )
        raw_expires = payload.get("expires_in")
        expires_in = int(raw_expires) if isinstance(raw_expires, int) and not isinstance(raw_expires, bool) else 900
        return cls(
            device_code=cast(str, device_code),
            user_code=cast(str, user_code),
            verification_uri=cast(str, verification_uri),
            verification_uri_complete=complete if isinstance(complete, str) and complete else None,
            interval=interval,
            expires_in=expires_in,
        )

    def as_dict(self) -> dict[str, Any]:
        """A JSON-safe form, ``device_code`` included."""
        return asdict(self)


@dataclass(frozen=True)
class ApiToken:
    """An API token the API minted: the credential and what it may do.

    There is no refresh token; a device-login token lives 365 days. Keep
    ``token_id`` to revoke it later with ``revoke_api_token``.
    """

    access_token: str = field(repr=False)
    token_type: str
    scopes: tuple[str, ...]
    expires_at: datetime | None
    token_id: str | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ApiToken:
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ThalovantAPIError("Thalovant API token response did not include access_token.")
        raw_scopes = payload.get("scopes")
        scopes = tuple(scope for scope in raw_scopes if isinstance(scope, str)) if isinstance(raw_scopes, list) else ()
        expires = payload.get("expires_at")
        expires_at = None
        if isinstance(expires, str) and expires:
            try:
                expires_at = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            except ValueError:
                expires_at = None
        token_id = payload.get("token_id")
        token_type = payload.get("token_type")
        return cls(
            access_token=access_token,
            token_type=token_type if isinstance(token_type, str) and token_type else "bearer",
            scopes=scopes,
            expires_at=expires_at,
            token_id=str(token_id) if isinstance(token_id, str) and token_id else None,
        )

    def as_dict(self) -> dict[str, Any]:
        """A JSON-safe form, the credential included: store it as a secret."""
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "scopes": list(self.scopes),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "token_id": self.token_id,
        }


def _deep_merge(base: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    """``incoming`` layered onto ``base``, mappings merged key by key.

    Anything that is not a mapping replaces rather than combines -- lists
    included. A list is a value, not a namespace, and a caller passing
    ``secondary_langs: ["fr-fr"]`` means that list and not "add these to
    whatever is there".
    """

    merged = dict(base)
    for key, value in incoming.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


class AsyncThalovantControlPlane:
    """The Thalovant API, on asyncio. Every method of :class:`ThalovantControlPlane`, awaited.

    ``session`` is the caller's ``aiohttp.ClientSession`` -- Home Assistant
    passes its shared one -- used as given and never closed. Without one the
    SDK opens its own on first use; ``aclose()`` (or ``async with``) closes it.
    Errors follow the ``api-errors`` contract: :class:`ThalovantAPIError`
    carries the status, the problem's ``code`` and ``detail``, and the whole
    body as ``problem``; a revoked or expired token raises
    :class:`ThalovantAuthError`, and a plan limit :class:`ThalovantPlanError`.

    See :class:`ThalovantControlPlane` for the provisioning gates.
    """

    def __init__(
        self,
        api_url: str = DEFAULT_CONTROL_API_URL,
        *,
        access_token: str | None = None,
        timeout: float = 10.0,
        user_agent: str = DEFAULT_CONTROL_USER_AGENT,
        session: Any = None,
    ) -> None:
        self.api_url = _normalize_control_api_url(api_url)
        self.access_token = access_token
        #: The id of the API token in ``access_token``, when the SDK minted it
        #: (a device login); what :meth:`revoke_api_token` revokes by default.
        self.token_id: str | None = None
        # Whether the token this client signed in with has been revoked and
        # forgotten, so that revoking it again is the no-op it should be.
        self._revoked_own = False
        self.timeout = timeout
        self.user_agent = user_agent
        self.session = session
        self._sender = _sender_for(session)
        # Each device code's poll interval, lengthened by every slow_down.
        self._device_intervals: dict[str, float] = {}

    async def aclose(self) -> None:
        """Close the HTTP session the SDK opened; a caller's session is left alone."""
        await self._sender.close()

    async def __aenter__(self) -> AsyncThalovantControlPlane:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def login(
        self,
        email: str,
        password: str,
        *,
        scope: str | None = None,
        otp_code: str | None = None,
        recovery_code: str | None = None,
    ) -> dict[str, Any]:
        """Authenticate with email/password and store the returned access token.

        MFA-enabled accounts must also provide a TOTP ``otp_code`` or a one-time
        ``recovery_code``; the API rejects the login with ``mfa_required``
        otherwise.
        """

        payload: dict[str, Any] = {"email": email, "password": password}
        if scope:
            payload["scope"] = scope
        if otp_code:
            payload["otp_code"] = otp_code
        if recovery_code:
            payload["recovery_code"] = recovery_code
        token = await self._request("POST", "/v1/auth/token", json=payload, auth=False)
        # A password sign-in answers with a session token, which has no
        # token_id: the id of an API token signed in with earlier must not
        # outlive it, or a later revoke_api_token() revokes that one.
        return self._accept_token(token)

    def _require_secure_token_exchange(self) -> None:
        """Refuse to put an authorization code and its verifier on the wire in
        cleartext.

        ``api_url`` accepts an ``http`` scheme -- a self-hosted or local
        control plane may legitimately be served that way -- and ``_request``
        sends to whatever it is given without looking. Every other
        call that would leak over http leaks a bearer token the caller already
        holds; this one leaks the two secrets that are about to become one, and
        a code is exchangeable by whoever sees it first.

        Loopback is allowed: a request that never leaves the machine has no
        cleartext to observe, and that is how the control plane is run while
        somebody is working on it.
        """

        parts = urlsplit(self.api_url)
        if parts.scheme.lower() == "https":
            return
        if (parts.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}:
            return
        raise ThalovantAPIError(
            "Refusing to send an authorization code and PKCE verifier in cleartext to "
            f"{parts.hostname or self.api_url}. Use https, or a loopback address while developing."
        )

    async def complete_native_sign_in(
        self,
        code: str,
        verifier: str,
        client_id: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        """Exchange an authorization code for a scoped access token and store it.

        The other half of :func:`thalovant.native_auth.begin_native_sign_in`.
        The verifier is sent here and nowhere else; it never entered the
        browser, which is what makes an intercepted code useless to whoever
        intercepted it.

        A code presented twice revokes the token the first exchange minted
        (RFC 9700), so retrying a failed exchange with the same code destroys
        the token it is trying to obtain. Start again from
        ``begin_native_sign_in`` instead.
        """

        self._require_secure_token_exchange()
        payload = {
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
        }
        token = await self._request("POST", "/v1/auth/native/token", json=payload, auth=False)
        return self._accept_token(token)

    async def login_with_browser(
        self,
        *,
        scopes: Iterable[str] | None = None,
        client_name: str | None = None,
        open_browser: bool = True,
        prompt: Callable[[dict[str, Any]], None] | None = None,
        timeout: float = 900.0,
    ) -> dict[str, Any]:
        """Sign in through the browser device flow and store the API token.

        This is the sign-in path for accounts without a password (for example
        Google sign-in). It requests a device authorization, tells the user to
        visit ``verification_uri`` and enter the short ``user_code`` (pass a
        ``prompt`` callable receiving the authorization payload to present it
        yourself), optionally opens the browser at
        ``verification_uri_complete``, and polls until the request is approved,
        denied, expired, or ``timeout`` seconds elapse.

        On approval the returned ``access_token`` is a durable scoped API token
        and is stored on ``self.access_token`` exactly like ``login()``.
        :meth:`begin_device_login` and :meth:`poll_device_login` are the same
        flow one step at a time, for a caller that runs its own loop.
        """

        grant = await self.begin_device_login(scopes=scopes, client_name=client_name)
        _present_device_login(grant, prompt=prompt, open_browser=open_browser)
        token = await self._poll_device_token(grant.device_code, interval=grant.interval, timeout=timeout)
        return self._accept_token(token)

    async def _poll_device_token(
        self,
        device_code: str,
        *,
        interval: float,
        timeout: float,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        """Poll the device token endpoint until approval or a terminal state.

        ``sleep`` and ``clock`` are injectable so tests can drive the loop
        without real waiting.
        """

        deadline = clock() + timeout
        self._device_intervals[device_code] = interval
        while True:
            try:
                return await self._device_token_once(device_code)
            except ThalovantDeviceLoginPending as pending:
                wait = pending.interval
            remaining = deadline - clock()
            if remaining <= 0:
                raise ThalovantTimeoutError(
                    "Timed out waiting for the device sign-in to be approved."
                )
            await sleep(min(wait, remaining))

    async def _device_token_once(self, device_code: str) -> dict[str, Any]:
        """One ``POST /v1/auth/device/token``: the token, or why there is none yet."""

        response = await self._send(
            "POST",
            "/v1/auth/device/token",
            json={"device_code": device_code},
            auth=False,
        )
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        if 200 <= response.status_code < 300:
            if not isinstance(body, dict):
                raise ThalovantAPIError(
                    "Thalovant API returned an unexpected response shape.",
                    status_code=response.status_code,
                )
            self._device_intervals.pop(device_code, None)
            return body
        error = (
            body.get("error")
            if response.status_code == 400 and isinstance(body, dict)
            else None
        )
        problem = body if isinstance(body, dict) else None
        interval = self._device_intervals.get(device_code, DEFAULT_DEVICE_POLL_INTERVAL)
        if error == "slow_down":
            # RFC 8628 §3.5: every slow_down adds five seconds, for good.
            interval += 5.0
            self._device_intervals[device_code] = interval
        if error in {"slow_down", "authorization_pending"}:
            raise ThalovantDeviceLoginPending(
                "The device sign-in has not been approved yet.",
                interval=interval,
                status_code=response.status_code,
                problem=problem,
            )
        if error == "access_denied":
            self._device_intervals.pop(device_code, None)
            raise ThalovantDeviceLoginDenied(
                "The device sign-in request was denied in the browser.",
                status_code=response.status_code,
                problem=problem,
            )
        if error == "expired_token":
            self._device_intervals.pop(device_code, None)
            raise ThalovantDeviceLoginExpired(
                "The device sign-in code expired before it was approved. "
                "Call login_with_browser() again to request a new code.",
                status_code=response.status_code,
                problem=problem,
            )
        raise _api_error(response)

    def _accept_token(self, token: dict[str, Any]) -> dict[str, Any]:
        """Keep a sign-in's token, and the id it came with or none.

        Every sign-in sets both from its own answer, so ``token_id`` always
        names the token in ``access_token`` -- or nothing, for a session
        token -- and never one signed in with before.
        """
        access_token = token.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ThalovantAPIError("Thalovant API token response did not include access_token.")
        self.access_token = access_token
        token_id = token.get("token_id")
        self.token_id = str(token_id) if isinstance(token_id, str) and token_id else None
        self._revoked_own = False
        return token

    async def begin_device_login(
        self,
        *,
        scopes: Iterable[str] | None = None,
        client_name: str | None = None,
    ) -> DeviceAuthorization:
        """Start a device sign-in: a code for a person to approve in a browser.

        ``scopes`` are what the token will carry; the API defaults to
        ``hubs:read`` and ``clients:write``. A Free plan can approve only
        ``hubs:read``, ``clients:read`` and ``clients:write``. Show the person
        ``verification_uri`` and ``user_code`` (or ``verification_uri_complete``,
        which carries the code), then call :meth:`poll_device_login` every
        ``interval`` seconds.
        """

        payload: dict[str, Any] = {}
        if scopes is not None:
            payload["scopes"] = list(scopes)
        if client_name:
            payload["client_name"] = client_name
        grant = await self._request("POST", "/v1/auth/device/authorize", json=payload, auth=False)
        authorization = DeviceAuthorization.from_dict(grant)
        self._device_intervals[authorization.device_code] = authorization.interval
        return authorization

    async def poll_device_login(self, authorization: DeviceAuthorization | str) -> ApiToken:
        """Ask once whether the device sign-in was approved.

        Returns the token and stores it on this client (``access_token`` and
        ``token_id``). Otherwise raises :class:`ThalovantDeviceLoginPending`
        -- poll again after its ``interval``, which a ``slow_down`` has already
        lengthened -- :class:`ThalovantDeviceLoginExpired` or
        :class:`ThalovantDeviceLoginDenied`. All three are
        :class:`ThalovantAPIError`.
        """

        device_code = authorization if isinstance(authorization, str) else authorization.device_code
        if not isinstance(authorization, str):
            self._device_intervals.setdefault(device_code, authorization.interval)
        token = self._accept_token(await self._device_token_once(device_code))
        return ApiToken.from_dict(token)

    async def revoke_api_token(self, token_id: str | None = None) -> None:
        """Revoke an API token; by default the one this client signed in with.

        A token may always revoke itself (``DELETE /v1/auth/api-tokens/{id}``),
        whatever its scopes. Revoking the token in use forgets it here too, so
        a later call fails locally rather than with a 401.

        Revoking the token in use is idempotent. A token already revoked, or
        expired, cannot authenticate its own revoke, so the API answers 401;
        the token is dead either way, so that counts as revoked and the token
        is forgotten; revoking it again then sends nothing and succeeds, until
        the next sign-in. Revoking another token by id is not: the API's own
        answer -- 404 for one it does not know -- is raised as usual.
        """

        target = token_id or self.token_id
        if not target:
            if self._revoked_own and self.access_token is None:
                return  # already revoked and forgotten: revoking again changes nothing
            raise ThalovantAPIError(
                "No API token id to revoke: pass token_id, or sign in with a device login first."
            )
        own = target == self.token_id
        try:
            await self._request("DELETE", f"/v1/auth/api-tokens/{quote(target, safe='')}")
        except ThalovantAuthError as error:
            if not (own and error.status_code == 401):
                raise
        if own:
            self.access_token = None
            self.token_id = None
            self._revoked_own = True

    async def get_profile(self) -> dict[str, Any]:
        """The signed-in account: ``id``, ``email``, ``display_name``, ``role``.

        The API wraps it in ``data``; this returns what is inside.
        """

        body = await self._request("GET", "/v1/users/profile")
        profile = body.get("data")
        return dict(profile) if isinstance(profile, Mapping) else body

    async def list_hubs(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        """List hubs visible to the authenticated user."""

        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if owner_id:
            params["owner_id"] = owner_id
        return await self._request("GET", "/v1/hubs", params=params)

    async def list_public_hubs(
        self,
        *,
        limit: int = 24,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List public, active hubs available for discovery without API auth."""

        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/v1/public/hubs", params=params, auth=False)

    async def get_operation(self, operation_id: str) -> OperationResource:
        """Read durable progress for an operation accepted by the API."""

        payload = await self._request("GET", f"/v1/operations/{operation_id}")
        return OperationResource.from_dict(payload)

    async def list_memory_items(
        self,
        *,
        scope: str | None = None,
        kind: str | None = None,
        owner_id: str | None = None,
        hub_id: str | None = None,
        query: str | None = None,
        include_deleted: bool = False,
        include_expired: bool = False,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """List durable memory items visible to the authenticated user."""

        params: dict[str, Any] = {}
        _set_param(params, "scope", scope)
        _set_param(params, "kind", kind)
        _set_param(params, "owner_id", owner_id)
        _set_param(params, "hub_id", hub_id)
        _set_param(params, "q", query)
        if include_deleted:
            params["include_deleted"] = "true"
        if include_expired:
            params["include_expired"] = "true"
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return await self._request("GET", "/v1/memory", params=params)

    async def get_memory_summary(self, *, owner_id: str | None = None) -> dict[str, Any]:
        """Summarize durable memory by scope and kind."""

        params: dict[str, Any] = {}
        _set_param(params, "owner_id", owner_id)
        return await self._request("GET", "/v1/memory/summary", params=params)

    async def create_memory_item(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Create a durable memory item."""

        return await self._request("POST", "/v1/memory", json=_memory_payload(payload))

    async def get_memory_item(self, memory_id: str) -> dict[str, Any]:
        """Fetch one durable memory item by id."""

        return await self._request("GET", f"/v1/memory/{memory_id}")

    async def update_memory_item(self, memory_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Update a durable memory item."""

        return await self._request("PATCH", f"/v1/memory/{memory_id}", json=_memory_payload(payload))

    async def delete_memory_item(self, memory_id: str) -> None:
        """Soft-delete a durable memory item."""

        await self._request("DELETE", f"/v1/memory/{memory_id}")

    async def get_analytics_overview(
        self,
        *,
        range: str | None = None,
        bucket: str | None = None,
        hub_id: str | None = None,
        client_id: str | None = None,
        country: str | None = None,
        message: str | None = None,
        utterance: str | None = None,
        intent: str | None = None,
        time_start: str | None = None,
        time_end: str | None = None,
        weekday: int | None = None,
        hour: int | None = None,
    ) -> dict[str, Any]:
        """Fetch the workspace analytics overview used by the dashboard."""

        params: dict[str, Any] = {}
        _set_param(params, "range", range)
        _set_param(params, "bucket", bucket)
        _set_param(params, "hub_id", hub_id)
        _set_param(params, "client_id", client_id)
        _set_param(params, "country", country)
        _set_param(params, "message", message)
        _set_param(params, "utterance", utterance)
        _set_param(params, "intent", intent)
        _set_param(params, "time_start", time_start)
        _set_param(params, "time_end", time_end)
        if weekday is not None:
            params["weekday"] = weekday
        if hour is not None:
            params["hour"] = hour
        return await self._request("GET", "/v1/analytics/overview", params=params)

    async def get_hub(self, hub_id: str) -> dict[str, Any]:
        """Fetch one hub resource."""

        return await self._request("GET", f"/v1/hubs/{hub_id}")

    async def get_public_hub(self, hub_ref: str) -> dict[str, Any]:
        """Fetch one public hub by slug or id without API auth."""

        return await self._request("GET", f"/v1/public/hubs/{hub_ref}", auth=False)

    async def create_hub(
        self,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a hub.

        ``payload`` mirrors the API's hub create body: ``name`` and ``spec`` are
        required, and ``slug``, ``namespace``, ``runtime_group_id``, ``domain``,
        ``active``, ``visibility``, ``capacity_profile``, and ``owner_id`` are
        optional. camelCase keys are accepted and sent as snake_case.

        ``spec`` is validated against the API's hub schema, which **requires a
        non-empty ``version`` string**; a spec without one fails with HTTP 422
        ``Schema validation failed`` rather than being defaulted.

        Retain an explicit ``idempotency_key`` before the first call and reuse
        it with the same payload after an uncertain outcome. An omitted key is
        freshly generated on each invocation, so retrying without the original
        key can create a second hub.

        Requires a paid plan and a token with the ``hubs:write`` scope; see
        :class:`ThalovantControlPlane` for why the scope gate is the one a
        free-plan API token actually hits.
        """

        headers = {"Idempotency-Key": idempotency_key or str(uuid4())}
        return await self._request("POST", "/v1/hubs", json=_hub_payload(payload), headers=headers)

    async def update_hub(
        self,
        hub_id: str,
        payload: Mapping[str, Any],
        *,
        etag: str,
    ) -> dict[str, Any]:
        """Partially update a hub.

        The API enforces optimistic locking on this route: pass the ``etag``
        from the hub resource you read, which is sent as ``If-Match``. A stale
        or missing value fails the request with HTTP 412 and no change is made;
        re-read the hub with :meth:`get_hub` and retry with the new ``etag``.

        A prior read is therefore mandatory, and it must be a *body* read: the
        API carries the validator only in the ``etag`` field of the hub
        resource and emits **no ``ETag`` response header**, so
        ``response.headers["ETag"]`` is not an alternative source. Take it from
        ``get_hub(hub_id)["etag"]`` (``list_hubs`` entries carry it too).

        ``name``, ``namespace``, and ``domain`` are immutable after creation.
        The API drops them from the patch when the value you send matches the
        stored one (or is ``None``) and rejects a *different* value with HTTP
        400 ``<Field> cannot be changed after hub creation``. This SDK sends
        them through rather than rejecting them locally, because it cannot know
        the stored values without a second read and refusing them outright
        would reject patches the API accepts. Send only the fields you mean to
        change and the distinction never comes up.

        ``slug``, ``active``, ``visibility``, ``capacity_profile``,
        ``runtime_group_id``, and ``spec`` are patchable; ``is_locked`` is
        admin-only.

        Requires a paid plan and a token with the ``hubs:write`` scope; see
        :class:`ThalovantControlPlane` for the gate ordering.
        """

        return await self._request(
            "PATCH",
            f"/v1/hubs/{hub_id}",
            json=_hub_payload(payload),
            headers={"If-Match": etag},
        )

    async def delete_hub(self, hub_id: str, *, etag: str) -> None:
        """Delete a hub and its dependent clients and ACLs.

        Like :meth:`update_hub` this route requires the hub's current ``etag``,
        sent as ``If-Match``; a stale value fails with HTTP 412. The value
        comes only from the hub resource's ``etag`` body field -- the API sends
        no ``ETag`` response header -- so a prior :meth:`get_hub` is mandatory.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """

        await self._request("DELETE", f"/v1/hubs/{hub_id}", headers={"If-Match": etag})

    async def release_hub(
        self,
        hub_id: str,
        *,
        channel: str | None = None,
        mode: str | None = None,
        version: str | None = None,
        images: Mapping[str, str] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Apply a hub release policy and return the updated hub.

        Every option is optional; omitted fields fall back to the workspace
        release policy. Passing ``images`` switches the hub to ``custom`` mode
        unless you also pass ``mode``. Unless you are a platform
        administrator, each image must be one the platform releases for its
        key: a catalog pin of the stable or alpha channel, the hub's current,
        recommended or release-policy image, or the platform's default image.
        ``listener`` also accepts any tag or digest of
        ``ghcr.io/thalovant/hivemind-listener``; ``preview_bridge`` takes only
        those images. The API refuses anything else with HTTP 403
        ``platform_image_required``, and the error's ``problem`` names what
        each refused key may be instead.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """

        return await self._request(
            "POST",
            f"/v1/hubs/{hub_id}/release",
            json=_release_payload(
                channel=channel,
                mode=mode,
                version=version,
                images=images,
                reason=reason,
            ),
        )

    async def set_hub_rating(self, hub_id: str, rating: int) -> dict[str, Any]:
        """Rate a public hub from 1 to 5 and return the updated hub.

        Only public hubs can be rated, and owners cannot rate their own hubs.
        Requires a token with the ``hubs:write`` scope; no paid plan is needed.
        """

        return await self._request("PUT", f"/v1/hubs/{hub_id}/rating", json={"rating": rating})

    async def clear_hub_rating(self, hub_id: str) -> dict[str, Any]:
        """Remove the caller's rating from a public hub and return the hub.

        Requires a token with the ``hubs:write`` scope; no paid plan is needed.
        """

        return await self._request("DELETE", f"/v1/hubs/{hub_id}/rating")

    async def get_hub_runtime_capabilities(self, hub_id: str) -> dict[str, Any]:
        """Read the skill and intent inventory a hub runtime exposes.

        The response is not always live, so **branch on the envelope's
        ``source``** rather than assuming the counts are current:

        ``ovos-runtime``
            A connected client answered. This is the only live, canonical
            reading, and the only one the API caches.
        ``ovos-runtime-unavailable`` / ``ovos-runtime-timeout``
            No client could answer, so the API fell back to the hub's runtime
            group snapshot (its desired skills merged with the last observed
            inventory) and still returned HTTP 200. Treat the skills and
            intents as **stale**: they describe what the group is configured
            to run, not what is running now.

        HTTP 409 is answered only when that fallback has nothing to serve
        either -- the hub is attached to no runtime group, or the group has no
        desired and no observed skills at all. A hub with a configured group is
        therefore far likelier to return a stale 200 than a 409.

        The route is also rate limited per caller and hub: HTTP 429 carries a
        ``Retry-After`` header with the number of seconds to wait.

        Requires a token with the ``hubs:inspect`` scope; no paid plan is
        needed.
        """

        return await self._request("GET", f"/v1/hubs/{hub_id}/runtime-capabilities")

    async def list_runtime_groups(self, *, owner_id: str | None = None) -> dict[str, Any]:
        """List runtime groups visible to the authenticated user.

        ``owner_id`` is **enforced, not silently scoped**: a non-admin caller
        passing another tenant's id gets HTTP 403 ``Ownership required``
        (tenant members of that owner are allowed). This is the opposite of
        :meth:`list_marketplace_skills`, where a non-admin's ``owner_id`` is
        quietly overridden with their own. Admin tokens may pass any
        ``owner_id``; omitting it as an admin lists every tenant's groups.

        Requires a token with the ``hubs:read`` scope.
        """

        params: dict[str, Any] = {}
        _set_param(params, "owner_id", owner_id)
        return await self._request("GET", "/v1/runtime-groups", params=params)

    async def get_runtime_group(self, runtime_group_id: str) -> dict[str, Any]:
        """Fetch one runtime group.

        Requires a token with the ``hubs:read`` scope.
        """

        return await self._request("GET", f"/v1/runtime-groups/{runtime_group_id}")

    async def create_runtime_group(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Create a runtime group.

        ``payload`` takes the API's create body: ``name`` is required, and
        ``description``, ``environment``, ``owner_id``, and
        ``clone_from_default`` are optional. camelCase keys are accepted and
        sent as snake_case.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """

        return await self._request("POST", "/v1/runtime-groups", json=_runtime_group_payload(payload))

    async def update_runtime_group(
        self,
        runtime_group_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Update a runtime group's ``name``, ``description``, or ``spec``.

        ``spec`` patches ``replicas`` and container ``resources``. This route
        does not use ``If-Match``.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """

        return await self._request(
            "PATCH",
            f"/v1/runtime-groups/{runtime_group_id}",
            json=_runtime_group_payload(payload),
        )

    async def get_runtime_group_config(self, runtime_group_id: str) -> dict[str, Any]:
        """Read a runtime group's runtime configuration and personas.

        Requires a token with the ``hubs:read`` scope.
        """

        return await self._request("GET", f"/v1/runtime-groups/{runtime_group_id}/config")

    async def update_runtime_group_config(
        self,
        runtime_group_id: str,
        config: Mapping[str, Any],
        *,
        personas: Mapping[str, Any] | None = None,
        merge: bool = True,
    ) -> dict[str, Any]:
        """Update a runtime group's configuration.

        **The top level of ``config`` is the OVOS mycroft configuration**, plus
        an ``env`` key for container variables. It is not a wrapper around one.
        The renderer pops ``env`` and passes everything else through as the
        runtime's mycroft config, so::

            # right -- these are read by ovos-core at load
            {"lang": "en-us", "secondary_langs": ["fr-fr"], "env": [...]}

            # wrong -- renders as spec.config.mycroft.mycroft and is ignored
            {"mycroft": {"lang": "en-us", "secondary_langs": ["fr-fr"]}}

        The wrong form fails silently: unknown keys are copied into mycroft.conf
        verbatim, so the call succeeds, reads back exactly what was sent, and
        the runtime never sees the setting. Worth stating because the CR and the
        gitops manifests *do* nest it one deeper -- ``spec.config.mycroft.*`` --
        and copying that shape into this call is the natural mistake.

        **The API replaces the stored configuration; it does not merge.** This
        docstring claimed the opposite, and the cost of believing it was real:
        sending ``{"mycroft": {...}}`` to add a language setting dropped the
        group's entire ``env`` block, taking the hub memory's Redis host, key
        prefix and both ``secretKeyRef`` credential bindings with it. Nothing
        in the response says so -- the call succeeds and reads back exactly
        what was sent.

        So by default this reads the stored configuration first and deep-merges
        ``config`` into it, which is the behaviour the old docstring described
        and every caller reasonably assumed. Mappings are merged key by key;
        anything else, lists included, is replaced by the incoming value,
        because a list is a value rather than a namespace and no caller means
        "append" by passing one.

        Pass ``merge=False`` for the raw replacing call, when the intent really
        is to define the whole configuration.

        Merges use the GET revision in a conditional PUT. If another writer
        changes the configuration, reread and reapply the original delta, up to
        three write attempts. Only a 412 conflict is retried; network and other
        failures propagate. An older API without revisions or conditional PUT
        support is rejected without falling back to an unsafe write.

        ``merge=False`` retains unconditional PATCH replacement semantics.
        Concurrent writers of the same key intentionally replace that value;
        use guarded writes consistently to preserve unrelated changes.

        ``personas`` is replaced only when provided, merge or not.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        Guarded merging also requires ``hubs:read``.
        """

        path = f"/v1/runtime-groups/{runtime_group_id}/config"
        if not merge:
            body: dict[str, Any] = {"config": deepcopy(dict(config))}
            if personas is not None:
                body["personas"] = deepcopy(dict(personas))
            return await self._request("PATCH", path, json=body)

        # Freeze the complete caller payload before I/O so conflict retries
        # cannot mix an earlier config delta with subsequently mutated personas.
        delta = deepcopy(dict(config))
        stable_personas = deepcopy(dict(personas)) if personas is not None else None
        for attempt in range(3):
            snapshot = await self.get_runtime_group_config(runtime_group_id)
            revision = snapshot.get("revision")
            if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
                raise ThalovantAPIError(
                    "This API does not support safe configuration merges; "
                    "upgrade the API before retrying."
                )
            stored = snapshot.get("config")
            if not isinstance(stored, Mapping):
                raise ThalovantAPIError("Thalovant API returned an invalid runtime configuration.")
            body = {"config": _deep_merge(stored, delta), "expected_revision": revision}
            if stable_personas is not None:
                body["personas"] = deepcopy(stable_personas)
            try:
                return await self._request("PUT", path, json=body)
            except ThalovantAPIError as exc:
                if exc.status_code != 412 or attempt == 2:
                    raise
        raise AssertionError("Configuration retry limit exhausted")  # pragma: no cover

    async def release_runtime_group(
        self,
        runtime_group_id: str,
        *,
        channel: str | None = None,
        mode: str | None = None,
        version: str | None = None,
        images: Mapping[str, str] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Apply a runtime image policy and return the updated runtime group.

        Options behave like :meth:`release_hub`, including the platform-image
        rule: ``core`` also accepts any tag or digest of
        ``ghcr.io/thalovant/ovos-core``, and ``bus`` takes only the images the
        platform releases for it.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """

        return await self._request(
            "POST",
            f"/v1/runtime-groups/{runtime_group_id}/release",
            json=_release_payload(
                channel=channel,
                mode=mode,
                version=version,
                images=images,
                reason=reason,
            ),
        )

    async def delete_runtime_group(self, runtime_group_id: str) -> None:
        """Delete a runtime group.

        The API answers HTTP 409 for the workspace default group and for a
        group that still has hubs attached.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """

        await self._request("DELETE", f"/v1/runtime-groups/{runtime_group_id}")

    async def list_marketplace_skills(
        self,
        *,
        owner_id: str | None = None,
        include_inactive: bool = False,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """List the marketplace skill catalog visible to the authenticated user.

        Returns ``{"data": [...]}`` where each entry carries the catalog fields
        an install needs -- ``skill_id``, ``source_type``, ``source_ref``,
        ``package_name``, ``version`` compatibility, ``config_schema`` and
        ``secret_schema`` -- alongside presentation and access fields such as
        ``category``, ``tags``, ``verified``, ``access_tier`` and
        ``billing_sku``. Global catalog entries and the caller's own tenant
        entries are both included.

        ``owner_id`` and ``include_inactive`` are honoured for admin tokens
        only; the API **silently** scopes a non-admin caller to their own
        tenant and to active entries -- no error is raised, so a non-admin
        passing another tenant's ``owner_id`` gets their own catalog back.
        Contrast :meth:`list_runtime_groups`, which answers HTTP 403 for the
        same mistake. ``force_refresh`` re-syncs the global catalog from its
        source before answering, which is slower.

        Requires a token with the ``hubs:read`` scope. Unlike the provisioning
        routes this catalog is **not** paid-gated, so free-tier callers can
        browse the marketplace before upgrading -- only the install itself
        needs a paid plan.
        """

        params: dict[str, Any] = {}
        _set_param(params, "owner_id", owner_id)
        if include_inactive:
            params["include_inactive"] = "true"
        if force_refresh:
            params["force_refresh"] = "true"
        return await self._request("GET", "/v1/marketplace/skills", params=params)

    async def list_runtime_group_marketplace(
        self,
        runtime_group_id: str,
        *,
        refresh_inventory: bool = False,
    ) -> dict[str, Any]:
        """List the marketplace catalog resolved against one runtime group.

        This is the discovery view to use before installing: every catalog
        entry is returned with the group's own state folded in -- whether the
        skill is desired (``active``, ``version_pin``, ``source_type``),
        whether it was observed running (``observed_source``,
        ``observed_at``, intent counts), operator status fields, and the
        access verdict for the tenant plan (``purchase_required``,
        ``installable``, ``access_message``). The envelope also carries
        ``runtime_group_id``, ``observed_at``, ``source``, ``operator_phase``
        and ``operator_message``.

        ``data`` is driven by the **catalog**, not by the runtime observation:
        it is the catalog unioned with the group's desired and observed skills,
        so it stays populated even when nothing is reporting. A ``source``
        saying the snapshot is empty therefore tells you nothing about the
        length of ``data`` -- do not use one as a proxy for the other. (``data``
        is empty only in the degenerate case of an empty catalog with no
        desired and no observed skills.)

        ``source`` on this route reports where the *observation* came from, and
        the default read draws from a different set of values than
        :meth:`list_runtime_group_inventory`:

        ``runtime-group-cache``
            Answered from a stored inventory snapshot.
        ``runtime-group-cache-empty``
            No snapshot is stored yet. This value is unique to this route.
        ``ovos-runtime-operator``
            The operator's published status differed from the stored snapshot,
            so the API re-synced it while serving the request.

        ``ovos-runtime-operator-pending`` appears here **only** when
        ``refresh_inventory=True``, which forces a live operator read (and can
        then return any of the inventory route's values).

        Requires a token with the ``hubs:inspect`` scope; no paid plan is
        needed to browse. The API answers HTTP 404 for an unknown group and
        HTTP 403 ``Ownership required`` when the caller neither owns it nor is
        an admin.
        """

        params: dict[str, Any] = {}
        if refresh_inventory:
            params["refresh_inventory"] = "true"
        return await self._request(
            "GET",
            f"/v1/runtime-groups/{runtime_group_id}/marketplace",
            params=params,
        )

    async def list_runtime_group_inventory(
        self,
        runtime_group_id: str,
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """List the skills a runtime group is actually observed running.

        Where :meth:`list_runtime_group_marketplace` answers "what could be
        installed here", this answers "what is loaded right now": each entry
        carries ``skill_id``, ``version``, ``source``, ``active``,
        ``adapt_intents``, ``padatious_intents``, ``total_intents`` and
        ``observed_at``. The envelope reports ``source`` -- the observation's
        provenance, one of ``ovos-runtime-operator``, ``runtime-group-cache``
        or ``ovos-runtime-operator-pending`` -- plus ``operator_phase`` and
        ``operator_message``. ``runtime-group-cache-empty`` never appears here;
        it belongs to :meth:`list_runtime_group_marketplace`.

        ``refresh`` forces a live operator read; the API also refreshes on its
        own when it holds no cached snapshot. When nothing is reporting this
        route returns an empty ``data`` list with
        ``source="ovos-runtime-operator-pending"`` rather than failing.

        Requires a token with the ``hubs:inspect`` scope; no paid plan is
        needed. HTTP 404 for an unknown group, HTTP 403 ``Ownership required``
        when the caller neither owns it nor is an admin.
        """

        params: dict[str, Any] = {}
        if refresh:
            params["refresh"] = "true"
        return await self._request(
            "GET",
            f"/v1/runtime-groups/{runtime_group_id}/inventory",
            params=params,
        )

    async def install_runtime_group_skill(
        self,
        runtime_group_id: str,
        skill_id: str,
        *,
        marketplace_skill_id: str | None = None,
        source_type: str = "catalog",
        source_ref: str | None = None,
        version_pin: str | None = None,
        active: bool = True,
    ) -> dict[str, Any]:
        """Install (or re-install) a skill in a runtime group.

        Answers HTTP **200**, not 201: the route upserts, so installing a skill
        that is already present updates the existing entry in place, and the
        returned desired-skill resource is the same shape either way.

        ``source_type`` is a free-form string of 1-32 characters, not an
        enumeration. Only two values are interpreted specially -- ``catalog``
        (the default) resolves the skill against the marketplace and answers
        HTTP 404 ``Marketplace skill not found.`` when it is absent, and
        ``git`` requires ``source_ref`` to be a valid repository URL (HTTP 422
        otherwise). Any other value is accepted and stored as given, with
        ``source_ref`` defaulting to ``skill_id``. The API lower-cases and
        strips whatever you send.

        Two *different* HTTP 402s can come back, and they mean different
        things:

        * ``API access requires a paid plan.`` -- the plan-level API gate that
          guards every provisioning route.
        * ``This skill requires paid marketplace access for the tenant plan.``
          -- a per-skill check on a catalog entry whose ``access_tier`` is
          ``paid`` when the tenant plan lacks marketplace access. The plan can
          be paid and this can still fail, so read
          ``installable``/``purchase_required`` from
          :meth:`list_runtime_group_marketplace` before installing.

        A deactivated catalog entry answers HTTP 409 instead.

        Requires a paid plan and a token with the ``hubs:write`` scope; see
        :class:`ThalovantControlPlane` for why a free-plan API token sees HTTP
        403 here and never either 402.
        """

        body: dict[str, Any] = {
            "skill_id": skill_id,
            "source_type": source_type,
            "active": active,
        }
        if marketplace_skill_id is not None:
            body["marketplace_skill_id"] = marketplace_skill_id
        if source_ref is not None:
            body["source_ref"] = source_ref
        if version_pin is not None:
            body["version_pin"] = version_pin
        return await self._request(
            "POST",
            f"/v1/runtime-groups/{runtime_group_id}/skills",
            json=body,
        )

    async def uninstall_runtime_group_skill(self, runtime_group_id: str, skill_id: str) -> None:
        """Remove a skill from a runtime group.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """

        await self._request("DELETE", f"/v1/runtime-groups/{runtime_group_id}/skills/{skill_id}")

    async def list_hub_skills(self, hub_id: str) -> HubSkillList:
        """List the skills one hub carries, with their install state.

        Where :meth:`list_runtime_group_inventory` describes a whole runtime
        group, this selects the runtime through its hub UUID. The :class:`HubSkillList` envelope
        says where the reading came from (``source``, ``observed_at``, the
        runtime's phase and message); each :class:`HubSkill` row in ``data``
        carries the requested ``version``, the ``installed_version`` and
        ``observed_version``, the catalog's ``latest_version`` with
        ``update_available``, and the ``state`` (``pending``, ``installed``,
        ``failed``, ``removing``, ``drifted``, ``quarantined``, ``unmanaged``)
        with the runtime's last error for a failed change. A hub can carry no
        skills at all; that is an empty ``data``, not an error.

        ``hub_id`` is the hub's id. The authenticated hub routes do not accept
        slugs. Hub-restricted tokens are honoured.

        Requires a token with the ``hubs:inspect`` scope (``hubs:read`` implies
        it).
        """

        return HubSkillList.from_dict(await self._request("GET", _hub_skills_path(hub_id)))

    async def list_hub_skill_history(self, hub_id: str, *, limit: int = 50) -> dict[str, Any]:
        """Read newest-first skill events and operations for the hub's shared runtime.

        Requires ``hubs:inspect`` (implied by ``hubs:read``). The limit is 1–200.
        Entries retain the API's event/operation fields, including nullable actor
        and version information. Every hub sharing this runtime sees its history.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer from 1 to 200")
        return await self._request("GET", f"{_hub_skills_path(hub_id)}/history", params={"limit": limit})

    async def install_hub_skill(
        self,
        hub_id: str,
        skill: str,
        *,
        version: str = "latest",
        wait: bool = False,
        timeout: float = DEFAULT_HUB_SKILL_WAIT_TIMEOUT,
    ) -> HubSkillOperation:
        """Install a skill on the runtime group attached to a hub.

        All hubs sharing that runtime group are affected; a restricted token
        must cover every served hub.

        The API accepts the change with HTTP 202 and applies it live on the
        hub, typically within about fifteen seconds and without restarting it.
        ``version`` is ``"latest"`` or an exact ``x.y.z``.

        By default this returns as soon as the change is accepted, with
        ``state="installing"`` and the ``operation_id`` to poll through
        :meth:`get_operation`. With ``wait=True`` it polls that operation for
        you, every :data:`DEFAULT_HUB_SKILL_POLL_INTERVAL` seconds, and returns
        ``state="installed"`` once the operation is ``ready``; a ``failed`` or
        ``timed_out`` operation raises :class:`ThalovantAPIError` carrying the
        operation's error message, and ``timeout`` seconds without convergence
        raise :class:`ThalovantTimeoutError`.

        Installing a skill the hub already carries at another version performs
        an update. The API answers HTTP 409 ``skill_version_already_installed``
        for the same version, HTTP 404 ``hub_without_runtime_group`` when the
        hub has no runtime group yet, and HTTP 422 for an unresolvable
        ``"latest"`` or an invalid version; the problem ``code`` is appended to
        the :class:`ThalovantAPIError` message.

        Requires a paid plan and a token with the ``hubs:write`` scope; see
        :class:`ThalovantControlPlane` for why a free-plan API token sees HTTP
        403 here rather than 402. Hub-restricted tokens are honoured.
        """

        accepted = HubSkillOperation.from_dict(
            await self._request(
                "POST",
                _hub_skills_path(hub_id),
                json={"skill": skill, "version": version},
            )
        )
        if not wait:
            return accepted
        return await self._wait_for_hub_skill_operation(accepted, converged="installed", timeout=timeout)

    async def update_hub_skill(
        self,
        hub_id: str,
        skill: str,
        *,
        version: str,
        wait: bool = False,
        timeout: float = DEFAULT_HUB_SKILL_WAIT_TIMEOUT,
    ) -> HubSkillOperation:
        """Move a skill on the hub's shared runtime group to another version.

        ``version`` is required: ``"latest"`` or an exact ``x.y.z``. The API
        accepts with HTTP 202 and ``state="updating"``; ``wait`` and
        ``timeout`` behave exactly as in :meth:`install_hub_skill`, converging
        on ``state="installed"``.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """

        accepted = HubSkillOperation.from_dict(
            await self._request("PATCH", _hub_skill_path(hub_id, skill), json={"version": version})
        )
        if not wait:
            return accepted
        return await self._wait_for_hub_skill_operation(accepted, converged="installed", timeout=timeout)

    async def remove_hub_skill(
        self,
        hub_id: str,
        skill: str,
        *,
        wait: bool = False,
        timeout: float = DEFAULT_HUB_SKILL_WAIT_TIMEOUT,
    ) -> HubSkillOperation:
        """Remove a skill from the hub's shared runtime group.

        The API accepts with HTTP 202 and ``state="removing"``; ``wait`` and
        ``timeout`` behave exactly as in :meth:`install_hub_skill`, converging
        on ``state="removed"``. The hub keeps running throughout.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """

        accepted = HubSkillOperation.from_dict(
            await self._request("DELETE", _hub_skill_path(hub_id, skill))
        )
        if not wait:
            return accepted
        return await self._wait_for_hub_skill_operation(accepted, converged="removed", timeout=timeout)

    async def _wait_for_hub_skill_operation(
        self,
        accepted: HubSkillOperation,
        *,
        converged: HubSkillOperationState,
        timeout: float,
        interval: float = DEFAULT_HUB_SKILL_POLL_INTERVAL,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> HubSkillOperation:
        """Poll an accepted hub skill command until its operation is terminal.

        ``ready`` converges to ``converged``; ``failed`` and ``timed_out``
        raise :class:`ThalovantAPIError`; anything else keeps polling until
        ``timeout`` seconds have elapsed, then :class:`ThalovantTimeoutError`.
        ``sleep`` and ``clock`` are injectable so tests can drive the loop
        without real waiting.
        """

        deadline = clock() + timeout
        while True:
            if clock() >= deadline:
                raise ThalovantTimeoutError(
                    f"Timed out after {timeout:g}s waiting for the hub skill change "
                    f"({accepted.skill}, operation {accepted.operation_id}) to converge."
                )
            try:
                operation = await self.get_operation(accepted.operation_id)
            except Exception as exc:
                error = ThalovantAPIError(
                    f"Could not read accepted hub skill operation {accepted.operation_id}. "
                    "Resume with get_operation using this ID."
                )
                if isinstance(exc, ThalovantAPIError):
                    raise error from exc
                raise error from None
            if operation.status == "ready":
                return replace(accepted, state=converged, operation=operation)
            if operation.status in _TERMINAL_OPERATION_STATUSES:
                detail = (
                    operation.error_message
                    or operation.error_code
                    or f"operation {operation.id} ended with status {operation.status}"
                )
                raise ThalovantAPIError(
                    f"Hub skill change for {accepted.skill} failed: {detail} "
                    f"(operation {accepted.operation_id})"
                )
            remaining = deadline - clock()
            if remaining <= 0:
                raise ThalovantTimeoutError(
                    f"Timed out after {timeout:g}s waiting for the hub skill change "
                    f"({accepted.skill}, operation {accepted.operation_id}) to converge."
                )
            await sleep(min(interval, remaining))

    async def create_client(self, payload: Mapping[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        """Create a hub client through the API."""

        headers = {"Idempotency-Key": idempotency_key or str(uuid4())}
        return await self._request("POST", "/v1/clients", json=dict(payload), headers=headers)

    async def create_client_identity(
        self,
        hub: str | Mapping[str, Any],
        *,
        name: str,
        site_id: str | None = None,
        spec: Mapping[str, Any] | None = None,
        owner_id: str | None = None,
        active: bool = True,
        preferred_protocols: Iterable[HubProtocol] = DEFAULT_PROTOCOL_PREFERENCE,
        idempotency_key: str | None = None,
        connection_type: str | None = None,
    ) -> BootstrapIdentityResult:
        """Provision a client and return local identity secrets for direct hub access.

        The API stores client credentials in Vault and returns only references.
        This method therefore generates the secret material locally, sends it to
        the API once, and keeps the usable identity in the returned object.

        ``connection_type`` (``voice_satellite``, ``web_chat``, ``developer``,
        ``embedded``, ``home_assistant``...) is sent as ``spec.connection_type``,
        and the kind decides what the connection may send and receive. The API
        must say the connection is of that kind: a 422 about the field, or a
        created connection whose type came back different, raises
        :class:`ThalovantUnsupportedConnectionTypeError` -- after deleting that
        connection, which would otherwise be an ordinary satellite nobody asked
        for. A plan that does not allow it raises :class:`ThalovantPlanError`;
        a hub that already holds the one link of its kind,
        :class:`ThalovantAlreadyLinkedError`.

        The result's ``operation`` tracks the hub admitting the connection,
        about ninety seconds; :meth:`wait_for_admission` waits for it.
        """

        hub_resource = await self.get_hub(hub) if isinstance(hub, str) else dict(hub)
        hub_id = _required_str(hub_resource, "id")
        site = _clean_site_id(site_id or name)
        api_key = _new_secret()
        password = _new_secret()

        client_spec = dict(spec or {})
        # ``spec`` is caller-supplied and passed straight into the request body,
        # and the error redaction covers only the secrets minted here -- so a
        # legacy crypto key left in could be echoed back inside an API error.
        # v3 issues no crypto key, so drop normalized legacy spellings.
        for key in tuple(client_spec):
            if isinstance(key, str) and _normalize_secret_key(key) == "cryptokey":
                client_spec.pop(key)
        client_spec.setdefault("version", "1")
        if connection_type is not None:
            client_spec["connection_type"] = connection_type
        client_spec.update(
            {
                "apiKey": api_key,
                "password": password,
                "siteId": site,
            }
        )

        payload: dict[str, Any] = {
            "hub_id": hub_id,
            "name": name,
            "spec": client_spec,
            "active": active,
        }
        if owner_id:
            payload["owner_id"] = owner_id

        try:
            client = await self.create_client(payload, idempotency_key=idempotency_key)
        except ThalovantAPIError as error:
            if connection_type is not None and _refuses_connection_type(error):
                raise ThalovantUnsupportedConnectionTypeError(
                    f"The Thalovant API cannot create a {connection_type!r} connection yet.",
                    status_code=error.status_code,
                    problem=error.problem,
                ) from error
            raise
        if connection_type is not None:
            await self._require_connection_type(client, connection_type)
        protocols = HubProtocolSettings.from_mapping(hub_resource)
        endpoints = HubDataPlaneEndpoints.from_hub(hub_resource)
        selected = select_data_plane_endpoint(endpoints, protocols, preferred_protocols)

        initial_identify = client.get("initial_identify") if isinstance(client, Mapping) else None
        if isinstance(initial_identify, Mapping):
            identity_payload = dict(initial_identify)
            identity_payload["data_plane_endpoints"] = endpoints.as_dict()
            identity_payload["protocols"] = protocols.as_dict()
            identity = ThalovantIdentity.from_mapping(identity_payload)
        else:
            identity = ThalovantIdentity(
                access_key=api_key,
                password=password,
                site_id=site,
                default_master=_default_master(hub_resource, endpoints, selected),
                default_port=443,
                default_path="",
                data_plane_endpoints=endpoints,
                protocols=protocols,
            )
        return BootstrapIdentityResult(
            identity=identity,
            hub=hub_resource,
            client=client,
            endpoint=selected,
            operation=_operation_or_none(client.get("operation") if isinstance(client, Mapping) else None),
        )

    async def _require_connection_type(self, client: Mapping[str, Any], connection_type: str) -> None:
        """Delete and refuse a connection the API did not make of the kind asked."""

        echoed = client.get("spec", {}).get("connection_type") if isinstance(client.get("spec"), Mapping) else None
        if echoed == connection_type:
            return
        client_id = client.get("id")
        detail = ""
        if isinstance(client_id, str) and client_id:
            try:
                await self.delete_client(client_id, etag=_optional_str(client.get("etag")))
            except ThalovantAPIError:
                detail = f" Deleting the connection it made instead ({client_id}) failed; remove it in the dashboard."
        raise ThalovantUnsupportedConnectionTypeError(
            f"The Thalovant API did not make a {connection_type!r} connection "
            f"(it answered {echoed or 'no type'!r}).{detail}"
        )

    async def get_client(self, client_id: str) -> dict[str, Any]:
        """Fetch one client (a hub connection), with the ``etag`` a change needs."""

        return await self._request("GET", f"/v1/clients/{quote(client_id, safe='')}")

    async def list_clients(
        self,
        *,
        hub_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        owner_id: str | None = None,
        include_spec: bool = True,
    ) -> dict[str, Any]:
        """List the clients (hub connections) visible to the caller."""

        params: dict[str, Any] = {"limit": limit}
        _set_param(params, "hub_id", hub_id)
        _set_param(params, "cursor", cursor)
        _set_param(params, "owner_id", owner_id)
        if not include_spec:
            params["include_spec"] = "false"
        return await self._request("GET", "/v1/clients", params=params)

    async def delete_client(self, client_id: str, *, etag: str | None = None) -> None:
        """Delete a client (a hub connection).

        The API wants the client's current ``etag`` as ``If-Match``. Without
        one this reads it first, and if another writer changed the client in
        between (HTTP 412) reads it once more and retries. A client that is
        already gone (HTTP 404) counts as deleted.
        """

        path = f"/v1/clients/{quote(client_id, safe='')}"
        for attempt in range(2):
            try:
                if etag is None:
                    etag = _required_str(await self.get_client(client_id), "etag")
                await self._request("DELETE", path, headers={"If-Match": etag})
                return
            except ThalovantAPIError as error:
                if error.status_code == 404:
                    return
                if error.status_code != 412 or attempt == 1:
                    raise
                etag = None

    async def wait_for_operation(
        self,
        operation: OperationResource | Mapping[str, Any] | str,
        *,
        timeout: float = DEFAULT_HUB_SKILL_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_OPERATION_POLL_INTERVAL,
    ) -> OperationResource:
        """Poll an accepted operation until it is ``ready``.

        ``operation`` is an :class:`OperationResource`, its dict, its id, or its
        ``links.self`` path. A ``failed`` or ``timed_out`` operation raises
        :class:`ThalovantAPIError` with the operation's own error; ``timeout``
        seconds without either raise :class:`ThalovantTimeoutError`. A 5xx while
        polling is ridden out, and so is a 429 -- a Free plan allows 60 requests
        a minute, and a wait must not end over one of them -- after the
        ``retry_after_seconds`` the API names, never past ``timeout``.
        """

        operation_id = _operation_id(operation)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            wait = poll_interval
            try:
                current = await self.get_operation(operation_id)
            except ThalovantAPIError as error:
                if error.status_code == 429:
                    wait = max(poll_interval, _retry_after_seconds(error.problem) or 0.0)
                    if wait > deadline - loop.time():
                        # The API asks for longer than is left: waiting it out
                        # would only end in the same timeout, later.
                        raise ThalovantTimeoutError(
                            f"Operation {operation_id} did not finish within {timeout:g}s "
                            "(the API asked to slow down)."
                        ) from None
                elif error.status_code is None or error.status_code < 500:
                    raise
                current = None
            if current is not None:
                if current.status == "ready":
                    return current
                if current.status in _TERMINAL_OPERATION_STATUSES:
                    raise ThalovantAPIError(
                        f"Operation {current.id} ended with status {current.status}: "
                        f"{current.error_message or current.error_code or 'no detail'}",
                        code=current.error_code,
                        detail=current.error_message,
                    )
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ThalovantTimeoutError(
                    f"Operation {operation_id} did not finish within {timeout:g}s."
                )
            await asyncio.sleep(min(wait, remaining))

    async def wait_for_admission(
        self,
        connection: BootstrapIdentityResult | OperationResource | Mapping[str, Any] | str | None,
        *,
        timeout: float = DEFAULT_ADMISSION_TIMEOUT,
        poll_interval: float = DEFAULT_OPERATION_POLL_INTERVAL,
    ) -> None:
        """Wait until the hub has admitted a new connection (about ninety seconds).

        ``connection`` is what :meth:`create_client_identity` returned, or its
        ``operation`` (the object, its dict, id or ``links.self``). Returns at
        once when there is nothing to wait on: no operation, or one the API no
        longer tracks (HTTP 404). Raises :class:`ThalovantAdmissionFailedError`
        when the operation failed or timed out on the platform, and
        :class:`ThalovantAdmissionTimeoutError` -- a
        :class:`ThalovantConnectionError` and a :class:`ThalovantTimeoutError`
        -- when ``timeout`` passes first; the connection may still be admitted
        after that. A hub that refuses the credentials inside this window is
        not admitting them yet, not refusing them.
        """

        operation = connection.operation if isinstance(connection, BootstrapIdentityResult) else connection
        if operation is None:
            return
        link = operation.links.get("self") if isinstance(operation, OperationResource) else None
        if isinstance(operation, Mapping):
            links = operation.get("links")
            link = links.get("self") if isinstance(links, Mapping) else None
        if isinstance(link, str) and link.startswith(("http://", "https://")):
            if urlsplit(link).netloc != urlsplit(self.api_url).netloc:
                # The token goes to the API's own origin and nowhere else.
                raise ThalovantAPIError("The admission operation points outside the Thalovant API.")
        try:
            await self.wait_for_operation(operation, timeout=timeout, poll_interval=poll_interval)
        except ThalovantTimeoutError:
            raise ThalovantAdmissionTimeoutError(
                f"The hub did not admit the connection within {timeout:g}s; it may still."
            ) from None
        except ThalovantAPIUnreachableError:
            # The API is out of reach, which says nothing about the hub: the
            # connection may be admitted already. Not a failed admission.
            raise
        except ThalovantAPIError as error:
            if error.status_code == 404:
                return
            raise ThalovantAdmissionFailedError(
                f"The hub could not admit the connection: {error}", error_code=error.code
            ) from error

    def require_runtime_protocol(
        self,
        result: BootstrapIdentityResult,
        *,
        protocol: HubProtocol | None = None,
    ) -> SelectedHubEndpoint:
        """Validate that a bootstrap result can be used by the current SDK runtime."""

        if protocol is None:
            protocol = result.selected_protocol or "wss"
        if protocol == "mqtt" and result.identity.mqtt is None:
            raise ThalovantUnsupportedProtocolError(
                "MQTT is enabled, but the API did not return client-scoped MQTT broker credentials."
            )
        if protocol not in {"https", "wss", "mqtt"}:
            raise ThalovantUnsupportedProtocolError(f"Unsupported protocol: {protocol}")
        endpoint = result.identity.endpoint_for(protocol)
        if not endpoint:
            raise ThalovantUnsupportedProtocolError(
                f"This hub does not expose a {protocol.upper()} endpoint for the SDK runtime."
            )
        return SelectedHubEndpoint(protocol=protocol, endpoint=endpoint)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        response = await self._send(method, path, json=json, params=params, headers=headers, auth=auth)
        if response.status_code < 200 or response.status_code >= 300:
            raise _api_error(response)
        if not response.text.strip():
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            raise ThalovantAPIError(
                "Thalovant API returned a non-JSON response.", status_code=response.status_code
            ) from exc
        if not isinstance(body, dict):
            raise ThalovantAPIError(
                "Thalovant API returned an unexpected response shape.", status_code=response.status_code
            )
        return body

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
    ) -> Any:
        request_headers = {
            "accept": "application/json",
            "user-agent": self.user_agent,
        }
        if json is not None:
            request_headers["content-type"] = "application/json"
        if headers:
            request_headers.update(headers)
        if auth:
            if not self.access_token:
                raise ThalovantAPIError("Missing Thalovant API access token.")
            request_headers["authorization"] = f"Bearer {self.access_token}"

        url = urljoin(self.api_url, path.lstrip("/"))
        parsed = urlsplit(url)
        if parsed.username or parsed.password:
            raise ThalovantAPIError("Control-plane URLs must not include embedded credentials.")
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        credential_headers = {"authorization", "proxy-authorization", "cookie"}
        has_credentials = (
            json is not None
            or any(
                str(key).lower() in credential_headers and bool(value)
                for key, value in {**self._sender.default_headers(), **request_headers}.items()
            )
            or self._sender.carries_credentials()
        )
        if has_credentials and parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ThalovantAPIError("Credential-bearing control-plane requests require HTTPS (except explicit loopback HTTP).")
        response = await self._sender.send(
            method,
            url,
            json=json,
            params=params,
            headers=request_headers,
            timeout=self.timeout,
            anonymous_plaintext=not has_credentials and parsed.scheme != "https" and not loopback,
        )
        if 300 <= response.status_code < 400:
            raise ThalovantAPIError(
                "Thalovant API redirected the request; redirects are disabled.",
                status_code=response.status_code,
            )
        return response



class ThalovantControlPlane:
    """Small authenticated client for the Thalovant API.

    **Provisioning gates.** The hub, runtime group, and skill provisioning
    routes are guarded twice: by a required scope and by a paid-plan check. The
    scope is checked **first**, so a token missing ``hubs:write`` gets HTTP 403
    ``Insufficient scopes`` and never reaches the plan gate.

    That ordering decides what a free-tier caller actually sees. Free-plan API
    tokens can only be minted with ``hubs:read``, ``clients:read``, and
    ``clients:write`` -- requesting more is refused at token creation -- so a
    free-plan API token can never carry ``hubs:write`` and therefore **never
    sees the HTTP 402 plan gate at all**: every provisioning call fails with
    HTTP 403 ``Insufficient scopes``. Do not tell a free-tier user to read the
    402 message; it will not arrive.

    HTTP 402 ``API access requires a paid plan.`` is still reachable two ways:
    from a dashboard *session* token, whose scopes are not capped by plan, and
    from an API token minted while on a paid plan and kept after a downgrade,
    since existing tokens retain the scopes they were created with.

    ``hubs:read`` implies ``hubs:inspect`` and ``hubs:preview``, so the
    inspection reads (:meth:`get_hub_runtime_capabilities`,
    :meth:`list_runtime_group_marketplace`,
    :meth:`list_runtime_group_inventory`) do work on a free-plan API token.
    """

    def __init__(
        self,
        api_url: str = DEFAULT_CONTROL_API_URL,
        *,
        access_token: str | None = None,
        timeout: float = 10.0,
        user_agent: str = DEFAULT_CONTROL_USER_AGENT,
        session: Any = None,
    ) -> None:
        if _is_aiohttp_session(session):
            raise TypeError(
                "An aiohttp.ClientSession belongs to its event loop; pass it to "
                "AsyncThalovantControlPlane. ThalovantControlPlane takes a requests-style session."
            )
        core = AsyncThalovantControlPlane(
            api_url, access_token=access_token, timeout=timeout, user_agent=user_agent, session=session,
        )
        runner = _LoopThread("thalovant-control-plane")
        self._core = core
        self._runner = runner
        self._session = session if session is not None else _SessionHandle(self)
        weakref.finalize(self, runner.stop, core.aclose)

    def _run(self, coro: Coroutine[Any, Any, _T]) -> _T:
        return self._runner.run(coro)

    @property
    def api_url(self) -> str:
        return self._core.api_url

    @api_url.setter
    def api_url(self, value: str) -> None:
        self._core.api_url = value

    @property
    def access_token(self) -> str | None:
        return self._core.access_token

    @access_token.setter
    def access_token(self, value: str | None) -> None:
        self._core.access_token = value

    @property
    def token_id(self) -> str | None:
        """The id of the token in ``access_token``, when a device login minted it."""
        return self._core.token_id

    @token_id.setter
    def token_id(self, value: str | None) -> None:
        self._core.token_id = value

    @property
    def timeout(self) -> float:
        return self._core.timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        self._core.timeout = value

    @property
    def user_agent(self) -> str:
        return self._core.user_agent

    @user_agent.setter
    def user_agent(self, value: str) -> None:
        self._core.user_agent = value

    @property
    def session(self) -> Any:
        """The requests-style session passed in, or a handle on the SDK's own.

        The handle has ``headers`` -- sent with every request, as a requests
        session's were -- and ``close()``.
        """
        return self._session

    def close(self) -> None:
        """Close the HTTP session the SDK opened; a caller's session is left alone."""
        if self._runner.running:
            self._run(self._core.aclose())

    def __enter__(self) -> ThalovantControlPlane:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _require_secure_token_exchange(self) -> None:
        self._core._require_secure_token_exchange()

    def require_runtime_protocol(
        self,
        result: BootstrapIdentityResult,
        *,
        protocol: HubProtocol | None = None,
    ) -> SelectedHubEndpoint:
        """Validate that a bootstrap result can be used by the current SDK runtime."""

        return self._core.require_runtime_protocol(result, protocol=protocol)

    def login_with_browser(
        self,
        *,
        scopes: Iterable[str] | None = None,
        client_name: str | None = None,
        open_browser: bool = True,
        prompt: Callable[[dict[str, Any]], None] | None = None,
        timeout: float = 900.0,
    ) -> dict[str, Any]:
        """Sign in through the browser device flow and store the API token.

        This is the sign-in path for accounts without a password (for example
        Google sign-in). It requests a device authorization, tells the user to
        visit ``verification_uri`` and enter the short ``user_code`` (pass a
        ``prompt`` callable receiving the authorization payload to present it
        yourself), optionally opens the browser at
        ``verification_uri_complete``, and polls until the request is approved,
        denied, expired, or ``timeout`` seconds elapse.

        On approval the returned ``access_token`` is a durable scoped API token
        and is stored on ``self.access_token`` exactly like ``login()``.
        :meth:`begin_device_login` and :meth:`poll_device_login` are the same
        flow one step at a time.
        """

        grant = self.begin_device_login(scopes=scopes, client_name=client_name)
        _present_device_login(grant, prompt=prompt, open_browser=open_browser)
        token = self._poll_device_token(grant.device_code, interval=grant.interval, timeout=timeout)
        return self._core._accept_token(token)

    def _poll_device_token(
        self,
        device_code: str,
        *,
        interval: float,
        timeout: float,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        """Poll the device token endpoint until approval or a terminal state.

        ``sleep`` and ``clock`` are injectable so tests can drive the loop
        without real waiting.
        """

        return self._run(
            self._core._poll_device_token(
                device_code, interval=interval, timeout=timeout,
                sleep=_awaitable_sleep(sleep), clock=clock,
            )
        )

    def install_hub_skill(
        self,
        hub_id: str,
        skill: str,
        *,
        version: str = "latest",
        wait: bool = False,
        timeout: float = DEFAULT_HUB_SKILL_WAIT_TIMEOUT,
    ) -> HubSkillOperation:
        """Install a skill on the runtime group attached to a hub.

        See :meth:`AsyncThalovantControlPlane.install_hub_skill`.
        """

        accepted = self._run(self._core.install_hub_skill(hub_id, skill, version=version))
        if not wait:
            return accepted
        return self._wait_for_hub_skill_operation(accepted, converged="installed", timeout=timeout)

    def update_hub_skill(
        self,
        hub_id: str,
        skill: str,
        *,
        version: str,
        wait: bool = False,
        timeout: float = DEFAULT_HUB_SKILL_WAIT_TIMEOUT,
    ) -> HubSkillOperation:
        """Move a skill on the hub's shared runtime group to another version.

        See :meth:`AsyncThalovantControlPlane.update_hub_skill`.
        """

        accepted = self._run(self._core.update_hub_skill(hub_id, skill, version=version))
        if not wait:
            return accepted
        return self._wait_for_hub_skill_operation(accepted, converged="installed", timeout=timeout)

    def remove_hub_skill(
        self,
        hub_id: str,
        skill: str,
        *,
        wait: bool = False,
        timeout: float = DEFAULT_HUB_SKILL_WAIT_TIMEOUT,
    ) -> HubSkillOperation:
        """Remove a skill from the hub's shared runtime group.

        See :meth:`AsyncThalovantControlPlane.remove_hub_skill`.
        """

        accepted = self._run(self._core.remove_hub_skill(hub_id, skill))
        if not wait:
            return accepted
        return self._wait_for_hub_skill_operation(accepted, converged="removed", timeout=timeout)

    def _wait_for_hub_skill_operation(
        self,
        accepted: HubSkillOperation,
        *,
        converged: HubSkillOperationState,
        timeout: float,
        interval: float = DEFAULT_HUB_SKILL_POLL_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> HubSkillOperation:
        """Poll an accepted hub skill command until its operation is terminal."""

        return self._run(
            self._core._wait_for_hub_skill_operation(
                accepted, converged=converged, timeout=timeout, interval=interval,
                sleep=_awaitable_sleep(sleep), clock=clock,
            )
        )

    def login(
        self,
        email: str,
        password: str,
        *,
        scope: str | None = None,
        otp_code: str | None = None,
        recovery_code: str | None = None,
    ) -> dict[str, Any]:
        """Authenticate with email/password and store the returned access token.

        MFA-enabled accounts must also provide a TOTP ``otp_code`` or a one-time
        ``recovery_code``; the API rejects the login with ``mfa_required``
        otherwise.
        """
        return self._run(self._core.login(email, password, scope=scope, otp_code=otp_code, recovery_code=recovery_code))

    def complete_native_sign_in(
        self,
        code: str,
        verifier: str,
        client_id: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        """Exchange an authorization code for a scoped access token and store it.

        The other half of :func:`thalovant.native_auth.begin_native_sign_in`.
        The verifier is sent here and nowhere else; it never entered the
        browser, which is what makes an intercepted code useless to whoever
        intercepted it.

        A code presented twice revokes the token the first exchange minted
        (RFC 9700), so retrying a failed exchange with the same code destroys
        the token it is trying to obtain. Start again from
        ``begin_native_sign_in`` instead.
        """
        return self._run(self._core.complete_native_sign_in(code, verifier, client_id, redirect_uri))

    def begin_device_login(
        self,
        *,
        scopes: Iterable[str] | None = None,
        client_name: str | None = None,
    ) -> DeviceAuthorization:
        """Start a device sign-in: a code for a person to approve in a browser.

        ``scopes`` are what the token will carry; the API defaults to
        ``hubs:read`` and ``clients:write``. A Free plan can approve only
        ``hubs:read``, ``clients:read`` and ``clients:write``. Show the person
        ``verification_uri`` and ``user_code`` (or ``verification_uri_complete``,
        which carries the code), then call :meth:`poll_device_login` every
        ``interval`` seconds.
        """
        return self._run(self._core.begin_device_login(scopes=scopes, client_name=client_name))

    def poll_device_login(self, authorization: DeviceAuthorization | str) -> ApiToken:
        """Ask once whether the device sign-in was approved.

        Returns the token and stores it on this client (``access_token`` and
        ``token_id``). Otherwise raises :class:`ThalovantDeviceLoginPending`
        -- poll again after its ``interval``, which a ``slow_down`` has already
        lengthened -- :class:`ThalovantDeviceLoginExpired` or
        :class:`ThalovantDeviceLoginDenied`. All three are
        :class:`ThalovantAPIError`.
        """
        return self._run(self._core.poll_device_login(authorization))

    def revoke_api_token(self, token_id: str | None = None) -> None:
        """Revoke an API token; by default the one this client signed in with.

        A token may always revoke itself (``DELETE /v1/auth/api-tokens/{id}``),
        whatever its scopes. Revoking the token in use forgets it here too, so
        a later call fails locally rather than with a 401.

        Revoking the token in use is idempotent. A token already revoked, or
        expired, cannot authenticate its own revoke, so the API answers 401;
        the token is dead either way, so that counts as revoked and the token
        is forgotten; revoking it again then sends nothing and succeeds, until
        the next sign-in. Revoking another token by id is not: the API's own
        answer -- 404 for one it does not know -- is raised as usual.
        """
        return self._run(self._core.revoke_api_token(token_id))

    def get_profile(self) -> dict[str, Any]:
        """The signed-in account: ``id``, ``email``, ``display_name``, ``role``.

        The API wraps it in ``data``; this returns what is inside.
        """
        return self._run(self._core.get_profile())

    def list_hubs(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        """List hubs visible to the authenticated user."""
        return self._run(self._core.list_hubs(limit=limit, cursor=cursor, owner_id=owner_id))

    def list_public_hubs(
        self,
        *,
        limit: int = 24,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List public, active hubs available for discovery without API auth."""
        return self._run(self._core.list_public_hubs(limit=limit, cursor=cursor))

    def get_operation(self, operation_id: str) -> OperationResource:
        """Read durable progress for an operation accepted by the API."""
        return self._run(self._core.get_operation(operation_id))

    def list_memory_items(
        self,
        *,
        scope: str | None = None,
        kind: str | None = None,
        owner_id: str | None = None,
        hub_id: str | None = None,
        query: str | None = None,
        include_deleted: bool = False,
        include_expired: bool = False,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """List durable memory items visible to the authenticated user."""
        return self._run(self._core.list_memory_items(scope=scope, kind=kind, owner_id=owner_id, hub_id=hub_id, query=query, include_deleted=include_deleted, include_expired=include_expired, limit=limit, offset=offset))

    def get_memory_summary(self, *, owner_id: str | None = None) -> dict[str, Any]:
        """Summarize durable memory by scope and kind."""
        return self._run(self._core.get_memory_summary(owner_id=owner_id))

    def create_memory_item(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Create a durable memory item."""
        return self._run(self._core.create_memory_item(payload))

    def get_memory_item(self, memory_id: str) -> dict[str, Any]:
        """Fetch one durable memory item by id."""
        return self._run(self._core.get_memory_item(memory_id))

    def update_memory_item(self, memory_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Update a durable memory item."""
        return self._run(self._core.update_memory_item(memory_id, payload))

    def delete_memory_item(self, memory_id: str) -> None:
        """Soft-delete a durable memory item."""
        return self._run(self._core.delete_memory_item(memory_id))

    def get_analytics_overview(
        self,
        *,
        range: str | None = None,
        bucket: str | None = None,
        hub_id: str | None = None,
        client_id: str | None = None,
        country: str | None = None,
        message: str | None = None,
        utterance: str | None = None,
        intent: str | None = None,
        time_start: str | None = None,
        time_end: str | None = None,
        weekday: int | None = None,
        hour: int | None = None,
    ) -> dict[str, Any]:
        """Fetch the workspace analytics overview used by the dashboard."""
        return self._run(self._core.get_analytics_overview(range=range, bucket=bucket, hub_id=hub_id, client_id=client_id, country=country, message=message, utterance=utterance, intent=intent, time_start=time_start, time_end=time_end, weekday=weekday, hour=hour))

    def get_hub(self, hub_id: str) -> dict[str, Any]:
        """Fetch one hub resource."""
        return self._run(self._core.get_hub(hub_id))

    def get_public_hub(self, hub_ref: str) -> dict[str, Any]:
        """Fetch one public hub by slug or id without API auth."""
        return self._run(self._core.get_public_hub(hub_ref))

    def create_hub(
        self,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a hub.

        ``payload`` mirrors the API's hub create body: ``name`` and ``spec`` are
        required, and ``slug``, ``namespace``, ``runtime_group_id``, ``domain``,
        ``active``, ``visibility``, ``capacity_profile``, and ``owner_id`` are
        optional. camelCase keys are accepted and sent as snake_case.

        ``spec`` is validated against the API's hub schema, which **requires a
        non-empty ``version`` string**; a spec without one fails with HTTP 422
        ``Schema validation failed`` rather than being defaulted.

        Retain an explicit ``idempotency_key`` before the first call and reuse
        it with the same payload after an uncertain outcome. An omitted key is
        freshly generated on each invocation, so retrying without the original
        key can create a second hub.

        Requires a paid plan and a token with the ``hubs:write`` scope; see
        :class:`ThalovantControlPlane` for why the scope gate is the one a
        free-plan API token actually hits.
        """
        return self._run(self._core.create_hub(payload, idempotency_key=idempotency_key))

    def update_hub(
        self,
        hub_id: str,
        payload: Mapping[str, Any],
        *,
        etag: str,
    ) -> dict[str, Any]:
        """Partially update a hub.

        The API enforces optimistic locking on this route: pass the ``etag``
        from the hub resource you read, which is sent as ``If-Match``. A stale
        or missing value fails the request with HTTP 412 and no change is made;
        re-read the hub with :meth:`get_hub` and retry with the new ``etag``.

        A prior read is therefore mandatory, and it must be a *body* read: the
        API carries the validator only in the ``etag`` field of the hub
        resource and emits **no ``ETag`` response header**, so
        ``response.headers["ETag"]`` is not an alternative source. Take it from
        ``get_hub(hub_id)["etag"]`` (``list_hubs`` entries carry it too).

        ``name``, ``namespace``, and ``domain`` are immutable after creation.
        The API drops them from the patch when the value you send matches the
        stored one (or is ``None``) and rejects a *different* value with HTTP
        400 ``<Field> cannot be changed after hub creation``. This SDK sends
        them through rather than rejecting them locally, because it cannot know
        the stored values without a second read and refusing them outright
        would reject patches the API accepts. Send only the fields you mean to
        change and the distinction never comes up.

        ``slug``, ``active``, ``visibility``, ``capacity_profile``,
        ``runtime_group_id``, and ``spec`` are patchable; ``is_locked`` is
        admin-only.

        Requires a paid plan and a token with the ``hubs:write`` scope; see
        :class:`ThalovantControlPlane` for the gate ordering.
        """
        return self._run(self._core.update_hub(hub_id, payload, etag=etag))

    def delete_hub(self, hub_id: str, *, etag: str) -> None:
        """Delete a hub and its dependent clients and ACLs.

        Like :meth:`update_hub` this route requires the hub's current ``etag``,
        sent as ``If-Match``; a stale value fails with HTTP 412. The value
        comes only from the hub resource's ``etag`` body field -- the API sends
        no ``ETag`` response header -- so a prior :meth:`get_hub` is mandatory.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """
        return self._run(self._core.delete_hub(hub_id, etag=etag))

    def release_hub(
        self,
        hub_id: str,
        *,
        channel: str | None = None,
        mode: str | None = None,
        version: str | None = None,
        images: Mapping[str, str] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Apply a hub release policy and return the updated hub.

        Every option is optional; omitted fields fall back to the workspace
        release policy. Passing ``images`` switches the hub to ``custom`` mode
        unless you also pass ``mode``. Unless you are a platform
        administrator, each image must be one the platform releases for its
        key: a catalog pin of the stable or alpha channel, the hub's current,
        recommended or release-policy image, or the platform's default image.
        ``listener`` also accepts any tag or digest of
        ``ghcr.io/thalovant/hivemind-listener``; ``preview_bridge`` takes only
        those images. The API refuses anything else with HTTP 403
        ``platform_image_required``, and the error's ``problem`` names what
        each refused key may be instead.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """
        return self._run(self._core.release_hub(hub_id, channel=channel, mode=mode, version=version, images=images, reason=reason))

    def set_hub_rating(self, hub_id: str, rating: int) -> dict[str, Any]:
        """Rate a public hub from 1 to 5 and return the updated hub.

        Only public hubs can be rated, and owners cannot rate their own hubs.
        Requires a token with the ``hubs:write`` scope; no paid plan is needed.
        """
        return self._run(self._core.set_hub_rating(hub_id, rating))

    def clear_hub_rating(self, hub_id: str) -> dict[str, Any]:
        """Remove the caller's rating from a public hub and return the hub.

        Requires a token with the ``hubs:write`` scope; no paid plan is needed.
        """
        return self._run(self._core.clear_hub_rating(hub_id))

    def get_hub_runtime_capabilities(self, hub_id: str) -> dict[str, Any]:
        """Read the skill and intent inventory a hub runtime exposes.

        The response is not always live, so **branch on the envelope's
        ``source``** rather than assuming the counts are current:

        ``ovos-runtime``
            A connected client answered. This is the only live, canonical
            reading, and the only one the API caches.
        ``ovos-runtime-unavailable`` / ``ovos-runtime-timeout``
            No client could answer, so the API fell back to the hub's runtime
            group snapshot (its desired skills merged with the last observed
            inventory) and still returned HTTP 200. Treat the skills and
            intents as **stale**: they describe what the group is configured
            to run, not what is running now.

        HTTP 409 is answered only when that fallback has nothing to serve
        either -- the hub is attached to no runtime group, or the group has no
        desired and no observed skills at all. A hub with a configured group is
        therefore far likelier to return a stale 200 than a 409.

        The route is also rate limited per caller and hub: HTTP 429 carries a
        ``Retry-After`` header with the number of seconds to wait.

        Requires a token with the ``hubs:inspect`` scope; no paid plan is
        needed.
        """
        return self._run(self._core.get_hub_runtime_capabilities(hub_id))

    def list_runtime_groups(self, *, owner_id: str | None = None) -> dict[str, Any]:
        """List runtime groups visible to the authenticated user.

        ``owner_id`` is **enforced, not silently scoped**: a non-admin caller
        passing another tenant's id gets HTTP 403 ``Ownership required``
        (tenant members of that owner are allowed). This is the opposite of
        :meth:`list_marketplace_skills`, where a non-admin's ``owner_id`` is
        quietly overridden with their own. Admin tokens may pass any
        ``owner_id``; omitting it as an admin lists every tenant's groups.

        Requires a token with the ``hubs:read`` scope.
        """
        return self._run(self._core.list_runtime_groups(owner_id=owner_id))

    def get_runtime_group(self, runtime_group_id: str) -> dict[str, Any]:
        """Fetch one runtime group.

        Requires a token with the ``hubs:read`` scope.
        """
        return self._run(self._core.get_runtime_group(runtime_group_id))

    def create_runtime_group(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Create a runtime group.

        ``payload`` takes the API's create body: ``name`` is required, and
        ``description``, ``environment``, ``owner_id``, and
        ``clone_from_default`` are optional. camelCase keys are accepted and
        sent as snake_case.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """
        return self._run(self._core.create_runtime_group(payload))

    def update_runtime_group(
        self,
        runtime_group_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Update a runtime group's ``name``, ``description``, or ``spec``.

        ``spec`` patches ``replicas`` and container ``resources``. This route
        does not use ``If-Match``.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """
        return self._run(self._core.update_runtime_group(runtime_group_id, payload))

    def get_runtime_group_config(self, runtime_group_id: str) -> dict[str, Any]:
        """Read a runtime group's runtime configuration and personas.

        Requires a token with the ``hubs:read`` scope.
        """
        return self._run(self._core.get_runtime_group_config(runtime_group_id))

    def update_runtime_group_config(
        self,
        runtime_group_id: str,
        config: Mapping[str, Any],
        *,
        personas: Mapping[str, Any] | None = None,
        merge: bool = True,
    ) -> dict[str, Any]:
        """Update a runtime group's configuration.

        **The top level of ``config`` is the OVOS mycroft configuration**, plus
        an ``env`` key for container variables. It is not a wrapper around one.
        The renderer pops ``env`` and passes everything else through as the
        runtime's mycroft config, so::

            # right -- these are read by ovos-core at load
            {"lang": "en-us", "secondary_langs": ["fr-fr"], "env": [...]}

            # wrong -- renders as spec.config.mycroft.mycroft and is ignored
            {"mycroft": {"lang": "en-us", "secondary_langs": ["fr-fr"]}}

        The wrong form fails silently: unknown keys are copied into mycroft.conf
        verbatim, so the call succeeds, reads back exactly what was sent, and
        the runtime never sees the setting. Worth stating because the CR and the
        gitops manifests *do* nest it one deeper -- ``spec.config.mycroft.*`` --
        and copying that shape into this call is the natural mistake.

        **The API replaces the stored configuration; it does not merge.** This
        docstring claimed the opposite, and the cost of believing it was real:
        sending ``{"mycroft": {...}}`` to add a language setting dropped the
        group's entire ``env`` block, taking the hub memory's Redis host, key
        prefix and both ``secretKeyRef`` credential bindings with it. Nothing
        in the response says so -- the call succeeds and reads back exactly
        what was sent.

        So by default this reads the stored configuration first and deep-merges
        ``config`` into it, which is the behaviour the old docstring described
        and every caller reasonably assumed. Mappings are merged key by key;
        anything else, lists included, is replaced by the incoming value,
        because a list is a value rather than a namespace and no caller means
        "append" by passing one.

        Pass ``merge=False`` for the raw replacing call, when the intent really
        is to define the whole configuration.

        Merges use the GET revision in a conditional PUT. If another writer
        changes the configuration, reread and reapply the original delta, up to
        three write attempts. Only a 412 conflict is retried; network and other
        failures propagate. An older API without revisions or conditional PUT
        support is rejected without falling back to an unsafe write.

        ``merge=False`` retains unconditional PATCH replacement semantics.
        Concurrent writers of the same key intentionally replace that value;
        use guarded writes consistently to preserve unrelated changes.

        ``personas`` is replaced only when provided, merge or not.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        Guarded merging also requires ``hubs:read``.
        """
        return self._run(self._core.update_runtime_group_config(runtime_group_id, config, personas=personas, merge=merge))

    def release_runtime_group(
        self,
        runtime_group_id: str,
        *,
        channel: str | None = None,
        mode: str | None = None,
        version: str | None = None,
        images: Mapping[str, str] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Apply a runtime image policy and return the updated runtime group.

        Options behave like :meth:`release_hub`, including the platform-image
        rule: ``core`` also accepts any tag or digest of
        ``ghcr.io/thalovant/ovos-core``, and ``bus`` takes only the images the
        platform releases for it.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """
        return self._run(self._core.release_runtime_group(runtime_group_id, channel=channel, mode=mode, version=version, images=images, reason=reason))

    def delete_runtime_group(self, runtime_group_id: str) -> None:
        """Delete a runtime group.

        The API answers HTTP 409 for the workspace default group and for a
        group that still has hubs attached.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """
        return self._run(self._core.delete_runtime_group(runtime_group_id))

    def list_marketplace_skills(
        self,
        *,
        owner_id: str | None = None,
        include_inactive: bool = False,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """List the marketplace skill catalog visible to the authenticated user.

        Returns ``{"data": [...]}`` where each entry carries the catalog fields
        an install needs -- ``skill_id``, ``source_type``, ``source_ref``,
        ``package_name``, ``version`` compatibility, ``config_schema`` and
        ``secret_schema`` -- alongside presentation and access fields such as
        ``category``, ``tags``, ``verified``, ``access_tier`` and
        ``billing_sku``. Global catalog entries and the caller's own tenant
        entries are both included.

        ``owner_id`` and ``include_inactive`` are honoured for admin tokens
        only; the API **silently** scopes a non-admin caller to their own
        tenant and to active entries -- no error is raised, so a non-admin
        passing another tenant's ``owner_id`` gets their own catalog back.
        Contrast :meth:`list_runtime_groups`, which answers HTTP 403 for the
        same mistake. ``force_refresh`` re-syncs the global catalog from its
        source before answering, which is slower.

        Requires a token with the ``hubs:read`` scope. Unlike the provisioning
        routes this catalog is **not** paid-gated, so free-tier callers can
        browse the marketplace before upgrading -- only the install itself
        needs a paid plan.
        """
        return self._run(self._core.list_marketplace_skills(owner_id=owner_id, include_inactive=include_inactive, force_refresh=force_refresh))

    def list_runtime_group_marketplace(
        self,
        runtime_group_id: str,
        *,
        refresh_inventory: bool = False,
    ) -> dict[str, Any]:
        """List the marketplace catalog resolved against one runtime group.

        This is the discovery view to use before installing: every catalog
        entry is returned with the group's own state folded in -- whether the
        skill is desired (``active``, ``version_pin``, ``source_type``),
        whether it was observed running (``observed_source``,
        ``observed_at``, intent counts), operator status fields, and the
        access verdict for the tenant plan (``purchase_required``,
        ``installable``, ``access_message``). The envelope also carries
        ``runtime_group_id``, ``observed_at``, ``source``, ``operator_phase``
        and ``operator_message``.

        ``data`` is driven by the **catalog**, not by the runtime observation:
        it is the catalog unioned with the group's desired and observed skills,
        so it stays populated even when nothing is reporting. A ``source``
        saying the snapshot is empty therefore tells you nothing about the
        length of ``data`` -- do not use one as a proxy for the other. (``data``
        is empty only in the degenerate case of an empty catalog with no
        desired and no observed skills.)

        ``source`` on this route reports where the *observation* came from, and
        the default read draws from a different set of values than
        :meth:`list_runtime_group_inventory`:

        ``runtime-group-cache``
            Answered from a stored inventory snapshot.
        ``runtime-group-cache-empty``
            No snapshot is stored yet. This value is unique to this route.
        ``ovos-runtime-operator``
            The operator's published status differed from the stored snapshot,
            so the API re-synced it while serving the request.

        ``ovos-runtime-operator-pending`` appears here **only** when
        ``refresh_inventory=True``, which forces a live operator read (and can
        then return any of the inventory route's values).

        Requires a token with the ``hubs:inspect`` scope; no paid plan is
        needed to browse. The API answers HTTP 404 for an unknown group and
        HTTP 403 ``Ownership required`` when the caller neither owns it nor is
        an admin.
        """
        return self._run(self._core.list_runtime_group_marketplace(runtime_group_id, refresh_inventory=refresh_inventory))

    def list_runtime_group_inventory(
        self,
        runtime_group_id: str,
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """List the skills a runtime group is actually observed running.

        Where :meth:`list_runtime_group_marketplace` answers "what could be
        installed here", this answers "what is loaded right now": each entry
        carries ``skill_id``, ``version``, ``source``, ``active``,
        ``adapt_intents``, ``padatious_intents``, ``total_intents`` and
        ``observed_at``. The envelope reports ``source`` -- the observation's
        provenance, one of ``ovos-runtime-operator``, ``runtime-group-cache``
        or ``ovos-runtime-operator-pending`` -- plus ``operator_phase`` and
        ``operator_message``. ``runtime-group-cache-empty`` never appears here;
        it belongs to :meth:`list_runtime_group_marketplace`.

        ``refresh`` forces a live operator read; the API also refreshes on its
        own when it holds no cached snapshot. When nothing is reporting this
        route returns an empty ``data`` list with
        ``source="ovos-runtime-operator-pending"`` rather than failing.

        Requires a token with the ``hubs:inspect`` scope; no paid plan is
        needed. HTTP 404 for an unknown group, HTTP 403 ``Ownership required``
        when the caller neither owns it nor is an admin.
        """
        return self._run(self._core.list_runtime_group_inventory(runtime_group_id, refresh=refresh))

    def install_runtime_group_skill(
        self,
        runtime_group_id: str,
        skill_id: str,
        *,
        marketplace_skill_id: str | None = None,
        source_type: str = "catalog",
        source_ref: str | None = None,
        version_pin: str | None = None,
        active: bool = True,
    ) -> dict[str, Any]:
        """Install (or re-install) a skill in a runtime group.

        Answers HTTP **200**, not 201: the route upserts, so installing a skill
        that is already present updates the existing entry in place, and the
        returned desired-skill resource is the same shape either way.

        ``source_type`` is a free-form string of 1-32 characters, not an
        enumeration. Only two values are interpreted specially -- ``catalog``
        (the default) resolves the skill against the marketplace and answers
        HTTP 404 ``Marketplace skill not found.`` when it is absent, and
        ``git`` requires ``source_ref`` to be a valid repository URL (HTTP 422
        otherwise). Any other value is accepted and stored as given, with
        ``source_ref`` defaulting to ``skill_id``. The API lower-cases and
        strips whatever you send.

        Two *different* HTTP 402s can come back, and they mean different
        things:

        * ``API access requires a paid plan.`` -- the plan-level API gate that
          guards every provisioning route.
        * ``This skill requires paid marketplace access for the tenant plan.``
          -- a per-skill check on a catalog entry whose ``access_tier`` is
          ``paid`` when the tenant plan lacks marketplace access. The plan can
          be paid and this can still fail, so read
          ``installable``/``purchase_required`` from
          :meth:`list_runtime_group_marketplace` before installing.

        A deactivated catalog entry answers HTTP 409 instead.

        Requires a paid plan and a token with the ``hubs:write`` scope; see
        :class:`ThalovantControlPlane` for why a free-plan API token sees HTTP
        403 here and never either 402.
        """
        return self._run(self._core.install_runtime_group_skill(runtime_group_id, skill_id, marketplace_skill_id=marketplace_skill_id, source_type=source_type, source_ref=source_ref, version_pin=version_pin, active=active))

    def uninstall_runtime_group_skill(self, runtime_group_id: str, skill_id: str) -> None:
        """Remove a skill from a runtime group.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """
        return self._run(self._core.uninstall_runtime_group_skill(runtime_group_id, skill_id))

    def list_hub_skills(self, hub_id: str) -> HubSkillList:
        """List the skills one hub carries, with their install state.

        Where :meth:`list_runtime_group_inventory` describes a whole runtime
        group, this selects the runtime through its hub UUID. The :class:`HubSkillList` envelope
        says where the reading came from (``source``, ``observed_at``, the
        runtime's phase and message); each :class:`HubSkill` row in ``data``
        carries the requested ``version``, the ``installed_version`` and
        ``observed_version``, the catalog's ``latest_version`` with
        ``update_available``, and the ``state`` (``pending``, ``installed``,
        ``failed``, ``removing``, ``drifted``, ``quarantined``, ``unmanaged``)
        with the runtime's last error for a failed change. A hub can carry no
        skills at all; that is an empty ``data``, not an error.

        ``hub_id`` is the hub's id. The authenticated hub routes do not accept
        slugs. Hub-restricted tokens are honoured.

        Requires a token with the ``hubs:inspect`` scope (``hubs:read`` implies
        it).
        """
        return self._run(self._core.list_hub_skills(hub_id))

    def list_hub_skill_history(self, hub_id: str, *, limit: int = 50) -> dict[str, Any]:
        """Read newest-first skill events and operations for the hub's shared runtime.

        Requires ``hubs:inspect`` (implied by ``hubs:read``). The limit is 1–200.
        Entries retain the API's event/operation fields, including nullable actor
        and version information. Every hub sharing this runtime sees its history.
        """
        return self._run(self._core.list_hub_skill_history(hub_id, limit=limit))

    def create_client(self, payload: Mapping[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        """Create a hub client through the API."""
        return self._run(self._core.create_client(payload, idempotency_key=idempotency_key))

    def create_client_identity(
        self,
        hub: str | Mapping[str, Any],
        *,
        name: str,
        site_id: str | None = None,
        spec: Mapping[str, Any] | None = None,
        owner_id: str | None = None,
        active: bool = True,
        preferred_protocols: Iterable[HubProtocol] = DEFAULT_PROTOCOL_PREFERENCE,
        idempotency_key: str | None = None,
        connection_type: str | None = None,
    ) -> BootstrapIdentityResult:
        """Provision a client and return local identity secrets for direct hub access.

        The API stores client credentials in Vault and returns only references.
        This method therefore generates the secret material locally, sends it to
        the API once, and keeps the usable identity in the returned object.

        ``connection_type`` (``voice_satellite``, ``web_chat``, ``developer``,
        ``embedded``, ``home_assistant``...) is sent as ``spec.connection_type``,
        and the kind decides what the connection may send and receive. The API
        must say the connection is of that kind: a 422 about the field, or a
        created connection whose type came back different, raises
        :class:`ThalovantUnsupportedConnectionTypeError` -- after deleting that
        connection, which would otherwise be an ordinary satellite nobody asked
        for. A plan that does not allow it raises :class:`ThalovantPlanError`;
        a hub that already holds the one link of its kind,
        :class:`ThalovantAlreadyLinkedError`.

        The result's ``operation`` tracks the hub admitting the connection,
        about ninety seconds; :meth:`wait_for_admission` waits for it.
        """
        return self._run(self._core.create_client_identity(hub, name=name, site_id=site_id, spec=spec, owner_id=owner_id, active=active, preferred_protocols=preferred_protocols, idempotency_key=idempotency_key, connection_type=connection_type))

    def get_client(self, client_id: str) -> dict[str, Any]:
        """Fetch one client (a hub connection), with the ``etag`` a change needs."""
        return self._run(self._core.get_client(client_id))

    def list_clients(
        self,
        *,
        hub_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        owner_id: str | None = None,
        include_spec: bool = True,
    ) -> dict[str, Any]:
        """List the clients (hub connections) visible to the caller."""
        return self._run(self._core.list_clients(hub_id=hub_id, limit=limit, cursor=cursor, owner_id=owner_id, include_spec=include_spec))

    def delete_client(self, client_id: str, *, etag: str | None = None) -> None:
        """Delete a client (a hub connection).

        The API wants the client's current ``etag`` as ``If-Match``. Without
        one this reads it first, and if another writer changed the client in
        between (HTTP 412) reads it once more and retries. A client that is
        already gone (HTTP 404) counts as deleted.
        """
        return self._run(self._core.delete_client(client_id, etag=etag))

    def wait_for_operation(
        self,
        operation: OperationResource | Mapping[str, Any] | str,
        *,
        timeout: float = DEFAULT_HUB_SKILL_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_OPERATION_POLL_INTERVAL,
    ) -> OperationResource:
        """Poll an accepted operation until it is ``ready``.

        ``operation`` is an :class:`OperationResource`, its dict, its id, or its
        ``links.self`` path. A ``failed`` or ``timed_out`` operation raises
        :class:`ThalovantAPIError` with the operation's own error; ``timeout``
        seconds without either raise :class:`ThalovantTimeoutError`. A 5xx while
        polling is ridden out, and so is a 429 -- a Free plan allows 60 requests
        a minute, and a wait must not end over one of them -- after the
        ``retry_after_seconds`` the API names, never past ``timeout``.
        """
        return self._run(self._core.wait_for_operation(operation, timeout=timeout, poll_interval=poll_interval))

    def wait_for_admission(
        self,
        connection: BootstrapIdentityResult | OperationResource | Mapping[str, Any] | str | None,
        *,
        timeout: float = DEFAULT_ADMISSION_TIMEOUT,
        poll_interval: float = DEFAULT_OPERATION_POLL_INTERVAL,
    ) -> None:
        """Wait until the hub has admitted a new connection (about ninety seconds).

        ``connection`` is what :meth:`create_client_identity` returned, or its
        ``operation`` (the object, its dict, id or ``links.self``). Returns at
        once when there is nothing to wait on: no operation, or one the API no
        longer tracks (HTTP 404). Raises :class:`ThalovantAdmissionFailedError`
        when the operation failed or timed out on the platform, and
        :class:`ThalovantAdmissionTimeoutError` -- a
        :class:`ThalovantConnectionError` and a :class:`ThalovantTimeoutError`
        -- when ``timeout`` passes first; the connection may still be admitted
        after that. A hub that refuses the credentials inside this window is
        not admitting them yet, not refusing them.
        """
        return self._run(self._core.wait_for_admission(connection, timeout=timeout, poll_interval=poll_interval))

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        return self._run(self._core._request(method, path, json=json, params=params, headers=headers, auth=auth))

    def _send(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
    ) -> Any:
        return self._run(self._core._send(method, path, json=json, params=params, headers=headers, auth=auth))


def _new_secret() -> str:
    return secrets.token_urlsafe(32)


_CLIENT_SECRET_KEYS = frozenset(
    {
        "initialidentify",
        "initialidentifytoken",
        "apikey",
        "accesskey",
        "password",
        "cryptokey",
        "token",
        "accesstoken",
        "refreshtoken",
        "authorization",
        "clientsecret",
        "privatekey",
        "secret",
        "apisecret",
        "secretkey",
        "credentials",
    }
)


def _normalize_secret_key(key: str) -> str:
    return key.lower().replace("_", "").replace("-", "")


def _scrub_client_secrets(value: Any) -> Any:
    """Deep-copy an API client resource with its secret-bearing keys removed.

    Drops the ``initial_identify`` block, ``initial_identify_token``, and the
    credential keys echoed back inside ``spec`` (``apiKey``/``password``/
    ``cryptoKey``), wherever they appear. Reference keys such as ``apiKeyRef``
    are kept: they point at Vault entries and carry no secret material.
    """

    if isinstance(value, Mapping):
        return {
            key: _scrub_client_secrets(item)
            for key, item in value.items()
            if not isinstance(key, str) or _normalize_secret_key(key) not in _CLIENT_SECRET_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_scrub_client_secrets(item) for item in value]
    return value


def _normalize_control_api_url(api_url: str) -> str:
    """Normalize the API root while accepting versioned roots for convenience."""

    trimmed = (api_url or DEFAULT_CONTROL_API_URL).strip().rstrip("/")
    if trimmed.endswith("/v1"):
        trimmed = trimmed[:-3]
    return trimmed.rstrip("/") + "/"


def _set_param(params: dict[str, Any], key: str, value: str | None) -> None:
    if value and value.strip():
        params[key] = value


def _snake_case_payload(
    payload: Mapping[str, Any],
    renames: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    """Copy a request body, renaming the camelCase keys the API takes as snake_case."""

    data = dict(payload)
    for source, target in renames:
        if source in data:
            data[target] = data.pop(source)
    return data


def _memory_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _snake_case_payload(
        payload,
        (
            ("ownerId", "owner_id"),
            ("hubId", "hub_id"),
            ("consentScope", "consent_scope"),
            ("consentVersion", "consent_version"),
            ("retentionPolicy", "retention_policy"),
            ("expiresAt", "expires_at"),
            ("clearExpiresAt", "clear_expires_at"),
        ),
    )


def _hub_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _snake_case_payload(
        payload,
        (
            ("ownerId", "owner_id"),
            ("runtimeGroupId", "runtime_group_id"),
            ("capacityProfile", "capacity_profile"),
            ("isLocked", "is_locked"),
        ),
    )


def _runtime_group_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _snake_case_payload(
        payload,
        (
            ("ownerId", "owner_id"),
            ("cloneFromDefault", "clone_from_default"),
        ),
    )


def _release_payload(
    *,
    channel: str | None,
    mode: str | None,
    version: str | None,
    images: Mapping[str, str] | None,
    reason: str | None,
) -> dict[str, Any]:
    """Build a release-apply body, omitting the options the caller left unset."""

    payload: dict[str, Any] = {}
    if channel is not None:
        payload["channel"] = channel
    if mode is not None:
        payload["mode"] = mode
    if version is not None:
        payload["version"] = version
    if images is not None:
        payload["images"] = dict(images)
    if reason is not None:
        payload["reason"] = reason
    return payload


def _required_str(values: Mapping[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise ThalovantAPIError(f"Hub resource is missing {key}.")
    return value


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _clean_site_id(value: str) -> str:
    cleaned = "-".join(part for part in value.strip().replace("_", "-").split() if part)
    return cleaned or f"thalovant-client-{secrets.token_hex(4)}"


def _default_master(
    hub: Mapping[str, Any],
    endpoints: HubDataPlaneEndpoints,
    selected: SelectedHubEndpoint | None,
) -> str:
    if endpoints.https:
        return _strip_path(endpoints.https)
    domain = hub.get("domain")
    if isinstance(domain, str) and domain.strip():
        return endpoint_from_domain(domain, "https")
    if selected:
        return _strip_path(selected.endpoint)
    raise ThalovantAPIError("Hub resource does not expose a usable data-plane endpoint.")


def _strip_path(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if not parsed.scheme or not parsed.netloc:
        return endpoint.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


_ERROR_DETAIL_MAX_CHARS = 200
_PROBLEM_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,80}")


def _api_error(response: Any) -> ThalovantAPIError:
    """The error for a response the API answered with a failure status.

    The body is parsed once. When it is a JSON object it rides on the error
    whole, as ``problem``, with its ``code`` and its unshortened ``detail``
    read out of it; the message stays the bounded line it always was. The
    class says what kind of refusal it is -- all of them are
    :class:`ThalovantAPIError`.
    """

    try:
        body = response.json()
    except ValueError:
        body = None
    problem = body if isinstance(body, dict) else None
    message = _error_message(response.status_code, body)
    kind = _error_kind(response.status_code, problem)
    if kind is ThalovantAlreadyLinkedError:
        return ThalovantAlreadyLinkedError(
            message, status_code=response.status_code, problem=problem,
            client_id=_linked_client_id(problem),
        )
    return kind(message, status_code=response.status_code, problem=problem)


def _error_kind(status_code: int, problem: Mapping[str, Any] | None) -> type[ThalovantAPIError]:
    probe = ThalovantAPIError(problem=problem)
    code, detail = probe.code, probe.detail
    if status_code in (401, 423) or (status_code == 403 and detail == "Insufficient scopes"):
        # A token that is unknown, expired or revoked; an account locked; or a
        # token without the scope: signing in again is the way out of each.
        return ThalovantAuthError
    if status_code == 402 or (status_code == 403 and code == "plan_limit"):
        return ThalovantPlanError
    if status_code == 409 and code == "home_assistant_already_linked":
        return ThalovantAlreadyLinkedError
    return ThalovantAPIError


def _linked_client_id(problem: Mapping[str, Any] | None) -> str | None:
    if not problem:
        return None
    detail = problem.get("detail")
    nested: Mapping[str, Any] = detail if isinstance(detail, Mapping) else {}
    for source in (problem, nested):
        for key in ("client_id", "existing_client_id", "connection_id"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _refuses_connection_type(error: ThalovantAPIError) -> bool:
    """A 422 whose problem is about ``connection_type``.

    Read from the problem's ``detail`` and ``code``, and from each validation
    error's ``loc`` and ``msg`` -- never from the rest of the body. A
    validation error echoes what was sent as ``input``, and the request
    always carries ``spec.connection_type``, so a 422 about any other field
    would otherwise read as "this kind is not supported".
    """
    if error.status_code != 422:
        return False
    said: list[str] = [text for text in (error.detail, error.code) if isinstance(text, str)]
    problem = error.problem or {}
    for key in ("errors", "detail"):
        entries = problem.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            location = entry.get("loc")
            if isinstance(location, (list, tuple)):
                said.append(".".join(str(part) for part in location))
            elif isinstance(location, str):
                said.append(location)
            if isinstance(entry.get("msg"), str):
                said.append(entry["msg"])
    return any("connection_type" in text or "connectionType" in text for text in said)


def _retry_after_seconds(problem: Mapping[str, Any] | None) -> float | None:
    """The ``retry_after_seconds`` of a 429, at the top of the problem or in a detail object.

    The API sends it inside ``detail`` (its 429s are FastAPI's envelope around
    a structured refusal), as the ``api-errors`` contract reads ``code``.
    """
    if not isinstance(problem, Mapping):
        return None
    nested = problem.get("detail")
    for source in (problem, nested if isinstance(nested, Mapping) else {}):
        value = source.get("retry_after_seconds")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value)
    return None


def json_dumps(value: Any) -> str:
    import json

    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def _operation_or_none(value: Any) -> OperationResource | None:
    if isinstance(value, OperationResource):
        return value
    if isinstance(value, Mapping):
        try:
            return OperationResource.from_dict(value)
        except ThalovantAPIError:
            return None
    return None


def _operation_id(operation: OperationResource | Mapping[str, Any] | str) -> str:
    if isinstance(operation, OperationResource):
        return quote(operation.id, safe="")
    if isinstance(operation, Mapping):
        value = operation.get("id")
        if isinstance(value, str) and value:
            return quote(value, safe="")
        links = operation.get("links")
        link = links.get("self") if isinstance(links, Mapping) else None
        text = str(link or "").strip()
    else:
        text = str(operation or "").strip()
    if "/v1/operations/" in text:
        text = text.rsplit("/v1/operations/", 1)[1].split("?", 1)[0].strip("/")
    if not text:
        raise ThalovantAPIError("An operation needs an id to wait on.")
    return quote(text, safe="")


def _safe_browser_url(value: Any) -> bool:
    try:
        parsed = urlsplit(value) if isinstance(value, str) else None
        return (
            parsed is not None and parsed.scheme in {"http", "https"}
            and bool(parsed.hostname) and "@" not in parsed.netloc
            and not any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
        )
    except ValueError:
        return False


def _present_device_login(
    grant: DeviceAuthorization,
    *,
    prompt: Callable[[dict[str, Any]], None] | None,
    open_browser: bool,
) -> None:
    if prompt is not None:
        prompt(grant.as_dict())
    else:
        print(f"To sign in, visit {grant.verification_uri} and enter the code {grant.user_code}")
    if open_browser and grant.verification_uri_complete:
        try:
            webbrowser.open(grant.verification_uri_complete)
        except Exception:  # noqa: BLE001 - browser availability is best-effort
            pass


def _awaitable_sleep(sleep: Callable[[float], Any]) -> Callable[[float], Awaitable[Any]]:
    """The async form of a sleep a synchronous caller passed in."""
    if sleep is time.sleep:
        return asyncio.sleep

    async def pause(seconds: float) -> None:
        sleep(seconds)

    return pause


def _is_aiohttp_session(session: Any) -> bool:
    if session is None:
        return False
    try:
        import aiohttp
    except ImportError:  # pragma: no cover - aiohttp is a core dependency
        return False
    return isinstance(session, aiohttp.ClientSession)


class _Response:
    """What the API answered, whichever HTTP library carried it."""

    def __init__(self, status_code: int, text: str, headers: Mapping[str, str]) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers

    def json(self) -> Any:
        import json

        return json.loads(self.text)


class _AiohttpSender:
    """Sends through aiohttp: the caller's session, or one the SDK opens."""

    def __init__(self, session: Any = None) -> None:
        self._external = session
        self._own: Any = None
        #: Sent with every request, as a requests session's headers were.
        self.headers: dict[str, str] = {}

    def default_headers(self) -> Mapping[str, str]:
        headers = dict(getattr(self._external, "headers", None) or {}) if self._external is not None else {}
        headers.update(self.headers)
        return headers

    def carries_credentials(self) -> bool:
        session = self._external
        if session is None:
            return False
        jar = getattr(session, "cookie_jar", None)
        try:
            has_cookies = bool(jar is not None and len(jar))
        except TypeError:
            has_cookies = False
        return bool(getattr(session, "auth", None)) or has_cookies

    def _session(self) -> Any:
        if self._external is not None:
            return self._external
        if self._own is None or self._own.closed:
            import aiohttp

            self._own = _aiohttp.new_session(cookie_jar=aiohttp.DummyCookieJar())
        return self._own

    async def send(
        self,
        method: str,
        url: str,
        *,
        json: Mapping[str, Any] | None,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str],
        timeout: float,
        anonymous_plaintext: bool,
    ) -> _Response:
        import aiohttp

        merged = {**self.headers, **headers}
        options: dict[str, Any] = {}
        if self._external is None:
            options["proxy"] = _aiohttp.proxy_for(url)
        try:
            async with self._session().request(
                method,
                url,
                json=dict(json) if json is not None else None,
                params=_query(params),
                headers=merged,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=timeout),
                **options,
            ) as response:
                text = await response.text()
                return _Response(response.status, text, response.headers)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as error:
            # Error chains may contain URL credentials or query data.
            raise _unreachable(error) from None

    async def close(self) -> None:
        session, self._own = self._own, None
        if session is not None and not session.closed:
            await session.close()


class _BlockingSender:
    """Sends through a requests-style session a caller handed in, as 0.8 did."""

    def __init__(self, session: Any) -> None:
        self.session = session

    def default_headers(self) -> Mapping[str, str]:
        return dict(getattr(self.session, "headers", {}) or {})

    def carries_credentials(self) -> bool:
        return bool(getattr(self.session, "auth", None)) or bool(getattr(self.session, "cookies", None))

    async def send(
        self,
        method: str,
        url: str,
        *,
        json: Mapping[str, Any] | None,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str],
        timeout: float,
        anonymous_plaintext: bool,
    ) -> Any:
        try:
            return self.session.request(
                method,
                url,
                json=json,
                params=params,
                headers=dict(headers),
                timeout=timeout,
                allow_redirects=False,
                # An anonymous plaintext public request must not silently load
                # credentials from netrc during Requests.prepare_request().
                auth=(lambda prepared: prepared) if anonymous_plaintext else None,
            )
        except Exception as error:
            if type(error).__module__.split(".", 1)[0] in {"requests", "urllib3"} or isinstance(error, OSError):
                # Requests error chains may contain URL credentials or query data.
                raise _unreachable(error) from None
            raise

    async def close(self) -> None:
        return None


def _unreachable(error: BaseException) -> ThalovantAPIError:
    """The error for a request the API never answered, with the same message 0.8 used.

    A request that could not even be formed -- a malformed URL, a body that is
    not JSON; aiohttp's and requests' URL errors are ``ValueError``s -- stays a
    plain :class:`ThalovantAPIError`: it is not the API being out of reach,
    and trying again will not help.
    """
    kind = ThalovantAPIError if isinstance(error, ValueError) else ThalovantAPIUnreachableError
    return kind("Could not reach the Thalovant API.")


def _sender_for(session: Any) -> _AiohttpSender | _BlockingSender:
    if session is None or _is_aiohttp_session(session):
        return _AiohttpSender(session)
    return _BlockingSender(session)


def _query(params: Mapping[str, Any] | None) -> dict[str, str] | None:
    """Query parameters as aiohttp takes them: strings, booleans spelt out."""
    if params is None:
        return None
    query: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            query[str(key)] = "true" if value else "false"
        else:
            query[str(key)] = str(value)
    return query


class _SessionHandle:
    """``ThalovantControlPlane.session`` when the SDK owns the session."""

    def __init__(self, owner: ThalovantControlPlane) -> None:
        self._owner = weakref.ref(owner)

    @property
    def headers(self) -> dict[str, str]:
        owner = self._owner()
        sender = owner._core._sender if owner is not None else None
        return sender.headers if isinstance(sender, _AiohttpSender) else {}

    def close(self) -> None:
        owner = self._owner()
        if owner is not None:
            owner.close()


def _error_message(status_code: int, body: Any) -> str:
    """Build a bounded error message that never includes the raw response body.

    Error bodies can echo the request back (for ``POST /v1/clients`` that
    request carries freshly generated credentials) and are attacker-sized, so
    only a short server-provided ``detail``/``message``/``error`` *string* is
    kept, newline-collapsed and truncated; never the whole body. What the API
    said in full is on the error itself (see :func:`_api_error`).
    """

    detail: str | None = None
    if isinstance(body, dict):
        for key in ("detail", "message", "error"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                detail = value
                break
    # RFC 7807 problem bodies carry a machine-readable ``code`` at the root
    # (for example ``skill_version_already_installed``); keep it, once, so a
    # caller can branch on it without parsing the prose.
    code = body.get("code") if isinstance(body, dict) else None
    if not isinstance(code, str) or not _PROBLEM_CODE.fullmatch(code):
        code = None
    if detail is None:
        if code is None:
            return f"Thalovant API request failed with HTTP {status_code}."
        return f"Thalovant API request failed with HTTP {status_code}: ({code})"
    detail = " ".join(detail.split())
    if len(detail) > _ERROR_DETAIL_MAX_CHARS:
        detail = detail[:_ERROR_DETAIL_MAX_CHARS] + "..."
    if code is not None and code != detail:
        detail = f"{detail} ({code})"
    return f"Thalovant API request failed with HTTP {status_code}: {detail}"
