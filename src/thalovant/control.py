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

DEFAULT_CONTROL_API_URL = "https://api.thalovant.com"
DEFAULT_CONTROL_USER_AGENT = "ThalovantPythonSDK/0.4.22"

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


def _memory_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    for source, target in (
        ("ownerId", "owner_id"),
        ("hubId", "hub_id"),
        ("consentScope", "consent_scope"),
        ("consentVersion", "consent_version"),
        ("retentionPolicy", "retention_policy"),
        ("expiresAt", "expires_at"),
        ("clearExpiresAt", "clear_expires_at"),
    ):
        if source in data:
            data[target] = data.pop(source)
    return data


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
