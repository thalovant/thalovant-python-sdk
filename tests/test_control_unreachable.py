"""A control plane out of reach is a connection error, and still an API error.

0.8 raised a bare ``ThalovantAPIError`` with no status for DNS, TCP, TLS and
timeouts alike, so a caller could not tell "try again later" from "the API
said no". ``ThalovantAPIUnreachableError`` is both, so an existing
``except ThalovantAPIError`` still catches it.
"""

from __future__ import annotations

import asyncio
import socket

import pytest
import requests

from thalovant import (
    AsyncThalovantControlPlane,
    ThalovantAdmissionFailedError,
    ThalovantAPIError,
    ThalovantAPIUnreachableError,
    ThalovantConnectionError,
    ThalovantControlPlane,
)


def _closed_port() -> int:
    """A loopback port nothing listens on: the connection is refused."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _is_unreachable(error: BaseException) -> None:
    assert isinstance(error, ThalovantAPIUnreachableError)
    assert isinstance(error, ThalovantAPIError) and isinstance(error, ThalovantConnectionError)
    assert error.status_code is None and error.code is None and error.problem is None
    assert str(error) == "Could not reach the Thalovant API."


def test_a_refused_connection_is_unreachable_on_the_async_control_plane():
    async def exercise():
        async with AsyncThalovantControlPlane(f"http://127.0.0.1:{_closed_port()}", access_token="synthetic") as api:
            with pytest.raises(ThalovantAPIError) as caught:
                await api.list_hubs()
        _is_unreachable(caught.value)

    asyncio.run(exercise())


def test_a_refused_connection_is_unreachable_on_the_sync_control_plane():
    with ThalovantControlPlane(f"http://127.0.0.1:{_closed_port()}", access_token="synthetic") as api:
        with pytest.raises(ThalovantConnectionError) as caught:
            api.list_hubs()
    _is_unreachable(caught.value)


def test_a_request_that_times_out_is_unreachable():
    async def exercise():
        held: list[asyncio.StreamWriter] = []

        async def silent(_reader, writer):
            held.append(writer)  # accepts the request and never answers it

        server = await asyncio.start_server(silent, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            api = AsyncThalovantControlPlane(f"http://127.0.0.1:{port}", access_token="synthetic", timeout=0.2)
            try:
                with pytest.raises(ThalovantAPIError) as caught:
                    await api.list_hubs()
            finally:
                await api.aclose()
        finally:
            for writer in held:
                writer.close()
            server.close()
            await server.wait_closed()
        _is_unreachable(caught.value)

    asyncio.run(exercise())


def test_a_requests_session_that_cannot_connect_is_unreachable(monkeypatch):
    def request(*_args, **_kwargs):
        raise requests.ConnectionError("synthetic: connection refused")

    monkeypatch.setattr(requests.Session, "request", request)
    api = ThalovantControlPlane(access_token="synthetic", session=requests.Session())
    with pytest.raises(ThalovantAPIError) as caught:
        api.list_hubs()
    _is_unreachable(caught.value)


@pytest.mark.parametrize("error", [requests.exceptions.InvalidURL, requests.exceptions.MissingSchema])
def test_a_request_that_could_not_be_formed_is_not_unreachable(monkeypatch, error):
    def request(*_args, **_kwargs):
        raise error("synthetic: not a URL")

    monkeypatch.setattr(requests.Session, "request", request)
    api = ThalovantControlPlane(access_token="synthetic", session=requests.Session())
    with pytest.raises(ThalovantAPIError) as caught:
        api.list_hubs()
    assert not isinstance(caught.value, ThalovantAPIUnreachableError)
    assert not isinstance(caught.value, ThalovantConnectionError)


def test_an_admission_wait_that_loses_the_api_is_not_a_failed_admission():
    async def exercise():
        async with AsyncThalovantControlPlane(f"http://127.0.0.1:{_closed_port()}", access_token="synthetic") as api:
            with pytest.raises(ThalovantConnectionError) as caught:
                await api.wait_for_admission({"id": "op-1"}, timeout=5, poll_interval=0.01)
        _is_unreachable(caught.value)
        assert not isinstance(caught.value, ThalovantAdmissionFailedError)

    asyncio.run(exercise())
