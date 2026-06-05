import json

import pytest

from thalovant import ThalovantIdentity, ThalovantIdentityError


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
    assert identity.endpoint_base() == "https://hub.example.com:443/base/hivemind/public"


def test_rejects_missing_required_field():
    with pytest.raises(ThalovantIdentityError, match="access_key"):
        ThalovantIdentity.from_mapping(
            {
                "password": "password",
                "site_id": "site",
                "default_master": "http://hub.local",
            }
        )
