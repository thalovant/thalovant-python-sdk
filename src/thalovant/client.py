"""High-level client for Thalovant HiveMind HTTP connections."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import threading
import time
from typing import Any, Callable, Protocol

from .errors import (
    ThalovantConnectionError,
    ThalovantRuntimeError,
    ThalovantTimeoutError,
)
from .identity import ThalovantIdentity


DEFAULT_USERAGENT = "ThalovantPythonSDK/0.1.0"


class _Transport(Protocol):
    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def on_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None: ...

    def remove_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None: ...

    def emit_event(
        self,
        event_type: str,
        data: dict[str, Any],
        context: dict[str, Any],
    ) -> Any: ...


@dataclass(frozen=True)
class ThalovantReply:
    """A normalized response from a hub utterance request."""

    text: str
    utterances: tuple[str, ...] = ()
    handled: bool = False
    raw_messages: tuple[Any, ...] = field(default_factory=tuple)


class ThalovantClient:
    """Developer-friendly wrapper around HiveMind's HTTP protocol client."""

    def __init__(
        self,
        identity: ThalovantIdentity,
        *,
        useragent: str = DEFAULT_USERAGENT,
        connect_timeout: float = 4.0,
        handshake_timeout: float = 6.0,
        reply_settle_seconds: float = 0.25,
        transport: _Transport | None = None,
    ) -> None:
        self.identity = identity
        self.useragent = useragent
        self.reply_settle_seconds = reply_settle_seconds
        self._transport = transport or HiveMindHTTPTransport(
            identity,
            useragent=useragent,
            connect_timeout=connect_timeout,
            handshake_timeout=handshake_timeout,
        )
        self._connected = False

    @classmethod
    def from_identity_file(
        cls,
        path: str | Path,
        **kwargs: Any,
    ) -> "ThalovantClient":
        """Create a client from a Thalovant/HiveMind identity JSON file."""

        return cls(ThalovantIdentity.from_file(path), **kwargs)

    @classmethod
    def from_env(cls, **kwargs: Any) -> "ThalovantClient":
        """Create a client from `THALOVANT_*` environment variables."""

        return cls(ThalovantIdentity.from_env(), **kwargs)

    def __enter__(self) -> "ThalovantClient":
        self.connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def connect(self) -> None:
        """Open the HiveMind HTTP connection if needed."""

        if self._connected:
            return
        self._transport.connect()
        self._connected = True

    def close(self) -> None:
        """Disconnect the underlying HiveMind HTTP client."""

        if not self._connected:
            return
        self._transport.disconnect()
        self._connected = False

    disconnect = close

    def emit(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Any:
        """Emit a raw OVOS/HiveMind bus event through the HTTP data plane."""

        self.connect()
        return self._transport.emit_event(event_type, data or {}, context or {})

    def ask(
        self,
        text: str,
        *,
        timeout: float = 12.0,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
    ) -> ThalovantReply:
        """Send a text utterance and wait for the hub's spoken reply."""

        prompt = text.strip()
        if not prompt:
            raise ValueError("ask() requires a non-empty text prompt.")

        self.connect()

        handled = threading.Event()
        failed = threading.Event()
        fragments: list[str] = []
        raw_messages: list[Any] = []

        def handle_speak(message: Any) -> None:
            raw_messages.append(message)
            utterance = _message_data(message).get("utterance")
            if isinstance(utterance, str) and utterance.strip():
                normalized = " ".join(utterance.strip().split())
                if not fragments or fragments[-1] != normalized:
                    fragments.append(normalized)

        def handle_handled(message: Any) -> None:
            raw_messages.append(message)
            handled.set()

        def handle_failure(message: Any) -> None:
            raw_messages.append(message)
            failed.set()
            handled.set()

        handlers = (
            ("speak", handle_speak),
            ("ovos.utterance.handled", handle_handled),
            ("complete_intent_failure", handle_failure),
        )

        for event_name, handler in handlers:
            self._transport.on_mycroft(event_name, handler)

        try:
            payload = {"utterances": [prompt], "lang": lang}
            self._transport.emit_event(
                "recognizer_loop:utterance",
                payload,
                context or {},
            )
            if not handled.wait(timeout=timeout):
                raise ThalovantTimeoutError(
                    f"Hub did not finish handling the utterance within {timeout:g}s."
                )
            if self.reply_settle_seconds > 0:
                time.sleep(self.reply_settle_seconds)
            if failed.is_set() and not fragments:
                raise ThalovantRuntimeError("Hub reported that the utterance could not be handled.")
            return ThalovantReply(
                text=" ".join(fragments),
                utterances=tuple(fragments),
                handled=handled.is_set() and not failed.is_set(),
                raw_messages=tuple(raw_messages),
            )
        finally:
            for event_name, handler in handlers:
                self._transport.remove_mycroft(event_name, handler)


class HiveMindHTTPTransport:
    """Thin adapter around `hivemind_bus_client.http_client.HiveMindHTTPClient`."""

    def __init__(
        self,
        identity: ThalovantIdentity,
        *,
        useragent: str = DEFAULT_USERAGENT,
        connect_timeout: float = 4.0,
        handshake_timeout: float = 6.0,
        handshake_poll_interval: float = 0.1,
        handshake_settle_seconds: float = 0.1,
        self_signed: bool = True,
        compress: bool = False,
        binarize: bool = False,
    ) -> None:
        self.identity = identity
        self.useragent = useragent
        self.connect_timeout = connect_timeout
        self.handshake_timeout = handshake_timeout
        self.handshake_poll_interval = handshake_poll_interval
        self.handshake_settle_seconds = handshake_settle_seconds
        self.self_signed = self_signed
        self.compress = compress
        self.binarize = binarize
        self._client: Any | None = None
        self._transport_connected = False
        self._deps: _HiveMindDeps | None = None

    def connect(self) -> None:
        if self._transport_connected:
            return

        deps = self._load_deps()
        client = deps.HiveMindHTTPClient(
            key=self.identity.access_key,
            password=self.identity.password,
            crypto_key=None,
            host=self.identity.default_master,
            port=self.identity.default_port,
            useragent=self.useragent,
            self_signed=self.self_signed,
            compress=self.compress,
            binarize=self.binarize,
        )
        protocol = self._build_protocol(client, deps)
        client.identity.site_id = self.identity.site_id
        client.protocol = protocol
        client.protocol.identity = client.identity
        client.protocol.site_id = self.identity.site_id
        client.protocol.bind(client.internal_bus)

        try:
            response = deps.requests.post(
                f"{client.base_url}/connect",
                params={"authorization": client.auth},
                timeout=self.connect_timeout,
            )
        except deps.requests.RequestException as exc:
            self._shutdown_client(client, transport_connected=False)
            raise ThalovantConnectionError("Could not reach the HiveMind HTTP endpoint.") from exc

        if getattr(response, "ok", False) is False:
            self._shutdown_client(client, transport_connected=False)
            detail = getattr(response, "text", "") or f"HTTP {getattr(response, 'status_code', 'error')}"
            raise ThalovantConnectionError(f"HiveMind HTTP connect failed: {detail}")

        client.connected.set()
        deadline = time.monotonic() + self.handshake_timeout
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            if client.handshake_event.wait(timeout=min(self.handshake_poll_interval, remaining)):
                time.sleep(self.handshake_settle_seconds)
                self._client = client
                self._transport_connected = True
                return

        self._shutdown_client(client, transport_connected=True)
        raise ThalovantTimeoutError("HiveMind HTTP handshake timed out.")

    def disconnect(self) -> None:
        client = self._client
        if client is None:
            return
        self._shutdown_client(client, transport_connected=self._transport_connected)
        self._client = None
        self._transport_connected = False

    def on_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self._require_client().on_mycroft(event_name, handler)

    def remove_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self._require_client().remove_mycroft(event_name, handler)

    def emit_event(
        self,
        event_type: str,
        data: dict[str, Any],
        context: dict[str, Any],
    ) -> Any:
        deps = self._load_deps()
        client = self._require_client()
        message = deps.Message(
            event_type,
            data,
            _runtime_bus_context(
                context,
                useragent=client.useragent,
                session_id=client.session_id,
                site_id=client.site_id,
            ),
        )
        return client.emit(message)

    def _require_client(self) -> Any:
        if self._client is None:
            raise ThalovantConnectionError("HiveMind HTTP transport is not connected.")
        return self._client

    def _load_deps(self) -> "_HiveMindDeps":
        if self._deps is not None:
            return self._deps
        try:
            import requests
            from hivemind_bus_client.http_client import HiveMindHTTPClient
            from hivemind_bus_client.message import HiveMessage, HiveMessageType
            from hivemind_bus_client.protocol import HiveMindSlaveProtocol
            from ovos_bus_client.message import Message
            from ovos_bus_client.session import Session
        except ImportError as exc:
            raise ThalovantConnectionError(
                "Install the SDK with HiveMind dependencies before connecting."
            ) from exc

        self._deps = _HiveMindDeps(
            HiveMindHTTPClient=HiveMindHTTPClient,
            HiveMessage=HiveMessage,
            HiveMessageType=HiveMessageType,
            HiveMindSlaveProtocol=HiveMindSlaveProtocol,
            Message=Message,
            Session=Session,
            requests=requests,
        )
        return self._deps

    def _build_protocol(self, client: Any, deps: "_HiveMindDeps") -> Any:
        crypto_key = _runtime_crypto_key(self.identity.crypto_key)

        if not crypto_key:
            return deps.HiveMindSlaveProtocol(
                client,
                shared_bus=client.share_bus,
                site_id=self.identity.site_id or "unknown",
                identity=client.identity,
            )

        class _ThalovantPresharedProtocol(deps.HiveMindSlaveProtocol):  # type: ignore[misc, valid-type]
            def handle_handshake(inner_self: Any, message: Any) -> None:
                payload = getattr(message, "payload", None)
                if (
                    isinstance(payload, dict)
                    and "envelope" not in payload
                    and payload.get("preshared_key")
                    and not payload.get("handshake")
                ):
                    inner_self.binarize = bool(payload.get("binarize", False))
                    session = deps.Session(inner_self.hm.session_id)
                    session.site_id = inner_self.site_id
                    inner_self.hm.emit(
                        deps.HiveMessage(
                            deps.HiveMessageType.HELLO,
                            {
                                "pubkey": inner_self.identity.public_key,
                                "session": session.serialize(),
                                "site_id": inner_self.site_id,
                            },
                        )
                    )
                    inner_self.hm.crypto_key = crypto_key
                    inner_self.hm.handshake_event.set()
                    return
                super().handle_handshake(message)

        return _ThalovantPresharedProtocol(
            client,
            shared_bus=client.share_bus,
            site_id=self.identity.site_id or "unknown",
            identity=client.identity,
        )

    def _shutdown_client(self, client: Any, *, transport_connected: bool) -> None:
        deps = self._load_deps()
        if transport_connected:
            try:
                deps.requests.post(
                    f"{client.base_url}/disconnect",
                    params={"authorization": client.auth},
                    timeout=self.connect_timeout,
                )
            except deps.requests.RequestException:
                pass
        try:
            client.connected.clear()
            client.handshake_event.clear()
        except Exception:
            pass
        try:
            client.connected.set()
            client.shutdown()
        except Exception:
            pass


@dataclass(frozen=True)
class _HiveMindDeps:
    HiveMindHTTPClient: Any
    HiveMessage: Any
    HiveMessageType: Any
    HiveMindSlaveProtocol: Any
    Message: Any
    Session: Any
    requests: Any


def _message_data(message: Any) -> dict[str, Any]:
    data = getattr(message, "data", None)
    return data if isinstance(data, dict) else {}


def _runtime_crypto_key(raw_crypto_key: str | None) -> str | None:
    if not isinstance(raw_crypto_key, str):
        return None
    normalized = raw_crypto_key.strip()
    if not normalized:
        return None
    return normalized[:16]


def _runtime_bus_context(
    raw_context: dict[str, Any] | None,
    *,
    useragent: str,
    session_id: str,
    site_id: str | None,
) -> dict[str, Any]:
    context = dict(raw_context or {})
    context.setdefault("source", useragent)
    context.setdefault("platform", useragent)
    context.setdefault("destination", "HiveMind")

    raw_session = context.get("session")
    session = dict(raw_session) if isinstance(raw_session, dict) else {}
    session["session_id"] = session_id
    session["site_id"] = site_id
    context["session"] = session
    return context
