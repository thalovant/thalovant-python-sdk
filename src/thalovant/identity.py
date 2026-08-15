"""Identity loading for Thalovant HiveMind clients."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

import yaml

from .errors import ThalovantIdentityError
from .protocols import (
    HubDataPlaneEndpoints,
    HubProtocol,
    HubProtocolSettings,
    _redact_credentials,
)


_MISSING = object()
DEFAULT_CONFIG_FILENAME = "config.yaml"


def default_config_path() -> Path:
    """Return the default per-user Thalovant SDK config path."""

    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home).expanduser() / "thalovant" / DEFAULT_CONFIG_FILENAME
    appdata = os.environ.get("APPDATA")
    if os.name == "nt" and appdata:
        return Path(appdata).expanduser() / "Thalovant" / DEFAULT_CONFIG_FILENAME
    return Path.home() / ".config" / "thalovant" / DEFAULT_CONFIG_FILENAME


@dataclass(frozen=True)
class MqttBrokerCredentials:
    """Per-client MQTT broker credentials returned by the Thalovant API.

    ``repr()`` hides the credential fields and the topic fields (topics can
    embed the client access key); serialization is unaffected — use
    ``as_dict(include_secrets=True)`` for the full material.
    """

    endpoint: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    topic_prefix: str | None = field(default=None, repr=False)
    qos: int = 1
    tls: bool = True

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "MqttBrokerCredentials | None":
        if not isinstance(values, Mapping):
            return None
        endpoint = _optional_string(values, "endpoint", aliases=("broker_url", "brokerUrl"))
        username = _optional_string(values, "username", aliases=("broker_username", "brokerUsername"))
        password = _optional_string(values, "password", aliases=("broker_password", "brokerPassword"))
        if not endpoint or not username or not password:
            return None
        topic_prefix = _optional_string(values, "topic_prefix", aliases=("topicPrefix",))
        return cls(
            endpoint=endpoint,
            username=username,
            password=password,
            topic_prefix=topic_prefix,
            qos=_qos_value(values, "qos", default=1),
            tls=_bool_value(values, "tls", default=endpoint.startswith("mqtts://")),
        )

    def __repr__(self) -> str:
        # Show only the non-secret fields, with any URL userinfo stripped from
        # the endpoint. The username/password/topic fields (which can embed the
        # access key) are omitted entirely.
        return (
            f"{type(self).__name__}("
            f"endpoint={_redact_credentials(self.endpoint)!r}, "
            f"qos={self.qos!r}, tls={self.tls!r})"
        )

    def as_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        # A broker endpoint URL may embed ``user:pass@`` userinfo; strip it from
        # the default (non-secret) output so it stays safe to log.
        endpoint = self.endpoint if include_secrets else _redact_credentials(self.endpoint)
        data: dict[str, Any] = {"endpoint": endpoint, "tls": self.tls}
        if include_secrets:
            data.update(
                {
                    "username": self.username,
                    "password": self.password,
                }
            )
            if self.topic_prefix:
                data["topic_prefix"] = self.topic_prefix
            if self.qos != 1:
                data["qos"] = self.qos
        return data


@dataclass(frozen=True)
class ThalovantIdentity:
    """HiveMind identity material provisioned by Thalovant.

    ``repr()`` hides the secret fields (``access_key``, ``password``,
    ``crypto_key``); serialization is unaffected — use
    ``as_dict(include_secrets=True)`` when persisting an identity file.
    """

    access_key: str = field(repr=False)
    password: str = field(repr=False)
    default_master: str
    site_id: str
    default_port: int = 5679
    default_path: str = ""
    crypto_key: str | None = field(default=None, repr=False)
    data_plane_endpoints: HubDataPlaneEndpoints = field(
        default_factory=HubDataPlaneEndpoints
    )
    protocols: HubProtocolSettings = field(default_factory=HubProtocolSettings)
    mqtt: MqttBrokerCredentials | None = None
    # metadata is arbitrary caller/API-supplied annotation and may carry
    # secret-keyed entries; keep it out of repr and redact it in the default
    # serializer (see as_dict).
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_file(cls, path: str | Path) -> "ThalovantIdentity":
        """Load identity material from a JSON file."""

        identity_path = Path(path).expanduser()
        _assert_secure_identity_file(identity_path)
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
    def from_config(
        cls,
        path: str | Path | None = None,
        *,
        profile: str | None = None,
    ) -> "ThalovantIdentity":
        """Load identity material from a protected YAML config file."""

        config_path = Path(path).expanduser() if path is not None else default_config_path()
        _assert_secure_config_file(config_path)
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise ThalovantIdentityError(
                f"Unable to read Thalovant config file: {config_path}"
            ) from exc
        except yaml.YAMLError as exc:
            raise ThalovantIdentityError(
                f"Thalovant config file is not valid YAML: {config_path}"
            ) from exc
        if not isinstance(raw, Mapping):
            raise ThalovantIdentityError("Thalovant config file must contain a YAML object.")
        return cls.from_mapping(_identity_config_mapping(raw, profile=profile))

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
                "mqtt": {
                    "endpoint": env.get(f"{prefix}MQTT_ENDPOINT")
                    or env.get(f"{prefix}HUB_MQTT_HOST"),
                    "username": env.get(f"{prefix}MQTT_USERNAME"),
                    "password": env.get(f"{prefix}MQTT_PASSWORD"),
                    "topic_prefix": env.get(f"{prefix}MQTT_TOPIC_PREFIX"),
                    "qos": env.get(f"{prefix}MQTT_QOS"),
                },
            }
        )

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "ThalovantIdentity":
        """Normalize identity material from a Thalovant/HiveMind mapping."""

        access_key = _required_string(values, "access_key", aliases=("key", "api_key", "apiKey"))
        password = _required_string(values, "password")
        default_master = _required_string(
            values,
            "default_master",
            aliases=("defaultMaster", "host", "hub_http_host", "hubHttpHost", "master"),
        )
        site_id = _required_string(
            values,
            "site_id",
            aliases=("siteId", "site", "client_id", "clientId"),
        )
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
            mqtt=MqttBrokerCredentials.from_mapping(values.get("mqtt")),
            metadata=_metadata_value(values),
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
        if self.mqtt:
            data["mqtt"] = self.mqtt.as_dict(include_secrets=include_secrets)
        if self.metadata:
            data["metadata"] = (
                dict(self.metadata)
                if include_secrets
                else _redact_secret_map(self.metadata)
            )
        return data


_SECRET_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "authorization",
    "apikey",
    "accesskey",
    "cryptokey",
    "privatekey",
    "sessionkey",
)


def _is_secret_key(key: Any) -> bool:
    """Return whether a mapping key names a likely secret (e.g. ``api_key``)."""

    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(marker in normalized for marker in _SECRET_KEY_MARKERS)


def _redact_secret_map(value: Any) -> Any:
    """Deep-copy an arbitrary structure, replacing secret-keyed values.

    Used for the default (non-secret) serialization of the free-form
    ``metadata`` map, whose keys the SDK does not control. Entries whose key
    looks like a secret are replaced with ``"<redacted>"``; everything else is
    preserved. ``include_secrets=True`` bypasses this entirely.
    """

    if isinstance(value, Mapping):
        return {
            key: "<redacted>" if _is_secret_key(key) else _redact_secret_map(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_secret_map(item) for item in value]
    return value


def _value(values: Mapping[str, Any], key: str, aliases: tuple[str, ...] = ()) -> Any:
    for candidate in (key, *aliases):
        value = values.get(candidate, _MISSING)
        if value is not _MISSING:
            return value
    return _MISSING


def _identity_config_mapping(
    values: Mapping[str, Any],
    *,
    profile: str | None,
) -> Mapping[str, Any]:
    profiles = values.get("profiles")
    if isinstance(profiles, Mapping):
        profile_name = (
            profile
            or _optional_string(values, "profile", aliases=("default_profile", "defaultProfile"))
            or "default"
        )
        selected = profiles.get(profile_name)
        if not isinstance(selected, Mapping):
            raise ThalovantIdentityError(f"Missing Thalovant config profile: {profile_name}")
        return _profile_identity_mapping(selected)
    return _profile_identity_mapping(values)


def _profile_identity_mapping(values: Mapping[str, Any]) -> Mapping[str, Any]:
    identity = values.get("identity")
    if isinstance(identity, Mapping):
        return identity
    return values


def _assert_secure_config_file(path: Path) -> None:
    _assert_secure_secret_file(path, "Thalovant config file")


def _assert_secure_identity_file(path: Path) -> None:
    _assert_secure_secret_file(path, "identity file")


def _assert_secure_secret_file(path: Path, description: str) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise ThalovantIdentityError(f"Unable to read {description}: {path}") from exc
    if os.name != "nt" and mode & 0o077 and not _is_kubernetes_projected_secret_file(path):
        raise ThalovantIdentityError(
            f"{description.capitalize()} is too permissive: {path}. Run `chmod 600 {path}`."
        )


def _is_kubernetes_projected_secret_file(path: Path) -> bool:
    if not os.environ.get("KUBERNETES_SERVICE_HOST"):
        return False
    try:
        data_link = path.parent / "..data"
        return data_link.is_symlink() and path.is_symlink()
    except OSError:
        return False


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


def _bool_value(
    values: Mapping[str, Any],
    key: str,
    *,
    default: bool,
    aliases: tuple[str, ...] = (),
) -> bool:
    value = _value(values, key, aliases)
    if value is _MISSING or value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _qos_value(values: Mapping[str, Any], key: str, *, default: int) -> int:
    value = _value(values, key)
    if value is _MISSING or value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed in {0, 1} else default


def _metadata_value(values: Mapping[str, Any]) -> dict[str, Any]:
    raw = values.get("metadata")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _normalize_path(path: str | None) -> str:
    if not path:
        return ""
    normalized = "/" + path.strip("/")
    return "" if normalized == "/" else normalized
