"""Real HTTPS admission cleanup failures and ownership boundaries."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import asyncio
import threading
import traceback

import pytest
import requests

from thalovant import AsyncThalovantClient, ThalovantClient, ThalovantConnectionError
from thalovant.transport import HiveMindHTTPTransport
from test_noise_transports import http_peer, identity


@pytest.mark.parametrize("body,status", [
    ({"error": "disconnect refused"}, 200),
    ({}, 200),
    ({"status": "Connected"}, 200),
    ({"status": "Disconnected", "ok": False}, 200),
    (b"invalid JSON", 200),
    ({"status": "Disconnected"}, 503),
])
def test_failed_disconnect_retains_admission_affinity_and_requires_explicit_retry(http_peer, tmp_path, body, status):
    peer, endpoint = http_peer
    transport = HiveMindHTTPTransport(identity(endpoint), useragent="cleanup", noise_state_dir=str(tmp_path / "client"),
                                      handshake_poll_interval=0.01)
    client = ThalovantClient(identity(endpoint), transport=transport)
    client.connect()
    old = transport._client
    pin = peer.pin
    peer.disconnect_response, peer.disconnect_status = body, status
    try:
        with pytest.raises(ThalovantConnectionError, match="disconnect"):
            client.close()
        with pytest.raises(ThalovantConnectionError, match="disconnect"):
            client.wait_closed(timeout=1)
        assert peer.admitted and old._admitted
        assert peer.pin == pin
        assert old._session.cookies.get("hivemind_http_replica") == "one"
        assert transport.connection_info().phase == "error"
        for connect in (client.connect, transport.connect):
            with pytest.raises(ThalovantConnectionError, match="cleanup"):
                connect()
        assert peer.connects == 1 and peer.disconnects == 1
        peer.disconnect_response, peer.disconnect_status = {"status": "Disconnected"}, 200
        client.close()
        client.wait_closed(timeout=1)
        assert not peer.admitted and not old._admitted
        assert transport.connection_info().phase == "closed"
        client.close()
        assert peer.disconnects == 2, "successful cleanup must remain idempotent"
        client.connect()
        assert peer.patterns == ["XXpsk2", "KKpsk0"]
    finally:
        peer.disconnect_response, peer.disconnect_status = {"status": "Disconnected"}, 200
        client.close()


def test_close_waits_for_connect_admission_publication_before_cleanup(http_peer, tmp_path):
    peer, endpoint = http_peer
    peer.connect_entered, peer.connect_gate = threading.Event(), threading.Event()
    transport = HiveMindHTTPTransport(identity(endpoint), useragent="cleanup", noise_state_dir=str(tmp_path / "client"),
                                      handshake_poll_interval=0.01)
    with ThreadPoolExecutor(max_workers=2) as pool:
        connecting = pool.submit(transport.connect)
        assert peer.connect_entered.wait(2)
        old = transport._client
        closing = pool.submit(transport.disconnect)
        try:
            assert old._stop.wait(2)
            with pytest.raises(FutureTimeout):
                closing.result(timeout=0.03)
            with pytest.raises(ThalovantConnectionError, match="in progress"):
                transport.connect()
            peer.connect_gate.set()
            closing.result(timeout=2)
            with pytest.raises(ThalovantConnectionError):
                connecting.result(timeout=2)
            assert not peer.admitted and not old._admitted
            assert peer.connects == 1 and peer.disconnects == 1
        finally:
            peer.connect_gate.set()
    transport.disconnect()


def test_http_request_connection_error_cannot_expose_authorization(http_peer, tmp_path, monkeypatch):
    peer, endpoint = http_peer
    transport = HiveMindHTTPTransport(identity(endpoint), useragent="cleanup", noise_state_dir=str(tmp_path / "client"),
                                      handshake_poll_interval=0.01)
    transport.connect()
    old = transport._client
    def fail(*args, **kwargs):
        raise requests.ConnectionError(f"request failed: {endpoint}/disconnect?authorization={old.auth}")
    try:
        with monkeypatch.context() as scope:
            scope.setattr(old._session, "request", fail)
            with pytest.raises(ThalovantConnectionError) as caught:
                old.request("/disconnect", method="POST")
            rendered = "".join(traceback.format_exception(caught.value))
            assert old.auth not in rendered
            assert "authorization=" not in rendered
        assert peer.admitted
    finally:
        transport.disconnect()


def test_primary_connect_error_survives_failed_cleanup_and_wait_closed_observes_it(http_peer, tmp_path):
    peer, endpoint = http_peer
    peer.offer = {"preshared_key": True}
    peer.disconnect_response = {"error": "disconnect refused"}
    transport = HiveMindHTTPTransport(identity(endpoint), useragent="cleanup", noise_state_dir=str(tmp_path / "client"),
                                      handshake_poll_interval=0.01)
    client = ThalovantClient(identity(endpoint), transport=transport)
    try:
        with pytest.raises(ThalovantConnectionError, match="Could not establish"):
            client.connect()
        with pytest.raises(ThalovantConnectionError, match="disconnect"):
            client.wait_closed(timeout=2)
        assert peer.admitted and peer.disconnects == 1
        with pytest.raises(ThalovantConnectionError, match="cleanup"):
            client.connect()
        assert peer.connects == 1 and peer.disconnects == 1
    finally:
        peer.disconnect_response = {"status": "Disconnected"}
        client.close()
        client.wait_closed(timeout=2)
    assert not peer.admitted and peer.disconnects == 2


def test_async_close_and_wait_closed_preserve_failure_until_explicit_retry(http_peer, tmp_path):
    peer, endpoint = http_peer
    transport = HiveMindHTTPTransport(identity(endpoint), useragent="cleanup", noise_state_dir=str(tmp_path / "client"),
                                      handshake_poll_interval=0.01)
    client = AsyncThalovantClient(identity(endpoint), transport=transport)
    async def exercise():
        await client.connect()
        peer.disconnect_response = {}
        try:
            with pytest.raises(ThalovantConnectionError, match="disconnect"):
                await client.close()
            with pytest.raises(ThalovantConnectionError, match="disconnect"):
                await client.wait_closed(timeout=2)
            with pytest.raises(ThalovantConnectionError, match="cleanup"):
                await client.connect()
            assert peer.admitted and peer.connects == 1
        finally:
            peer.disconnect_response = {"status": "Disconnected"}
            await client.close()
            await client.wait_closed(timeout=2)
        assert not peer.admitted
    asyncio.run(exercise())


def test_http_server_refusal_body_is_not_copied_into_cleanup_diagnostics(http_peer, tmp_path):
    peer, endpoint = http_peer
    transport = HiveMindHTTPTransport(identity(endpoint), useragent="cleanup", noise_state_dir=str(tmp_path / "client"),
                                      handshake_poll_interval=0.01)
    transport.connect()
    peer.disconnect_response = {"error": "arbitrary-refusal-body-must-not-be-logged"}
    try:
        with pytest.raises(ThalovantConnectionError) as caught:
            transport.disconnect()
        assert "arbitrary-refusal-body" not in "".join(traceback.format_exception(caught.value))
        assert "arbitrary-refusal-body" not in transport.healthcheck().last_error
    finally:
        peer.disconnect_response = {"status": "Disconnected"}
        transport.disconnect()


def test_public_close_cannot_report_success_while_direct_transport_cleanup_is_pending(http_peer, tmp_path):
    peer, endpoint = http_peer
    transport = HiveMindHTTPTransport(identity(endpoint), useragent="cleanup", noise_state_dir=str(tmp_path / "client"),
                                      handshake_poll_interval=0.01)
    client = ThalovantClient(identity(endpoint), transport=transport)
    client.connect()
    peer.disconnect_entered, peer.disconnect_gate = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        closing = pool.submit(transport.disconnect)
        try:
            assert peer.disconnect_entered.wait(2)
            with pytest.raises(ThalovantConnectionError, match="cleanup.*in progress"):
                client.close(timeout=1)
            with pytest.raises(ThalovantConnectionError, match="cleanup.*in progress"):
                client.wait_closed(timeout=1)
            assert peer.admitted and peer.disconnects == 1
            peer.disconnect_gate.set()
            closing.result(timeout=2)
        finally:
            peer.disconnect_gate.set()
    client.close()
    client.wait_closed(timeout=1)
    assert not peer.admitted and peer.disconnects == 1
