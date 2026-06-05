"""High-level client for Thalovant HiveMind HTTP connections."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import queue
import threading
import time
from typing import Any, AsyncIterator, Callable, Iterator, Protocol

from .errors import (
    ThalovantConnectionError,
    ThalovantRuntimeError,
    ThalovantTimeoutError,
)
from .identity import ThalovantIdentity


DEFAULT_USERAGENT = "ThalovantPythonSDK/0.2.0"


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

    def healthcheck(self) -> "ThalovantHealth": ...

    def is_connected(self) -> bool: ...

    def last_error(self) -> BaseException | None: ...


EventHandler = Callable[["ThalovantEvent"], None]
EventPredicate = Callable[["ThalovantEvent"], bool]


@dataclass(frozen=True)
class ThalovantEvent:
    """A normalized event received from a hub."""

    name: str
    data: dict[str, Any]
    context: dict[str, Any]
    raw: Any


@dataclass(frozen=True)
class ThalovantHealth:
    """Snapshot of the SDK's live HiveMind HTTP transport state."""

    connected: bool
    handshake_complete: bool
    transport_alive: bool
    last_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.connected and self.handshake_complete and self.transport_alive and not self.last_error


@dataclass(frozen=True)
class ThalovantReply:
    """A normalized response from a hub utterance request."""

    text: str
    utterances: tuple[str, ...] = ()
    handled: bool = False
    raw_messages: tuple[Any, ...] = field(default_factory=tuple)
    events: tuple[ThalovantEvent, ...] = field(default_factory=tuple)
    failure_event: ThalovantEvent | None = None


class ThalovantSubscription:
    """Handle returned by `ThalovantClient.on`."""

    def __init__(
        self,
        client: "ThalovantClient",
        event_name: str,
        handler: Callable[[Any], None],
    ) -> None:
        self._client = client
        self.event_name = event_name
        self._handler = handler
        self._closed = False

    def __enter__(self) -> "ThalovantSubscription":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._client._remove_subscription(self.event_name, self._handler)
        self._closed = True

    unsubscribe = close


class ThalovantClient:
    """Developer-friendly wrapper around HiveMind's HTTP protocol client."""

    def __init__(
        self,
        identity: ThalovantIdentity,
        *,
        useragent: str = DEFAULT_USERAGENT,
        connect_timeout: float = 4.0,
        handshake_timeout: float = 6.0,
        send_timeout: float = 8.0,
        reply_settle_seconds: float = 0.25,
        auto_reconnect: bool = True,
        reconnect_attempts: int = 1,
        transport: _Transport | None = None,
    ) -> None:
        self.identity = identity
        self.useragent = useragent
        self.reply_settle_seconds = reply_settle_seconds
        self.auto_reconnect = auto_reconnect
        self.reconnect_attempts = max(0, reconnect_attempts)
        self._transport = transport or HiveMindHTTPTransport(
            identity,
            useragent=useragent,
            connect_timeout=connect_timeout,
            handshake_timeout=handshake_timeout,
            send_timeout=send_timeout,
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

        if self._connected and self._transport.is_connected():
            return
        if self._connected:
            self.close()
        self._transport.connect()
        self._connected = True

    def close(self) -> None:
        """Disconnect the underlying HiveMind HTTP client."""

        if not self._connected:
            return
        self._transport.disconnect()
        self._connected = False

    disconnect = close

    def healthcheck(self) -> ThalovantHealth:
        """Connect if needed and return the transport health snapshot."""

        self.connect()
        return self._transport.healthcheck()

    def on(self, event_name: str, handler: EventHandler) -> ThalovantSubscription:
        """Subscribe to a hub event and receive normalized `ThalovantEvent` objects."""

        self.connect()

        def wrapped(raw_message: Any) -> None:
            handler(_event_from_message(event_name, raw_message))

        self._transport.on_mycroft(event_name, wrapped)
        return ThalovantSubscription(self, event_name, wrapped)

    def wait_for_event(
        self,
        event_name: str,
        *,
        timeout: float = 12.0,
        predicate: EventPredicate | None = None,
    ) -> ThalovantEvent:
        """Wait for one matching hub event."""

        events: queue.Queue[ThalovantEvent] = queue.Queue()

        def handler(event: ThalovantEvent) -> None:
            if predicate is None or predicate(event):
                events.put(event)

        deadline = time.monotonic() + timeout
        with self.on(event_name, handler):
            while True:
                self._raise_if_transport_stopped()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ThalovantTimeoutError(
                        f"Hub did not emit {event_name!r} within {timeout:g}s."
                    )
                try:
                    return events.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    continue

    def listen(
        self,
        event_name: str,
        *,
        timeout: float | None = None,
        max_events: int | None = None,
    ) -> Iterator[ThalovantEvent]:
        """Yield hub events until `timeout` expires or `max_events` is reached."""

        events: queue.Queue[ThalovantEvent] = queue.Queue()
        deadline = None if timeout is None else time.monotonic() + timeout
        yielded = 0

        with self.on(event_name, events.put):
            while max_events is None or yielded < max_events:
                self._raise_if_transport_stopped()
                wait_time = 0.1
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    wait_time = min(wait_time, remaining)
                try:
                    event = events.get(timeout=wait_time)
                except queue.Empty:
                    continue
                yielded += 1
                yield event

    def emit(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Any:
        """Emit a raw OVOS/HiveMind bus event through the HTTP data plane."""

        return self._with_reconnect(
            lambda: self._transport.emit_event(event_type, data or {}, context or {})
        )

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

        last_error: BaseException | None = None
        attempts = self.reconnect_attempts + 1 if self.auto_reconnect else 1
        for attempt in range(attempts):
            try:
                return self._ask_once(prompt, timeout=timeout, lang=lang, context=context or {})
            except ThalovantConnectionError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                self.close()
        raise ThalovantConnectionError("HiveMind HTTP transport failed while waiting for reply.") from last_error

    def _ask_once(
        self,
        prompt: str,
        *,
        timeout: float,
        lang: str,
        context: dict[str, Any],
    ) -> ThalovantReply:
        self.connect()

        handled = threading.Event()
        failed = threading.Event()
        fragments: list[str] = []
        raw_messages: list[Any] = []
        events: list[ThalovantEvent] = []
        failure_event: ThalovantEvent | None = None

        def remember(event_name: str, message: Any) -> ThalovantEvent:
            event = _event_from_message(event_name, message)
            events.append(event)
            raw_messages.append(message)
            return event

        def handle_speak(message: Any) -> None:
            event = remember("speak", message)
            utterance = event.data.get("utterance")
            if isinstance(utterance, str) and utterance.strip():
                normalized = " ".join(utterance.strip().split())
                if not fragments or fragments[-1] != normalized:
                    fragments.append(normalized)

        def handle_handled(message: Any) -> None:
            remember("ovos.utterance.handled", message)
            handled.set()

        def handle_failure(message: Any) -> None:
            nonlocal failure_event
            failure_event = remember(_message_name(message) or "complete_intent_failure", message)
            failed.set()
            handled.set()

        handlers = (
            ("speak", handle_speak),
            ("ovos.utterance.handled", handle_handled),
            ("complete_intent_failure", handle_failure),
            ("hive.policy.denied", handle_failure),
        )

        for event_name, handler in handlers:
            self._transport.on_mycroft(event_name, handler)

        try:
            payload = {"utterances": [prompt], "lang": lang}
            self._transport.emit_event("recognizer_loop:utterance", payload, context)
            self._wait_for_handled(handled, timeout=timeout)
            if self.reply_settle_seconds > 0:
                time.sleep(self.reply_settle_seconds)
            if failed.is_set() and not fragments:
                reason = _failure_reason(failure_event)
                raise ThalovantRuntimeError(reason)
            return ThalovantReply(
                text=" ".join(fragments),
                utterances=tuple(fragments),
                handled=handled.is_set() and not failed.is_set(),
                raw_messages=tuple(raw_messages),
                events=tuple(events),
                failure_event=failure_event,
            )
        finally:
            for event_name, handler in handlers:
                try:
                    self._transport.remove_mycroft(event_name, handler)
                except ThalovantConnectionError:
                    pass

    def _wait_for_handled(self, handled: threading.Event, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not handled.is_set():
            self._raise_if_transport_stopped()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ThalovantTimeoutError(
                    f"Hub did not finish handling the utterance within {timeout:g}s."
                )
            handled.wait(timeout=min(0.1, remaining))

    def _with_reconnect(self, operation: Callable[[], Any]) -> Any:
        last_error: BaseException | None = None
        attempts = self.reconnect_attempts + 1 if self.auto_reconnect else 1
        for attempt in range(attempts):
            self.connect()
            try:
                return operation()
            except (ConnectionAbortedError, RuntimeError, ThalovantConnectionError) as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                self.close()
        raise ThalovantConnectionError("HiveMind HTTP transport failed after reconnect.") from last_error

    def _raise_if_transport_stopped(self) -> None:
        if self._transport.is_connected():
            return
        error = self._transport.last_error()
        detail = f": {error}" if error else ""
        raise ThalovantConnectionError(f"HiveMind HTTP transport stopped{detail}")

    def _remove_subscription(self, event_name: str, handler: Callable[[Any], None]) -> None:
        try:
            self._transport.remove_mycroft(event_name, handler)
        except ThalovantConnectionError:
            pass


class AsyncThalovantClient:
    """Async wrapper for web apps and long-running Python agents."""

    def __init__(self, identity: ThalovantIdentity, **kwargs: Any) -> None:
        self._client = ThalovantClient(identity, **kwargs)

    @classmethod
    def from_identity_file(
        cls,
        path: str | Path,
        **kwargs: Any,
    ) -> "AsyncThalovantClient":
        return cls(ThalovantIdentity.from_file(path), **kwargs)

    @classmethod
    def from_env(cls, **kwargs: Any) -> "AsyncThalovantClient":
        return cls(ThalovantIdentity.from_env(), **kwargs)

    async def __aenter__(self) -> "AsyncThalovantClient":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def connect(self) -> None:
        await asyncio.to_thread(self._client.connect)

    async def close(self) -> None:
        await asyncio.to_thread(self._client.close)

    disconnect = close

    async def healthcheck(self) -> ThalovantHealth:
        return await asyncio.to_thread(self._client.healthcheck)

    def on(self, event_name: str, handler: EventHandler) -> ThalovantSubscription:
        loop = asyncio.get_running_loop()

        def dispatch(event: ThalovantEvent) -> None:
            def run_handler() -> None:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)

            loop.call_soon_threadsafe(run_handler)

        return self._client.on(event_name, dispatch)

    async def listen(
        self,
        event_name: str,
        *,
        timeout: float | None = None,
        max_events: int | None = None,
    ) -> AsyncIterator[ThalovantEvent]:
        await self.connect()
        loop = asyncio.get_running_loop()
        events: asyncio.Queue[ThalovantEvent] = asyncio.Queue()
        deadline = None if timeout is None else loop.time() + timeout
        yielded = 0

        def handler(event: ThalovantEvent) -> None:
            loop.call_soon_threadsafe(events.put_nowait, event)

        subscription = self._client.on(event_name, handler)
        try:
            while max_events is None or yielded < max_events:
                wait_timeout = None
                if deadline is not None:
                    wait_timeout = deadline - loop.time()
                    if wait_timeout <= 0:
                        return
                try:
                    event = await asyncio.wait_for(events.get(), timeout=wait_timeout)
                except asyncio.TimeoutError:
                    return
                yielded += 1
                yield event
        finally:
            subscription.close()

    async def emit(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Any:
        return await asyncio.to_thread(self._client.emit, event_type, data, context)

    async def ask(
        self,
        text: str,
        *,
        timeout: float = 12.0,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
    ) -> ThalovantReply:
        return await asyncio.to_thread(
            self._client.ask,
            text,
            timeout=timeout,
            lang=lang,
            context=context,
        )

    async def wait_for_event(
        self,
        event_name: str,
        *,
        timeout: float = 12.0,
        predicate: EventPredicate | None = None,
    ) -> ThalovantEvent:
        return await asyncio.to_thread(
            self._client.wait_for_event,
            event_name,
            timeout=timeout,
            predicate=predicate,
        )


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
        send_timeout: float = 8.0,
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
        self.send_timeout = send_timeout
        self.self_signed = self_signed
        self.compress = compress
        self.binarize = binarize
        self._client: Any | None = None
        self._transport_connected = False
        self._deps: _HiveMindDeps | None = None
        self._last_error: BaseException | None = None

    def connect(self) -> None:
        if self.is_connected():
            return

        deps = self._load_deps()
        self._last_error = None
        http_client_class = self._build_http_client_class(deps.HiveMindHTTPClient)
        client = http_client_class(
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
        client = self._require_live_client()
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
        hive_message = deps.HiveMessage(deps.HiveMessageType.BUS, message)
        payload = deps.serialize_message(hive_message)
        if client.crypto_key:
            payload = deps.encrypt_as_json(
                client.crypto_key,
                payload,
                cipher=client.cipher,
                encoding=client.json_encoding,
            )

        try:
            response = deps.requests.post(
                f"{client.base_url}/send_message",
                data={"message": payload},
                params={"authorization": client.auth},
                timeout=self.send_timeout,
            )
        except deps.requests.RequestException as exc:
            self._last_error = exc
            raise ThalovantConnectionError("Could not send the HiveMind HTTP message.") from exc

        self._raise_for_emit_response(response)
        return response

    def _require_client(self) -> Any:
        if self._client is None:
            raise ThalovantConnectionError("HiveMind HTTP transport is not connected.")
        return self._client

    def _require_live_client(self) -> Any:
        client = self._require_client()
        if not self.is_connected():
            error = self.last_error()
            detail = f": {error}" if error else ""
            raise ThalovantConnectionError(f"HiveMind HTTP transport is not connected{detail}")
        return client

    def is_connected(self) -> bool:
        client = self._client
        if client is None or not self._transport_connected:
            return False
        try:
            return bool(
                client.connected.is_set()
                and client.handshake_event.is_set()
                and client.is_alive()
            )
        except Exception:
            return False

    def last_error(self) -> BaseException | None:
        client = self._client
        if client is not None:
            error = getattr(client, "thalovant_last_error", None)
            if isinstance(error, BaseException):
                return error
        return self._last_error

    def healthcheck(self) -> ThalovantHealth:
        client = self._client
        connected = False
        handshake_complete = False
        transport_alive = False
        if client is not None and self._transport_connected:
            try:
                connected = bool(client.connected.is_set())
                handshake_complete = bool(client.handshake_event.is_set())
                transport_alive = bool(client.is_alive())
            except Exception as exc:
                self._last_error = exc
        error = self.last_error()
        return ThalovantHealth(
            connected=connected,
            handshake_complete=handshake_complete,
            transport_alive=transport_alive,
            last_error=str(error) if error else None,
        )

    def _load_deps(self) -> "_HiveMindDeps":
        if self._deps is not None:
            return self._deps
        try:
            import requests
            from hivemind_bus_client.http_client import HiveMindHTTPClient
            from hivemind_bus_client.encryption import encrypt_as_json
            from hivemind_bus_client.message import HiveMessage, HiveMessageType
            from hivemind_bus_client.protocol import HiveMindSlaveProtocol
            from hivemind_bus_client.util import serialize_message
            from ovos_bus_client.message import Message
            from ovos_bus_client.session import Session
        except ImportError as exc:
            raise ThalovantConnectionError(
                "Install the SDK with HiveMind dependencies before connecting."
            ) from exc

        self._deps = _HiveMindDeps(
            HiveMindHTTPClient=HiveMindHTTPClient,
            encrypt_as_json=encrypt_as_json,
            HiveMessage=HiveMessage,
            HiveMessageType=HiveMessageType,
            HiveMindSlaveProtocol=HiveMindSlaveProtocol,
            Message=Message,
            Session=Session,
            serialize_message=serialize_message,
            requests=requests,
        )
        return self._deps

    def _build_http_client_class(self, base_class: Any) -> Any:
        transport = self

        class _ObservedHiveMindHTTPClient(base_class):  # type: ignore[misc, valid-type]
            thalovant_last_error: BaseException | None = None

            def run(inner_self: Any) -> None:
                try:
                    super().run()
                except Exception as exc:
                    inner_self.thalovant_last_error = exc
                    transport._last_error = exc
                    try:
                        inner_self.connected.clear()
                    except Exception:
                        pass

        return _ObservedHiveMindHTTPClient

    def _raise_for_emit_response(self, response: Any) -> None:
        status_code = getattr(response, "status_code", None)
        try:
            body = response.json()
        except Exception:
            body = {}

        error = body.get("error") if isinstance(body, dict) else None
        if error:
            if "not connected" in str(error).lower():
                exc = ThalovantConnectionError(f"HiveMind HTTP send failed: {error}")
                self._last_error = exc
                raise exc
            raise ThalovantRuntimeError(f"HiveMind HTTP send failed: {error}")

        if getattr(response, "ok", False) is False:
            detail = getattr(response, "text", "") or f"HTTP {status_code or 'error'}"
            raise ThalovantRuntimeError(f"HiveMind HTTP send failed: {detail}")

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
    encrypt_as_json: Any
    HiveMessage: Any
    HiveMessageType: Any
    HiveMindSlaveProtocol: Any
    Message: Any
    Session: Any
    serialize_message: Any
    requests: Any


def _message_data(message: Any) -> dict[str, Any]:
    data = getattr(message, "data", None)
    return data if isinstance(data, dict) else {}


def _message_context(message: Any) -> dict[str, Any]:
    context = getattr(message, "context", None)
    return context if isinstance(context, dict) else {}


def _message_name(message: Any) -> str | None:
    name = getattr(message, "msg_type", None)
    return str(name) if name is not None else None


def _event_from_message(event_name: str, message: Any) -> ThalovantEvent:
    return ThalovantEvent(
        name=_message_name(message) or event_name,
        data=_message_data(message),
        context=_message_context(message),
        raw=message,
    )


def _failure_reason(event: ThalovantEvent | None) -> str:
    if event is None:
        return "Hub reported that the utterance could not be handled."
    reason = event.data.get("reason") or event.data.get("error") or event.data.get("code")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return f"Hub reported {event.name}."


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
