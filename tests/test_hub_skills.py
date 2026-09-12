"""Hub-scoped skill management: ``/v1/hubs/{hub_id}/skills``."""

from typing import get_args
import traceback

import pytest
import requests

from thalovant import (
    HubSkill,
    HubSkillList,
    HubSkillOperation,
    HubSkillOperationState,
    HubSkillState,
    OperationResource,
    ThalovantAPIError,
    ThalovantControlPlane,
    ThalovantTimeoutError,
)
from thalovant.control import DEFAULT_HUB_SKILL_WAIT_TIMEOUT


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = "" if body is None else str(body)

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


BASE_URL = "https://dash.example.com/api/"

WEATHER_ROW = {
    "skill": "skill-weather",
    "title": "Weather",
    "marketplace_skill_id": "8d6f7c2e-1c1a-4f7e-9c0f-2b1a1c3d4e5f",
    "package_name": "skill-weather",
    "source_type": "catalog",
    "install_source": "index",
    "version": "latest",
    "version_pin": None,
    "installed_version": "1.1.0",
    "observed_version": "1.1.0",
    "previous_version": "1.0.0",
    "latest_version": "1.2.0",
    "available_version": "1.2.0",
    "update_available": True,
    "changelog": "Adds hourly forecasts.",
    "active": True,
    "state": "installed",
    "operator_phase": "Ready",
    "operator_message": None,
    "operator_last_error": None,
    "last_transition_at": "2026-09-09T12:00:00Z",
}

LISTING = {
    "hub_id": "hub-1",
    "runtime_group_id": "rg-1",
    "observed_at": "2026-09-09T12:00:05Z",
    "source": "ovos-runtime-operator",
    "operator_phase": "Ready",
    "operator_message": None,
    "data": [
        WEATHER_ROW,
        {
            "skill": "skill-jokes",
            "version": "0.3.0",
            "installed_version": "0.3.0",
            "latest_version": "0.3.0",
            "update_available": False,
            "active": False,
            "state": "failed",
            "operator_last_error": "pip install failed",
            "last_transition_at": "2026-09-09T12:01:00Z",
        },
    ],
}

ACCEPTED_INSTALL = {
    "operation_id": "op-1",
    "hub_id": "hub-1",
    "runtime_group_id": "rg-1",
    "skill": "skill-weather",
    "version": "latest",
    "previous_version": None,
    "state": "installing",
}


def operation(status, **overrides):
    payload = {
        "id": "op-1",
        "kind": "hub.skill.install",
        "aggregate_type": "hub",
        "aggregate_id": "hub-1",
        "status": status,
        "details": {},
        "git_commit_sha": None,
        "error_code": None,
        "error_message": None,
        "created_at": "2026-09-09T12:00:00Z",
        "updated_at": "2026-09-09T12:00:01Z",
        "committed_at": None,
        "applied_at": None,
        "ready_at": None,
        "terminal_at": None,
        "links": {"self": "/v1/operations/op-1"},
    }
    payload.update(overrides)
    return payload


class HubSkillSession:
    """Scripted session: routes map to responses, operation reads pop a queue."""

    def __init__(self, routes=None, operations=()):
        self.requests = []
        self.routes = dict(routes or {})
        self.operations = list(operations)

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        assert kwargs["headers"]["authorization"] == "Bearer token"
        assert url.startswith(BASE_URL)
        path = "/" + url[len(BASE_URL) :]
        if method == "GET" and path == "/v1/operations/op-1":
            assert self.operations, "unexpected operation read"
            return FakeResponse(200, self.operations.pop(0))
        if (method, path) not in self.routes:
            raise AssertionError(f"{method} {path}")
        return FakeResponse(*self.routes[(method, path)])


def api_for(session):
    return ThalovantControlPlane("https://dash.example.com/api", access_token="token", session=session)


def calls(session, method, path):
    return [
        kwargs
        for recorded_method, url, kwargs in session.requests
        if recorded_method == method and url == BASE_URL + path.lstrip("/")
    ]


def instant(api):
    """Drive the wait loop without real sleeping: sleeps advance a fake clock."""

    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    def clock():
        return now[0]

    original = api._wait_for_hub_skill_operation

    def patched(accepted, **kwargs):
        return original(accepted, sleep=sleep, clock=clock, **kwargs)

    api._wait_for_hub_skill_operation = patched
    return now


def test_state_literals_match_the_contract():
    assert set(get_args(HubSkillState)) == {
        "pending", "installed", "failed", "removing", "drifted", "quarantined", "unmanaged",
    }
    assert set(get_args(HubSkillOperationState)) == {
        "installing", "updating", "removing", "installed", "removed", "failed",
    }
    assert DEFAULT_HUB_SKILL_WAIT_TIMEOUT == 120.0


def test_list_hub_skills_parses_the_data_envelope():
    session = HubSkillSession({("GET", "/v1/hubs/hub-1/skills"): (200, LISTING)})

    listing = api_for(session).list_hub_skills("hub-1")

    assert isinstance(listing, HubSkillList)
    assert listing.hub_id == "hub-1"
    assert listing.runtime_group_id == "rg-1"
    assert listing.observed_at == "2026-09-09T12:00:05Z"
    assert listing.source == "ovos-runtime-operator"
    assert listing.operator_phase == "Ready"
    assert listing.operator_message is None
    assert len(listing) == 2
    weather, jokes = listing
    assert isinstance(weather, HubSkill)
    assert weather.as_dict() == WEATHER_ROW
    # Absent row fields come back as None / False / True, never missing.
    assert jokes.state == "failed"
    assert jokes.title is None
    assert jokes.marketplace_skill_id is None
    assert jokes.previous_version is None
    assert jokes.update_available is False
    assert jokes.active is False
    assert jokes.operator_last_error == "pip install failed"
    assert jokes.operator_phase is None
    assert listing.as_dict()["data"][1]["operator_last_error"] == "pip install failed"
    assert calls(session, "GET", "/v1/hubs/hub-1/skills")[0]["json"] is None


def test_list_hub_skills_accepts_an_empty_hub_and_defaults_active():
    session = HubSkillSession(
        {("GET", "/v1/hubs/hub-1/skills"): (200, {"hub_id": "hub-1", "source": "runtime-group-cache-empty", "data": []})}
    )
    listing = api_for(session).list_hub_skills("hub-1")
    assert listing.data == []
    assert list(listing) == []
    assert listing.observed_at is None

    session = HubSkillSession(
        {("GET", "/v1/hubs/hub-1/skills"): (200, {"hub_id": "hub-1", "data": [{"skill": "skill-x", "state": "pending"}]})}
    )
    (row,) = api_for(session).list_hub_skills("hub-1")
    assert row.active is True
    assert row.update_available is False
    assert row.installed_version is None


@pytest.mark.parametrize("body", [{"hub_id": "hub-1", "items": []}, {"hub_id": "hub-1", "data": "nope"}])
def test_list_hub_skills_rejects_an_envelope_without_a_data_list(body):
    session = HubSkillSession({("GET", "/v1/hubs/hub-1/skills"): (200, body)})
    with pytest.raises(ThalovantAPIError, match="unexpected hub skill listing shape"):
        api_for(session).list_hub_skills("hub-1")


@pytest.mark.parametrize("invalid", [None, False, "not a skill", []])
@pytest.mark.parametrize("valid_first", [False, True])
def test_list_hub_skills_rejects_malformed_rows_instead_of_dropping_them(invalid, valid_first):
    rows = [WEATHER_ROW, invalid] if valid_first else [invalid, WEATHER_ROW]
    session = HubSkillSession({("GET", "/v1/hubs/hub-1/skills"): (200, {**LISTING, "data": rows})})
    with pytest.raises(ThalovantAPIError, match="unexpected hub skill row"):
        api_for(session).list_hub_skills("hub-1")


def test_install_hub_skill_sends_latest_by_default_and_returns_accepted():
    session = HubSkillSession({("POST", "/v1/hubs/hub-1/skills"): (202, ACCEPTED_INSTALL)})

    result = api_for(session).install_hub_skill("hub-1", "skill-weather")

    assert isinstance(result, HubSkillOperation)
    assert result.operation_id == "op-1"
    assert result.hub_id == "hub-1"
    assert result.runtime_group_id == "rg-1"
    assert result.state == "installing"
    assert result.version == "latest"
    assert result.previous_version is None
    assert result.operation is None
    assert calls(session, "POST", "/v1/hubs/hub-1/skills")[0]["json"] == {
        "skill": "skill-weather",
        "version": "latest",
    }
    assert not calls(session, "GET", "/v1/operations/op-1")
    assert result.as_dict() == {**ACCEPTED_INSTALL, "operation": None}


def test_install_hub_skill_sends_an_exact_version_and_reports_the_previous_one():
    session = HubSkillSession(
        {("POST", "/v1/hubs/hub-1/skills"): (202, {**ACCEPTED_INSTALL, "version": "1.2.0", "previous_version": "1.1.0"})}
    )
    result = api_for(session).install_hub_skill("hub-1", "skill-weather", version="1.2.0")
    assert result.version == "1.2.0"
    assert result.previous_version == "1.1.0"
    assert calls(session, "POST", "/v1/hubs/hub-1/skills")[0]["json"]["version"] == "1.2.0"


def test_install_hub_skill_waits_until_the_operation_is_ready():
    session = HubSkillSession(
        {("POST", "/v1/hubs/hub-1/skills"): (202, ACCEPTED_INSTALL)},
        operations=[operation("requested"), operation("applied"), operation("ready", ready_at="2026-09-09T12:00:15Z")],
    )
    api = api_for(session)
    clock = instant(api)

    result = api.install_hub_skill("hub-1", "skill-weather", wait=True)

    assert result.state == "installed"
    assert result.operation_id == "op-1"
    assert result.hub_id == "hub-1"
    assert isinstance(result.operation, OperationResource)
    assert result.operation.status == "ready"
    assert len(calls(session, "GET", "/v1/operations/op-1")) == 3
    assert clock[0] == 4.0  # two polls apart at the 2 s interval
    assert result.as_dict()["operation"]["status"] == "ready"


def test_update_hub_skill_patches_the_version_and_waits():
    session = HubSkillSession(
        {
            ("PATCH", "/v1/hubs/hub-1/skills/skill-weather"): (
                202,
                {**ACCEPTED_INSTALL, "version": "1.2.0", "previous_version": "1.1.0", "state": "updating"},
            )
        },
        operations=[operation("ready")],
    )
    api = api_for(session)
    instant(api)

    accepted = api.update_hub_skill("hub-1", "skill-weather", version="1.2.0")
    assert accepted.state == "updating"
    assert accepted.previous_version == "1.1.0"
    assert calls(session, "PATCH", "/v1/hubs/hub-1/skills/skill-weather")[0]["json"] == {"version": "1.2.0"}

    waited = api.update_hub_skill("hub-1", "skill-weather", version="1.2.0", wait=True)
    assert waited.state == "installed"
    assert waited.version == "1.2.0"


def test_update_hub_skill_requires_a_version():
    with pytest.raises(TypeError):
        api_for(HubSkillSession()).update_hub_skill("hub-1", "skill-weather")  # type: ignore[call-arg]


def test_remove_hub_skill_deletes_without_a_body_and_converges_on_removed():
    session = HubSkillSession(
        {
            ("DELETE", "/v1/hubs/hub-1/skills/skill-weather"): (
                202,
                {**ACCEPTED_INSTALL, "version": None, "previous_version": "1.1.0", "state": "removing"},
            )
        },
        operations=[operation("ready")],
    )
    api = api_for(session)
    instant(api)

    accepted = api.remove_hub_skill("hub-1", "skill-weather")
    assert accepted.state == "removing"
    assert accepted.version is None
    assert accepted.previous_version == "1.1.0"
    assert calls(session, "DELETE", "/v1/hubs/hub-1/skills/skill-weather")[0]["json"] is None

    waited = api.remove_hub_skill("hub-1", "skill-weather", wait=True)
    assert waited.state == "removed"


def test_hub_and_skill_path_segments_are_url_encoded():
    session = HubSkillSession(
        {
            ("DELETE", "/v1/hubs/hub%2F1/skills/skill%20weather%2Fx"): (
                202,
                {**ACCEPTED_INSTALL, "skill": "skill weather/x", "version": None, "state": "removing"},
            )
        }
    )
    api_for(session).remove_hub_skill("hub/1", "skill weather/x")
    assert len(session.requests) == 1


def test_wait_raises_the_operation_error_message_on_failure():
    session = HubSkillSession(
        {("POST", "/v1/hubs/hub-1/skills"): (202, ACCEPTED_INSTALL)},
        operations=[operation("failed", error_code="install_failed", error_message="pip install failed")],
    )
    api = api_for(session)
    instant(api)

    with pytest.raises(ThalovantAPIError, match="skill-weather failed: pip install failed") as caught:
        api.install_hub_skill("hub-1", "skill-weather", wait=True)
    assert "operation op-1" in str(caught.value)


def test_wait_falls_back_to_the_error_code_and_then_the_status():
    session = HubSkillSession(
        {("POST", "/v1/hubs/hub-1/skills"): (202, ACCEPTED_INSTALL)},
        operations=[operation("timed_out", error_code="runtime_timeout")],
    )
    api = api_for(session)
    instant(api)
    with pytest.raises(ThalovantAPIError, match="failed: runtime_timeout") as caught:
        api.install_hub_skill("hub-1", "skill-weather", wait=True)
    assert "operation op-1" in str(caught.value)

    session.operations = [operation("timed_out")]
    with pytest.raises(ThalovantAPIError, match="ended with status timed_out") as caught:
        api.install_hub_skill("hub-1", "skill-weather", wait=True)
    assert "operation op-1" in str(caught.value)


def test_wait_times_out_with_a_typed_error():
    session = HubSkillSession(
        {("POST", "/v1/hubs/hub-1/skills"): (202, ACCEPTED_INSTALL)},
        operations=[operation("requested")] * 10,
    )
    api = api_for(session)
    instant(api)

    with pytest.raises(ThalovantTimeoutError, match="Timed out after 5s"):
        api.install_hub_skill("hub-1", "skill-weather", wait=True, timeout=5)
    # Reads at 0 s, 2 s and 4 s; the final sleep cannot start a read at 5 s.
    assert len(calls(session, "GET", "/v1/operations/op-1")) == 3


def test_wait_does_not_poll_when_sleep_resumes_after_the_deadline():
    session = HubSkillSession(
        {("POST", "/v1/hubs/hub-1/skills"): (202, ACCEPTED_INSTALL)},
        operations=[operation("requested"), operation("ready")],
    )
    api = api_for(session)
    accepted = api.install_hub_skill("hub-1", "skill-weather")
    now = [0.0]

    def delayed_sleep(_):
        now[0] = 10.0

    with pytest.raises(ThalovantTimeoutError, match="operation op-1"):
        api._wait_for_hub_skill_operation(
            accepted, converged="installed", timeout=5, sleep=delayed_sleep, clock=lambda: now[0],
        )
    assert len(calls(session, "GET", "/v1/operations/op-1")) == 1


@pytest.mark.parametrize("failure", ["http503", "network", "custom"])
def test_wait_read_failure_preserves_accepted_id_without_retry_or_raw_cause(failure):
    secret = "synthetic-poll-credential-must-not-appear"

    class FailingPollSession(HubSkillSession):
        def request(self, method, url, **kwargs):
            if method == "GET" and url.endswith("/v1/operations/op-1"):
                self.requests.append((method, url, kwargs))
                if failure == "http503":
                    return FakeResponse(503, {"detail": "Service unavailable"})
                if failure == "network":
                    raise requests.ConnectionError(secret)
                raise RuntimeError(secret)
            return super().request(method, url, **kwargs)

    session = FailingPollSession({("POST", "/v1/hubs/hub-1/skills"): (202, ACCEPTED_INSTALL)})
    with pytest.raises(ThalovantAPIError, match="operation op-1") as caught:
        api_for(session).install_hub_skill("hub-1", "skill-weather", wait=True)
    assert len(calls(session, "POST", "/v1/hubs/hub-1/skills")) == 1
    assert len(calls(session, "GET", "/v1/operations/op-1")) == 1
    if failure == "custom":
        assert caught.value.__cause__ is None
    else:
        assert isinstance(caught.value.__cause__, ThalovantAPIError)
        if failure == "http503":
            assert "HTTP 503" in str(caught.value.__cause__)
    assert secret not in "".join(traceback.format_exception(caught.value))


def test_problem_codes_are_kept_in_the_error_message():
    conflict = HubSkillSession(
        {
            ("POST", "/v1/hubs/hub-1/skills"): (
                409,
                {
                    "type": "about:blank",
                    "title": "Conflict",
                    "status": 409,
                    "code": "skill_version_already_installed",
                    "message": "Skill version already installed.",
                },
            )
        }
    )
    with pytest.raises(ThalovantAPIError) as raised:
        api_for(conflict).install_hub_skill("hub-1", "skill-weather", version="1.2.0")
    assert str(raised.value) == (
        "Thalovant API request failed with HTTP 409: "
        "Skill version already installed. (skill_version_already_installed)"
    )

    no_group = HubSkillSession(
        {("POST", "/v1/hubs/hub-1/skills"): (404, {"status": 404, "code": "hub_without_runtime_group", "message": "Hub has no runtime group."})}
    )
    with pytest.raises(ThalovantAPIError, match=r"HTTP 404: Hub has no runtime group\. \(hub_without_runtime_group\)"):
        api_for(no_group).install_hub_skill("hub-1", "skill-weather")

    # A plain 404 (unknown hub, or a skill that is not installed) carries no code.
    plain = HubSkillSession({("DELETE", "/v1/hubs/hub-1/skills/skill-weather"): (404, {"detail": "Not Found"})})
    with pytest.raises(ThalovantAPIError) as raised:
        api_for(plain).remove_hub_skill("hub-1", "skill-weather")
    assert str(raised.value) == "Thalovant API request failed with HTTP 404: Not Found"

    invalid = HubSkillSession(
        {("PATCH", "/v1/hubs/hub-1/skills/skill-weather"): (422, {"code": "invalid_version", "message": "Version 'x' is not valid."})}
    )
    with pytest.raises(ThalovantAPIError, match=r"HTTP 422: Version 'x' is not valid\. \(invalid_version\)"):
        api_for(invalid).update_hub_skill("hub-1", "skill-weather", version="x")

    # A code alone is still surfaced; a code that is not a token is ignored.
    code_only = HubSkillSession({("GET", "/v1/hubs/hub-1/skills"): (409, {"code": "skill_version_already_installed"})})
    with pytest.raises(ThalovantAPIError, match=r"HTTP 409: \(skill_version_already_installed\)$"):
        api_for(code_only).list_hub_skills("hub-1")
    junk = HubSkillSession({("GET", "/v1/hubs/hub-1/skills"): (403, {"detail": "Insufficient scopes", "code": "no spaces allowed"})})
    with pytest.raises(ThalovantAPIError, match=r"HTTP 403: Insufficient scopes$"):
        api_for(junk).list_hub_skills("hub-1")


def test_accepted_body_must_carry_an_operation_id():
    session = HubSkillSession(
        {("POST", "/v1/hubs/hub-1/skills"): (202, {"skill": "skill-weather", "state": "installing"})}
    )
    with pytest.raises(ThalovantAPIError, match="missing operation_id"):
        api_for(session).install_hub_skill("hub-1", "skill-weather")


def test_history_preserves_event_and_operation_fields_and_encodes_hub():
    history = {"hub_id": "hub/one", "runtime_group_id": "shared", "data": [
        {"id": "event:1", "kind": "event", "actor_email": None, "version": "1.2.0"},
        {"id": "operation:1", "kind": "operation", "status": "failed", "operation_id": "op-1"},
    ]}
    session = HubSkillSession({("GET", "/v1/hubs/hub%2Fone/skills/history"): (200, history)})
    assert api_for(session).list_hub_skill_history("hub/one", limit=200) == history
    assert session.requests[0][2]["params"] == {"limit": 200}


@pytest.mark.parametrize("limit", [0, 201, -1, True, 1.5, "50"])
def test_history_invalid_limit_does_not_send(limit):
    session = HubSkillSession()
    with pytest.raises(ValueError):
        api_for(session).list_hub_skill_history("hub-1", limit=limit)
    assert not session.requests
