"""High-level sync and async clients."""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
import queue
import threading
import time
from typing import Any, AsyncIterator, Callable, Iterable, Iterator
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
    EVENT_INTENT_UNMATCHED,
    EVENT_OVOS_UTTERANCE_SPEAK,
    EVENT_POLICY_DENIED,
    EVENT_QUERY_TIMEOUT,
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
    _new_session_id,
    _runtime_bus_context,
    _session_id_from_context,
    _utterance_payload,
)
from .identity import ThalovantIdentity
from .intents import HubIntentInventory, IntentDefinition, IntentRegistration
from .models import (
    ThalovantConnectionInfo,
    ThalovantDoctorCheck,
    ThalovantDoctorReport,
    ThalovantHealth,
    ThalovantReply,
)
from .subscriptions import ThalovantSubscription
from .transport import (
    HiveMindHTTPTransport,
    HiveMindMQTTTransport,
    HiveMindWSSTransport,
    Transport,
    _redact_error_text,
)
from .protocols import DEFAULT_PROTOCOL_PREFERENCE, HubProtocol
from ._version import USER_AGENT


DEFAULT_USERAGENT = USER_AGENT


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
    noise_state_dir: str | None = None,
) -> Transport:
    kwargs = {
        "useragent": useragent,
        "connect_timeout": connect_timeout,
        "handshake_timeout": handshake_timeout,
        "send_timeout": send_timeout,
        "noise_state_dir": noise_state_dir,
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


# Poll admitted readiness without extending the caller's connect deadline.
_SETTLE_POLL = 0.02


def _message_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _message_field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _query_id_from_hive_message(message: Any) -> str | None:
    metadata = _message_mapping(_message_field(message, "metadata"))
    query_id = metadata.get("query_id") or metadata.get("queryId")
    return str(query_id) if query_id is not None else None


def _event_from_query_hive_message(message: Any) -> ThalovantEvent | None:
    payload = _message_field(message, "payload")
    bus_payload = _bus_payload_from_hive_payload(payload)
    if bus_payload is None:
        return None
    return ThalovantEvent(
        name=str(bus_payload.get("type") or ""),
        data=_message_mapping(bus_payload.get("data")),
        context=_message_mapping(bus_payload.get("context")),
        raw=message,
    )


def _bus_payload_from_hive_payload(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        if isinstance(payload.get("type"), str):
            return {
                "type": payload["type"],
                "data": _message_mapping(payload.get("data")),
                "context": _message_mapping(payload.get("context")),
            }
        if "payload" in payload:
            return _bus_payload_from_hive_payload(payload.get("payload"))

    msg_type = _message_field(payload, "msg_type")
    if msg_type == "bus":
        return _bus_payload_from_hive_payload(_message_field(payload, "payload"))
    if isinstance(msg_type, str):
        return {
            "type": msg_type,
            "data": _message_mapping(_message_field(payload, "data")),
            "context": _message_mapping(_message_field(payload, "context")),
        }
    return None


class ThalovantClient:
    """Developer-friendly wrapper around HiveMind's HTTP protocol client."""

    def __init__(
        self,
        identity: ThalovantIdentity,
        *,
        useragent: str = DEFAULT_USERAGENT,
        connect_timeout: float = 4.0,
        handshake_timeout: float = 20.0,
        send_timeout: float = 8.0,
        reply_settle_seconds: float = 0.25,
        empty_reply_wait_seconds: float = 5.0,
        auto_reconnect: bool = True,
        reconnect_attempts: int = 1,
        protocol: HubProtocol | None = None,
        transport: Transport | None = None,
        noise_state_dir: str | None = None,
    ) -> None:
        self.identity = identity
        self.useragent = useragent
        if any(not math.isfinite(value) or value < 0 for value in (
            reply_settle_seconds, empty_reply_wait_seconds,
        )):
            raise ValueError("Reply settlement windows must be finite and non-negative.")
        self.reply_settle_seconds = reply_settle_seconds
        self.empty_reply_wait_seconds = empty_reply_wait_seconds
        self._hard_connect_timeout = max(0.1, connect_timeout + handshake_timeout + 1.0)
        self.auto_reconnect = auto_reconnect
        self.reconnect_attempts = max(0, reconnect_attempts)
        self._transport = transport or _transport_for_protocol(
            identity,
            protocol=protocol or _default_runtime_protocol(identity),
            useragent=useragent,
            connect_timeout=connect_timeout,
            handshake_timeout=handshake_timeout,
            send_timeout=send_timeout,
            noise_state_dir=noise_state_dir,
        )
        self._connected = False
        self._connection_lock = threading.Lock()
        self._connection_state = threading.RLock()
        self._connection_generation = 0
        self._closing = 0
        self._closed_event = threading.Event()
        self._closed_event.set()
        self._close_errors: list[BaseException] = []
        self._automatic_cleanups = 0
        self._cancel_connect: Callable[[BaseException], None] | None = None

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

    def connect(self, timeout: float | None = None) -> None:
        """Reach authenticated readiness within one caller deadline.

        Timed-out work retains the lifecycle lock until its connect and cleanup
        finish, so a later attempt cannot replace or be closed by that session.
        """
        self._connect(timeout)

    def _connect(
        self, timeout: float | None = None, cancellation: threading.Event | None = None,
        operation: Callable[[], Any] | None = None,
    ) -> None:
        budget = self._hard_connect_timeout if timeout is None else timeout

        def timeout_error() -> ThalovantConnectionError | ThalovantTimeoutError:
            if operation is not None:
                return ThalovantTimeoutError(f"Hub query send did not complete within {budget:g}s.")
            error = ThalovantConnectionError(f"Hub connection did not complete within {budget:g}s.")
            error.__cause__ = ThalovantTimeoutError("Hub connection deadline expired.")
            return error

        if not math.isfinite(budget) or budget <= 0:
            raise timeout_error()
        deadline = time.monotonic() + budget

        with self._connection_state:
            if self._closing:
                raise ThalovantConnectionError("Hub connection is closing.")
            if self._close_errors:
                raise ThalovantConnectionError("Previous connection cleanup failed; retry close before reconnecting.") from None
            generation = self._connection_generation
            if operation is None and not self._connection_lock.locked() and self._connected and self._transport.is_connected():
                return
        while True:
            if cancellation is not None and cancellation.is_set():
                raise ThalovantConnectionError("Hub connection was cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise timeout_error()
            if self._connection_lock.acquire(timeout=min(_SETTLE_POLL, remaining)):
                break
        with self._connection_state:
            if time.monotonic() >= deadline:
                self._connection_lock.release()
                raise timeout_error()
            if cancellation is not None and cancellation.is_set():
                self._connection_lock.release()
                raise ThalovantConnectionError("Hub connection was cancelled.")
            if self._closing or generation != self._connection_generation:
                self._connection_lock.release()
                raise ThalovantConnectionError("Hub connection was closed before it became ready.")
            if self._close_errors:
                self._connection_lock.release()
                raise ThalovantConnectionError("Previous connection cleanup failed; retry close before reconnecting.") from None
            reuse_connection = self._connected and self._transport.is_connected()
            if operation is None and reuse_connection:
                self._connection_lock.release()
                return
            if not reuse_connection:
                self._connected = False
            done = threading.Event()
            cancelled = threading.Event()
            errors: list[BaseException] = []
            cleanup: threading.Thread | None = None

            def disconnect() -> None:
                try:
                    retire = getattr(self._transport, "_retire_connection", self._transport.disconnect)
                    retire()
                except BaseException as error:
                    with self._connection_state:
                        self._close_errors.append(error)

            def cancel(error: BaseException) -> None:
                nonlocal cleanup
                with self._connection_state:
                    if done.is_set():
                        return
                    errors.append(error)
                    cancelled.set()
                    self._connected = False
                    self._automatic_cleanups += 1
                    self._closed_event.clear()
                    cleanup = threading.Thread(target=disconnect, daemon=True)
                    cleanup.start()
                    done.set()

            self._cancel_connect = cancel

            def run_connect() -> None:
                transport_completed = reuse_connection
                try:
                    if not reuse_connection:
                        self._transport.connect()
                        transport_completed = True
                    while not cancelled.is_set():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            cancel(timeout_error())
                            break
                        if self._transport.is_connected():
                            with self._connection_state:
                                if time.monotonic() >= deadline:
                                    cancel(timeout_error())
                                elif not cancelled.is_set():
                                    self._connected = True
                            break
                        cancelled.wait(min(_SETTLE_POLL, remaining))
                    if not cancelled.is_set():
                        if operation is not None:
                            operation()
                        with self._connection_state:
                            if time.monotonic() >= deadline:
                                cancel(timeout_error())
                            elif not cancelled.is_set():
                                done.set()
                except BaseException as exc:
                    cancel(exc)
                finally:
                    with self._connection_state:
                        owned_cleanup = cleanup
                    if owned_cleanup is not None:
                        owned_cleanup.join()
                        if transport_completed:
                            # A custom transport may finish after its first
                            # disconnect. Retire that late completion too.
                            disconnect()
                    with self._connection_state:
                        if self._cancel_connect is cancel:
                            self._cancel_connect = None
                        if owned_cleanup is not None:
                            self._automatic_cleanups -= 1
                            if not self._automatic_cleanups and not self._closing:
                                self._closed_event.set()
                    self._connection_lock.release()

            threading.Thread(target=run_connect, daemon=True).start()
        while not done.is_set():
            if cancellation is not None and cancellation.is_set():
                cancel(ThalovantConnectionError("Hub connection was cancelled."))
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                cancel(timeout_error())
                break
            done.wait(timeout=min(_SETTLE_POLL, remaining))
        if errors:
            raise errors[0]

    def connect_with_info(self, timeout: float | None = None) -> ThalovantConnectionInfo:
        """Connect and return the transport timing snapshot."""

        self.connect(timeout=timeout)
        return self.connection_info()

    def connection_info(self) -> ThalovantConnectionInfo:
        """Return connection timing for the current or most recent transport."""

        return self._transport.connection_info()

    def close(self, timeout: float | None = None) -> None:
        """Cancel pending work and close within a caller budget.

        A timeout retains cleanup ownership. Use ``wait_closed`` to observe
        actual completion before handing this identity to another client.
        """
        budget = self._hard_connect_timeout if timeout is None else timeout
        if not math.isfinite(budget) or budget <= 0:
            raise ThalovantConnectionError("Hub close deadline expired.") from ThalovantTimeoutError(
                "Hub close requires a positive finite timeout."
            )
        completed = threading.Event()
        errors: list[BaseException] = []
        with self._connection_state:
            self._connection_generation += 1
            self._closing += 1
            self._closed_event.clear()
            self._connected = False
            if self._cancel_connect is not None:
                self._cancel_connect(ThalovantConnectionError("Hub connection was closed before it became ready."))

        def run_close() -> None:
            try:
                with self._connection_lock:
                    self._transport.disconnect()
                    with self._connection_state:
                        self._close_errors.clear()
            except BaseException as exc:
                errors.append(exc)
                with self._connection_state:
                    self._close_errors.append(exc)
            finally:
                with self._connection_state:
                    self._closing -= 1
                    if not self._closing and not self._automatic_cleanups:
                        self._closed_event.set()
                completed.set()

        threading.Thread(target=run_close, daemon=True).start()
        if not completed.wait(budget):
            raise ThalovantConnectionError(f"Hub close did not complete within {budget:g}s.")
        if errors:
            raise errors[0]

    def wait_closed(self, timeout: float | None = None) -> None:
        """Wait for actual pending cleanup; no timeout means wait until retired."""
        if not self._closed_event.wait(timeout):
            raise ThalovantConnectionError("Hub cleanup is still pending.")
        with self._connection_state:
            if self._close_errors:
                raise self._close_errors[0]

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
                # doctor output is printed by the CLI; scrub any URL query
                # (which carries the data-plane access key) from the message.
                detail = _redact_error_text(exc)
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
        self, event_name: str, *, timeout: float = 12.0,
        predicate: EventPredicate | None = None,
        context: dict[str, Any] | None = None,
        session_id: str | None = None, request_id: str | None = None,
    ) -> ThalovantEvent:
        """Wait for one matching event within a connect/registration/wait budget."""
        return self._wait_for_event(
            event_name, timeout=timeout, predicate=predicate, context=context,
            session_id=session_id, request_id=request_id,
        )

    def _wait_for_event(self, event_name: str, **kwargs: Any) -> ThalovantEvent:
        stream = self._listen(event_name, max_events=1, max_buffered_events=1, **kwargs)
        try:
            return next(stream)
        except StopIteration:
            raise ThalovantTimeoutError(f"Hub did not emit {event_name!r} within the caller deadline.") from None
        finally:
            stream.close()

    def listen(
        self, event_name: str, *, timeout: float | None = None,
        max_events: int | None = None, max_buffered_events: int = 256,
        predicate: EventPredicate | None = None,
        context: dict[str, Any] | None = None,
        session_id: str | None = None, request_id: str | None = None,
    ) -> Iterator[ThalovantEvent]:
        """Yield bounded buffered events; overflow raises ThalovantRuntimeError.

        A supplied timeout includes connection and subscription setup. With no
        timeout, setup uses the normal connect budget and listening is unlimited.
        """
        yield from self._listen(
            event_name, timeout=timeout, max_events=max_events,
            max_buffered_events=max_buffered_events, predicate=predicate,
            context=context, session_id=session_id, request_id=request_id,
        )

    def _listen(
        self, event_name: str, *, timeout: float | None = None,
        max_events: int | None = None, max_buffered_events: int = 256,
        predicate: EventPredicate | None = None,
        context: dict[str, Any] | None = None,
        session_id: str | None = None, request_id: str | None = None,
        cancellation: threading.Event | None = None,
    ) -> Iterator[ThalovantEvent]:
        if isinstance(max_buffered_events, bool) or not isinstance(max_buffered_events, int) or max_buffered_events <= 0:
            raise ValueError("max_buffered_events must be a positive integer.")
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise ThalovantTimeoutError("Event deadline expired before connection.")
        if max_events is not None and max_events <= 0:
            return
        cancellation = cancellation if cancellation is not None else threading.Event()
        deadline = None if timeout is None else time.monotonic() + timeout
        setup_deadline = deadline if deadline is not None else time.monotonic() + self._hard_connect_timeout
        events: queue.Queue[ThalovantEvent] = queue.Queue(maxsize=max_buffered_events)
        state = threading.RLock()
        errors: list[BaseException] = []
        active = True
        subscribed = False
        setup_done = threading.Event()
        accepted = 0
        yielded = 0
        expected = _context_with_correlation(context, session_id=session_id, request_id=request_id)

        def retire() -> None:
            nonlocal active, subscribed
            with state:
                active = False
                remove = subscribed
                subscribed = False
            if remove:
                try:
                    self._transport.remove_mycroft(event_name, handler)
                except ThalovantConnectionError:
                    pass

        def handler(raw: Any) -> None:
            nonlocal accepted
            with state:
                if not active or cancellation.is_set() or (deadline is not None and time.monotonic() >= deadline):
                    return
                if max_events is not None and accepted >= max_events:
                    return
            event = _event_from_message(event_name, raw)
            if not _event_matches_context(event, expected):
                return
            try:
                if predicate is not None and not predicate(event):
                    return
            except BaseException as error:
                with state:
                    errors.append(error)
                retire()
                return
            overflow = False
            complete = False
            with state:
                if not active or cancellation.is_set() or (deadline is not None and time.monotonic() >= deadline):
                    return
                if max_events is not None and accepted >= max_events:
                    return
                try:
                    events.put_nowait(event)
                    accepted += 1
                    complete = max_events is not None and accepted >= max_events
                except queue.Full:
                    errors.append(ThalovantRuntimeError("Event buffer overflow; subscription retired."))
                    overflow = True
            if overflow or complete:
                retire()

        def register() -> None:
            nonlocal subscribed
            with state:
                if not active or cancellation.is_set():
                    return
            self._transport.on_mycroft(event_name, handler)
            with state:
                expired = not active or cancellation.is_set() or time.monotonic() >= setup_deadline
                if not expired:
                    subscribed = True
            if expired:
                self._transport.remove_mycroft(event_name, handler)

        def setup() -> None:
            try:
                self._connect(setup_deadline - time.monotonic(), cancellation=cancellation, operation=register)
                setup_done.set()
                # Predicate implementations belong off the caller thread: even
                # a custom blocking transport cannot extend its wait budget.
                while not cancellation.wait(_SETTLE_POLL):
                    with state:
                        if not active:
                            return
                    if deadline is not None and time.monotonic() >= deadline:
                        retire()
                        return
                    self._raise_if_transport_stopped()
            except BaseException as error:
                with state:
                    if active and not cancellation.is_set():
                        errors.append(error)
            finally:
                setup_done.set()

        expiry = None
        if deadline is not None:
            # Retirement must happen even while the generator is paused at a
            # yield or a custom transport status predicate is blocked.
            expiry = threading.Timer(max(0.0, deadline - time.monotonic()), retire)
            expiry.daemon = True
            expiry.start()
        threading.Thread(target=setup, daemon=True).start()
        try:
            while max_events is None or yielded < max_events:
                if cancellation.is_set():
                    return
                with state:
                    if errors:
                        raise errors[0]
                if not setup_done.is_set() and time.monotonic() >= setup_deadline:
                    cancellation.set()
                    raise ThalovantTimeoutError("Event connection/registration deadline expired.")
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return
                try:
                    event = events.get(timeout=_SETTLE_POLL if remaining is None else min(_SETTLE_POLL, remaining))
                except queue.Empty:
                    continue
                with state:
                    if errors:
                        raise errors[0]
                if cancellation.is_set():
                    return
                yielded += 1
                yield event
        finally:
            cancellation.set()
            if expiry is not None:
                expiry.cancel()
            retire()

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

    def _emit_query_with_timeout(
        self, event_type: str, data: dict[str, Any], context: dict[str, Any], timeout: float,
    ) -> None:
        """Internal read query: one connect/send deadline, without replaying it."""
        if timeout <= 0:
            raise ThalovantTimeoutError("Hub query deadline expired before send.")
        self._connect(timeout, operation=lambda: self._transport.emit_event(
            event_type, data, self._context_with_identity_metadata(context),
        ))

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

        return self._ask(
            text, timeout=timeout, lang=lang, context=context,
            session_id=session_id, request_id=request_id,
        )

    def _ask(
        self, text: str, *, timeout: float = 12.0, lang: str = "en-us",
        context: dict[str, Any] | None = None, session_id: str | None = None,
        request_id: str | None = None, cancellation: threading.Event | None = None,
    ) -> ThalovantReply:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ThalovantTimeoutError("Hub request deadline expired.")
        deadline = time.monotonic() + timeout
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
        published = threading.Event()
        attempts = self.reconnect_attempts + 1 if self.auto_reconnect else 1
        for attempt in range(attempts):
            try:
                return self._query(
                    prompt,
                    timeout=deadline - time.monotonic(),
                    lang=lang,
                    context=request_context,
                    request_id=request_id,
                    session_id=_session_id_from_context(request_context),
                    cancellation=cancellation, direct=False,
                    published=published,
                )
            except ThalovantConnectionError as exc:
                last_error = exc
                if published.is_set():
                    raise
                if cancellation is not None and cancellation.is_set():
                    raise
                if attempt + 1 >= attempts:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ThalovantTimeoutError("Hub request deadline expired.") from None
                self.close(timeout=remaining)
        raise ThalovantConnectionError(
            "HiveMind transport failed while waiting for reply."
        ) from last_error

    def intents(
        self,
        languages: Iterable[str] | None = None,
        *,
        timeout: float = 5.0,
        describe: bool = True,
        fallback: bool = True,
    ) -> HubIntentInventory:
        """Everything the hub can be asked, per language, grouped by skill.

        Read from the runtime's intent manifest over this session, so no
        control-plane credential is involved. Each intent carries the
        sentences a person says to reach it, as the skill wrote them,
        ``{slot}`` placeholders included. ``languages`` defaults to the
        identity's language or ``en-us``.

        Raises :class:`ThalovantPolicyDeniedError` when the hub refuses the
        query and ``fallback`` is off; with it on, a hub allowed for only the
        engines' manifests yields intent names with ``source`` set to
        ``engine-manifests``.
        """

        from . import intents as _intents

        # A str is an Iterable[str], so intents("en-us") would otherwise
        # expand into ["e", "n", "-", "u", "s"] and ask the hub five
        # nonsense manifest queries. The empty check stays first so that
        # "" keeps falling back to the default language rather than
        # becoming a single blank tag that inventory() then rejects.
        if not languages:
            chosen = [self._default_lang()]
        elif isinstance(languages, str):
            chosen = [languages]
        else:
            chosen = list(languages)
        return _intents.inventory(
            self, chosen, timeout=timeout, describe=describe, fallback=fallback
        )

    def list_intents(
        self,
        lang: str | None = None,
        *,
        timeout: float = 5.0,
        include_definitions: bool = False,
    ) -> list[IntentRegistration]:
        """The hub's intent manifest for one language, one row per registration."""

        from . import intents as _intents

        return _intents.list_intents(
            self,
            lang or self._default_lang(),
            timeout=timeout,
            include_definitions=include_definitions,
        )

    def describe_intent(
        self,
        skill_id: str,
        intent_name: str,
        lang: str | None = None,
        *,
        timeout: float = 5.0,
    ) -> list[IntentDefinition]:
        """The registrations behind one intent in one language, sentences included."""

        from . import intents as _intents

        return _intents.describe_intent(
            self, skill_id, intent_name, lang or self._default_lang(), timeout=timeout
        )

    @staticmethod
    def _default_lang() -> str:
        return "en-us"

    def query(
        self,
        text: str,
        *,
        timeout: float = 12.0,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        query_id: str | None = None,
    ) -> ThalovantReply:
        """Send a direct HiveMind query within one connect/send/reply deadline.

        Intent misses remain provisional until completion and can recover with
        later speech. Completion and hard failures freeze the scoped reply.
        Timed-out raw I/O retains lifecycle ownership until it is retired.
        """

        return self._query(
            text, timeout=timeout, lang=lang, context=context, session_id=session_id,
            request_id=request_id, query_id=query_id,
        )

    def _query(
        self, text: str, *, timeout: float = 12.0, lang: str = "en-us",
        context: dict[str, Any] | None = None, session_id: str | None = None,
        request_id: str | None = None, query_id: str | None = None,
        cancellation: threading.Event | None = None, direct: bool = True,
        published: threading.Event | None = None,
    ) -> ThalovantReply:
        prompt = text.strip()
        if not prompt:
            raise ValueError("query() requires a non-empty text prompt.")

        request_id = request_id or _new_request_id()
        query_id = query_id or request_id
        request_context = _context_with_correlation(
            self._context_with_identity_metadata(context),
            session_id=(session_id or _new_session_id()) if direct else session_id,
            site_id=self.identity.site_id,
            lang=lang,
            request_id=request_id,
        )
        if not math.isfinite(timeout) or timeout <= 0:
            raise ThalovantTimeoutError("Hub query deadline expired.")
        deadline = time.monotonic() + timeout
        send_hive_message = getattr(self._transport, "send_hive_message", None)
        on_hive_message = getattr(self._transport, "on_hive_message", None)
        remove_hive_message = getattr(self._transport, "remove_hive_message", None)
        if direct and not all(callable(method) for method in (
            send_hive_message, on_hive_message, remove_hive_message,
        )):
            raise ThalovantRuntimeError("This transport does not support HiveMind query frames.")

        done = threading.Event()
        caller_cancellation = cancellation
        cancellation = threading.Event()
        state = threading.RLock()
        fragments: list[str] = []
        raw_messages: list[Any] = []
        events: list[ThalovantEvent] = []
        registered: list[tuple[str, Callable[[Any], None]]] = []
        errors: list[BaseException] = []
        failure_event: ThalovantEvent | None = None
        soft_failure_event: ThalovantEvent | None = None
        terminal = False
        empty_deadline: float | None = None
        settle_deadline: float | None = None

        def collection_deadline() -> float:
            if direct:
                return deadline
            phase_deadline = settle_deadline if settle_deadline is not None else empty_deadline
            return deadline if phase_deadline is None else min(deadline, phase_deadline)

        def finish_at_deadline() -> None:
            nonlocal terminal
            if direct:
                fail(timeout_error())
            else:
                terminal = True
                done.set()

        def timeout_error() -> ThalovantTimeoutError:
            return ThalovantTimeoutError(f"Hub did not finish the query within {timeout:g}s.")

        def fail(error: BaseException) -> None:
            nonlocal terminal
            with state:
                if terminal:
                    return
                if caller_cancellation is not None and caller_cancellation.is_set():
                    error = ThalovantConnectionError("Hub request was cancelled.")
                elif not direct and time.monotonic() >= collection_deadline():
                    finish_at_deadline()
                    return
                terminal = True
                errors.append(error)
                done.set()

        def handle_query_frame(message: Any) -> None:
            nonlocal failure_event, soft_failure_event, terminal
            nonlocal empty_deadline, settle_deadline
            with state:
                if terminal:
                    return
                now = time.monotonic()
                if now >= collection_deadline():
                    finish_at_deadline()
                    return
                if direct:
                    if _query_id_from_hive_message(message) != query_id:
                        return
                    event = _event_from_query_hive_message(message)
                    if event is None:
                        return
                else:
                    event = message
                    # The runtime may replace the conversation session. The
                    # request ID remains required to exclude ambient replies.
                    if event.request_id != request_id:
                        return
                raw_messages.append(message if direct else event.raw)
                events.append(event)
                if direct and event.name == "hive.query.complete":
                    terminal = True
                    done.set()
                elif not direct and event.name == EVENT_UTTERANCE_HANDLED:
                    if not fragments and empty_deadline is None:
                        empty_deadline = now + self.empty_reply_wait_seconds
                elif event.name in {EVENT_SPEAK, EVENT_OVOS_UTTERANCE_SPEAK}:
                    normalized = " ".join(event.text.strip().split())
                    if normalized:
                        if not fragments or fragments[-1] != normalized:
                            fragments.append(normalized)
                        soft_failure_event = None
                        if not direct and settle_deadline is None:
                            settle_deadline = now + self.reply_settle_seconds
                elif event.name in {EVENT_INTENT_FAILURE, EVENT_INTENT_UNMATCHED}:
                    if not fragments:
                        soft_failure_event = event
                        if not direct and empty_deadline is None:
                            empty_deadline = now + self.empty_reply_wait_seconds
                elif event.is_failure:
                    failure_event = event
                    terminal = True
                    done.set()

        def send() -> None:
            # Registration happens inside the owned connection generation, after
            # readiness. A caller that already expired can never subscribe later.
            kinds = ("query", "cascade") if direct else (
                EVENT_SPEAK, EVENT_OVOS_UTTERANCE_SPEAK, EVENT_UTTERANCE_HANDLED,
                EVENT_INTENT_FAILURE, EVENT_INTENT_UNMATCHED, EVENT_POLICY_DENIED, EVENT_QUERY_TIMEOUT,
            )
            for kind in kinds:
                with state:
                    if terminal or cancellation.is_set():
                        return
                handler = handle_query_frame if direct else (
                    lambda raw, name=kind: handle_query_frame(_event_from_message(name, raw))
                )
                subscribe = on_hive_message if direct else self._transport.on_mycroft
                unsubscribe = remove_hive_message if direct else self._transport.remove_mycroft
                subscribe(kind, handler)
                with state:
                    expired = terminal or cancellation.is_set()
                    if not expired:
                        registered.append((kind, handler))
                if expired:
                    unsubscribe(kind, handler)
                    return
            payload = _utterance_payload(prompt, lang)
            frame = None
            if direct:
                inner = {
                    "msg_type": "bus",
                    "payload": {
                        "type": EVENT_RECOGNIZER_LOOP_UTTERANCE,
                        "data": payload,
                        "context": request_context,
                    },
                    "metadata": {}, "route": [], "node": None,
                    "target_site_id": None, "target_pubkey": None, "source_peer": None,
                }
                frame = {
                    "msg_type": "query", "payload": inner,
                    "metadata": {"query_id": query_id}, "route": [], "node": None,
                    "target_site_id": None, "target_pubkey": None, "source_peer": None,
                }
            with state:
                if terminal or cancellation.is_set() or (
                    caller_cancellation is not None and caller_cancellation.is_set()
                ):
                    return
                if time.monotonic() >= collection_deadline():
                    finish_at_deadline()
                    return
                # Admission is atomic with the final deadline/cancellation
                # check. An admitted write may finish late, but is never replayed.
                if published is not None:
                    published.set()
            if direct:
                send_hive_message(frame, encrypt=True)
            else:
                self._transport.emit_event(EVENT_RECOGNIZER_LOOP_UTTERANCE, payload, request_context)

        def run() -> None:
            try:
                if caller_cancellation is not None and caller_cancellation.is_set():
                    cancellation.set()
                    raise ThalovantConnectionError("Hub request was cancelled.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise timeout_error()
                # _connect owns the raw write and any delayed cleanup until they
                # settle, even when the query caller has already returned.
                self._connect(remaining, cancellation=cancellation, operation=send)
                while not done.wait(_SETTLE_POLL) and not cancellation.is_set():
                    with state:
                        if time.monotonic() >= collection_deadline():
                            finish_at_deadline()
                            return
                    self._raise_if_transport_stopped()
            except BaseException as error:
                if isinstance(error, ThalovantConnectionError) and isinstance(
                    error.__cause__, ThalovantTimeoutError,
                ):
                    error = timeout_error()
                fail(error)

        threading.Thread(target=run, daemon=True).start()
        completed = False
        try:
            while not done.is_set():
                if cancellation.is_set() or (caller_cancellation is not None and caller_cancellation.is_set()):
                    cancellation.set()
                    fail(ThalovantConnectionError("Hub request was cancelled."))
                    break
                with state:
                    remaining = collection_deadline() - time.monotonic()
                if remaining <= 0:
                    with state:
                        finish_at_deadline()
                    break
                done.wait(min(_SETTLE_POLL, remaining))
            with state:
                if errors:
                    raise errors[0]
                failure_event = failure_event or soft_failure_event
            if caller_cancellation is not None and caller_cancellation.is_set():
                raise ThalovantConnectionError("Hub request was cancelled.")
            if failure_event is not None and not fragments:
                raise ThalovantRuntimeError(_failure_reason(failure_event))
            if not fragments:
                raise ThalovantTimeoutError("Hub finished the query but did not emit a speak reply.")
            reply = ThalovantReply(
                text=" ".join(fragments),
                utterances=tuple(fragments),
                handled=failure_event is None,
                session_id=(
                    next((value for value in (event.session_id for event in events) if value and value.strip()), None)
                    or _session_id_from_context(request_context)
                ),
                request_id=request_id,
                raw_messages=tuple(raw_messages),
                events=tuple(events),
                failure_event=failure_event,
            )
            completed = True
            return reply
        finally:
            # Successful collection need not retire a healthy admitted write.
            # _connect retains ownership and its original send deadline until
            # the physical write completes, even after this caller returns.
            if not completed:
                cancellation.set()
            with state:
                terminal = True
                owned_handlers = tuple(registered)
                registered.clear()
            for kind, handler in owned_handlers:
                try:
                    unsubscribe = remove_hive_message if direct else self._transport.remove_mycroft
                    unsubscribe(kind, handler)
                except ThalovantConnectionError:
                    pass

    def _with_reconnect(self, operation: Callable[[], Any]) -> Any:
        last_error: BaseException | None = None
        attempts = self.reconnect_attempts + 1 if self.auto_reconnect else 1
        for attempt in range(attempts):
            try:
                self.connect()
            except (
                ConnectionAbortedError,
                RuntimeError,
                ThalovantConnectionError,
            ) as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                self.close()
            else:
                # Publication can succeed remotely even if its local write
                # reports a failure. Reconnect only before invoking it.
                return operation()
        raise ThalovantConnectionError("HiveMind transport failed before publication.") from last_error

    def _raise_if_transport_stopped(self) -> None:
        if self._transport.is_connected():
            return
        error = self._transport.last_error()
        detail = f": {_redact_error_text(error)}" if error else ""
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

    async def connect(self, timeout: float | None = None) -> None:
        cancellation = threading.Event()
        try:
            await asyncio.to_thread(self._client._connect, timeout, cancellation)
        except asyncio.CancelledError:
            cancellation.set()
            raise

    async def connect_with_info(self, timeout: float | None = None) -> ThalovantConnectionInfo:
        await self.connect(timeout=timeout)
        return await self.connection_info()

    async def connection_info(self) -> ThalovantConnectionInfo:
        return await asyncio.to_thread(self._client.connection_info)

    async def close(self, timeout: float | None = None) -> None:
        await asyncio.to_thread(self._client.close, timeout)

    async def wait_closed(self, timeout: float | None = None) -> None:
        await asyncio.to_thread(self._client.wait_closed, timeout)

    disconnect = close

    async def healthcheck(self) -> ThalovantHealth:
        return await asyncio.to_thread(self._client.healthcheck)

    async def intents(
        self,
        languages: Iterable[str] | None = None,
        *,
        timeout: float = 5.0,
        describe: bool = True,
        fallback: bool = True,
    ) -> HubIntentInventory:
        return await asyncio.to_thread(
            self._client.intents,
            languages,
            timeout=timeout,
            describe=describe,
            fallback=fallback,
        )

    async def list_intents(
        self,
        lang: str | None = None,
        *,
        timeout: float = 5.0,
        include_definitions: bool = False,
    ) -> list[IntentRegistration]:
        return await asyncio.to_thread(
            self._client.list_intents,
            lang,
            timeout=timeout,
            include_definitions=include_definitions,
        )

    async def describe_intent(
        self,
        skill_id: str,
        intent_name: str,
        lang: str | None = None,
        *,
        timeout: float = 5.0,
    ) -> list[IntentDefinition]:
        return await asyncio.to_thread(
            self._client.describe_intent, skill_id, intent_name, lang, timeout=timeout
        )

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
        self, event_name: str, *, timeout: float | None = None,
        max_events: int | None = None, max_buffered_events: int = 256,
        predicate: EventPredicate | None = None,
        context: dict[str, Any] | None = None,
        session_id: str | None = None, request_id: str | None = None,
    ) -> AsyncIterator[ThalovantEvent]:
        cancellation = threading.Event()
        stream = self._client._listen(
            event_name, timeout=timeout, max_events=max_events,
            max_buffered_events=max_buffered_events, predicate=predicate,
            context=context, session_id=session_id, request_id=request_id,
            cancellation=cancellation,
        )
        exhausted = object()
        pending: asyncio.Task[Any] | None = None

        def close_stream() -> None:
            try:
                stream.close()
            except ValueError:
                # Event-loop shutdown can cancel the Task while its executor
                # thread is still inside next(). The cancellation flag makes
                # that thread exit through the generator's own finally block.
                if not stream.gi_running:
                    raise

        try:
            while True:
                pending = asyncio.create_task(asyncio.to_thread(next, stream, exhausted))
                event = await asyncio.shield(pending)
                if event is exhausted:
                    return
                yield event
        finally:
            cancellation.set()
            if pending is not None and not pending.done():
                def close_after_next(task: asyncio.Task[Any]) -> None:
                    try:
                        task.result()
                    except BaseException:
                        pass
                    close_stream()
                pending.add_done_callback(close_after_next)
            else:
                close_stream()

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
        cancellation = threading.Event()
        try:
            return await asyncio.to_thread(
                self._client._ask,
                text,
                timeout=timeout,
                lang=lang,
                context=context,
                session_id=session_id,
                request_id=request_id,
                cancellation=cancellation,
            )
        except asyncio.CancelledError:
            cancellation.set()
            raise

    async def query(
        self,
        text: str,
        *,
        timeout: float = 12.0,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        query_id: str | None = None,
    ) -> ThalovantReply:
        cancellation = threading.Event()
        try:
            return await asyncio.to_thread(
                self._client._query,
                text,
                timeout=timeout,
                lang=lang,
                context=context,
                session_id=session_id,
                request_id=request_id,
                query_id=query_id,
                cancellation=cancellation,
            )
        except asyncio.CancelledError:
            cancellation.set()
            raise

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
        cancellation = threading.Event()
        try:
            return await asyncio.to_thread(
                self._client._wait_for_event,
                event_name,
                timeout=timeout,
                predicate=predicate,
                context=context,
                session_id=session_id,
                request_id=request_id,
                cancellation=cancellation,
            )
        except asyncio.CancelledError:
            cancellation.set()
            raise
