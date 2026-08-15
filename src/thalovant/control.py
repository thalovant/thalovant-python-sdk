"""Thalovant control-plane helpers."""

from __future__ import annotations

from dataclasses import dataclass
import secrets
import time
from typing import Any, Callable, Iterable, Literal, Mapping, cast
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4
import webbrowser

import requests

from .errors import ThalovantAPIError, ThalovantTimeoutError, ThalovantUnsupportedProtocolError
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

DEFAULT_CONTROL_API_URL = "https://api.thalovant.com"
DEFAULT_CONTROL_USER_AGENT = USER_AGENT

DEFAULT_DEVICE_POLL_INTERVAL = 5.0

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


@dataclass(frozen=True)
class BootstrapIdentityResult:
    """Result returned after provisioning a hub client identity through the API."""

    identity: ThalovantIdentity
    hub: dict[str, Any]
    client: dict[str, Any]
    endpoint: SelectedHubEndpoint | None

    @property
    def selected_protocol(self) -> HubProtocol | None:
        return self.endpoint.protocol if self.endpoint else None

    def as_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        """Return a serializable result, redacting identity secrets by default."""

        return {
            "identity": self.identity.as_dict(include_secrets=include_secrets),
            "hub": self.hub,
            "client": self.client,
            "selected_protocol": self.selected_protocol,
            "selected_endpoint": self.endpoint.endpoint if self.endpoint else None,
        }


class ThalovantControlPlane:
    """Small authenticated client for the Thalovant API."""

    def __init__(
        self,
        api_url: str = DEFAULT_CONTROL_API_URL,
        *,
        access_token: str | None = None,
        timeout: float = 10.0,
        user_agent: str = DEFAULT_CONTROL_USER_AGENT,
        session: requests.Session | None = None,
    ) -> None:
        self.api_url = _normalize_control_api_url(api_url)
        self.access_token = access_token
        self.timeout = timeout
        self.user_agent = user_agent
        self.session = session or requests.Session()

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

        payload: dict[str, Any] = {"email": email, "password": password}
        if scope:
            payload["scope"] = scope
        if otp_code:
            payload["otp_code"] = otp_code
        if recovery_code:
            payload["recovery_code"] = recovery_code
        token = self._request("POST", "/v1/auth/token", json=payload, auth=False)
        access_token = token.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ThalovantAPIError("Thalovant API token response did not include access_token.")
        self.access_token = access_token
        return token

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
        """

        payload: dict[str, Any] = {}
        if scopes is not None:
            payload["scopes"] = list(scopes)
        if client_name:
            payload["client_name"] = client_name
        grant = self._request("POST", "/v1/auth/device/authorize", json=payload, auth=False)

        device_code = grant.get("device_code")
        user_code = grant.get("user_code")
        verification_uri = grant.get("verification_uri")
        for value in (device_code, user_code, verification_uri):
            if not isinstance(value, str) or not value:
                raise ThalovantAPIError(
                    "Thalovant API device authorization response was incomplete."
                )
        raw_interval = grant.get("interval")
        interval = (
            float(raw_interval)
            if isinstance(raw_interval, (int, float)) and raw_interval >= 0
            else DEFAULT_DEVICE_POLL_INTERVAL
        )

        if prompt is not None:
            prompt(grant)
        else:
            print(f"To sign in, visit {verification_uri} and enter the code {user_code}")
        if open_browser:
            complete_uri = grant.get("verification_uri_complete")
            if isinstance(complete_uri, str) and complete_uri:
                try:
                    webbrowser.open(complete_uri)
                except Exception:  # noqa: BLE001 - browser availability is best-effort
                    pass

        token = self._poll_device_token(
            cast(str, device_code), interval=interval, timeout=timeout
        )
        access_token = token.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ThalovantAPIError("Thalovant API token response did not include access_token.")
        self.access_token = access_token
        return token

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

        deadline = clock() + timeout
        wait = interval
        while True:
            response = self._send(
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
                        "Thalovant API returned an unexpected response shape."
                    )
                return body
            error = (
                body.get("error")
                if response.status_code == 400 and isinstance(body, dict)
                else None
            )
            if error == "slow_down":
                wait += 5.0
            elif error == "access_denied":
                raise ThalovantAPIError(
                    "The device sign-in request was denied in the browser."
                )
            elif error == "expired_token":
                raise ThalovantAPIError(
                    "The device sign-in code expired before it was approved. "
                    "Call login_with_browser() again to request a new code."
                )
            elif error != "authorization_pending":
                raise ThalovantAPIError(_error_detail(response))
            remaining = deadline - clock()
            if remaining <= 0:
                raise ThalovantTimeoutError(
                    "Timed out waiting for the device sign-in to be approved."
                )
            sleep(min(wait, remaining))

    def list_hubs(
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
        return self._request("GET", "/v1/hubs", params=params)

    def list_public_hubs(
        self,
        *,
        limit: int = 24,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List public, active hubs available for discovery without API auth."""

        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/v1/public/hubs", params=params, auth=False)

    def get_operation(self, operation_id: str) -> OperationResource:
        """Read durable progress for an operation accepted by the API."""

        payload = self._request("GET", f"/v1/operations/{operation_id}")
        return OperationResource.from_dict(payload)

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
        return self._request("GET", "/v1/memory", params=params)

    def get_memory_summary(self, *, owner_id: str | None = None) -> dict[str, Any]:
        """Summarize durable memory by scope and kind."""

        params: dict[str, Any] = {}
        _set_param(params, "owner_id", owner_id)
        return self._request("GET", "/v1/memory/summary", params=params)

    def create_memory_item(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Create a durable memory item."""

        return self._request("POST", "/v1/memory", json=_memory_payload(payload))

    def get_memory_item(self, memory_id: str) -> dict[str, Any]:
        """Fetch one durable memory item by id."""

        return self._request("GET", f"/v1/memory/{memory_id}")

    def update_memory_item(self, memory_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Update a durable memory item."""

        return self._request("PATCH", f"/v1/memory/{memory_id}", json=_memory_payload(payload))

    def delete_memory_item(self, memory_id: str) -> None:
        """Soft-delete a durable memory item."""

        self._request("DELETE", f"/v1/memory/{memory_id}")

    def get_analytics_overview(
        self,
        *,
        admin: bool = False,
        range: str | None = None,
        bucket: str | None = None,
        owner_id: str | None = None,
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
        """Fetch the workspace or admin analytics overview used by the dashboard."""

        params: dict[str, Any] = {}
        _set_param(params, "range", range)
        _set_param(params, "bucket", bucket)
        if admin:
            _set_param(params, "owner_id", owner_id)
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
        endpoint = "/v1/admin/analytics/overview" if admin else "/v1/analytics/overview"
        return self._request("GET", endpoint, params=params)

    def get_hub(self, hub_id: str) -> dict[str, Any]:
        """Fetch one hub resource."""

        return self._request("GET", f"/v1/hubs/{hub_id}")

    def get_public_hub(self, hub_ref: str) -> dict[str, Any]:
        """Fetch one public hub by slug or id without API auth."""

        return self._request("GET", f"/v1/public/hubs/{hub_ref}", auth=False)

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

        The request is idempotent: a generated ``Idempotency-Key`` is sent
        unless you pass your own, so a retried create returns the first hub
        instead of making a second one.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """

        headers = {"Idempotency-Key": idempotency_key or str(uuid4())}
        return self._request("POST", "/v1/hubs", json=_hub_payload(payload), headers=headers)

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

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """

        return self._request(
            "PATCH",
            f"/v1/hubs/{hub_id}",
            json=_hub_payload(payload),
            headers={"If-Match": etag},
        )

    def delete_hub(self, hub_id: str, *, etag: str) -> None:
        """Delete a hub and its dependent clients and ACLs.

        Like :meth:`update_hub` this route requires the hub's current ``etag``,
        sent as ``If-Match``; a stale value fails with HTTP 412.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """

        self._request("DELETE", f"/v1/hubs/{hub_id}", headers={"If-Match": etag})

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
        unless you also pass ``mode``.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """

        return self._request(
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

    def set_hub_rating(self, hub_id: str, rating: int) -> dict[str, Any]:
        """Rate a public hub from 1 to 5 and return the updated hub.

        Only public hubs can be rated, and owners cannot rate their own hubs.
        Requires a token with the ``hubs:write`` scope; no paid plan is needed.
        """

        return self._request("PUT", f"/v1/hubs/{hub_id}/rating", json={"rating": rating})

    def clear_hub_rating(self, hub_id: str) -> dict[str, Any]:
        """Remove the caller's rating from a public hub and return the hub.

        Requires a token with the ``hubs:write`` scope; no paid plan is needed.
        """

        return self._request("DELETE", f"/v1/hubs/{hub_id}/rating")

    def get_hub_runtime_capabilities(self, hub_id: str) -> dict[str, Any]:
        """Read the live skill and intent inventory a hub runtime exposes.

        Requires a token with the ``hubs:inspect`` scope. The API answers HTTP
        409 when the hub has no connected client that can report inventory.
        """

        return self._request("GET", f"/v1/hubs/{hub_id}/runtime-capabilities")

    def list_runtime_groups(self, *, owner_id: str | None = None) -> dict[str, Any]:
        """List runtime groups visible to the authenticated user.

        Requires a token with the ``hubs:read`` scope.
        """

        params: dict[str, Any] = {}
        _set_param(params, "owner_id", owner_id)
        return self._request("GET", "/v1/runtime-groups", params=params)

    def get_runtime_group(self, runtime_group_id: str) -> dict[str, Any]:
        """Fetch one runtime group.

        Requires a token with the ``hubs:read`` scope.
        """

        return self._request("GET", f"/v1/runtime-groups/{runtime_group_id}")

    def create_runtime_group(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Create a runtime group.

        ``payload`` takes the API's create body: ``name`` is required, and
        ``description``, ``environment``, ``owner_id``, and
        ``clone_from_default`` are optional. camelCase keys are accepted and
        sent as snake_case.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """

        return self._request("POST", "/v1/runtime-groups", json=_runtime_group_payload(payload))

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

        return self._request(
            "PATCH",
            f"/v1/runtime-groups/{runtime_group_id}",
            json=_runtime_group_payload(payload),
        )

    def get_runtime_group_config(self, runtime_group_id: str) -> dict[str, Any]:
        """Read a runtime group's runtime configuration and personas.

        Requires a token with the ``hubs:read`` scope.
        """

        return self._request("GET", f"/v1/runtime-groups/{runtime_group_id}/config")

    def update_runtime_group_config(
        self,
        runtime_group_id: str,
        config: Mapping[str, Any],
        *,
        personas: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Merge runtime configuration into a runtime group.

        The API merges ``config`` into the stored configuration rather than
        replacing it, and marks the group pending so the runtime operator
        reconciles the change. ``personas`` is replaced only when provided.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """

        body: dict[str, Any] = {"config": dict(config)}
        if personas is not None:
            body["personas"] = dict(personas)
        return self._request(
            "PATCH",
            f"/v1/runtime-groups/{runtime_group_id}/config",
            json=body,
        )

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

        Options behave like :meth:`release_hub`.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """

        return self._request(
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

    def delete_runtime_group(self, runtime_group_id: str) -> None:
        """Delete a runtime group.

        The API answers HTTP 409 for the workspace default group and for a
        group that still has hubs attached.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """

        self._request("DELETE", f"/v1/runtime-groups/{runtime_group_id}")

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
        only; the API silently scopes a non-admin caller to their own tenant
        and to active entries. ``force_refresh`` re-syncs the global catalog
        from its source before answering, which is slower.

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
        return self._request("GET", "/v1/marketplace/skills", params=params)

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

        ``refresh_inventory`` forces a live read from the runtime operator
        instead of answering from the cached inventory snapshot.

        Requires a token with the ``hubs:inspect`` scope; no paid plan is
        needed to browse. The API answers HTTP 404 for an unknown group and
        HTTP 403 when the caller does not own it.
        """

        params: dict[str, Any] = {}
        if refresh_inventory:
            params["refresh_inventory"] = "true"
        return self._request(
            "GET",
            f"/v1/runtime-groups/{runtime_group_id}/marketplace",
            params=params,
        )

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
        ``operator_message``.

        ``refresh`` forces a live operator read; the API also refreshes on its
        own when it holds no cached snapshot. Unlike
        :meth:`get_hub_runtime_capabilities` this route does not answer HTTP
        409 when nothing is reporting -- it returns an empty ``data`` list
        with a pending ``source`` instead.

        Requires a token with the ``hubs:inspect`` scope; no paid plan is
        needed.
        """

        params: dict[str, Any] = {}
        if refresh:
            params["refresh"] = "true"
        return self._request(
            "GET",
            f"/v1/runtime-groups/{runtime_group_id}/inventory",
            params=params,
        )

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

        The default ``source_type`` of ``catalog`` installs a marketplace skill
        and requires the skill to exist in the catalog. ``git`` installs need a
        ``source_ref`` repository URL. Installing a skill that is already
        present updates the existing entry.

        Requires a paid plan and a token with the ``hubs:write`` scope. Paid
        marketplace skills also need marketplace access on the tenant plan.
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
        return self._request(
            "POST",
            f"/v1/runtime-groups/{runtime_group_id}/skills",
            json=body,
        )

    def uninstall_runtime_group_skill(self, runtime_group_id: str, skill_id: str) -> None:
        """Remove a skill from a runtime group.

        Requires a paid plan and a token with the ``hubs:write`` scope.
        """

        self._request("DELETE", f"/v1/runtime-groups/{runtime_group_id}/skills/{skill_id}")

    def create_client(self, payload: Mapping[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        """Create a hub client through the API."""

        headers = {"Idempotency-Key": idempotency_key or str(uuid4())}
        return self._request("POST", "/v1/clients", json=dict(payload), headers=headers)

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
    ) -> BootstrapIdentityResult:
        """Provision a client and return local identity secrets for direct hub access.

        The API stores client credentials in Vault and returns only references.
        This method therefore generates the secret material locally, sends it to
        the API once, and keeps the usable identity in the returned object.
        """

        hub_resource = self.get_hub(hub) if isinstance(hub, str) else dict(hub)
        hub_id = _required_str(hub_resource, "id")
        site = _clean_site_id(site_id or name)
        api_key = _new_secret()
        password = _new_secret()
        crypto_key = _new_secret()

        client_spec = dict(spec or {})
        client_spec.setdefault("version", "1")
        client_spec.update(
            {
                "apiKey": api_key,
                "password": password,
                "cryptoKey": crypto_key,
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

        client = self.create_client(payload, idempotency_key=idempotency_key)
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
                crypto_key=crypto_key,
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
        )

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
        response = self._send(method, path, json=json, params=params, headers=headers, auth=auth)
        if response.status_code < 200 or response.status_code >= 300:
            raise ThalovantAPIError(_error_detail(response))
        if not response.text.strip():
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            raise ThalovantAPIError("Thalovant API returned a non-JSON response.") from exc
        if not isinstance(body, dict):
            raise ThalovantAPIError("Thalovant API returned an unexpected response shape.")
        return body

    def _send(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
    ) -> requests.Response:
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

        try:
            return self.session.request(
                method,
                urljoin(self.api_url, path.lstrip("/")),
                json=json,
                params=params,
                headers=request_headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ThalovantAPIError("Could not reach the Thalovant API.") from exc


def _new_secret() -> str:
    return secrets.token_urlsafe(32)


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


def _error_detail(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = response.text
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("error") or body
    else:
        detail = body
    return f"Thalovant API request failed with HTTP {response.status_code}: {detail}"
