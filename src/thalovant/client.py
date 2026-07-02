"""High-level sync and async clients."""

from __future__ import annotations

import asyncio
from pathlib import Path
import queue
import threading
import time
from typing import Any, AsyncIterator, Callable, Iterator
from urllib.parse import urlparse

from .conversation import AsyncThalovantConversation, ThalovantConversation
from .errors import (
    ThalovantUnsupportedProtocolError,
    ThalovantConnectionError,
    ThalovantRuntimeError,
    ThalovantTimeoutError,
)
from .events import (
    EVENT_INTENT_FAILURE,
    EVENT_POLICY_DENIED,
    EVENT_RECOGNIZER_LOOP_UTTERANCE,
    EVENT_SPEAK,
    EVENT_UTTERANCE_HANDLED,
    EventHandler,
    EventPredicate,
    ThalovantEvent,
    _context_with_correlation,
    _event_from_message,
    _event_matches_context,
    _failure_reason,
    _merge_context,
    _new_request_id,
    _runtime_bus_context,
    _runtime_crypto_key,
    _session_id_from_context,
    _utterance_payload,
)
from .identity import ThalovantIdentity
from .models import (
    ThalovantDoctorCheck,
    ThalovantDoctorReport,
    ThalovantHealth,
    ThalovantReply,
)
from .subscriptions import ThalovantSubscription
from .transport import HiveMindHTTPTransport, HiveMindMQTTTransport, HiveMindWSSTransport, Transport
from .protocols import DEFAULT_PROTOCOL_PREFERENCE, HubProtocol


DEFAULT_USERAGENT = "ThalovantPythonSDK/0.4.17"


def _default_runtime_protocol(identity: ThalovantIdentity) -> HubProtocol:
    for protocol in DEFAULT_PROTOCOL_PREFERENCE:
        if protocol == "wss":
            if identity.supports_protocol("wss") and identity.endpoint_for("wss"):
                return "wss"
            continue
        if protocol == "https":
            if identity.supports_protocol("https") or identity.endpoint_for("https"):
                return "https"
            continue
        if protocol == "mqtt" and identity.supports_protocol("mqtt") and identity.mqtt:
            return "mqtt"
    raise ThalovantUnsupportedProtocolError(
        "The identity does not include a usable WSS, HTTPS, or MQTT endpoint."
    )


def _transport_for_protocol(
    identity: ThalovantIdentity,
    *,
    protocol: HubProtocol,
    useragent: str,
    connect_timeout: float,
    handshake_timeout: float,
    send_timeout: float,
) -> Transport:
    kwargs = {
        "useragent": useragent,
        "connect_timeout": connect_timeout,
        "handshake_timeout": handshake_timeout,
        "send_timeout": send_timeout,
    }
    if protocol == "https":
        return HiveMindHTTPTransport(identity, **kwargs)
    if protocol == "wss":
        endpoint = identity.endpoint_for("wss")
        if not endpoint:
            raise ThalovantUnsupportedProtocolError(
                "WSS is enabled, but the identity does not include a WSS endpoint."
            )
        return HiveMindWSSTransport(identity, **kwargs)
    if protocol == "mqtt":
        if identity.mqtt is None:
            raise ThalovantUnsupportedProtocolError(
                "MQTT is enabled, but the identity does not include MQTT broker credentials."
            )
        return HiveMindMQTTTransport(identity, **kwargs)
    raise ThalovantUnsupportedProtocolError(f"Unsupported protocol: {protocol}")


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
        protocol: HubProtocol | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.identity = identity
        self.useragent = useragent
        self.reply_settle_seconds = reply_settle_seconds
        self.auto_reconnect = auto_reconnect
        self.reconnect_attempts = max(0, reconnect_attempts)
        self._transport = transport or _transport_for_protocol(
            identity,
            protocol=protocol or _default_runtime_protocol(identity),
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

    @classmethod
    def from_config(
        cls,
        path: str | Path | None = None,
        *,
        profile: str | None = None,
        **kwargs: Any,
    ) -> "ThalovantClient":
        """Create a client from the per-user Thalovant YAML config."""

        return cls(ThalovantIdentity.from_config(path, profile=profile), **kwargs)

    def __enter__(self) -> "ThalovantClient":
        self.connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def conversation(
        self,
        *,
        session_id: str | None = None,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
    ) -> ThalovantConversation:
        """Create a scoped conversation with a stable session id."""

        return ThalovantConversation(
            self,
            session_id=session_id,
            lang=lang,
            context=context,
        )

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

    def doctor(self) -> ThalovantDoctorReport:
        """Run identity, endpoint, connection, and transport diagnostics."""

        checks: list[ThalovantDoctorCheck] = []

        def check(name: str, operation: Callable[[], str]) -> None:
            started = time.monotonic()
            try:
                detail = operation()
                ok = True
            except Exception as exc:
                detail = str(exc)
                ok = False
            checks.append(
                ThalovantDoctorCheck(
                    name=name,
                    ok=ok,
                    detail=detail,
                    duration_ms=(time.monotonic() - started) * 1000,
                )
            )

        check("identity", self._doctor_identity)
        check("endpoint", self._doctor_endpoint)
        check("connect", self._doctor_connect)
        check("transport", self._doctor_transport)
        return ThalovantDoctorReport(
            identity=self.identity.as_dict(include_secrets=False),
            checks=tuple(checks),
        )

    def on(
        self,
        event_name: str,
        handler: EventHandler,
        *,
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        predicate: EventPredicate | None = None,
    ) -> ThalovantSubscription:
        """Subscribe to a hub event and receive normalized `ThalovantEvent` objects."""

        self.connect()
        expected_context = _context_with_correlation(
            context,
            session_id=session_id,
            request_id=request_id,
        )

        def wrapped(raw_message: Any) -> None:
            event = _event_from_message(event_name, raw_message)
            if not _event_matches_context(event, expected_context):
                return
            if predicate is not None and not predicate(event):
                return
            handler(event)

        self._transport.on_mycroft(event_name, wrapped)
        return ThalovantSubscription(self, event_name, wrapped)

    def wait_for_event(
        self,
        event_name: str,
        *,
        timeout: float = 12.0,
        predicate: EventPredicate | None = None,
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> ThalovantEvent:
        """Wait for one matching hub event."""

        events: queue.Queue[ThalovantEvent] = queue.Queue()
        deadline = time.monotonic() + timeout
        with self.on(
            event_name,
            events.put,
            context=context,
            session_id=session_id,
            request_id=request_id,
            predicate=predicate,
        ):
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
        predicate: EventPredicate | None = None,
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> Iterator[ThalovantEvent]:
        """Yield hub events until `timeout` expires or `max_events` is reached."""

        events: queue.Queue[ThalovantEvent] = queue.Queue()
        deadline = None if timeout is None else time.monotonic() + timeout
        yielded = 0

        with self.on(
            event_name,
            events.put,
            context=context,
            session_id=session_id,
            request_id=request_id,
            predicate=predicate,
        ):
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
            lambda: self._transport.emit_event(
                event_type,
                data or {},
                self._context_with_identity_metadata(context),
            )
        )

    def send_utterance(
        self,
        text: str,
        *,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> Any:
        """Emit a text utterance without waiting for a spoken reply."""

        prompt = text.strip()
        if not prompt:
            raise ValueError("send_utterance() requires a non-empty text prompt.")

        request_context = _context_with_correlation(
            self._context_with_identity_metadata(context),
            session_id=session_id,
            site_id=self.identity.site_id,
            lang=lang,
            request_id=request_id or _new_request_id(),
        )
        return self.emit(
            EVENT_RECOGNIZER_LOOP_UTTERANCE,
            _utterance_payload(prompt, lang),
            request_context,
        )

    def send_action(
        self,
        payload: str,
        *,
        title: str | None = None,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> Any:
        """Emit a selected action or quick-reply payload as an utterance."""

        prompt = payload.strip()
        if not prompt:
            raise ValueError("send_action() requires a non-empty payload.")
        action_context = _merge_context(
            context,
            {"input": {"kind": "action", "title": title, "payload": prompt}},
        )
        return self.send_utterance(
            prompt,
            lang=lang,
            context=action_context,
            session_id=session_id,
            request_id=request_id,
        )

    def send_code(
        self,
        value: str,
        *,
        kind: str = "code",
        label: str | None = None,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> Any:
        """Emit an exact scanned/typed code value without speech transcription loss."""

        code = value.strip()
        if not code:
            raise ValueError("send_code() requires a non-empty value.")
        request_id = request_id or _new_request_id()
        request_context = _context_with_correlation(
            _merge_context(
                self._context_with_identity_metadata(context),
                {"input": {"kind": kind, "label": label, "value": code, "exact": True}},
            ),
            session_id=session_id,
            site_id=self.identity.site_id,
            lang=lang,
            request_id=request_id,
        )
        data = _utterance_payload(code, lang)
        data["input"] = {"kind": kind, "label": label, "value": code, "exact": True}
        return self.emit(EVENT_RECOGNIZER_LOOP_UTTERANCE, data, request_context)

    def ask(
        self,
        text: str,
        *,
        timeout: float = 12.0,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> ThalovantReply:
        """Send a text utterance and wait for the hub's spoken reply."""

        prompt = text.strip()
        if not prompt:
            raise ValueError("ask() requires a non-empty text prompt.")

        request_id = request_id or _new_request_id()
        request_context = _context_with_correlation(
            self._context_with_identity_metadata(context),
            session_id=session_id,
            site_id=self.identity.site_id,
            lang=lang,
            request_id=request_id,
        )
        last_error: BaseException | None = None
        attempts = self.reconnect_attempts + 1 if self.auto_reconnect else 1
        for attempt in range(attempts):
            try:
                return self._ask_once(
                    prompt,
                    timeout=timeout,
                    lang=lang,
                    context=request_context,
                    request_id=request_id,
                    session_id=_session_id_from_context(request_context),
                )
            except ThalovantConnectionError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                self.close()
        raise ThalovantConnectionError(
            "HiveMind transport failed while waiting for reply."
        ) from last_error

    def _ask_once(
        self,
        prompt: str,
        *,
        timeout: float,
        lang: str,
        context: dict[str, Any],
        request_id: str | None,
        session_id: str | None,
    ) -> ThalovantReply:
        self.connect()

        handled = threading.Event()
        failed = threading.Event()
        fragments: list[str] = []
        raw_messages: list[Any] = []
        events: list[ThalovantEvent] = []
        failure_event: ThalovantEvent | None = None

        def remember(event_name: str, message: Any) -> ThalovantEvent | None:
            event = _event_from_message(event_name, message)
            if not _event_matches_context(event, context):
                return None
            events.append(event)
            raw_messages.append(message)
            return event

        def handle_speak(message: Any) -> None:
            event = remember(EVENT_SPEAK, message)
            if event is None:
                return
            utterance = event.data.get("utterance")
            if isinstance(utterance, str) and utterance.strip():
                normalized = " ".join(utterance.strip().split())
                if not fragments or fragments[-1] != normalized:
                    fragments.append(normalized)

        def handle_handled(message: Any) -> None:
            if remember(EVENT_UTTERANCE_HANDLED, message) is not None:
                handled.set()

        def handle_failure(message: Any) -> None:
            nonlocal failure_event
            event = remember(
                getattr(message, "msg_type", None) or EVENT_INTENT_FAILURE, message
            )
            if event is None:
                return
            failure_event = event
            failed.set()
            handled.set()

        handlers = (
            (EVENT_SPEAK, handle_speak),
            (EVENT_UTTERANCE_HANDLED, handle_handled),
            (EVENT_INTENT_FAILURE, handle_failure),
            (EVENT_POLICY_DENIED, handle_failure),
        )

        for event_name, handler in handlers:
            self._transport.on_mycroft(event_name, handler)

        try:
            self._transport.emit_event(
                EVENT_RECOGNIZER_LOOP_UTTERANCE,
                _utterance_payload(prompt, lang),
                context,
            )
            self._wait_for_handled(handled, timeout=timeout)
            if self.reply_settle_seconds > 0:
                time.sleep(self.reply_settle_seconds)
            if failed.is_set() and not fragments:
                raise ThalovantRuntimeError(_failure_reason(failure_event))
            return ThalovantReply(
                text=" ".join(fragments),
                utterances=tuple(fragments),
                handled=handled.is_set() and not failed.is_set(),
                session_id=session_id,
                request_id=request_id,
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
            except (
                ConnectionAbortedError,
                RuntimeError,
                ThalovantConnectionError,
            ) as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                self.close()
        raise ThalovantConnectionError(
            "HiveMind transport failed after reconnect."
        ) from last_error

    def _raise_if_transport_stopped(self) -> None:
        if self._transport.is_connected():
            return
        error = self._transport.last_error()
        detail = f": {error}" if error else ""
        raise ThalovantConnectionError(f"HiveMind transport stopped{detail}")

    def _context_with_identity_metadata(
        self,
        context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        merged = dict(context or {})
        if self.identity.metadata:
            merged["metadata"] = {
                **dict(self.identity.metadata),
                **dict(merged.get("metadata") or {}),
            }
        return merged

    def _remove_subscription(
        self, event_name: str, handler: Callable[[Any], None]
    ) -> None:
        try:
            self._transport.remove_mycroft(event_name, handler)
        except ThalovantConnectionError:
            pass

    def _doctor_identity(self) -> str:
        self.identity.as_dict(include_secrets=False)
        return f"site_id={self.identity.site_id}"

    def _doctor_endpoint(self) -> str:
        parsed = urlparse(self.identity.default_master)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("default_master must start with http:// or https://")
        if not parsed.netloc:
            raise ValueError("default_master must include a host")
        if self.identity.default_port <= 0:
            raise ValueError("default_port must be positive")
        return self.identity.endpoint_base()

    def _doctor_connect(self) -> str:
        self.connect()
        return "connected and handshake completed"

    def _doctor_transport(self) -> str:
        health = self.healthcheck()
        if not health.ok:
            raise ThalovantConnectionError(str(health.as_dict()))
        return "polling thread alive"


class AsyncThalovantClient:
    """Async wrapper for web apps and long-running Python agents."""

    def __init__(self, identity: ThalovantIdentity, **kwargs: Any) -> None:
        self._client = ThalovantClient(identity, **kwargs)

    @property
    def identity(self) -> ThalovantIdentity:
        return self._client.identity

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

    @classmethod
    def from_config(
        cls,
        path: str | Path | None = None,
        *,
        profile: str | None = None,
        **kwargs: Any,
    ) -> "AsyncThalovantClient":
        return cls(ThalovantIdentity.from_config(path, profile=profile), **kwargs)

    async def __aenter__(self) -> "AsyncThalovantClient":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    def conversation(
        self,
        *,
        session_id: str | None = None,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
    ) -> AsyncThalovantConversation:
        return AsyncThalovantConversation(
            self,
            session_id=session_id,
            lang=lang,
            context=context,
        )

    async def connect(self) -> None:
        await asyncio.to_thread(self._client.connect)

    async def close(self) -> None:
        await asyncio.to_thread(self._client.close)

    disconnect = close

    async def healthcheck(self) -> ThalovantHealth:
        return await asyncio.to_thread(self._client.healthcheck)

    async def doctor(self) -> ThalovantDoctorReport:
        return await asyncio.to_thread(self._client.doctor)

    def on(
        self,
        event_name: str,
        handler: EventHandler,
        *,
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        predicate: EventPredicate | None = None,
    ) -> ThalovantSubscription:
        loop = asyncio.get_running_loop()

        def dispatch(event: ThalovantEvent) -> None:
            def run_handler() -> None:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)

            loop.call_soon_threadsafe(run_handler)

        return self._client.on(
            event_name,
            dispatch,
            context=context,
            session_id=session_id,
            request_id=request_id,
            predicate=predicate,
        )

    async def listen(
        self,
        event_name: str,
        *,
        timeout: float | None = None,
        max_events: int | None = None,
        predicate: EventPredicate | None = None,
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> AsyncIterator[ThalovantEvent]:
        await self.connect()
        loop = asyncio.get_running_loop()
        events: asyncio.Queue[ThalovantEvent] = asyncio.Queue()
        deadline = None if timeout is None else loop.time() + timeout
        yielded = 0

        def handler(event: ThalovantEvent) -> None:
            loop.call_soon_threadsafe(events.put_nowait, event)

        subscription = self._client.on(
            event_name,
            handler,
            context=context,
            session_id=session_id,
            request_id=request_id,
            predicate=predicate,
        )
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

    async def send_utterance(
        self,
        text: str,
        *,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> Any:
        return await asyncio.to_thread(
            self._client.send_utterance,
            text,
            lang=lang,
            context=context,
            session_id=session_id,
            request_id=request_id,
        )

    async def send_action(
        self,
        payload: str,
        *,
        title: str | None = None,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> Any:
        return await asyncio.to_thread(
            self._client.send_action,
            payload,
            title=title,
            lang=lang,
            context=context,
            session_id=session_id,
            request_id=request_id,
        )

    async def send_code(
        self,
        value: str,
        *,
        kind: str = "code",
        label: str | None = None,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> Any:
        return await asyncio.to_thread(
            self._client.send_code,
            value,
            kind=kind,
            label=label,
            lang=lang,
            context=context,
            session_id=session_id,
            request_id=request_id,
        )

    async def ask(
        self,
        text: str,
        *,
        timeout: float = 12.0,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> ThalovantReply:
        return await asyncio.to_thread(
            self._client.ask,
            text,
            timeout=timeout,
            lang=lang,
            context=context,
            session_id=session_id,
            request_id=request_id,
        )

    async def wait_for_event(
        self,
        event_name: str,
        *,
        timeout: float = 12.0,
        predicate: EventPredicate | None = None,
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> ThalovantEvent:
        return await asyncio.to_thread(
            self._client.wait_for_event,
            event_name,
            timeout=timeout,
            predicate=predicate,
            context=context,
            session_id=session_id,
            request_id=request_id,
        )
