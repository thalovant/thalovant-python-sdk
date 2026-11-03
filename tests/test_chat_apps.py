"""Chat apps: the connector catalog, the allowance, and the owner routes under ``/v1/bridges``."""

from __future__ import annotations

import asyncio
import copy
import json
import pickle
from typing import get_args

import pytest

from thalovant import (
    AsyncThalovantControlPlane,
    Bridge,
    BridgeAllowedChat,
    BridgeAvatar,
    BridgeChatList,
    BridgeHoldReason,
    BridgeNetworks,
    BridgePhase,
    BridgeStatusReason,
    BridgeStatusSource,
    ChatAppConnector,
    ChatAppHelper,
    ChatAppStatus,
    LocalizedText,
    OperationResource,
    SecretValue,
    ThalovantAPIError,
    ThalovantAuthError,
    ThalovantControlPlane,
    ThalovantPlanError,
)
from thalovant.chat_apps import MAX_BRIDGE_AVATAR_BYTES

BASE_URL = "https://api.example.com/"
BRIDGE_ID = "6b1d6c1e-6f0b-4f43-9d7e-3f4f0d9a7c11"
HUB_ID = "0f8a7c3e-2b1d-4c5e-9a6f-1d2c3b4a5e6f"
OWNER_ID = "3c2b1a0f-9e8d-4c7b-a6f5-e4d3c2b1a0f9"
#: Stand-ins for credentials: any test that finds one of these in output failed.
FAKE_TOKEN = "123456:FAKE-telegram-token-value"
FAKE_ACCESS = "syt_FAKE_matrix_access_token"
FAKE_REFRESH = "FAKE-twitch-refresh-token"
FAKE_CLIENT_SECRET = "FAKE-twitch-client-secret"


def bridge_payload(**overrides):
    payload = {
        "id": BRIDGE_ID,
        "hub_id": HUB_ID,
        "owner_id": OWNER_ID,
        "network": "telegram",
        "connector": {"id": "telegram", "name": "Telegram", "version": "1.4.0"},
        "settings": {},
        "secrets_set": ["token"],
        "state": "active",
        "resource_name": f"bridge-{BRIDGE_ID}",
        "bot": {
            "user_id": "123456",
            "username": "kitchen_hub_bot",
            "display_name": "Kitchen hub",
            "application_id": None,
            "avatar_url": f"/v1/bridges/{BRIDGE_ID}/avatar",
        },
        "invite_links": {
            "add_to_group": "https://t.me/kitchen_hub_bot?startgroup=thalovant",
            "direct_chat": "https://t.me/kitchen_hub_bot",
        },
        "allowed": [{"id": "-1001234567890", "title": "Family", "kind": "supergroup"}],
        "require_mention": True,
        "session_scope": "person",
        "paused": False,
        "hold": None,
        "status": {
            "phase": "Ready",
            "ready": True,
            "reason": "Connected",
            "message": None,
            "operator_reason": None,
            "generation": 3,
            "observed_generation": 3,
            "applied": True,
            "source": "operator",
        },
        "etag": "etag-1",
        "token_updated_at": "2026-11-03T10:00:00Z",
        "activated_at": "2026-11-03T10:05:00Z",
        "created_at": "2026-11-03T10:00:00Z",
        "updated_at": "2026-11-03T10:05:00Z",
        "operation": None,
    }
    payload.update(overrides)
    return payload


CONNECTORS = {
    "data": [
        {
            "id": "telegram",
            "name": "Telegram",
            "mark": "telegram",
            "status": "offered",
            "description": {"en": "Answer in Telegram groups.", "fr": "Répondre dans Telegram."},
            "setup_steps": {"en": "1. Talk to BotFather.", "fr": "1. Parlez à BotFather."},
            "config_schema": {"type": "object", "properties": {}},
            "secrets": [{"key": "token", "label": {"en": "Bot token", "fr": "Jeton du bot"}, "required": True}],
            "targets": {"label": {"en": "Chats"}, "pattern": "^-?[0-9]{1,20}$", "max": 50},
            "options": {"require_mention_fixed": False, "session_scopes": ["person", "chat"]},
            "helpers": ["validate", "discover", "test_message", "avatar"],
            "offered": True,
            "version": {"version": "1.4.0", "released_at": "2026-11-02T12:00:00Z"},
        },
        {
            "id": "twitch",
            "name": "Twitch",
            "mark": "twitch",
            "status": "beta",
            "description": {"en": "Answer in Twitch chat."},
            "setup_steps": {"en": "Register an application."},
            "config_schema": {
                "type": "object",
                "properties": {
                    "bot_login": {"type": "string", "title": "Bot login", "x-title-fr": "Identifiant du bot"},
                    "client_id": {"type": "string", "title": "Client ID"},
                },
                "required": ["bot_login", "client_id"],
            },
            "secrets": [
                {"key": "refresh_token", "label": {"en": "Refresh token"}},
                {"key": "client_secret", "label": {"en": "Client secret"}, "description": {"en": "From the console."}},
            ],
            "targets": {
                "label": {"en": "Channels"},
                "pattern": "^[a-z0-9_]{4,25}$",
                "max": 50,
                "lowercase": True,
                "strip_prefix": "#",
            },
            "options": {"require_mention_fixed": False},
            "helpers": [],
            "offered": False,
            "version": None,
            "brand_new_field": {"kept": "out of the model, harmlessly"},
        },
    ]
}

NETWORKS = {
    "networks": [
        {"network": "telegram", "offered": True, "name": "Telegram", "mark": "telegram", "status": "offered"},
        {"network": "matrix", "offered": True, "name": "Matrix", "mark": "matrix", "status": "beta"},
    ],
    "allowance": {
        "owner_id": OWNER_ID,
        "plan": "Home",
        "paid_plan": True,
        "limit": 2,
        "used": 1,
        "remaining": 1,
        "can_create": True,
    },
}


class FakeResponse:
    def __init__(self, status_code, body, *, content=b"", headers=None):
        self.status_code = status_code
        self._body = body
        self.text = "" if body is None else json.dumps(body)
        self.content = content
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class BridgeSession:
    """A requests-style session: routes map to responses, every request is kept."""

    def __init__(self, routes):
        self.requests = []
        self.routes = dict(routes)

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        assert kwargs["headers"]["authorization"] == "Bearer token"
        assert url.startswith(BASE_URL)
        path = "/" + url[len(BASE_URL):]
        if (method, path) not in self.routes:
            raise AssertionError(f"{method} {path}")
        response = self.routes[(method, path)]
        return response if isinstance(response, FakeResponse) else FakeResponse(*response)


def api_for(session):
    return ThalovantControlPlane("https://api.example.com", access_token="token", session=session)


def only(session):
    assert len(session.requests) == 1
    return session.requests[0]


def test_value_sets_match_the_api():
    assert set(get_args(BridgeStatusReason)) == {
        "Setup", "Starting", "Connected", "Paused", "Unavailable", "TokenMissing", "TokenRejected",
    }
    assert set(get_args(BridgePhase)) == {"Setup", "Pending", "Ready", "Degraded", "Paused", "Error"}
    assert set(get_args(BridgeStatusSource)) == {"lifecycle", "operator", "watcher", "unavailable"}
    assert set(get_args(BridgeHoldReason)) == {"plan_limit", "account_suspended"}
    assert set(get_args(ChatAppStatus)) == {"offered", "beta", "hidden"}
    assert set(get_args(ChatAppHelper)) == {"validate", "discover", "test_message", "avatar"}


def test_list_chat_app_connectors_reads_the_catalog():
    session = BridgeSession({("GET", "/v1/bridges/connectors"): (200, CONNECTORS)})

    telegram, twitch = api_for(session).list_chat_app_connectors()

    assert isinstance(telegram, ChatAppConnector)
    assert telegram.status == "offered" and telegram.offered is True
    assert telegram.setup_steps.text("fr-CA") == "1. Parlez à BotFather."
    assert telegram.setup_steps.text("en") == "1. Talk to BotFather."
    assert [secret.key for secret in telegram.secrets] == ["token"]
    assert telegram.targets.pattern == "^-?[0-9]{1,20}$"
    assert telegram.helpers == ("validate", "discover", "test_message", "avatar")
    assert telegram.version.version == "1.4.0"

    assert twitch.status == "beta" and twitch.offered is False and twitch.version is None
    assert twitch.description.text("fr") == "Answer in Twitch chat."  # no French: English
    assert [(s.key, s.required) for s in twitch.secrets] == [("refresh_token", True), ("client_secret", True)]
    assert twitch.secrets[1].description.en == "From the console."
    assert twitch.targets.lowercase is True and twitch.targets.strip_prefix == "#"
    assert twitch.config_schema["properties"]["bot_login"]["x-title-fr"] == "Identifiant du bot"
    assert twitch.options.session_scopes == ("person", "chat")
    assert twitch.as_dict()["targets"]["max"] == 50


def test_get_bridge_networks_reads_the_allowance_and_sends_hub_id():
    session = BridgeSession({("GET", "/v1/bridges/networks"): (200, NETWORKS)})

    networks = api_for(session).get_bridge_networks(HUB_ID)

    assert isinstance(networks, BridgeNetworks)
    assert [(n.network, n.offered, n.status) for n in networks.networks] == [
        ("telegram", True, "offered"), ("matrix", True, "beta"),
    ]
    allowance = networks.allowance
    assert (allowance.plan, allowance.limit, allowance.used, allowance.remaining) == ("Home", 2, 1, 1)
    assert allowance.can_create is True and allowance.paid_plan is True
    assert only(session)[2]["params"] == {"hub_id": HUB_ID}


def test_get_bridge_networks_without_hub_sends_no_query():
    session = BridgeSession({("GET", "/v1/bridges/networks"): (200, NETWORKS)})

    api_for(session).get_bridge_networks()

    assert only(session)[2]["params"] is None


def test_list_bridges_parses_every_field_and_filters():
    held = bridge_payload(
        paused=True,
        hold={"reason": "plan_limit", "since": "2026-11-03T11:00:00Z"},
        status={"phase": "Paused", "reason": "Paused", "source": "operator", "applied": True},
    )
    session = BridgeSession({("GET", "/v1/bridges"): (200, {"data": [bridge_payload(), held]})})

    running, paused = api_for(session).list_bridges(HUB_ID, owner_id=OWNER_ID, limit=50)

    assert only(session)[2]["params"] == {"hub_id": HUB_ID, "owner_id": OWNER_ID, "limit": 50}
    assert isinstance(running, Bridge)
    assert running.connector.version == "1.4.0"
    assert running.status.reason == "Connected" and running.status.ready is True
    assert running.status.message is None and running.status.operator_reason is None
    assert running.bot.username == "kitchen_hub_bot"
    assert running.invite_links.direct_chat == "https://t.me/kitchen_hub_bot"
    assert running.allowed == (BridgeAllowedChat(id="-1001234567890", title="Family", kind="supergroup"),)
    assert running.secrets_set == ("token",)
    assert running.hold is None and running.operation is None
    assert paused.paused is True
    assert paused.hold.reason == "plan_limit" and paused.hold.since == "2026-11-03T11:00:00Z"
    assert paused.status.ready is False


def test_list_bridges_refuses_a_blank_filter_and_a_bad_limit_before_sending():
    session = BridgeSession({})
    api = api_for(session)

    for call in (
        lambda: api.list_bridges(""),
        lambda: api.list_bridges(owner_id="  "),
        lambda: api.list_bridges(limit=0),
        lambda: api.list_bridges(limit=501),
        lambda: api.list_bridges(limit=True),
        lambda: api.get_bridge_networks(""),
    ):
        with pytest.raises(ValueError):
            call()
    assert session.requests == []


def test_unknown_values_and_missing_optional_fields_survive():
    sparse = {
        "id": BRIDGE_ID,
        "hub_id": HUB_ID,
        "network": "signal",
        "state": "migrating",
        "session_scope": "thread",
        "bot": {"user_id": f"bridge:{BRIDGE_ID}"},
        "status": {"phase": "Hibernating", "reason": "CredentialsExpiring"},
        "etag": "e",
        "hold": {"reason": "fraud_review"},
    }
    session = BridgeSession({("GET", f"/v1/bridges/{BRIDGE_ID}"): (200, sparse)})

    bridge = api_for(session).get_bridge(BRIDGE_ID)

    assert bridge.network == "signal" and bridge.state == "migrating" and bridge.session_scope == "thread"
    assert bridge.status.phase == "Hibernating" and bridge.status.reason == "CredentialsExpiring"
    assert bridge.status.source == "unavailable" and bridge.status.applied is None
    assert bridge.hold.reason == "fraud_review" and bridge.hold.since is None
    assert bridge.connector is None and bridge.invite_links is None
    assert bridge.allowed == () and bridge.settings == {} and bridge.secrets_set == ()
    assert bridge.require_mention is True and bridge.paused is False


@pytest.mark.parametrize("missing", ["id", "hub_id", "network", "state", "etag", "session_scope", "status", "bot"])
def test_a_bridge_without_a_required_member_is_an_unexpected_shape(missing):
    payload = bridge_payload()
    del payload[missing]
    session = BridgeSession({("GET", f"/v1/bridges/{BRIDGE_ID}"): (200, payload)})

    with pytest.raises(ThalovantAPIError) as raised:
        api_for(session).get_bridge(BRIDGE_ID)
    assert raised.value.status_code is None


def test_a_listing_that_is_not_a_list_is_refused():
    session = BridgeSession({("GET", "/v1/bridges"): (200, {"data": {"id": BRIDGE_ID}})})

    with pytest.raises(ThalovantAPIError, match="unexpected bridge listing shape"):
        api_for(session).list_bridges()


def test_create_bridge_with_a_telegram_token():
    created = bridge_payload(state="setup", allowed=[], status={"phase": "Setup", "reason": "Setup", "source": "lifecycle"})
    session = BridgeSession({("POST", "/v1/bridges"): (201, created)})

    bridge = api_for(session).create_bridge(HUB_ID, "telegram", token=SecretValue(FAKE_TOKEN))

    assert bridge.state == "setup" and bridge.status.reason == "Setup"
    assert only(session)[2]["json"] == {"hub_id": HUB_ID, "connector": "telegram", "token": FAKE_TOKEN}


def test_create_bridge_with_matrix_secrets_and_settings():
    session = BridgeSession({("POST", "/v1/bridges"): (201, bridge_payload(network="matrix", invite_links=None))})
    settings = {"homeserver": "https://matrix.example.org", "user_id": "@hub:example.org"}

    bridge = api_for(session).create_bridge(
        HUB_ID, "matrix", secrets={"access_token": SecretValue(FAKE_ACCESS)}, settings=settings,
    )

    assert bridge.invite_links is None
    assert only(session)[2]["json"] == {
        "hub_id": HUB_ID,
        "connector": "matrix",
        "secrets": {"access_token": FAKE_ACCESS},
        "settings": settings,
    }


def test_create_bridge_with_twitch_secrets_as_plain_strings():
    session = BridgeSession({("POST", "/v1/bridges"): (201, bridge_payload(network="twitch"))})

    api_for(session).create_bridge(
        HUB_ID, "twitch",
        secrets={"refresh_token": FAKE_REFRESH, "client_secret": SecretValue(FAKE_CLIENT_SECRET)},
        settings={"bot_login": "kitchenhub", "client_id": "abc123"},
    )

    body = only(session)[2]["json"]
    assert body["secrets"] == {"refresh_token": FAKE_REFRESH, "client_secret": FAKE_CLIENT_SECRET}
    assert body["settings"] == {"bot_login": "kitchenhub", "client_id": "abc123"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"token": FAKE_TOKEN, "secrets": {"token": FAKE_TOKEN}},
        {"token": 12345},
        {"secrets": {"access_token": ["not", "a", "string"]}},
        {"token": FAKE_TOKEN, "settings": {"password": SecretValue(FAKE_TOKEN)}},
    ],
)
def test_create_bridge_refuses_bad_credentials_locally_without_echoing_them(kwargs):
    session = BridgeSession({})

    with pytest.raises(ValueError) as raised:
        api_for(session).create_bridge(HUB_ID, "telegram", **kwargs)

    assert FAKE_TOKEN not in str(raised.value)
    assert session.requests == []


def test_update_bridge_sends_only_what_was_set_and_if_match_when_given():
    chat_list = BridgeChatList.from_dict({
        "network": "discord",
        "data": [{"id": "111", "title": "general", "kind": "text", "guild_id": "999", "guild_name": "Home"}],
    })
    updated = bridge_payload(etag="etag-2", operation={
        "id": "op-1", "kind": "bridge.sync", "aggregate_type": "bridge", "aggregate_id": BRIDGE_ID,
        "status": "requested", "created_at": "2026-11-03T10:06:00Z", "updated_at": "2026-11-03T10:06:00Z",
    })
    session = BridgeSession({("PATCH", f"/v1/bridges/{BRIDGE_ID}"): (200, updated)})

    bridge = api_for(session).update_bridge(
        BRIDGE_ID,
        allowed=[chat_list.data[0], "-100555", {"id": "-100777", "title": None}, BridgeAllowedChat(id="-100888")],
        session_scope="chat",
        etag="etag-1",
    )

    _, _, kwargs = only(session)
    assert kwargs["json"] == {
        "allowed": [
            {"id": "111", "title": "general", "kind": "text", "guild_id": "999", "guild_name": "Home"},
            {"id": "-100555"},
            {"id": "-100777"},
            {"id": "-100888"},
        ],
        "session_scope": "chat",
    }
    assert kwargs["headers"]["If-Match"] == "etag-1"
    assert bridge.etag == "etag-2"
    assert isinstance(bridge.operation, OperationResource) and bridge.operation.kind == "bridge.sync"


def test_pause_and_resume_send_paused_without_if_match():
    session = BridgeSession({("PATCH", f"/v1/bridges/{BRIDGE_ID}"): (200, bridge_payload(paused=True))})
    api = api_for(session)

    api.update_bridge(BRIDGE_ID, paused=True)
    api.update_bridge(BRIDGE_ID, paused=False, require_mention=False, settings={"homeserver": "https://m.example.org"})

    first, second = (kwargs for _, _, kwargs in session.requests)
    assert first["json"] == {"paused": True}
    assert "If-Match" not in first["headers"]
    assert second["json"] == {"paused": False, "require_mention": False, "settings": {"homeserver": "https://m.example.org"}}


@pytest.mark.parametrize(
    "kwargs",
    [{"allowed": "-100"}, {"allowed": [{"title": "no id"}]}, {"allowed": [""]}, {"paused": "yes"}, {"session_scope": ""}],
)
def test_update_bridge_refuses_malformed_changes_locally(kwargs):
    session = BridgeSession({})

    with pytest.raises(ValueError):
        api_for(session).update_bridge(BRIDGE_ID, **kwargs)
    assert session.requests == []


def test_a_stale_etag_is_the_apis_412():
    session = BridgeSession({
        ("PATCH", f"/v1/bridges/{BRIDGE_ID}"): (412, {"type": "about:blank", "status": 412, "detail": "ETag mismatch"}),
    })

    with pytest.raises(ThalovantAPIError) as raised:
        api_for(session).update_bridge(BRIDGE_ID, paused=True, etag="old")

    assert raised.value.status_code == 412 and raised.value.detail == "ETag mismatch"


def test_replace_token_and_secrets():
    session = BridgeSession({
        ("PUT", f"/v1/bridges/{BRIDGE_ID}/token"): (200, bridge_payload()),
        ("PUT", f"/v1/bridges/{BRIDGE_ID}/secrets"): (200, bridge_payload(network="twitch")),
    })
    api = api_for(session)

    api.replace_bridge_token(BRIDGE_ID, SecretValue(FAKE_TOKEN))
    bridge = api.replace_bridge_secrets(BRIDGE_ID, {"refresh_token": SecretValue(FAKE_REFRESH)})

    token_call, secrets_call = (kwargs for _, _, kwargs in session.requests)
    assert token_call["json"] == {"token": FAKE_TOKEN}
    assert secrets_call["json"] == {"secrets": {"refresh_token": FAKE_REFRESH}}
    assert bridge.network == "twitch"
    with pytest.raises(ValueError):
        api.replace_bridge_secrets(BRIDGE_ID, {})


def test_list_bridge_chats_and_manual_entry():
    discovered = {
        "network": "telegram",
        "data": [{"id": "-100123", "title": "Family", "kind": "supergroup", "allowed": True}],
        "note": "Telegram shows only chats with recent activity.",
    }
    session = BridgeSession({
        ("GET", f"/v1/bridges/{BRIDGE_ID}/chats"): (200, discovered),
        ("GET", "/v1/bridges/other/chats"): (200, {"network": "matrix", "data": [], "manual_entry": True}),
    })
    api = api_for(session)

    chats = api.list_bridge_chats(BRIDGE_ID)
    typed = api.list_bridge_chats("other")

    assert len(chats) == 1 and chats.data[0].allowed is True and chats.data[0].selectable is True
    assert chats.data[0].encrypted is False and chats.note.startswith("Telegram")
    assert chats.manual_entry is False
    assert typed.manual_entry is True and list(typed) == []


def test_send_bridge_test_message():
    session = BridgeSession({
        ("POST", f"/v1/bridges/{BRIDGE_ID}/test"): (200, {"sent": True, "chat_id": "-100123", "language": "fr"}),
    })

    result = api_for(session).send_bridge_test_message(BRIDGE_ID, "-100123")

    assert (result.sent, result.chat_id, result.language) == (True, "-100123", "fr")
    assert only(session)[2]["json"] == {"chat_id": "-100123"}


def test_get_bridge_avatar_returns_the_bytes():
    picture = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    session = BridgeSession({
        ("GET", f"/v1/bridges/{BRIDGE_ID}/avatar"): FakeResponse(
            200, None, content=picture, headers={"Content-Type": "image/png; charset=binary"},
        ),
    })

    avatar = api_for(session).get_bridge_avatar(BRIDGE_ID)

    assert isinstance(avatar, BridgeAvatar)
    assert avatar.content == picture and avatar.content_type == "image/png"
    assert "PNG" not in repr(avatar)
    assert only(session)[2]["headers"]["accept"] == "image/*"


def test_a_missing_avatar_is_the_apis_404_and_an_oversized_one_is_refused():
    session = BridgeSession({
        ("GET", f"/v1/bridges/{BRIDGE_ID}/avatar"): (404, {"status": 404, "detail": "This bot has no picture"}),
        ("GET", "/v1/bridges/big/avatar"): FakeResponse(
            200, None, content=b"x" * (MAX_BRIDGE_AVATAR_BYTES + 1), headers={"content-type": "image/png"},
        ),
    })
    api = api_for(session)

    with pytest.raises(ThalovantAPIError) as raised:
        api.get_bridge_avatar(BRIDGE_ID)
    assert raised.value.status_code == 404 and raised.value.detail == "This bot has no picture"
    with pytest.raises(ThalovantAPIError, match="larger than allowed"):
        api.get_bridge_avatar("big")


def test_delete_bridge_and_path_escaping():
    session = BridgeSession({
        ("DELETE", f"/v1/bridges/{BRIDGE_ID}"): (204, None),
        ("GET", "/v1/bridges/a%2Fb"): (200, bridge_payload()),
    })
    api = api_for(session)

    assert api.delete_bridge(BRIDGE_ID) is None
    api.get_bridge("a/b")
    with pytest.raises(ValueError):
        api.get_bridge("")


def test_a_settings_refusal_carries_its_code_and_fields():
    problem = {
        "type": "about:blank",
        "title": "HTTPException",
        "status": 422,
        "detail": "homeserver must be a public https address.",
        "code": "bridge_url_not_public",
        "fields": ["homeserver", 7],
    }
    session = BridgeSession({("PATCH", f"/v1/bridges/{BRIDGE_ID}"): (422, problem)})

    with pytest.raises(ThalovantAPIError) as raised:
        api_for(session).update_bridge(BRIDGE_ID, settings={"homeserver": "https://10.0.0.1"})

    error = raised.value
    assert (error.status_code, error.code, error.fields) == (422, "bridge_url_not_public", ("homeserver",))
    assert error.detail == "homeserver must be a public https address."


def test_fields_inside_a_detail_object_are_read_too():
    error = ThalovantAPIError(problem={"detail": {"code": "bridge_secrets_invalid", "fields": ["client_secret"]}})

    assert error.code == "bridge_secrets_invalid" and error.fields == ("client_secret",)
    assert ThalovantAPIError("local").fields == ()
    assert ThalovantAPIError(problem={"fields": ["a"]}, fields=["b"]).fields == ("b",)


def test_plan_and_auth_refusals_keep_their_classes():
    session = BridgeSession({
        ("POST", "/v1/bridges"): (403, {"detail": "Plan limit reached", "code": "plan_limit", "resource": "chat_bridge"}),
        ("GET", "/v1/bridges"): (402, {"detail": "API access requires a paid plan."}),
        ("GET", "/v1/bridges/networks"): (401, {"detail": "Not authenticated"}),
        ("POST", f"/v1/bridges/{BRIDGE_ID}/test"): FakeResponse(
            429, {"detail": "Several test messages were sent just now.", "code": "bridge_rate_limited"},
            headers={"Retry-After": "30"},
        ),
    })
    api = api_for(session)

    with pytest.raises(ThalovantPlanError) as plan:
        api.create_bridge(HUB_ID, "telegram", token=SecretValue(FAKE_TOKEN))
    assert plan.value.problem["resource"] == "chat_bridge"
    with pytest.raises(ThalovantPlanError):
        api.list_bridges()
    with pytest.raises(ThalovantAuthError):
        api.get_bridge_networks()
    with pytest.raises(ThalovantAPIError) as limited:
        api.send_bridge_test_message(BRIDGE_ID, "-100123")
    assert limited.value.code == "bridge_rate_limited" and limited.value.retry_after_seconds == 30.0


def test_a_secret_never_prints():
    secret = SecretValue(FAKE_TOKEN)

    for shown in (str(secret), repr(secret), f"{secret}", f"{secret!r}", "%s" % secret, format(secret, ">40")):
        assert FAKE_TOKEN not in shown
    assert repr(SecretValue(secret)) == "SecretValue('<redacted>')"
    assert secret.reveal() == FAKE_TOKEN
    assert secret == SecretValue(FAKE_TOKEN) and secret != SecretValue("other")
    assert copy.deepcopy(secret) is secret
    with pytest.raises(TypeError):
        pickle.dumps(secret)
    with pytest.raises(AttributeError):
        secret._value = "changed"
    with pytest.raises(TypeError):
        SecretValue(123)
    assert FAKE_TOKEN not in repr({"token": secret})


def test_a_refused_create_never_echoes_the_credentials():
    # The API's 422 for /v1/bridges never repeats the body; an error built
    # from it must not either, and neither may anything the SDK prints.
    problem = {"detail": "The network refused this token.", "code": "bridge_token_invalid", "status": 422}
    session = BridgeSession({("POST", "/v1/bridges"): (422, problem)})

    with pytest.raises(ThalovantAPIError) as raised:
        api_for(session).create_bridge(HUB_ID, "telegram", token=SecretValue(FAKE_TOKEN))

    error = raised.value
    assert error.code == "bridge_token_invalid"
    for shown in (str(error), repr(error), repr(error.problem), repr(error.args)):
        assert FAKE_TOKEN not in shown


def test_localized_text_falls_back_to_english():
    text = LocalizedText(en="Hello", fr="Bonjour")
    assert text.text("fr") == "Bonjour" and text.text("FR_ca") == "Bonjour" and text.text("de") == "Hello"
    assert LocalizedText(en="Hello").text("fr") == "Hello"


def test_the_async_control_plane_reads_an_avatar_over_aiohttp_with_a_cap():
    from aiohttp import web

    picture = b"GIF89a" + b"\x00" * 64

    async def avatar(request):
        assert request.headers["authorization"] == "Bearer token"
        if request.match_info["bridge"] == "big":
            return web.Response(body=b"x" * (MAX_BRIDGE_AVATAR_BYTES + 10), content_type="image/png")
        if request.match_info["bridge"] == "gone":
            return web.json_response({"detail": "This bot has no picture"}, status=404)
        return web.Response(body=picture, content_type="image/gif")

    async def bridge(request):
        body = await request.json()
        assert body == {"paused": True}
        assert request.headers["If-Match"] == "etag-1"
        return web.json_response(bridge_payload(paused=True))

    async def exercise():
        app = web.Application()
        app.router.add_get("/v1/bridges/{bridge}/avatar", avatar)
        app.router.add_patch(f"/v1/bridges/{BRIDGE_ID}", bridge)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with AsyncThalovantControlPlane(f"http://127.0.0.1:{port}", access_token="token") as api:
                got = await api.get_bridge_avatar(BRIDGE_ID)
                assert got.content == picture and got.content_type == "image/gif"
                with pytest.raises(ThalovantAPIError, match="larger than allowed"):
                    await api.get_bridge_avatar("big")
                with pytest.raises(ThalovantAPIError) as raised:
                    await api.get_bridge_avatar("gone")
                assert raised.value.status_code == 404
                assert raised.value.detail == "This bot has no picture"
                paused = await api.update_bridge(BRIDGE_ID, paused=True, etag="etag-1")
                assert paused.paused is True
        finally:
            await runner.cleanup()

    asyncio.run(exercise())
