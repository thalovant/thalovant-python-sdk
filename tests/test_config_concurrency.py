"""Regressions for closed PR 39: independent clients must not lose updates."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier, Lock

import pytest
import requests

from thalovant import ThalovantAPIError, ThalovantControlPlane
from tests.test_control import FakeResponse


class ConditionalConfigServer:
    def __init__(self):
        self.config = {"env": [{"name": "REDIS_HOST", "value": "redis.internal"}]}
        self.revision = 1
        self.lock = Lock()
        self.first_reads = Barrier(2)
        self.read_count = 0
        self.conflicts = 0

    def request(self, method, url, **kwargs):
        assert url.endswith("/v1/runtime-groups/g/config")
        if method == "GET":
            with self.lock:
                self.read_count += 1
                first_read = self.read_count <= 2
                result = {"config": deepcopy(self.config), "revision": f"{self.revision:064x}"}
            if first_read:
                self.first_reads.wait(timeout=5)
            return FakeResponse(200, result)
        assert method in {"PUT", "PATCH"}
        with self.lock:
            body = kwargs["json"]
            if method == "PUT" and body["expected_revision"] != f"{self.revision:064x}":
                self.conflicts += 1
                return FakeResponse(412, {"detail": "Configuration changed"})
            self.config = deepcopy(body["config"])
            self.revision += 1
            return FakeResponse(200, {"config": deepcopy(self.config)})


def plane(session):
    api = ThalovantControlPlane("https://api.example.test", session=session)
    api.access_token = "test-token"
    return api


def test_two_clients_remerge_original_deltas_after_conflict():
    server = ConditionalConfigServer()
    original_env = deepcopy(server.config["env"])
    first, second = plane(server), plane(server)
    deltas = [{"intents": {"one": True}}, {"intents": {"two": True}}]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(api.update_runtime_group_config, "g", delta)
                   for api, delta in zip((first, second), deltas)]
        for future in futures:
            future.result(timeout=10)
    assert server.config == {"env": original_env, "intents": {"one": True, "two": True}}
    assert server.conflicts == 1
    assert server.read_count == 3
    assert deltas == [{"intents": {"one": True}}, {"intents": {"two": True}}]


class ScriptedSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.methods = []

    def request(self, method, url, **kwargs):
        self.methods.append(method)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


SNAPSHOT = FakeResponse(200, {"config": {"env": []}, "revision": "a" * 64})


@pytest.mark.parametrize("failure", [302, 307, 400, 401, 403, 404, 405, 409, 422, 429, 500, 503])
def test_only_revision_conflicts_are_retried(failure):
    session = ScriptedSession([SNAPSHOT, FakeResponse(failure, {"detail": "Failure"})])
    with pytest.raises(ThalovantAPIError) as exc:
        plane(session).update_runtime_group_config("g", {"lang": "fr-fr"})
    assert exc.value.status_code == failure
    assert session.methods == ["GET", "PUT"]


def test_conflicts_are_bounded_to_three_write_attempts():
    session = ScriptedSession([SNAPSHOT, FakeResponse(412, {"detail": "Changed"})] * 3)
    with pytest.raises(ThalovantAPIError) as exc:
        plane(session).update_runtime_group_config("g", {"lang": "fr-fr"})
    assert exc.value.status_code == 412
    assert session.methods == ["GET", "PUT"] * 3


@pytest.mark.parametrize("failure", [requests.Timeout(), requests.ConnectionError()])
def test_uncertain_write_is_never_replayed(failure):
    session = ScriptedSession([SNAPSHOT, failure])
    with pytest.raises(ThalovantAPIError):
        plane(session).update_runtime_group_config("g", {"lang": "fr-fr"})
    assert session.methods == ["GET", "PUT"]


@pytest.mark.parametrize("revision", [None, "", 1, "legacy", "g" * 64])
def test_old_or_invalid_revision_prevents_any_write(revision):
    session = ScriptedSession([FakeResponse(200, {"config": {}, "revision": revision})])
    with pytest.raises(ThalovantAPIError, match="does not support safe"):
        plane(session).update_runtime_group_config("g", {"lang": "fr-fr"})
    assert session.methods == ["GET"]


@pytest.mark.parametrize("config", [None, [], "invalid"])
def test_malformed_snapshot_is_not_replaced_with_empty_config(config):
    session = ScriptedSession([FakeResponse(200, {"config": config, "revision": "a" * 64})])
    with pytest.raises(ThalovantAPIError, match="invalid runtime configuration"):
        plane(session).update_runtime_group_config("g", {"lang": "fr-fr"})
    assert session.methods == ["GET"]


@pytest.mark.parametrize("args", [(), ("failure",), ("failure", "context")])
def test_http_status_preserves_existing_exception_construction(args):
    error = ThalovantAPIError(*args, status_code=412)
    assert error.args == args
    assert error.status_code == 412
    assert str(error) == str(Exception(*args))


@pytest.mark.parametrize("error", ["access_denied", "expired_token"])
def test_device_sign_in_errors_keep_their_http_status(error):
    session = ScriptedSession([FakeResponse(400, {"error": error})])
    with pytest.raises(ThalovantAPIError) as exc:
        plane(session)._poll_device_token("test-device", interval=1, timeout=5)
    assert exc.value.status_code == 400
    assert session.methods == ["POST"]


@pytest.mark.parametrize("payload", [[], "invalid", None])
def test_malformed_http_response_keeps_its_status(payload):
    session = ScriptedSession([FakeResponse(200, payload)])
    with pytest.raises(ThalovantAPIError) as exc:
        plane(session).get_runtime_group_config("g")
    assert exc.value.status_code == 200
