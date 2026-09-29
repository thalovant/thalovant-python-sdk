"""Keeping a hub link up, against ``link-keeping-vectors.json``.

``close`` cases hold the transport's reading of a close to the vectors, and a
few run for real against the in-process hub. ``handshake`` cases run a real
Noise handshake that fails the way the case says. ``supervise`` cases drive
the supervisor ``AsyncHubSession.run()`` asks after every attempt.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from conformance_record import record
from fake_hub import FakeHub, speak_back
from thalovant import (
    AsyncHubSession,
    AsyncThalovantClient,
    HubSessionPolicy,
    ThalovantClientKeyRejectedError,
    ThalovantConnectionError,
    ThalovantHubKeyChangedError,
    ThalovantHubRefusedError,
)
from thalovant import _hive, _noise
from thalovant.session import LinkSupervisor

VECTORS = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts" / "conformance" / "link-keeping-vectors.json").read_text(encoding="utf-8")
)
POLICY = VECTORS["policy"]


def _cases(kind: str) -> list[dict[str, Any]]:
    return [case for case in VECTORS["cases"] if case["kind"] == kind]


def test_the_policy_is_the_sdks() -> None:
    defaults = HubSessionPolicy()
    assert defaults.retry_seconds * 1000 == POLICY["retry_ms"]
    assert defaults.retry_ceiling_seconds * 1000 == POLICY["retry_ceiling_ms"]
    assert defaults.probe_seconds * 1000 == POLICY["probe_ms"]
    assert defaults.probe_down_seconds * 1000 == POLICY["probe_down_ms"]
    assert defaults.refusal_grace_seconds * 1000 == POLICY["refusal_grace_ms"]
    assert _hive.REFUSAL_SETTLE_MS == POLICY["settle_ms"]
    assert _hive.CLOSE_CODE_GRACE_MS == POLICY["close_code_grace_ms"]
    assert sorted(_hive.REFUSAL_CLOSE_CODES) == POLICY["refusal_close_codes"]
    import inspect

    settle = inspect.signature(AsyncHubSession).parameters["settle_seconds"].default
    assert settle * 1000 == POLICY["settle_ms"]


@pytest.mark.parametrize("case", _cases("close"), ids=lambda case: case["name"])
def test_close_vectors(case: dict[str, Any]) -> None:
    refused = _hive.close_refuses(
        case["code"],
        closed_after_handshake_ms=case.get("after_ms") if case["when"] == "after_handshake" else None,
        code_late_ms=case.get("code_late_ms", 0),
        after_authenticated_frame=case.get("after_authenticated_frame", False),
    )
    produced = {"outcome": "refused" if refused else "dropped"}
    record("link-keeping-vectors.json", case["name"], produced)
    assert produced == case["expect"]


def _identity(hub: FakeHub, record_: Any, password: str | None = None) -> Any:
    from dataclasses import replace

    identity = hub.identity(record_)
    return replace(identity, password=password) if password else identity


async def _connect(identity: Any, state: str) -> None:
    client = AsyncThalovantClient(identity, noise_state_dir=state, reply_settle_seconds=0.05, auto_reconnect=False)
    try:
        await client.connect(timeout=10)
    finally:
        await client.close()


def _outcome(error: BaseException | None) -> str:
    if error is None:
        return "connected"
    if isinstance(error, ThalovantClientKeyRejectedError):
        return "client_key_rejected"
    if isinstance(error, ThalovantHubRefusedError):
        return "refused"
    if isinstance(error, ThalovantHubKeyChangedError):
        return "key_changed"
    assert isinstance(error, ThalovantConnectionError), error
    return "failed"


async def _attempt(identity: Any, state: str) -> BaseException | None:
    """One connect as a kept link makes it: the handshake, then the settle window."""
    session = AsyncHubSession.for_identity(identity, noise_state_dir=state, settle_seconds=POLICY["settle_ms"] / 1000)
    try:
        await asyncio.wait_for(session.connect(), 15)
    except ThalovantConnectionError as error:
        return error
    finally:
        await session.close()
    return None


def _replace_client_key(state: str) -> None:
    """Give the client a new static key, keeping the hub pins it has."""
    (key,) = Path(state).glob("*_noise.key")
    key.write_text(_noise.generate_private_key().hex(), encoding="ascii")


@pytest.mark.parametrize("case", _cases("handshake"), ids=lambda case: case["name"])
def test_handshake_vectors(case: dict[str, Any], tmp_path: Path) -> None:
    async def exercise() -> dict[str, Any]:
        hub = FakeHub()
        hub.responder = speak_back
        await hub.start()
        try:
            record_ = hub.register(password="the-right-password")
            state = str(tmp_path / "noise")
            identity = _identity(hub, record_)
            situation = case["situation"]
            if situation in ("pinned", "password_changed_since_pinning", "hub_key_changed",
                             "client_key_changed", "client_key_changed_pinned_here"):
                await _connect(identity, state)  # first contact pins both ways
            if situation == "wrong_password":
                identity = _identity(hub, record_, "a-wrong-password")
            elif situation == "password_changed_since_pinning":
                record_.password = "the-password-now"  # the hub's side changed
            elif situation == "hub_key_changed":
                hub.static_key = _noise.generate_private_key()  # the hub was replaced
                hub.offer_kk = case["hub_offers_kk"]
            elif situation == "upgrade_status":
                hub.upgrade_status = case["status"]
            elif situation == "client_key_changed":
                state = str(tmp_path / "another-program")  # its own folder, its own key
            elif situation == "client_key_changed_pinned_here":
                _replace_client_key(state)
            elif situation == "closed_after_first_frame":
                hub.close_after_handshake = True
                hub.close_after_handshake_speaks = True
            before = len(hub.patterns_chosen)
            outcome = _outcome(await _attempt(identity, state))
            patterns = [pattern[:2] for pattern in hub.patterns_chosen[before:]]
            return {"outcome": outcome, "patterns": patterns}
        finally:
            await hub.stop()

    produced = asyncio.run(exercise())
    record("link-keeping-vectors.json", case["name"], produced)
    assert produced == case["expect"]


@pytest.mark.parametrize("case", _cases("supervise"), ids=lambda case: case["name"])
def test_supervise_vectors(case: dict[str, Any]) -> None:
    supervisor = LinkSupervisor(
        HubSessionPolicy(
            retry_seconds=POLICY["retry_ms"] / 1000,
            retry_ceiling_seconds=POLICY["retry_ceiling_ms"] / 1000,
            probe_seconds=POLICY["probe_ms"] / 1000,
            probe_down_seconds=POLICY["probe_down_ms"] / 1000,
            refusal_grace_seconds=POLICY["refusal_grace_ms"] / 1000,
        )
    )
    produced = []
    for event in case["events"]:
        decision = supervisor.after(event["outcome"], event["at_ms"] / 1000)
        if decision.action == "retry":
            produced.append({"action": "retry", "wait_ms": round(decision.wait_seconds * 1000)})
        elif decision.action == "give_up":
            produced.append({"action": "give_up", "reason": decision.reason})
        else:
            produced.append({"action": decision.action})
    record("link-keeping-vectors.json", case["name"], produced)
    assert produced == case["expect"]


@pytest.mark.parametrize(("code", "refused"), [(None, True), (1000, True), (1008, True), (1011, False), (1001, False)])
def test_a_real_close_right_after_the_handshake(code: int | None, refused: bool, tmp_path: Path) -> None:
    async def exercise() -> None:
        hub = FakeHub()
        await hub.start()
        record_ = hub.register()
        hub.close_after_handshake = True
        hub.close_after_handshake_code = code
        session = AsyncHubSession.for_identity(
            hub.identity(record_), noise_state_dir=str(tmp_path / "noise"), settle_seconds=0.5
        )
        try:
            with pytest.raises(ThalovantConnectionError) as caught:
                await session.connect()
            assert isinstance(caught.value, ThalovantHubRefusedError) is refused
        finally:
            await session.close()
            await hub.stop()

    asyncio.run(exercise())


def test_run_stops_at_once_when_the_hub_key_changed(tmp_path: Path) -> None:
    async def exercise() -> None:
        hub = FakeHub()
        await hub.start()
        record_ = hub.register()
        state = str(tmp_path / "noise")
        await _connect(hub.identity(record_), state)
        hub.static_key = _noise.generate_private_key()
        session = AsyncHubSession.for_identity(
            hub.identity(record_), noise_state_dir=state, settle_seconds=0.05,
            policy=HubSessionPolicy(retry_seconds=0.05, retry_ceiling_seconds=0.1, probe_seconds=0.05,
                                    probe_down_seconds=0.05, refusal_grace_seconds=30),
        )
        try:
            # KK against the old key fails, XX follows at once and meets the
            # pin: run() ends there rather than retrying for ever.
            with pytest.raises(ThalovantHubKeyChangedError):
                await asyncio.wait_for(session.run(), 10)
            assert hub.patterns_chosen[-2:] == ["KKpsk0", "XXpsk2"]
            assert hub.attempts == 3  # the pinning connect, then KK and XX
        finally:
            await session.close()
            await hub.stop()

    asyncio.run(exercise())


@pytest.mark.parametrize(("code", "refused"), [(None, True), (1008, True), (1011, False)])
def test_a_close_before_connect_returns_is_read_like_one_after(
    code: int | None, refused: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window between the end of the handshake and connect() returning.

    connect() is held, deterministically, until the reader has seen the hub's
    close. It used to wait out its whole deadline for a link that was gone and
    report a timeout -- never a refusal.
    """
    import time

    import thalovant.client as client_module

    original = client_module.AsyncThalovantClient._reapply_subscriptions

    async def after_the_close(self: Any) -> None:
        stopped = self._link.stopped()
        await asyncio.wait_for(stopped.wait(), 5)
        await original(self)

    monkeypatch.setattr(client_module.AsyncThalovantClient, "_reapply_subscriptions", after_the_close)

    async def exercise() -> tuple[BaseException, float]:
        hub = FakeHub()
        await hub.start()
        record_ = hub.register()
        hub.close_after_handshake = True
        hub.close_after_handshake_code = code
        client = AsyncThalovantClient(hub.identity(record_), noise_state_dir=str(tmp_path / "noise"), auto_reconnect=False)
        started = time.monotonic()
        try:
            await client.connect(timeout=5)
        except ThalovantConnectionError as error:
            return error, time.monotonic() - started
        finally:
            await client.close()
            await hub.stop()
        raise AssertionError("connected to a hub that had closed")

    error, took = asyncio.run(exercise())
    assert isinstance(error, ThalovantHubRefusedError) is refused
    assert took < 2
