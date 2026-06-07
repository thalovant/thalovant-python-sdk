"""Protocol and endpoint helpers for Thalovant hubs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit, urlunsplit

HubProtocol = Literal["wss", "https", "mqtt"]


@dataclass(frozen=True)
class HubProtocolSettings:
    """Enabled public protocol paths for a hub."""

    wss: bool = True
    http: bool = False
    mqtt: bool = False

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "HubProtocolSettings":
        """Read protocol flags from a hub spec, hub resource, or identity payload."""

        if not isinstance(values, Mapping):
            return cls()

        spec = _mapping_value(values, "spec")
        source = spec if spec is not None else values
        protocols = _mapping_value(source, "protocols") or {}
        network = _mapping_value(source, "network") or {}

        return cls(
            wss=_enabled_value(
                (
                    _first_value(protocols, "wss", "websocket")
                    if protocols
                    else _first_value(network, "wss", "websocket")
                ),
                True,
            ),
            http=_enabled_value(
                (
                    _first_value(protocols, "http", "https")
                    if protocols
                    else _first_value(network, "http", "https")
                ),
                False,
            ),
            mqtt=_enabled_value(
                (
                    _first_value(protocols, "mqtt")
                    if protocols
                    else _first_value(network, "mqtt")
                ),
                False,
            ),
        )

    @property
    def https(self) -> bool:
        """Whether the HTTP protocol is exposed as HTTPS at the public edge."""

        return self.http

    def enabled_protocols(self) -> tuple[HubProtocol, ...]:
        """Return enabled client-facing protocols in preferred display order."""

        enabled: list[HubProtocol] = []
        if self.wss:
            enabled.append("wss")
        if self.http:
            enabled.append("https")
        if self.mqtt:
            enabled.append("mqtt")
        return tuple(enabled)

    def is_enabled(self, protocol: HubProtocol) -> bool:
        """Return whether a client-facing protocol is enabled."""

        if protocol == "wss":
            return self.wss
        if protocol == "https":
            return self.http
        if protocol == "mqtt":
            return self.mqtt
        return False

    def as_dict(self) -> dict[str, dict[str, bool]]:
        """Return the hub spec representation."""

        return {
            "wss": {"enabled": self.wss},
            "http": {"enabled": self.http},
            "mqtt": {"enabled": self.mqtt},
        }


@dataclass(frozen=True)
class HubDataPlaneEndpoints:
    """Public data-plane endpoints returned by the Thalovant API."""

    https: str | None = None
    wss: str | None = None
    mqtt: str | None = None

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "HubDataPlaneEndpoints":
        """Read endpoints from a hub resource, identity payload, or endpoint map."""

        if not isinstance(values, Mapping):
            return cls()

        source = (
            _mapping_value(values, "data_plane_endpoints")
            or _mapping_value(values, "dataPlaneEndpoints")
            or _mapping_value(values, "endpoints")
            or values
        )
        return cls(
            https=_normalize_endpoint(_first_value(source, "https", "http")),
            wss=_normalize_endpoint(_first_value(source, "wss", "ws")),
            mqtt=_normalize_endpoint(_first_value(source, "mqtt", "mqtts")),
        )

    @classmethod
    def from_hub(cls, hub: Mapping[str, Any]) -> "HubDataPlaneEndpoints":
        """Build endpoints from an API hub resource, falling back to hub domain."""

        endpoints = cls.from_mapping(hub)
        settings = HubProtocolSettings.from_mapping(hub)
        domain = _optional_string(hub.get("domain"))
        if not domain:
            return endpoints
        return cls(
            https=endpoints.https
            or (endpoint_from_domain(domain, "https") if settings.http else None),
            wss=endpoints.wss
            or (endpoint_from_domain(domain, "wss") if settings.wss else None),
            mqtt=endpoints.mqtt,
        )

    def endpoint_for(self, protocol: HubProtocol) -> str | None:
        """Return the endpoint for a client-facing protocol."""

        if protocol == "https":
            return self.https
        if protocol == "wss":
            return self.wss
        if protocol == "mqtt":
            return self.mqtt
        return None

    def http_base(
        self, fallback_master: str, fallback_port: int, fallback_path: str
    ) -> str:
        """Return the HTTPS HTTP-protocol endpoint base used by SDK transports."""

        if self.https:
            return endpoint_base(self.https, fallback_port, "")
        master = _coerce_scheme(
            fallback_master, {"wss://": "https://", "ws://": "http://"}
        )
        return endpoint_base(master, fallback_port, fallback_path)

    def as_dict(self, *, redact_credentials: bool = False) -> dict[str, str]:
        """Return populated endpoint values."""

        data = {
            "https": self.https,
            "wss": self.wss,
            "mqtt": self.mqtt,
        }
        return {
            key: _redact_credentials(value) if redact_credentials else value
            for key, value in data.items()
            if value
        }


def endpoint_from_domain(domain: str, protocol: HubProtocol) -> str:
    """Derive a public endpoint URL from a hub domain."""

    normalized = domain.strip().rstrip("/")
    if protocol == "wss":
        if normalized.startswith(("wss://", "ws://")):
            return _normalize_endpoint(normalized) or ""
        if normalized.startswith(("https://", "http://")):
            return (
                _normalize_endpoint(
                    _coerce_scheme(
                        normalized, {"https://": "wss://", "http://": "wss://"}
                    )
                )
                or ""
            )
        return _normalize_endpoint(f"wss://{normalized}") or ""
    if protocol == "https":
        if normalized.startswith(("https://", "http://")):
            return (
                _normalize_endpoint(_coerce_scheme(normalized, {"http://": "https://"}))
                or ""
            )
        if normalized.startswith(("wss://", "ws://")):
            return (
                _normalize_endpoint(
                    _coerce_scheme(
                        normalized, {"wss://": "https://", "ws://": "https://"}
                    )
                )
                or ""
            )
        return _normalize_endpoint(f"https://{normalized}") or ""
    return ""


def endpoint_base(master: str, default_port: int, default_path: str) -> str:
    """Return an HTTP endpoint base URL including port and optional path."""

    parsed = urlsplit(master)
    if parsed.scheme and parsed.netloc:
        netloc = parsed.netloc
        if ":" not in netloc.rsplit("@", 1)[-1]:
            netloc = f"{netloc}:{default_port}"
        path = "/".join(
            part.strip("/")
            for part in (parsed.path, default_path)
            if part and part.strip("/")
        )
        return urlunsplit((parsed.scheme, netloc, f"/{path}" if path else "", "", ""))
    return f"{master.rstrip('/')}:{default_port}{default_path}"


def _mapping_value(values: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = values.get(key)
    return value if isinstance(value, Mapping) else None


def _first_value(values: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in values:
            return values[key]
    return None


def _enabled_value(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, Mapping):
        return _enabled_value(value.get("enabled"), fallback)
    return fallback


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _normalize_endpoint(value: Any) -> str | None:
    raw = _optional_string(value)
    if not raw:
        return None
    parsed = urlsplit(raw)
    if parsed.scheme and parsed.scheme not in {
        "http",
        "https",
        "ws",
        "wss",
        "mqtt",
        "mqtts",
    }:
        return None
    if parsed.scheme and not parsed.netloc:
        return None
    return raw.rstrip("/")


def _coerce_scheme(value: str, replacements: Mapping[str, str]) -> str:
    for prefix, replacement in replacements.items():
        if value.startswith(prefix):
            return replacement + value[len(prefix) :]
    return value


def _redact_credentials(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    if not parsed.username and not parsed.password:
        return value
    host = parsed.hostname or parsed.netloc
    if parsed.port is not None and parsed.hostname:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
