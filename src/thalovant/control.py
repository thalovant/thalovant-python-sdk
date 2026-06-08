"""Thalovant control-plane helpers."""

from __future__ import annotations

from dataclasses import dataclass
import secrets
from typing import Any, Iterable, Mapping
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

import requests

from .errors import ThalovantAPIError, ThalovantUnsupportedProtocolError
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

DEFAULT_CONTROL_USER_AGENT = "ThalovantPythonSDK/0.4.3"


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
        api_url: str,
        *,
        access_token: str | None = None,
        timeout: float = 10.0,
        user_agent: str = DEFAULT_CONTROL_USER_AGENT,
        session: requests.Session | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/") + "/"
        self.access_token = access_token
        self.timeout = timeout
        self.user_agent = user_agent
        self.session = session or requests.Session()

    def login(self, email: str, password: str, *, scope: str | None = None) -> dict[str, Any]:
        """Authenticate with email/password and store the returned access token."""

        payload: dict[str, Any] = {"email": email, "password": password}
        if scope:
            payload["scope"] = scope
        token = self._request("POST", "/v1/auth/token", json=payload, auth=False)
        access_token = token.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ThalovantAPIError("Thalovant API token response did not include access_token.")
        self.access_token = access_token
        return token

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

    def get_hub(self, hub_id: str) -> dict[str, Any]:
        """Fetch one hub resource."""

        return self._request("GET", f"/v1/hubs/{hub_id}")

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
        protocol: HubProtocol = "https",
    ) -> SelectedHubEndpoint:
        """Validate that a bootstrap result can be used by the current SDK runtime."""

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
            response = self.session.request(
                method,
                urljoin(self.api_url, path.lstrip("/")),
                json=json,
                params=params,
                headers=request_headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ThalovantAPIError("Could not reach the Thalovant API.") from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise ThalovantAPIError(_error_detail(response))
        try:
            body = response.json()
        except ValueError as exc:
            raise ThalovantAPIError("Thalovant API returned a non-JSON response.") from exc
        if not isinstance(body, dict):
            raise ThalovantAPIError("Thalovant API returned an unexpected response shape.")
        return body


def _new_secret() -> str:
    return secrets.token_urlsafe(32)


def _required_str(values: Mapping[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise ThalovantAPIError(f"Hub resource is missing {key}.")
    return value


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
