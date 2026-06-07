import json

import pytest

from thalovant import HubDataPlaneEndpoints, ThalovantIdentity, ThalovantIdentityError


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


def test_rejects_missing_required_field():
    with pytest.raises(ThalovantIdentityError, match="access_key"):
        ThalovantIdentity.from_mapping(
            {
                "password": "password",
                "site_id": "site",
                "default_master": "http://hub.local",
            }
        )
