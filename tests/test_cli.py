import json

from thalovant import ThalovantDoctorCheck, ThalovantDoctorReport, ThalovantHealth, ThalovantReply
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
