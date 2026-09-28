"""Where a client's Noise key lives, and the hub refusing a key it did not pin.

An identity read from a file keeps its key in a ``hivemind`` folder beside
that file -- ``~/.config/thalovant/identity.json`` beside
``~/.config/thalovant/hivemind``, where thalovant-voice keeps it -- so every
program that reads the file presents the same key. The first time, the key
this identity had in the old shared folder is copied over, never moved, so
no device is locked out by the move. Found live: the CLI and the satellite
kept two keys for one identity, and the hub let in only the first.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from fake_hub import FakeHub
from thalovant import (
    AsyncHubSession,
    AsyncThalovantClient,
    ThalovantClientKeyRejectedError,
    ThalovantHubRefusedError,
    ThalovantIdentity,
)
from thalovant import _noise_runtime


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    config = xdg / "thalovant"
    config.mkdir(parents=True)
    return {"legacy": xdg / "hivemind", "config": config, "beside": config / "hivemind"}


def _identity_file(directory: Path, host: str = "hub.example.com") -> Path:
    path = directory / "identity.json"
    path.write_text(json.dumps({
        "access_key": "k", "password": "p", "site_id": "s", "default_master": f"https://{host}", "default_port": 443,
    }))
    path.chmod(0o600)
    return path


def _legacy_key(legacy: Path, pins: dict[str, str]) -> str:
    """What 0.9.0 left in ~/.config/hivemind for an identity that connected."""
    legacy.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32).hex()
    (legacy / "kitchen_noise.key").write_text(key)
    (legacy / "_identity.json").write_text(
        json.dumps({"name": "kitchen", "pinned_noise_keys": pins, "key": "secret-access"})
    )
    return key


def test_an_identity_file_remembers_where_it_came_from(homes: dict[str, Path]) -> None:
    path = _identity_file(homes["config"])
    identity = ThalovantIdentity.from_file(path)
    assert identity.source_path == str(path)
    assert "source_path" not in identity.as_dict(include_secrets=True)
    assert identity == replace(identity, source_path=None)  # not identity material
    assert ThalovantIdentity.from_mapping({"access_key": "k", "password": "p", "site_id": "s",
                                           "default_master": "https://h"}).source_path is None


def test_a_config_file_identity_remembers_it_too(homes: dict[str, Path]) -> None:
    config = homes["config"] / "config.yaml"
    config.write_text("access_key: k\npassword: p\nsite_id: s\ndefault_master: https://hub.example.com\n")
    config.chmod(0o600)
    assert ThalovantIdentity.from_config(config).source_path == str(config)


def test_the_default_folder_is_beside_the_identity_file(homes: dict[str, Path]) -> None:
    identity = ThalovantIdentity.from_file(_identity_file(homes["config"]))
    store = _noise_runtime.noise_identity(identity=identity)
    assert Path(store.IDENTITY_FILE.path).parent == homes["beside"]
    assert Path(_noise_runtime.prepare_noise_key(store)).parent == homes["beside"]
    # An identity that came from no file keeps the shared default.
    loose = replace(identity, source_path=None)
    assert Path(_noise_runtime.noise_identity(identity=loose).IDENTITY_FILE.path).parent == homes["legacy"]
    # And an explicit folder always wins.
    chosen = homes["config"].parent / "chosen"
    assert Path(_noise_runtime.noise_identity(str(chosen), identity=identity).IDENTITY_FILE.path).parent == chosen


def test_the_key_this_identity_used_is_copied_never_moved(homes: dict[str, Path]) -> None:
    identity = ThalovantIdentity.from_file(_identity_file(homes["config"]))
    pins = {"wss://hub.example.com:443": "ab" * 32}
    key = _legacy_key(homes["legacy"], pins)
    store = _noise_runtime.noise_identity(identity=identity)
    copied = Path(_noise_runtime.prepare_noise_key(store))
    assert copied == homes["beside"] / "kitchen_noise.key" and copied.read_text() == key
    assert store.pinned_noise_keys == pins
    # The old folder is untouched: another program may still read it.
    assert (homes["legacy"] / "kitchen_noise.key").read_text() == key
    assert json.loads((homes["legacy"] / "_identity.json").read_text())["key"] == "secret-access"
    # Only the key and the pins came across.
    assert set(json.loads((homes["beside"] / "_identity.json").read_text())) == {"name", "pinned_noise_keys"}
    if os.name == "posix":
        assert copied.stat().st_mode & 0o777 == 0o600
    # Once: a later change in the old folder is not copied again.
    (homes["legacy"] / "kitchen_noise.key").write_text(os.urandom(32).hex())
    again = _noise_runtime.noise_identity(identity=identity)
    assert Path(_noise_runtime.prepare_noise_key(again)).read_text() == key


def test_a_key_that_never_met_this_hub_is_not_copied(homes: dict[str, Path]) -> None:
    identity = ThalovantIdentity.from_file(_identity_file(homes["config"]))
    key = _legacy_key(homes["legacy"], {"wss://another-hub.example.com:443": "cd" * 32})
    store = _noise_runtime.noise_identity(identity=identity)
    fresh = Path(_noise_runtime.prepare_noise_key(store)).read_text()
    assert fresh != key  # a new key: the old one was never pinned for this identity's hub


def test_a_folder_that_already_has_a_key_keeps_it(homes: dict[str, Path]) -> None:
    identity = ThalovantIdentity.from_file(_identity_file(homes["config"]))
    homes["beside"].mkdir()
    own = os.urandom(32).hex()
    (homes["beside"] / "unnamed-node_noise.key").write_text(own)
    _legacy_key(homes["legacy"], {"https://hub.example.com": "ab" * 32})
    store = _noise_runtime.noise_identity(identity=identity)
    assert Path(_noise_runtime.prepare_noise_key(store)).read_text() == own


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a directory this user cannot write")
def test_an_identity_where_its_reader_cannot_write_keeps_the_shared_folder(homes: dict[str, Path]) -> None:
    shared = homes["config"].parent / "etc-thalovant"
    shared.mkdir()
    path = _identity_file(shared)
    shared.chmod(0o500)
    try:
        identity = ThalovantIdentity.from_file(path)
        store = _noise_runtime.noise_identity(identity=identity)
        assert Path(store.IDENTITY_FILE.path).parent == homes["legacy"]
    finally:
        shared.chmod(0o700)


def test_the_hub_refusing_a_key_it_did_not_pin_names_both_folders(tmp_path: Path, homes: dict[str, Path]) -> None:
    """Two programs, one identity, two folders: the second is refused, and told why."""

    async def exercise() -> BaseException:
        hub = FakeHub()
        await hub.start()
        try:
            record = hub.register()
            identity = replace(hub.identity(record), source_path=str(homes["config"] / "identity.json"))
            satellite = AsyncThalovantClient(identity, noise_state_dir=str(tmp_path / "satellite"), auto_reconnect=False)
            await satellite.connect(timeout=10)  # the hub pins the satellite's key
            await satellite.close()
            session = AsyncHubSession.for_identity(identity)  # the CLI: the default folder, another key
            try:
                with pytest.raises(ThalovantHubRefusedError) as caught:
                    await session.connect()
            finally:
                await session.close()
            return caught.value
        finally:
            await hub.stop()

    error = asyncio.run(exercise())
    assert isinstance(error, ThalovantClientKeyRejectedError)
    assert error.key_folder == str(homes["beside"])
    assert error.other_key_folder == str(homes["legacy"])
    message = str(error)
    assert str(homes["beside"]) in message and str(homes["legacy"]) in message
    assert "Re-pair, or share the key folder" in message


def test_run_stops_at_once_on_a_refused_client_key(tmp_path: Path) -> None:
    from thalovant import HubSessionPolicy

    async def exercise() -> None:
        hub = FakeHub()
        await hub.start()
        try:
            record = hub.register()
            identity = hub.identity(record)
            first = AsyncThalovantClient(identity, noise_state_dir=str(tmp_path / "first"), auto_reconnect=False)
            await first.connect(timeout=10)
            await first.close()
            session = AsyncHubSession.for_identity(
                identity, noise_state_dir=str(tmp_path / "second"), settle_seconds=0.5,
                policy=HubSessionPolicy(retry_seconds=0.05, retry_ceiling_seconds=0.1, probe_seconds=0.05,
                                        probe_down_seconds=0.05, refusal_grace_seconds=30),
            )
            try:
                with pytest.raises(ThalovantClientKeyRejectedError):
                    await asyncio.wait_for(session.run(), 10)
                assert hub.attempts == 2  # pinned once, refused once, and not dialled again
            finally:
                await session.close()
        finally:
            await hub.stop()

    asyncio.run(exercise())


def test_a_close_after_a_kk_handshake_is_an_ordinary_refusal(tmp_path: Path) -> None:
    """Only XX shows the hub a key it could refuse; after KK the close says nothing about the key."""
    from thalovant import _hive

    class Transport:
        closed_key_rejected = False

    error = _hive.refusal_after_handshake(Transport())
    assert type(error) is ThalovantHubRefusedError
