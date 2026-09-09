"""Transport-independent HiveMind v3 negotiation using the published primitives.

No patched HTTP client wheel is needed. A channel belongs to one connection;
its static identity and trusted server keys outlive that connection.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import tempfile
from typing import Any, Callable

from .errors import ThalovantConnectionError

_store_lock = threading.RLock()


def noise_identity(state_dir: str | None = None) -> Any:
    from hivemind_bus_client.identity import NodeIdentity
    from json_database import JsonStorage

    class PrivateNoiseIdentity(NodeIdentity):
        def _read_current(self) -> dict[str, Any]:
            path = Path(self.IDENTITY_FILE.path)
            if path.is_symlink():
                raise ThalovantConnectionError("Noise identity must not be a symlink.")
            if not path.exists():
                return {}
            data = json.loads(path.read_text(encoding="utf8"))
            if not isinstance(data, dict):
                raise ThalovantConnectionError("Stored Noise identity is invalid.")
            return data

        def _write_private(self, data: dict[str, Any]) -> None:
            path = Path(self.IDENTITY_FILE.path)
            if path.is_symlink():
                raise ThalovantConnectionError("Noise identity must not be a symlink.")
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".noise-identity-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf8") as output:
                    json.dump(data, output, ensure_ascii=False)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

        def save(self) -> None:
            with _store_lock, self.IDENTITY_FILE.lock:
                current = self._read_current()
                # Other processes may have learned additional peers meanwhile.
                pins = current.get("pinned_noise_keys", self.pinned_noise_keys)
                current.update(self.IDENTITY_FILE)
                current["pinned_noise_keys"] = pins
                self._write_private(current)

        def get_pinned_noise_key(self, node_id: str) -> str | None:
            with _store_lock, self.IDENTITY_FILE.lock:
                data = self._read_current()
                pins = data.get("pinned_noise_keys", self.pinned_noise_keys)
                pin = pins.get(node_id)
                if pin is not None and (not isinstance(pin, str) or len(pin) != 64 or len(bytes.fromhex(pin)) != 32):
                    raise ThalovantConnectionError("Stored Noise server pin is invalid.")
                return pin

        def pin_noise_key(self, node_id: str, pubkey: str) -> None:
            with _store_lock, self.IDENTITY_FILE.lock:
                current = self._read_current()
                pins = current.get("pinned_noise_keys", {})
                if pins.get(node_id) not in (None, pubkey):
                    raise ThalovantConnectionError("Trusted Noise server key changed; refusing connection.")
                pins[node_id] = pubkey
                self.IDENTITY_FILE["pinned_noise_keys"] = pins
                current.update(self.IDENTITY_FILE)
                self._write_private(current)

        def forget_noise_key(self, node_id: str) -> bool:
            # This is an explicit administrative operation, never called by
            # authentication or reconnect error handling.
            with _store_lock, self.IDENTITY_FILE.lock:
                current = self._read_current()
                pins = current.get("pinned_noise_keys", {})
                if node_id not in pins:
                    return False
                del pins[node_id]
                current["pinned_noise_keys"] = pins
                self.IDENTITY_FILE["pinned_noise_keys"] = pins
                self._write_private(current)
                return True

    if state_dir is None:
        identity = PrivateNoiseIdentity()
    else:
        directory = Path(state_dir).expanduser()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink():
            raise ThalovantConnectionError("Noise state directory must not be a symlink.")
        identity = PrivateNoiseIdentity(JsonStorage(str(directory / "_identity.json")))
    path = Path(identity.IDENTITY_FILE.path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ThalovantConnectionError("Noise identity storage must not be a symlink.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        path.parent.chmod(0o700)
    return identity


def prepare_noise_key(identity: Any) -> str:
    """Create once with restrictive permissions; never replace malformed state."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    path = Path(identity.noise_key)
    with _store_lock:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise ThalovantConnectionError("Noise key must not be a symlink.")
        if not path.exists():
            key = X25519PrivateKey.generate().private_bytes(
                serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
            )
            fd, temporary = tempfile.mkstemp(prefix=".noise-key-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="ascii") as output:
                    output.write(key.hex())
                    output.flush()
                    os.fsync(output.fileno())
                # link publishes only the complete value and never replaces a
                # different process's winning identity (unlike os.replace).
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                os.unlink(temporary)
        if path.is_symlink():
            raise ThalovantConnectionError("Noise key must not be a symlink.")
        try:
            if len(bytes.fromhex(path.read_text(encoding="ascii").strip())) != 32:
                raise ValueError("invalid key length")
        except (ValueError, UnicodeError) as exc:
            raise ThalovantConnectionError("Stored Noise key is invalid; restore the existing identity.") from exc
        if os.name == "posix":
            path.chmod(0o600)
    return str(path)


class NoiseChannel:
    def __init__(self, identity: Any, *, state_dir: str | None, pin_id: str,
                 hello: dict[str, Any], write: Callable[[str | bytes], Any]) -> None:
        self.identity = identity
        self.store = noise_identity(state_dir)
        self.pin_id = pin_id
        self.hello = hello
        self.write = write
        self.server_hello: dict[str, Any] | None = None
        self.handshake: Any = None
        self.session: Any = None
        self.failed = False
        self.ready = False
        # Covers sealing AND delivery, so concurrent callers cannot reorder
        # nonce counters or interleave chunks. Receives are serial per adapter.
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self.failed = True
            self.ready = False
            self.handshake = self.session = self.server_hello = None

    def send(self, message: Any) -> Any:
        from hivemind_bus_client.util import serialize_message

        with self._lock:
            if self.failed or self.session is None:
                raise ThalovantConnectionError("HiveMind v3 Noise session is not established.")
            try:
                return self.session.send_message(serialize_message(message), self.write)
            except Exception:
                self.close()
                raise

    def receive(self, raw: str | bytes) -> Any:
        with self._lock:
            if self.failed:
                raise ThalovantConnectionError("Noise session failed; reconnect required.")
            try:
                return self._receive(raw)
            except Exception:
                self.close()
                raise

    def _receive(self, raw: str | bytes) -> Any:
        from hivemind_bus_client.client import decode_bitstring
        from hivemind_bus_client.message import HiveMessage
        from hivemind_bus_client.noise import (
            NoiseTransport, build_prologue, canonical_json, noise_protocol_name,
            select_noise_options, start_noise_handshake,
        )

        if self.session is not None:
            if not isinstance(raw, bytes):
                raise ThalovantConnectionError("Plaintext received after Noise authentication.")
            decoded = self.session.decrypt_frame(raw)
            if decoded is None:
                return None
            return HiveMessage(**json.loads(decoded)) if isinstance(decoded, str) else decode_bitstring(decoded)

        message = json.loads(raw)
        if not isinstance(message, dict) or not isinstance(message.get("payload"), dict):
            raise ThalovantConnectionError("Malformed HiveMind negotiation.")
        payload = message["payload"]
        if message.get("msg_type") == "hello":
            if self.server_hello is not None or not isinstance(payload.get("node_id"), str) or not payload["node_id"]:
                raise ThalovantConnectionError("Missing node_id or duplicate Noise HELLO.")
            self.server_hello = payload
            return None
        if message.get("msg_type") not in {"shake", "handshake"}:
            raise ThalovantConnectionError("Application traffic received before Noise authentication.")
        params = payload.get("noise")
        if not isinstance(params, dict):
            raise ThalovantConnectionError("The hub did not offer HiveMind v3 Noise.")
        if "msg" not in params:
            if self.server_hello is None or self.handshake is not None:
                raise ThalovantConnectionError("Out-of-order Noise negotiation.")
            pin = self.store.get_pinned_noise_key(self.pin_id)
            selection = select_noise_options(params.get("patterns") or [], params.get("suites") or [], pin)
            if selection is None:
                raise ThalovantConnectionError("No supported Noise pattern and cipher suite.")
            pattern, suite = selection
            name = noise_protocol_name(pattern, suite)
            self.handshake = start_noise_handshake(
                True, pattern, suite, self.identity.password, self.server_hello["node_id"],
                build_prologue(self.server_hello, payload, name),
                key_path=prepare_noise_key(self.store), remote_pubkey=pin,
            )
            msg = self.handshake.write_message(canonical_json({"binarize": False, "encodings": []}))
            self._shake({"pattern": pattern, "suite": suite, "msg": msg.hex()})
            return None
        if self.handshake is None:
            raise ThalovantConnectionError("Noise response arrived before negotiation.")
        wire = params["msg"]
        if not isinstance(wire, str) or len(wire) > 131070:
            raise ThalovantConnectionError("Malformed Noise handshake envelope.")
        self.handshake.read_message(bytes.fromhex(wire))
        if not self.handshake.handshake_finished:
            self._shake({"msg": self.handshake.write_message(b"").hex()})
        session = NoiseTransport(self.handshake)
        with _store_lock:
            pin = self.store.get_pinned_noise_key(self.pin_id)
            if pin and pin != session.remote_static_key:
                raise ThalovantConnectionError("Trusted Noise server key changed; refusing connection.")
            if not session.remote_static_key:
                raise ThalovantConnectionError("Noise handshake did not authenticate a server key.")
            if not pin:
                self.store.pin_noise_key(self.pin_id, session.remote_static_key)
                path = Path(self.store.IDENTITY_FILE.path)
                if os.name == "posix":
                    path.chmod(0o600)
        self.session = session
        self.handshake = None
        self.send(self.hello)
        self.ready = True
        return None

    def _shake(self, params: dict[str, Any]) -> None:
        self.write(json.dumps({"msg_type": "shake", "payload": {"noise": params}, "metadata": {}, "route": []}))
