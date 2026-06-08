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
class MqttBrokerCredentials:
    """Per-client MQTT broker credentials returned by the Thalovant API."""

    endpoint: str
    username: str
    password: str
    topic_prefix: str | None = None
    hub_id: str | None = None
    c2s_topic: str | None = None
    s2c_topic: str | None = None
    status_topic: str | None = None
    hash_topics: bool = False
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
        hub_id = _optional_string(values, "hub_id", aliases=("hubId",))
        c2s_topic = _optional_string(values, "c2s_topic", aliases=("c2sTopic",))
        s2c_topic = _optional_string(values, "s2c_topic", aliases=("s2cTopic",))
        status_topic = _optional_string(values, "status_topic", aliases=("statusTopic",))
        return cls(
            endpoint=endpoint,
            username=username,
            password=password,
            topic_prefix=topic_prefix,
            hub_id=hub_id,
            c2s_topic=c2s_topic,
            s2c_topic=s2c_topic,
            status_topic=status_topic,
            hash_topics=_bool_value(values, "hash_topics", aliases=("hashTopics",), default=False),
            qos=_qos_value(values, "qos", default=1),
            tls=_bool_value(values, "tls", default=endpoint.startswith("mqtts://")),
        )

    def as_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {"endpoint": self.endpoint, "tls": self.tls}
        if include_secrets:
            data.update(
                {
                    "username": self.username,
                    "password": self.password,
                }
            )
            if self.topic_prefix:
                data["topic_prefix"] = self.topic_prefix
            if self.hub_id:
                data["hub_id"] = self.hub_id
            if self.c2s_topic:
                data["c2s_topic"] = self.c2s_topic
            if self.s2c_topic:
                data["s2c_topic"] = self.s2c_topic
            if self.status_topic:
                data["status_topic"] = self.status_topic
            if self.hash_topics:
                data["hash_topics"] = True
            if self.qos != 1:
                data["qos"] = self.qos
        return data


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
    mqtt: MqttBrokerCredentials | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

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
                "mqtt": {
                    "endpoint": env.get(f"{prefix}MQTT_ENDPOINT")
                    or env.get(f"{prefix}HUB_MQTT_HOST"),
                    "username": env.get(f"{prefix}MQTT_USERNAME"),
                    "password": env.get(f"{prefix}MQTT_PASSWORD"),
                    "topic_prefix": env.get(f"{prefix}MQTT_TOPIC_PREFIX"),
                    "hub_id": env.get(f"{prefix}MQTT_HUB_ID"),
                    "c2s_topic": env.get(f"{prefix}MQTT_C2S_TOPIC"),
                    "s2c_topic": env.get(f"{prefix}MQTT_S2C_TOPIC"),
                    "status_topic": env.get(f"{prefix}MQTT_STATUS_TOPIC"),
                    "hash_topics": env.get(f"{prefix}MQTT_HASH_TOPICS"),
                    "qos": env.get(f"{prefix}MQTT_QOS"),
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
            data["metadata"] = dict(self.metadata)
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
