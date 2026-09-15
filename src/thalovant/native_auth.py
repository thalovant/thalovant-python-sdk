"""The authorization-code grant with PKCE (RFC 7636), for a client that can
open a browser.

``login_with_browser`` is the device grant, and it exists for something that
*cannot* open one: somebody reads a code off one screen and types it into
another. A desktop tool, or an app on a phone, is not in that position -- it
can open the browser itself and be handed the answer back -- and asking its
user to copy a code between two windows on the same machine is a worse
experience than the one every other tool on that machine offers.

The API grew this grant and no SDK followed it, so ``thalovant-android`` wrote
its own verifier, challenge, URL builder and redirect check. Every client that
needs this needs the same four things, and getting any of them subtly wrong is
a security bug rather than a visible one.

    begun = begin_native_sign_in(client_id="my-app", redirect_uri="myapp://auth")
    webbrowser.open(begun.authorization_url)
    code = begun.code_from(redirect)          # verifies state; None when not ours
    plane.complete_native_sign_in(code, begun.verifier, "my-app", "myapp://auth")

``begun.verifier`` never leaves the process and never enters the browser. That
is what PKCE is for: a code intercepted by whatever else claimed the redirect
is useless without it.
"""

from __future__ import annotations

from base64 import urlsafe_b64encode
from dataclasses import dataclass
from hashlib import sha256
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit

__all__ = [
    "DEFAULT_DASHBOARD_URL",
    "DEFAULT_NATIVE_SCOPES",
    "NativeSignIn",
    "begin_native_sign_in",
    "challenge_for",
    "is_thalovant_url",
    "new_verifier",
]

#: Where a person approves the request.
DEFAULT_DASHBOARD_URL = "https://dash.thalovant.com"

#: The three a phone needs; also the three a free plan may mint.
DEFAULT_NATIVE_SCOPES = ("hubs:read", "clients:read", "clients:write")


def _base64url(raw: bytes) -> str:
    return urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_verifier() -> str:
    """A PKCE verifier: 64 random bytes, base64url, no padding."""

    return _base64url(secrets.token_bytes(64))


def challenge_for(verifier: str) -> str:
    """The S256 challenge for a verifier."""

    return _base64url(sha256(verifier.encode("ascii")).digest())


def is_thalovant_url(url: str) -> bool:
    """Whether a URL belongs to Thalovant, for a caller that wants to show
    where it is about to send somebody.

    Scheme and host only: a display check, not an authorization one.
    """

    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        return False
    # Reject embedded credentials: ``https://evil.test@dash.thalovant.com/``
    # has a host that passes, and a URL somebody is about to be sent to should
    # not read as one host and resolve to another.
    if parts.username or parts.password:
        return False
    host = (parts.hostname or "").lower()
    return host == "thalovant.com" or host.endswith(".thalovant.com")


@dataclass(frozen=True)
class NativeSignIn:
    """One sign-in attempt in progress.

    Keep it until the browser comes back; it holds the two secrets that make
    the round trip safe.
    """

    #: Open this in a browser.
    authorization_url: str
    #: Proves the redirect answers *this* attempt and not a replayed one.
    state: str
    #: Never send this to the browser. Exchanged with the code, once.
    verifier: str

    def code_from(self, redirect: str) -> str | None:
        """The authorization code out of the redirect, or ``None`` when it is
        not an answer to this attempt.

        ``None`` rather than an exception on a state mismatch, a missing code,
        or an ``error=`` response -- including one that also carries a code:
        all of those mean "do not continue", and a
        caller that handles them alike cannot accidentally treat one of them as
        success.
        """

        query = urlsplit(redirect).query
        if not query:
            return None
        found = dict(parse_qsl(query, keep_blank_values=True))
        if found.get("state") != self.state:
            return None
        # A refusal that also carries a code is still a refusal. Checking only
        # for a missing code accepted that pair and would have started an
        # exchange on a code the server had just declined to issue.
        if "error" in found:
            return None
        code = found.get("code") or ""
        return code or None


def begin_native_sign_in(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: tuple[str, ...] | list[str] | None = None,
    dashboard_url: str = DEFAULT_DASHBOARD_URL,
) -> NativeSignIn:
    """Start a sign-in. Returns the URL to open and the secrets to keep.

    ``redirect_uri`` must be one the API has registered for ``client_id``; the
    authorization endpoint matches it exactly and refuses anything else, so it
    cannot be turned into an open redirect.
    """

    if not client_id.strip():
        raise ValueError("client_id is required to start a sign-in.")
    if not redirect_uri.strip():
        raise ValueError("redirect_uri is required to start a sign-in.")
    verifier = new_verifier()
    state = _base64url(secrets.token_bytes(24))
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge": challenge_for(verifier),
            # S256 only. `plain` is refused by the API, and offering it here
            # would only give a caller a way to ask for the weaker one.
            "code_challenge_method": "S256",
            "scope": " ".join(scopes if scopes is not None else DEFAULT_NATIVE_SCOPES),
            "state": state,
        }
    )
    return NativeSignIn(
        authorization_url=f"{dashboard_url.rstrip('/')}/authorize?{query}",
        state=state,
        verifier=verifier,
    )
