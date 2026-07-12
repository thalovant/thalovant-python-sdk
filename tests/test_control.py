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
        if url.endswith("/v1/operations/operation-1"):
            assert kwargs["headers"]["authorization"] == "Bearer token"
            return FakeResponse(
                200,
                {
                    "id": "operation-1",
                    "kind": "gitops.commit",
                    "aggregate_type": "gitops",
                    "aggregate_id": None,
                    "status": "committed",
                    "details": {"git_commit_created": True},
                    "git_commit_sha": "abc123",
                    "error_code": None,
                    "error_message": None,
                    "created_at": "2026-07-11T00:00:00Z",
                    "updated_at": "2026-07-11T00:00:01Z",
                    "committed_at": "2026-07-11T00:00:01Z",
                    "applied_at": None,
                    "ready_at": None,
                    "terminal_at": None,
                    "links": {"self": "/api/v1/operations/operation-1"},
                },
            )
        if url.endswith("/v1/memory"):
            assert kwargs["headers"]["authorization"] == "Bearer token"
            if method == "GET":
                assert kwargs["params"] == {
                    "scope": "workspace",
                    "kind": "preference",
                    "owner_id": "owner-1",
                    "hub_id": "hub-1",
                    "q": "timezone",
                    "include_deleted": "true",
                    "include_expired": "true",
                    "limit": 25,
                    "offset": 50,
                }
                return FakeResponse(
                    200,
                    {
                        "data": [{"id": "memory-1", "content": "Use UTC."}],
                        "meta": {"count": 1, "next": None},
                        "links": {"next": None},
                    },
                )
            if method == "POST":
                assert kwargs["json"] == {
                    "scope": "workspace",
                    "kind": "preference",
                    "content": "Use UTC.",
                    "owner_id": "owner-1",
                    "hub_id": "hub-1",
                    "consent_scope": "daily_desk_memory",
                    "retention_policy": "user_controlled",
                }
                return FakeResponse(
                    201,
                    {
                        "id": "memory-1",
                        "scope": "workspace",
                        "kind": "preference",
                        "content": "Use UTC.",
                    },
                )
        if url.endswith("/v1/memory/summary"):
            assert kwargs["headers"]["authorization"] == "Bearer token"
            assert kwargs["params"] == {"owner_id": "owner-1"}
            return FakeResponse(
                200,
                {
                    "total": 1,
                    "by_scope": {"workspace": 1},
                    "by_kind": {"preference": 1},
                    "expired": 0,
                    "deleted": 0,
                },
            )
        if url.endswith("/v1/memory/memory-1"):
            assert kwargs["headers"]["authorization"] == "Bearer token"
            if method == "GET":
                return FakeResponse(
                    200,
                    {
                        "id": "memory-1",
                        "scope": "workspace",
                        "kind": "preference",
                        "content": "Use UTC.",
                    },
                )
            if method == "PATCH":
                assert kwargs["json"] == {
                    "content": "Use America/Toronto.",
                    "clear_expires_at": True,
                }
                return FakeResponse(
                    200,
                    {
                        "id": "memory-1",
                        "scope": "workspace",
                        "kind": "preference",
                        "content": "Use America/Toronto.",
                    },
                )
            if method == "DELETE":
                return FakeResponse(204, "")
        if url.endswith("/v1/admin/analytics/overview"):
            assert kwargs["headers"]["authorization"] == "Bearer token"
            assert kwargs["params"] == {
                "range": "30d",
                "bucket": "1d",
                "owner_id": "owner-1",
                "hub_id": "hub-1",
                "client_id": "client-1",
                "country": "CA",
                "message": "speak",
                "utterance": "hello",
                "intent": "DailyDeskIntent",
                "time_start": "2026-05-03T20:00:00Z",
                "time_end": "2026-05-03T21:00:00Z",
                "weekday": 6,
                "hour": 0,
            }
            return FakeResponse(200, {"meta": {"scope": "admin"}, "totals": {"utterances": 7}})
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


def test_control_plane_gets_typed_operation():
    api = ThalovantControlPlane(access_token="token", session=FakeSession())

    operation = api.get_operation("operation-1")

    assert operation.id == "operation-1"
    assert operation.status == "committed"
    assert operation.git_commit_sha == "abc123"
    assert operation.details["git_commit_created"] is True


def test_control_plane_manages_memory_items():
    session = FakeSession()
    api = ThalovantControlPlane(
        "https://dash.example.com/api",
        access_token="token",
        session=session,
    )

    page = api.list_memory_items(
        scope="workspace",
        kind="preference",
        owner_id="owner-1",
        hub_id="hub-1",
        query="timezone",
        include_deleted=True,
        include_expired=True,
        limit=25,
        offset=50,
    )
    summary = api.get_memory_summary(owner_id="owner-1")
    created = api.create_memory_item(
        {
            "scope": "workspace",
            "kind": "preference",
            "content": "Use UTC.",
            "ownerId": "owner-1",
            "hubId": "hub-1",
            "consentScope": "daily_desk_memory",
            "retentionPolicy": "user_controlled",
        }
    )
    item = api.get_memory_item("memory-1")
    updated = api.update_memory_item(
        "memory-1",
        {
            "content": "Use America/Toronto.",
            "clearExpiresAt": True,
        },
    )
    api.delete_memory_item("memory-1")

    assert len(page["data"]) == 1
    assert summary["total"] == 1
    assert created["id"] == "memory-1"
    assert item["content"] == "Use UTC."
    assert updated["content"] == "Use America/Toronto."


def test_control_plane_get_analytics_overview():
    session = FakeSession()
    api = ThalovantControlPlane(
        "https://dash.example.com/api",
        access_token="token",
        session=session,
    )

    overview = api.get_analytics_overview(
        admin=True,
        range="30d",
        bucket="1d",
        owner_id="owner-1",
        hub_id="hub-1",
        client_id="client-1",
        country="CA",
        message="speak",
        utterance="hello",
        intent="DailyDeskIntent",
        time_start="2026-05-03T20:00:00Z",
        time_end="2026-05-03T21:00:00Z",
        weekday=6,
        hour=0,
    )

    assert overview["meta"]["scope"] == "admin"
    assert overview["totals"]["utterances"] == 7


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
