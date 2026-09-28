"""The HiveMind data plane on asyncio: one authenticated connection to a hub.

Two carriers bring the same negotiation to a hub: a WebSocket, and HTTPS
polling for networks that refuse WebSockets. Either way the hub says HELLO
in the clear, offers its Noise patterns, and after the handshake every frame
in both directions is a Noise transport message; see
:class:`thalovant._noise_runtime.NoiseClientProtocol`.

These classes are the implementation. ``thalovant.transport`` keeps the
synchronous classes of the same names, and the clients drive these directly.
Everything here runs on one event loop and must be called from it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiohttp

from . import _aiohttp
from ._loop import OffLoop
from ._noise_runtime import NoiseClientProtocol, hello_message, noise_identity
from ._wire import BusMessage, HiveMessage, binary_kind_name
from .errors import (
    ThalovantConnectionError,
    ThalovantHubRefusedError,
    ThalovantRuntimeError,
    ThalovantTimeoutError,
)
from .events import ThalovantBinary, _runtime_bus_context
from .identity import ThalovantIdentity
from .models import ThalovantConnectionInfo, ThalovantHealth

__all__ = [
    "AsyncHiveMindHTTPTransport",
    "AsyncHiveMindTransport",
    "AsyncHiveMindWSSTransport",
    "HIVE_DISPATCHED",
    "redact_error_text",
]

log = logging.getLogger("thalovant.transport")

_URL_QUERY_RE = re.compile(r"\?\S+")

#: Hive frame kinds handed to ``on_hive_message`` subscribers. ``query`` and
#: ``cascade`` are this client's own request/response traffic; the five after
#: them belong to the mesh.
HIVE_DISPATCHED = frozenset(
    {"query", "cascade", "broadcast", "propagate", "escalate", "intercom", "rendezvous"}
)

#: A hub closes the socket without a status for an access key it does not
#: know and after a Noise abort, and with 1008 for a malformed authorization.
#: Anything else (1011, 1013, a dropped socket) is the hub's trouble or the
#: network's, not a verdict on the credentials.
_REFUSAL_CODES = frozenset({0, 1000, 1005, 1008})
#: A Noise transport message is at most 64 KiB; a WebSocket frame bigger than
#: this is not one.
_MAX_FRAME = 256 * 1024
#: The WebSocket keepalive: a ping this often, and the link is dropped when
#: its pong has not come back within half of it.
WSS_HEARTBEAT_SECONDS = 20.0

Handler = Callable[[Any], Any]


def redact_error_text(error: object) -> str:
    """An error for humans, with any URL query -- which carries the access key -- cut."""
    return _URL_QUERY_RE.sub("?<redacted>", str(error))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _elapsed_ms(start: float, end: float) -> float:
    return round(max(0.0, end - start) * 1000, 3)


class _Closed(Exception):
    """The carrier closed; ``refused`` says whether that is a verdict."""

    def __init__(self, code: int | None, *, refused: bool) -> None:
        super().__init__(f"closed ({code})")
        self.code = code
        self.refused = refused


class AsyncHiveMindTransport:
    """One hub connection, reconnectable, with subscriptions that outlive it.

    Subclasses supply the carrier. Subscribers are called on the loop; a
    subscriber that raises is logged and does not cost the others their
    message or the connection.
    """

    #: Named in errors and phases: "WSS" or "HTTP".
    carrier_name = "WSS"
    handshake_timeout_message = "HiveMind WSS handshake timed out."

    def __init__(
        self,
        identity: ThalovantIdentity,
        *,
        useragent: str,
        connect_timeout: float = 4.0,
        handshake_timeout: float = 20.0,
        send_timeout: float = 8.0,
        noise_state_dir: str | None = None,
        self_signed: bool = False,
        session: aiohttp.ClientSession | None = None,
        **_: Any,
    ) -> None:
        self.identity = identity
        self.useragent = useragent
        self.connect_timeout = connect_timeout
        self.handshake_timeout = handshake_timeout
        self.send_timeout = send_timeout
        self.noise_state_dir = noise_state_dir
        self.self_signed = self_signed
        self._external_session = session
        self._own_session: aiohttp.ClientSession | None = None
        self._protocol: NoiseClientProtocol | None = None
        self._carrier: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._generation = 0
        self._connecting = False
        self._closing = False
        self._transport_connected = False
        self._failed_cleanup: tuple[Any, BaseException] | None = None
        self._last_error: BaseException | None = None
        self._send_lock: asyncio.Lock | None = None
        self._attempt_task: asyncio.Future[None] | None = None
        self._attempt_stopped: asyncio.Future[None] | None = None
        self._connect_started = 0.0
        self._transport_opened = 0.0
        self._connection_info = ThalovantConnectionInfo()
        self.session_id = self._new_session_id()
        self._bus_handlers: dict[str, list[Handler]] = {}
        self._hive_handlers: dict[str, list[Handler]] = {}
        self._binary_handlers: list[Handler] = []
        #: Set whenever the live connection ends, so a waiter need not poll.
        self.stopped = asyncio.Event()
        self.stopped.set()
        #: The loop this transport runs on, once it has run on one.
        self.owner_loop: asyncio.AbstractEventLoop | None = None

    # -- identity of the connection -------------------------------------------

    @staticmethod
    def _new_session_id() -> str:
        return f"thalovant-python-{uuid.uuid4().hex}"

    def session_token(self) -> int:
        """Changes exactly when a new underlying session opens."""
        return self._generation

    def _pin_id(self) -> str:
        raise NotImplementedError

    def _http_session(self) -> aiohttp.ClientSession:
        if self._external_session is not None:
            return self._external_session
        if self._own_session is None or self._own_session.closed:
            self._own_session = _aiohttp.new_session(cookie_jar=aiohttp.DummyCookieJar())
        return self._own_session

    async def aclose(self) -> None:
        """Retire the connection and the HTTP session the SDK opened for it."""
        with contextlib.suppress(Exception):
            await self.disconnect()
        await self.release_session()

    async def release_session(self) -> None:
        """Close the HTTP session the SDK opened, if no connection is using it.

        The SDK's own session lives as long as one connection: a new one is
        made for the next. Cookies that matter -- the HTTP replica an admission
        belongs to -- are kept on the transport, not in the session.
        """
        if self._transport_connected or self._connecting or self._closing:
            return
        await self._close_owned_session()

    async def _close_owned_session(self) -> None:
        session, self._own_session = self._own_session, None
        if session is not None and not session.closed:
            with contextlib.suppress(Exception):
                await session.close()

    # -- subscriptions ------------------------------------------------------------
    #
    # Bus and hive subscriptions belong to one session, as they did when each
    # session was a new upstream client: a new session starts with none, and
    # the client re-registers what it wants when the session token changes.
    # Binary subscribers belong to the transport and survive a reconnect.

    def _require_session(self) -> None:
        if self._carrier is None:
            raise ThalovantConnectionError(f"HiveMind {self.carrier_name} transport is not connected.")

    def on_mycroft(self, event_name: str, handler: Handler) -> None:
        self._require_session()
        self._bus_handlers.setdefault(event_name, []).append(handler)

    def remove_mycroft(self, event_name: str, handler: Handler) -> None:
        # Cleanup may already have detached the session. Unsubscribing then
        # must not mask the original failure with a connection error.
        self._bus_handlers[event_name] = [
            entry for entry in self._bus_handlers.get(event_name, []) if entry is not handler
        ]

    def on_hive_message(self, msg_type: str, handler: Handler) -> None:
        self._require_session()
        self._hive_handlers.setdefault(msg_type, []).append(handler)

    def remove_hive_message(self, msg_type: str, handler: Handler) -> None:
        self._hive_handlers[msg_type] = [
            entry for entry in self._hive_handlers.get(msg_type, []) if entry is not handler
        ]

    def on_binary(self, handler: Handler) -> None:
        self._binary_handlers.append(handler)

    def remove_binary(self, handler: Handler) -> None:
        self._binary_handlers = [entry for entry in self._binary_handlers if entry is not handler]

    def _deliver(self, message: HiveMessage) -> None:
        kind = message.msg_type
        if kind == "hello":
            return
        if kind == "bus" and isinstance(message.payload, BusMessage):
            self._deliver_bus(message.payload)
        elif (
            kind == "broadcast"
            and isinstance(message.payload, HiveMessage)
            and message.payload.msg_type == "bus"
            and isinstance(message.payload.payload, BusMessage)
            and message.target_site_id
            and message.target_site_id == self.identity.site_id
        ):
            # A master's broadcast aimed at this site is taken in like BUS.
            self._deliver_bus(message.payload.payload)
        elif kind == "bin":
            payload = bytes(message.payload) if isinstance(message.payload, (bytes, bytearray)) else b""
            name = binary_kind_name(message.bin_type)
            for handler in tuple(self._binary_handlers):
                # A frame each: one subscriber editing ``metadata`` must not
                # hand the next a value the hub never sent.
                self._call(handler, ThalovantBinary(name, payload, dict(message.metadata)))
            return
        for handler in tuple(self._hive_handlers.get(kind, ())):
            self._call(handler, message)

    def _deliver_bus(self, payload: BusMessage) -> None:
        receive_bus(payload)
        for handler in tuple(self._bus_handlers.get(payload.msg_type, ())):
            self._call(handler, payload)

    @staticmethod
    def _call(handler: Handler, value: Any) -> None:
        if type(handler) is OffLoop:
            handler.post(value)
            return
        try:
            result = handler(value)
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)
        except Exception:
            log.exception("A subscriber raised; continuing.")

    # -- state ------------------------------------------------------------------------

    def connection_info(self) -> ThalovantConnectionInfo:
        return self._connection_info

    def is_connected(self) -> bool:
        carrier = self._carrier
        protocol = self._protocol
        return bool(
            carrier is not None
            and self._transport_connected
            and protocol is not None
            and protocol.ready
            and carrier.alive()
        )

    def last_error(self) -> BaseException | None:
        return self._last_error

    def healthcheck(self) -> ThalovantHealth:
        carrier = self._carrier
        connected = bool(carrier is not None and self._transport_connected and carrier.open_ok())
        handshake = bool(connected and self._protocol is not None and self._protocol.ready)
        alive = bool(connected and carrier is not None and carrier.alive())
        error = self._last_error
        return ThalovantHealth(
            connected=connected,
            handshake_complete=handshake,
            transport_alive=alive,
            last_error=redact_error_text(error) if error else None,
            connection=self._connection_info,
        )

    def _begin_connection(self) -> None:
        self._last_error = None
        self._connect_started = time.monotonic()
        self._transport_opened = 0.0
        self._connection_info = ThalovantConnectionInfo(phase="connecting", started_at=_utc_now())

    def _mark_transport_open(self, *, socket: bool) -> None:
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
            last_error=redact_error_text(error),
        )

    def _mark_closed(self) -> None:
        info = self._connection_info
        self._connection_info = ThalovantConnectionInfo(
            phase="closed",
            started_at=info.started_at,
            connected_at=info.connected_at,
            transport_open_ms=info.transport_open_ms,
            socket_open_ms=info.socket_open_ms,
            handshake_ms=info.handshake_ms,
            connect_ms=info.connect_ms,
            last_error=info.last_error,
        )

    # -- lifecycle ------------------------------------------------------------------

    def _new_carrier(self) -> Any:
        raise NotImplementedError

    def _connect_error(self, error: BaseException) -> BaseException:
        """What a failed connect raises, for this carrier."""
        raise NotImplementedError

    def _preflight(self) -> None:
        """Refuse an endpoint this transport cannot use, before anything starts."""

    def _own(self) -> None:
        """Bind this transport to the running loop, rebuilding what belongs to a loop."""
        loop = asyncio.get_running_loop()
        if self.owner_loop is loop:
            return
        previous = self.owner_loop
        if previous is not None and previous.is_running() and not previous.is_closed() and (
            self._carrier is not None or self._connecting or self._closing
        ):
            raise ThalovantConnectionError(
                "This transport is in use on another event loop; use one transport per loop."
            )
        self.owner_loop = loop
        stopped = asyncio.Event()
        if self.stopped.is_set():
            stopped.set()
        self.stopped = stopped
        self._send_lock = None
        if previous is not None and self._own_session is not None:
            self._own_session = None  # bound to the old loop; a new one is made on demand

    async def connect(self) -> None:
        self._own()
        if self.is_connected():
            return
        self._preflight()
        if self._connecting or self._closing:
            raise ThalovantConnectionError("A connection lifecycle operation is already in progress.")
        if self._failed_cleanup is not None:
            raise ThalovantConnectionError(
                "Previous connection cleanup failed; retry disconnect before reconnecting."
            ) from None
        self._connecting = True
        self._generation += 1
        generation = self._generation
        old, self._carrier = self._carrier, None
        self._transport_connected = False
        self._bus_handlers = {}
        self._hive_handlers = {}
        self._begin_connection()
        # The attempt runs as a task of its own so that disconnect() can stop
        # it wherever it is -- in DNS, the TLS handshake, or waiting on a hub
        # that accepted the socket and never said HELLO.
        attempt = asyncio.ensure_future(self._attempt(old, generation))
        self._attempt_task = attempt
        try:
            await attempt
        except asyncio.CancelledError:
            if self._attempt_stopped is attempt:
                raise self._connect_error(
                    ThalovantConnectionError("Connection attempt was cancelled.")
                ) from None
            raise
        finally:
            if self._attempt_task is attempt:
                self._attempt_task = None
            if self._attempt_stopped is attempt:
                self._attempt_stopped = None

    async def _attempt(self, old: Any, generation: int) -> None:
        carrier = None
        try:
            if old is not None:
                self._closing = True
                try:
                    await self._close_carrier_once(old)
                finally:
                    self._closing = False
            self._check(generation)
            carrier = self._new_carrier()
            self._carrier = carrier
            if self._protocol is None:
                self._protocol = NoiseClientProtocol(
                    store=noise_identity(self.noise_state_dir),
                    pin_id=self._pin_id(),
                    hello=hello_message(self.session_id, self.identity.site_id),
                    password=self.identity.password,
                    access_key=self.identity.access_key,
                )
            self.session_id = self._new_session_id()
            self._protocol.hello = hello_message(self.session_id, self.identity.site_id)
            self._protocol.reset()
            await self._handshake(carrier, generation)
            self._check(generation)
            self._connecting = False
            self._transport_connected = True
            self._complete_handshake()
            self.stopped.clear()
            carrier.start()
            self._reader = asyncio.ensure_future(self._read(carrier, generation))
        except BaseException as exc:
            await self._abort_connection(carrier, generation, exc)
            if isinstance(exc, (asyncio.CancelledError, ThalovantTimeoutError)):
                raise
            raise self._connect_error(exc) from exc

    def _check(self, generation: int) -> None:
        if generation != self._generation or not self._connecting:
            raise ThalovantConnectionError("Connection attempt was cancelled.")

    async def _handshake(self, carrier: Any, generation: int) -> None:
        protocol = self._protocol
        assert protocol is not None
        loop = asyncio.get_running_loop()
        await carrier.open(self.connect_timeout)
        self._check(generation)
        self._mark_transport_open(socket=carrier.is_socket)
        deadline = loop.time() + self.handshake_timeout
        phase = "hello"
        while not protocol.ready:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ThalovantTimeoutError(self.handshake_timeout_message)
            try:
                raw = await carrier.receive(remaining, handshake=True)
            except asyncio.TimeoutError:
                raise ThalovantTimeoutError(self.handshake_timeout_message) from None
            except _Closed as closed:
                if closed.refused and phase != "offer":
                    raise ThalovantHubRefusedError(
                        "The hub refused this connection's credentials."
                    ) from None
                raise ThalovantConnectionError(
                    f"HiveMind {self.carrier_name} closed before handshake completed ({closed.code})."
                ) from None
            self._check(generation)
            if raw is None:
                continue
            step = protocol.receive(raw)
            if protocol.server_hello is not None and phase == "hello":
                phase = "offer"
            if step.need_psk is not None:
                phase = "response"
                # argon2id: about a tenth of a second of CPU, off the loop.
                psk = await loop.run_in_executor(None, protocol.derive_psk)
                self._check(generation)
                step = protocol.provide_psk(psk)
            elif step.send and not protocol.ready:
                phase = "response"
            for frame in step.send:
                await carrier.send(frame, self.send_timeout)
            self._check(generation)
            for message in step.messages:
                self._deliver(message)

    async def _read(self, carrier: Any, generation: int) -> None:
        protocol = self._protocol
        assert protocol is not None
        error: BaseException | None = None
        try:
            while generation == self._generation:
                try:
                    raw = await carrier.receive(None, handshake=False)
                except _Closed as closed:
                    error = ThalovantConnectionError(
                        f"HiveMind {self.carrier_name} connection closed ({closed.code})."
                    )
                    break
                if raw is None:
                    continue
                step = protocol.receive(raw)
                for message in step.messages:
                    self._deliver(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc
        finally:
            if generation == self._generation and self._transport_connected:
                self._transport_connected = False
                if error is not None:
                    self._fail_connection(error)
                else:
                    self._mark_closed()
                self.stopped.set()
                with contextlib.suppress(Exception):
                    await carrier.close_socket()
                await self._close_owned_session()

    async def _abort_connection(self, carrier: Any, generation: int, error: BaseException) -> None:
        if generation == self._generation:
            self._carrier = None
            self._connecting = False
            self._transport_connected = False
            if not isinstance(error, asyncio.CancelledError):
                self._fail_connection(error)
            self.stopped.set()
        if carrier is not None:
            # Keep the primary failure; a failed cleanup stays retained for an
            # explicit retry through disconnect().
            with contextlib.suppress(BaseException):
                await self._close_carrier_once(carrier)
        if generation == self._generation:
            await self.release_session()

    async def _close_carrier_once(self, carrier: Any, *, retry_failed: bool = False) -> None:
        """Close *carrier* once; a concurrent caller waits for the same close.

        A failed close is retained with its carrier -- an HTTP admission the
        hub still holds -- and only an explicit ``disconnect()`` retries it.
        """
        task: asyncio.Future[None] | None = carrier.cleanup_task
        if task is not None:
            if not task.done():
                await asyncio.shield(task)
                return
            if carrier.cleanup_error is None:
                return
            if not retry_failed:
                raise carrier.cleanup_error
        task = asyncio.ensure_future(self._run_cleanup(carrier))
        task.add_done_callback(_consume)
        carrier.cleanup_task = task
        await asyncio.shield(task)

    async def _run_cleanup(self, carrier: Any) -> None:
        try:
            await carrier.close()
        except BaseException as error:
            carrier.cleanup_error = error
            self._failed_cleanup = (carrier, error)
            self._transport_connected = False
            self._fail_connection(error)
            raise
        carrier.cleanup_error = None
        if self._failed_cleanup is not None and self._failed_cleanup[0] is carrier:
            self._failed_cleanup = None

    async def disconnect(self) -> None:
        self._own()
        await self._disconnect(retry_failed=True)

    async def _retire_connection(self) -> None:
        """Automatic cancellation never silently retries a failed admission."""
        self._own()
        await self._disconnect(retry_failed=False)

    def _stop_attempt(self) -> None:
        attempt = self._attempt_task
        if attempt is not None and not attempt.done() and attempt is not asyncio.current_task():
            self._attempt_stopped = attempt
            attempt.cancel()

    async def _disconnect(self, *, retry_failed: bool) -> None:
        self._stop_attempt()
        if self._closing:
            # An existing cleanup owns the old connection; invalidate an
            # attempt waiting on it without running it twice.
            self._generation += 1
            self._connecting = False
            if retry_failed:
                raise ThalovantConnectionError("Connection cleanup is already in progress.")
            return
        carrier = self._failed_cleanup[0] if self._failed_cleanup is not None else self._carrier
        self._carrier = None
        self._generation += 1
        self._connecting = False
        self._transport_connected = False
        self._closing = carrier is not None
        reader, self._reader = self._reader, None
        try:
            if reader is not None and not reader.done() and reader is not asyncio.current_task():
                reader.cancel()
                with contextlib.suppress(BaseException):
                    await reader
            if carrier is not None:
                await self._close_carrier_once(carrier, retry_failed=retry_failed)
            self._mark_closed()
        finally:
            self._closing = False
            self.stopped.set()
            await self.release_session()

    # -- sending --------------------------------------------------------------------

    def _require_live(self) -> tuple[Any, NoiseClientProtocol]:
        carrier, protocol = self._carrier, self._protocol
        if carrier is None or protocol is None or not self.is_connected():
            error = self.last_error()
            detail = f": {redact_error_text(error)}" if error else ""
            raise ThalovantConnectionError(
                f"HiveMind {self.carrier_name} transport is not connected{detail}"
            )
        return carrier, protocol

    async def _send(self, message: dict[str, Any]) -> Any:
        carrier, protocol = self._require_live()
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        # Shielded: a caller cancelled half-way must not leave half a chunked
        # message on the wire, or the next sender's frames out of nonce order.
        write = asyncio.ensure_future(self._write(carrier, protocol, message))
        write.add_done_callback(_consume)
        return await asyncio.shield(write)

    async def _write(self, carrier: Any, protocol: NoiseClientProtocol, message: dict[str, Any]) -> Any:
        assert self._send_lock is not None
        async with self._send_lock:
            if carrier is not self._carrier:
                raise ThalovantConnectionError(
                    f"HiveMind {self.carrier_name} transport is not connected"
                )
            try:
                result = None
                for frame in protocol.seal(message):
                    result = await carrier.send(frame, self.send_timeout)
                return result
            except (ThalovantConnectionError, ThalovantTimeoutError) as error:
                self._fail_live(carrier, error)
                raise
            except Exception as error:
                self._fail_live(carrier, error)
                raise ThalovantConnectionError(
                    f"Could not send the HiveMind {self.carrier_name} message."
                ) from error

    def _fail_live(self, carrier: Any, error: BaseException) -> None:
        if carrier is self._carrier:
            self._transport_connected = False
            self._fail_connection(error)
            self.stopped.set()
            asyncio.ensure_future(carrier.close_socket()).add_done_callback(_consume)

    def _bus_frame(self, event_type: str, data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return {
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

    async def emit_event(self, event_type: str, data: dict[str, Any], context: dict[str, Any]) -> Any:
        return await self._send(self._bus_frame(event_type, data, context))

    async def send_hive_message(self, message: dict[str, Any], *, encrypt: bool = True) -> Any:
        # The flag stays for source compatibility; v3 application traffic is
        # always encrypted and cannot bypass the authenticated channel.
        return await self._send(dict(message))

    async def send_hive_frame(
        self, kind: str, event_type: str, data: dict[str, Any], context: dict[str, Any]
    ) -> Any:
        # Nested, because that is what a hub reads: it takes ``payload`` of a
        # mesh frame as a HiveMessage of its own, re-stamps the route on it and
        # forwards *that*. A flat frame would lose the route.
        inner = self._bus_frame(event_type, data, context)
        return await self._send(
            {
                "msg_type": kind,
                "payload": inner,
                "metadata": {},
                "route": [],
                "node": None,
                "target_site_id": None,
                "target_pubkey": None,
                "source_peer": None,
            }
        )


#: A verified-origin claim only this node may make; see :func:`receive_bus`.
VERIFIED_SOURCE_PEER_KEY = "hivemind_verified_source_peer"


def receive_bus(message: BusMessage) -> BusMessage:
    """Take a bus message from the hub in, as a HiveMind node does (HIVEMIND-BRIDGE-1 §3.1).

    - A claim that the message was verified as coming from some peer is
      dropped: nothing on this path verifies a signature, so a sender could
      otherwise set it and have it believed.
    - ``destination`` becomes ``source``: from here on the message is this
      node's own, as a satellite's local bus sees it.

    The context as it arrived is kept in ``wire_context``, which is what a
    reply is routed from.
    """
    context = message.context
    if message.wire_context is None:
        message.wire_context = copy.deepcopy(context)
    context.pop(VERIFIED_SOURCE_PEER_KEY, None)
    if "destination" in context:
        context["source"] = context.pop("destination")
    return message


def _consume(future: asyncio.Future[Any]) -> None:
    """Retrieve an outcome nobody awaits any more."""
    if not future.cancelled():
        future.exception()


# -- WebSocket ---------------------------------------------------------------------------


class _WSSCarrier:
    is_socket = True

    def __init__(self, transport: AsyncHiveMindWSSTransport) -> None:
        self._transport = transport
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self.cleanup_task: asyncio.Future[None] | None = None
        self.cleanup_error: BaseException | None = None

    def open_ok(self) -> bool:
        return self._ws is not None and not self._ws.closed

    def alive(self) -> bool:
        return self.open_ok()

    def start(self) -> None:
        """Nothing to start: the socket delivers frames as they come."""

    async def open(self, timeout: float) -> None:
        transport = self._transport
        try:
            self._ws = await asyncio.wait_for(
                transport._http_session().ws_connect(
                    transport._authorized_wss_url(),
                    heartbeat=transport.heartbeat,
                    autoping=True,
                    max_msg_size=_MAX_FRAME,
                    ssl=_aiohttp.client_ssl(self_signed=transport.self_signed),
                    timeout=aiohttp.ClientWSTimeout(ws_close=min(10.0, transport.send_timeout)),
                    headers={"User-Agent": transport.useragent},
                ),
                timeout,
            )
        except asyncio.TimeoutError:
            raise ThalovantConnectionError("Could not reach the hub (timed out opening the socket).") from None
        except aiohttp.WSServerHandshakeError as err:
            # Never chain the cause: its text carries the URL, and the URL
            # carries the access key.
            if err.status in (401, 403):
                raise ThalovantHubRefusedError(
                    f"The hub refused this connection's credentials (HTTP {err.status})."
                ) from None
            raise ThalovantConnectionError(
                f"The hub refused the WebSocket upgrade (HTTP {err.status})."
            ) from None
        except (aiohttp.ClientError, OSError) as err:
            raise ThalovantConnectionError(f"Could not reach the hub ({type(err).__name__}).") from None

    async def receive(self, timeout: float | None, *, handshake: bool) -> str | bytes | None:
        ws = self._ws
        if ws is None:
            raise _Closed(None, refused=False)
        if timeout is None:
            message = await ws.receive()
        else:
            message = await asyncio.wait_for(ws.receive(), timeout)
        if message.type is aiohttp.WSMsgType.TEXT:
            return str(message.data)
        if message.type is aiohttp.WSMsgType.BINARY:
            return bytes(message.data)
        code = message.data if message.type is aiohttp.WSMsgType.CLOSE else ws.close_code
        if message.type is aiohttp.WSMsgType.ERROR:
            code = None
        raise _Closed(code, refused=code in _REFUSAL_CODES)

    async def send(self, frame: str | bytes, timeout: float) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise ThalovantConnectionError("HiveMind WSS transport is not connected.")
        try:
            if isinstance(frame, str):
                await asyncio.wait_for(ws.send_str(frame), timeout)
            else:
                await asyncio.wait_for(ws.send_bytes(frame), timeout)
        except asyncio.TimeoutError:
            raise ThalovantTimeoutError("HiveMind WSS send timed out.") from None
        except (aiohttp.ClientError, OSError, RuntimeError) as err:
            raise ThalovantConnectionError(f"HiveMind WSS send failed: {type(err).__name__}") from None

    async def close_socket(self) -> None:
        ws = self._ws
        if ws is not None and not ws.closed:
            await ws.close()

    async def close(self) -> None:
        await self.close_socket()


class AsyncHiveMindWSSTransport(AsyncHiveMindTransport):
    """HiveMind v3 over a WebSocket, on ``aiohttp``."""

    carrier_name = "WSS"

    def __init__(self, identity: ThalovantIdentity, **kwargs: Any) -> None:
        self.heartbeat = float(kwargs.pop("heartbeat", WSS_HEARTBEAT_SECONDS))
        super().__init__(identity, **kwargs)

    def _endpoint(self) -> Any:
        endpoint = self.identity.endpoint_for("wss")
        if not endpoint:
            raise ThalovantConnectionError("The identity does not include a WSS endpoint.")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
            raise ThalovantConnectionError("WSS endpoint must start with ws:// or wss://.")
        return parsed

    def _pin_id(self) -> str:
        # hivemind-bus-client pinned the hub's key under "<scheme>://<host>:<port>";
        # a device that upgrades keeps the pin it already has.
        parsed = self._endpoint()
        host = parsed.hostname or parsed.netloc
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        return f"{parsed.scheme}://{host}:{port}"

    def _preflight(self) -> None:
        self._endpoint()

    def _authorized_wss_url(self, *, key: str | None = None, useragent: str | None = None) -> str:
        parsed = self._endpoint()
        key = self.identity.access_key if key is None else key
        useragent = self.useragent if useragent is None else useragent
        authorization = base64.b64encode(f"{useragent}:{key}".encode("utf-8")).decode("ascii")
        query = [
            item
            for item in parse_qsl(parsed.query, keep_blank_values=True)
            if item[0] != "authorization"
        ]
        query.append(("authorization", authorization))
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "", "", urlencode(query), ""))

    def _new_carrier(self) -> _WSSCarrier:
        self._endpoint()
        return _WSSCarrier(self)

    def _connect_error(self, error: BaseException) -> BaseException:
        if isinstance(error, ThalovantHubRefusedError):
            return ThalovantHubRefusedError("HiveMind WSS connect failed: the hub refused the credentials.")
        return ThalovantConnectionError("HiveMind WSS connect failed.")


# -- HTTPS polling ------------------------------------------------------------------------


class _HTTPCarrier:
    """Cookie-affine HTTPS carrier: admission, polling, sends, and cleanup."""

    is_socket = False

    def __init__(self, transport: AsyncHiveMindHTTPTransport) -> None:
        self._transport = transport
        self.base_url = transport.identity.endpoint_base()
        self._auth = base64.b64encode(
            f"{transport.useragent}:{transport.identity.access_key}".encode()
        ).decode("ascii")
        self._admitted = False
        self._connected = False
        self._queue: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self._poller: asyncio.Task[None] | None = None
        self._deadline: float | None = None
        self._closed = False
        self._stopped = False
        self._request_lock = asyncio.Lock()
        self._admission: asyncio.Future[dict[str, Any]] | None = None
        self.cleanup_task: asyncio.Future[None] | None = None
        self.cleanup_error: BaseException | None = None

    def open_ok(self) -> bool:
        return self._connected and not self._stopped

    def alive(self) -> bool:
        return bool(self.open_ok() and (self._poller is None or not self._poller.done()))

    def start(self) -> None:
        """Begin polling for what the hub has queued for this session."""
        if self._poller is None:
            self._deadline = None
            self._poller = asyncio.ensure_future(self._poll_forever())

    async def request(self, path: str, *, method: str = "GET", data: dict[str, str] | None = None) -> dict[str, Any]:
        transport = self._transport
        loop = asyncio.get_running_loop()
        async with self._request_lock:
            remaining = transport.send_timeout
            if self._deadline is not None:
                remaining = min(remaining, self._deadline - loop.time())
            if remaining <= 0:
                raise ThalovantTimeoutError("HiveMind HTTP Noise handshake timed out.")
            headers = {"User-Agent": transport.useragent}
            if transport._replica_cookie:
                headers["Cookie"] = transport._replica_cookie
            try:
                async with transport._http_session().request(
                    method,
                    f"{self.base_url}{path}",
                    params={"authorization": self._auth},
                    data=data,
                    headers=headers,
                    ssl=_aiohttp.client_ssl(self_signed=transport.self_signed),
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=remaining),
                ) as response:
                    status = response.status
                    for value in response.headers.getall("Set-Cookie", []):
                        cookie = value.split(";", 1)[0].strip()
                        if cookie.startswith("hivemind_http_replica="):
                            transport._replica_cookie = cookie
                    text = await response.text()
            except asyncio.TimeoutError:
                detail = (
                    "HiveMind HTTP Noise handshake timed out."
                    if self._deadline is not None
                    else "HiveMind HTTP request timed out."
                )
                raise ThalovantTimeoutError(detail) from None
            except (aiohttp.ClientError, OSError):
                raise ThalovantConnectionError("HiveMind HTTP request failed.") from None
        if 300 <= status < 400:
            raise ThalovantConnectionError("HiveMind HTTP endpoint redirected the request.")
        if not 200 <= status < 300:
            raise ThalovantConnectionError(f"HiveMind HTTP request failed with HTTP {status}.")
        try:
            body = json.loads(text)
        except ValueError:
            raise ThalovantConnectionError("Invalid HiveMind HTTP response.") from None
        if not isinstance(body, dict):
            raise ThalovantConnectionError("Invalid HiveMind HTTP response.")
        # The upstream /disconnect handler answers this exactly when an
        # earlier successful acknowledgment was lost.
        already = path == "/disconnect" and body == {"error": "Already Disconnected"}
        if body.get("error") and not already:
            raise ThalovantConnectionError("HiveMind HTTP request was refused.")
        return body

    async def _admit(self) -> dict[str, Any]:
        body = await self.request("/connect", method="POST")
        self._admitted = True
        return body

    async def open(self, timeout: float) -> None:
        transport = self._transport
        loop = asyncio.get_running_loop()
        self._deadline = loop.time() + transport.connect_timeout + transport.handshake_timeout
        # Shielded: once the hub may have admitted this client, the answer is
        # read whatever happens to the caller, so cleanup knows there is an
        # admission to release rather than leaving one on the hub.
        self._admission = asyncio.ensure_future(self._admit())
        self._admission.add_done_callback(_consume)
        body = await asyncio.shield(self._admission)
        if body.get("status") not in (None, "Connected"):
            raise ThalovantConnectionError("Invalid HiveMind HTTP admission response.")
        self._connected = True

    async def _poll(self) -> None:
        messages = (await self.request("/get_messages")).get("messages")
        if not isinstance(messages, list):
            raise ThalovantConnectionError("Invalid HiveMind HTTP message response.")
        for raw in messages:
            if not isinstance(raw, str):
                raise ThalovantConnectionError("Invalid HiveMind HTTP cleartext frame.")
            self._queue.put_nowait(raw)
        protocol = self._transport._protocol
        if protocol is not None and protocol.session is not None:
            frames = (await self.request("/get_binary_messages")).get("b64_messages")
            if not isinstance(frames, list):
                raise ThalovantConnectionError("Invalid HiveMind HTTP binary response.")
            for raw in frames:
                if not isinstance(raw, str):
                    raise ThalovantConnectionError("Invalid HiveMind HTTP binary frame.")
                try:
                    self._queue.put_nowait(base64.b64decode(raw, validate=True))
                except ValueError:
                    raise ThalovantConnectionError("Invalid HiveMind HTTP binary frame.") from None

    async def _poll_forever(self) -> None:
        interval = self._transport.handshake_poll_interval
        try:
            while not self._stopped:
                await asyncio.sleep(interval)
                await self._poll()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._stopped = True
            self._queue.put_nowait(None)
            self._transport._poll_error = exc

    async def receive(self, timeout: float | None, *, handshake: bool) -> str | bytes | None:
        if handshake:
            if self._queue.empty():
                await self._poll()
            if self._queue.empty():
                await asyncio.sleep(min(self._transport.handshake_poll_interval, max(timeout or 0, 0)))
                return None
            return self._queue.get_nowait()
        raw = await self._queue.get()
        if raw is None:
            error = getattr(self._transport, "_poll_error", None)
            if error is not None:
                raise error
            raise _Closed(None, refused=False)
        return raw

    async def send(self, frame: str | bytes, timeout: float) -> Any:
        data = (
            {"message": frame}
            if isinstance(frame, str)
            else {"message": base64.b64encode(frame).decode("ascii"), "binary": "1"}
        )
        return await self.request("/send_message", method="POST", data=data)

    async def close_socket(self) -> None:
        self._stopped = True
        poller = self._poller
        if poller is not None and not poller.done() and poller is not asyncio.current_task():
            poller.cancel()
            with contextlib.suppress(BaseException):
                await poller
        self._connected = False
        self._queue.put_nowait(None)

    async def close(self) -> None:
        await self.close_socket()
        admission = self._admission
        if admission is not None and not admission.done():
            # Wait for the admission it may have been given, then release it.
            await asyncio.wait({admission})
        if self._admitted:
            loop = asyncio.get_running_loop()
            try:
                self._deadline = loop.time() + min(2.0, self._transport.send_timeout)
                reply = await self.request("/disconnect", method="POST")
                if reply != {"error": "Already Disconnected"} and (
                    reply.get("status") != "Disconnected" or reply.get("ok") is False
                ):
                    raise ThalovantConnectionError("Invalid disconnect acknowledgment.")
            except Exception:
                # Keep this admission and its replica cookie for an explicit
                # retry. Never log what the server or the error said.
                raise ThalovantConnectionError(
                    "HiveMind HTTP disconnect was not acknowledged; admission is retained."
                ) from None
            finally:
                self._deadline = None
            self._admitted = False
        self._closed = True


class AsyncHiveMindHTTPTransport(AsyncHiveMindTransport):
    """HiveMind v3 over HTTPS polling, on ``aiohttp``."""

    carrier_name = "HTTP"
    handshake_timeout_message = "HiveMind HTTP Noise handshake timed out."

    def __init__(self, identity: ThalovantIdentity, **kwargs: Any) -> None:
        self.handshake_poll_interval = float(kwargs.pop("handshake_poll_interval", 0.1))
        self.handshake_settle_seconds = float(kwargs.pop("handshake_settle_seconds", 0.1))
        kwargs.pop("compress", None)
        kwargs.pop("binarize", None)
        super().__init__(identity, **kwargs)
        #: The replica that admitted this client. Kept across reconnects: a
        #: new session must return to the replica whose admission it owns.
        self._replica_cookie = ""
        self._poll_error: BaseException | None = None

    def _pin_id(self) -> str:
        return self.identity.endpoint_base()

    def _preflight(self) -> None:
        scheme = urlparse(self.identity.endpoint_base()).scheme
        if scheme != "https":
            raise ThalovantConnectionError(
                f"Refusing to use the HTTP transport over {scheme or 'no'}://. "
                "It needs an https:// endpoint: without TLS every message and the "
                "access key travel in the clear."
            )

    def _new_carrier(self) -> _HTTPCarrier:
        self._preflight()
        self._poll_error = None
        return _HTTPCarrier(self)

    def _raise_for_emit_response(self, response: Any) -> None:
        try:
            raise_for_emit_response(response)
        except ThalovantConnectionError as error:
            self._last_error = error
            raise

    def _connect_error(self, error: BaseException) -> BaseException:
        if isinstance(error, ThalovantHubRefusedError):
            return ThalovantHubRefusedError(
                "Could not establish the HiveMind HTTP Noise session: the hub refused the credentials."
            )
        return ThalovantConnectionError("Could not establish the HiveMind HTTP Noise session.")


def raise_for_emit_response(response: Any) -> None:
    """Map an HTTP send response onto the SDK's errors (kept for callers)."""
    status_code = getattr(response, "status_code", None)
    try:
        body = response.json()
    except Exception:
        body = {}
    error = body.get("error") if isinstance(body, dict) else None
    if error:
        redacted = redact_error_text(error)
        if "not connected" in str(error).lower():
            raise ThalovantConnectionError(f"HiveMind HTTP send failed: {redacted}")
        raise ThalovantRuntimeError(f"HiveMind HTTP send failed: {redacted}")
    if getattr(response, "ok", False) is False:
        detail = redact_error_text(getattr(response, "text", "")) or f"HTTP {status_code or 'error'}"
        raise ThalovantRuntimeError(f"HiveMind HTTP send failed: {detail}")
