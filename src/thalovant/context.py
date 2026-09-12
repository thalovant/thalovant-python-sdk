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


def build_location(
    city: str = "",
    region: str = "",
    country: str = "",
    latitude: float | str | None = None,
    longitude: float | str | None = None,
    timezone: str = "",
) -> dict[str, Any] | None:
    """Where the caller is, in the shape the hub's skills read.

    Sent at the request level (``ask(location=...)``) it outranks the hub's own
    configured place, where a session-carried one ranks below it: ovos-bus-client
    fills session location from whichever process built the session, and a
    stock default arrives that way. Seen in the field without it: a bare
    weather question answered for the hub's city, and a named place going up
    with no hint and coming back HTTP 409 ambiguous.

    ``None`` when there is nothing worth sending -- a location needs a city. A
    zero coordinate is not a position, it is the Atlantic, so ``0, 0`` and
    anything outside the globe are left out rather than sent.
    """
    city = (city or "").strip()
    if not city:
        return None
    location: dict[str, Any] = {"city": city}
    if (region or "").strip():
        location["region"] = region.strip()
    if (country or "").strip():
        location["country_code"] = country.strip().upper()
    if (timezone or "").strip():
        location["timezone"] = {"code": timezone.strip()}
    try:
        lat, lon = float(latitude), float(longitude)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return location
    if (lat or lon) and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
        location["coordinate"] = {"latitude": lat, "longitude": lon}
    return location


def request_context(
    context: Mapping[str, Any] | None = None,
    *,
    stt_lang: str | None = None,
    pipeline: Sequence[str] | None = None,
    location: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Per-request hints a hub reads, merged into ``context``.

    ``stt_lang`` is the language a recogniser decided on by transcribing in it
    and scoring better than the other model did. ovos-core reads three hints in
    priority order -- ``stt_lang``, ``request_lang``, ``detected_lang`` -- and
    validates the value against the hub's own languages, so this is a request,
    not an override. ``pipeline`` names the intent stages the hub should run,
    in order, under ``session``; left out, the hub decides, which is the right
    default for anything that is not measured. ``location`` is what
    ``build_location()`` returns. ``None`` when there is nothing to send.
    """
    result = dict(context or {})
    if isinstance(result.get("session"), Mapping):
        result["session"] = dict(result["session"])
    if pipeline:
        stages = [str(stage).strip() for stage in pipeline if str(stage).strip()]
        if stages:
            session = dict(result.get("session") or {})
            session["pipeline"] = stages
            result["session"] = session
    if stt_lang and stt_lang.strip():
        result["stt_lang"] = stt_lang.strip()
    if location:
        result["location"] = dict(location)
    return result or None
