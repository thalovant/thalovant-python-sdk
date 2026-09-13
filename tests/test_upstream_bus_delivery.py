"""Exercise the installed HiveMind dispatcher and protocol, without network I/O."""
from types import SimpleNamespace

import pytest
from hivemind_bus_client.client import HiveMessageBusClient
from hivemind_bus_client.message import HiveMessage, HiveMessageType
from hivemind_bus_client.protocol import HiveMindSlaveProtocol, VERIFIED_SOURCE_PEER_KEY
from ovos_bus_client.message import Message
from ovos_bus_client.session import SessionManager

from thalovant._noise_runtime import noise_identity
from thalovant.transport import HiveMindWSSTransport
from test_query_semantics import QueryTransport, client


@pytest.mark.parametrize("binding", ["connect", "manual", "unbound"])
def test_bus_delivery_once_after_protocol_processing(tmp_path, monkeypatch, binding):
    transport = QueryTransport()
    sdk = client(transport)
    adapter = HiveMindWSSTransport(sdk.identity, useragent="conformance")
    upstream_type = adapter._build_wss_client_class(HiveMessageBusClient, None)
    monkeypatch.setattr(upstream_type, "create_client", lambda self: None)
    upstream = upstream_type(
        key="synthetic", password="synthetic", host="127.0.0.1", port=1,
        useragent="conformance", identity=noise_identity(tmp_path),
    )
    updates = []
    monkeypatch.setattr(SessionManager, "update", lambda session: updates.append(session))
    if binding != "unbound":
        protocol = adapter._build_protocol(
            upstream, SimpleNamespace(HiveMindSlaveProtocol=HiveMindSlaveProtocol),
        )
        if binding == "connect":
            # connect() assigns the protocol before binding; no socket needed.
            upstream.protocol = protocol
        protocol.bind(upstream.internal_bus)
    transport.on_mycroft = upstream.on_mycroft
    transport.remove_mycroft = upstream.remove
    delivered = []
    try:
        sdk.on("conformance.event", lambda event: delivered.append(
            (dict(event.context), len(updates))))
        for _ in range(2):
            updates.clear()
            upstream._handle_hive_protocol(HiveMessage(
                HiveMessageType.BUS,
                Message("conformance.event", {"verb": "one-frame"}, {
                    "destination": "receiver", VERIFIED_SOURCE_PEER_KEY: "forged",
                    "session": {"session_id": upstream.session_id},
                }),
            ))
        # Equal payloads in distinct frames remain distinct deliveries.
        assert len(delivered) == 2
        if binding == "unbound":
            # Upstream's direct fallback remains usable when no protocol binds.
            assert [count for _, count in delivered] == [0, 0]
        else:
            assert [count for _, count in delivered] == [1, 1]
            for context, _ in delivered:
                assert context["source"] == "receiver"
                assert "destination" not in context
                assert VERIFIED_SOURCE_PEER_KEY not in context
            assert updates[0].site_id == sdk.identity.site_id
    finally:
        sdk.close()
