"""HiveMind data-plane transport adapters."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import re
import queue
import socket
import threading
import time
import uuid
from typing import Any, Callable, NamedTuple, Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .errors import (
    ThalovantConnectionError,
    ThalovantRuntimeError,
    ThalovantTimeoutError,
)
from .events import _runtime_bus_context
from .identity import ThalovantIdentity
from .models import ThalovantConnectionInfo, ThalovantHealth

_URL_QUERY_RE = re.compile(r"\?\S+")


def _redact_error_text(error: object) -> str:
    """Render an error for humans with URL query strings stripped.

    Connection failures embed the request URL in the error text, and the
    data-plane URLs carry the access key in the query
    (``?authorization=base64(<userAgent>:<accessKey>)``), so everything from
    ``?`` onward is dropped before the text is stored or displayed.
    """

    return _URL_QUERY_RE.sub("?<redacted>", str(error))


class Transport(Protocol):
    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def on_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None: ...

    def remove_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None: ...

    def on_hive_message(self, msg_type: str, handler: Callable[[Any], None]) -> None: ...

    def remove_hive_message(self, msg_type: str, handler: Callable[[Any], None]) -> None: ...

    def send_hive_message(self, message: dict[str, Any], *, encrypt: bool = True) -> Any: ...

    def emit_event(
        self,
        event_type: str,
        data: dict[str, Any],
        context: dict[str, Any],
    ) -> Any: ...

    def healthcheck(self) -> ThalovantHealth: ...

    def connection_info(self) -> ThalovantConnectionInfo: ...

    def is_connected(self) -> bool: ...

    def last_error(self) -> BaseException | None: ...



def _require_tls_endpoint(endpoint: str) -> None:
    """Refuse a hub endpoint that is not https."""
    parsed = urlparse(endpoint)
    if parsed.scheme != "https":
        raise ThalovantConnectionError(
            f"Refusing to use the HTTP transport over {parsed.scheme or 'no'}://. "
            "It needs an https:// endpoint: without TLS every message and the "
            "access key travel in the clear."
        )


class _ConnectionLifecycle:
    """Short state locks; blocking socket operations never hold this lock."""

    def _init_lifecycle(self) -> None:
        self._lifecycle_lock = threading.RLock()
        self._generation = 0
        self._connecting = False
        self._closing = False
        self._failed_cleanup: tuple[Any, BaseException] | None = None

    def _is_current_client(self, client: Any) -> bool:
        with self._lifecycle_lock:
            return self._client is client

    def _reserve_connection(self) -> tuple[int, Any]:
        with self._lifecycle_lock:
            if self._connecting or self._closing:
                raise ThalovantConnectionError("A connection lifecycle operation is already in progress.")
            if self._failed_cleanup is not None:
                raise ThalovantConnectionError("Previous connection cleanup failed; retry disconnect before reconnecting.") from None
            self._connecting = True
            self._generation += 1
            old = self._client
            self._client = None
            self._closing = old is not None
            self._begin_connection(close_previous=False)
            return self._generation, old

    def _close_client_once(self, client: Any, *, retry_failed: bool = False) -> None:
        with self._lifecycle_lock:
            if getattr(client, "thalovant_cleanup_started", False):
                error = getattr(client, "thalovant_cleanup_error", None)
                if error is None:
                    return
                if not retry_failed:
                    raise error
            client.thalovant_cleanup_started = True
        try:
            self._close_detached(client)
        except BaseException as error:
            with self._lifecycle_lock:
                client.thalovant_cleanup_error = error
                self._failed_cleanup = (client, error)
                self._transport_connected = False
                self._fail_connection(error)
            raise
        else:
            with self._lifecycle_lock:
                client.thalovant_cleanup_error = None
                if self._failed_cleanup is not None and self._failed_cleanup[0] is client:
                    self._failed_cleanup = None

    def _cleanup_reserved(self, old: Any) -> None:
        try:
            if old is not None:
                self._close_client_once(old)
        except BaseException:
            with self._lifecycle_lock:
                self._connecting = False
            raise
        finally:
            with self._lifecycle_lock:
                self._closing = False

    def _install_client(self, client: Any, generation: int) -> None:
        with self._lifecycle_lock:
            if generation != self._generation or not self._connecting:
                raise ThalovantConnectionError("Connection attempt was cancelled.")
            self._client = client

    def _finish_connection(self, client: Any, generation: int) -> None:
        with self._lifecycle_lock:
            if generation != self._generation or self._client is not client or not self._connecting:
                raise ThalovantConnectionError("Connection attempt was cancelled.")
            self._connecting = False
            self._transport_connected = True
            self._complete_handshake()

    def _fail_current_client(self, client: Any, error: BaseException) -> None:
        with self._lifecycle_lock:
            if self._client is client:
                self._fail_connection(error)

    def _abort_connection(self, client: Any, generation: int, error: BaseException) -> None:
        cleanup = False
        with self._lifecycle_lock:
            if generation == self._generation:
                self._client = None
                self._connecting = False
                self._transport_connected = False
                self._closing = client is not None
                self._fail_connection(error)
                cleanup = client is not None
        if cleanup:
            try:
                self._close_client_once(client)
            except BaseException:
                # Keep the primary connection failure; the cleanup error and
                # exact client remain retained for observation/explicit retry.
                pass
            finally:
                with self._lifecycle_lock:
                    self._closing = False
        elif client is not None:
            # A client constructed after cancellation still needs local cleanup.
            # The once guard prevents repeating an earlier HTTP /disconnect.
            try:
                self._close_client_once(client)
            except BaseException:
                pass

    def disconnect(self) -> None:
        self._disconnect(retry_failed=True)

    def _retire_connection(self) -> None:
        """Automatic cancellation never silently retries a failed admission."""
        self._disconnect(retry_failed=False)

    def _disconnect(self, *, retry_failed: bool) -> None:
        with self._lifecycle_lock:
            if self._closing:
                # An existing cleanup owns the old socket; invalidate an
                # attempt waiting on that cleanup without running it twice.
                self._generation += 1
                self._connecting = False
                if retry_failed:
                    raise ThalovantConnectionError("Connection cleanup is already in progress.")
                return
            client = self._failed_cleanup[0] if self._failed_cleanup is not None else self._client
            self._client = None
            self._generation += 1
            self._connecting = False
            self._transport_connected = False
            self._closing = client is not None
        try:
            if client is not None:
                self._close_client_once(client, retry_failed=retry_failed)
            with self._lifecycle_lock:
                self._mark_closed()
        finally:
            with self._lifecycle_lock:
                self._closing = False


class HiveMindHTTPTransport(_ConnectionLifecycle):
    """HTTPS adapter with cookie-affine polling and authenticated v3 Noise."""

    def __init__(
        self,
        identity: ThalovantIdentity,
        *,
        useragent: str,
        connect_timeout: float = 4.0,
        handshake_timeout: float = 20.0,
        handshake_poll_interval: float = 0.1,
        handshake_settle_seconds: float = 0.1,
        send_timeout: float = 8.0,
        self_signed: bool = False,
        noise_state_dir: str | None = None,
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
        self.noise_state_dir = noise_state_dir
        self.compress = compress
        self.binarize = binarize
        self._init_lifecycle()
        self._client: Any | None = None
        self._transport_connected = False
        self._deps: _HiveMindDeps | None = None
        self._last_error: BaseException | None = None
        self._connect_started = 0.0
        self._transport_opened = 0.0
        self._connection_info = ThalovantConnectionInfo()

    def connection_info(self) -> ThalovantConnectionInfo:
        return self._connection_info

    def _begin_connection(self, *, close_previous: bool = True) -> None:
        self._last_error = None
        self._connect_started = time.monotonic()
        self._transport_opened = 0.0
        self._connection_info = ThalovantConnectionInfo(
            phase="connecting",
            started_at=_utc_now(),
        )

    def _mark_transport_open(self, *, socket: bool = False) -> None:
        if not self._connect_started:
            self._begin_connection()
        if self._transport_opened:
            return
        self._transport_opened = time.monotonic()
        open_ms = _elapsed_ms(self._connect_started, self._transport_opened)
        self._connection_info = ThalovantConnectionInfo(
            phase="handshake",
            started_at=self._connection_info.started_at,
            transport_open_ms=open_ms,
            socket_open_ms=open_ms if socket else self._connection_info.socket_open_ms,
        )

    def _complete_handshake(self) -> None:
        now = time.monotonic()
        opened = self._transport_opened or self._connect_started or now
        started = self._connect_started or opened
        self._connection_info = ThalovantConnectionInfo(
            phase="ready",
            started_at=self._connection_info.started_at,
            connected_at=_utc_now(),
            transport_open_ms=self._connection_info.transport_open_ms,
            socket_open_ms=self._connection_info.socket_open_ms,
            handshake_ms=_elapsed_ms(opened, now),
            connect_ms=_elapsed_ms(started, now),
        )

    def _fail_connection(self, error: BaseException) -> None:
        self._last_error = error
        started = self._connect_started or time.monotonic()
        self._connection_info = ThalovantConnectionInfo(
            phase="error",
            started_at=self._connection_info.started_at,
            transport_open_ms=self._connection_info.transport_open_ms,
            socket_open_ms=self._connection_info.socket_open_ms,
            handshake_ms=self._connection_info.handshake_ms,
            connect_ms=_elapsed_ms(started, time.monotonic()),
            last_error=_redact_error_text(error),
        )

    def _mark_closed(self) -> None:
        self._connection_info = ThalovantConnectionInfo(
            phase="closed",
            started_at=self._connection_info.started_at,
            connected_at=self._connection_info.connected_at,
            transport_open_ms=self._connection_info.transport_open_ms,
            socket_open_ms=self._connection_info.socket_open_ms,
            handshake_ms=self._connection_info.handshake_ms,
            connect_ms=self._connection_info.connect_ms,
            last_error=self._connection_info.last_error,
        )

    def connect(self) -> None:
        if self.is_connected():
            return
        _require_tls_endpoint(self.identity.endpoint_base())
        from ._http_runtime import HTTPNoiseClient

        generation, old = self._reserve_connection()
        self._cleanup_reserved(old)
        client = None
        try:
            client = HTTPNoiseClient(self)
            self._install_client(client, generation)
            client.connect()
            if not client.channel.ready or not client.connected.is_set():
                raise ThalovantConnectionError("HTTP Noise session closed before readiness.")
            self._finish_connection(client, generation)
        except Exception as exc:
            self._abort_connection(client, generation, exc)
            if isinstance(exc, ThalovantTimeoutError):
                raise
            raise ThalovantConnectionError("Could not establish the HiveMind HTTP Noise session.") from exc

    def _close_detached(self, client: Any) -> None:
        self._shutdown_client(client, transport_connected=True)

    def on_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self._require_client().on_mycroft(event_name, handler)

    def remove_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None:
        # Timeout cleanup may already have detached this session. Unsubscribing
        # then must not mask the original timeout with a connection error.
        with self._lifecycle_lock:
            client = self._client
        if client is not None:
            client.remove_mycroft(event_name, handler)

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
        return self._send_hive_message_object(hive_message, encrypt=True)

    def on_hive_message(self, msg_type: str, handler: Callable[[Any], None]) -> None:
        self._require_client().on(msg_type, handler)

    def remove_hive_message(self, msg_type: str, handler: Callable[[Any], None]) -> None:
        self._require_client().remove(msg_type, handler)

    def send_hive_message(self, message: dict[str, Any], *, encrypt: bool = True) -> Any:
        deps = self._load_deps()
        return self._send_hive_message_object(deps.HiveMessage(**message), encrypt=encrypt)

    def _send_hive_message_object(self, hive_message: Any, *, encrypt: bool) -> Any:
        # The legacy flag remains source-compatible; v3 application traffic is
        # always encrypted and cannot bypass the authenticated channel.
        return self._require_live_client().emit(hive_message)

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
            last_error=_redact_error_text(error) if error else None,
            connection=self.connection_info(),
        )

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

    def _require_client(self) -> Any:
        if self._client is None:
            raise ThalovantConnectionError("HiveMind HTTP transport is not connected.")
        return self._client

    def _require_live_client(self) -> Any:
        client = self._require_client()
        if not self.is_connected():
            error = self.last_error()
            detail = f": {_redact_error_text(error)}" if error else ""
            raise ThalovantConnectionError(f"HiveMind HTTP transport is not connected{detail}")
        return client

    def _load_deps(self) -> "_HiveMindDeps":
        if self._deps is not None:
            return self._deps
        try:
            import requests
            from hivemind_bus_client.client import HiveMessageBusClient, WebSocketApp
            from hivemind_bus_client.encryption import encrypt_as_json
            from hivemind_bus_client.http_client import HiveMindHTTPClient
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
            HiveMessageBusClient=HiveMessageBusClient,
            encrypt_as_json=encrypt_as_json,
            HiveMessage=HiveMessage,
            HiveMessageType=HiveMessageType,
            HiveMindSlaveProtocol=HiveMindSlaveProtocol,
            Message=Message,
            Session=Session,
            serialize_message=serialize_message,
            requests=requests,
            WebSocketApp=WebSocketApp,
        )
        return self._deps

    def _build_wss_client_class(self, base_class: Any, web_socket_app: Any) -> Any:
        transport = self

        class _ObservedHiveMessageBusClient(base_class):  # type: ignore[misc, valid-type]
            thalovant_last_error: BaseException | None = None
            thalovant_closed: bool = False

            def on_message(inner_self: Any, *args: Any) -> None:
                if not transport._is_current_client(inner_self) or inner_self.thalovant_closed:
                    return
                raw = args[-1]
                if inner_self.noise_transport is not None:
                    allowed = isinstance(raw, bytes)
                else:
                    try:
                        allowed = isinstance(raw, str) and json.loads(raw).get("msg_type") in {"hello", "shake", "handshake"}
                    except (ValueError, AttributeError):
                        allowed = False
                if not allowed:
                    error = ThalovantConnectionError("WSS traffic violated the authenticated Noise session boundary.")
                    inner_self.thalovant_last_error = error
                    transport._fail_current_client(inner_self, error)
                    inner_self.handshake_event.clear()
                    inner_self.close_connection()
                    return
                super().on_message(*args)

            def on_error(inner_self: Any, *args: Any) -> None:
                # The transport closed us on purpose: do not sleep-and-reconnect.
                if inner_self.thalovant_closed or not transport._is_current_client(inner_self):
                    try:
                        inner_self.connected_event.clear()
                        inner_self.handshake_event.clear()
                    except Exception:
                        pass
                    return
                super().on_error(*args)

            def create_client(inner_self: Any) -> Any:
                if inner_self.thalovant_closed:
                    # Reached from the base on_error's retry after its sleep; the
                    # WebSocketException is swallowed there and the loop ends.
                    from websocket import WebSocketException

                    raise WebSocketException("transport closed")
                return web_socket_app(
                    transport._authorized_wss_url(
                        key=inner_self.key,
                        useragent=inner_self.useragent,
                    ),
                    on_open=inner_self.on_open,
                    on_close=inner_self.on_close,
                    on_error=inner_self.on_error,
                    on_message=inner_self.on_message,
                )

            def run_forever(inner_self: Any) -> None:
                try:
                    super().run_forever()
                except Exception as exc:
                    inner_self.thalovant_last_error = exc
                    transport._fail_current_client(inner_self, exc)
                    try:
                        inner_self.handshake_event.clear()
                    except Exception:
                        pass
                    raise

            def wait_for_handshake(
                inner_self: Any,
                timeout: float = 5,
                max_retries: int = 15,
            ) -> None:
                deadline = time.monotonic() + transport.handshake_timeout
                proactive_handshake_at = time.monotonic() + min(
                    1.0, transport.handshake_timeout
                )
                while time.monotonic() < deadline:
                    if inner_self.thalovant_closed or not transport._is_current_client(inner_self):
                        raise ThalovantConnectionError("WSS connection attempt was cancelled.")
                    remaining = max(0.0, deadline - time.monotonic())
                    wait_for = min(transport.handshake_poll_interval, remaining)
                    if inner_self.connected_event.is_set():
                        with transport._lifecycle_lock:
                            if transport._client is inner_self:
                                transport._mark_transport_open(socket=True)
                    if inner_self.handshake_event.wait(timeout=wait_for):
                        time.sleep(transport.handshake_settle_seconds)
                        if not transport._is_current_client(inner_self) or not inner_self.connected_event.is_set():
                            raise ThalovantConnectionError("WSS closed during Noise negotiation.")
                        return
                    should_start_handshake = (
                        inner_self.connected_event.is_set()
                        and time.monotonic() >= proactive_handshake_at
                    )
                    if should_start_handshake:
                        try:
                            inner_self.protocol.start_handshake()
                        except Exception as exc:
                            inner_self.thalovant_last_error = exc
                            transport._fail_current_client(inner_self, exc)
                            raise
                    elif not inner_self.connected_event.is_set():
                        inner_self.connected_event.wait(timeout=wait_for)
                raise ThalovantTimeoutError("HiveMind WSS handshake timed out.")

        return _ObservedHiveMessageBusClient

    def _authorized_wss_url(self, *, key: str, useragent: str) -> str:
        endpoint = self.identity.endpoint_for("wss")
        if not endpoint:
            raise ThalovantConnectionError("The identity does not include a WSS endpoint.")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
            raise ThalovantConnectionError("WSS endpoint must start with ws:// or wss://.")
        authorization = base64.b64encode(f"{useragent}:{key}".encode("utf-8")).decode(
            "ascii"
        )
        query = [
            item
            for item in parse_qsl(parsed.query, keep_blank_values=True)
            if item[0] != "authorization"
        ]
        query.append(("authorization", authorization))
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path or "",
                "",
                urlencode(query),
                "",
            )
        )

    def _raise_for_emit_response(self, response: Any) -> None:
        status_code = getattr(response, "status_code", None)
        try:
            body = response.json()
        except Exception:
            body = {}

        error = body.get("error") if isinstance(body, dict) else None
        if error:
            redacted = _redact_error_text(error)
            if "not connected" in str(error).lower():
                exc = ThalovantConnectionError(f"HiveMind HTTP send failed: {redacted}")
                self._last_error = exc
                raise exc
            raise ThalovantRuntimeError(f"HiveMind HTTP send failed: {redacted}")

        if getattr(response, "ok", False) is False:
            detail = _redact_error_text(getattr(response, "text", "")) or (
                f"HTTP {status_code or 'error'}"
            )
            raise ThalovantRuntimeError(f"HiveMind HTTP send failed: {detail}")

    def _build_protocol(self, client: Any, deps: "_HiveMindDeps") -> Any:
        """Build the slave protocol.

        There is nothing to choose between any more: a HiveMind-core 5.x hub
        accepts only the v3 Noise handshake, which the bus client performs
        itself from the identity password.
        """
        class StrictNoiseProtocol(deps.HiveMindSlaveProtocol):
            def _drop_stale_pin_after_kk_failure(self) -> None:
                # Failed authentication cannot authorize changing a trusted key.
                return

            def _legacy_start_handshake(self, server_payload: dict[str, Any]) -> None:
                # An offer may not have arrived during the client's proactive
                # timer. Wait for it; a real incompatible offer fails closed.
                if server_payload:
                    self._abort_noise("The SDK requires a HiveMind v3 Noise offer.")

        return StrictNoiseProtocol(
            client,
            shared_bus=client.share_bus,
            site_id=self.identity.site_id or "unknown",
            identity=client.identity,
        )

    def _shutdown_client(self, client: Any, *, transport_connected: bool) -> None:
        client.close()


# The OVOS client library logs a deprecation every time it reads a session
# whose location is in the retired nested shape -- and the hub's own sessions
# arrive in that shape, on every reply, as do the ones hivemind's fake bus
# builds. Nothing a client can change: the shape is the hub's. Three lines per
# question in a voice satellite's journal is noise, so that one record is
# dropped. Every other warning from the library still goes through.
#
# Where it is dropped matters. ovos-utils names the logger of a deprecation
# after its call site ("OVOS - ovos_bus_client.session:_normalize_location_
# input:87"), one logger per site with its own handler and no propagation, so
# no logger a client could name in advance ever sees the record, and the
# library offers no switch for deprecations. The one factory those loggers
# come through, `LOG.create_logger`, is wrapped once to attach the filter to
# every logger it hands out under the library's name; loggers it already
# handed out get it too. Nothing else about the library's logging changes.
_UPSTREAM_LOCATION_DEPRECATION = "nested mycroft.conf 'location' shape"


class _QuietUpstreamLocationDeprecation(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return _UPSTREAM_LOCATION_DEPRECATION not in record.getMessage()
        except Exception:  # a record that cannot even render is not ours to drop
            return True


def _quiet(logger: logging.Logger) -> None:
    if not any(isinstance(f, _QuietUpstreamLocationDeprecation) for f in logger.filters):
        logger.addFilter(_QuietUpstreamLocationDeprecation())


def quiet_upstream_location_deprecation() -> None:
    """Drop the library's deprecation about the hub's session location shape."""
    try:
        from ovos_utils.log import LOG
    except ImportError:  # pragma: no cover - the transport cannot run without it
        return
    if getattr(LOG, "_thalovant_quiet_location", False):
        return
    factory = LOG.create_logger  # the bound classmethod, kept as it is

    def create_logger(name: str, tostdout: bool = True) -> logging.Logger:
        logger = factory(name, tostdout)
        if str(name).startswith(str(LOG.name)):
            _quiet(logger)
        return logger

    LOG.create_logger = staticmethod(create_logger)
    for logger in list((getattr(LOG, "_loggers", None) or {}).values()):
        _quiet(logger)
    LOG._thalovant_quiet_location = True


class HiveMindWSSTransport(HiveMindHTTPTransport):
    """Adapter around `hivemind_bus_client.client.HiveMessageBusClient`."""

    def connect(self) -> None:
        quiet_upstream_location_deprecation()
        if self.is_connected():
            return
        endpoint = self.identity.endpoint_for("wss")
        if not endpoint:
            raise ThalovantConnectionError("The identity does not include a WSS endpoint.")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
            raise ThalovantConnectionError("WSS endpoint must start with ws:// or wss://.")
        generation, old = self._reserve_connection()
        self._cleanup_reserved(old)
        client = None
        try:
            deps = self._load_deps()
            wss_client_class = self._build_wss_client_class(deps.HiveMessageBusClient, deps.WebSocketApp)
            from ._noise_runtime import noise_identity, prepare_noise_key
            persistent_identity = noise_identity(self.noise_state_dir)
            prepare_noise_key(persistent_identity)
            client = wss_client_class(
                key=self.identity.access_key, password=self.identity.password, crypto_key=None,
                host=f"{parsed.scheme}://{_endpoint_host(parsed)}",
                port=parsed.port or (443 if parsed.scheme == "wss" else 80),
                useragent=self.useragent, self_signed=self.self_signed,
                compress=self.compress, binarize=self.binarize, identity=persistent_identity,
            )
            protocol = self._build_protocol(client, deps)
            self._install_client(client, generation)
            client.connect(bus=client.internal_bus, protocol=protocol, site_id=self.identity.site_id)
            if not client.connected_event.is_set() or not client.handshake_event.is_set():
                raise ThalovantConnectionError("WSS Noise session closed before readiness.")
            self._finish_connection(client, generation)
        except Exception as exc:
            self._abort_connection(client, generation, exc)
            if isinstance(exc, ThalovantTimeoutError):
                raise
            raise ThalovantConnectionError("HiveMind WSS connect failed.") from exc

    def _close_detached(self, client: Any) -> None:
        self._shutdown_wss_client(client)

    def remove_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None:
        with self._lifecycle_lock:
            client = self._client
        if client is not None:
            client.remove(event_name, handler)

    def send_hive_message(self, message: dict[str, Any], *, encrypt: bool = True) -> Any:
        deps = self._load_deps()
        client = self._require_live_client()
        try:
            return client.emit(deps.HiveMessage(**message))
        except Exception as exc:
            self._fail_current_client(client, exc)
            raise ThalovantConnectionError("Could not send the HiveMind WSS message.") from exc

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
        try:
            return client.emit_mycroft(message)
        except Exception as exc:
            self._fail_current_client(client, exc)
            raise ThalovantConnectionError("Could not send the HiveMind WSS message.") from exc

    def healthcheck(self) -> ThalovantHealth:
        client = self._client
        connected = False
        handshake_complete = False
        transport_alive = False
        if client is not None and self._transport_connected:
            try:
                connected = bool(client.connected_event.is_set())
                handshake_complete = bool(client.handshake_event.is_set())
                transport_alive = connected
            except Exception as exc:
                self._fail_current_client(client, exc)
        error = self.last_error()
        return ThalovantHealth(
            connected=connected,
            handshake_complete=handshake_complete,
            transport_alive=transport_alive,
            last_error=_redact_error_text(error) if error else None,
            connection=self.connection_info(),
        )

    def is_connected(self) -> bool:
        client = self._client
        if client is None or not self._transport_connected:
            return False
        try:
            return bool(client.connected_event.is_set() and client.handshake_event.is_set())
        except Exception:
            return False

    def _require_client(self) -> Any:
        if self._client is None:
            raise ThalovantConnectionError("HiveMind WSS transport is not connected.")
        return self._client

    def _require_live_client(self) -> Any:
        client = self._require_client()
        if not self.is_connected():
            error = self.last_error()
            detail = f": {_redact_error_text(error)}" if error else ""
            raise ThalovantConnectionError(f"HiveMind WSS transport is not connected{detail}")
        return client

    def _shutdown_wss_client(self, client: Any) -> None:
        try:
            client.thalovant_closed = True
        except Exception:
            pass
        try:
            client.handshake_event.clear()
        except Exception:
            pass
        # close() closes the fd and sets keep_running=False, but a run_forever
        # thread already blocked in the handshake recv() (hub accepted the socket
        # and never replied) is not woken by closing the fd -- only shutdown() does
        # that. Without this, each such failed connect() parks a thread forever (#28).
        raw = getattr(getattr(getattr(client, "client", None), "sock", None), "sock", None)
        if raw is not None:
            try:
                raw.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            client.close()
        except Exception:
            pass
        try:
            client.connected_event.clear()
        except Exception:
            pass


class MqttTopicSet(NamedTuple):
    inbound: str
    outbound: str
    status: str


class HiveMindMQTTTransport(_ConnectionLifecycle):
    """MQTT broker-mediated HiveMind transport following hivemind-mqtt-protocol."""

    def __init__(
        self,
        identity: ThalovantIdentity,
        *,
        useragent: str,
        connect_timeout: float = 4.0,
        handshake_timeout: float = 20.0,
        send_timeout: float = 8.0,
        noise_state_dir: str | None = None,
        **_: Any,
    ) -> None:
        self.identity = identity
        self.useragent = useragent
        self.connect_timeout = connect_timeout
        self.handshake_timeout = handshake_timeout
        self.send_timeout = send_timeout
        self.session_id = f"thalovant-python-mqtt-{uuid.uuid4().hex}"
        self.topics = mqtt_topics_for_identity(identity)
        self._init_lifecycle()
        self._client: Any | None = None
        self._connected = threading.Event()
        self._subscribed = threading.Event()
        self._handshake = threading.Event()
        self._last_error: BaseException | None = None
        self._handlers: dict[str, list[Callable[[Any], None]]] = {}
        self._hive_handlers: dict[str, list[Callable[[Any], None]]] = {}
        self.noise_state_dir = noise_state_dir
        self._noise: Any = None
        self._inbound: queue.Queue[bytes | None] = queue.Queue(maxsize=256)
        self._worker: threading.Thread | None = None
        self._connect_started = 0.0
        self._transport_opened = 0.0
        self._connection_info = ThalovantConnectionInfo()

    def connection_info(self) -> ThalovantConnectionInfo:
        return self._connection_info

    def _begin_connection(self, *, close_previous: bool = True) -> None:
        self._last_error = None
        if close_previous and self._noise is not None:
            self._noise.close()
        self._noise = None
        self._connected.clear()
        self._subscribed.clear()
        self._handshake.clear()
        self._connect_started = time.monotonic()
        self._transport_opened = 0.0
        self._connection_info = ThalovantConnectionInfo(
            phase="connecting",
            started_at=_utc_now(),
        )

    def _mark_transport_open(self) -> None:
        if self._transport_opened:
            return
        self._transport_opened = time.monotonic()
        self._connection_info = ThalovantConnectionInfo(
            phase="handshake",
            started_at=self._connection_info.started_at,
            transport_open_ms=_elapsed_ms(self._connect_started, self._transport_opened),
        )

    def _complete_handshake(self) -> None:
        now = time.monotonic()
        opened = self._transport_opened or self._connect_started or now
        started = self._connect_started or opened
        self._connection_info = ThalovantConnectionInfo(
            phase="ready",
            started_at=self._connection_info.started_at,
            connected_at=_utc_now(),
            transport_open_ms=self._connection_info.transport_open_ms,
            socket_open_ms=self._connection_info.socket_open_ms,
            handshake_ms=_elapsed_ms(opened, now),
            connect_ms=_elapsed_ms(started, now),
        )

    def _fail_connection(self, error: BaseException) -> None:
        self._last_error = error
        started = self._connect_started or time.monotonic()
        self._connection_info = ThalovantConnectionInfo(
            phase="error",
            started_at=self._connection_info.started_at,
            transport_open_ms=self._connection_info.transport_open_ms,
            socket_open_ms=self._connection_info.socket_open_ms,
            handshake_ms=self._connection_info.handshake_ms,
            connect_ms=_elapsed_ms(started, time.monotonic()),
            last_error=_redact_error_text(error),
        )

    def _mark_closed(self) -> None:
        self._connection_info = ThalovantConnectionInfo(
            phase="closed",
            started_at=self._connection_info.started_at,
            connected_at=self._connection_info.connected_at,
            transport_open_ms=self._connection_info.transport_open_ms,
            socket_open_ms=self._connection_info.socket_open_ms,
            handshake_ms=self._connection_info.handshake_ms,
            connect_ms=self._connection_info.connect_ms,
            last_error=self._connection_info.last_error,
        )

    def connect(self) -> None:
        if self.is_connected():
            return
        if self.identity.mqtt is None:
            raise ThalovantConnectionError("The identity does not include MQTT broker credentials.")
        parsed = urlparse(self.identity.mqtt.endpoint)
        if parsed.scheme not in {"mqtt", "mqtts", "tcp", "ssl"} or not parsed.hostname:
            raise ThalovantConnectionError("MQTT endpoint must start with mqtt://, mqtts://, tcp://, or ssl://.")
        tls_enabled = _mqtt_tls_enabled(self.identity.mqtt, parsed.scheme)
        if not tls_enabled:
            raise ThalovantConnectionError("HiveMind MQTT requires a broker connection with TLS.")
        generation, old = self._reserve_connection()
        self._cleanup_reserved(old)
        client = None
        try:
            mqtt = self._load_mqtt_module()
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                 client_id=f"thalovant-{uuid.uuid4().hex}", reconnect_on_failure=False)
            client.username_pw_set(self.identity.mqtt.username, self.identity.mqtt.password)
            client.tls_set()
            client.will_set(self.topics.status, "offline", qos=1, retain=True)
            client.on_connect = self._on_connect
            client.on_subscribe = self._on_subscribe
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message
            from ._noise_runtime import NoiseChannel
            channel = NoiseChannel(self.identity, state_dir=self.noise_state_dir,
                pin_id=self.identity.endpoint_base(), hello=self._hello_message(),
                write=lambda payload: self._publish(payload, client=client))
            incoming: queue.Queue[bytes | None] = queue.Queue(maxsize=256)
            worker = threading.Thread(target=self._receive_loop, args=(client, channel, incoming),
                                      daemon=True, name="thalovant-mqtt")
            # Keep connection-owned resources on that client. Cleanup must never
            # read a newer attempt's channel, queue or worker from self.
            client.thalovant_channel = channel
            client.thalovant_inbound = incoming
            client.thalovant_worker = worker
            client.thalovant_disconnected = False
            self._install_client(client, generation)
            with self._lifecycle_lock:
                if self._client is not client:
                    raise ThalovantConnectionError("MQTT connection attempt was cancelled.")
                self._noise, self._inbound, self._worker = channel, incoming, worker
            worker.start()
            client.connect(parsed.hostname, parsed.port or _mqtt_default_port(tls_enabled), keepalive=60)
            with self._lifecycle_lock:
                cancelled = self._client is not client or client.thalovant_disconnected
                if not cancelled:
                    # loop_start is nonblocking; reserve its ownership before
                    # disconnect can stop the network worker.
                    client.loop_start()
            if cancelled:
                # A blocking dial may finish after cancellation's first close.
                # Close that late socket without touching the new generation.
                client.disconnect()
                raise ThalovantConnectionError("MQTT dial completed after cancellation.")
            self._wait_mqtt_event(client, self._connected, self.connect_timeout, "broker connection")
            self._subscribed.clear()
            client.subscribe(self.topics.outbound, qos=self.identity.mqtt.qos)
            self._wait_mqtt_event(client, self._subscribed, self.connect_timeout, "subscription")
            self._publish(json.dumps(self._hello_message()), client=client)
            with self._lifecycle_lock:
                if self._client is client:
                    self._mark_transport_open()
            self._wait_mqtt_event(client, self._handshake, self.handshake_timeout, "Noise handshake")
            with self._lifecycle_lock:
                if self._client is not client:
                    raise ThalovantConnectionError("MQTT connection attempt was cancelled.")
                client.publish(self.topics.status, "online", qos=1, retain=True)
            self._finish_connection(client, generation)
        except Exception as exc:
            self._abort_connection(client, generation, exc)
            raise

    def _wait_mqtt_event(self, client: Any, event: threading.Event, timeout: float, phase: str) -> None:
        deadline = time.monotonic() + timeout
        while True:
            with self._lifecycle_lock:
                if self._client is not client or getattr(client, "thalovant_disconnected", False):
                    raise ThalovantConnectionError("MQTT connection attempt was cancelled or disconnected.")
                if self._last_error is not None:
                    raise ThalovantConnectionError(f"HiveMind MQTT {phase} failed.") from self._last_error
                if event.is_set():
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ThalovantTimeoutError(f"HiveMind MQTT {phase} timed out.")
            event.wait(timeout=min(remaining, 0.05))

    def _close_detached(self, client: Any) -> None:
        client.thalovant_disconnected = True
        incoming = getattr(client, "thalovant_inbound", None)
        if incoming is not None:
            try:
                incoming.put_nowait(None)
            except queue.Full:
                pass
        try:
            client.publish(self.topics.status, "offline", qos=1, retain=True)
        except Exception:
            pass
        try:
            client.disconnect()
            client.loop_stop()
        except Exception:
            pass
        worker = getattr(client, "thalovant_worker", None)
        if worker is not None and worker is not threading.current_thread() and worker.ident is not None:
            worker.join(timeout=self.send_timeout + 1)
        channel = getattr(client, "thalovant_channel", None)
        if channel is not None:
            channel.close()

    def disconnect(self) -> None:
        super().disconnect()
        with self._lifecycle_lock:
            if self._client is None:
                self._noise = None
                self._connected.clear()
                self._subscribed.clear()
                self._handshake.clear()

    def on_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self._handlers.setdefault(event_name, []).append(handler)

    def remove_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self._handlers[event_name] = [
            entry for entry in self._handlers.get(event_name, []) if entry is not handler
        ]

    def on_hive_message(self, msg_type: str, handler: Callable[[Any], None]) -> None:
        self._hive_handlers.setdefault(msg_type, []).append(handler)

    def remove_hive_message(self, msg_type: str, handler: Callable[[Any], None]) -> None:
        self._hive_handlers[msg_type] = [
            entry for entry in self._hive_handlers.get(msg_type, []) if entry is not handler
        ]

    def send_hive_message(self, message: dict[str, Any], *, encrypt: bool = True) -> Any:
        return self._send_hive_message(message)

    def emit_event(
        self,
        event_type: str,
        data: dict[str, Any],
        context: dict[str, Any],
    ) -> Any:
        message = {
            "msg_type": "bus",
            "payload": {
                "type": event_type,
                "data": data,
                "context": _runtime_bus_context(
                    context,
                    useragent=self.useragent,
                    session_id=self.session_id,
                    site_id=self.identity.site_id,
                ),
            },
            "metadata": {},
            "route": [],
            "node": None,
            "target_site_id": None,
            "target_pubkey": None,
            "source_peer": None,
        }
        return self._send_hive_message(message)

    def healthcheck(self) -> ThalovantHealth:
        return ThalovantHealth(
            connected=self._connected.is_set(),
            handshake_complete=self._handshake.is_set(),
            transport_alive=self.is_connected(),
            last_error=_redact_error_text(self._last_error) if self._last_error else None,
            connection=self.connection_info(),
        )

    def is_connected(self) -> bool:
        client = self._client
        try:
            broker_connected = bool(client and client.is_connected())
        except Exception:
            broker_connected = False
        return self._connected.is_set() and self._handshake.is_set() and broker_connected

    def last_error(self) -> BaseException | None:
        return self._last_error

    def _on_connect(self, client: Any, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None) -> None:
        with self._lifecycle_lock:
            if self._client is not client:
                return
            if _reason_code_value(reason_code) != 0:
                self._fail_connection(ThalovantConnectionError(f"HiveMind MQTT connect failed: {reason_code}"))
                return
            self._connected.set()

    def _on_subscribe(self, client: Any, _userdata: Any, _mid: Any, reason_codes: Any, _properties: Any = None) -> None:
        with self._lifecycle_lock:
            if self._client is not client:
                return
            codes = reason_codes if isinstance(reason_codes, (list, tuple)) else [reason_codes]
            failures = [code for code in codes if _reason_code_value(code) >= 128]
            if failures:
                self._fail_connection(ThalovantConnectionError(f"HiveMind MQTT subscribe failed: {failures[0]}"))
                return
            self._subscribed.set()

    def _on_disconnect(self, client: Any, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None) -> None:
        # Paho must remain free to process PUBACKs. Never acquire Noise's lock
        # here: a sender can hold it while waiting for this network thread.
        with self._lifecycle_lock:
            if self._client is not client:
                return
            client.thalovant_disconnected = True
            self._connected.clear(); self._subscribed.clear(); self._handshake.clear()
            if _reason_code_value(reason_code) != 0:
                self._fail_connection(ThalovantConnectionError(f"HiveMind MQTT disconnected: {reason_code}"))
            else:
                self._mark_closed()
            incoming = self._inbound
        try:
            incoming.put_nowait(None)
        except queue.Full:
            pass

    def _on_message(self, client: Any, _userdata: Any, message: Any) -> None:
        with self._lifecycle_lock:
            if client is not self._client or message.topic != self.topics.outbound:
                return
            incoming = self._inbound
        try:
            incoming.put_nowait(bytes(message.payload))
        except queue.Full:
            self._fail_current_client(client, ThalovantConnectionError("HiveMind MQTT receive queue exceeded its bound."))
            client.thalovant_disconnected = True
            with self._lifecycle_lock:
                if self._client is client:
                    self._connected.clear(); self._handshake.clear()
            client.disconnect()

    def _receive_loop(self, client: Any, channel: Any, incoming: Any) -> None:
        try:
            while self._is_current_client(client) and not getattr(client, "thalovant_disconnected", False):
                try:
                    raw = incoming.get(timeout=0.1)
                except queue.Empty:
                    continue
                if raw is None or not self._is_current_client(client) or getattr(client, "thalovant_disconnected", False):
                    return
                try:
                    self._handle_raw_message(raw, client=client, channel=channel)
                except Exception as exc:
                    self._fail_current_client(client, exc)
                    with self._lifecycle_lock:
                        if self._client is client:
                            self._connected.clear(); self._handshake.clear()
                    client.thalovant_disconnected = True
                    client.disconnect()
                    return
        finally:
            channel.close()

    def _handle_raw_message(self, raw: bytes | str, *, client: Any = None, channel: Any = None) -> None:
        channel = channel if channel is not None else self._noise
        if channel is None:
            raise ThalovantConnectionError("MQTT Noise negotiation has not started.")
        message = channel.receive(raw)
        with self._lifecycle_lock:
            if client is not None and (self._client is not client or self._noise is not channel):
                return
            if channel.ready:
                self._handshake.set()
        if message is None:
            return
        msg_type = _message_type_value(message.msg_type)
        payload = message.payload if isinstance(message.payload, dict) else {}
        if msg_type == "hello":
            return
        if msg_type == "bus":
            bus_message = message.payload
            if hasattr(bus_message, "msg_type"):
                event_name = str(bus_message.msg_type)
                data = bus_message.data if isinstance(bus_message.data, dict) else {}
                context = bus_message.context if isinstance(bus_message.context, dict) else {}
            else:
                event_name = str(payload.get("type") or "")
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
            message = _RuntimeBusMessage(
                data=data,
                context=context,
                msg_type=event_name,
            )
            for handler in tuple(self._handlers.get(event_name, ())):
                handler(message)
        elif msg_type in {"query", "cascade"}:
            for handler in tuple(self._hive_handlers.get(msg_type, ())):
                handler(message)

    def _send_hive_message(self, message: dict[str, Any]) -> Any:
        with self._lifecycle_lock:
            client, channel = self._client, self._noise
            if not self.is_connected() or channel is None:
                raise ThalovantConnectionError("HiveMind MQTT transport is not connected.")
        try:
            return channel.send(message)
        except Exception as exc:
            self._fail_current_client(client, exc)
            with self._lifecycle_lock:
                if self._client is client:
                    self._connected.clear(); self._handshake.clear()
            raise

    def _publish(self, payload: str | bytes, *, client: Any = None) -> Any:
        client = self._client if client is None else client
        if client is None or not self._is_current_client(client) or not client.is_connected():
            raise ThalovantConnectionError("HiveMind MQTT broker is not connected.")
        result = client.publish(self.topics.inbound, payload,
            qos=self.identity.mqtt.qos if self.identity.mqtt else 1, retain=False)
        result.wait_for_publish(timeout=self.send_timeout)
        if not self._is_current_client(client) or not result.is_published():
            raise ThalovantTimeoutError("HiveMind MQTT publish timed out or its connection closed.")
        return result

    def _hello_message(self) -> dict[str, Any]:
        return {
            "msg_type": "hello",
            "payload": {
                "pubkey": "",
                "session": {"session_id": self.session_id},
                "site_id": self.identity.site_id,
            },
            "metadata": {},
            "route": [],
            "node": None,
            "target_site_id": None,
            "target_pubkey": None,
            "source_peer": None,
        }

    @staticmethod
    def _load_mqtt_module() -> Any:
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise ThalovantConnectionError("Install paho-mqtt before using MQTT runtime transport.") from exc
        return mqtt


@dataclass(frozen=True)
class _HiveMindDeps:
    HiveMindHTTPClient: Any
    HiveMessageBusClient: Any
    encrypt_as_json: Any
    HiveMessage: Any
    HiveMessageType: Any
    HiveMindSlaveProtocol: Any
    Message: Any
    Session: Any
    serialize_message: Any
    requests: Any
    WebSocketApp: Any


def _endpoint_host(parsed: Any) -> str:
    host = parsed.hostname or parsed.netloc
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return host


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _elapsed_ms(start: float, end: float) -> float:
    return round(max(0.0, end - start) * 1000, 3)


@dataclass(frozen=True)
class _RuntimeBusMessage:
    data: dict[str, Any]
    context: dict[str, Any]
    msg_type: str


def mqtt_topics_for_identity(identity: ThalovantIdentity) -> MqttTopicSet:
    credentials = identity.mqtt
    if credentials is None:
        raise ThalovantConnectionError("The identity does not include MQTT broker credentials.")
    if not credentials.topic_prefix:
        raise ThalovantConnectionError("MQTT credentials must include topic_prefix.")
    prefix = credentials.topic_prefix.strip().strip("/").strip()
    if not prefix:
        raise ThalovantConnectionError("MQTT credentials must include topic_prefix.")
    if any(char in "#+" or ord(char) < 0x20 for char in prefix):
        raise ThalovantConnectionError(
            "MQTT topic_prefix contains characters that are not valid in an MQTT topic."
        )
    return MqttTopicSet(
        inbound=f"{prefix}/in",
        outbound=f"{prefix}/out",
        status=f"{prefix}/status",
    )


def _safe_mqtt_client_id(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "-", value)[:48]
    return normalized or uuid.uuid4().hex


def _mqtt_tls_enabled(credentials: Any, scheme: str) -> bool:
    return bool(getattr(credentials, "tls", False)) or scheme in {"mqtts", "ssl"}


def _mqtt_default_port(tls_enabled: bool) -> int:
    return 8883 if tls_enabled else 1883


def _message_type_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw) if raw else ""


def _reason_code_value(reason_code: Any) -> int:
    try:
        return int(reason_code)
    except (TypeError, ValueError):
        value = getattr(reason_code, "value", 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
