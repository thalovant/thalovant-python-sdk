"""The authorization-code grant, which every client that needed it wrote for
itself until this existed.

What is tested is what is a security bug when wrong and looks fine when wrong:
that the challenge really is S256 of the verifier, that the verifier never
reaches the browser, that a redirect answering a different attempt is refused,
and that ``plain`` cannot be asked for.
"""

from __future__ import annotations

from base64 import urlsafe_b64encode
from hashlib import sha256
from urllib.parse import parse_qsl, urlsplit

import pytest

from thalovant.native_auth import (
    DEFAULT_NATIVE_SCOPES,
    begin_native_sign_in,
    challenge_for,
    is_thalovant_url,
    new_verifier,
)


def query(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))


def test_the_challenge_is_the_s256_of_the_verifier() -> None:
    begun = begin_native_sign_in(client_id="thalovant-cli", redirect_uri="http://127.0.0.1:8765/")
    expected = urlsafe_b64encode(sha256(begun.verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    assert query(begun.authorization_url)["code_challenge"] == expected
    assert challenge_for(begun.verifier) == expected


def test_the_verifier_never_reaches_the_browser() -> None:
    begun = begin_native_sign_in(client_id="app", redirect_uri="app://auth")
    assert begun.verifier not in begun.authorization_url


def test_s256_is_the_only_method_offered() -> None:
    begun = begin_native_sign_in(client_id="app", redirect_uri="app://auth")
    assert query(begun.authorization_url)["code_challenge_method"] == "S256"


def test_every_attempt_gets_its_own_verifier_and_state() -> None:
    first = begin_native_sign_in(client_id="app", redirect_uri="app://auth")
    second = begin_native_sign_in(client_id="app", redirect_uri="app://auth")
    assert first.verifier != second.verifier
    assert first.state != second.state
    assert new_verifier() != new_verifier()


def test_the_request_carries_what_the_authorize_endpoint_matches_on() -> None:
    begun = begin_native_sign_in(
        client_id="thalovant-cli",
        redirect_uri="http://127.0.0.1:8765/",
        scopes=["hubs:read", "clients:write"],
    )
    parameters = query(begun.authorization_url)
    assert parameters["client_id"] == "thalovant-cli"
    assert parameters["redirect_uri"] == "http://127.0.0.1:8765/"
    assert parameters["response_type"] == "code"
    assert parameters["scope"] == "hubs:read clients:write"
    assert parameters["state"] == begun.state
    assert begun.authorization_url.startswith("https://dash.thalovant.com/authorize?")


def test_the_default_scopes_are_the_three_a_free_plan_may_mint() -> None:
    begun = begin_native_sign_in(client_id="app", redirect_uri="app://auth")
    assert query(begun.authorization_url)["scope"] == " ".join(DEFAULT_NATIVE_SCOPES)


def test_a_redirect_answering_a_different_attempt_is_refused() -> None:
    begun = begin_native_sign_in(client_id="app", redirect_uri="app://auth")
    assert begun.code_from("app://auth?code=abc&state=somebody-elses") is None
    assert begun.code_from(f"app://auth?code=abc&state={begun.state}") == "abc"


def test_no_code_or_an_error_instead_is_not_success() -> None:
    begun = begin_native_sign_in(client_id="app", redirect_uri="app://auth")
    assert begun.code_from(f"app://auth?state={begun.state}") is None
    assert begun.code_from(f"app://auth?code=&state={begun.state}") is None
    assert begun.code_from(f"app://auth?error=access_denied&state={begun.state}") is None
    assert begun.code_from("app://auth") is None


def test_a_code_with_url_escaped_characters_survives_the_round_trip() -> None:
    begun = begin_native_sign_in(client_id="app", redirect_uri="app://auth")
    assert begun.code_from(f"app://auth?code=a%2Bb%2Fc%3D&state={begun.state}") == "a+b/c="


def test_an_empty_client_or_redirect_is_refused_here_rather_than_at_the_api() -> None:
    with pytest.raises(ValueError):
        begin_native_sign_in(client_id="", redirect_uri="app://auth")
    with pytest.raises(ValueError):
        begin_native_sign_in(client_id="app", redirect_uri="   ")


def test_a_thalovant_url_is_recognised_by_scheme_and_host() -> None:
    assert is_thalovant_url("https://dash.thalovant.com/authorize?x=1")
    assert is_thalovant_url("https://thalovant.com")
    assert not is_thalovant_url("http://dash.thalovant.com")
    # The one that matters: a lookalike host ending in the same letters.
    assert not is_thalovant_url("https://dash.thalovant.com.evil.test")
    # A host that passes, reached through credentials that read as another.
    assert not is_thalovant_url("https://evil.test@dash.thalovant.com")
    assert not is_thalovant_url("https://notthalovant.com")
    assert not is_thalovant_url("nonsense")


def test_a_refusal_that_also_carries_a_code_is_still_a_refusal() -> None:
    # CodeRabbit caught this: checking only for a missing code accepted
    # `error=access_denied&code=...` and would have started an exchange on a
    # code the authorization server had just declined to issue.
    begun = begin_native_sign_in(client_id="app", redirect_uri="app://auth")
    assert begun.code_from(f"app://auth?error=access_denied&code=abc&state={begun.state}") is None
    assert begun.code_from(f"app://auth?code=abc&error=server_error&state={begun.state}") is None


def test_the_token_exchange_refuses_cleartext_and_allows_loopback() -> None:
    from thalovant.control import ThalovantControlPlane
    from thalovant.errors import ThalovantAPIError

    plane = ThalovantControlPlane(api_url="http://control.example.test")
    with pytest.raises(ThalovantAPIError, match="cleartext"):
        plane.complete_native_sign_in("code", "verifier", "app", "app://auth")
    assert plane.access_token is None

    # Loopback has no cleartext to observe, and is how the API is run locally.
    for host in ("http://localhost:8080", "http://127.0.0.1:8080"):
        local = ThalovantControlPlane(api_url=host)
        try:
            local._require_secure_token_exchange()
        except ThalovantAPIError as error:  # pragma: no cover - a failure here is the point
            raise AssertionError(f"loopback was refused: {error}") from error
