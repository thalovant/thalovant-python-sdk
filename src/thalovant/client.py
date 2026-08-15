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
    _runtime_crypto_key,
    _session_id_from_context,
    _utterance_payload,
)
from .identity import ThalovantIdentity
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


def _connect_transport_with_timeout(transport: Transport, timeout: float) -> None:
    done = threading.Event()
    errors: list[BaseException] = []

    def run_connect() -> None:
        try:
            transport.connect()
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=run_connect, daemon=True)
    thread.start()
    if not done.wait(timeout=timeout):
        try:
            transport.disconnect()
        except Exception:
            pass
        raise ThalovantConnectionError(
            f"Hub connection did not complete within {timeout:g}s."
        )
    if errors:
        raise errors[0]


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

    def connect(self, timeout: float | None = None) -> None:
        """Open the HiveMind HTTP connection if needed."""

        if self._connected and self._transport.is_connected():
            return
        if self._connected:
            self.close()
        _connect_transport_with_timeout(
            self._transport,
            timeout if timeout and timeout > 0 else self._hard_connect_timeout,
        )
        self._connected = True

    def connect_with_info(self, timeout: float | None = None) -> ThalovantConnectionInfo:
        """Connect and return the transport timing snapshot."""

        self.connect(timeout=timeout)
        return self.connection_info()

    def connection_info(self) -> ThalovantConnectionInfo:
        """Return connection timing for the current or most recent transport."""

        return self._transport.connection_info()

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
        """Send a direct HiveMind query frame and wait for its scoped reply."""

        prompt = text.strip()
        if not prompt:
            raise ValueError("query() requires a non-empty text prompt.")

        request_id = request_id or _new_request_id()
        query_id = query_id or request_id
        request_context = _context_with_correlation(
            self._context_with_identity_metadata(context),
            session_id=session_id or _new_session_id(),
            site_id=self.identity.site_id,
            lang=lang,
            request_id=request_id,
        )
        self.connect()
        send_hive_message = getattr(self._transport, "send_hive_message", None)
        on_hive_message = getattr(self._transport, "on_hive_message", None)
        remove_hive_message = getattr(self._transport, "remove_hive_message", None)
        if not (
            callable(send_hive_message)
            and callable(on_hive_message)
            and callable(remove_hive_message)
        ):
            raise ThalovantRuntimeError("This transport does not support HiveMind query frames.")

        done = threading.Event()
        fragments: list[str] = []
        raw_messages: list[Any] = []
        events: list[ThalovantEvent] = []
        failure_event: ThalovantEvent | None = None

        def handle_query_frame(message: Any) -> None:
            nonlocal failure_event
            if _query_id_from_hive_message(message) != query_id:
                return
            event = _event_from_query_hive_message(message)
            if event is None:
                return
            raw_messages.append(message)
            events.append(event)
            if event.name == "hive.query.complete":
                done.set()
                return
            if event.name in {EVENT_SPEAK, EVENT_OVOS_UTTERANCE_SPEAK}:
                normalized = " ".join(event.text.strip().split())
                if normalized and (not fragments or fragments[-1] != normalized):
                    fragments.append(normalized)
                return
            if event.is_failure:
                failure_event = event
                done.set()

        on_hive_message("query", handle_query_frame)
        on_hive_message("cascade", handle_query_frame)
        try:
            inner = {
                "msg_type": "bus",
                "payload": {
                    "type": EVENT_RECOGNIZER_LOOP_UTTERANCE,
                    "data": _utterance_payload(prompt, lang),
                    "context": request_context,
                },
                "metadata": {},
                "route": [],
                "node": None,
                "target_site_id": None,
                "target_pubkey": None,
                "source_peer": None,
            }
            send_hive_message(
                {
                    "msg_type": "query",
                    "payload": inner,
                    "metadata": {"query_id": query_id},
                    "route": [],
                    "node": None,
                    "target_site_id": None,
                    "target_pubkey": None,
                    "source_peer": None,
                },
                encrypt=True,
            )
            self._wait_for_query(done, timeout=timeout)
            if self.reply_settle_seconds > 0:
                time.sleep(self.reply_settle_seconds)
            if failure_event is not None and not fragments:
                raise ThalovantRuntimeError(_failure_reason(failure_event))
            if not fragments:
                raise ThalovantTimeoutError("Hub finished the query but did not emit a speak reply.")
            return ThalovantReply(
                text=" ".join(fragments),
                utterances=tuple(fragments),
                handled=failure_event is None,
                session_id=_session_id_from_context(request_context),
                request_id=request_id,
                raw_messages=tuple(raw_messages),
                events=tuple(events),
                failure_event=failure_event,
            )
        finally:
            for msg_type in ("query", "cascade"):
                try:
                    remove_hive_message(msg_type, handle_query_frame)
                except ThalovantConnectionError:
                    pass

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
            (EVENT_OVOS_UTTERANCE_SPEAK, handle_speak),
            (EVENT_UTTERANCE_HANDLED, handle_handled),
            (EVENT_INTENT_FAILURE, handle_failure),
            (EVENT_POLICY_DENIED, handle_failure),
            (EVENT_QUERY_TIMEOUT, handle_failure),
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

    def _wait_for_query(self, done: threading.Event, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not done.is_set():
            self._raise_if_transport_stopped()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ThalovantTimeoutError(
                    f"Hub did not finish the query within {timeout:g}s."
                )
            done.wait(timeout=min(0.1, remaining))

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
        await asyncio.to_thread(self._client.connect, timeout=timeout)

    async def connect_with_info(self, timeout: float | None = None) -> ThalovantConnectionInfo:
        return await asyncio.to_thread(self._client.connect_with_info, timeout=timeout)

    async def connection_info(self) -> ThalovantConnectionInfo:
        return await asyncio.to_thread(self._client.connection_info)

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
        return await asyncio.to_thread(
            self._client.query,
            text,
            timeout=timeout,
            lang=lang,
            context=context,
            session_id=session_id,
            request_id=request_id,
            query_id=query_id,
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
