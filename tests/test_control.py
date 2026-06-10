import pytest

from thalovant import ThalovantControlPlane, ThalovantUnsupportedProtocolError


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self):
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if url.endswith("/v1/auth/token"):
            return FakeResponse(200, {"access_token": "token", "expires_in": 3600})
        if url.endswith("/v1/public/hubs"):
            assert "authorization" not in kwargs["headers"]
            assert kwargs["params"] == {"limit": 12}
            return FakeResponse(
                200,
                {
                    "data": [
                        {
                            "id": "hub-public",
                            "name": "joke-garden",
                            "slug": "joke-garden",
                            "title": "Joke Garden",
                        }
                    ],
                    "meta": {"count": 1, "next": None},
                    "links": {"next": None},
                },
            )
        if url.endswith("/v1/public/hubs/joke-garden"):
            assert "authorization" not in kwargs["headers"]
            return FakeResponse(
                200,
                {
                    "id": "hub-public",
                    "name": "joke-garden",
                    "slug": "joke-garden",
                    "title": "Joke Garden",
                },
            )
        if url.endswith("/v1/hubs/hub-1"):
            return FakeResponse(
                200,
                {
                    "id": "hub-1",
                    "name": "joke-garden",
                    "domain": "jokes.thalovant.io",
                    "spec": {
                        "protocols": {
                            "wss": {"enabled": True},
                            "http": {"enabled": True},
                            "mqtt": {"enabled": False},
                        }
                    },
                },
            )
        if url.endswith("/v1/hubs/hub-mqtt"):
            return FakeResponse(
                200,
                {
                    "id": "hub-mqtt",
                    "name": "mqtt-hub",
                    "domain": "mqtt.thalovant.io",
                    "data_plane_endpoints": {
                        "https": "https://mqtt.thalovant.io",
                        "wss": "wss://mqtt.thalovant.io",
                        "mqtt": "mqtts://broker.thalovant.io:8883",
                    },
                    "spec": {
                        "protocols": {
                            "wss": {"enabled": True},
                            "http": {"enabled": True},
                            "mqtt": {
                                "enabled": True,
                                "brokerUrl": "mqtts://broker.thalovant.io:8883",
                            },
                        }
                    },
                },
            )
        if url.endswith("/v1/clients"):
            payload = kwargs["json"]
            assert payload["spec"]["apiKey"]
            assert payload["spec"]["password"]
            assert payload["spec"]["cryptoKey"]
            if payload["hub_id"] == "hub-mqtt":
                return FakeResponse(
                    201,
                    {
                        "id": "client-mqtt",
                        "name": payload["name"],
                        "hub_id": payload["hub_id"],
                        "spec": {},
                        "initial_identify": {
                            "password": payload["spec"]["password"],
                            "access_key": payload["spec"]["apiKey"],
                            "crypto_key": payload["spec"]["cryptoKey"],
                            "site_id": payload["spec"]["siteId"],
                            "default_port": 443,
                            "default_master": "wss://mqtt.thalovant.io",
                            "mqtt": {
                                "endpoint": "mqtts://broker.thalovant.io:8883",
                                "username": payload["spec"]["apiKey"],
                                "password": "broker-password",
                                "topic_prefix": f"hivemind/hub-mqtt/{payload['spec']['apiKey']}",
                                "tls": True,
                            },
                        },
                    },
                )
            return FakeResponse(
                201,
                {
                    "id": "client-1",
                    "name": payload["name"],
                    "hub_id": payload["hub_id"],
                    "spec": {
                        "version": "1",
                        "apiKeyRef": {"name": "secret", "key": "apiKey"},
                    },
                },
            )
        raise AssertionError(url)


def test_control_plane_bootstrap_generates_local_identity_secrets():
    session = FakeSession()
    api = ThalovantControlPlane("https://dash.example.com/api", session=session)

    api.login("ada@example.com", "secret")
    result = api.create_client_identity("hub-1", name="kiosk")

    assert result.identity.access_key
    assert result.identity.password
    assert result.identity.crypto_key
    assert result.identity.site_id == "kiosk"
    assert result.identity.endpoint_for("https") == "https://jokes.thalovant.io:443"
    assert result.selected_protocol == "wss"
    assert api.require_runtime_protocol(result).endpoint == "wss://jokes.thalovant.io"
    assert (
        api.require_runtime_protocol(result, protocol="wss").endpoint
        == "wss://jokes.thalovant.io"
    )
    with pytest.raises(ThalovantUnsupportedProtocolError, match="MQTT"):
        api.require_runtime_protocol(result, protocol="mqtt")
    assert "access_key" not in result.as_dict()["identity"]
    assert result.as_dict(include_secrets=True)["identity"]["access_key"]


def test_control_plane_uses_public_api_default_and_normalizes_v1_root():
    assert ThalovantControlPlane().api_url == "https://api.thalovant.com/"
    assert (
        ThalovantControlPlane("https://api.thalovant.com/v1").api_url
        == "https://api.thalovant.com/"
    )
    assert (
        ThalovantControlPlane("https://dash.example.com/api/v1").api_url
        == "https://dash.example.com/api/"
    )


def test_control_plane_lists_public_hubs_without_auth():
    session = FakeSession()
    api = ThalovantControlPlane("https://dash.example.com/api", session=session)

    page = api.list_public_hubs(limit=12)
    hub = api.get_public_hub("joke-garden")

    assert page["data"][0]["slug"] == "joke-garden"
    assert hub["title"] == "Joke Garden"


def test_control_plane_bootstrap_uses_api_returned_mqtt_credentials():
    session = FakeSession()
    api = ThalovantControlPlane("https://dash.example.com/api", session=session)

    api.login("ada@example.com", "secret")
    result = api.create_client_identity("hub-mqtt", name="kiosk")

    assert result.identity.mqtt is not None
    assert result.identity.mqtt.endpoint == "mqtts://broker.thalovant.io:8883"
    assert result.identity.mqtt.password == "broker-password"
    assert result.identity.endpoint_for("mqtt") == "mqtts://broker.thalovant.io:8883"
    assert api.require_runtime_protocol(result, protocol="mqtt").endpoint == "mqtts://broker.thalovant.io:8883"
    assert result.as_dict()["identity"]["mqtt"] == {
        "endpoint": "mqtts://broker.thalovant.io:8883",
        "tls": True,
    }
