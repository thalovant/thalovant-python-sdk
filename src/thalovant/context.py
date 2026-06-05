"""Generic context helpers for user, auth, device, and channel metadata."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def build_client_context(
    context: Mapping[str, Any] | None = None,
    *,
    user_id: str | None = None,
    user_name: str | None = None,
    auth_token: str | None = None,
    auth_provider: str | None = None,
    auth_claims: Mapping[str, Any] | None = None,
    roles: Sequence[str] | None = None,
    platform: str | None = None,
    source: str | None = None,
    destination: str | None = None,
    channel: str | None = None,
    device_id: str | None = None,
    locale: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Build a portable HiveMind context for enterprise clients.

    The helper keeps provider-specific details out of the SDK surface. Use
    `auth_provider` for labels such as "oidc" or "keycloak", and pass any
    non-standard keys through `context` or `metadata`.
    """

    result = dict(context or {})

    if user_id or user_name or roles:
        user = dict(result.get("user") or {})
        if user_id:
            user["id"] = user_id
            result.setdefault("user_id", user_id)
        if user_name:
            user["name"] = user_name
            result.setdefault("user_name", user_name)
        if roles:
            user["roles"] = list(roles)
            result.setdefault("roles", list(roles))
        result["user"] = user

    if auth_token or auth_provider or auth_claims:
        auth = dict(result.get("auth") or {})
        if auth_token:
            auth["token"] = auth_token
            result.setdefault("auth_token", auth_token)
        if auth_provider:
            auth["provider"] = auth_provider
        if auth_claims:
            auth["claims"] = dict(auth_claims)
        result["auth"] = auth

    if platform:
        result.setdefault("platform", platform)
    if source:
        result.setdefault("source", source)
    if destination:
        result.setdefault("destination", destination)
    if channel:
        result.setdefault("channel", channel)
    if locale:
        result.setdefault("locale", locale)
    if device_id:
        device = dict(result.get("device") or {})
        device.setdefault("id", device_id)
        if platform:
            device.setdefault("platform", platform)
        result["device"] = device

    if metadata:
        result["metadata"] = {**dict(result.get("metadata") or {}), **dict(metadata)}

    if session_id:
        session = dict(result.get("session") or {})
        session.setdefault("session_id", session_id)
        result.setdefault("session_id", session_id)
        result["session"] = session

    return result
