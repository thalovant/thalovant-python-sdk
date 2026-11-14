"""Home bridges (``connection_type: connector``) and the refusals that came with them.

Written against the API's ``Idempotent-Replayed`` header, ``replay`` marker,
``serves`` block and the problem codes of ``ConnectionProblemCode``. Each test
also holds for an API that does not have them yet.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from thalovant import (
    CONNECTION_TYPE_CONNECTOR,
    CONNECTION_TYPE_HOME_ASSISTANT,
    ConnectionProblemCode,
    ThalovantAPIError,
    ThalovantConnectionTypeUnavailableError,
    ThalovantConnectorsInvalidError,
    ThalovantControlPlane,
    ThalovantCredentialsNotReplayedError,
    ThalovantHubScaleOutBreaksLinksError,
    ThalovantIdempotencyKeySpentError,
    ThalovantOwnerOnlyBesideLinkError,
    ThalovantPrivateHubOnlyError,
    ThalovantUnsupportedConnectionTypeError,
)

HUB = {"id": "hub-1", "name": "Home", "domain": "home.example.com", "wss_enabled": True, "http_enabled": True}


class Response:
    def __init__(self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self._body


class Session:
    def __init__(self, response: Response) -> None:
        self.response = response
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        self.sent.append((method, url, kwargs))
        return self.response


def plane(response: Response) -> tuple[ThalovantControlPlane, Session]:
    session = Session(response)
    return ThalovantControlPlane("https://api.example.com", access_token="t", session=session), session


def created(connection_type: str, **extra: Any) -> dict[str, Any]:
    body = {
        "id": "client-1",
        "name": "Bridge",
        "hub_id": "hub-1",
        "spec": {"version": "1", "connection_type": connection_type},
        "etag": "e1",
        "initial_identify": {
            "password": "platform-password",
            "access_key": "platform-key",
            "site_id": "bridge",
            "default_port": 443,
            "default_master": "wss://home.example.com",
        },
    }
    body.update(extra)
    return body


def problem(status: int, code: str, **extra: Any) -> Response:
    return Response(status, {"detail": "refused", "code": code, **extra})


def test_the_kind_constants_are_the_wire_values() -> None:
    assert CONNECTION_TYPE_CONNECTOR == "connector"
    assert CONNECTION_TYPE_HOME_ASSISTANT == "home_assistant"


def test_a_bridge_is_created_with_its_connector_list_and_the_platforms_credentials() -> None:
    serves = {
        "devices": [{"id": "d1", "name": "Kitchen", "connection_type": "voice_satellite", "active": True}],
        "count": 1,
        "warning": None,
    }
    api, session = plane(Response(201, created("connector", serves=serves)))
    result = api.create_client_identity(
        HUB, name="Bridge", connection_type=CONNECTION_TYPE_CONNECTOR, spec={"connectors": ["music_assistant"]}
    )
    sent = session.sent[0][2]["json"]["spec"]
    assert sent["connection_type"] == "connector"
    assert sent["connectors"] == ["music_assistant"]
    # The answer's credentials are the connection's; the ones minted locally are not.
    assert result.identity.access_key == "platform-key"
    assert result.identity.password == "platform-password"
    assert result.connection_type == "connector"
    assert result.replayed is False
    assert result.serves == serves
    assert result.serves_warning is None


def test_a_link_that_serves_nobody_says_so() -> None:
    serves = {"devices": [], "count": 0, "warning": "No device of your account is on this hub."}
    api, _ = plane(Response(201, created("home_assistant", serves=serves)))
    result = api.create_client_identity(HUB, name="HA", connection_type="home_assistant")
    assert result.serves_warning == "No device of your account is on this hub."


def test_an_api_without_serves_still_works() -> None:
    api, _ = plane(Response(201, created("connector")))
    result = api.create_client_identity(HUB, name="Bridge", connection_type="connector")
    assert result.serves is None
    assert result.serves_warning is None


@pytest.mark.parametrize(
    "response",
    [
        Response(201, created("connector"), {"Idempotent-Replayed": "true"}),
        Response(201, created("connector", replay={"code": "idempotent_replay", "credentials_included": False, "detail": "x"})),
    ],
    ids=["header", "marker"],
)
def test_a_replayed_create_never_hands_back_an_identity_the_sdk_made_up(response: Response) -> None:
    body = response._body
    body.pop("initial_identify")
    api, _ = plane(response)
    with pytest.raises(ThalovantCredentialsNotReplayedError) as caught:
        api.create_client_identity(HUB, name="Bridge", connection_type="connector", idempotency_key="k1")
    assert caught.value.client_id == "client-1"
    assert "initial_identify" not in caught.value.client


def test_an_answer_without_credentials_that_is_not_a_replay_keeps_the_old_behaviour() -> None:
    body = created("connector")
    body.pop("initial_identify")
    api, _ = plane(Response(201, body))
    result = api.create_client_identity(HUB, name="Bridge", connection_type="connector")
    assert result.identity.access_key


def test_a_link_on_another_accounts_hub_names_the_reason() -> None:
    api, session = plane(problem(422, "connection_type_unavailable", reason="hub_operated_by_another_account"))
    with pytest.raises(ThalovantConnectionTypeUnavailableError) as caught:
        api.create_client_identity(HUB, name="HA", connection_type="home_assistant")
    assert caught.value.reason == "hub_operated_by_another_account"
    assert caught.value.code == ConnectionProblemCode.CONNECTION_TYPE_UNAVAILABLE
    assert isinstance(caught.value, ThalovantUnsupportedConnectionTypeError)
    assert len(session.sent) == 1, "nothing to delete: the API made nothing"


@pytest.mark.parametrize("code", ["connection_type_unknown", "connection_type_ambiguous"])
def test_an_unknown_or_ambiguous_kind_is_unsupported(code: str) -> None:
    api, _ = plane(problem(422, code, known=["voice_satellite", "connector"]))
    with pytest.raises(ThalovantUnsupportedConnectionTypeError) as caught:
        api.create_client_identity(HUB, name="X", connection_type="plex")
    assert caught.value.code == code


@pytest.mark.parametrize("reason", ["duplicate", "own_kind", "unknown_or_malformed"])
def test_a_refused_connector_list_says_why_and_what_is_known(reason: str) -> None:
    api, _ = plane(problem(422, "connectors_invalid", reason=reason, known=["music_assistant"]))
    with pytest.raises(ThalovantConnectorsInvalidError) as caught:
        api.create_client_identity(
            HUB, name="Bridge", connection_type="connector", spec={"connectors": ["music_assistant", "music_assistant"]}
        )
    assert caught.value.reason == reason
    assert caught.value.known == ("music_assistant",)


@pytest.mark.parametrize(
    "code",
    [
        "device_owner_only_beside_home_link",
        "setup_session_owner_only_beside_home_link",
        "device_identity_owner_only_beside_home_link",
    ],
)
def test_an_owner_only_refusal_names_the_link(code: str) -> None:
    api, _ = plane(problem(403, code, link_client_id="client-9"))
    with pytest.raises(ThalovantOwnerOnlyBesideLinkError) as caught:
        api.create_client_identity(HUB, name="Speaker", owner_id="someone")
    assert caught.value.link_client_id == "client-9"
    assert caught.value.status_code == 403


def test_the_other_new_codes_are_typed() -> None:
    cases = [
        (409, "hub_scale_out_breaks_links", {"links": 2}, ThalovantHubScaleOutBreaksLinksError),
        (409, "idempotency_key_spent", {}, ThalovantIdempotencyKeySpentError),
        (422, "skill_config_private_hub_only", {"settings": ["url", "token"]}, ThalovantPrivateHubOnlyError),
    ]
    for status, code, extra, kind in cases:
        api, _ = plane(problem(status, code, **extra))
        with pytest.raises(kind) as caught:
            api.create_client_identity(HUB, name="X")
        assert caught.value.code == code
    api, _ = plane(problem(409, "hub_scale_out_breaks_links", links=2))
    with pytest.raises(ThalovantHubScaleOutBreaksLinksError) as scale:
        api.create_client_identity(HUB, name="X")
    assert scale.value.links == 2
    api, _ = plane(problem(422, "skill_config_private_hub_only", settings=["url", "token"]))
    with pytest.raises(ThalovantPrivateHubOnlyError) as private:
        api.create_client_identity(HUB, name="X")
    assert private.value.settings == ("url", "token")


def test_a_structured_refusal_inside_detail_is_read_too() -> None:
    api, _ = plane(Response(422, {"detail": {"code": "connectors_invalid", "reason": "duplicate", "known": ["music_assistant"]}}))
    with pytest.raises(ThalovantConnectorsInvalidError) as caught:
        api.create_client_identity(HUB, name="Bridge", connection_type="connector")
    assert caught.value.reason == "duplicate"


def test_an_unlisted_code_stays_a_plain_api_error() -> None:
    api, _ = plane(problem(409, "something_new"))
    with pytest.raises(ThalovantAPIError) as caught:
        api.create_client_identity(HUB, name="X")
    assert type(caught.value) is ThalovantAPIError


def test_a_connectors_refusal_that_names_connection_type_stays_itself() -> None:
    api, _ = plane(
        Response(
            422,
            {
                "detail": "Duplicate connectors for connection_type connector.",
                "code": "connectors_invalid",
                "reason": "duplicate",
                "known": ["music_assistant"],
            },
        )
    )
    with pytest.raises(ThalovantConnectorsInvalidError) as caught:
        api.create_client_identity(HUB, name="Bridge", connection_type="connector")
    assert caught.value.reason == "duplicate"
    assert caught.value.known == ("music_assistant",)


def test_the_unknown_kind_codes_are_unsupported_on_any_call() -> None:
    for code in ("connection_type_unknown", "connection_type_ambiguous"):
        api, _ = plane(problem(422, code))
        with pytest.raises(ThalovantUnsupportedConnectionTypeError):
            api.create_client({"hub_id": "hub-1", "name": "x", "spec": {}})
