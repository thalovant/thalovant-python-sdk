"""Control-plane credential boundaries against local HTTP peers."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import traceback

import pytest
import requests
from thalovant import ThalovantAPIError, ThalovantControlPlane


@contextmanager
def server(handler):
    class Peer(BaseHTTPRequestHandler):
        def do_GET(self):
            handler(self)
        do_POST = do_GET
        def log_message(self, *_):
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


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("auth", ["bearer", "password"])
def test_redirect_never_contacts_target_or_forwards_credentials(status, auth):
    received = []
    sent = []
    def target(peer):
        received.append(peer.path)
        peer.send_response(200)
        peer.end_headers()
        peer.wfile.write(b"{}")
    with server(target) as target_url:
        def origin(peer):
            body = peer.rfile.read(int(peer.headers.get("content-length", 0)))
            sent.append((peer.headers.get("authorization"), body))
            peer.send_response(status)
            peer.send_header("location", target_url + "/credentials")
            peer.end_headers()
        with server(origin) as origin_url:
            api = ThalovantControlPlane(origin_url, access_token="synthetic-token" if auth == "bearer" else None)
            try:
                with pytest.raises(ThalovantAPIError, match="redirects are disabled"):
                    if auth == "bearer":
                        api.list_hubs()
                    else:
                        api.login("synthetic@example.invalid", "synthetic-password")
                assert len(sent) == 1
                if auth == "bearer":
                    assert sent[0][0] == "Bearer synthetic-token"
                else:
                    assert json.loads(sent[0][1])["password"] == "synthetic-password"
                assert received == []
            finally:
                api.session.close()


def test_credential_transport_policy_checks_before_requests_and_keeps_explicit_loopback(monkeypatch):
    calls = []
    def request(session, method, url, **kwargs):
        calls.append((url, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"access_token":"synthetic-token","hubs":[]}'
        return response
    monkeypatch.setattr(requests.Session, "request", request)
    for url in ("http://example.invalid", "http://localhost.example.invalid", "ftp://127.0.0.1", "https://user:synthetic-password@example.invalid"):
        with pytest.raises(ThalovantAPIError):
            ThalovantControlPlane(url, access_token="synthetic-token").list_hubs()
        with pytest.raises(ThalovantAPIError):
            ThalovantControlPlane(url).login("synthetic@example.invalid", "synthetic-password")
    assert calls == []
    for url in ("https://custom.example.invalid", "http://localhost", "http://127.0.0.1", "http://[::1]"):
        ThalovantControlPlane(url, access_token="synthetic-token").list_hubs()
        ThalovantControlPlane(url).login("synthetic@example.invalid", "synthetic-password")
    assert len(calls) == 8
    assert all(kwargs["allow_redirects"] is False for _, kwargs in calls)


def test_request_exception_traceback_does_not_expose_credentials(monkeypatch):
    secret = "synthetic-secret-do-not-log"
    def request(*args, **kwargs):
        raise requests.ConnectionError("https://example.invalid?authorization=" + secret)
    monkeypatch.setattr(requests.Session, "request", request)
    with pytest.raises(ThalovantAPIError) as caught:
        ThalovantControlPlane(access_token="synthetic-token").list_hubs()
    assert secret not in "".join(traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__))
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
