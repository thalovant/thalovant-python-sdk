"""Identity loading for Thalovant HiveMind clients."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .errors import ThalovantIdentityError
from .protocols import HubDataPlaneEndpoints, HubProtocol, HubProtocolSettings


_MISSING = object()


@dataclass(frozen=True)
class ThalovantIdentity:
    """HiveMind identity material provisioned by Thalovant."""

    access_key: str
    password: str
    default_master: str
    site_id: str
    default_port: int = 5679
    default_path: str = ""
    crypto_key: str | None = None
    data_plane_endpoints: HubDataPlaneEndpoints = field(
        default_factory=HubDataPlaneEndpoints
    )
    protocols: HubProtocolSettings = field(default_factory=HubProtocolSettings)

    @classmethod
    def from_file(cls, path: str | Path) -> "ThalovantIdentity":
        """Load identity material from a JSON file."""

        identity_path = Path(path).expanduser()
        try:
            raw = json.loads(identity_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ThalovantIdentityError(
                f"Unable to read identity file: {identity_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ThalovantIdentityError(
                f"Identity file is not valid JSON: {identity_path}"
            ) from exc

        if not isinstance(raw, Mapping):
            raise ThalovantIdentityError("Identity file must contain a JSON object.")
        return cls.from_mapping(raw)

    @classmethod
    def from_env(cls, prefix: str = "THALOVANT_") -> "ThalovantIdentity":
        """Load identity material from environment variables."""

        env = os.environ
        return cls.from_mapping(
            {
                "access_key": env.get(f"{prefix}ACCESS_KEY"),
                "password": env.get(f"{prefix}PASSWORD"),
                "crypto_key": env.get(f"{prefix}CRYPTO_KEY"),
                "site_id": env.get(f"{prefix}SITE_ID"),
                "default_master": env.get(f"{prefix}HUB_HTTP_HOST")
                or env.get(f"{prefix}DEFAULT_MASTER"),
                "default_port": env.get(f"{prefix}HUB_HTTP_PORT")
                or env.get(f"{prefix}DEFAULT_PORT"),
                "default_path": env.get(f"{prefix}HUB_HTTP_PATH")
                or env.get(f"{prefix}DEFAULT_PATH"),
                "data_plane_endpoints": {
                    "https": env.get(f"{prefix}HUB_HTTPS_HOST")
                    or env.get(f"{prefix}HUB_HTTP_HOST"),
                    "wss": env.get(f"{prefix}HUB_WSS_HOST")
                    or env.get(f"{prefix}HUB_WEBSOCKET_HOST"),
                    "mqtt": env.get(f"{prefix}HUB_MQTT_HOST"),
                },
            }
        )

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "ThalovantIdentity":
        """Normalize identity material from a Thalovant/HiveMind mapping."""

        access_key = _required_string(values, "access_key", aliases=("key", "api_key"))
        password = _required_string(values, "password")
        default_master = _required_string(
            values,
            "default_master",
            aliases=("host", "hub_http_host", "master"),
        )
        site_id = _required_string(values, "site_id", aliases=("siteId", "site"))
        default_port = _int_value(
            values, "default_port", aliases=("port", "hub_http_port")
        )
        default_path = _optional_string(
            values,
            "default_path",
            aliases=("defaultPath", "hub_http_path", "path", "uri_path"),
        )
        crypto_key = _optional_string(values, "crypto_key", aliases=("cryptoKey",))

        return cls(
            access_key=access_key,
            password=password,
            default_master=default_master.rstrip("/"),
            default_port=default_port or 5679,
            default_path=_normalize_path(default_path),
            site_id=site_id,
            crypto_key=crypto_key,
            data_plane_endpoints=HubDataPlaneEndpoints.from_mapping(values),
            protocols=HubProtocolSettings.from_mapping(values),
        )

    def endpoint_base(self) -> str:
        """Return the HTTPS HTTP-protocol endpoint base used by SDK transports."""

        return self.data_plane_endpoints.http_base(
            self.default_master,
            self.default_port,
            self.default_path,
        )

    def endpoint_for(self, protocol: HubProtocol) -> str | None:
        """Return a public data-plane endpoint for a protocol when known."""

        if protocol == "https":
            return self.endpoint_base()
        return self.data_plane_endpoints.endpoint_for(protocol)

    def enabled_protocols(self) -> tuple[HubProtocol, ...]:
        """Return enabled hub protocols in preferred client order."""

        return self.protocols.enabled_protocols()

    def supports_protocol(self, protocol: HubProtocol) -> bool:
        """Return whether the identity says a protocol is enabled."""

        return self.protocols.is_enabled(protocol)

    def as_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        """Return a serializable identity summary."""

        data: dict[str, Any] = {
            "site_id": self.site_id,
            "default_master": self.default_master,
            "default_port": self.default_port,
            "default_path": self.default_path,
        }
        endpoints = self.data_plane_endpoints.as_dict(
            redact_credentials=not include_secrets
        )
        if endpoints:
            data["data_plane_endpoints"] = endpoints
        if include_secrets:
            data.update(
                {
                    "access_key": self.access_key,
                    "password": self.password,
                    "crypto_key": self.crypto_key,
                }
            )
        return data


def _value(values: Mapping[str, Any], key: str, aliases: tuple[str, ...] = ()) -> Any:
    for candidate in (key, *aliases):
        value = values.get(candidate, _MISSING)
        if value is not _MISSING:
            return value
    return _MISSING


def _required_string(
    values: Mapping[str, Any], key: str, aliases: tuple[str, ...] = ()
) -> str:
    value = _optional_string(values, key, aliases=aliases)
    if value is None:
        accepted = ", ".join((key, *aliases))
        raise ThalovantIdentityError(f"Missing required identity field: {accepted}")
    return value


def _optional_string(
    values: Mapping[str, Any],
    key: str,
    aliases: tuple[str, ...] = (),
) -> str | None:
    value = _value(values, key, aliases)
    if value is _MISSING or value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _int_value(
    values: Mapping[str, Any], key: str, aliases: tuple[str, ...] = ()
) -> int | None:
    value = _value(values, key, aliases)
    if value is _MISSING or value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        accepted = ", ".join((key, *aliases))
        raise ThalovantIdentityError(
            f"Identity field must be an integer: {accepted}"
        ) from exc


def _normalize_path(path: str | None) -> str:
    if not path:
        return ""
    normalized = "/" + path.strip("/")
    return "" if normalized == "/" else normalized
