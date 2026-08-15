import json
from typing import get_args

import pytest

from thalovant import (
    OperationStatus,
    ThalovantAPIError,
    ThalovantControlPlane,
    ThalovantTimeoutError,
    ThalovantUnsupportedProtocolError,
)


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
                    "links": {"self": "/v1/operations/operation-1"},
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
        if url.endswith("/v1/analytics/overview"):
            assert "/v1/admin/" not in url
            assert kwargs["headers"]["authorization"] == "Bearer token"
            assert kwargs["params"] == {
                "range": "30d",
                "bucket": "1d",
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
            return FakeResponse(200, {"meta": {"scope": "workspace"}, "totals": {"utterances": 7}})
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
                        "spec": {
                            "version": "1",
                            "siteId": payload["spec"]["siteId"],
                            "apiKey": payload["spec"]["apiKey"],
                            "password": payload["spec"]["password"],
                            "cryptoKey": payload["spec"]["cryptoKey"],
                        },
                        "initial_identify_token": "identify-token-secret",
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
    assert operation.status in get_args(OperationStatus)
    assert operation.git_commit_sha == "abc123"
    assert operation.details["git_commit_created"] is True
    assert operation.links["self"] == "/v1/operations/operation-1"


def test_control_plane_login_sends_mfa_fields_only_when_provided():
    session = FakeSession()
    api = ThalovantControlPlane("https://dash.example.com/api", session=session)

    api.login("ada@example.com", "secret")
    _, _, plain = session.requests[0]
    assert plain["json"] == {"email": "ada@example.com", "password": "secret"}

    api.login("ada@example.com", "secret", otp_code="123456")
    _, _, with_otp = session.requests[1]
    assert with_otp["json"] == {
        "email": "ada@example.com",
        "password": "secret",
        "otp_code": "123456",
    }

    api.login("ada@example.com", "secret", scope="hubs:read", recovery_code="rec-code-1")
    _, _, with_recovery = session.requests[2]
    assert with_recovery["json"] == {
        "email": "ada@example.com",
        "password": "secret",
        "scope": "hubs:read",
        "recovery_code": "rec-code-1",
    }
    assert api.access_token == "token"


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
        range="30d",
        bucket="1d",
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

    assert overview["meta"]["scope"] == "workspace"
    assert overview["totals"]["utterances"] == 7
    method, url, _ = session.requests[-1]
    assert method == "GET"
    assert url.endswith("/v1/analytics/overview")


def test_control_plane_analytics_overview_no_longer_takes_admin():
    """The admin-only analytics route was removed from this non-admin SDK."""

    api = ThalovantControlPlane(access_token="token", session=FakeSession())

    with pytest.raises(TypeError, match="admin"):
        api.get_analytics_overview(admin=True)
    with pytest.raises(TypeError, match="owner_id"):
        api.get_analytics_overview(owner_id="owner-1")


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


def _bootstrap_mqtt_result():
    """Provision against the MQTT hub fixture, whose client response carries
    every secret twice: the echoed request spec and the initial_identify block."""

    session = FakeSession()
    api = ThalovantControlPlane(
        "https://dash.example.com/api", access_token="token", session=session
    )
    result = api.create_client_identity("hub-mqtt", name="kiosk")
    secrets = {
        "access_key": result.identity.access_key,
        "password": result.identity.password,
        "crypto_key": result.identity.crypto_key,
        "mqtt_password": result.identity.mqtt.password,
        "initial_identify_token": "identify-token-secret",
    }
    return session, result, secrets


def test_bootstrap_result_default_as_dict_contains_no_secret_values():
    _, result, secrets = _bootstrap_mqtt_result()

    serialized = json.dumps(result.as_dict())

    for name, value in secrets.items():
        assert value not in serialized, f"default as_dict leaked {name}"
    redacted_client = result.as_dict()["client"]
    assert "initial_identify" not in redacted_client
    assert "initial_identify_token" not in redacted_client
    assert "apiKey" not in redacted_client["spec"]
    assert "password" not in redacted_client["spec"]
    assert "cryptoKey" not in redacted_client["spec"]
    # Non-secret client fields survive the scrub.
    assert redacted_client["id"] == "client-mqtt"
    assert redacted_client["spec"]["version"] == "1"
    assert redacted_client["spec"]["siteId"] == "kiosk"


def test_bootstrap_result_include_secrets_still_returns_everything():
    _, result, secrets = _bootstrap_mqtt_result()

    full = result.as_dict(include_secrets=True)

    assert full["client"] is result.client, "client must pass through unchanged"
    assert full["identity"]["access_key"] == secrets["access_key"]
    assert full["identity"]["password"] == secrets["password"]
    assert full["identity"]["crypto_key"] == secrets["crypto_key"]
    assert full["identity"]["mqtt"]["password"] == secrets["mqtt_password"]
    assert full["client"]["initial_identify_token"] == "identify-token-secret"
    assert full["client"]["initial_identify"]["access_key"] == secrets["access_key"]
    assert full["client"]["spec"]["apiKey"] == secrets["access_key"]
    serialized = json.dumps(full)
    for name, value in secrets.items():
        assert value in serialized, f"include_secrets=True lost {name}"


def test_bootstrap_redaction_does_not_touch_the_wire_request():
    session, result, secrets = _bootstrap_mqtt_result()

    result.as_dict()  # exercising the redaction must not corrupt anything

    wire_body = [
        kwargs for _, url, kwargs in session.requests if url.endswith("/v1/clients")
    ][0]["json"]
    assert wire_body["spec"]["apiKey"] == secrets["access_key"]
    assert wire_body["spec"]["password"] == secrets["password"]
    assert wire_body["spec"]["cryptoKey"] == secrets["crypto_key"]
    assert result.client["initial_identify"]["access_key"] == secrets["access_key"]


def test_bootstrap_identity_file_round_trip_keeps_secrets(tmp_path):
    _, result, secrets = _bootstrap_mqtt_result()

    path = tmp_path / "_identity.json"
    path.write_text(
        json.dumps(result.identity.as_dict(include_secrets=True)), encoding="utf-8"
    )
    path.chmod(0o600)

    from thalovant import ThalovantIdentity

    loaded = ThalovantIdentity.from_file(path)
    assert loaded.access_key == secrets["access_key"]
    assert loaded.password == secrets["password"]
    assert loaded.crypto_key == secrets["crypto_key"]
    assert loaded.mqtt is not None
    assert loaded.mqtt.password == secrets["mqtt_password"]


def test_bootstrap_result_repr_is_redacted():
    _, result, secrets = _bootstrap_mqtt_result()

    for rendered in (repr(result), str(result), repr(result.identity), repr(result.identity.mqtt)):
        for name, value in secrets.items():
            assert value not in rendered, f"repr leaked {name}"
    # repr stays useful for the non-secret fields.
    assert "site_id='kiosk'" in repr(result.identity)


def test_bootstrap_result_keeps_spec_references_in_default_as_dict():
    session = FakeSession()
    api = ThalovantControlPlane(
        "https://dash.example.com/api", access_token="token", session=session
    )
    result = api.create_client_identity("hub-1", name="kiosk")

    redacted = result.as_dict()

    assert redacted["client"]["spec"]["apiKeyRef"] == {"name": "secret", "key": "apiKey"}
    serialized = json.dumps(redacted)
    for value in (
        result.identity.access_key,
        result.identity.password,
        result.identity.crypto_key,
    ):
        assert value not in serialized


HUB_RESOURCE = {
    "id": "hub-1",
    "name": "joke-garden",
    "slug": "joke-garden",
    "namespace": "tenant-1",
    "runtime_group_id": "rg-1",
    "active": True,
    "visibility": "private",
    "etag": "etag-2",
    "spec": {"protocols": {"wss": {"enabled": True}}},
}

RUNTIME_GROUP_RESOURCE = {
    "id": "rg-1",
    "tenant_id": "owner-1",
    "environment": "prod",
    "namespace": "tenant-1",
    "name": "kiosks",
    "slug": "kiosks",
    "is_default": False,
    "status": "pending",
    "spec": {"replicas": 2},
}

RUNTIME_GROUP_CONFIG = {
    "runtime_group_id": "rg-1",
    "config": {"lang": "en-us"},
    "personas": {"default": "friendly"},
}

DESIRED_SKILL = {
    "id": "desired-1",
    "runtime_group_id": "rg-1",
    "skill_id": "skill-weather",
    "source_type": "catalog",
    "source_ref": "skill-weather",
    "active": True,
}

RUNTIME_CAPABILITIES = {
    "hub_id": "hub-1",
    "client_name": "kiosk",
    "generated_at": "2026-08-13T00:00:00Z",
    "source": "ovos-runtime",
    "skills": [{"id": "skill-weather", "active": True, "total_intents": 3}],
    "intents": [],
    "counts": {"skills": 1, "adapt_intents": 2, "padatious_intents": 1, "total_intents": 3},
}

MARKETPLACE_SKILL = {
    "id": "mkt-1",
    "skill_id": "skill-weather",
    "catalog_scope": "global",
    "catalog_origin": "reference",
    "title": "Weather",
    "summary": "Forecasts and conditions.",
    "category": "information",
    "tags": ["weather"],
    "verified": True,
    "source_type": "package",
    "source_ref": "ovos-skill-weather",
    "access_tier": "included",
    "is_active": True,
}

RUNTIME_GROUP_MARKETPLACE = {
    "runtime_group_id": "rg-1",
    "observed_at": "2026-08-13T00:00:00Z",
    "source": "runtime-group-cache",
    "operator_phase": "Ready",
    "operator_message": None,
    "data": [
        {
            "skill_id": "skill-weather",
            "title": "Weather",
            "active": True,
            "installable": True,
            "purchase_required": False,
            "total_intents": 3,
        }
    ],
}

RUNTIME_GROUP_INVENTORY = {
    "runtime_group_id": "rg-1",
    "observed_at": "2026-08-13T00:00:00Z",
    "source": "ovos-runtime-operator",
    "operator_phase": "Ready",
    "operator_message": None,
    "data": [
        {
            "id": "inv-1",
            "runtime_group_id": "rg-1",
            "skill_id": "skill-weather",
            "version": "0.1.16",
            "source": "package",
            "active": True,
            "adapt_intents": ["weather.current"],
            "padatious_intents": [],
            "total_intents": 1,
            "observed_at": "2026-08-13T00:00:00Z",
        }
    ],
}

PROVISIONING_ROUTES = {
    ("GET", "/v1/marketplace/skills"): (200, {"data": [MARKETPLACE_SKILL]}),
    ("GET", "/v1/runtime-groups/rg-1/marketplace"): (200, RUNTIME_GROUP_MARKETPLACE),
    ("GET", "/v1/runtime-groups/rg-1/inventory"): (200, RUNTIME_GROUP_INVENTORY),
    ("POST", "/v1/hubs"): (201, HUB_RESOURCE),
    ("PATCH", "/v1/hubs/hub-1"): (200, HUB_RESOURCE),
    ("DELETE", "/v1/hubs/hub-1"): (204, ""),
    ("POST", "/v1/hubs/hub-1/release"): (200, HUB_RESOURCE),
    ("PUT", "/v1/hubs/hub-1/rating"): (200, HUB_RESOURCE),
    ("DELETE", "/v1/hubs/hub-1/rating"): (200, HUB_RESOURCE),
    ("GET", "/v1/hubs/hub-1/runtime-capabilities"): (200, RUNTIME_CAPABILITIES),
    ("GET", "/v1/runtime-groups"): (200, {"data": [RUNTIME_GROUP_RESOURCE]}),
    ("POST", "/v1/runtime-groups"): (201, RUNTIME_GROUP_RESOURCE),
    ("GET", "/v1/runtime-groups/rg-1"): (200, RUNTIME_GROUP_RESOURCE),
    ("PATCH", "/v1/runtime-groups/rg-1"): (200, RUNTIME_GROUP_RESOURCE),
    ("DELETE", "/v1/runtime-groups/rg-1"): (204, ""),
    ("GET", "/v1/runtime-groups/rg-1/config"): (200, RUNTIME_GROUP_CONFIG),
    ("PATCH", "/v1/runtime-groups/rg-1/config"): (200, RUNTIME_GROUP_CONFIG),
    ("POST", "/v1/runtime-groups/rg-1/release"): (200, RUNTIME_GROUP_RESOURCE),
    ("POST", "/v1/runtime-groups/rg-1/skills"): (200, DESIRED_SKILL),
    ("DELETE", "/v1/runtime-groups/rg-1/skills/skill-weather"): (204, ""),
}


class ProvisioningSession:
    """Fake session for the hub and runtime-group provisioning routes."""

    BASE_URL = "https://dash.example.com/api/"

    def __init__(self, error=None):
        self.requests = []
        self.error = error

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        assert kwargs["headers"]["authorization"] == "Bearer token"
        assert url.startswith(self.BASE_URL)
        if self.error is not None:
            return FakeResponse(*self.error)
        path = "/" + url[len(self.BASE_URL) :]
        if (method, path) not in PROVISIONING_ROUTES:
            raise AssertionError(f"{method} {path}")
        return FakeResponse(*PROVISIONING_ROUTES[(method, path)])


def provisioning_api(session):
    return ThalovantControlPlane(
        "https://dash.example.com/api",
        access_token="token",
        session=session,
    )


def recorded_call(session, method, path):
    """Return the kwargs of the one recorded request for ``method`` and ``path``."""

    matches = [
        kwargs
        for recorded_method, url, kwargs in session.requests
        if recorded_method == method and url == ProvisioningSession.BASE_URL + path.lstrip("/")
    ]
    if not matches:
        raise AssertionError(f"{method} {path} was not requested")
    assert len(matches) == 1
    return matches[0]


def test_control_plane_creates_hub_with_idempotency_key_and_snake_case_body():
    session = ProvisioningSession()
    api = provisioning_api(session)

    hub = api.create_hub(
        {
            "name": "joke-garden",
            "spec": {"protocols": {"wss": {"enabled": True}}},
            "ownerId": "owner-1",
            "runtimeGroupId": "rg-1",
            "capacityProfile": "standard",
            "visibility": "private",
        },
        idempotency_key="create-hub-1",
    )

    call = recorded_call(session, "POST", "/v1/hubs")
    assert call["json"] == {
        "name": "joke-garden",
        "spec": {"protocols": {"wss": {"enabled": True}}},
        "owner_id": "owner-1",
        "runtime_group_id": "rg-1",
        "capacity_profile": "standard",
        "visibility": "private",
    }
    assert call["headers"]["Idempotency-Key"] == "create-hub-1"
    assert hub["id"] == "hub-1"


def test_control_plane_create_hub_generates_an_idempotency_key():
    session = ProvisioningSession()

    provisioning_api(session).create_hub({"name": "joke-garden", "spec": {}})

    call = recorded_call(session, "POST", "/v1/hubs")
    assert call["headers"]["Idempotency-Key"]


def test_control_plane_update_and_delete_hub_send_if_match():
    session = ProvisioningSession()
    api = provisioning_api(session)

    updated = api.update_hub("hub-1", {"active": False, "isLocked": False}, etag="etag-1")
    api.delete_hub("hub-1", etag="etag-2")

    patch_call = recorded_call(session, "PATCH", "/v1/hubs/hub-1")
    assert patch_call["json"] == {"active": False, "is_locked": False}
    assert patch_call["headers"]["If-Match"] == "etag-1"
    delete_call = recorded_call(session, "DELETE", "/v1/hubs/hub-1")
    assert delete_call["headers"]["If-Match"] == "etag-2"
    assert delete_call["json"] is None
    assert updated["etag"] == "etag-2"


def test_control_plane_update_hub_forwards_the_immutable_fields_untouched():
    """``name``/``namespace``/``domain`` are the API's to reject, not the SDK's.

    The API drops them when they match the stored hub and answers HTTP 400 only
    for a changed value. The SDK cannot tell those apart without another read,
    so refusing them client-side would reject patches the API accepts.
    """

    session = ProvisioningSession()

    provisioning_api(session).update_hub(
        "hub-1",
        {"name": "joke-garden", "namespace": "tenant-1", "domain": "", "active": False},
        etag="etag-1",
    )

    assert recorded_call(session, "PATCH", "/v1/hubs/hub-1")["json"] == {
        "name": "joke-garden",
        "namespace": "tenant-1",
        "domain": "",
        "active": False,
    }


def test_control_plane_update_hub_surfaces_immutable_field_rejection():
    session = ProvisioningSession(
        error=(400, {"detail": "Name cannot be changed after hub creation"})
    )

    with pytest.raises(ThalovantAPIError, match="HTTP 400: Name cannot be changed"):
        provisioning_api(session).update_hub("hub-1", {"name": "renamed"}, etag="etag-1")


def test_control_plane_surfaces_the_per_skill_marketplace_payment_gate():
    """The per-skill 402 is distinct from the plan-level API gate."""

    session = ProvisioningSession(
        error=(402, {"detail": "This skill requires paid marketplace access for the tenant plan."})
    )

    with pytest.raises(ThalovantAPIError, match="HTTP 402: This skill requires paid marketplace"):
        provisioning_api(session).install_runtime_group_skill("rg-1", "skill-weather")


def test_control_plane_update_hub_surfaces_etag_mismatch():
    session = ProvisioningSession(error=(412, {"detail": "ETag mismatch"}))
    api = provisioning_api(session)

    with pytest.raises(ThalovantAPIError, match="HTTP 412: ETag mismatch"):
        api.update_hub("hub-1", {"active": False}, etag="stale")


def test_api_error_message_never_echoes_a_structured_error_body():
    """A 4xx that echoes the request body back (as FastAPI-style validation
    detail does) must not launder the /v1/clients credentials into logs."""

    echoed = {
        "detail": [
            {
                "loc": ["body", "spec", "apiKey"],
                "msg": "value is invalid",
                "input": "SECRET-API-KEY-VALUE",
            }
        ]
    }

    class EchoSession:
        def request(self, method, url, **kwargs):
            return FakeResponse(422, echoed)

    api = ThalovantControlPlane(
        "https://dash.example.com/api", access_token="token", session=EchoSession()
    )

    with pytest.raises(ThalovantAPIError) as excinfo:
        api.create_client({"hub_id": "hub-1", "name": "kiosk", "spec": {"apiKey": "SECRET-API-KEY-VALUE"}})

    message = str(excinfo.value)
    assert "SECRET-API-KEY-VALUE" not in message
    assert message == "Thalovant API request failed with HTTP 422."


def test_api_error_message_is_bounded_and_newline_free():
    huge = "poisoned\ndetail " * 500
    session = ProvisioningSession(error=(400, {"detail": huge}))

    with pytest.raises(ThalovantAPIError) as excinfo:
        provisioning_api(session).create_hub({"name": "joke-garden", "spec": {}})

    message = str(excinfo.value)
    assert "\n" not in message
    assert "\r" not in message
    assert len(message) < 300
    assert message.startswith("Thalovant API request failed with HTTP 400: poisoned detail")


def test_api_error_message_omits_non_json_bodies():
    session = ProvisioningSession(
        error=(500, "<html>Internal error for /v1/clients?authorization=SECRET</html>")
    )

    with pytest.raises(ThalovantAPIError) as excinfo:
        provisioning_api(session).create_hub({"name": "joke-garden", "spec": {}})

    assert str(excinfo.value) == "Thalovant API request failed with HTTP 500."


def test_control_plane_release_hub_sends_only_the_options_given():
    session = ProvisioningSession()
    api = provisioning_api(session)

    api.release_hub("hub-1", channel="stable")
    assert recorded_call(session, "POST", "/v1/hubs/hub-1/release")["json"] == {
        "channel": "stable"
    }

    images_session = ProvisioningSession()
    provisioning_api(images_session).release_hub(
        "hub-1",
        channel="stable",
        mode="custom",
        version="1.4.0",
        images={"listener": "ghcr.io/thalovant/listener:1.4.0"},
        reason="pin the kiosk fleet",
    )
    assert recorded_call(images_session, "POST", "/v1/hubs/hub-1/release")["json"] == {
        "channel": "stable",
        "mode": "custom",
        "version": "1.4.0",
        "images": {"listener": "ghcr.io/thalovant/listener:1.4.0"},
        "reason": "pin the kiosk fleet",
    }


def test_control_plane_sets_and_clears_a_hub_rating():
    session = ProvisioningSession()
    api = provisioning_api(session)

    rated = api.set_hub_rating("hub-1", 5)
    cleared = api.clear_hub_rating("hub-1")

    assert recorded_call(session, "PUT", "/v1/hubs/hub-1/rating")["json"] == {"rating": 5}
    assert recorded_call(session, "DELETE", "/v1/hubs/hub-1/rating")["json"] is None
    assert rated["id"] == "hub-1"
    assert cleared["id"] == "hub-1"


def test_control_plane_reads_hub_runtime_capabilities():
    session = ProvisioningSession()

    capabilities = provisioning_api(session).get_hub_runtime_capabilities("hub-1")

    recorded_call(session, "GET", "/v1/hubs/hub-1/runtime-capabilities")
    assert capabilities["counts"]["total_intents"] == 3
    assert capabilities["skills"][0]["id"] == "skill-weather"


def test_control_plane_manages_runtime_groups():
    session = ProvisioningSession()
    api = provisioning_api(session)

    page = api.list_runtime_groups(owner_id="owner-1")
    created = api.create_runtime_group(
        {
            "name": "kiosks",
            "description": "Lobby kiosks",
            "environment": "prod",
            "ownerId": "owner-1",
            "cloneFromDefault": True,
        }
    )
    fetched = api.get_runtime_group("rg-1")
    updated = api.update_runtime_group("rg-1", {"name": "kiosks-eu", "spec": {"replicas": 2}})
    api.release_runtime_group("rg-1", channel="stable")
    api.delete_runtime_group("rg-1")

    assert recorded_call(session, "GET", "/v1/runtime-groups")["params"] == {
        "owner_id": "owner-1"
    }
    assert recorded_call(session, "POST", "/v1/runtime-groups")["json"] == {
        "name": "kiosks",
        "description": "Lobby kiosks",
        "environment": "prod",
        "owner_id": "owner-1",
        "clone_from_default": True,
    }
    assert recorded_call(session, "PATCH", "/v1/runtime-groups/rg-1")["json"] == {
        "name": "kiosks-eu",
        "spec": {"replicas": 2},
    }
    assert recorded_call(session, "POST", "/v1/runtime-groups/rg-1/release")["json"] == {
        "channel": "stable"
    }
    assert recorded_call(session, "DELETE", "/v1/runtime-groups/rg-1")["json"] is None
    assert page["data"][0]["id"] == "rg-1"
    assert created["id"] == "rg-1"
    assert fetched["name"] == "kiosks"
    assert updated["status"] == "pending"


def test_control_plane_reads_and_merges_runtime_group_config():
    session = ProvisioningSession()
    api = provisioning_api(session)

    current = api.get_runtime_group_config("rg-1")
    merged = api.update_runtime_group_config(
        "rg-1",
        {"lang": "en-us"},
        personas={"default": "friendly"},
    )
    without_personas = ProvisioningSession()
    provisioning_api(without_personas).update_runtime_group_config("rg-1", {"lang": "fr-fr"})

    recorded_call(session, "GET", "/v1/runtime-groups/rg-1/config")
    assert recorded_call(session, "PATCH", "/v1/runtime-groups/rg-1/config")["json"] == {
        "config": {"lang": "en-us"},
        "personas": {"default": "friendly"},
    }
    assert recorded_call(without_personas, "PATCH", "/v1/runtime-groups/rg-1/config")["json"] == {
        "config": {"lang": "fr-fr"}
    }
    assert current["config"] == {"lang": "en-us"}
    assert merged["personas"] == {"default": "friendly"}


def test_control_plane_installs_and_uninstalls_a_runtime_group_skill():
    session = ProvisioningSession()
    api = provisioning_api(session)

    installed = api.install_runtime_group_skill("rg-1", "skill-weather")
    api.uninstall_runtime_group_skill("rg-1", "skill-weather")

    assert recorded_call(session, "POST", "/v1/runtime-groups/rg-1/skills")["json"] == {
        "skill_id": "skill-weather",
        "source_type": "catalog",
        "active": True,
    }
    uninstall = recorded_call(session, "DELETE", "/v1/runtime-groups/rg-1/skills/skill-weather")
    assert uninstall["json"] is None
    assert installed["skill_id"] == "skill-weather"


def test_control_plane_installs_a_git_skill_with_every_option():
    session = ProvisioningSession()

    provisioning_api(session).install_runtime_group_skill(
        "rg-1",
        "skill-lobby",
        marketplace_skill_id="marketplace-1",
        source_type="git",
        source_ref="https://github.com/acme/skill-lobby",
        version_pin="v1.2.0",
        active=False,
    )

    assert recorded_call(session, "POST", "/v1/runtime-groups/rg-1/skills")["json"] == {
        "skill_id": "skill-lobby",
        "source_type": "git",
        "active": False,
        "marketplace_skill_id": "marketplace-1",
        "source_ref": "https://github.com/acme/skill-lobby",
        "version_pin": "v1.2.0",
    }


def test_control_plane_surfaces_the_paid_plan_gate_as_an_api_error():
    session = ProvisioningSession(error=(402, {"detail": "API access requires a paid plan."}))
    api = provisioning_api(session)

    with pytest.raises(ThalovantAPIError, match="HTTP 402: API access requires a paid plan."):
        api.create_hub({"name": "joke-garden", "spec": {}})
    with pytest.raises(ThalovantAPIError, match="HTTP 402"):
        api.create_runtime_group({"name": "kiosks"})


def test_control_plane_surfaces_the_scope_gate_as_an_api_error():
    session = ProvisioningSession(error=(403, {"detail": "Insufficient scopes"}))
    api = provisioning_api(session)

    with pytest.raises(ThalovantAPIError, match="HTTP 403: Insufficient scopes"):
        api.install_runtime_group_skill("rg-1", "skill-weather")
    with pytest.raises(ThalovantAPIError, match="HTTP 403: Insufficient scopes"):
        api.delete_hub("hub-1", etag="etag-2")


def test_control_plane_lists_marketplace_skills_without_optional_params():
    session = ProvisioningSession()
    api = provisioning_api(session)

    catalog = api.list_marketplace_skills()

    assert catalog["data"][0]["skill_id"] == "skill-weather"
    assert recorded_call(session, "GET", "/v1/marketplace/skills")["params"] == {}


def test_control_plane_lists_marketplace_skills_with_every_param():
    session = ProvisioningSession()
    api = provisioning_api(session)

    api.list_marketplace_skills(
        owner_id="owner-1",
        include_inactive=True,
        force_refresh=True,
    )

    assert recorded_call(session, "GET", "/v1/marketplace/skills")["params"] == {
        "owner_id": "owner-1",
        "include_inactive": "true",
        "force_refresh": "true",
    }


def test_control_plane_marketplace_skills_omits_false_and_blank_params():
    session = ProvisioningSession()
    api = provisioning_api(session)

    api.list_marketplace_skills(owner_id="  ", include_inactive=False, force_refresh=False)

    assert recorded_call(session, "GET", "/v1/marketplace/skills")["params"] == {}


def test_control_plane_lists_runtime_group_marketplace():
    session = ProvisioningSession()
    api = provisioning_api(session)

    view = api.list_runtime_group_marketplace("rg-1")

    assert view["source"] == "runtime-group-cache"
    assert view["data"][0]["installable"] is True
    assert recorded_call(session, "GET", "/v1/runtime-groups/rg-1/marketplace")["params"] == {}


def test_control_plane_runtime_group_marketplace_sends_refresh_inventory():
    session = ProvisioningSession()
    api = provisioning_api(session)

    api.list_runtime_group_marketplace("rg-1", refresh_inventory=True)

    assert recorded_call(session, "GET", "/v1/runtime-groups/rg-1/marketplace")["params"] == {
        "refresh_inventory": "true"
    }


def test_control_plane_lists_runtime_group_inventory():
    session = ProvisioningSession()
    api = provisioning_api(session)

    inventory = api.list_runtime_group_inventory("rg-1")

    assert inventory["source"] == "ovos-runtime-operator"
    assert inventory["data"][0]["total_intents"] == 1
    assert recorded_call(session, "GET", "/v1/runtime-groups/rg-1/inventory")["params"] == {}


def test_control_plane_runtime_group_inventory_sends_refresh():
    session = ProvisioningSession()
    api = provisioning_api(session)

    api.list_runtime_group_inventory("rg-1", refresh=True)

    assert recorded_call(session, "GET", "/v1/runtime-groups/rg-1/inventory")["params"] == {
        "refresh": "true"
    }


def test_control_plane_discovery_reads_surface_the_inspect_scope_gate():
    session = ProvisioningSession(error=(403, {"detail": "Insufficient scopes"}))
    api = provisioning_api(session)

    with pytest.raises(ThalovantAPIError, match="HTTP 403: Insufficient scopes"):
        api.list_runtime_group_marketplace("rg-1")
    with pytest.raises(ThalovantAPIError, match="HTTP 403: Insufficient scopes"):
        api.list_runtime_group_inventory("rg-1")


def test_control_plane_discovery_reads_surface_a_missing_runtime_group():
    session = ProvisioningSession(error=(404, {"detail": "Runtime group not found."}))
    api = provisioning_api(session)

    with pytest.raises(ThalovantAPIError, match="HTTP 404: Runtime group not found."):
        api.list_runtime_group_inventory("rg-missing")
    with pytest.raises(ThalovantAPIError, match="HTTP 404"):
        api.list_runtime_group_marketplace("rg-missing")


DEVICE_GRANT = {
    "device_code": "device-code-1",
    "user_code": "WDJB-MJHT",
    "verification_uri": "https://dash.thalovant.com/activate",
    "verification_uri_complete": "https://dash.thalovant.com/activate?user_code=WDJB-MJHT",
    "expires_in": 900,
    "interval": 0,
}

DEVICE_TOKEN = {
    "access_token": "device-token",
    "token_type": "bearer",
    "scopes": ["hubs:read", "clients:write"],
    "expires_at": "2027-08-13T00:00:00Z",
    "token_id": "token-1",
}


class DeviceFlowSession:
    """Scripted fake session for the device-flow endpoints."""

    def __init__(self, token_responses):
        self.requests = []
        self.token_responses = list(token_responses)

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        assert method == "POST"
        assert "authorization" not in kwargs["headers"]
        if url.endswith("/v1/auth/device/authorize"):
            return FakeResponse(200, dict(DEVICE_GRANT))
        if url.endswith("/v1/auth/device/token"):
            assert kwargs["json"] == {"device_code": "device-code-1"}
            return FakeResponse(*self.token_responses.pop(0))
        raise AssertionError(url)


def test_control_plane_login_with_browser_polls_until_token(monkeypatch, capsys):
    session = DeviceFlowSession(
        [
            (400, {"error": "authorization_pending"}),
            (400, {"error": "authorization_pending"}),
            (200, dict(DEVICE_TOKEN)),
        ]
    )
    opened = []
    monkeypatch.setattr("thalovant.control.webbrowser.open", opened.append)
    api = ThalovantControlPlane("https://dash.example.com/api", session=session)

    token = api.login_with_browser(scopes=["hubs:read"], client_name="pytest")

    assert token == DEVICE_TOKEN
    assert api.access_token == "device-token"
    assert opened == ["https://dash.thalovant.com/activate?user_code=WDJB-MJHT"]
    _, _, authorize = session.requests[0]
    assert authorize["json"] == {"scopes": ["hubs:read"], "client_name": "pytest"}
    assert len(session.requests) == 4
    out = capsys.readouterr().out
    assert (
        "To sign in, visit https://dash.thalovant.com/activate "
        "and enter the code WDJB-MJHT" in out
    )


def test_control_plane_login_with_browser_custom_prompt_and_no_browser(monkeypatch):
    session = DeviceFlowSession([(200, dict(DEVICE_TOKEN))])
    monkeypatch.setattr(
        "thalovant.control.webbrowser.open",
        lambda url: pytest.fail("browser must not open"),
    )
    api = ThalovantControlPlane("https://dash.example.com/api", session=session)
    grants = []

    api.login_with_browser(open_browser=False, prompt=grants.append)

    assert grants == [DEVICE_GRANT]
    _, _, authorize = session.requests[0]
    assert authorize["json"] == {}


def test_control_plane_device_poll_slow_down_grows_interval():
    session = DeviceFlowSession(
        [
            (400, {"error": "authorization_pending"}),
            (400, {"error": "slow_down"}),
            (400, {"error": "authorization_pending"}),
            (200, dict(DEVICE_TOKEN)),
        ]
    )
    api = ThalovantControlPlane("https://dash.example.com/api", session=session)
    sleeps = []

    token = api._poll_device_token(
        "device-code-1",
        interval=5.0,
        timeout=900.0,
        sleep=sleeps.append,
        clock=lambda: 0.0,
    )

    assert token == DEVICE_TOKEN
    assert sleeps == [5.0, 10.0, 10.0]


def test_control_plane_login_with_browser_raises_on_access_denied():
    session = DeviceFlowSession([(400, {"error": "access_denied"})])
    api = ThalovantControlPlane("https://dash.example.com/api", session=session)

    with pytest.raises(ThalovantAPIError, match="denied"):
        api.login_with_browser(open_browser=False, prompt=lambda grant: None)
    assert api.access_token is None


def test_control_plane_login_with_browser_raises_on_expired_token():
    session = DeviceFlowSession([(400, {"error": "expired_token"})])
    api = ThalovantControlPlane("https://dash.example.com/api", session=session)

    with pytest.raises(ThalovantAPIError, match="expired.*again"):
        api.login_with_browser(open_browser=False, prompt=lambda grant: None)
    assert api.access_token is None


def test_control_plane_device_poll_times_out():
    session = DeviceFlowSession([(400, {"error": "authorization_pending"})] * 3)
    api = ThalovantControlPlane("https://dash.example.com/api", session=session)
    now = {"value": 0.0}

    def clock():
        return now["value"]

    def sleep(seconds):
        now["value"] += seconds

    with pytest.raises(ThalovantTimeoutError, match="Timed out"):
        api._poll_device_token("device-code-1", interval=5.0, timeout=10.0, sleep=sleep, clock=clock)
    assert len(session.requests) == 3
    assert now["value"] == 10.0
