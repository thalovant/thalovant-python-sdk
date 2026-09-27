"""What a control-plane error carries, against the shared vectors.

`contracts/conformance/api-error-vectors.json` is part of the parity reference,
so every SDK runs the same cases. The API answers a refusal with a Problem+JSON
body whose structured fields say what to do next -- the images a caller may
pin instead, the plan's numbers -- and a message cut at 200 characters is not
where anybody can read them. Each case is served by a real HTTP peer and read
back through the public client, so what is recorded is what a caller gets.
"""

from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any, Iterator

import pytest

from thalovant import ThalovantAPIError, ThalovantControlPlane
from conformance_record import record

CONFORMANCE = Path(__file__).resolve().parents[1] / "contracts" / "conformance"


def load_vectors(name: str) -> dict[str, Any]:
    return json.loads((CONFORMANCE / name).read_text(encoding="utf-8"))


VECTORS = load_vectors("api-error-vectors.json")


@contextmanager
def answering(response: dict[str, Any]) -> Iterator[str]:
    """A loopback API that answers every request with ``response``, byte for byte."""

    body = response["body"].encode("utf-8")

    class Peer(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(response["status"])
            self.send_header("content-type", response["content_type"])
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
    worker = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}"
    finally:
        http.shutdown()
        http.server_close()
        worker.join(1)


def refusal(response: dict[str, Any]) -> ThalovantAPIError:
    with answering(response) as url:
        api = ThalovantControlPlane(url, access_token="synthetic-token")
        with pytest.raises(ThalovantAPIError) as raised:
            api.get_hub("hub-1")
    return raised.value


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
def test_an_api_error_carries_what_its_vector_names(case):
    error = refusal(case["response"])
    produced = {
        "status": error.status_code,
        "code": error.code,
        "detail": error.detail,
        "problem": error.problem,
    }
    # Recorded before the assert: the record is what this SDK produced, not a
    # restatement of what the vector says it should have.
    record("api-error-vectors.json", case["name"], produced)
    assert produced == case["expect"]
    for echoed in case.get("message_excludes", []):
        assert echoed not in str(error)
        assert echoed not in repr(error)


def test_the_message_may_be_shortened_but_the_detail_never_is():
    case = next(case for case in VECTORS["cases"] if case["expect"]["code"] == "platform_image_required")
    error = refusal(case["response"])
    # The display line is what it always was: one bounded line, with the code.
    assert str(error).startswith("Thalovant API request failed with HTTP 403: Only an administrator")
    assert str(error).endswith("... (platform_image_required)")
    # The sentence the API wrote is whole, and every list it sent is there.
    assert error.detail == case["expect"]["detail"]
    assert len(error.detail) > 200
    assert error.problem is not None
    assert error.problem["allowed_images"]["core"] == [
        "ghcr.io/thalovant/ovos-core:2026.09.2",
        "ghcr.io/thalovant/ovos-core:2026.09.3-alpha.1",
    ]
    assert error.problem["allowed_repositories"] == {"core": "ghcr.io/thalovant/ovos-core"}
    assert error.problem["refused_images"]["bus"] == "docker.io/example/ovos-messagebus:custom"


def test_an_error_raised_the_old_way_still_reads_the_old_way():
    error = ThalovantAPIError("Missing Thalovant API access token.")
    assert (error.status_code, error.code, error.detail, error.problem) == (None, None, None, None)
    assert str(error) == "Missing Thalovant API access token."

    error = ThalovantAPIError("conflict", status_code=412)
    assert error.status_code == 412
    assert (error.code, error.detail, error.problem) == (None, None, None)


def test_a_problem_alone_gives_its_code_and_detail_and_an_explicit_one_wins():
    problem = {"detail": "Free plan allows up to 1 connection.", "code": "plan_limit", "limit": 1}
    error = ThalovantAPIError("refused", status_code=403, problem=problem)
    assert (error.code, error.detail) == ("plan_limit", "Free plan allows up to 1 connection.")
    assert error.problem == problem
    assert error.problem is not problem, "the error keeps its own copy of what it was given"

    error = ThalovantAPIError("refused", status_code=403, code="other", detail="said differently", problem=problem)
    assert (error.code, error.detail) == ("other", "said differently")


def test_the_vectors_cover_every_shape_the_rules_name():
    """A vector set that quietly lost its non-JSON or its nested case would still pass."""
    expects = [case["expect"] for case in VECTORS["cases"]]
    assert any(e["problem"] is None for e in expects)
    assert any(e["code"] and e["detail"] is None for e in expects)
    assert any(e["detail"] and e["code"] is None for e in expects)
    assert any(isinstance(e["problem"], dict) and isinstance(e["problem"].get("detail"), dict) for e in expects)
    assert any(isinstance(e["problem"], dict) and isinstance(e["problem"].get("detail"), list) for e in expects)
    assert any(len(e["detail"] or "") > 256 for e in expects), "no detail longer than any SDK's message limit"
    assert any("\n" in (e["detail"] or "") for e in expects)
    assert any(case.get("message_excludes") for case in VECTORS["cases"])
