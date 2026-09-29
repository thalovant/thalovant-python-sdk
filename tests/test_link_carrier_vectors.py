"""KK then XX on HTTPS polling and MQTT, against ``link-carrier-vectors.json``.

Each case runs one connect, as a kept link makes it (the handshake, then the
settle window), through a real carrier: TLS HTTPS polling against
``carrier_hub.HttpsHub``, and the MQTT transport against an in-memory broker.
The hub side is ``carrier_hub.CarrierPeer``, the SDK's own Noise responder.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from carrier_hub import HttpsHub, MqttBroker
from conformance_record import record
from thalovant import (
    AsyncHubSession,
    ThalovantClientKeyRejectedError,
    ThalovantConnectionError,
    ThalovantHubKeyChangedError,
    ThalovantHubRefusedError,
    ThalovantTimeoutError,
    _noise,
)
from thalovant.transport import HiveMindMQTTTransport

VECTORS = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts" / "conformance" / "link-carrier-vectors.json").read_text(
        encoding="utf-8"
    )
)


def _outcome(error: BaseException | None) -> str:
    if error is None:
        return "connected"
    if isinstance(error, ThalovantClientKeyRejectedError):
        return "client_key_rejected"
    if isinstance(error, ThalovantHubRefusedError):
        return "refused"
    if isinstance(error, ThalovantHubKeyChangedError):
        return "key_changed"
    assert isinstance(error, (ThalovantConnectionError, ThalovantTimeoutError)), error
    return "failed"


async def _attempt(identity: Any, state: str, protocol: str) -> BaseException | None:
    session = AsyncHubSession.for_identity(
        identity, protocol=protocol, noise_state_dir=state, settle_seconds=0.75,
        connect_timeout=3, handshake_timeout=3, auto_reconnect=False,
    )
    try:
        await asyncio.wait_for(session.connect(), 20)
    except (ThalovantConnectionError, ThalovantTimeoutError) as error:
        return error
    finally:
        await session.close()
    return None


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
def test_carrier_vectors(case: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    carrier = case["carrier"]
    hub: Any
    if carrier == "https":
        hub = HttpsHub(tmp_path)
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(hub.cert_path))
        protocol = "https"
    else:
        hub = MqttBroker()
        monkeypatch.setattr(HiveMindMQTTTransport, "_load_mqtt_module", staticmethod(lambda: hub.module))
        protocol = "mqtt"
    peer = hub.peer
    identity = hub.identity()
    state = str(tmp_path / "noise")

    async def exercise() -> dict[str, Any]:
        nonlocal identity, state
        situation = case["situation"]
        if situation in ("pinned", "password_changed_since_pinning", "hub_key_changed",
                         "client_key_changed", "kk_answer_unauthenticated"):
            assert await _attempt(identity, state, protocol) is None  # first contact pins both ways
        if situation == "wrong_password":
            peer.password = "the-password-the-hub-holds"
        elif situation == "password_changed_since_pinning":
            peer.password = "the-password-now"
        elif situation == "hub_key_changed":
            peer.static_key = _noise.generate_private_key()
            peer.offer_kk = case["hub_offers_kk"]
        elif situation == "client_key_changed":
            state = str(tmp_path / "another-program")
        elif situation == "kk_answer_unauthenticated":
            peer.tamper_kk_answer = True
        before = len(peer.patterns)
        outcome = _outcome(await _attempt(identity, state, protocol))
        return {"outcome": outcome, "patterns": [pattern[:2] for pattern in peer.patterns[before:]]}

    try:
        produced = asyncio.run(exercise())
    finally:
        if carrier == "https":
            hub.close()
    record("link-carrier-vectors.json", case["name"], produced)
    assert produced == case["expect"]
