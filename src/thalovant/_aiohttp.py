"""The HTTP client the SDK owns: its session, its trust store, its limits.

A caller may hand in its own ``aiohttp.ClientSession`` (Home Assistant does);
it is used as given and never closed. When the SDK makes its own:

- **Trust** follows the order requests used, so nothing that verified before
  stops verifying: ``REQUESTS_CA_BUNDLE``, ``CURL_CA_BUNDLE`` and
  ``SSL_CERT_FILE`` first, then certifi's bundle when certifi is installed,
  then the system store.
- **Resolution** goes through ``socket.getaddrinfo`` on a worker thread with
  no DNS cache, so ``thalovant.session.preferred_origin`` -- a scoped override
  of that function -- still decides where a hub is dialled.
"""

from __future__ import annotations

import os
import ssl
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    import aiohttp

__all__ = ["client_ssl", "new_session", "proxy_for"]


@lru_cache(maxsize=4)
def _verified_context(bundle: str | None) -> ssl.SSLContext:
    if bundle:
        return ssl.create_default_context(
            cafile=bundle if os.path.isfile(bundle) else None,
            capath=bundle if os.path.isdir(bundle) else None,
        )
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def client_ssl(*, self_signed: bool = False) -> ssl.SSLContext | bool:
    """What to pass as ``ssl=`` for one request or socket."""
    if self_signed:
        return False
    bundle = next(
        (
            value
            for value in (
                os.environ.get("REQUESTS_CA_BUNDLE"),
                os.environ.get("CURL_CA_BUNDLE"),
                os.environ.get("SSL_CERT_FILE"),
            )
            if value
        ),
        None,
    )
    return _verified_context(bundle)


def new_session(**kwargs: Any) -> aiohttp.ClientSession:
    """An SDK-owned session. Call from inside the loop that will use it."""
    import aiohttp

    connector = aiohttp.TCPConnector(
        resolver=aiohttp.ThreadedResolver(),
        use_dns_cache=False,
        ssl=client_ssl(),
    )
    return aiohttp.ClientSession(connector=connector, **kwargs)


def proxy_for(url: str) -> str | None:
    """The proxy the environment names for *url*, the way requests found it.

    ``HTTPS_PROXY``, ``HTTP_PROXY``, ``ALL_PROXY`` and ``NO_PROXY``; a
    WebSocket follows its HTTP scheme. Credentials in ``.netrc`` are never
    read, unlike aiohttp's own ``trust_env``.
    """
    import urllib.request
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    scheme = {"wss": "https", "ws": "http"}.get(parsed.scheme, parsed.scheme)
    proxies = urllib.request.getproxies_environment()
    if not proxies:
        return None
    try:
        if urllib.request.proxy_bypass_environment(parsed.hostname or "", proxies):
            return None
    except Exception:  # noqa: BLE001 - a malformed NO_PROXY is no reason to fail
        pass
    return proxies.get(scheme) or proxies.get("all") or None
