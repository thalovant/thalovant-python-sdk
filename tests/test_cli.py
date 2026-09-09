import json

import pytest

from thalovant import (
    HubSkill,
    HubSkillList,
    HubSkillOperation,
    ThalovantDoctorCheck,
    ThalovantDoctorReport,
    ThalovantHealth,
    ThalovantReply,
)
import thalovant.cli as cli


class FakeClient:
    instances = []

    def __init__(self, identity):
        self.identity = identity
        self.calls = []
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def healthcheck(self):
        return ThalovantHealth(
            connected=True,
            handshake_complete=True,
            transport_alive=True,
        )

    def doctor(self):
        return ThalovantDoctorReport(
            identity=self.identity.as_dict(),
            checks=(ThalovantDoctorCheck("identity", True, "ok"),),
        )

    def ask(self, text, **kwargs):
        self.calls.append(("ask", text, kwargs))
        return ThalovantReply(text="hello from cli", handled=True)

    def emit(self, event, data, context):
        self.calls.append(("emit", event, data, context))

    def send_utterance(self, text, **kwargs):
        self.calls.append(("utter", text, kwargs))

    def listen(self, *_args, **_kwargs):
        return iter(())


def identity_file(tmp_path):
    path = tmp_path / "_identity.json"
    path.write_text(
        json.dumps(
            {
                "access_key": "key",
                "password": "password",
                "site_id": "site",
                "default_master": "https://hub.example.com",
                "default_port": 443,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_cli_ask_prints_reply(monkeypatch, tmp_path, capsys):
    FakeClient.instances = []
    monkeypatch.setattr(cli, "ThalovantClient", FakeClient)

    result = cli.main(["--identity", str(identity_file(tmp_path)), "ask", "hello"])

    assert result == 0
    assert capsys.readouterr().out == "hello from cli\n"
    assert FakeClient.instances[0].calls[0][0] == "ask"


def test_cli_emit_parses_json(monkeypatch, tmp_path, capsys):
    FakeClient.instances = []
    monkeypatch.setattr(cli, "ThalovantClient", FakeClient)

    result = cli.main(
        [
            "--identity",
            str(identity_file(tmp_path)),
            "emit",
            "custom.event",
            "--data",
            '{"x": 1}',
            "--context",
            '{"source": "test"}',
        ]
    )

    assert result == 0
    assert capsys.readouterr().out == "sent\n"
    assert FakeClient.instances[0].calls == [
        ("emit", "custom.event", {"x": 1}, {"source": "test"})
    ]


class FakeControlPlane:
    """Records the ``skills`` subcommand's calls; no HTTP is involved."""

    instances = []

    def __init__(self, api_url, *, access_token):
        self.api_url = api_url
        self.access_token = access_token
        self.calls = []
        self.instances.append(self)

    def list_hub_skills(self, hub_id):
        self.calls.append(("list", hub_id))
        return HubSkillList(
            hub_id=hub_id,
            runtime_group_id="rg-1",
            observed_at="2026-09-09T12:00:05Z",
            source="ovos-runtime-operator",
            data=[
                HubSkill(
                    skill="skill-weather",
                    state="installed",
                    title="Weather",
                    version="latest",
                    installed_version="1.1.0",
                    latest_version="1.2.0",
                    update_available=True,
                ),
                HubSkill(
                    skill="skill-jokes",
                    state="failed",
                    version="0.3.0",
                    installed_version="0.3.0",
                    latest_version="0.3.0",
                    active=False,
                    operator_last_error="pip install failed",
                ),
                HubSkill(skill="skill-time", state="pending", version="latest"),
            ],
        )

    def install_hub_skill(self, hub_id, skill, **kwargs):
        self.calls.append(("install", hub_id, skill, kwargs))
        state = "installed" if kwargs.get("wait") else "installing"
        return HubSkillOperation("op-1", skill, kwargs.get("version"), state, hub_id=hub_id)

    def update_hub_skill(self, hub_id, skill, **kwargs):
        self.calls.append(("update", hub_id, skill, kwargs))
        return HubSkillOperation("op-2", skill, kwargs.get("version"), "updating", hub_id=hub_id, previous_version="1.1.0")

    def remove_hub_skill(self, hub_id, skill, **kwargs):
        self.calls.append(("remove", hub_id, skill, kwargs))
        return HubSkillOperation("op-3", skill, None, "removing", hub_id=hub_id, previous_version="1.1.0")


def control_plane(monkeypatch):
    FakeControlPlane.instances = []
    monkeypatch.setattr(cli, "ThalovantControlPlane", FakeControlPlane)
    monkeypatch.setenv("THALOVANT_API_TOKEN", "tvpat_test")
    monkeypatch.delenv("THALOVANT_API_URL", raising=False)


def test_cli_skills_list_prints_state_per_skill(monkeypatch, capsys):
    control_plane(monkeypatch)

    result = cli.main(["skills", "list", "--hub", "hub-1"])

    assert result == 0
    out = capsys.readouterr().out
    assert out == (
        "skill-weather\t1.1.0\tinstalled\tlatest=1.2.0\n"
        "skill-jokes\t0.3.0\tfailed\tinactive\terror=pip install failed\n"
        "skill-time\tlatest\tpending\n"
    )
    api = FakeControlPlane.instances[0]
    assert api.calls == [("list", "hub-1")]
    assert api.access_token == "tvpat_test"
    assert api.api_url == "https://api.thalovant.com"


def test_cli_skills_list_json(monkeypatch, capsys):
    control_plane(monkeypatch)

    result = cli.main(["--json", "skills", "list", "--hub", "hub-1"])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hub_id"] == "hub-1"
    assert payload["source"] == "ovos-runtime-operator"
    assert [entry["skill"] for entry in payload["data"]] == ["skill-weather", "skill-jokes", "skill-time"]
    assert payload["data"][1]["operator_last_error"] == "pip install failed"
    assert payload["data"][0]["update_available"] is True


def test_cli_skills_add_defaults_to_latest_without_waiting(monkeypatch, capsys):
    control_plane(monkeypatch)

    result = cli.main(["skills", "add", "--hub", "hub-1", "skill-weather"])

    assert result == 0
    assert capsys.readouterr().out == "accepted: installing skill-weather@latest (operation op-1)\n"
    assert FakeControlPlane.instances[0].calls == [
        ("install", "hub-1", "skill-weather", {"version": "latest", "wait": False, "timeout": 120.0})
    ]


def test_cli_skills_add_with_version_wait_and_json(monkeypatch, capsys):
    control_plane(monkeypatch)

    result = cli.main([
        "--json", "skills", "add", "--hub", "hub-1", "skill-weather",
        "--version", "1.2.0", "--wait", "--timeout", "30",
    ])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "installed"
    assert payload["operation_id"] == "op-1"
    assert payload["hub_id"] == "hub-1"
    assert payload["previous_version"] is None
    assert FakeControlPlane.instances[0].calls == [
        ("install", "hub-1", "skill-weather", {"version": "1.2.0", "wait": True, "timeout": 30.0})
    ]


def test_cli_skills_update_requires_a_version(monkeypatch, capsys):
    control_plane(monkeypatch)

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["skills", "update", "--hub", "hub-1", "skill-weather"])
    assert exit_info.value.code == 2
    assert "--version" in capsys.readouterr().err

    result = cli.main(["skills", "update", "--hub", "hub-1", "skill-weather", "--version", "1.2.0"])
    assert result == 0
    assert capsys.readouterr().out == "accepted: updating skill-weather@1.2.0 (operation op-2)\n"
    assert FakeControlPlane.instances[0].calls == [
        ("update", "hub-1", "skill-weather", {"version": "1.2.0", "wait": False, "timeout": 120.0})
    ]


def test_cli_skills_remove(monkeypatch, capsys):
    control_plane(monkeypatch)

    result = cli.main(["skills", "remove", "--hub", "hub-1", "skill-weather"])

    assert result == 0
    assert capsys.readouterr().out == "accepted: removing skill-weather (operation op-3)\n"
    assert FakeControlPlane.instances[0].calls == [
        ("remove", "hub-1", "skill-weather", {"wait": False, "timeout": 120.0})
    ]


def test_cli_skills_honours_api_url_and_token_options(monkeypatch):
    control_plane(monkeypatch)
    monkeypatch.setenv("THALOVANT_API_URL", "https://api.example.test/v1")

    assert cli.main(["skills", "list", "--hub", "hub-1"]) == 0
    assert FakeControlPlane.instances[-1].api_url == "https://api.example.test/v1"

    assert cli.main([
        "skills", "list", "--hub", "hub-1",
        "--api-url", "https://other.example.test", "--token", "tvpat_other",
    ]) == 0
    assert FakeControlPlane.instances[-1].api_url == "https://other.example.test"
    assert FakeControlPlane.instances[-1].access_token == "tvpat_other"


def test_cli_skills_requires_a_token(monkeypatch, capsys):
    control_plane(monkeypatch)
    monkeypatch.delenv("THALOVANT_API_TOKEN")

    result = cli.main(["skills", "list", "--hub", "hub-1"])

    assert result == 1
    assert "THALOVANT_API_TOKEN" in capsys.readouterr().err
    assert FakeControlPlane.instances == []


def test_cli_skills_does_not_load_an_identity(monkeypatch):
    control_plane(monkeypatch)
    FakeClient.instances = []
    monkeypatch.setattr(cli, "ThalovantClient", FakeClient)

    assert cli.main(["skills", "list", "--hub", "hub-1"]) == 0
    assert FakeClient.instances == []
