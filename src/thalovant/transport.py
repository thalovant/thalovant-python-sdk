"""HiveMind data-plane transports.

The WebSocket and HTTPS transports are asyncio code, in
:mod:`thalovant._hive`; a client drives those directly. The classes of the
same names here are synchronous handles on them, for code that builds a
transport itself: each runs its asyncio transport on a private event-loop
thread. Handing one to a client as ``transport=`` is the same as letting the
client build it.

``HiveMindMQTTTransport`` is synchronous, on paho-mqtt, which is the
``thalovant[mqtt]`` extra.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import threading
import time
import uuid
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, NamedTuple, Protocol, TypeVar, cast
from urllib.parse import urlparse

from ._hive import AsyncHiveMindHTTPTransport, AsyncHiveMindTransport, AsyncHiveMindWSSTransport
from ._loop import LoopThread as _LoopThread
from ._loop import on_loop_thread as _on_loop_thread
from .errors import (
    ThalovantConnectionError,
    ThalovantRuntimeError,  # noqa: F401 - importable from here, as it always was
    ThalovantTimeoutError,
)
from .events import _runtime_bus_context
from .identity import ThalovantIdentity
from .models import ThalovantConnectionInfo, ThalovantHealth


T = TypeVar("T")

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

    def send_hive_frame(
        self, kind: str, event_type: str, data: dict[str, Any], context: dict[str, Any],
    ) -> Any: ...

    def on_binary(self, handler: Callable[[Any], None]) -> None: ...

    def remove_binary(self, handler: Callable[[Any], None]) -> None: ...

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


class _SyncHiveTransport:
    """A synchronous handle on one asyncio HiveMind transport.

    Handlers registered here run on the handle's own event-loop thread. A
    client given this handle drives the asyncio transport directly instead.
    """

    _async_class: type[AsyncHiveMindTransport] = AsyncHiveMindWSSTransport

    def __init__(self, identity: ThalovantIdentity, **kwargs: Any) -> None:
        self._async_transport = self._async_class(identity, **kwargs)
        self._runner = _LoopThread("thalovant-transport")
        weakref.finalize(self, _abandon, self._runner, self._async_transport)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self.__dict__["_async_transport"], name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Settings such as the timeouts belong to the transport that uses them.
        if name in {"_async_transport", "_runner"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._async_transport, name, value)

    def _loop(self) -> Any:
        """The loop that owns the transport: a client's, when one drives it."""
        owner = self._async_transport.owner_loop
        if owner is not None and owner.is_running() and not owner.is_closed():
            return owner
        return self._runner.loop()

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        loop = self._loop()
        if _on_loop_thread(loop):
            coro.close()
            raise RuntimeError(
                "A synchronous transport call was made from the loop that runs it; "
                "await the asyncio transport there."
            )
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        loop = self._loop()
        if _on_loop_thread(loop):
            return fn(*args)

        async def invoke() -> Any:
            return fn(*args)

        return asyncio.run_coroutine_threadsafe(invoke(), loop).result()

    def connect(self) -> None:
        self._run(self._async_transport.connect())

    def disconnect(self) -> None:
        self._run(self._async_transport.disconnect())

    def _retire_connection(self) -> None:
        self._run(self._async_transport._retire_connection())

    def on_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self._call(self._async_transport.on_mycroft, event_name, handler)

    def remove_mycroft(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self._call(self._async_transport.remove_mycroft, event_name, handler)

    def on_hive_message(self, msg_type: str, handler: Callable[[Any], None]) -> None:
        self._call(self._async_transport.on_hive_message, msg_type, handler)

    def remove_hive_message(self, msg_type: str, handler: Callable[[Any], None]) -> None:
        self._call(self._async_transport.remove_hive_message, msg_type, handler)

    def on_binary(self, handler: Callable[[Any], None]) -> None:
        self._call(self._async_transport.on_binary, handler)

    def remove_binary(self, handler: Callable[[Any], None]) -> None:
        self._call(self._async_transport.remove_binary, handler)

    def emit_event(self, event_type: str, data: dict[str, Any], context: dict[str, Any]) -> Any:
        return self._run(self._async_transport.emit_event(event_type, data, context))

    def send_hive_message(self, message: dict[str, Any], *, encrypt: bool = True) -> Any:
        return self._run(self._async_transport.send_hive_message(message, encrypt=encrypt))

    def send_hive_frame(
        self, kind: str, event_type: str, data: dict[str, Any], context: dict[str, Any]
    ) -> Any:
        return self._run(self._async_transport.send_hive_frame(kind, event_type, data, context))

    def healthcheck(self) -> ThalovantHealth:
        return self._async_transport.healthcheck()

    def connection_info(self) -> ThalovantConnectionInfo:
        return self._async_transport.connection_info()

    def is_connected(self) -> bool:
        return self._async_transport.is_connected()

    def last_error(self) -> BaseException | None:
        return self._async_transport.last_error()

    def session_token(self) -> int:
        return self._async_transport.session_token()


def _abandon(runner: _LoopThread, transport: AsyncHiveMindTransport) -> None:
    """A handle nobody holds any more: close what its own loop left open."""
    if transport.owner_loop is not None and transport.owner_loop is not runner._loop:
        runner.stop()
        return  # a client drives it, on the client's loop
    runner.stop(transport.aclose, timeout=2.0)


class HiveMindHTTPTransport(_SyncHiveTransport):
    """HiveMind v3 over HTTPS polling, with cookie affinity and Noise."""

    _async_class = AsyncHiveMindHTTPTransport

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
        session: Any = None,
    ) -> None:
        super().__init__(
            identity, useragent=useragent, connect_timeout=connect_timeout,
            handshake_timeout=handshake_timeout, handshake_poll_interval=handshake_poll_interval,
            handshake_settle_seconds=handshake_settle_seconds, send_timeout=send_timeout,
            self_signed=self_signed, noise_state_dir=noise_state_dir, session=session,
        )


class HiveMindWSSTransport(_SyncHiveTransport):
    """HiveMind v3 over a WebSocket, with Noise.

    ``compress``, ``binarize`` and the handshake polling settings are accepted
    for compatibility; a v3 session negotiates neither, and the WebSocket
    needs no polling.
    """

    _async_class = AsyncHiveMindWSSTransport

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
        session: Any = None,
    ) -> None:
        super().__init__(
            identity, useragent=useragent, connect_timeout=connect_timeout,
            handshake_timeout=handshake_timeout, send_timeout=send_timeout,
            self_signed=self_signed, noise_state_dir=noise_state_dir, session=session,
        )


class _ConnectionLifecycle:
    """Short state locks; blocking socket operations never hold this lock."""

    _client: Any

    def _begin_connection(self, *, close_previous: bool = True) -> None:
        raise NotImplementedError

    def _close_detached(self, client: Any) -> None:
        raise NotImplementedError

    def _fail_connection(self, error: BaseException) -> None:
        raise NotImplementedError

    def _complete_handshake(self) -> None:
        raise NotImplementedError

    def _mark_closed(self) -> None:
        raise NotImplementedError

    def _init_lifecycle(self) -> None:
        self._lifecycle_lock = threading.RLock()
        self._generation = 0
        self._connecting = False
        self._closing = False
        self._failed_cleanup: tuple[Any, BaseException] | None = None
        # Held here and not on the upstream client, so a reconnect -- which
        # builds a new client -- keeps its subscribers.
        self._binary_handlers: list[Callable[[Any], None]] = []

    def on_binary(self, handler: Callable[[Any], None]) -> None:
        self._binary_handlers.append(handler)

    def remove_binary(self, handler: Callable[[Any], None]) -> None:
        self._binary_handlers = [
            entry for entry in self._binary_handlers if entry is not handler
        ]

    def _deliver_binary(self, kind: str, data: bytes, metadata: dict[str, Any]) -> None:
        from .events import ThalovantBinary

        payload = bytes(data)
        for handler in tuple(self._binary_handlers):
            # A frame each. The dataclass is frozen, but freezing it does not
            # freeze the dict inside it -- one subscriber editing `metadata`
            # would hand the next a value the hub never sent. The bytes are
            # immutable and shared.
            frame = ThalovantBinary(kind=kind, data=payload, metadata=dict(metadata or {}))
            # One subscriber raising must not cost the others their frame, and
            # must not take down the socket's read loop with it.
            try:
                handler(frame)
            except Exception:
                logging.getLogger(__name__).exception(
                    "A binary-frame subscriber raised; continuing."
                )

    def _is_current_client(self, client: Any) -> bool:
        with self._lifecycle_lock:
            return self._client is client

    def session_token(self) -> int:
        """A value that changes exactly when a new underlying session is opened.

        The client re-registers its subscriptions on a new session and leaves a
        session it finds already open alone: the library reconnects on its own
        after a dropped socket, and registering again there would answer every
        event twice.
        """
        with self._lifecycle_lock:
            return self._generation

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
            except BaseException:  # noqa: BLE001 - the primary failure is the one raised
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
            except BaseException:  # noqa: BLE001 - a late completion's cleanup is best effort
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
        except Exception:  # noqa: BLE001 - a record that cannot even render is not ours to drop
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
        logger: logging.Logger = factory(name, tostdout)
        if str(name).startswith(str(LOG.name)):
            _quiet(logger)
        return logger

    LOG.create_logger = staticmethod(create_logger)
    for logger in list((getattr(LOG, "_loggers", None) or {}).values()):
        _quiet(logger)
    LOG._thalovant_quiet_location = True


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
        self._client: Any = None
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
        client: Any = cast(Any, None)
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
        except Exception:  # noqa: BLE001 - best effort: the broker may already be gone
            pass
        try:
            client.disconnect()
            client.loop_stop()
        except Exception:  # noqa: BLE001 - best effort: the broker may already be gone
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

    def send_hive_frame(
        self, kind: str, event_type: str, data: dict[str, Any], context: dict[str, Any],
    ) -> Any:
        # The inner frame is a whole BUS envelope, not a bare bus message: a
        # hub reads `message.payload` of a mesh frame as a HiveMessage of its
        # own and re-stamps the route on it before forwarding.
        return self._send_hive_message({
            "msg_type": kind,
            "payload": {
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
            },
            "metadata": {},
            "route": [],
            "node": None,
            "target_site_id": None,
            "target_pubkey": None,
            "source_peer": None,
        })

    def emit_event(
        self,
        event_type: str,
        data: dict[str, Any],
        context: dict[str, Any],
    ) -> Any:
        message: dict[str, Any] = {
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
        except Exception:  # noqa: BLE001 - a client that cannot say is not connected
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
            self._connected.clear()
            self._subscribed.clear()
            self._handshake.clear()
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
                    self._connected.clear()
                    self._handshake.clear()
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
                except Exception as exc:  # noqa: BLE001 - any failure ends the session, and is reported
                    self._fail_current_client(client, exc)
                    with self._lifecycle_lock:
                        if self._client is client:
                            self._connected.clear()
                            self._handshake.clear()
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
                raw_data, raw_context = bus_message.data, bus_message.context
            else:
                event_name = str(payload.get("type") or "")
                raw_data, raw_context = payload.get("data"), payload.get("context")
            data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
            context: dict[str, Any] = raw_context if isinstance(raw_context, dict) else {}
            message = _RuntimeBusMessage(
                data=data,
                context=context,
                msg_type=event_name,
            )
            for handler in tuple(self._handlers.get(event_name, ())):
                handler(message)
        elif msg_type in ("bin", "binary"):
            # The wire numbers the payload type; name the two a hub actually
            # sends and pass anything else through under its number rather
            # than dropping it, which is what happened to every one of these
            # before: no handler, no branch, no log line.
            self._deliver_binary(
                _BINARY_KINDS.get(_binary_type_value(message), _unnamed_binary(message)),
                bytes(message.payload) if isinstance(message.payload, (bytes, bytearray)) else b"",
                message.metadata if isinstance(getattr(message, "metadata", None), dict) else {},
            )
        elif msg_type in _HIVE_DISPATCHED:
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
                    self._connected.clear()
                    self._handshake.clear()
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
            raise ThalovantConnectionError(
                "Install paho-mqtt before using MQTT runtime transport: pip install 'thalovant[mqtt]'."
            ) from exc
        return mqtt




def _endpoint_host(parsed: Any) -> str:
    host = str(parsed.hostname or parsed.netloc)
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


#: Hive frame kinds this transport hands to `on_hive_message` subscribers.
#:
#: `query` and `cascade` are this client's own request/response traffic; the
#: five after them belong to the mesh and used to fall off the end of the
#: dispatch with no branch and no log line.
_HIVE_DISPATCHED = frozenset({
    "query", "cascade", "broadcast", "propagate", "escalate", "intercom", "rendezvous",
})

#: The binary payload types this SDK gives a name to, by wire number.
_BINARY_KINDS = {
    1: "raw_audio",
    2: "numpy_image",
    3: "file",
    4: "stt_transcribe",
    5: "stt_handle",
    6: "tts_audio",
}


def _binary_type_value(message: Any) -> int:
    raw = getattr(getattr(message, "bin_type", None), "value", getattr(message, "bin_type", None))
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _unnamed_binary(message: Any) -> str:
    """A payload type nobody here has named, kept rather than dropped."""

    return f"binary:{_binary_type_value(message)}"


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
