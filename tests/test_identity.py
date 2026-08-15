import json
import os

import pytest

from thalovant import (
    HubDataPlaneEndpoints,
    HubProtocolSettings,
    ThalovantIdentity,
    ThalovantIdentityError,
    select_data_plane_endpoint,
)


def _secure_config(path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def _secure_json(path, content: dict) -> None:
    path.write_text(json.dumps(content), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def test_loads_identity_from_file(tmp_path):
    path = tmp_path / "_identity.json"
    _secure_json(
        path,
        {
            "access_key": "key",
            "password": "password",
            "crypto_key": "crypto",
            "site_id": "client-site",
            "default_master": "http://hub.local/",
            "default_port": "5679",
        },
    )

    identity = ThalovantIdentity.from_file(path)

    assert identity.access_key == "key"
    assert identity.password == "password"
    assert identity.crypto_key == "crypto"
    assert identity.site_id == "client-site"
    assert identity.default_master == "http://hub.local"
    assert identity.default_port == 5679


def test_rejects_permissive_identity_file(tmp_path):
    if os.name == "nt":
        pytest.skip("Windows ACLs are not represented by POSIX mode bits")
    path = tmp_path / "_identity.json"
    path.write_text(
        json.dumps(
            {
                "access_key": "key",
                "password": "password",
                "site_id": "client-site",
                "default_master": "http://hub.local/",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o644)

    with pytest.raises(ThalovantIdentityError, match="too permissive"):
        ThalovantIdentity.from_file(path)


def test_loads_identity_from_kubernetes_projected_secret(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("Kubernetes projected Secret symlinks use POSIX paths")
    mount = tmp_path / "client-secret"
    data_dir = mount / "..2026_06_29_00_00_00.000000000"
    data_dir.mkdir(parents=True)
    target = data_dir / "client-config.json"
    target.write_text(
        json.dumps(
            {
                "apiKey": "key",
                "password": "password",
                "clientId": "client-site",
                "defaultMaster": "https://hub.example.com",
            }
        ),
        encoding="utf-8",
    )
    target.chmod(0o644)
    (mount / "..data").symlink_to(data_dir.name)
    path = mount / "client-config.json"
    path.symlink_to("..data/client-config.json")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")

    identity = ThalovantIdentity.from_file(path)

    assert identity.access_key == "key"
    assert identity.site_id == "client-site"


def test_loads_identity_from_yaml_config_profile(tmp_path):
    path = tmp_path / "config.yaml"
    _secure_config(
        path,
        """
version: 1
profile: prod
profiles:
  prod:
    identity:
      access_key: key
      password: password
      crypto_key: crypto
      site_id: client-site
      default_master: https://hub.example.com
      default_port: 443
      mqtt:
        endpoint: mqtts://mqtt.example.com:8883
        username: key
        password: broker-password
        topic_prefix: hivemind/hub/key
""",
    )

    identity = ThalovantIdentity.from_config(path)

    assert identity.access_key == "key"
    assert identity.crypto_key == "crypto"
    assert identity.default_master == "https://hub.example.com"
    assert identity.mqtt is not None
    assert identity.mqtt.password == "broker-password"


def test_rejects_permissive_yaml_config(tmp_path):
    if os.name == "nt":
        pytest.skip("Windows ACLs are not represented by POSIX mode bits")
    path = tmp_path / "config.yaml"
    path.write_text("identity: {}\n", encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(ThalovantIdentityError, match="too permissive"):
        ThalovantIdentity.from_config(path)


def test_loads_identity_aliases():
    identity = ThalovantIdentity.from_mapping(
        {
            "key": "key",
            "password": "password",
            "cryptoKey": "crypto",
            "siteId": "site",
            "host": "http://hub.local",
            "port": 5680,
        }
    )

    assert identity.access_key == "key"
    assert identity.crypto_key == "crypto"
    assert identity.site_id == "site"
    assert identity.default_master == "http://hub.local"
    assert identity.default_port == 5680


def test_identity_supports_reverse_proxy_path():
    identity = ThalovantIdentity.from_mapping(
        {
            "key": "key",
            "password": "password",
            "site": "site",
            "host": "wss://hub.example.com/base/",
            "port": 443,
            "path": "/hivemind/public",
        }
    )

    assert identity.default_path == "/hivemind/public"
    assert (
        identity.endpoint_base() == "https://hub.example.com:443/base/hivemind/public"
    )


def test_identity_uses_protocol_aware_data_plane_endpoints():
    identity = ThalovantIdentity.from_mapping(
        {
            "key": "key",
            "password": "password",
            "site": "site",
            "host": "wss://hub.example.com",
            "port": 443,
            "path": "/hivemind/public",
            "data_plane_endpoints": {
                "https": "https://api.example.com/hivemind/public",
                "wss": "wss://socket.example.com/hivemind/public",
                "mqtt": "mqtts://mqtt.example.com:8883",
            },
            "protocols": {
                "wss": {"enabled": True},
                "http": {"enabled": True},
                "mqtt": {"enabled": True},
            },
        }
    )

    assert identity.endpoint_base() == "https://api.example.com:443/hivemind/public"
    assert identity.endpoint_for("wss") == "wss://socket.example.com/hivemind/public"
    assert identity.endpoint_for("mqtt") == "mqtts://mqtt.example.com:8883"
    assert identity.enabled_protocols() == ("wss", "https", "mqtt")
    assert identity.supports_protocol("https")


def test_identity_loads_mqtt_credentials_and_redacts_by_default():
    identity = ThalovantIdentity.from_mapping(
        {
            "access_key": "key",
            "password": "password",
            "site_id": "site",
            "default_master": "wss://hub.example.com",
            "default_port": 443,
            "mqtt": {
                "endpoint": "mqtts://mqtt.example.com:8883",
                "username": "key",
                "password": "broker-password",
                "topic_prefix": "hivemind/hub/key",
            },
        }
    )

    assert identity.mqtt is not None
    assert identity.mqtt.endpoint == "mqtts://mqtt.example.com:8883"
    assert identity.mqtt.username == "key"
    assert identity.as_dict()["mqtt"] == {
        "endpoint": "mqtts://mqtt.example.com:8883",
        "tls": True,
    }
    assert identity.as_dict(include_secrets=True)["mqtt"]["password"] == "broker-password"


def test_identity_repr_hides_secret_material():
    identity = ThalovantIdentity.from_mapping(
        {
            "access_key": "access-key-secret",
            "password": "password-secret",
            "crypto_key": "crypto-key-secret",
            "site_id": "site",
            "default_master": "wss://hub.example.com",
            "default_port": 443,
            "mqtt": {
                "endpoint": "mqtts://mqtt.example.com:8883",
                "username": "broker-username-secret",
                "password": "broker-password-secret",
                "topic_prefix": "hivemind/hub/access-key-secret",
            },
        }
    )

    for rendered in (repr(identity), str(identity), repr(identity.mqtt)):
        for secret in (
            "access-key-secret",
            "password-secret",
            "crypto-key-secret",
            "broker-username-secret",
            "broker-password-secret",
        ):
            assert secret not in rendered

    # Non-secret fields stay visible for debugging. Assert against the object's
    # own accessor rather than a URL string literal (a literal URL on the left
    # of ``in`` reads as broken host validation to static analysis).
    assert "site_id='site'" in repr(identity)
    assert identity.mqtt.endpoint in repr(identity.mqtt)

    # The endpoint map only ever absorbs the broker URL from the credentials
    # block, so the default as_dict carries no secret through it either.
    redacted = json.dumps(identity.as_dict())
    for secret in ("access-key-secret", "password-secret", "broker-username-secret"):
        assert secret not in redacted
    assert identity.endpoint_for("mqtt") == "mqtts://mqtt.example.com:8883"

    # Serialization is unaffected: the secret paths still carry the values.
    full = identity.as_dict(include_secrets=True)
    assert full["access_key"] == "access-key-secret"
    assert full["password"] == "password-secret"
    assert full["crypto_key"] == "crypto-key-secret"
    assert full["mqtt"]["password"] == "broker-password-secret"


def test_mqtt_endpoint_userinfo_redacted_in_default_output():
    """A broker endpoint URL may embed ``user:pass@`` userinfo. The default
    (non-secret) serialization and repr must strip it; include_secrets keeps it."""

    identity = ThalovantIdentity.from_mapping(
        {
            "access_key": "key",
            "password": "password",
            "site_id": "site",
            "default_master": "wss://hub.example.com",
            "default_port": 443,
            "mqtt": {
                "endpoint": "mqtts://broker-user:BROKER-URL-SECRET@mqtt.example.com:8883",
                "username": "broker-user",
                "password": "broker-password-secret",
            },
        }
    )

    default = identity.as_dict()
    assert default["mqtt"]["endpoint"] == "mqtts://mqtt.example.com:8883"
    assert "BROKER-URL-SECRET" not in json.dumps(default)
    assert "BROKER-URL-SECRET" not in repr(identity)
    assert "BROKER-URL-SECRET" not in repr(identity.mqtt)
    # The data_plane_endpoints view (default redacts, and repr too) is clean.
    assert "BROKER-URL-SECRET" not in repr(identity.data_plane_endpoints)

    # (c) MQTT username == the access key is never in the default output/repr.
    assert "username" not in default["mqtt"]
    assert "broker-user" not in repr(identity.mqtt)

    # include_secrets=True keeps the full endpoint (userinfo included) for
    # persistence and the wire path.
    full = identity.as_dict(include_secrets=True)
    assert full["mqtt"]["endpoint"] == "mqtts://broker-user:BROKER-URL-SECRET@mqtt.example.com:8883"
    assert full["mqtt"]["username"] == "broker-user"
    assert full["mqtt"]["password"] == "broker-password-secret"


def test_identity_metadata_redacts_secret_keyed_entries_by_default():
    """Free-form metadata (keys the SDK does not control) must not leak
    secret-keyed values through the default, log-safe serializer."""

    identity = ThalovantIdentity.from_mapping(
        {
            "access_key": "key",
            "password": "password",
            "site_id": "site",
            "default_master": "https://hub.example.com",
            "default_port": 443,
            "metadata": {
                "user_id": "u42",
                "role": "member",
                "api_key": "META-APIKEY-SECRET",
                "Session-Token": "META-TOKEN-SECRET",
                "nested": {"client_secret": "META-NESTED-SECRET", "kept": "ok"},
            },
        }
    )

    default_meta = identity.as_dict()["metadata"]
    assert default_meta["user_id"] == "u42"
    assert default_meta["role"] == "member"
    assert default_meta["api_key"] == "<redacted>"
    assert default_meta["Session-Token"] == "<redacted>"
    assert default_meta["nested"] == {"client_secret": "<redacted>", "kept": "ok"}

    blob = json.dumps(identity.as_dict())
    for secret in ("META-APIKEY-SECRET", "META-TOKEN-SECRET", "META-NESTED-SECRET"):
        assert secret not in blob
    # metadata is kept out of repr entirely.
    assert "META-APIKEY-SECRET" not in repr(identity)

    # include_secrets=True returns the metadata verbatim (persistence path).
    full_meta = identity.as_dict(include_secrets=True)["metadata"]
    assert full_meta["api_key"] == "META-APIKEY-SECRET"
    assert full_meta["Session-Token"] == "META-TOKEN-SECRET"
    assert full_meta["nested"]["client_secret"] == "META-NESTED-SECRET"


def test_identity_loads_operator_generated_client_config_aliases():
    identity = ThalovantIdentity.from_mapping(
        {
            "apiKey": "client-access-key",
            "password": "client-password",
            "cryptoKey": "client-crypto",
            "site_id": "32",
            "defaultMaster": "https://daily-desk.thalovant.io",
            "default_port": 443,
            "mqtt": {
                "endpoint": "mqtts://mqtt.thalovant.com:8883",
                "brokerUsername": "client-access-key",
                "brokerPassword": "broker-password",
                "topicPrefix": "hivemind",
                "hubId": "hub-alpha",
            },
        }
    )

    assert identity.access_key == "client-access-key"
    assert identity.crypto_key == "client-crypto"
    assert identity.site_id == "32"
    assert identity.default_master == "https://daily-desk.thalovant.io"
    assert identity.mqtt is not None
    assert identity.mqtt.username == "client-access-key"
    assert identity.mqtt.password == "broker-password"
    assert identity.mqtt.topic_prefix == "hivemind"
    assert identity.mqtt.hub_id == "hub-alpha"


def test_builds_data_plane_endpoints_from_hub_resource():
    endpoints = HubDataPlaneEndpoints.from_hub(
        {
            "domain": "jokes.thalovant.io",
            "spec": {
                "protocols": {
                    "wss": {"enabled": True},
                    "http": {"enabled": True},
                    "mqtt": {"enabled": False},
                }
            },
        }
    )

    assert endpoints.wss == "wss://jokes.thalovant.io"
    assert endpoints.https == "https://jokes.thalovant.io"
    assert endpoints.mqtt is None


def test_selects_first_enabled_endpoint_from_preference():
    endpoints = HubDataPlaneEndpoints(
        https="https://hub.example.com/public",
        wss="wss://hub.example.com/public",
    )
    selected = select_data_plane_endpoint(
        endpoints,
        HubProtocolSettings(wss=True, http=True),
        ("mqtt", "wss", "https"),
    )

    assert selected is not None
    assert selected.protocol == "wss"
    assert selected.endpoint == "wss://hub.example.com/public"


def test_rejects_missing_required_field():
    with pytest.raises(ThalovantIdentityError, match="access_key"):
        ThalovantIdentity.from_mapping(
            {
                "password": "password",
                "site_id": "site",
                "default_master": "http://hub.local",
            }
        )
