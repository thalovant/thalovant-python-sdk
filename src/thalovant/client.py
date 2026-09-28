"""High-level clients: the asyncio core and the synchronous client over it.

:class:`AsyncThalovantClient` is the implementation. Everything a hub
conversation needs lives there, on one event loop: the connect lifecycle with
its single deadline, the ask and query collectors with their settle, empty
and carry windows, the refusal attribution, event streams, and the memory of
each conversation's state.

:class:`ThalovantClient` keeps every name and behaviour it always had. It runs
an :class:`AsyncThalovantClient` on a private event-loop thread and waits for
it. Its handlers run on a thread of their own, in arrival order, so a handler
may call the client -- emit, ask -- without waiting on itself.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import random
import threading
import time
import weakref
from collections import OrderedDict, deque
from contextlib import contextmanager
from pathlib import Path
from typing import (
    Any,
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
    TypeVar,
)
from urllib.parse import urlparse

from ._link import BINARY as _BINARY
from ._link import BUS as _BUS
from ._link import HIVE as _HIVE
from ._link import NativeLink as _NativeLink
from ._link import SyncLink as _SyncLink
from ._link import link_for as _link_for
from ._loop import CallbackThread as _CallbackThread
from ._loop import LoopThread as _LoopThread
from ._loop import OffLoop as _OffLoop
from ._loop import on_loop_thread as _on_loop_thread
from ._version import USER_AGENT
from .context import request_context
from .conversation import AsyncThalovantConversation, ThalovantConversation
from .errors import (
    ThalovantConnectionError,
    ThalovantHubRefusedError,
    ThalovantRuntimeError,
    ThalovantTimeoutError,
    ThalovantUnsupportedProtocolError,
)
from .events import (
    CONVERSATION_SESSION_FIELDS,
    EVENT_AUDIO_QUEUE,
    EVENT_INTENT_FAILURE,
    EVENT_INTENT_UNMATCHED,
    EVENT_OVOS_UTTERANCE_SPEAK,
    EVENT_POLICY_DENIED,
    EVENT_QUERY_TIMEOUT,
    EVENT_RECOGNIZER_LOOP_UTTERANCE,
    EVENT_SPEAK,
    EVENT_UTTERANCE_HANDLED,
    HIVE_BROADCAST,
    HIVE_ESCALATE,
    HIVE_KINDS,
    HIVE_PROPAGATE,
    MAX_AUDIO_CLIP_BYTES,
    MAX_REPLY_MEDIA_BYTES,
    UNTRACKED_UTTERANCE_GRACE_SECONDS,
    EventHandler,
    EventPredicate,
    ThalovantBinary,
    ThalovantEvent,
    _context_with_correlation,
    _event_from_message,
    _event_matches_context,
    _merge_context,
    _new_request_id,
    _new_session_id,
    _runtime_bus_context,  # noqa: F401 - importable from here, as it always was
    _session_from_context,
    _session_id_from_context,
    _utterance_payload,
    carry_conversation,
    failure_error,
    refusal_belongs_to_ask,
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
from .protocols import DEFAULT_PROTOCOL_PREFERENCE, HubProtocol
from .subscriptions import ThalovantSubscription
from .transport import (
    HiveMindHTTPTransport,
    HiveMindMQTTTransport,
    HiveMindWSSTransport,
    Transport,
    _redact_error_text,
)

DEFAULT_USERAGENT = USER_AGENT

T = TypeVar("T")


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
    self_signed: bool = False,
    session: Any = None,
) -> Transport:
    kwargs: dict[str, Any] = {
        "useragent": useragent,
        "connect_timeout": connect_timeout,
        "handshake_timeout": handshake_timeout,
        "send_timeout": send_timeout,
        "noise_state_dir": noise_state_dir,
    }
    if protocol == "https":
        return HiveMindHTTPTransport(identity, self_signed=self_signed, session=session, **kwargs)
    if protocol == "wss":
        endpoint = identity.endpoint_for("wss")
        if not endpoint:
            raise ThalovantUnsupportedProtocolError(
                "WSS is enabled, but the identity does not include a WSS endpoint."
            )
        return HiveMindWSSTransport(identity, self_signed=self_signed, session=session, **kwargs)
    if protocol == "mqtt":
        if identity.mqtt is None:
            raise ThalovantUnsupportedProtocolError(
                "MQTT is enabled, but the identity does not include MQTT broker credentials."
            )
        return HiveMindMQTTTransport(identity, **kwargs)
    raise ThalovantUnsupportedProtocolError(f"Unsupported protocol: {protocol}")


log = logging.getLogger("thalovant.client")

# Poll admitted readiness without extending the caller's connect deadline.
_SETTLE_POLL = 0.02
#: After the hub drops a link, the first redial waits about this long
#: (jittered x0.5-1.5), doubling to the ceiling. It is the ladder
#: hivemind-bus-client ran under 0.8.7's WebSocket transport: a client that
#: only listens got its link back after a hub restart without calling
#: anything, and callers rely on that. The jitter keeps a fleet that a hub
#: restart disconnected at once from dialling back at the same instant.
_REDIAL_FIRST_SECONDS = 5.0
_REDIAL_CEILING_SECONDS = 60.0
#: How long a finished ask keeps listening for the hub's "what the
#: conversation now is" frame.
#:
#: Measured on production 2026-09-16, 12 turns driven onto the runtime bus:
#: ``ovos.utterance.handled`` lands 4.2-15.3 ms after the last ``speak``
#: (median 11.2), against the satellite's 100 ms settle window -- so this is
#: insurance, not the common path. It exists for the case the margin narrows:
#: a loaded hub, or a caller settling in nothing flat.
CARRY_GRACE_SECONDS = 2.0


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


def reply_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    """The context of a reply to a message that carried *context* (OVOS-MSG-1 §5.2).

    A deep copy, so the reply keeps the request's session and everything else
    it said, with the routing turned round: the reply goes to whoever sent the
    request (``destination`` becomes the old ``source``) and comes from whoever
    it was sent to (``source`` becomes the old ``destination``, its first entry
    when that is a list). A request with a destination and no source gets a
    reply with no destination. A hub uses this to route the answer back to the
    satellite that asked, across bridges and NAT.
    """

    swapped = copy.deepcopy(dict(context or {}))
    source = swapped.get("source")
    destination = swapped.get("destination")
    if destination is not None:
        swapped["source"] = (
            destination[0] if isinstance(destination, list) and destination else destination
        )
    if source is not None:
        swapped["destination"] = source
    elif destination is not None:
        # Nobody to send it back to: the request said who it was for but not
        # who sent it. Keeping the old destination would address the reply to
        # its own sender, so the reply carries no destination at all and
        # the hub routes it as it routes any message without one.
        del swapped["destination"]
    return swapped


def _reply_to(event: Any, context: Mapping[str, Any] | None) -> dict[str, Any]:
    """The context of a reply to *event*, routed from what the hub actually sent.

    A delivered message has already been taken in -- its ``destination`` read
    as its ``source`` -- so the reply is built from the context as it came off
    the wire, when the SDK kept it.
    """
    raw = getattr(event, "raw", event)
    wire = getattr(raw, "wire_context", None)
    base = dict(wire if isinstance(wire, Mapping) else (getattr(event, "context", None) or {}))
    if context:
        base.update(context)
    return reply_context(base)


class _Ownership:
    """The connection lifecycle's owner token.

    Not an ``asyncio.Lock``: a waiter must be able to give up at its own
    deadline without the lock changing hands underneath it, and the owner is
    whichever piece of work holds it -- possibly long after its caller left.
    """

    def __init__(self) -> None:
        self._held = False
        self._released: asyncio.Event | None = None

    def locked(self) -> bool:
        return self._held

    def try_acquire(self) -> bool:
        if self._held:
            return False
        self._held = True
        return True

    def release(self) -> None:
        self._held = False
        released, self._released = self._released, None
        if released is not None:
            released.set()

    async def wait_released(self, timeout: float | None, *others: asyncio.Event) -> None:
        if not self._held:
            return
        if self._released is None:
            self._released = asyncio.Event()
        await _wait_any((self._released, *others), timeout)


async def _wait_any(events: Sequence[asyncio.Event | None], timeout: float | None) -> None:
    """Return when any of *events* is set, or after *timeout*."""
    present = [event for event in events if event is not None]
    if any(event.is_set() for event in present):
        return
    if timeout is not None and timeout <= 0:
        return
    if not present:
        await asyncio.sleep(timeout if timeout is not None else 0)
        return
    waiters = [asyncio.ensure_future(event.wait()) for event in present]
    try:
        await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()


def _run_handler(handler: Callable[[Any], Any], value: Any) -> None:
    """Call a handler on the loop; a coroutine it returns becomes a task."""
    result = handler(value)
    if asyncio.iscoroutine(result):
        asyncio.ensure_future(result)


class AsyncThalovantClient:
    """The asyncio client: one hub connection, conversations over it.

    Every coroutine runs on the loop that awaits it and never blocks it. A
    caller may hand in its own ``aiohttp.ClientSession`` as ``session``; it is
    used as given and never closed.
    """

    #: How many concurrent conversations one client remembers. A satellite
    #: runs a single session for its whole life; the cap only bounds a caller
    #: that mints session ids faster than it retires them.
    MAX_REMEMBERED_CONVERSATIONS = 32
    #: Aliases kept for one conversation. A hub that re-translates the
    #: session id every turn would otherwise grow one group forever.
    MAX_CONVERSATION_ALIASES = 8

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
        self_signed: bool = False,
        session: Any = None,
    ) -> None:
        self._identity = identity
        self.useragent = useragent
        # Certificate checking is on unless the caller turns it off for a
        # development hub.
        self.self_signed = self_signed
        if any(
            not math.isfinite(value) or value < 0
            for value in (reply_settle_seconds, empty_reply_wait_seconds)
        ):
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
            self_signed=self_signed,
            session=session,
        )
        self._link: _NativeLink | _SyncLink = _link_for(self._transport)
        self._connected = False
        # Read from the caller's thread by the sync client, so guarded.
        self._reply_ids_lock = threading.Lock()
        self._active_reply_ids: set[tuple[str, str]] = set()
        # When each fire-and-forget utterance went out; see _utterances_in_flight().
        self._untracked_sends: deque[float] = deque(maxlen=1024)
        self._connection_generation = 0
        # The conversation each session id is in the middle of. A hub is
        # stateless for a named session, so what the last turn activated comes
        # back on ovos.utterance.handled and has to be sent again with the next
        # utterance or it is gone -- see CONVERSATION_SESSION_FIELDS. Bounded:
        # a long-lived client that is handed a fresh session id per turn must
        # not accumulate one entry per turn forever.
        self._conversations_lock = threading.Lock()
        self._conversations: OrderedDict[
            str | None, tuple[tuple[str | None, ...], dict[str, Any]]
        ] = OrderedDict()
        # Every live subscription, so a session opened by a transparent
        # reconnect gets them back: a new session starts with none.
        self._subscription_session_ready = False
        self._subscription_session_token: Any = None
        self._bound_subscriptions: dict[Callable[[Any], Any], tuple[str, str]] = {}
        self._event_subscriptions: list[tuple[str, Callable[[Any], Any]]] = []
        self._hive_subscriptions: list[tuple[str, Callable[[Any], Any]]] = []
        # Each subscription's "closed" flag, so a closed one never runs again.
        self._closers: dict[Callable[[Any], Any], list[bool]] = {}
        self._closing = 0
        self._close_errors: list[BaseException] = []
        self._automatic_cleanups = 0
        self._cancel_connect: Callable[[BaseException], None] | None = None
        # Work that must outlive the call that started it: cleanups, and the
        # writes and registrations a timed-out caller left behind.
        self._background: set[asyncio.Future[Any]] = set()
        #: Redials after the hub drops the link; see _redial_after_drops().
        self._supervisor: asyncio.Future[Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        #: The class whose conversation limits apply: a sync client's.
        self._limits: type | None = None
        self._closed_state = True
        self._bind_loop_objects()

    @property
    def identity(self) -> ThalovantIdentity:
        """The identity this client connects with."""
        return self._identity

    @identity.setter
    def identity(self, value: ThalovantIdentity) -> None:
        self._identity = value

    # -- the loop this client lives on -------------------------------------------

    def _bind_loop_objects(self) -> None:
        self._connection_lock = _Ownership()
        self._subscriptions_lock: asyncio.Lock | None = None
        self._closed_event: asyncio.Event | None = None

    def _enter(self) -> asyncio.AbstractEventLoop:
        """The running loop, rebinding this client to it when it is idle.

        A client closed on one loop can be used again on another (two
        ``asyncio.run`` calls, or a sync client whose loop was stopped), but
        never two at once.
        """
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            if self._loop is not None and (
                self._connection_lock.locked() or self._closing or self._automatic_cleanups
            ) and not self._loop.is_closed() and self._loop.is_running():
                raise ThalovantRuntimeError(
                    "This client is busy on another event loop; use one client per loop."
                )
            self._loop = loop
            self._bind_loop_objects()
            self._background = set()
            self._supervisor = None
        return loop

    def _closed(self) -> asyncio.Event:
        if self._closed_event is None:
            self._closed_event = asyncio.Event()
            if self._closed_state:
                self._closed_event.set()
        return self._closed_event

    def _set_closed(self, value: bool) -> None:
        self._closed_state = value
        event = self._closed()
        if value:
            event.set()
        else:
            event.clear()

    def _subscriptions(self) -> asyncio.Lock:
        if self._subscriptions_lock is None:
            self._subscriptions_lock = asyncio.Lock()
        return self._subscriptions_lock

    def _keep(self, future: asyncio.Future[Any]) -> asyncio.Future[Any]:
        self._background.add(future)
        future.add_done_callback(self._background.discard)
        future.add_done_callback(_retrieve)
        return future

    # -- construction -------------------------------------------------------------

    @classmethod
    def from_identity_file(cls, path: str | Path, **kwargs: Any) -> AsyncThalovantClient:
        """Create a client from a Thalovant/HiveMind identity JSON file."""
        return cls(ThalovantIdentity.from_file(path), **kwargs)

    @classmethod
    def from_env(cls, **kwargs: Any) -> AsyncThalovantClient:
        """Create a client from `THALOVANT_*` environment variables."""
        return cls(ThalovantIdentity.from_env(), **kwargs)

    @classmethod
    def from_config(
        cls,
        path: str | Path | None = None,
        *,
        profile: str | None = None,
        **kwargs: Any,
    ) -> AsyncThalovantClient:
        """Create a client from the per-user Thalovant YAML config."""
        return cls(ThalovantIdentity.from_config(path, profile=profile), **kwargs)

    async def __aenter__(self) -> AsyncThalovantClient:
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
        """Create a scoped conversation with a stable session id."""
        return AsyncThalovantConversation(self, session_id=session_id, lang=lang, context=context)

    # -- the connection -----------------------------------------------------------

    async def connect(self, timeout: float | None = None) -> None:
        """Reach authenticated readiness within one caller deadline.

        Timed-out work keeps the connection's ownership until its connect and
        cleanup finish, so a later attempt cannot replace or be closed by that
        session.
        """
        await self._connect(timeout)

    async def _connect(
        self,
        timeout: float | None = None,
        cancellation: asyncio.Event | None = None,
        operation: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self._enter()
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

        if self._closing:
            raise ThalovantConnectionError("Hub connection is closing.")
        if self._close_errors:
            raise ThalovantConnectionError(
                "Previous connection cleanup failed; retry close before reconnecting."
            ) from None
        generation = self._connection_generation
        if operation is None and not self._connection_lock.locked() and self._connected:
            state = await self._link.probe(max(0.0, deadline - time.monotonic()))
            if state is None:
                raise timeout_error()
            if state and not self._connection_lock.locked():
                return
        while True:
            if cancellation is not None and cancellation.is_set():
                raise ThalovantConnectionError("Hub connection was cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise timeout_error()
            if self._connection_lock.try_acquire():
                break
            await self._connection_lock.wait_released(remaining, *([cancellation] if cancellation else []))
        try:
            if time.monotonic() >= deadline:
                raise timeout_error()
            if cancellation is not None and cancellation.is_set():
                raise ThalovantConnectionError("Hub connection was cancelled.")
            if self._closing or generation != self._connection_generation:
                raise ThalovantConnectionError("Hub connection was closed before it became ready.")
            if self._close_errors:
                raise ThalovantConnectionError(
                    "Previous connection cleanup failed; retry close before reconnecting."
                ) from None
            reuse_connection = False
            if self._connected:
                state = await self._link.probe(max(0.0, deadline - time.monotonic()))
                if state is None:
                    raise timeout_error()
                reuse_connection = state
            if operation is None and reuse_connection:
                self._connection_lock.release()
                return
        except BaseException:
            self._connection_lock.release()
            raise
        if not reuse_connection:
            self._connected = False
        done = asyncio.Event()
        cancelled = asyncio.Event()
        errors: list[BaseException] = []
        cleanup: asyncio.Future[None] | None = None

        async def disconnect() -> None:
            try:
                await self._link.retire()
            except BaseException as error:  # noqa: BLE001 - kept for close() to report
                self._close_errors.append(error)

        def cancel(error: BaseException) -> None:
            nonlocal cleanup
            if done.is_set():
                return
            errors.append(error)
            cancelled.set()
            self._connected = False
            self._automatic_cleanups += 1
            self._set_closed(False)
            cleanup = self._keep(asyncio.ensure_future(disconnect()))
            done.set()

        self._cancel_connect = cancel

        async def run_connect() -> None:
            transport_completed = reuse_connection
            try:
                if not reuse_connection:
                    # Stop binding new listeners to a session being replaced.
                    self._subscription_session_ready = False
                    await self._link.connect()
                    transport_completed = True
                    await self._reapply_subscriptions()
                while not cancelled.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        cancel(timeout_error())
                        break
                    closed = self._closed_while_connecting()
                    if closed is not None:
                        # The hub closed the link between the end of the
                        # handshake and this connect returning -- which is how
                        # a hub that does not know this key refuses it. Not a
                        # link to wait on until the deadline.
                        raise closed
                    if await self._link.probe(remaining):
                        if time.monotonic() >= deadline:
                            cancel(timeout_error())
                        elif not cancelled.is_set():
                            self._connected = True
                        break
                    await _wait_any((cancelled,), min(_SETTLE_POLL, max(0.0, deadline - time.monotonic())))
                if not cancelled.is_set():
                    if operation is not None:
                        await operation()
                    if time.monotonic() >= deadline:
                        cancel(timeout_error())
                    elif not cancelled.is_set():
                        self._supervise_link()
                        done.set()
            except BaseException as exc:  # noqa: BLE001 - handed to the caller
                cancel(exc)
            finally:
                owned_cleanup = cleanup
                if owned_cleanup is not None:
                    await asyncio.shield(owned_cleanup)
                    if transport_completed:
                        # A custom transport may finish after its first
                        # disconnect. Retire that late completion too.
                        await disconnect()
                if self._cancel_connect is cancel:
                    self._cancel_connect = None
                if owned_cleanup is not None:
                    self._automatic_cleanups -= 1
                    if not self._automatic_cleanups and not self._closing:
                        self._set_closed(True)
                self._connection_lock.release()

        self._keep(asyncio.ensure_future(run_connect()))
        try:
            while not done.is_set():
                if cancellation is not None and cancellation.is_set():
                    cancel(ThalovantConnectionError("Hub connection was cancelled."))
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    cancel(timeout_error())
                    break
                await _wait_any((done, cancellation), remaining)
        except asyncio.CancelledError:
            cancel(ThalovantConnectionError("Hub connection was cancelled."))
            raise
        if errors:
            raise errors[0]

    def _closed_while_connecting(self) -> BaseException | None:
        """Why the link just opened is already gone, or ``None`` while it is up."""
        stopped = self._link.stopped()
        if stopped is None or not stopped.is_set():
            return None
        transport = getattr(self._link, "transport", None)
        if getattr(transport, "closed_refused", False):
            return ThalovantHubRefusedError(
                "The hub closed the link right after the handshake: it does not accept these credentials, or not yet."
            )
        error = self._link.last_error()
        return error if isinstance(error, ThalovantConnectionError) else ThalovantConnectionError(
            "The hub closed the link right after the handshake."
        )

    async def connect_with_info(self, timeout: float | None = None) -> ThalovantConnectionInfo:
        """Connect and return the transport timing snapshot."""
        await self.connect(timeout=timeout)
        return self._link.connection_info()

    async def connection_info(self) -> ThalovantConnectionInfo:
        """Return connection timing for the current or most recent transport."""
        return self._link.connection_info()

    async def close(self, timeout: float | None = None) -> None:
        """Cancel pending work and close within a caller budget.

        A timeout retains cleanup ownership. Use ``wait_closed`` to observe
        actual completion before handing this identity to another client.
        """
        self._enter()
        budget = self._hard_connect_timeout if timeout is None else timeout
        if not math.isfinite(budget) or budget <= 0:
            raise ThalovantConnectionError("Hub close deadline expired.") from ThalovantTimeoutError(
                "Hub close requires a positive finite timeout."
            )
        errors: list[BaseException] = []
        self._connection_generation += 1
        self._closing += 1
        self._set_closed(False)
        self._connected = False
        self._stop_supervisor()
        if self._cancel_connect is not None:
            self._cancel_connect(ThalovantConnectionError("Hub connection was closed before it became ready."))

        async def run_close() -> None:
            try:
                while not self._connection_lock.try_acquire():
                    await self._connection_lock.wait_released(None)
                try:
                    await self._link.disconnect()
                    self._close_errors.clear()
                finally:
                    self._connection_lock.release()
            except BaseException as exc:  # noqa: BLE001 - reported below and kept
                errors.append(exc)
                self._close_errors.append(exc)
            finally:
                self._closing -= 1
                if not self._closing and not self._automatic_cleanups:
                    self._set_closed(True)
                    if not self._close_errors:
                        await self._release_resources()

        task = self._keep(asyncio.ensure_future(run_close()))
        finished, _ = await asyncio.wait({task}, timeout=budget)
        if not finished:
            raise ThalovantConnectionError(f"Hub close did not complete within {budget:g}s.")
        if errors:
            raise errors[0]

    async def _abandon(self) -> None:
        """Close what a client nobody holds any more left open.

        Only the SDK's own transport: a transport the caller built and handed
        in is the caller's to close.
        """
        self._stop_supervisor()
        if not self._link.native:
            return
        try:
            await self._link.retire()
        finally:
            await self._link.release()

    def _supervise_link(self) -> None:
        """Watch a link that is up, to redial it if the hub drops it."""
        if not self.auto_reconnect or self._link.stopped() is None:
            return
        current = self._supervisor
        if current is None or current.done():
            self._supervisor = self._keep(asyncio.ensure_future(self._redial_after_drops()))

    def _stop_supervisor(self) -> None:
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None and supervisor is not asyncio.current_task():
            supervisor.cancel()

    async def _redial_after_drops(self) -> None:
        """Get a dropped link back, on the ladder 0.8.7's WebSocket library ran.

        Only a link the hub or the network dropped: a close, or a connect that
        failed or was cancelled, ends this. A call made meanwhile reconnects
        at once, as it always has, and the ladder steps back. Subscriptions
        come back with the link. Each attempt is logged at DEBUG.
        """
        generation = self._connection_generation
        retry = _REDIAL_FIRST_SECONDS
        while True:
            stopped = self._link.stopped()
            if stopped is None:
                return
            await stopped.wait()
            if generation != self._connection_generation or self._closing or not self._connected:
                return
            while stopped.is_set():
                wait = min(retry, _REDIAL_CEILING_SECONDS) * random.uniform(0.5, 1.5)
                log.debug("hub link dropped; redialling in %.1fs", wait)
                await asyncio.sleep(wait)
                if generation != self._connection_generation or self._closing:
                    return
                if not stopped.is_set():
                    break  # a call reconnected it
                retry = min(max(retry, 1.0) * 2, _REDIAL_CEILING_SECONDS)
                try:
                    await self._connect()
                except Exception as failure:  # noqa: BLE001 - any failure is retried on the ladder
                    log.debug("hub link: redial failed (%s)", _redact_error_text(failure))
                else:
                    log.debug("hub link: redialled")
            retry = _REDIAL_FIRST_SECONDS

    async def _release_resources(self) -> None:
        try:
            await self._link.release()
        except Exception:  # noqa: BLE001 - an HTTP session that will not close is not the caller's problem
            pass

    async def wait_closed(self, timeout: float | None = None) -> None:
        """Wait for actual pending cleanup; no timeout means wait until retired."""
        self._enter()
        closed = self._closed()
        if not closed.is_set():
            try:
                await asyncio.wait_for(closed.wait(), timeout)
            except asyncio.TimeoutError:
                raise ThalovantConnectionError("Hub cleanup is still pending.") from None
        if self._close_errors:
            raise self._close_errors[0]

    disconnect = close

    async def healthcheck(self) -> ThalovantHealth:
        """Connect if needed and return the transport health snapshot."""
        await self.connect()
        return self._link.healthcheck()

    async def doctor(self) -> ThalovantDoctorReport:
        """Run identity, endpoint, connection, and transport diagnostics."""
        checks: list[ThalovantDoctorCheck] = []

        async def check(name: str, operation: Callable[[], Awaitable[str]]) -> None:
            started = time.monotonic()
            try:
                detail = await operation()
                ok = True
            except Exception as exc:  # noqa: BLE001 - doctor reports every check's failure
                # doctor output is printed by the CLI; scrub any URL query
                # (which carries the data-plane access key) from the message.
                detail = _redact_error_text(exc)
                ok = False
            checks.append(
                ThalovantDoctorCheck(
                    name=name, ok=ok, detail=detail, duration_ms=(time.monotonic() - started) * 1000
                )
            )

        async def identity() -> str:
            return _doctor_identity(self.identity)

        async def endpoint() -> str:
            return _doctor_endpoint(self.identity)

        async def connect() -> str:
            await self.connect()
            return "connected and handshake completed"

        async def transport() -> str:
            health = await self.healthcheck()
            if not health.ok:
                raise ThalovantConnectionError(str(health.as_dict()))
            return "polling thread alive"

        await check("identity", identity)
        await check("endpoint", endpoint)
        await check("connect", connect)
        await check("transport", transport)
        return ThalovantDoctorReport(
            identity=self.identity.as_dict(include_secrets=False), checks=tuple(checks)
        )

    # -- subscriptions --------------------------------------------------------------

    def _event_handler(
        self,
        event_name: str,
        handler: EventHandler,
        *,
        context: dict[str, Any] | None,
        session_id: str | None,
        request_id: str | None,
        predicate: EventPredicate | None,
        runner: _CallbackThread | None,
    ) -> Callable[[Any], Any]:
        expected_context = _context_with_correlation(
            context, session_id=session_id, request_id=request_id
        )

        closed = [False]

        # The supported transports deliver each _BUS frame once, after protocol
        # processing. Do not suppress custom transport events based on object
        # identity: a reusable message object can be valid.
        def wrapped(raw_message: Any) -> Any:
            if closed[0]:
                # Closed while a registration was in flight: once close()
                # returns, the handler never runs again.
                return None
            event = _event_from_message(event_name, raw_message)
            if not _event_matches_context(event, expected_context):
                return None
            if predicate is not None and not predicate(event):
                return None
            return handler(event)

        registered: Callable[[Any], Any]
        if runner is not None:
            registered = _OffLoop(wrapped, runner)
        else:

            def on_loop(raw_message: Any) -> None:
                result = wrapped(raw_message)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)

            registered = on_loop
        self._closers[registered] = closed
        return registered

    async def _on(
        self,
        event_name: str,
        handler: EventHandler,
        *,
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        predicate: EventPredicate | None = None,
        runner: _CallbackThread | None = None,
        subscription_client: Any = None,
    ) -> ThalovantSubscription:
        """Connect, then subscribe; the subscription is live when this returns."""
        await self.connect()
        wrapped = self._event_handler(
            event_name, handler, context=context, session_id=session_id,
            request_id=request_id, predicate=predicate, runner=runner,
        )
        await self._add_subscription(event_name, wrapped)
        return ThalovantSubscription(subscription_client or self, event_name, wrapped)

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
        """Subscribe to a hub event and receive normalized `ThalovantEvent` objects.

        The handler runs on this loop. An ``async def`` handler is scheduled as
        a task. The subscription is registered at once and bound to the
        connection as soon as it is up; the connection is opened in the
        background if it is not.
        """
        self._enter()
        wrapped = self._event_handler(
            event_name, handler, context=context, session_id=session_id,
            request_id=request_id, predicate=predicate, runner=None,
        )
        subscription = ThalovantSubscription(self, event_name, wrapped)
        if self._subscription_session_ready and not self._subscriptions().locked():
            # Bound before this returns, as it always was: an event the hub
            # sends right after must not find nobody listening.
            self._link.bind_now(_BUS, event_name, wrapped)
            self._bound_subscriptions[wrapped] = (_BUS, event_name)
            self._event_subscriptions.append((event_name, wrapped))
            return subscription

        async def subscribe() -> None:
            await self._add_subscription(event_name, wrapped)
            await self.connect()

        self._keep(asyncio.ensure_future(subscribe()))
        return subscription

    async def _add_subscription(self, event_name: str, handler: Callable[[Any], Any]) -> None:
        async with self._subscriptions():
            if self._subscription_session_ready:
                # Commit the subscription only after registration succeeds.
                await self._link.bind(_BUS, event_name, handler)
                self._bound_subscriptions[handler] = (_BUS, event_name)
            self._event_subscriptions.append((event_name, handler))

    def _remove_subscription(self, event_name: str, handler: Callable[[Any], Any]) -> None:
        closed = self._closers.pop(handler, None)
        if closed is not None:
            closed[0] = True
        if not any(entry[1] is handler for entry in self._event_subscriptions):
            return
        self._event_subscriptions = [
            entry for entry in self._event_subscriptions if entry[1] is not handler
        ]
        if self._subscription_session_ready and handler in self._bound_subscriptions:
            try:
                self._link.unbind(_BUS, event_name, handler)
            except ThalovantConnectionError:
                pass
            self._bound_subscriptions.pop(handler, None)

    async def _reapply_subscriptions(self) -> None:
        """Reconcile live listeners with the connected transport session.

        Session tokens preserve existing bindings when the transport keeps its
        session. Registry edits during connection remain pending until this
        locked handoff can reconcile additions and removals exactly once.
        """
        token = self._link.session_token()
        async with self._subscriptions():
            if token is not None and token != self._subscription_session_token:
                self._bound_subscriptions.clear()

            def live() -> dict[Callable[[Any], Any], tuple[str, str]]:
                entries = {handler: (_BUS, name) for name, handler in self._event_subscriptions}
                entries.update({handler: (_HIVE, kind) for kind, handler in self._hive_subscriptions})
                return entries

            wanted = live()
            for handler, (channel, name) in tuple(self._bound_subscriptions.items()):
                if handler not in wanted:
                    try:
                        self._link.unbind(channel, name, handler)
                    except ThalovantConnectionError:
                        pass
                    except (KeyError, ValueError):
                        if token is not None:
                            raise
                    del self._bound_subscriptions[handler]
            if token is None:
                # Retire pending removals before forgetting old bindings: a
                # legacy transport may have kept the same session alive.
                self._bound_subscriptions.clear()
            for handler, (channel, name) in tuple(wanted.items()):
                if handler in self._bound_subscriptions:
                    continue
                if token is None:
                    # Legacy custom transports have no session token. Replace
                    # any existing registration before adding it again.
                    try:
                        self._link.unbind(channel, name, handler)
                    except (ThalovantConnectionError, KeyError, ValueError):
                        pass
                await self._link.bind(channel, name, handler)
                self._bound_subscriptions[handler] = (channel, name)
            # A subscription closed while a registration was in flight.
            wanted = live()
            for handler, (channel, name) in tuple(self._bound_subscriptions.items()):
                if handler not in wanted:
                    try:
                        self._link.unbind(channel, name, handler)
                    except (ThalovantConnectionError, KeyError, ValueError):
                        pass
                    del self._bound_subscriptions[handler]
            self._subscription_session_token = token
            self._subscription_session_ready = True

    async def _on_hive(
        self, kind: str, handler: Callable[[Any], Any]
    ) -> Callable[[], None]:
        if kind not in HIVE_KINDS:
            # Named rather than silently never firing: subscribing to "bus" or
            # to a typo is the kind of mistake that looks like a quiet hub.
            raise ValueError(
                f"{kind!r} is not a hive frame kind; expected one of {', '.join(HIVE_KINDS)}."
            )
        await self.connect()
        async with self._subscriptions():
            if self._subscription_session_ready:
                await self._link.bind(_HIVE, kind, handler)
                self._bound_subscriptions[handler] = (_HIVE, kind)
            self._hive_subscriptions.append((kind, handler))

        def unsubscribe() -> None:
            self._call_on_loop(self._remove_hive, kind, handler)

        return unsubscribe

    def _remove_hive(self, kind: str, handler: Callable[[Any], Any]) -> None:
        self._hive_subscriptions = [
            entry for entry in self._hive_subscriptions if entry[1] is not handler
        ]
        if handler in self._bound_subscriptions:
            self._bound_subscriptions.pop(handler, None)
            try:
                self._link.unbind(_HIVE, kind, handler)
            except ThalovantConnectionError:
                pass

    async def _on_binary(self, handler: Callable[[Any], Any]) -> Callable[[], None]:
        await self.connect()
        await self._link.bind(_BINARY, "", handler)

        def unsubscribe() -> None:
            self._call_on_loop(self._link.unbind, _BINARY, "", handler)

        return unsubscribe

    def _call_on_loop(self, fn: Callable[..., Any], *args: Any) -> None:
        """Run *fn* on this client's loop, from whichever thread calls."""
        loop = self._loop
        if loop is None or _on_loop_thread(loop) or not loop.is_running():
            fn(*args)
            return
        finished = threading.Event()
        failure: list[BaseException] = []

        def run() -> None:
            try:
                fn(*args)
            except BaseException as error:  # noqa: BLE001 - re-raised in the caller
                failure.append(error)
            finally:
                finished.set()

        loop.call_soon_threadsafe(run)
        finished.wait()
        if failure:
            raise failure[0]

    async def on_hive(self, kind: str, handler: Callable[[Any], Any]) -> Callable[[], None]:
        """Listen to one of the hive's own frame kinds. Returns an unsubscriber.

        The handler runs on this loop; an ``async def`` handler is scheduled
        as a task. The frame is handed over as the hub sent it -- a
        HiveMessage, not a normalized `ThalovantEvent`.
        """
        self._enter()
        return await self._on_hive(kind, _loop_handler(handler))

    async def on_binary(self, handler: Callable[[Any], Any]) -> Callable[[], None]:
        """Listen for binary frames: rendered speech, and files."""
        self._enter()
        return await self._on_binary(_loop_handler(handler))

    # -- event streams ------------------------------------------------------------

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
        """Wait for one matching event within a connect/registration/wait budget."""
        return await self._wait_for_event(
            event_name, timeout=timeout, predicate=predicate, context=context,
            session_id=session_id, request_id=request_id,
        )

    async def _wait_for_event(self, event_name: str, **kwargs: Any) -> ThalovantEvent:
        stream = self._listen(event_name, max_events=1, max_buffered_events=1, **kwargs)
        try:
            return await stream.__anext__()
        except StopAsyncIteration:
            raise ThalovantTimeoutError(
                f"Hub did not emit {event_name!r} within the caller deadline."
            ) from None
        finally:
            await stream.aclose()

    async def listen(
        self,
        event_name: str,
        *,
        timeout: float | None = None,
        max_events: int | None = None,
        max_buffered_events: int = 256,
        predicate: EventPredicate | None = None,
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> AsyncIterator[ThalovantEvent]:
        """Yield bounded buffered events; overflow raises ThalovantRuntimeError.

        A supplied timeout includes connection and subscription setup. With no
        timeout, setup uses the normal connect budget and listening is unlimited.
        """
        stream = self._listen(
            event_name, timeout=timeout, max_events=max_events,
            max_buffered_events=max_buffered_events, predicate=predicate,
            context=context, session_id=session_id, request_id=request_id,
        )
        try:
            async for event in stream:
                yield event
        finally:
            await stream.aclose()

    async def _listen(
        self,
        event_name: str,
        *,
        timeout: float | None = None,
        max_events: int | None = None,
        max_buffered_events: int = 256,
        predicate: EventPredicate | None = None,
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> AsyncGenerator[ThalovantEvent, None]:
        if (
            isinstance(max_buffered_events, bool)
            or not isinstance(max_buffered_events, int)
            or max_buffered_events <= 0
        ):
            raise ValueError("max_buffered_events must be a positive integer.")
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise ThalovantTimeoutError("Event deadline expired before connection.")
        if max_events is not None and max_events <= 0:
            return
        loop = self._enter()
        cancellation = asyncio.Event()
        deadline = None if timeout is None else time.monotonic() + timeout
        setup_deadline = (
            deadline if deadline is not None else time.monotonic() + self._hard_connect_timeout
        )
        events: deque[ThalovantEvent] = deque()
        arrived = asyncio.Event()
        errors: list[BaseException] = []
        active = True
        subscribed = False
        setup_done = asyncio.Event()
        accepted = 0
        yielded = 0
        expected = _context_with_correlation(context, session_id=session_id, request_id=request_id)

        def expired() -> bool:
            return deadline is not None and time.monotonic() >= deadline

        def retire() -> None:
            nonlocal active, subscribed
            active = False
            subscribed = False
            arrived.set()
            # Including a registration still in flight: it is taken back off
            # the moment it lands, and delivers nothing meanwhile.
            self._link.discard(_BUS, event_name, handler)

        def handler(raw: Any) -> None:
            nonlocal accepted
            if not active or cancellation.is_set() or expired():
                return
            if max_events is not None and accepted >= max_events:
                return
            event = _event_from_message(event_name, raw)
            if not _event_matches_context(event, expected):
                return
            try:
                if predicate is not None and not predicate(event):
                    return
            except BaseException as error:  # noqa: BLE001 - raised to the consumer
                errors.append(error)
                retire()
                return
            if not active or cancellation.is_set() or expired():
                return
            if len(events) >= max_buffered_events:
                errors.append(ThalovantRuntimeError("Event buffer overflow; subscription retired."))
                retire()
                return
            events.append(event)
            accepted += 1
            arrived.set()
            if max_events is not None and accepted >= max_events:
                retire()

        async def register() -> None:
            nonlocal subscribed
            if not active or cancellation.is_set():
                return
            await self._link.bind(_BUS, event_name, handler)
            late = not active or cancellation.is_set() or time.monotonic() >= setup_deadline
            if not late:
                subscribed = True
            else:
                self._link.discard(_BUS, event_name, handler)

        async def setup() -> None:
            try:
                await self._connect(
                    setup_deadline - time.monotonic(), cancellation=cancellation, operation=register
                )
                setup_done.set()
                arrived.set()
                stopped = self._link.stopped()
                while not cancellation.is_set():
                    if not active:
                        return
                    if expired():
                        retire()
                        return
                    await _wait_any(
                        (cancellation, stopped),
                        _SETTLE_POLL if stopped is None else _remaining(deadline),
                    )
                    if cancellation.is_set() or not active:
                        return
                    await self._raise_if_transport_stopped(_remaining(deadline, _SETTLE_POLL))
            except BaseException as error:  # noqa: BLE001 - raised to the consumer
                # Expiry may retire the subscription before initial setup
                # reports its error. That failure must not become a clean
                # end-of-stream; caller cancellation still takes priority.
                if not cancellation.is_set() and (active or not setup_done.is_set()):
                    errors.append(error)
            finally:
                setup_done.set()
                arrived.set()

        # Retirement must happen even while the consumer is paused at a yield
        # or a custom transport's status probe is blocked.
        expiry = (
            _schedule_expiry(loop, max(0.0, deadline - time.monotonic()), retire)
            if deadline is not None
            else None
        )
        self._keep(asyncio.ensure_future(setup()))
        try:
            while max_events is None or yielded < max_events:
                if cancellation.is_set():
                    return
                if errors:
                    raise errors[0]
                if not setup_done.is_set() and time.monotonic() >= setup_deadline:
                    cancellation.set()
                    raise ThalovantTimeoutError("Event connection/registration deadline expired.")
                if events:
                    event = events.popleft()
                    if errors:
                        raise errors[0]
                    yielded += 1
                    yield event
                    continue
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return
                if not active and setup_done.is_set():
                    return
                wait = remaining
                if not setup_done.is_set():
                    until_setup = setup_deadline - time.monotonic()
                    wait = until_setup if wait is None else min(wait, until_setup)
                arrived.clear()
                await _wait_any((arrived, cancellation), wait)
        finally:
            cancellation.set()
            if expiry is not None:
                expiry.cancel()
            retire()

    # -- sending ------------------------------------------------------------------

    async def emit(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Any:
        """Emit a raw OVOS/HiveMind bus event."""
        self._enter()
        if event_type != EVENT_RECOGNIZER_LOOP_UTTERANCE:
            return await self._with_reconnect(
                lambda: self._link.emit_event(
                    event_type, data or {}, self._context_with_identity_metadata(context)
                )
            )

        # A fire-and-forget utterance: nothing will wait on it, but the hub may
        # refuse it, and that refusal carries no request id.
        #
        # Recorded once the connection is up and immediately before the publish.
        # Connecting can take seconds, and starting the window there would spend
        # the grace on a handshake -- leaving a denial to land after it, where
        # an unrelated ask would take it. A connect that fails never publishes,
        # so it records nothing at all.
        #
        # A publish that raises keeps its record: the HTTP transport can fail
        # after the hub already holds the frame, and the hub refuses what it
        # holds. A record that need not have been there costs an ask its
        # deadline; a missing one ends a question the hub never refused.
        async def publish() -> Any:
            with self._reply_ids_lock:
                self._untracked_sends.append(time.monotonic())
            return await self._link.emit_event(
                event_type, data or {}, self._context_with_identity_metadata(context)
            )

        return await self._with_reconnect(publish)

    async def reply(
        self,
        event: ThalovantEvent | Any,
        msg_type: str,
        data: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Any:
        """Answer a message the hub sent, back along the route it came.

        The reply carries a deep copy of the request's context -- its session,
        its request id, everything a skill waiting on it matches -- with
        ``source`` and ``destination`` swapped (OVOS-MSG-1 §5.2). *event* is a
        :class:`ThalovantEvent` or a raw bus message; *context* entries are laid
        over the copy before the swap.
        """
        if not msg_type or not str(msg_type).strip():
            raise ValueError("A reply needs a non-empty message type.")
        return await self.emit(str(msg_type).strip(), dict(data or {}), _reply_to(event, context))

    async def _emit_query_with_timeout(
        self, event_type: str, data: dict[str, Any], context: dict[str, Any], timeout: float
    ) -> None:
        """Internal read query: one connect/send deadline, without replaying it."""
        if timeout <= 0:
            raise ThalovantTimeoutError("Hub query deadline expired before send.")

        async def send() -> None:
            await self._link.emit_event(
                event_type, data, self._context_with_identity_metadata(context)
            )

        await self._connect(timeout, operation=send)

    async def propagate(
        self, event_type: str, data: dict[str, Any] | None = None, context: dict[str, Any] | None = None
    ) -> Any:
        """Send an event across the hive.

        Every node sees it once; the route each frame carries is what stops it
        going round for ever.
        """
        return await self._send_hive(HIVE_PROPAGATE, event_type, data, context)

    async def escalate(
        self, event_type: str, data: dict[str, Any] | None = None, context: dict[str, Any] | None = None
    ) -> Any:
        """Send an event up to the parent node."""
        return await self._send_hive(HIVE_ESCALATE, event_type, data, context)

    async def broadcast(
        self, event_type: str, data: dict[str, Any] | None = None, context: dict[str, Any] | None = None
    ) -> Any:
        """Send an event down to every child. **Admin only** -- a hub
        disconnects a client that may not; see ``ThalovantClient.broadcast``."""
        return await self._send_hive(HIVE_BROADCAST, event_type, data, context)

    async def _send_hive(
        self, kind: str, event_type: str, data: dict[str, Any] | None, context: dict[str, Any] | None
    ) -> Any:
        """Wrap a bus event in a hive frame and send it.

        The envelope is nested on purpose: a hub reads ``message.payload`` of a
        mesh frame as a HiveMessage of its own and re-stamps its route on it
        before forwarding, so the inner frame is what travels.
        """
        if not event_type or not event_type.strip():
            raise ValueError("A hive frame needs a non-empty event type.")
        self._enter()
        return await self._with_reconnect(
            lambda: self._link.send_hive_frame(
                kind, event_type.strip(), data or {}, self._context_with_identity_metadata(context)
            )
        )

    async def send_utterance(
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
        request = _context_with_correlation(
            self._context_with_identity_metadata(context),
            session_id=session_id,
            site_id=self.identity.site_id,
            lang=lang,
            request_id=request_id or _new_request_id(),
        )
        return await self.emit(EVENT_RECOGNIZER_LOOP_UTTERANCE, _utterance_payload(prompt, lang), request)

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
        """Emit a selected action or quick-reply payload as an utterance."""
        prompt = payload.strip()
        if not prompt:
            raise ValueError("send_action() requires a non-empty payload.")
        action_context = _merge_context(
            context, {"input": {"kind": "action", "title": title, "payload": prompt}}
        )
        return await self.send_utterance(
            prompt, lang=lang, context=action_context, session_id=session_id, request_id=request_id
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
        """Emit an exact scanned/typed code value without speech transcription loss."""
        code = value.strip()
        if not code:
            raise ValueError("send_code() requires a non-empty value.")
        request_id = request_id or _new_request_id()
        request = _context_with_correlation(
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
        return await self.emit(EVENT_RECOGNIZER_LOOP_UTTERANCE, data, request)

    # -- asking -------------------------------------------------------------------

    async def ask(
        self,
        text: str,
        *,
        timeout: float = 12.0,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        stt_lang: str | None = None,
        pipeline: Sequence[str] | None = None,
        location: Mapping[str, Any] | None = None,
    ) -> ThalovantReply:
        """Send a text utterance and wait for the hub's spoken reply.

        ``stt_lang`` is the language a recogniser decided on, sent as the
        hub's highest-priority language hint; ``pipeline`` names the intent
        stages to run, in order; ``location`` is ``build_location()``'s
        result, which outranks the hub's own configured place. All three are
        merged into ``context`` by ``request_context()``. Embedded skill
        sounds (``mycroft.audio.queue``) arrive in ``reply.media_events``, in
        order with the speech.
        """
        return await self._ask(
            text, timeout=timeout, lang=lang,
            context=request_context(context, stt_lang=stt_lang, pipeline=pipeline, location=location),
            session_id=session_id, request_id=request_id,
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
        """Send a direct HiveMind query within one connect/send/reply deadline.

        Intent misses remain provisional until completion and can recover with
        later speech. Completion and hard failures freeze the scoped reply.
        Timed-out raw I/O retains lifecycle ownership until it is retired.
        """
        return await self._query(
            text, timeout=timeout, lang=lang, context=context, session_id=session_id,
            request_id=request_id, query_id=query_id,
        )

    def _remember_conversation(
        self, session_id: str | None | Sequence[str | None], session: dict[str, Any] | None
    ) -> None:
        """Keep the session a hub returned, to send with the next utterance.

        Keyed on the id the *request* used, never the one the reply carries.
        A hub is free to answer under an id of its own -- it NATs a declared
        one to a per-connection identity (HIVEMIND-BRIDGE-1 §4) and undoes
        that on the way out, and older hubs substituted a uuid outright. The
        next turn can only look this up by the id it is about to send, so
        that is what it is filed under. ``None`` keys the conversation of a
        caller that declares no id and lets the connection's own session
        stand; no real session id can collide with it.
        """
        keys: list[str | None] = []
        for value in (
            (session_id,) if isinstance(session_id, (str, type(None))) else session_id
        ):
            if value not in keys:
                keys.append(value)
        known = session or {}
        kept = {field: known[field] for field in _conversation_fields() if known.get(field)}
        with self._conversations_lock:
            # Take over every id these already reach, rather than dropping
            # them: a turn the caller continued under the hub's id must not
            # forget the id the satellite still uses for the same conversation.
            # The names accumulate, the conversation stays one entry.
            for key in list(keys):
                previous = self._conversations.pop(key, None)
                if previous is None:
                    continue
                for sibling in previous[0]:
                    self._conversations.pop(sibling, None)
                    if sibling not in keys:
                        keys.append(sibling)
            if not kept:
                # The turn ended with nothing to carry -- a skill deactivated,
                # a context cleared. Forgetting is the state, not an absence
                # of one: leaving the old entry would resurrect it.
                return
            # One conversation, however many ids reach it; `keys` is ordered
            # this turn's ids first, then the ones inherited from groups it
            # absorbed, so dropping from the tail discards the stalest aliases
            # and keeps the id the next turn is actually going to send.
            aliases, remembered = self._conversation_limits()
            del keys[aliases:]
            group = tuple(keys)
            for key in keys:
                self._conversations[key] = (group, kept)
            while self._remembered_conversations() > remembered:
                _, (oldest, _) = next(iter(self._conversations.items()))
                for sibling in oldest:
                    self._conversations.pop(sibling, None)

    def _conversation_limits(self) -> tuple[int, int]:
        """The alias and conversation caps: a sync client's own class attributes, where callers set them."""
        owner: Any = self._limits or type(self)
        return int(owner.MAX_CONVERSATION_ALIASES), int(owner.MAX_REMEMBERED_CONVERSATIONS)

    def _remembered_conversations(self) -> int:
        """Conversations held, counting a group of aliases once."""
        return len({entry[0] for entry in self._conversations.values()})

    def _continue_conversation(
        self, context: dict[str, Any] | None, session_id: str | None
    ) -> dict[str, Any] | None:
        """Put the last turn's conversation state back into this turn."""
        session = _session_from_context(context)
        # Resolved exactly as the storing side resolves it. A session id is
        # accepted in three places -- this argument, `context["session"]`, and
        # a top-level `context["session_id"]` -- and the turn is filed under
        # whatever `_session_id_from_context` makes of the request.
        session_id = session_id or _session_id_from_context(context) or None
        with self._conversations_lock:
            entry = self._conversations.get(session_id)
            previous = None
            if entry is not None:
                group, previous = entry
                # Most recently used: every id of this conversation moves
                # together, so reaching it by one name keeps the others alive.
                for key in group:
                    if key in self._conversations:
                        self._conversations.move_to_end(key)
        carried = carry_conversation(previous, session)
        if carried == session:
            return context
        continued = dict(context or {})
        continued["session"] = carried
        return continued

    async def _ask(
        self,
        text: str,
        *,
        timeout: float = 12.0,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> ThalovantReply:
        request_id = request_id or _new_request_id()
        with self._reserve_reply_id("ask", request_id):
            return await self._ask_reserved(
                text, timeout=timeout, lang=lang, context=context,
                session_id=session_id, request_id=request_id,
            )

    async def _ask_reserved(
        self,
        text: str,
        *,
        timeout: float = 12.0,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> ThalovantReply:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ThalovantTimeoutError("Hub request deadline expired.")
        deadline = time.monotonic() + timeout
        prompt = text.strip()
        if not prompt:
            raise ValueError("ask() requires a non-empty text prompt.")

        request_id = request_id or _new_request_id()
        request = _context_with_correlation(
            self._continue_conversation(self._context_with_identity_metadata(context), session_id),
            session_id=session_id,
            site_id=self.identity.site_id,
            lang=lang,
            request_id=request_id,
        )
        last_error: BaseException | None = None
        published = asyncio.Event()
        attempts = self.reconnect_attempts + 1 if self.auto_reconnect else 1
        for attempt in range(attempts):
            try:
                return await self._query(
                    prompt,
                    timeout=deadline - time.monotonic(),
                    lang=lang,
                    context=request,
                    request_id=request_id,
                    session_id=_session_id_from_context(request),
                    direct=False,
                    published=published,
                )
            except ThalovantConnectionError as exc:
                last_error = exc
                if published.is_set():
                    raise
                if attempt + 1 >= attempts:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ThalovantTimeoutError("Hub request deadline expired.") from None
                await self.close(timeout=remaining)
        raise ThalovantConnectionError(
            "HiveMind transport failed while waiting for reply."
        ) from last_error

    async def _query(
        self,
        text: str,
        *,
        timeout: float = 12.0,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        query_id: str | None = None,
        direct: bool = True,
        published: asyncio.Event | None = None,
    ) -> ThalovantReply:
        request_id = request_id or _new_request_id()
        query_id = query_id or request_id

        async def collect() -> ThalovantReply:
            return await self._query_reserved(
                text, timeout=timeout, lang=lang, context=context,
                session_id=session_id, request_id=request_id, query_id=query_id,
                direct=direct, published=published,
            )

        if not direct:
            return await collect()  # Ask owns its bus request ID across pre-publication retries.
        with self._reserve_reply_id("query", query_id):
            return await collect()

    def _utterances_in_flight(self) -> dict[str, int]:
        """How many utterances this client may still have refused, for a denial with no id.

        Asks and queries are counted while they wait. A fire-and-forget
        utterance has nothing to wait on, so it counts for the grace window
        after it was sent -- its refusal could land while an ask is waiting.
        """
        now = time.monotonic()
        with self._reply_ids_lock:
            asks = sum(1 for namespace, _ in self._active_reply_ids if namespace == "ask")
            queries = sum(1 for namespace, _ in self._active_reply_ids if namespace == "query")
            while (
                self._untracked_sends
                and now - self._untracked_sends[0] > UNTRACKED_UTTERANCE_GRACE_SECONDS
            ):
                self._untracked_sends.popleft()
            sends = len(self._untracked_sends)
        return {"asks_in_flight": asks, "queries_in_flight": queries, "sends_in_flight": sends}

    @contextmanager
    def _reserve_reply_id(self, namespace: str, identifier: str) -> Iterator[None]:
        """Reject ambiguous overlapping collectors without disturbing the owner."""
        key = (namespace, identifier)
        with self._reply_ids_lock:
            if key in self._active_reply_ids:
                raise ThalovantRuntimeError("A reply collector with this correlation ID is already active.")
            self._active_reply_ids.add(key)
        try:
            yield
        finally:
            with self._reply_ids_lock:
                self._active_reply_ids.remove(key)

    async def _query_reserved(
        self,
        text: str,
        *,
        timeout: float = 12.0,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        query_id: str | None = None,
        direct: bool = True,
        published: asyncio.Event | None = None,
    ) -> ThalovantReply:
        prompt = text.strip()
        if not prompt:
            raise ValueError("query() requires a non-empty text prompt.")

        request_id = request_id or _new_request_id()
        query_id = query_id or request_id
        request = _context_with_correlation(
            self._context_with_identity_metadata(context),
            session_id=(session_id or _new_session_id()) if direct else session_id,
            site_id=self.identity.site_id,
            lang=lang,
            request_id=request_id,
        )
        if not math.isfinite(timeout) or timeout <= 0:
            raise ThalovantTimeoutError("Hub query deadline expired.")
        loop = self._enter()
        deadline = time.monotonic() + timeout
        if direct and not self._link.supports_query():
            raise ThalovantRuntimeError("This transport does not support HiveMind query frames.")

        done = asyncio.Event()
        # Set whenever a handler moves a deadline, so the wait below re-reads it.
        moved = asyncio.Event()
        cancellation = asyncio.Event()
        fragments: list[str] = []
        raw_messages: list[Any] = []
        events: list[ThalovantEvent] = []
        # What the handled turn said the conversation is, held so the reply can
        # file it under the id the caller is handed.
        handled_session: list[tuple[list[str | None], dict[str, Any]]] = []
        # Filled in just before the reply is handed back, so a handled event
        # arriving inside the grace window can file the carry under the id the
        # caller actually holds.
        returned_session_id: list[str | None] = [None]
        dropped_media = 0
        media_chars = 0
        registered: list[tuple[str, Callable[[Any], None]]] = []
        errors: list[BaseException] = []
        failure_event: ThalovantEvent | None = None
        soft_failure_event: ThalovantEvent | None = None
        terminal = False
        empty_deadline: float | None = None
        settle_deadline: float | None = None
        channel = _HIVE if direct else _BUS

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
            if terminal:
                return
            if not direct and time.monotonic() >= collection_deadline():
                finish_at_deadline()
                return
            terminal = True
            errors.append(error)
            done.set()

        def handle_query_frame(message: Any) -> None:
            nonlocal failure_event, soft_failure_event, terminal
            nonlocal empty_deadline, settle_deadline, dropped_media, media_chars
            # The end of the turn is the one place a hub states what the
            # conversation now is, and it keeps none of it for a named
            # session (OVOS-SESSION-2 §2.2). This is the copy that has to
            # travel with the next utterance or converse has nobody to
            # poll. Kept ahead of every gate below: whether this reply is
            # still being collected, already settled or already failed
            # says nothing about what the next one will need.
            if (
                not direct
                and getattr(message, "name", None) == EVENT_UTTERANCE_HANDLED
                and message.request_id == request_id
            ):
                carried = _session_from_context(message.context)
                # One conversation, every id that can reach it: the id the
                # request used, which a satellite reuses, and the one the
                # hub answered with, which is what an ordinary caller is
                # handed. Filed in a single call so they age and are
                # evicted together rather than as separate conversations.
                answered_with = _session_id_from_context(message.context)
                keys: list[str | None] = [session_id]
                if answered_with and answered_with != session_id:
                    keys.append(answered_with)
                # Arriving late -- inside the grace window, after the reply
                # was handed back -- this is the only chance to file under
                # the id the caller was given.
                returned = returned_session_id[0]
                if returned and returned not in keys:
                    keys.append(returned)
                handled_session[:] = [(keys, carried)]
                self._remember_conversation(keys, carried)
            if terminal:
                return
            now = time.monotonic()
            if now >= collection_deadline():
                finish_at_deadline()
                return
            if direct:
                if _query_id_from_hive_message(message) != query_id:
                    return
                maybe = _event_from_query_hive_message(message)
                if maybe is None:
                    return
                event = maybe
            else:
                event = message
                # The runtime may replace the conversation session. The
                # request ID remains required to exclude ambient replies --
                # except for the one reply the hub cannot correlate. A
                # denial carries no request id, only the type it refused,
                # and dropping it here turned a refusal the hub made at
                # once into a full timeout.
                if event.name == EVENT_POLICY_DENIED:
                    denied_type = event.data.get("denied_type")
                    if not refusal_belongs_to_ask(
                        request_id=event.request_id,
                        own_request_id=request_id,
                        denied_type=denied_type if isinstance(denied_type, str) else None,
                        **self._utterances_in_flight(),
                    ):
                        return
                elif event.request_id != request_id:
                    return
            if event.name == EVENT_AUDIO_QUEUE:
                # A skill sound rides along with the speech, in order. It
                # never settles or fails the reply, and it is bounded here
                # before it is kept: the payload is the hub's to size, and
                # the bus can deliver one message object twice.
                if any(previous.raw is event.raw for previous in events):
                    return
                encoded = event.data.get("binary_data")
                if (
                    not isinstance(encoded, str)
                    or len(encoded) > MAX_AUDIO_CLIP_BYTES * 2
                    or media_chars + len(encoded) > MAX_REPLY_MEDIA_BYTES * 2
                ):
                    dropped_media += 1
                    return
                media_chars += len(encoded)
            raw_messages.append(message if direct else event.raw)
            events.append(event)
            if direct and event.name == "hive.query.complete":
                terminal = True
                done.set()
            elif not direct and event.name == EVENT_UTTERANCE_HANDLED:
                if not fragments and empty_deadline is None:
                    empty_deadline = now + self.empty_reply_wait_seconds
                    moved.set()
            elif event.name in {EVENT_SPEAK, EVENT_OVOS_UTTERANCE_SPEAK}:
                normalized = " ".join(event.text.strip().split())
                if normalized:
                    if not fragments or fragments[-1] != normalized:
                        fragments.append(normalized)
                    soft_failure_event = None
                    if not direct and settle_deadline is None:
                        settle_deadline = now + self.reply_settle_seconds
                        moved.set()
            elif event.name in {EVENT_INTENT_FAILURE, EVENT_INTENT_UNMATCHED}:
                if not fragments:
                    soft_failure_event = event
                    if not direct and empty_deadline is None:
                        empty_deadline = now + self.empty_reply_wait_seconds
                        moved.set()
            elif event.is_failure:
                failure_event = event
                terminal = True
                done.set()

        async def send() -> None:
            # Registration happens inside the owned connection generation, after
            # readiness. A caller that already expired can never subscribe later.
            kinds = ("query", "cascade") if direct else (
                EVENT_SPEAK, EVENT_OVOS_UTTERANCE_SPEAK, EVENT_UTTERANCE_HANDLED,
                EVENT_INTENT_FAILURE, EVENT_INTENT_UNMATCHED, EVENT_POLICY_DENIED,
                EVENT_QUERY_TIMEOUT, EVENT_AUDIO_QUEUE,
            )
            for kind in kinds:
                if terminal or cancellation.is_set():
                    return
                handler: Callable[[Any], None] = handle_query_frame if direct else _bus_collector(
                    kind, handle_query_frame
                )
                await self._link.bind(channel, kind, handler)
                late = terminal or cancellation.is_set()
                if not late:
                    registered.append((kind, handler))
                else:
                    self._link.unbind(channel, kind, handler)
                    return
            payload = _utterance_payload(prompt, lang)
            frame: dict[str, Any] | None = None
            if direct:
                inner: dict[str, Any] = {
                    "msg_type": "bus",
                    "payload": {
                        "type": EVENT_RECOGNIZER_LOOP_UTTERANCE,
                        "data": payload,
                        "context": request,
                    },
                    "metadata": {}, "route": [], "node": None,
                    "target_site_id": None, "target_pubkey": None, "source_peer": None,
                }
                frame = {
                    "msg_type": "query", "payload": inner,
                    "metadata": {"query_id": query_id}, "route": [], "node": None,
                    "target_site_id": None, "target_pubkey": None, "source_peer": None,
                }
            if terminal or cancellation.is_set():
                return
            if time.monotonic() >= collection_deadline():
                finish_at_deadline()
                return
            # Admission is atomic with the final deadline/cancellation check.
            # An admitted write may finish late, but is never replayed.
            if published is not None:
                published.set()
            if frame is not None:
                await self._link.send_hive_message(frame, encrypt=True)
            else:
                await self._link.emit_event(EVENT_RECOGNIZER_LOOP_UTTERANCE, payload, request)

        async def run() -> None:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise timeout_error()
                # _connect owns the raw write and any delayed cleanup until they
                # settle, even when the query caller has already returned.
                await self._connect(remaining, cancellation=cancellation, operation=send)
                stopped = self._link.stopped()
                while not done.is_set() and not cancellation.is_set():
                    if time.monotonic() >= collection_deadline():
                        finish_at_deadline()
                        return
                    await _wait_any(
                        (done, cancellation, stopped),
                        _SETTLE_POLL if stopped is None else collection_deadline() - time.monotonic(),
                    )
                    if done.is_set() or cancellation.is_set():
                        return
                    await self._raise_if_transport_stopped(
                        max(0.0, min(_SETTLE_POLL, collection_deadline() - time.monotonic()))
                    )
            except BaseException as error:  # noqa: BLE001 - handed to the caller
                if isinstance(error, ThalovantConnectionError) and isinstance(
                    error.__cause__, ThalovantTimeoutError
                ):
                    error = timeout_error()
                fail(error)

        self._keep(asyncio.ensure_future(run()))
        completed = False
        try:
            native = self._link.native
            while not done.is_set():
                if cancellation.is_set():
                    fail(ThalovantConnectionError("Hub request was cancelled."))
                    break
                remaining = collection_deadline() - time.monotonic()
                if remaining <= 0:
                    finish_at_deadline()
                    break
                if native:
                    moved.clear()
                    await _wait_any((done, moved, cancellation), remaining)
                else:
                    # A transport written synchronously delivers a burst from
                    # one thread, a frame at a time; the window is read at the
                    # same cadence it always was, so a burst lands whole.
                    await _wait_any((done, cancellation), min(_SETTLE_POLL, remaining))
            if errors:
                raise errors[0]
            failure_event = failure_event or soft_failure_event
            if failure_event is not None and not fragments:
                # Typed, so a caller can tell a refusal from a question the hub
                # cannot answer from a fault.
                raise failure_error(failure_event)
            if not fragments:
                raise ThalovantTimeoutError("Hub finished the query but did not emit a speak reply.")
            reply_session_id = (
                next(
                    (value for value in (event.session_id for event in events) if value and value.strip()),
                    None,
                )
                or _session_id_from_context(request)
            )
            # And under exactly the id the caller is about to be handed.
            returned_session_id[0] = reply_session_id
            if handled_session and reply_session_id:
                keys, carried = handled_session[0]
                if reply_session_id not in keys:
                    self._remember_conversation([*keys, reply_session_id], carried)
            reply = ThalovantReply(
                text=" ".join(fragments),
                utterances=tuple(fragments),
                handled=failure_event is None,
                session_id=reply_session_id,
                request_id=request_id,
                raw_messages=tuple(raw_messages),
                events=tuple(events),
                failure_event=failure_event,
                dropped_media=dropped_media,
            )
            completed = True
            return reply
        finally:
            # Successful collection need not retire a healthy admitted write.
            # _connect retains ownership and its original send deadline until
            # the physical write completes, even after this caller returns.
            if not completed:
                cancellation.set()
            terminal = True
            owned_handlers = tuple(registered)
            registered.clear()

            def drop(kind: str, handler: Callable[[Any], None]) -> None:
                try:
                    self._link.unbind(channel, kind, handler)
                except ThalovantConnectionError:
                    pass

            for kind, handler in owned_handlers:
                if completed and not direct and not handled_session and kind == EVENT_UTTERANCE_HANDLED:
                    # Answered, but the hub has not yet said what the
                    # conversation now is. A short settle window -- zero, most
                    # of all -- finishes the reply before
                    # ``ovos.utterance.handled`` arrives, and dropping the
                    # subscription here loses the carry the next turn needs.
                    # The handler records it ahead of every other gate, so
                    # leaving this one registered for a bounded moment is all
                    # it takes. Everything else goes now.
                    loop.call_later(CARRY_GRACE_SECONDS, drop, kind, handler)
                    continue
                drop(kind, handler)

    # -- helpers ------------------------------------------------------------------

    async def _with_reconnect(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        last_error: BaseException | None = None
        attempts = self.reconnect_attempts + 1 if self.auto_reconnect else 1
        for attempt in range(attempts):
            try:
                await self.connect()
            except (ConnectionAbortedError, RuntimeError, ThalovantConnectionError) as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                await self.close()
            else:
                # Publication can succeed remotely even if its local write
                # reports a failure. Reconnect only before invoking it.
                return await operation()
        raise ThalovantConnectionError("HiveMind transport failed before publication.") from last_error

    async def _raise_if_transport_stopped(self, timeout: float | None = None) -> None:
        state = await self._link.probe(timeout)
        if state is None or state:
            return
        error = self._link.last_error()
        detail = f": {_redact_error_text(error)}" if error else ""
        raise ThalovantConnectionError(f"HiveMind transport stopped{detail}")

    def _context_with_identity_metadata(self, context: dict[str, Any] | None) -> dict[str, Any]:
        merged = dict(context or {})
        if self.identity.metadata:
            merged["metadata"] = {
                **dict(self.identity.metadata),
                **dict(merged.get("metadata") or {}),
            }
        return merged

    # -- the hub's intents ----------------------------------------------------------

    async def intents(
        self,
        languages: Iterable[str] | None = None,
        *,
        timeout: float = 5.0,
        describe: bool = True,
        fallback: bool = True,
        nearest: bool = True,
    ) -> HubIntentInventory:
        """Everything the hub can be asked, per language, grouped by skill.

        See :meth:`ThalovantClient.intents`.
        """
        from . import intents as _intents

        return await _intents._inventory(
            self, _languages(languages, self._default_lang()), timeout=timeout,
            describe=describe, fallback=fallback, nearest=nearest,
        )

    async def list_intents(
        self,
        lang: str | None = None,
        *,
        timeout: float = 5.0,
        include_definitions: bool = False,
    ) -> list[IntentRegistration]:
        """The hub's intent manifest for one language, one row per registration."""
        from . import intents as _intents

        return await _intents._list_intents(
            self, lang or self._default_lang(), timeout=timeout,
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
        """The registrations behind one intent in one language, sentences included."""
        from . import intents as _intents

        return await _intents._describe_intent(
            self, skill_id, intent_name, lang or self._default_lang(), timeout=timeout
        )

    @staticmethod
    def _default_lang() -> str:
        return "en-us"


def _conversation_fields() -> tuple[str, ...]:
    return CONVERSATION_SESSION_FIELDS


def _schedule_expiry(
    loop: asyncio.AbstractEventLoop, delay: float, callback: Callable[[], None]
) -> asyncio.TimerHandle:
    """When a listener retires on its own; a seam the tests order events by."""
    return loop.call_later(delay, callback)


def _remaining(deadline: float | None, cap: float | None = None) -> float | None:
    if deadline is None:
        return cap
    remaining = max(0.0, deadline - time.monotonic())
    return remaining if cap is None else min(cap, remaining)


def _bus_collector(name: str, handle: Callable[[Any], None]) -> Callable[[Any], None]:
    def collector(raw: Any) -> None:
        handle(_event_from_message(name, raw))

    return collector


def _loop_handler(handler: Callable[[Any], Any]) -> Callable[[Any], None]:
    """A handler run on the loop; a coroutine it returns is scheduled."""

    def run(frame: Any) -> None:
        _run_handler(handler, frame)

    return run


def _languages(languages: Iterable[str] | None, default: str) -> list[str]:
    # A str is an Iterable[str], so intents("en-us") would otherwise expand
    # into ["e", "n", "-", "u", "s"] and ask the hub five nonsense manifest
    # queries. The empty check stays first so that "" keeps falling back to
    # the default language rather than becoming a single blank tag that
    # inventory() then rejects.
    if not languages:
        return [default]
    if isinstance(languages, str):
        return [languages]
    return list(languages)


def _doctor_identity(identity: ThalovantIdentity) -> str:
    identity.as_dict(include_secrets=False)
    return f"site_id={identity.site_id}"


def _doctor_endpoint(identity: ThalovantIdentity) -> str:
    parsed = urlparse(identity.default_master)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("default_master must start with http:// or https://")
    if not parsed.netloc:
        raise ValueError("default_master must include a host")
    if identity.default_port <= 0:
        raise ValueError("default_port must be positive")
    return identity.endpoint_base()


def _stop_threads(runner: _LoopThread, callbacks: _CallbackThread, core: AsyncThalovantClient) -> None:
    callbacks.stop()
    runner.stop(core._abandon, timeout=2.0)


def _retrieve(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


# -- the synchronous client -------------------------------------------------------


#: Attributes of the core a caller may set through the sync client.
_FORWARDED = frozenset(
    {
        "identity",
        "useragent",
        "self_signed",
        "reply_settle_seconds",
        "empty_reply_wait_seconds",
        "auto_reconnect",
        "reconnect_attempts",
        "_hard_connect_timeout",
        "_connected",
        "_transport",
        "_conversations",
    }
)


class ThalovantClient:
    """The synchronous client, over :class:`AsyncThalovantClient`.

    Each call runs on a private event-loop thread and returns when the
    asyncio core does, under the same deadlines. Handlers registered with
    :meth:`on`, :meth:`on_hive` and :meth:`on_binary` run on a thread of their
    own, one at a time, in arrival order: a handler that blocks holds up the
    next frame, never the connection, and may call this client.
    """

    MAX_REMEMBERED_CONVERSATIONS = AsyncThalovantClient.MAX_REMEMBERED_CONVERSATIONS
    MAX_CONVERSATION_ALIASES = AsyncThalovantClient.MAX_CONVERSATION_ALIASES

    _core: AsyncThalovantClient
    _runner: _LoopThread
    _callbacks: _CallbackThread

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
        self_signed: bool = False,
    ) -> None:
        core = AsyncThalovantClient(
            identity,
            useragent=useragent,
            connect_timeout=connect_timeout,
            handshake_timeout=handshake_timeout,
            send_timeout=send_timeout,
            reply_settle_seconds=reply_settle_seconds,
            empty_reply_wait_seconds=empty_reply_wait_seconds,
            auto_reconnect=auto_reconnect,
            reconnect_attempts=reconnect_attempts,
            protocol=protocol,
            transport=transport,
            noise_state_dir=noise_state_dir,
            self_signed=self_signed,
        )
        runner = _LoopThread("thalovant-client")
        callbacks = _CallbackThread("thalovant-handlers")
        object.__setattr__(self, "_core", core)
        object.__setattr__(self, "_runner", runner)
        object.__setattr__(self, "_callbacks", callbacks)
        core._limits = type(self)
        # A client nobody holds any more takes its threads, and its
        # connection, with it.
        weakref.finalize(self, _stop_threads, runner, callbacks, core)

    def __getattr__(self, name: str) -> Any:
        # Private state the tests and older callers read lives on the core.
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_core"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _FORWARDED:
            setattr(self._core, name, value)
        else:
            object.__setattr__(self, name, value)

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        return self._runner.run(coro)

    @classmethod
    def from_identity_file(cls, path: str | Path, **kwargs: Any) -> ThalovantClient:
        """Create a client from a Thalovant/HiveMind identity JSON file."""
        return cls(ThalovantIdentity.from_file(path), **kwargs)

    @classmethod
    def from_env(cls, **kwargs: Any) -> ThalovantClient:
        """Create a client from `THALOVANT_*` environment variables."""
        return cls(ThalovantIdentity.from_env(), **kwargs)

    @classmethod
    def from_config(
        cls,
        path: str | Path | None = None,
        *,
        profile: str | None = None,
        **kwargs: Any,
    ) -> ThalovantClient:
        """Create a client from the per-user Thalovant YAML config."""
        return cls(ThalovantIdentity.from_config(path, profile=profile), **kwargs)

    def __enter__(self) -> ThalovantClient:
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
        return ThalovantConversation(self, session_id=session_id, lang=lang, context=context)

    def connect(self, timeout: float | None = None) -> None:
        """Reach authenticated readiness within one caller deadline.

        Timed-out work retains the lifecycle lock until its connect and cleanup
        finish, so a later attempt cannot replace or be closed by that session.
        """
        self._run(self._core.connect(timeout))

    def connect_with_info(self, timeout: float | None = None) -> ThalovantConnectionInfo:
        """Connect and return the transport timing snapshot."""
        self.connect(timeout=timeout)
        return self.connection_info()

    def connection_info(self) -> ThalovantConnectionInfo:
        """Return connection timing for the current or most recent transport."""
        return self._core._link.connection_info()

    def close(self, timeout: float | None = None) -> None:
        """Cancel pending work and close within a caller budget.

        A timeout retains cleanup ownership. Use ``wait_closed`` to observe
        actual completion before handing this identity to another client.
        """
        self._run(self._core.close(timeout))
        self._callbacks.stop()

    def wait_closed(self, timeout: float | None = None) -> None:
        """Wait for actual pending cleanup; no timeout means wait until retired."""
        if not self._runner.running:
            if self._core._close_errors:
                raise self._core._close_errors[0]
            return
        self._run(self._core.wait_closed(timeout))

    disconnect = close

    def healthcheck(self) -> ThalovantHealth:
        """Connect if needed and return the transport health snapshot."""
        self.connect()
        return self._core._link.healthcheck()

    def doctor(self) -> ThalovantDoctorReport:
        """Run identity, endpoint, connection, and transport diagnostics."""
        checks: list[ThalovantDoctorCheck] = []

        def check(name: str, operation: Callable[[], str]) -> None:
            started = time.monotonic()
            try:
                detail = operation()
                ok = True
            except Exception as exc:  # noqa: BLE001 - doctor reports every check's failure
                # doctor output is printed by the CLI; scrub any URL query
                # (which carries the data-plane access key) from the message.
                detail = _redact_error_text(exc)
                ok = False
            checks.append(
                ThalovantDoctorCheck(
                    name=name, ok=ok, detail=detail, duration_ms=(time.monotonic() - started) * 1000
                )
            )

        check("identity", self._doctor_identity)
        check("endpoint", self._doctor_endpoint)
        check("connect", self._doctor_connect)
        check("transport", self._doctor_transport)
        return ThalovantDoctorReport(
            identity=self.identity.as_dict(include_secrets=False), checks=tuple(checks)
        )

    def _doctor_identity(self) -> str:
        return _doctor_identity(self.identity)

    def _doctor_endpoint(self) -> str:
        return _doctor_endpoint(self.identity)

    def _doctor_connect(self) -> str:
        self.connect()
        return "connected and handshake completed"

    def _doctor_transport(self) -> str:
        health = self.healthcheck()
        if not health.ok:
            raise ThalovantConnectionError(str(health.as_dict()))
        return "polling thread alive"

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
        """Subscribe to a hub event and receive normalized `ThalovantEvent` objects.

        ``handler`` (and ``predicate``) run on this client's handler thread,
        in arrival order. A handler may call the client.
        """
        return self._run(
            self._core._on(
                event_name, handler, context=context, session_id=session_id,
                request_id=request_id, predicate=predicate, runner=self._callbacks,
                subscription_client=self,
            )
        )

    def _remove_subscription(self, event_name: str, handler: Callable[[Any], Any]) -> None:
        self._core._call_on_loop(self._core._remove_subscription, event_name, handler)

    def _add_subscription(self, event_name: str, handler: Callable[[Any], Any]) -> None:
        self._run(self._core._add_subscription(event_name, handler))

    def on_hive(self, kind: str, handler: Callable[[Any], None]) -> Callable[[], None]:
        """Listen to one of the hive's own frame kinds.

        A hub relays more than this client's conversation. ``broadcast`` is
        aimed down at every child, ``propagate`` walks the whole hive,
        ``escalate`` goes up to the parent, ``intercom`` is addressed node to
        node, and ``rendezvous`` is the mailbox peers use to find each other
        through NAT. See :data:`HIVE_KINDS`.

        Returns a callable that unsubscribes. The frame is handed over as the
        hub sent it -- a HiveMessage, not a normalized `ThalovantEvent` -- so
        nothing is lost in a shape this SDK does not model yet.

        ``handler`` runs on this client's handler thread, in arrival order,
        like every other subscription in this SDK. A handler that blocks holds
        up the next frame. ``AsyncThalovantClient`` runs it on its loop.
        """
        if kind not in HIVE_KINDS:
            raise ValueError(
                f"{kind!r} is not a hive frame kind; expected one of {', '.join(HIVE_KINDS)}."
            )
        return self._run(self._core._on_hive(kind, _OffLoop(handler, self._callbacks)))

    def on_binary(self, handler: Callable[[ThalovantBinary], None]) -> Callable[[], None]:
        """Listen for binary frames: rendered speech, and files.

        This is what a hub sends back for ``speak:synth`` -- the audio itself,
        so a client with no synthesiser can still speak -- and how it hands
        over a file. Returns a callable that unsubscribes.

        Delivered by subscription and not on a reply, because a binary frame
        carries no request id: it cannot be attributed to one ``ask()``. Its
        ``utterance`` is the only thread back to a turn.

        ``handler`` runs on this client's handler thread, in arrival order. A
        handler that blocks holds up the next frame, so hand slow work --
        decoding, playback, writing to disk -- to a thread or a queue of your
        own.
        """
        return self._run(self._core._on_binary(_OffLoop(handler, self._callbacks)))

    def propagate(
        self, event_type: str, data: dict[str, Any] | None = None, context: dict[str, Any] | None = None
    ) -> Any:
        """Send an event across the hive.

        Every node sees it once; the route each frame carries is what stops it
        going round for ever.
        """
        return self._run(self._core._send_hive(HIVE_PROPAGATE, event_type, data, context))

    def escalate(
        self, event_type: str, data: dict[str, Any] | None = None, context: dict[str, Any] | None = None
    ) -> Any:
        """Send an event up to the parent node."""
        return self._run(self._core._send_hive(HIVE_ESCALATE, event_type, data, context))

    def broadcast(
        self, event_type: str, data: dict[str, Any] | None = None, context: dict[str, Any] | None = None
    ) -> Any:
        """Send an event down to every child of this hub. **Admin only.**

        A hub requires both admin standing and the ``can_broadcast`` grant for
        this, and a client that sends one without them is not answered with an
        error -- it is **disconnected for misbehaviour**. The same is true of
        ``propagate`` and ``escalate`` where an operator has revoked their
        grants, which are on by default.

        Nothing here can check first: a hub's HELLO carries its public key, its
        peer name and its node id, and says nothing about what this client is
        allowed to do. So a refusal arrives as a closed socket on the next
        read, not as a raised exception from this call.
        """
        return self._run(self._core._send_hive(HIVE_BROADCAST, event_type, data, context))

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
        """Wait for one matching event within a connect/registration/wait budget."""
        return self._run(
            self._core._wait_for_event(
                event_name, timeout=timeout, predicate=predicate, context=context,
                session_id=session_id, request_id=request_id,
            )
        )

    def _wait_for_event(self, event_name: str, **kwargs: Any) -> ThalovantEvent:
        return self._run(self._core._wait_for_event(event_name, **kwargs))

    def listen(
        self,
        event_name: str,
        *,
        timeout: float | None = None,
        max_events: int | None = None,
        max_buffered_events: int = 256,
        predicate: EventPredicate | None = None,
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
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

    def _listen(self, event_name: str, **kwargs: Any) -> Iterator[ThalovantEvent]:
        stream = self._core._listen(event_name, **kwargs)

        async def step() -> ThalovantEvent:
            return await stream.__anext__()

        try:
            while True:
                try:
                    event = self._run(step())
                except StopAsyncIteration:
                    return
                yield event
        finally:
            if self._runner.running:
                self._run(stream.aclose())

    def emit(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Any:
        """Emit a raw OVOS/HiveMind bus event through the data plane."""
        return self._run(self._core.emit(event_type, data, context))

    def reply(
        self,
        event: ThalovantEvent | Any,
        msg_type: str,
        data: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Any:
        """Answer a message the hub sent, back along the route it came.

        See :meth:`AsyncThalovantClient.reply`.
        """
        return self._run(self._core.reply(event, msg_type, data, context))

    def _emit_query_with_timeout(
        self, event_type: str, data: dict[str, Any], context: dict[str, Any], timeout: float
    ) -> None:
        """Internal read query: one connect/send deadline, without replaying it."""
        self._run(self._core._emit_query_with_timeout(event_type, data, context, timeout))

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
        return self._run(
            self._core.send_utterance(
                text, lang=lang, context=context, session_id=session_id, request_id=request_id
            )
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
        return self._run(
            self._core.send_action(
                payload, title=title, lang=lang, context=context,
                session_id=session_id, request_id=request_id,
            )
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
        return self._run(
            self._core.send_code(
                value, kind=kind, label=label, lang=lang, context=context,
                session_id=session_id, request_id=request_id,
            )
        )

    def ask(
        self,
        text: str,
        *,
        timeout: float = 12.0,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        stt_lang: str | None = None,
        pipeline: Sequence[str] | None = None,
        location: Mapping[str, Any] | None = None,
    ) -> ThalovantReply:
        """Send a text utterance and wait for the hub's spoken reply.

        ``stt_lang`` is the language a recogniser decided on, sent as the
        hub's highest-priority language hint; ``pipeline`` names the intent
        stages to run, in order; ``location`` is ``build_location()``'s
        result, which outranks the hub's own configured place. All three are
        merged into ``context`` by ``request_context()``. Embedded skill
        sounds (``mycroft.audio.queue``) arrive in ``reply.media_events``, in
        order with the speech.
        """
        return self._ask(
            text, timeout=timeout, lang=lang,
            context=request_context(context, stt_lang=stt_lang, pipeline=pipeline, location=location),
            session_id=session_id, request_id=request_id,
        )

    def _ask(self, text: str, **kwargs: Any) -> ThalovantReply:
        return self._run(self._core._ask(text, **kwargs))

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

    def _query(self, text: str, **kwargs: Any) -> ThalovantReply:
        return self._run(self._core._query(text, **kwargs))

    def _raise_if_transport_stopped(self) -> None:
        self._run(self._core._raise_if_transport_stopped())

    def intents(
        self,
        languages: Iterable[str] | None = None,
        *,
        timeout: float = 5.0,
        describe: bool = True,
        fallback: bool = True,
        nearest: bool = True,
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
        return self._run(
            self._core.intents(
                languages, timeout=timeout, describe=describe, fallback=fallback, nearest=nearest
            )
        )

    def list_intents(
        self,
        lang: str | None = None,
        *,
        timeout: float = 5.0,
        include_definitions: bool = False,
    ) -> list[IntentRegistration]:
        """The hub's intent manifest for one language, one row per registration."""
        return self._run(
            self._core.list_intents(lang, timeout=timeout, include_definitions=include_definitions)
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
        return self._run(self._core.describe_intent(skill_id, intent_name, lang, timeout=timeout))

    @staticmethod
    def _default_lang() -> str:
        return "en-us"
