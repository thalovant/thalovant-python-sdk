import json

import pytest

from thalovant import (
    HubDataPlaneEndpoints,
    HubProtocolSettings,
    ThalovantIdentity,
    ThalovantIdentityError,
    select_data_plane_endpoint,
)


def test_loads_identity_from_file(tmp_path):
    path = tmp_path / "_identity.json"
    path.write_text(
        json.dumps(
            {
                "access_key": "key",
                "password": "password",
                "crypto_key": "crypto",
                "site_id": "client-site",
                "default_master": "http://hub.local/",
                "default_port": "5679",
            }
        ),
        encoding="utf-8",
    )

    identity = ThalovantIdentity.from_file(path)

    assert identity.access_key == "key"
    assert identity.password == "password"
    assert identity.crypto_key == "crypto"
    assert identity.site_id == "client-site"
    assert identity.default_master == "http://hub.local"
    assert identity.default_port == 5679


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
