"""Which 422s mean "this API cannot make that kind of connection".

Only what the problem says about itself counts -- its detail and code, and
each validation error's loc and msg. A validation error echoes the request
as its ``input``, and the request always carries ``spec.connection_type``.
"""

from __future__ import annotations

import pytest

from thalovant import ThalovantAPIError
from thalovant.control import _refuses_connection_type

ECHO = {"version": "1", "connection_type": "home_assistant"}


@pytest.mark.parametrize(
    ("problem", "refuses"),
    [
        ({"detail": "Schema validation failed at spec.connection_type", "code": "schema_validation_failed"}, True),
        ({"detail": "Unsupported kind", "code": "unsupported_connection_type"}, True),
        ({"detail": "Request validation failed",
          "errors": [{"loc": ["body", "spec", "connection_type"], "msg": "Input should be 'embedded'", "input": "x"}]}, True),
        ({"detail": "Request validation failed",
          "errors": [{"loc": ["body", "spec"], "msg": "connectionType is not allowed here", "input": {}}]}, True),
        # FastAPI's own shape: the list under detail.
        ({"detail": [{"loc": ["body", "spec", "connection_type"], "msg": "bad", "input": "x"}]}, True),
        # Echoes only: never a refusal of the kind.
        ({"detail": "Request validation failed",
          "errors": [{"loc": ["body", "spec", "siteId"], "msg": "Field required", "input": ECHO}]}, False),
        ({"detail": [{"loc": ["body", "name"], "msg": "String too long", "input": "connection_type"}]}, False),
        ({"detail": "Invalid spec", "code": "invalid_spec", "spec": ECHO}, False),
    ],
)
def test_only_what_the_problem_says_about_itself_counts(problem, refuses):
    error = ThalovantAPIError("refused", status_code=422, problem=problem)
    assert _refuses_connection_type(error) is refuses


def test_another_status_is_never_a_kind_refusal():
    error = ThalovantAPIError("refused", status_code=400, problem={"detail": "bad connection_type"})
    assert not _refuses_connection_type(error)


@pytest.mark.parametrize(
    ("problem", "seconds"),
    [
        ({"detail": {"code": "token_rate_limited", "retry_after_seconds": 7}}, 7.0),  # what the API sends
        ({"code": "token_rate_limited", "retry_after_seconds": 3}, 3.0),  # lifted to the top
        ({"detail": {"retry_after_seconds": "soon"}}, None),
        ({"retry_after_seconds": True}, None),
        ({"retry_after_seconds": -1}, None),
        (None, None),
    ],
)
def test_a_429_names_its_wait_at_the_top_or_inside_detail(problem, seconds):
    error = ThalovantAPIError("slow down", status_code=429, problem=problem)
    assert error.retry_after_seconds == seconds


@pytest.mark.parametrize(
    ("headers", "seconds"),
    [
        ({"Retry-After": "4"}, 4.0),
        ({"RateLimit-Reset": "9", "RateLimit-Remaining": "0"}, 9.0),  # the API's own rate limiter
        ({"Retry-After": "2", "RateLimit-Reset": "9"}, 2.0),
        ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),
        ({}, None),
    ],
)
def test_a_plain_429_names_its_wait_in_a_header(headers, seconds):
    from thalovant.control import _api_error, _Response

    error = _api_error(_Response(429, "Too Many Requests", headers))
    assert error.status_code == 429 and error.problem is None
    assert error.retry_after_seconds == seconds
