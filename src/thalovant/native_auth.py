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

The same grant signs a *program* in -- a script, a command line, the MCP
server -- through ``ThalovantControlPlane.browser_login``, which listens on a
loopback port for the answer (RFC 8252 section 7.3). The pieces it is made of
live here, so that each can be checked against the shared vectors on its own:
``browser_authorization_url`` builds the address the browser opens,
``loopback_redirect_uri`` the redirect it comes back to, ``new_state`` the
value that ties the two together, ``NativeSignInConfig`` reads where the API
says to send the browser, and ``can_open_browser`` says whether this machine
has a browser to send. A loopback redirect cannot be relayed: the browser that
approves hands the code to its own computer, never to whoever sent the link.
"""

from __future__ import annotations

from base64 import urlsafe_b64encode
from dataclasses import dataclass
from hashlib import sha256
import os
import re
import secrets
import socket
import sys
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

__all__ = [
    "DEFAULT_BROWSER_LOGIN_TIMEOUT",
    "DEFAULT_DASHBOARD_URL",
    "DEFAULT_NATIVE_SCOPES",
    "LOOPBACK_CALLBACK_PATH",
    "MAX_DEVICE_NAME_LENGTH",
    "NativeSignIn",
    "NativeSignInConfig",
    "SDK_CLIENT_ID",
    "begin_native_sign_in",
    "browser_authorization_url",
    "can_open_browser",
    "challenge_for",
    "default_device_name",
    "is_thalovant_url",
    "loopback_redirect_uri",
    "new_state",
    "new_verifier",
]

#: Where a person approves the request.
DEFAULT_DASHBOARD_URL = "https://dash.thalovant.com"

#: The three a phone needs; also the three a free plan may mint.
DEFAULT_NATIVE_SCOPES = ("hubs:read", "clients:read", "clients:write")

#: The SDKs' own registered app, which a program signs in as through the
#: browser. ``GET /v1/auth/native/config`` names it as ``sdk_client_id``.
SDK_CLIENT_ID = "thalovant-sdk"
#: The one path the loopback listener answers.
LOOPBACK_CALLBACK_PATH = "/callback"
#: Seconds a browser sign-in waits for the browser to come back.
DEFAULT_BROWSER_LOGIN_TIMEOUT = 300.0
#: The longest ``device_name`` the consent page shows and the token is named by,
#: in Unicode code points (the API refuses a longer one).
MAX_DEVICE_NAME_LENGTH = 120
#: The name this SDK gives itself in ``default_device_name``.
SDK_DEVICE_LABEL = "thalovant-python-sdk"


def _base64url(raw: bytes) -> str:
    return urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_verifier() -> str:
    """A PKCE verifier: 64 random bytes, base64url, no padding."""

    return _base64url(secrets.token_bytes(64))


def challenge_for(verifier: str) -> str:
    """The S256 challenge for a verifier."""

    return _base64url(sha256(verifier.encode("ascii")).digest())


def new_state() -> str:
    """An OAuth ``state``: 24 random bytes (192 bits), base64url, no padding.

    It ties the answer the browser brings back to the request that sent it.
    Compare it in constant time.
    """

    return _base64url(secrets.token_bytes(24))


def loopback_redirect_uri(port: int) -> str:
    """``http://127.0.0.1:<port>/callback``, the one redirect a program is answered on.

    The IP literal, never ``localhost``, which a hosts file or a resolver can
    send elsewhere; that path; no query, no trailing slash. The API matches it
    exactly and only for an unprivileged port, which is what a port bound as
    port 0 always is.
    """

    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError(f"A loopback redirect needs a port from 1024 to 65535, not {port!r}.")
    return f"http://127.0.0.1:{port}{LOOPBACK_CALLBACK_PATH}"


def _query_value(value: str) -> str:
    """RFC 3986 percent-encoding: unreserved characters as they are, every other
    byte of the UTF-8 form as ``%XX``, a space as ``%20``."""

    return quote(value, safe="")


def browser_authorization_url(
    authorization_endpoint: str,
    *,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scopes: Iterable[str] | None = None,
    device_name: str | None = None,
    client_id: str = SDK_CLIENT_ID,
) -> str:
    """The address a browser sign-in opens.

    ``{authorization_endpoint}?response_type=code&client_id=...&redirect_uri=...
    &code_challenge=...&code_challenge_method=S256&state=...&scope=...
    &device_name=...``, in that order, every value percent-encoded as RFC 3986
    has it (a space is ``%20``). Empty strings among ``scopes`` are dropped and
    the rest joined by one space; ``scope`` is left out when nothing is left,
    and the API then grants its default (``hubs:read clients:write``).
    ``device_name`` is cut to :data:`MAX_DEVICE_NAME_LENGTH` code points, and
    left out when empty.
    """

    parameters = [
        ("response_type", "code"),
        ("client_id", client_id),
        ("redirect_uri", redirect_uri),
        ("code_challenge", code_challenge),
        # S256 only: `plain` puts the verifier in the browser.
        ("code_challenge_method", "S256"),
        ("state", state),
    ]
    scope = " ".join(value for value in (scopes or ()) if value)
    if scope:
        parameters.append(("scope", scope))
    if device_name:
        parameters.append(("device_name", device_name[:MAX_DEVICE_NAME_LENGTH]))
    query = "&".join(f"{key}={_query_value(value)}" for key, value in parameters)
    return f"{authorization_endpoint}?{query}"


def default_device_name() -> str:
    """``thalovant-python-sdk on <hostname>``: what the consent page and the token show."""

    try:
        host = socket.gethostname().strip()
    except OSError:
        host = ""
    name = f"{SDK_DEVICE_LABEL} on {host}" if host else SDK_DEVICE_LABEL
    return name[:MAX_DEVICE_NAME_LENGTH]


def _platform_name() -> str:
    """This machine as the browser rule names it: ``linux``, ``macos``, ``windows`` or another."""

    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def can_open_browser(environ: Mapping[str, str] | None = None, platform: str | None = None) -> bool:
    """Whether this machine can show a browser to the person signing in.

    Never in CI (``CI`` set) or over SSH (``SSH_CONNECTION`` or ``SSH_TTY``
    set): the browser would open on another machine, or nowhere. Otherwise
    yes on ``macos`` and ``windows``; anywhere else -- Linux, the BSDs -- only
    with a display (``DISPLAY`` or ``WAYLAND_DISPLAY``). A variable counts as
    set when it has a value: an empty one is not set. ``environ`` and
    ``platform`` default to this process's.
    """

    env: Mapping[str, str] = os.environ if environ is None else environ

    def present(name: str) -> bool:
        return bool(env.get(name))

    if present("CI") or present("SSH_CONNECTION") or present("SSH_TTY"):
        return False
    system = _platform_name() if platform is None else platform
    if system in {"macos", "windows"}:
        return True
    return present("DISPLAY") or present("WAYLAND_DISPLAY")


_CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _usable_authorization_endpoint(value: Any) -> bool:
    """https, or http on a loopback host while developing; a host; no
    credentials, query, fragment, whitespace or control characters."""

    if not isinstance(value, str) or not value:
        return False
    if any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        return False
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018 - raises ValueError for a port no browser can open
    except ValueError:
        return False
    if not host or "@" in parts.netloc or parts.query or parts.fragment or "?" in value or "#" in value:
        return False
    scheme = parts.scheme.lower()
    return scheme == "https" or (scheme == "http" and host in {"localhost", "127.0.0.1", "::1"})


def _usable_token_endpoint(value: Any) -> bool:
    """A path on the API: one leading slash, and no colon, query, fragment,
    backslash or whitespace.

    No colon, because a URL joiner can read what follows the slash as a URL of
    its own: ``/https://collector.example/token`` and
    ``/collector.example:443/token`` both leave the API's origin once the
    slash is taken off, and the exchange carries the code and the verifier.
    """

    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        return False
    return not any(
        char in ":?#\\" or char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 for char in value
    )


@dataclass(frozen=True)
class NativeSignInConfig:
    """Where a program signs in through the browser (``GET /v1/auth/native/config``).

    ``authorization_endpoint`` is the dashboard page the browser opens;
    ``token_endpoint`` the path on the API the code is exchanged at;
    ``sdk_client_id`` the registered app the program signs in as. The API
    reads it from its own settings, so a staging or self-hosted API sends the
    browser to its own dashboard.
    """

    authorization_endpoint: str
    token_endpoint: str
    sdk_client_id: str
    device_authorization_endpoint: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> NativeSignInConfig:
        """Read the answer; refuse one a browser sign-in cannot safely use.

        Unusable (``ValueError``): an ``authorization_endpoint`` that is not
        https (or http on a loopback host), carries credentials, a query or a
        fragment; a ``token_endpoint`` that is not a path on the API (a colon
        anywhere in it included); a missing
        ``sdk_client_id``; or ``code_challenge_methods_supported`` without
        ``S256``. Other members are ignored.
        """

        if not isinstance(payload, Mapping):
            raise ValueError("The browser sign-in configuration is not an object.")
        authorization = payload.get("authorization_endpoint")
        token = payload.get("token_endpoint")
        client_id = payload.get("sdk_client_id")
        methods = payload.get("code_challenge_methods_supported")
        if not _usable_authorization_endpoint(authorization):
            raise ValueError("The browser sign-in configuration has no usable authorization_endpoint.")
        if not _usable_token_endpoint(token):
            raise ValueError("The browser sign-in configuration has no usable token_endpoint.")
        if not isinstance(client_id, str) or not _CLIENT_ID.fullmatch(client_id):
            raise ValueError("The browser sign-in configuration has no usable sdk_client_id.")
        if not isinstance(methods, list) or "S256" not in methods:
            raise ValueError("The browser sign-in configuration does not offer S256.")
        device = payload.get("device_authorization_endpoint")
        return cls(
            authorization_endpoint=str(authorization),
            token_endpoint=str(token),
            sdk_client_id=client_id,
            device_authorization_endpoint=device if isinstance(device, str) and device else None,
        )


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


def _require_safe_dashboard(url: str) -> None:
    """The authorization request carries the challenge, scopes and state.

    A caller may point this at their own dashboard -- a self-hosted control
    plane is a real thing -- but not at a cleartext one, and not at one whose
    address reads as a different host than it resolves to. Loopback is allowed
    because it never leaves the machine.
    """

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.username or parts.password:
        raise ValueError("dashboard_url must not carry credentials.")
    # A query or a fragment breaks the address this builds. "<dash>#x" becomes
    # "<dash>#x/authorize?client_id=..." -- every parameter lands in the
    # fragment, which a browser never sends, so the authorize endpoint receives
    # nothing and says nothing. A query mangles the path the same way. A plain
    # path is fine: "<dash>/app" gives "<dash>/app/authorize".
    if parts.query or parts.fragment:
        raise ValueError("dashboard_url must not carry a query or a fragment.")
    if parts.scheme.lower() == "https":
        return
    if parts.scheme.lower() == "http" and host in {"localhost", "127.0.0.1", "::1"}:
        return
    raise ValueError(
        f"dashboard_url must be https (or a loopback address while developing), not {url!r}."
    )


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
    #: The redirect this attempt asked for. A callback that arrives at some
    #: other address is not this attempt's, however good its state looks.
    redirect_uri: str

    def code_from(self, redirect: str) -> str | None:
        """The authorization code out of the redirect, or ``None`` when it is
        not an answer to this attempt.

        ``None`` rather than an exception on a state mismatch, a missing code,
        or an ``error=`` response -- including one that also carries a code:
        all of those mean "do not continue", and a
        caller that handles them alike cannot accidentally treat one of them as
        success.
        """

        parts = urlsplit(redirect)
        if not parts.query:
            return None
        # The callback has to arrive where this attempt asked it to. State
        # proves the answer belongs to this request; the address proves it came
        # back to the app that made it, and not to some other page that was
        # handed the same query string.
        wanted = urlsplit(self.redirect_uri)
        if (parts.scheme.lower(), (parts.hostname or "").lower(), parts.path.rstrip("/")) != (
            wanted.scheme.lower(), (wanted.hostname or "").lower(), wanted.path.rstrip("/")
        ):
            return None
        found = dict(parse_qsl(parts.query, keep_blank_values=True))
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
    _require_safe_dashboard(dashboard_url)
    verifier = new_verifier()
    state = new_state()
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
        redirect_uri=redirect_uri,
    )
