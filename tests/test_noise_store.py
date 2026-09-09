"""Persistent identity stays private, immutable on corruption, and atomic."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from thalovant._noise_runtime import noise_identity, prepare_noise_key
from thalovant import ThalovantConnectionError


def test_noise_identity_uses_one_key_and_merges_independent_peer_pins(tmp_path):
    def create(_):
        identity = noise_identity(str(tmp_path))
        return Path(prepare_noise_key(identity)).read_text()
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert len(set(pool.map(create, range(24)))) == 1
    first, second = noise_identity(str(tmp_path)), noise_identity(str(tmp_path))
    first.pin_noise_key("hub-one", "11" * 32)
    second.pin_noise_key("hub-two", "22" * 32)
    assert first.get_pinned_noise_key("hub-two") == "22" * 32
    assert second.get_pinned_noise_key("hub-one") == "11" * 32
    with pytest.raises(ThalovantConnectionError, match="key changed"):
        second.pin_noise_key("hub-one", "33" * 32)
    assert first.get_pinned_noise_key("hub-one") == "11" * 32
    first.save()
    assert second.get_pinned_noise_key("hub-two") == "22" * 32
    assert second.forget_noise_key("hub-one") is True
    assert first.get_pinned_noise_key("hub-one") is None
    if os.name == "posix":
        assert tmp_path.stat().st_mode & 0o777 == 0o700
        for file in tmp_path.iterdir(): assert file.stat().st_mode & 0o777 == 0o600


def test_noise_key_corruption_and_symlink_never_mint_replacement(tmp_path):
    identity = noise_identity(str(tmp_path))
    key = Path(prepare_noise_key(identity))
    key.write_text("broken")
    with pytest.raises(ThalovantConnectionError, match="invalid"):
        prepare_noise_key(identity)
    assert key.read_text() == "broken"
    key.unlink()
    target = tmp_path / "unrelated"
    target.write_text("11" * 32)
    key.symlink_to(target)
    with pytest.raises(ThalovantConnectionError, match="symlink"):
        prepare_noise_key(identity)
    assert target.read_text() == "11" * 32


def test_corrupt_pin_store_is_not_overwritten(tmp_path):
    identity = noise_identity(str(tmp_path))
    store = Path(identity.IDENTITY_FILE.path)
    store.write_text("{")
    with pytest.raises(ThalovantConnectionError):
        identity.pin_noise_key("hub", "11" * 32)
    assert store.read_text() == "{"


@pytest.mark.parametrize("pins", [None, [], "invalid", 42, {"hub": "g" * 64},
                                  {"hub": "11" * 31}, {"hub": None}])
@pytest.mark.parametrize("operation", ["read", "pin", "forget", "save"])
def test_malformed_pin_state_raises_domain_error_and_preserves_file(tmp_path, pins, operation):
    identity = noise_identity(str(tmp_path))
    identity.pin_noise_key("hub", "11" * 32)
    store = Path(identity.IDENTITY_FILE.path)
    data = json.loads(store.read_text())
    data["pinned_noise_keys"] = pins
    original = json.dumps(data)
    store.write_text(original)
    with pytest.raises(ThalovantConnectionError, match="Stored Noise server pin"):
        if operation == "read":
            identity.get_pinned_noise_key("hub")
        elif operation == "pin":
            identity.pin_noise_key("hub", "11" * 32)
        elif operation == "forget":
            identity.forget_noise_key("hub")
        else:
            identity.save()
    assert store.read_text() == original


def test_processes_observe_only_the_complete_winning_static_key(tmp_path):
    import subprocess
    import sys
    program = """
import hashlib,sys
from pathlib import Path
from thalovant._noise_runtime import noise_identity,prepare_noise_key
key=Path(prepare_noise_key(noise_identity(sys.argv[1]))).read_bytes()
assert len(key)==64
print(hashlib.sha256(key).hexdigest())
"""
    def child(_):
        result = subprocess.run([sys.executable, "-c", program, str(tmp_path)], capture_output=True, text=True, timeout=15, check=True)
        return result.stdout.splitlines()[-1]
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert len(set(pool.map(child, range(16)))) == 1
