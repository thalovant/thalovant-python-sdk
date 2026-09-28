"""HiveMind v3 negotiation and the state it keeps on disk.

Three things live here, all transport-independent:

- **The identity store.** The static X25519 key the hub pins on first
  contact, the hub keys this client pinned, and the cache of derived
  pre-shared keys. They stay where hivemind-bus-client kept them, in the same
  formats, so a device that upgrades keeps the key its hub already trusts:
  ``$XDG_CONFIG_HOME/hivemind/_identity.json`` (or ``<state_dir>/_identity.json``),
  the key at ``<name>_noise.key`` beside it, and ``<name>_noise_psks.json``.
- **:class:`NoiseClientProtocol`**, the client side of the negotiation as a
  state machine with no I/O: feed it what the hub sent, send what it returns.
  The argon2id derivation is a separate step the driver runs where it likes
  (the event loop's executor, or inline on a worker thread).
- **:class:`NoiseChannel`**, the same machine behind a synchronous ``write``
  callback, for the MQTT carrier.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import tempfile
import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import _noise
from ._wire import HiveMessage, decode_binary_frame, hive_message_from_json
from .errors import ThalovantConnectionError, ThalovantHubKeyChangedError, ThalovantHubRefusedError

__all__ = [
    "NoiseChannel",
    "NoiseClientProtocol",
    "NoiseIdentityStore",
    "NoiseStep",
    "forget_cached_psk",
    "load_cached_psk",
    "noise_identity",
    "prepare_noise_key",
    "save_cached_psk",
]

log = logging.getLogger("thalovant.transport")

_store_lock = threading.RLock()

#: A Noise PSK is exactly this long; anything else is not a key.
_PSK_LENGTH = 32
_MAX_NODE_ID_LENGTH = 512
_MAX_CACHE_ENTRIES = 32
_MAX_CACHE_BYTES = 1 << 20
#: Beside ``<name>_noise.key``, as hivemind-bus-client names it.
NOISE_PSK_CACHE_SUFFIX = "_psks.json"
#: The largest hex-encoded Noise message a peer may send us (65535 bytes).
_MAX_NOISE_HEX = 131070


# -- storage primitives ------------------------------------------------------


def _config_home() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return Path(configured) if configured else Path.home() / ".config"


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Serialize writers across processes with an OS lock beside ``path``."""
    lock_path = path.with_name(f".{path.name}.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "posix":
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        else:  # pragma: no cover - exercised on Windows CI only
            import msvcrt

            windows: Any = msvcrt
            windows.locking(fd, windows.LK_LOCK, 1)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                windows.locking(fd, windows.LK_UNLCK, 1)
    finally:
        os.close(fd)


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace ``path`` atomically with a file only its owner can read.

    ``mkstemp`` creates the file 0600 from the start, so the content is never
    readable by anyone else, even for the moment between writing and renaming.
    """
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        if os.name == "posix":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _validated_noise_pins(value: Any) -> dict[str, str]:
    """Reject malformed trust state before reading or changing any peer pin."""
    if not isinstance(value, dict):
        raise ThalovantConnectionError("Stored Noise server pins must be an object.")
    for pin in value.values():
        try:
            if not isinstance(pin, str) or len(pin) != 64 or len(bytes.fromhex(pin)) != 32:
                raise ValueError("invalid pin length")
        except ValueError as exc:
            raise ThalovantConnectionError("Stored Noise server pin is invalid.") from exc
    return value


class _IdentityFile(dict):  # type: ignore[type-arg]
    """The in-memory copy of ``_identity.json``, with the path and lock beside it.

    Shaped like the JsonStorage hivemind-bus-client used, because code that
    reached through ``store.IDENTITY_FILE`` keeps reading ``.path`` and
    taking ``.lock``.
    """

    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        super().__init__(data)
        self.path = str(path)
        self._path = path

    @property
    def lock(self) -> contextlib.AbstractContextManager[None]:
        return _file_lock(self._path)


class NoiseIdentityStore:
    """The Noise identity of one client: its static key and the hubs it trusts."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.IDENTITY_FILE = _IdentityFile(path, self._read_current())

    # hivemind-bus-client's NodeIdentity names the key after the node.
    @property
    def name(self) -> str:
        data = self.IDENTITY_FILE
        name = data.get("name")
        if isinstance(name, str) and name:
            return name
        key = data.get("key")
        if isinstance(key, str) and key:
            return os.path.basename(key)
        return "unnamed-node"

    @property
    def noise_key(self) -> str:
        """Where the static X25519 key lives, resolved the way NodeIdentity does."""
        base = self._path.parent
        stored = self.IDENTITY_FILE.get("noise_key")
        default_name = f"{self.name}_noise.key"
        if not isinstance(stored, str) or not stored:
            return str(base / default_name)
        candidate = Path(stored)
        if not candidate.is_absolute():
            return str(base / candidate)
        if candidate.is_file():
            return stored
        probe = candidate.parent
        while not probe.is_dir() and probe != probe.parent:
            probe = probe.parent
        if probe.is_dir() and os.access(probe, os.W_OK):
            return stored
        return str(base / candidate.name)

    @property
    def pinned_noise_keys(self) -> dict[str, str]:
        pins = self.IDENTITY_FILE.get("pinned_noise_keys")
        return dict(pins) if isinstance(pins, dict) else {}

    def _read_current(self) -> dict[str, Any]:
        path = self._path
        if path.is_symlink():
            raise ThalovantConnectionError("Noise identity must not be a symlink.")
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf8"))
        except (ValueError, UnicodeError) as exc:
            raise ThalovantConnectionError("Stored Noise identity is invalid.") from exc
        if not isinstance(data, dict):
            raise ThalovantConnectionError("Stored Noise identity is invalid.")
        return data

    def _write_private(self, data: dict[str, Any]) -> None:
        path = self._path
        if path.is_symlink():
            raise ThalovantConnectionError("Noise identity must not be a symlink.")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_private_json(path, data)

    def save(self) -> None:
        with _store_lock, self.IDENTITY_FILE.lock:
            current = self._read_current()
            # Other processes may have learned additional peers meanwhile.
            pins = _validated_noise_pins(
                current.get("pinned_noise_keys", self.IDENTITY_FILE.get("pinned_noise_keys", {}))
            )
            current.update(self.IDENTITY_FILE)
            current["pinned_noise_keys"] = pins
            self._write_private(current)

    def get_pinned_noise_key(self, node_id: str) -> str | None:
        with _store_lock, self.IDENTITY_FILE.lock:
            data = self._read_current()
            pins = _validated_noise_pins(
                data.get("pinned_noise_keys", self.IDENTITY_FILE.get("pinned_noise_keys", {}))
            )
            return pins.get(node_id)

    def pin_noise_key(self, node_id: str, pubkey: str) -> None:
        with _store_lock, self.IDENTITY_FILE.lock:
            current = self._read_current()
            pins = _validated_noise_pins(current.get("pinned_noise_keys", {}))
            _validated_noise_pins({node_id: pubkey})
            pinned = pins.get(node_id)
            if pinned is not None:
                if pinned.lower() != pubkey.lower():
                    raise ThalovantHubKeyChangedError(
                        "Trusted Noise server key changed; refusing connection."
                    )
                return
            pins[node_id] = pubkey
            self.IDENTITY_FILE["pinned_noise_keys"] = pins
            current.update(self.IDENTITY_FILE)
            self._write_private(current)

    def forget_noise_key(self, node_id: str) -> bool:
        # An explicit administrative operation, never called by
        # authentication or reconnect error handling.
        with _store_lock, self.IDENTITY_FILE.lock:
            current = self._read_current()
            pins = _validated_noise_pins(current.get("pinned_noise_keys", {}))
            if node_id not in pins:
                return False
            del pins[node_id]
            current["pinned_noise_keys"] = pins
            self.IDENTITY_FILE["pinned_noise_keys"] = pins
            self._write_private(current)
            return True


#: The folder a client's Noise state is kept in, beside its identity file.
STATE_DIR_NAME = "hivemind"


def legacy_state_dir() -> Path:
    """Where 0.9.0 and hivemind-bus-client kept every client's Noise state: ``~/.config/hivemind``."""
    return _config_home() / STATE_DIR_NAME


def identity_state_dir(identity: Any) -> Path | None:
    """The ``hivemind`` folder beside the file *identity* was read from, if it was read from one.

    ``~/.config/thalovant/identity.json`` keeps its key in
    ``~/.config/thalovant/hivemind``, where thalovant-voice and the
    satellite installer keep it, so a CLI run and the satellite that read
    the same identity file present the same key to the hub.
    """
    source = getattr(identity, "source_path", None)
    if not isinstance(source, str) or not source:
        return None
    return Path(source).expanduser().parent / STATE_DIR_NAME


def default_state_dir(identity: Any) -> Path:
    """Where the Noise state of *identity* lives when no ``noise_state_dir`` is given.

    Beside its identity file when it came from one and that folder exists or
    can be made; otherwise the shared default, as before 0.9.1 -- an identity
    in ``/etc/thalovant`` read by a user who cannot write there keeps using
    the user's own folder.
    """
    beside = identity_state_dir(identity)
    if beside is None:
        return legacy_state_dir()
    if beside.is_dir() or os.access(beside.parent, os.W_OK):
        return beside
    return legacy_state_dir()


def _hub_hosts(identity: Any) -> set[str]:
    """The hub host names an identity's pins can be filed under, whatever the carrier."""
    from urllib.parse import urlsplit

    hosts: set[str] = set()
    for endpoint in (
        _call_quietly(getattr(identity, "endpoint_base", None)),
        _call_quietly(getattr(identity, "endpoint_for", None), "wss"),
    ):
        if isinstance(endpoint, str) and endpoint:
            host = urlsplit(endpoint).hostname
            if host:
                hosts.add(host.lower())
    return hosts


def _call_quietly(method: Any, *args: Any) -> Any:
    if not callable(method):
        return None
    try:
        return method(*args)
    except Exception:  # noqa: BLE001 - an identity without that endpoint names no host
        return None


def adopt_legacy_key(target: Path, identity: Any, legacy: Path | None = None) -> bool:
    """Copy this identity's key from the old default folder into *target*, once.

    Before 0.9.1 a client with no ``noise_state_dir`` kept its key in
    ``~/.config/hivemind`` whatever file its identity came from. The hub pins
    the first key a connection presents, so a device that silently got a new
    key in a new folder would be locked out. When *target* holds no Noise
    state yet and the old folder holds a key that has already met this
    identity's hub (a pin filed under the hub's host), that key and the hub
    pins are **copied** -- never moved: another program may still read the
    old folder. Returns whether it copied.
    """
    legacy = legacy_state_dir() if legacy is None else legacy
    with contextlib.suppress(OSError):
        if target.resolve() == legacy.resolve():
            return False
    if (target / "_identity.json").exists() or any(target.glob("*_noise.key")):
        return False
    source_file = legacy / "_identity.json"
    if not source_file.is_file() or source_file.is_symlink() or legacy.is_symlink():
        return False
    try:
        source = NoiseIdentityStore(source_file)
    except ThalovantConnectionError:
        return False
    key_path = Path(source.noise_key)
    if not key_path.is_file() or key_path.is_symlink():
        return False
    pins = source.pinned_noise_keys
    hosts = _hub_hosts(identity)
    if not any(_pin_host(pin_id) in hosts for pin_id in pins):
        # The old key never met this identity's hub: it is not the key the
        # hub pinned for it, so there is nothing to keep.
        return False
    try:
        key = bytes.fromhex(key_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError, UnicodeError):
        return False
    if len(key) != 32:
        return False
    if target.is_symlink():
        raise ThalovantConnectionError("Noise state directory must not be a symlink.")
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity_file = target / "_identity.json"
    with _store_lock, _file_lock(identity_file):
        # Another process may have adopted (or started afresh) meanwhile.
        if identity_file.exists() or any(target.glob("*_noise.key")):
            return False
        name = source.name
        copied_key = target / f"{name}_noise.key"
        fd, temporary = tempfile.mkstemp(prefix=".noise-key-", dir=target)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as output:
                output.write(key.hex())
                output.flush()
                os.fsync(output.fileno())
            if os.name == "posix":
                os.chmod(temporary, 0o600)
            with contextlib.suppress(FileExistsError):
                os.link(temporary, copied_key)
        finally:
            os.unlink(temporary)
        # The key and the hub pins, and nothing else the old file may hold.
        data = {"name": name, "pinned_noise_keys": _validated_noise_pins(pins)}
        _write_private_json(identity_file, data)
    log.info(
        "Copied this identity's Noise key from %s to %s, where it is kept from now on; %s is left as it was.",
        legacy, target, legacy,
    )
    return True


def _pin_host(pin_id: str) -> str | None:
    from urllib.parse import urlsplit

    try:
        host = urlsplit(pin_id).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def noise_identity(state_dir: str | None = None, *, identity: Any = None) -> NoiseIdentityStore:
    """The identity store in ``state_dir``; by default, beside the identity's file.

    With no ``state_dir``, an identity read from a file keeps its state in
    the ``hivemind`` folder beside that file (:func:`default_state_dir`), and
    the first time that folder is used it takes the key this identity had in
    the old shared folder (:func:`adopt_legacy_key`). Any other identity
    uses the shared default, ``$XDG_CONFIG_HOME/hivemind``.
    """
    if state_dir is None:
        directory = default_state_dir(identity)
        if directory != legacy_state_dir():
            adopt_legacy_key(directory, identity)
    else:
        directory = Path(state_dir).expanduser()
    if directory.is_symlink():
        raise ThalovantConnectionError("Noise state directory must not be a symlink.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "_identity.json"
    if path.is_symlink() or path.parent.is_symlink():
        raise ThalovantConnectionError("Noise identity storage must not be a symlink.")
    if os.name == "posix":
        path.parent.chmod(0o700)
    return NoiseIdentityStore(path)


def prepare_noise_key(identity: NoiseIdentityStore) -> str:
    """Create the static key once with restrictive permissions; never replace it."""
    path = Path(identity.noise_key)
    with _store_lock:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise ThalovantConnectionError("Noise key must not be a symlink.")
        if not path.exists():
            key = _noise.generate_private_key()
            fd, temporary = tempfile.mkstemp(prefix=".noise-key-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="ascii") as output:
                    output.write(key.hex())
                    output.flush()
                    os.fsync(output.fileno())
                # link publishes only the complete value and never replaces a
                # different process's winning identity (unlike os.replace).
                with contextlib.suppress(FileExistsError):
                    os.link(temporary, path)
            finally:
                os.unlink(temporary)
        if path.is_symlink():
            raise ThalovantConnectionError("Noise key must not be a symlink.")
        try:
            if len(bytes.fromhex(path.read_text(encoding="ascii").strip())) != 32:
                raise ValueError("invalid key length")
        except (ValueError, UnicodeError) as exc:
            raise ThalovantConnectionError(
                "Stored Noise key is invalid; restore the existing identity."
            ) from exc
        if os.name == "posix":
            path.chmod(0o600)
    return str(path)


def load_noise_key(identity: NoiseIdentityStore) -> bytes:
    """The raw static private key, created on first use."""
    return bytes.fromhex(Path(prepare_noise_key(identity)).read_text(encoding="ascii").strip())


# -- the pre-shared key cache ---------------------------------------------------


def _psk_cache_path(key_path: str | None) -> Path | None:
    if not key_path:
        return None
    base, _extension = os.path.splitext(key_path)
    return Path(f"{base}{NOISE_PSK_CACHE_SUFFIX}")


def _cache_entry(node_id: str, scope: str | None) -> str:
    # The scope is the client's access key, stored as a truncated digest so
    # the file never carries the credential, and so two clients sharing one
    # key file keep separate entries.
    if not scope:
        return node_id
    tag = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
    return f"{tag}@{node_id}"


def _cacheable(node_id: Any) -> bool:
    return isinstance(node_id, str) and 0 < len(node_id) <= _MAX_NODE_ID_LENGTH


def _read_psk_cache(path: Path) -> dict[str, Any]:
    # Derivable state: a damaged, oversized or too-permissive cache costs one
    # derivation, never a failed connection.
    try:
        if path.stat().st_size > _MAX_CACHE_BYTES:
            return {}
        if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            return {}
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return cache if isinstance(cache, dict) else {}


def load_cached_psk(key_path: str | None, node_id: str, scope: str | None = None) -> bytes | None:
    """The stored PSK for ``node_id`` under ``scope``, or ``None``."""
    path = _psk_cache_path(key_path)
    if path is None or not _cacheable(node_id):
        return None
    encoded = _read_psk_cache(path).get(_cache_entry(node_id, scope))
    if not isinstance(encoded, str):
        return None
    try:
        psk = bytes.fromhex(encoded)
    except ValueError:
        return None
    return psk if len(psk) == _PSK_LENGTH else None


def save_cached_psk(
    key_path: str | None, node_id: str, psk: bytes, scope: str | None = None
) -> None:
    """Keep a derived PSK so the next connection skips argon2id. Best effort."""
    path = _psk_cache_path(key_path)
    if path is None or not _cacheable(node_id) or len(psk) != _PSK_LENGTH:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        cache = _read_psk_cache(path)
        entry = _cache_entry(node_id, scope)
        if cache.get(entry) == psk.hex():
            return
        cache.pop(entry, None)
        cache[entry] = psk.hex()
        while len(cache) > _MAX_CACHE_ENTRIES:
            cache.pop(next(iter(cache)))
        _write_private_json(path, cache)
    except OSError:
        pass


def forget_cached_psk(key_path: str | None, node_id: str, scope: str | None = None) -> None:
    """Drop a stored key; a rejected handshake is how a rotated password shows."""
    path = _psk_cache_path(key_path)
    if path is None or not _cacheable(node_id):
        return
    try:
        cache = _read_psk_cache(path)
        if cache.pop(_cache_entry(node_id, scope), None) is None:
            return
        _write_private_json(path, cache)
    except OSError:
        pass


# -- the negotiation ---------------------------------------------------------------


def hello_message(session_id: str, site_id: str | None) -> dict[str, Any]:
    """The encrypted HELLO: the first transport message of every session."""
    return {
        "msg_type": "hello",
        "payload": {"pubkey": "", "session": {"session_id": session_id}, "site_id": site_id},
        "metadata": {},
        "route": [],
        "node": None,
        "target_site_id": None,
        "target_pubkey": None,
        "source_peer": None,
    }


def _shake(params: dict[str, Any]) -> str:
    return json.dumps(
        {"msg_type": "shake", "payload": {"noise": params}, "metadata": {}, "route": []}
    )


@dataclass
class NoiseStep:
    """What one inbound frame produced.

    ``send`` holds frames to write in order (cleartext handshake frames are
    ``str``, transport frames ``bytes``); ``messages`` the decoded application
    traffic. ``need_psk`` names the hub whose pre-shared key must be supplied
    through :meth:`NoiseClientProtocol.provide_psk` before anything else.
    """

    send: list[str | bytes] = field(default_factory=list)
    messages: list[HiveMessage] = field(default_factory=list)
    need_psk: str | None = None
    ready: bool = False


class NoiseClientProtocol:
    """The client side of the HiveMind v3 negotiation, with no I/O.

    HELLO carries the hub's node id; the offer names its patterns and suites;
    message 1 goes out, message 2 comes back, message 3 goes out for
    ``XXpsk2``; then the hub's static key is checked against the pin and the
    encrypted HELLO is the first transport message. ``KKpsk0`` is chosen only
    with a pin in hand. After a ``KKpsk0`` failure the next negotiation of
    the same instance uses ``XXpsk2`` once, which must present the pinned key.
    """

    def __init__(
        self,
        *,
        store: NoiseIdentityStore,
        pin_id: str,
        hello: dict[str, Any],
        password: str,
        access_key: str | None = None,
    ) -> None:
        self.store = store
        self.pin_id = pin_id
        self.hello = hello
        self._password = password
        self._scope = access_key
        self._xx_retry = False
        self.reset()

    def reset(self) -> None:
        """Forget one connection's negotiation; the pins and the retry note stay."""
        self.server_hello: dict[str, Any] | None = None
        self.handshake: _noise.NoiseHandshake | None = None
        self.session: _noise.NoiseSession | None = None
        self.pattern: str | None = None
        self.node_id: str | None = None
        self._offer: dict[str, Any] | None = None
        self._selection: tuple[str, str, str | None] | None = None
        self.failed = False
        self.ready = False

    @property
    def awaiting_psk(self) -> bool:
        return self._selection is not None and self.handshake is None

    @property
    def kk_failed(self) -> bool:
        """Whether the attempt just made was KK and did not authenticate: the next one uses XX."""
        return self._xx_retry and self.pattern == _noise.PATTERN_KK

    def refused_during_handshake(self) -> None:
        """The hub closed with a refusal code while this client's handshake was under way.

        After a KK first message, that is what a hub does when it cannot
        authenticate it -- because the password changed, or because the hub's
        own key did and the message was sealed to the old one. Only XX tells
        those apart, so the next attempt uses it, as it does after a KK answer
        that does not authenticate here.
        """
        if self.pattern == _noise.PATTERN_KK and self.handshake is not None:
            self._xx_retry = True
            self._forget_psk()

    def close(self) -> None:
        self.failed = True
        self.ready = False
        self.handshake = self.session = self.server_hello = None

    # The key file can only be read once the negotiation has started, so a
    # store problem surfaces as a failed connection rather than at construction.
    def _key_path(self) -> str:
        return prepare_noise_key(self.store)

    def cached_psk(self) -> bytes | None:
        if self.node_id is None:
            return None
        return load_cached_psk(self._key_path(), self.node_id, self._scope)

    def derive_psk(self) -> bytes:
        """Stretch the password for this hub: argon2id, about 0.1 s of CPU."""
        if self.node_id is None:
            raise ThalovantConnectionError("Out-of-order Noise negotiation.")
        return _noise.derive_psk(self._password, self.node_id)

    def receive(self, raw: str | bytes) -> NoiseStep:
        if self.failed:
            raise ThalovantConnectionError("Noise session failed; reconnect required.")
        try:
            return self._receive(raw)
        except Exception:
            self.close()
            raise

    def provide_psk(self, psk: bytes) -> NoiseStep:
        """Continue the negotiation the offer started, with the hub's PSK."""
        if self.failed:
            raise ThalovantConnectionError("Noise session failed; reconnect required.")
        try:
            return self._start(psk)
        except Exception:
            self.close()
            raise

    def seal(self, message: dict[str, Any] | HiveMessage | str) -> list[bytes]:
        """Encrypt one application message into its transport frames."""
        if self.failed or self.session is None:
            raise ThalovantConnectionError("HiveMind v3 Noise session is not established.")
        if isinstance(message, HiveMessage):
            text = message.serialize()
        elif isinstance(message, str):
            text = message
        else:
            text = json.dumps(message, ensure_ascii=False)
        try:
            return self.session.encrypt_message(text.encode("utf-8"))
        except Exception:
            self.close()
            raise

    def _receive(self, raw: str | bytes) -> NoiseStep:
        if self.session is not None:
            if not isinstance(raw, (bytes, bytearray)):
                raise ThalovantConnectionError("Plaintext received after Noise authentication.")
            try:
                frame = self.session.decrypt_frame(bytes(raw))
            except _noise.NoiseError as exc:
                raise ThalovantConnectionError(str(exc)) from None
            if frame is None:
                return NoiseStep(ready=True)
            if frame.is_json:
                message = hive_message_from_json(frame.payload.decode("utf-8"))
            else:
                message = decode_binary_frame(frame.payload)
            return NoiseStep(messages=[message], ready=True)

        if isinstance(raw, (bytes, bytearray)):
            raise ThalovantConnectionError("Application traffic received before Noise authentication.")
        try:
            message = json.loads(raw)
        except ValueError:
            raise ThalovantConnectionError("Malformed HiveMind negotiation.") from None
        if not isinstance(message, dict) or not isinstance(message.get("payload"), dict):
            raise ThalovantConnectionError("Malformed HiveMind negotiation.")
        payload: dict[str, Any] = message["payload"]
        if message.get("msg_type") == "hello":
            node_id = payload.get("node_id")
            if self.server_hello is not None or not isinstance(node_id, str) or not node_id:
                raise ThalovantConnectionError("Missing node_id or duplicate Noise HELLO.")
            self.server_hello = payload
            self.node_id = node_id
            return NoiseStep()
        if message.get("msg_type") not in {"shake", "handshake"}:
            raise ThalovantConnectionError("Application traffic received before Noise authentication.")
        params = payload.get("noise")
        if not isinstance(params, dict):
            raise ThalovantConnectionError("The hub did not offer HiveMind v3 Noise.")
        if "msg" not in params:
            if self.server_hello is None or self._selection is not None:
                raise ThalovantConnectionError("Out-of-order Noise negotiation.")
            patterns = [item for item in params.get("patterns") or [] if isinstance(item, str)]
            suites = [item for item in params.get("suites") or [] if isinstance(item, str)]
            pin = None if self._xx_retry else self.store.get_pinned_noise_key(self.pin_id)
            selection = _noise.select_options(patterns, suites, pinned=pin is not None)
            if selection is None:
                raise ThalovantConnectionError("No supported Noise pattern and cipher suite.")
            pattern, suite = selection
            self._offer = payload
            self._selection = (pattern, suite, pin if pattern == _noise.PATTERN_KK else None)
            psk = self.cached_psk()
            if psk is None:
                assert self.node_id is not None
                return NoiseStep(need_psk=self.node_id)
            return self._start(psk)
        if self.handshake is None:
            raise ThalovantConnectionError("Noise response arrived before negotiation.")
        wire = params["msg"]
        if not isinstance(wire, str) or len(wire) > _MAX_NOISE_HEX:
            raise ThalovantConnectionError("Malformed Noise handshake envelope.")
        try:
            response = bytes.fromhex(wire)
        except ValueError:
            raise ThalovantConnectionError("Malformed Noise handshake envelope.") from None
        try:
            self.handshake.read_message(response)
        except _noise.NoiseError:
            # The pin is never dropped for a failed authentication. The PSK is
            # the other thing this message authenticates, so a rejection may
            # mean it came from a password since rotated: forget it.
            if self.pattern == _noise.PATTERN_KK:
                self._xx_retry = True
            self._forget_psk()
            # The hub's answer did not authenticate under the key this
            # password derives: the hub turned the credentials away (or the
            # negotiation was tampered with, which retrying will not fix
            # either). A refusal, like the hub closing on an unknown key.
            raise ThalovantHubRefusedError(
                "Noise handshake authentication failed (wrong password or tampered negotiation)."
            ) from None
        step = NoiseStep()
        if not self.handshake.finished:
            step.send.append(_shake({"msg": self.handshake.write_message(b"").hex()}))
        session = self.handshake.into_session()
        remote = session.remote_static.hex() if session.remote_static else None
        with _store_lock:
            if not remote:
                raise ThalovantConnectionError("Noise handshake did not authenticate a server key.")
            pin = self.store.get_pinned_noise_key(self.pin_id)
            if pin and pin.lower() != remote.lower():
                raise ThalovantHubKeyChangedError("Trusted Noise server key changed; refusing connection.")
            if not pin:
                self.store.pin_noise_key(self.pin_id, remote)
                if os.name == "posix":
                    with contextlib.suppress(OSError):
                        Path(self.store.IDENTITY_FILE.path).chmod(0o600)
        self.session = session
        self.handshake = None
        self._xx_retry = False
        step.send.extend(self.seal(self.hello))
        self.ready = True
        step.ready = True
        return step

    def _start(self, psk: bytes) -> NoiseStep:
        if self._selection is None or self.server_hello is None or self._offer is None:
            raise ThalovantConnectionError("Out-of-order Noise negotiation.")
        if self.handshake is not None:
            raise ThalovantConnectionError("Duplicate Noise negotiation.")
        pattern, suite, pin = self._selection
        assert self.node_id is not None
        save_cached_psk(self._key_path(), self.node_id, psk, self._scope)
        name = _noise.protocol_name(pattern, suite)
        self.handshake = _noise.NoiseHandshake(
            pattern,
            suite,
            psk,
            _noise.build_prologue(self.server_hello, self._offer, name),
            load_noise_key(self.store),
            remote_static=bytes.fromhex(pin) if pin else None,
        )
        self.pattern = pattern
        first = self.handshake.write_message(
            _noise.canonical_json({"binarize": False, "encodings": []})
        )
        return NoiseStep(send=[_shake({"pattern": pattern, "suite": suite, "msg": first.hex()})])

    def _forget_psk(self) -> None:
        if self.node_id is None:
            return
        with contextlib.suppress(Exception):
            forget_cached_psk(self._key_path(), self.node_id, self._scope)


class NoiseChannel:
    """:class:`NoiseClientProtocol` behind a synchronous ``write`` callback.

    One channel belongs to one connection; its static identity and trusted
    server keys outlive it. Sends and receives are serialized, so concurrent
    callers cannot reorder nonce counters or interleave chunks.
    """

    def __init__(
        self,
        identity: Any,
        *,
        state_dir: str | None,
        pin_id: str,
        hello: dict[str, Any],
        write: Callable[[str | bytes], Any],
        use_xx: bool = False,
    ) -> None:
        self.identity = identity
        self.store = noise_identity(state_dir, identity=identity)
        self.pin_id = pin_id
        self.hello = hello
        self.write = write
        self._protocol = NoiseClientProtocol(
            store=self.store,
            pin_id=pin_id,
            hello=hello,
            password=str(getattr(identity, "password", "") or ""),
            access_key=getattr(identity, "access_key", None),
        )
        if use_xx:
            # The attempt before this one was KK and did not authenticate.
            self._protocol._xx_retry = True
        self._lock = threading.RLock()

    @property
    def kk_failed(self) -> bool:
        """Whether this channel's KK attempt did not authenticate: the next one uses XX."""
        return self._protocol.kk_failed

    @property
    def ready(self) -> bool:
        return self._protocol.ready

    @property
    def failed(self) -> bool:
        return self._protocol.failed

    @property
    def session(self) -> Any:
        return self._protocol.session

    @property
    def server_hello(self) -> dict[str, Any] | None:
        return self._protocol.server_hello

    def close(self) -> None:
        with self._lock:
            self._protocol.close()

    def send(self, message: Any) -> Any:
        with self._lock:
            result = None
            for frame in self._protocol.seal(message):
                result = self.write(frame)
            return result

    def receive(self, raw: str | bytes) -> HiveMessage | None:
        with self._lock:
            if self._protocol.session is None and isinstance(raw, (bytes, bytearray)):
                # MQTT carries every frame as bytes; before authentication the
                # only frames are the cleartext negotiation, which is JSON text.
                try:
                    raw = bytes(raw).decode("utf-8")
                except UnicodeDecodeError:
                    pass
            step = self._protocol.receive(raw)
            if step.need_psk is not None:
                psk = self._protocol.derive_psk()
                step = self._protocol.provide_psk(psk)
            for frame in step.send:
                try:
                    self.write(frame)
                except Exception:
                    self._protocol.close()
                    raise
            return step.messages[0] if step.messages else None
