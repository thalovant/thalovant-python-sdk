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
        if url.endswith("/v1/clients"):
            payload = kwargs["json"]
            assert payload["spec"]["apiKey"]
            assert payload["spec"]["password"]
            assert payload["spec"]["cryptoKey"]
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
    assert result.selected_protocol == "https"
    assert (
        api.require_runtime_protocol(result, protocol="wss").endpoint
        == "wss://jokes.thalovant.io"
    )
    with pytest.raises(ThalovantUnsupportedProtocolError, match="MQTT"):
        api.require_runtime_protocol(result, protocol="mqtt")
    assert "access_key" not in result.as_dict()["identity"]
    assert result.as_dict(include_secrets=True)["identity"]["access_key"]
