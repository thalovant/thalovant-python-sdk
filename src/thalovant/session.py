"""One long-lived hub session, kept by policy rather than by luck.

A satellite and a connector each used to keep their own copy of this: a
client rebuilt when it broke, a retry ladder for the unattended attempts, a
liveness probe, and one origin tried before the public path. Each copy got a
detail wrong once -- a dead client left alive with its handler wired, a retry
window opening between two probes -- so the policy lives here, measured on
the appliance, and both consumers take it as it is.

The numbers are the appliance's. The hub link drops a few times a day and
its refusals clear in about a minute, so the ladder starts at ten seconds
and stops at two minutes: a ten-minute ceiling once slept through a whole
recovery, roughly 10.7 hours of dead session in one day. The probe comes
round every minute while a session is held and every five seconds while
none is, so a retry window that opens between two minute marks is honoured
when it opens; measured before that, two 502s from the gateway cost the room
two minutes of silence. A person asking is never held back by the ladder:
every ask tries for itself, and only the unattended attempts back off.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import math
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterator
from urllib.parse import urlsplit

from .errors import (
    ThalovantConnectionError,
    ThalovantHubKeyChangedError,
    ThalovantHubRefusedError,
    ThalovantRuntimeError,
    ThalovantTimeoutError,
)

log = logging.getLogger("thalovant.session")
_RESOLVER_LOCK = threading.RLock()

__all__ = [
    "AsyncHubSession",
    "HubSession",
    "HubSessionPolicy",
    "LinkDecision",
    "LinkSupervisor",
    "OriginPreference",
    "alive",
    "hub_hostname",
    "preferred_origin",
]


@dataclass(frozen=True)
class HubSessionPolicy:
    """How a session waits: the retry ladder and the probe cadence, in seconds."""

    #: After a failed connect, the wait before the next unattended attempt.
    retry_seconds: float = 10.0
    #: The ladder doubles towards this and stays there.
    retry_ceiling_seconds: float = 120.0
    #: How often a held session is checked for having died while idle.
    probe_seconds: float = 60.0
    #: How often the probe comes round while no session is held.
    probe_down_seconds: float = 5.0
    #: How long :meth:`AsyncHubSession.run` keeps trying through refusals
    #: before it gives up. A connection just created is refused until its hub
    #: has admitted it -- about ninety seconds -- so a refusal is only final
    #: once it has lasted this long.
    refusal_grace_seconds: float = 600.0

    def __post_init__(self) -> None:
        for name in ("retry_seconds", "retry_ceiling_seconds", "probe_seconds", "probe_down_seconds",
                     "refusal_grace_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.retry_ceiling_seconds < self.retry_seconds:
            raise ValueError("retry ceiling must not be below the initial wait")

    def next_wait(self, current: float) -> float:
        return min(current * 2, self.retry_ceiling_seconds)


@dataclass(frozen=True)
class LinkDecision:
    """What to do after one outcome of keeping a link up.

    ``action`` is ``"hold"`` (the link is up), ``"retry"`` after
    ``wait_seconds``, or ``"give_up"`` for ``reason`` -- ``"refused"`` or
    ``"key_changed"``.
    """

    action: str
    wait_seconds: float = 0.0
    reason: str | None = None


class LinkSupervisor:
    """How a long-lived link is kept up, as a pure function of what happened and when.

    :meth:`AsyncHubSession.run` asks it after every attempt; every SDK follows
    the same rules (``link-keeping-vectors.json``):

    - ``"up"``: hold, and start the ladder and the refusal clock afresh.
    - ``"dropped"`` (an established link went down): dial again at once.
    - ``"failed"`` (the hub or the network could not be reached): wait the
      ladder's step -- ``retry_seconds``, doubling to
      ``retry_ceiling_seconds`` -- and stop counting refusals.
    - ``"refused"``: the hub turned the credentials away. A new connection is
      refused until its hub admits it, so wait the ladder's step as for a
      failure, until the refusals have lasted ``refusal_grace_seconds`` since
      the first of them; then give up.
    - ``"key_changed"``: the hub's Noise key is not the pinned one. Retrying
      cannot change that, so give up at once.
    """

    OUTCOMES = ("up", "dropped", "failed", "refused", "key_changed")

    def __init__(self, policy: HubSessionPolicy | None = None) -> None:
        self.policy = policy or HubSessionPolicy()
        self._wait = float(self.policy.retry_seconds)
        self._refused_since: float | None = None

    def after(self, outcome: str, now: float) -> LinkDecision:
        """The decision after *outcome*, observed at *now* (seconds, any monotonic origin)."""
        if outcome == "up":
            self._wait = float(self.policy.retry_seconds)
            self._refused_since = None
            return LinkDecision("hold")
        if outcome == "dropped":
            return LinkDecision("retry", 0.0)
        if outcome == "key_changed":
            return LinkDecision("give_up", reason="key_changed")
        if outcome == "refused":
            if self._refused_since is None:
                self._refused_since = now
            if now - self._refused_since >= self.policy.refusal_grace_seconds:
                return LinkDecision("give_up", reason="refused")
        elif outcome == "failed":
            self._refused_since = None
        else:
            raise ValueError(f"unknown outcome {outcome!r}")
        wait, self._wait = self._wait, self.policy.next_wait(self._wait)
        return LinkDecision("retry", wait)


def alive(client: Any) -> bool:
    """Whether *client* still has a live transport, without dialling.

    Best effort and deliberately optimistic: anything unexpected reads as
    alive, because a probe that guessed "dead" would tear down a working
    session on every check. ``connection_info()`` reports the session without
    dialling; a ``healthcheck()`` would reconnect, the wrong question here.
    """
    if client is None:
        return False
    try:
        phase = getattr(client.connection_info(), "phase", None)
    except Exception:  # noqa: BLE001 - optimistic by design: see the docstring
        return True
    return phase not in ("closed", "error") if isinstance(phase, str) else True


class HubSession:
    """One long-lived hub connection, reconnected only when it breaks.

    ``connect`` builds and connects a client (a :class:`ThalovantClient` or
    anything with ``ask``, ``emit``, ``on``, ``connection_info`` and
    ``close``). A broken connection costs one failed call: it is torn down and
    rebuilt before the next call. An admitted call is never replayed because
    Ask can trigger actions and correlation IDs do not promise deduplication.
    Subscriptions made with :meth:`on` are
    wired onto every client the session builds, so a rebuild keeps them.
    """

    def __init__(
        self,
        connect: Callable[[], Any],
        *,
        policy: HubSessionPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
        warm: bool = True,
        thread_name: str = "thalovant-hub-session",
    ) -> None:
        self._connect_fn = connect
        self.policy = policy or HubSessionPolicy()
        self._client: Any = None
        self._retired: Any = None
        self._closed = False
        self._lock = threading.Lock()
        # Held for the duration of a call. The client reconnects inside ask()
        # and the transport reports not-connected meanwhile; a probe landing
        # then dropped a live client, and the ask completed on a session
        # nothing referenced -- one leaked permanent session per hit.
        self._busy = threading.Lock()
        # Held for one background connect, so a probe during a warm does not
        # start a second one.
        self._warming = threading.Lock()
        self._clock = clock
        self._thread_name = thread_name
        self._retry_at = 0.0
        self._retry_wait = float(self.policy.retry_seconds)
        self._subscriptions: list[tuple[str, Callable[[Any], None]]] = []
        if warm:
            self.warm()

    # -- state -------------------------------------------------------------

    @property
    def held(self) -> bool:
        """Whether a client is currently held (not whether it is alive)."""
        with self._lock:
            return self._client is not None

    @property
    def retry_at(self) -> float:
        """When the next unattended attempt may go out; 0 when it may now."""
        return self._retry_at

    @property
    def retry_wait(self) -> float:
        """The ladder's current rung."""
        return self._retry_wait

    def on(self, event_name: str, handler: Callable[[Any], None]) -> None:
        """Subscribe on the current client and on every one built after it.

        Registered on the held client before it is remembered: a registration
        the client refuses is not queued for every client after it.
        """
        with self._lock:
            if self._closed:
                raise ThalovantConnectionError("Hub session is closed")
            client = self._client
            if client is not None:
                client.on(event_name, handler)
            self._subscriptions.append((event_name, handler))

    # -- lifecycle ---------------------------------------------------------

    def _ensure(self) -> Any:
        with self._lock:
            if self._closed:
                raise ThalovantConnectionError("Hub session is closed")
            self._cleanup()
            if self._client is None:
                started = self._clock()
                try:
                    client = self._connect_fn()
                    try:
                        for event_name, handler in self._subscriptions:
                            client.on(event_name, handler)
                    except BaseException:
                        # A client that cannot carry the subscriptions is not
                        # kept half-wired and not leaked: closed, and counted
                        # as a failed attempt like a connect that never opened.
                        self._retired = client
                        self._cleanup()
                        raise
                except Exception:
                    # Back off the unattended probe; the next question still
                    # tries for itself.
                    self._retry_at = self._clock() + self._retry_wait
                    self._retry_wait = self.policy.next_wait(self._retry_wait)
                    raise
                self._client = client
                self._retry_at = 0.0
                self._retry_wait = float(self.policy.retry_seconds)
                log.info("hub session opened in %dms", int((self._clock() - started) * 1000))
            return self._client

    def warm(self) -> None:
        """Open the connection off-path so no interaction pays for it.

        A no-op inside the retry window. A handshake that is going to time out
        takes its full timeout to say so, and every wake word warms while every
        utterance asks, so an unreachable hub was dialled twice for one
        question: measured at thirteen seconds between the wake word and "I
        could not reach the hub". The utterance still asks for itself, so what
        this drops is the duplicate, not the attempt.
        """
        with self._lock:
            if self._closed or self._clock() < self._retry_at:
                return
        if not self._warming.acquire(blocking=False):
            return

        def _connect() -> None:
            try:
                try:
                    with self._busy:
                        self._ensure()
                except (TypeError, AttributeError, ImportError):
                    # A transport that cannot be *built* is a fault in the
                    # configuration or the installed SDK; it will not fix
                    # itself, and swallowing it leaves a client that answers
                    # nothing and logs no reason.
                    log.exception("the hub transport could not be built")
                except Exception:  # noqa: BLE001 - an unreachable hub is not news
                    pass
            finally:
                self._warming.release()

        try:
            threading.Thread(target=_connect, name=self._thread_name, daemon=True).start()
        except RuntimeError:
            # No thread to run the release: a lock left held would silence
            # every off-path reconnect for the life of the process.
            self._warming.release()
            raise

    def probe(self) -> None:
        """Rebuild a connection that died while nobody was speaking.

        The link drops a few times a day, and the client's own reconnect does
        not always beat the next call to it. Whichever call lands on the gap
        pays the full reply timeout and is lost; noticing it out here costs
        nothing and moves the reconnect off the path a person is waiting on.
        """
        if not self._busy.acquire(blocking=False):
            return  # a call is in flight; whatever it finds, it handles
        try:
            with self._lock:
                client = self._client
                dead = client is not None and not alive(client)
            if dead:
                log.info("hub session went away while idle; rebuilding")
                self._drop(client)
        finally:
            self._busy.release()
        if not self.held and self._clock() >= self._retry_at:
            self.warm()

    def probe_delay(self) -> float:
        """How long the probe loop should wait before its next look."""
        return float(self.policy.probe_seconds if self.held else self.policy.probe_down_seconds)

    def _cleanup(self) -> None:
        # Called within lifecycle admission. Failed close retains ownership;
        # a later call must finish retirement before building another client.
        if self._retired is not None:
            self._retired.close()
            self._retired = None

    def _drop(self, client: Any) -> None:
        """Close *client*, but only while it is still the one held.

        The judgement that a session is dead and the act of closing it happen
        at different moments, and a call or the probe can have replaced it in
        between. Closing whatever is current then would tear down the
        replacement, possibly with a call in flight on it.
        """
        if client is None:
            return
        with self._lock:
            if self._client is not client:
                return
            self._retired, self._client = client, None
        self._cleanup()

    def close(self) -> None:
        """Retire this session after its admitted call finishes.

        A queued warm or probe cannot reopen a closed session. Create a new
        HubSession to resume. Close may wait for an admitted call's budget.
        """
        with self._lock:
            self._closed = True
        with self._busy:
            with self._lock:
                client, self._client = self._client, None
            if client is not None:
                self._retired = client
            self._cleanup()

    # -- calls -------------------------------------------------------------

    def ask(self, text: str, **kwargs: Any) -> Any:
        """``client.ask`` on a live session, without replaying an ambiguous call."""
        return self._call("ask", text, **kwargs)

    def emit(self, event_type: str, data: Any = None, context: Any = None) -> Any:
        """``client.emit`` on a live session; a dead socket is dropped, not retried.

        An event can reach the hub before the response that says so reaches
        the client (the HTTP path loses a response now and then), and an
        event carries no identifier a hub could deduplicate on. The caller's
        outbox, which keeps the envelope until it is accepted, is where a
        retry belongs.
        """
        return self._call("emit", event_type, data, context)

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        # Admission includes choosing the client. A caller queued behind a
        # failed call must not keep the retired client it observed earlier;
        # a probe must not close a client during admission either.
        with self._busy:
            with self._lock:
                held = self._client
                stale = held is not None and not alive(held)
            if stale:
                self._drop(held)
            client = self._ensure()
            try:
                return getattr(client, method)(*args, **kwargs)
            except ThalovantRuntimeError:
                # A remote refusal proves a live, authenticated session.
                raise
            except Exception:
                self._drop(client)
                # No hub deduplication contract exists for Ask or Emit.
                # A lost response cannot prove the command was not accepted.
                raise


class AsyncHubSession:
    """One long-lived hub connection on asyncio, kept by policy: :class:`HubSession`'s twin.

    ``connect`` builds and connects a client -- an
    :class:`~thalovant.AsyncThalovantClient`, or anything with ``on``,
    ``reply``, ``ask``, ``emit`` and ``close`` -- and returns it;
    :meth:`for_identity` builds that for an identity. Subscriptions made with
    :meth:`on` are wired onto every client the session builds.

    :meth:`connect` makes one attempt. :meth:`run` stays connected until
    :meth:`close`: after a failed attempt it waits ``retry_seconds``, doubling
    up to ``retry_ceiling_seconds``, and a held link is looked at every
    ``probe_seconds``. A link that :meth:`connect` already opened is the one
    :meth:`run` keeps; it does not dial again. A hub that refuses the
    credentials is retried like any other failure until the refusals have
    lasted ``refusal_grace_seconds`` -- a new connection is refused until its
    hub admits it -- and then :meth:`run` raises
    :class:`~thalovant.errors.ThalovantHubRefusedError`. A hub whose Noise key
    is not the pinned one ends :meth:`run` at once with
    :class:`~thalovant.errors.ThalovantHubKeyChangedError`: retrying cannot
    change it. :class:`LinkSupervisor` holds these rules. A close with a
    refusal code within ``settle_seconds`` (0.75) of the handshake is a
    refusal: a hub that does not know the client's key says so only that way.
    Every attempt, drop and recovery is logged at DEBUG on
    ``thalovant.session``; what deserves more is for the application to say.
    """

    def __init__(
        self,
        connect: Callable[[], Awaitable[Any]],
        *,
        policy: HubSessionPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
        settle_seconds: float = 0.75,
    ) -> None:
        if not math.isfinite(settle_seconds) or settle_seconds < 0:
            raise ValueError("settle_seconds must be finite and not negative")
        self._connect_fn = connect
        self.policy = policy or HubSessionPolicy()
        self._clock = clock
        #: How long a new link must stay up before it counts: a hub that does
        #: not know the client's static key says so only by closing right
        #: after the handshake.
        self.settle_seconds = settle_seconds
        self._client: Any = None
        self._closed = False
        self._subscriptions: list[tuple[str, Callable[[Any], Any]]] = []
        # (event, handler, subscription, client) for every binding made.
        self._bound: list[tuple[str, Callable[[Any], Any], Any, Any]] = []
        self._state_callbacks: list[Callable[[bool], Any]] = []
        self._state = False
        self._supervisor = LinkSupervisor(self.policy)
        self._lock: asyncio.Lock | None = None
        self._wake: asyncio.Event | None = None

    @classmethod
    def for_identity(
        cls,
        identity: Any,
        *,
        session: Any = None,
        policy: HubSessionPolicy | None = None,
        settle_seconds: float = 0.75,
        **client_kwargs: Any,
    ) -> AsyncHubSession:
        """A session whose clients connect with *identity*, over *session* when given."""
        from .client import AsyncThalovantClient

        async def connect() -> Any:
            client = AsyncThalovantClient(identity, session=session, **client_kwargs)
            try:
                await client.connect()
            except BaseException:
                with contextlib.suppress(Exception):
                    await client.close()
                raise
            return client

        return cls(connect, policy=policy, settle_seconds=settle_seconds)

    # -- state -------------------------------------------------------------

    @property
    def held(self) -> bool:
        """Whether a client is currently held (not whether it is alive)."""
        return self._client is not None

    @property
    def connected(self) -> bool:
        """Whether a client is held and its link is up."""
        return self._client is not None and _alive_now(self._client)

    @property
    def client(self) -> Any:
        """The client currently held, if any."""
        return self._client

    def on_state_change(self, callback: Callable[[bool], Any]) -> Callable[[], None]:
        """Call *callback* with ``True``/``False`` whenever the link comes up or goes down."""
        self._state_callbacks.append(callback)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._state_callbacks.remove(callback)

        return unsubscribe

    def _set_state(self, up: bool) -> None:
        if up == self._state:
            return
        self._state = up
        for callback in tuple(self._state_callbacks):
            try:
                result = callback(up)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:
                log.exception("a state callback raised")

    def on(self, event_name: str, handler: Callable[[Any], Any]) -> Callable[[], None]:
        """Subscribe on the current client and on every one built after it. Returns an unsubscriber."""
        if self._closed:
            raise ThalovantConnectionError("Hub session is closed")
        entry = (event_name, handler)
        self._subscriptions.append(entry)
        if self._client is not None:
            self._bind(self._client, event_name, handler)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._subscriptions.remove(entry)
            for bound in tuple(self._bound):
                if bound[0] == event_name and bound[1] is handler:
                    self._bound.remove(bound)
                    _close_subscription(bound[2])

        return unsubscribe

    def _bind(self, client: Any, event_name: str, handler: Callable[[Any], Any]) -> None:
        self._bound.append((event_name, handler, client.on(event_name, handler), client))

    # -- lifecycle -----------------------------------------------------------

    def _guard(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _waker(self) -> asyncio.Event:
        if self._wake is None:
            self._wake = asyncio.Event()
        return self._wake

    async def connect(self) -> None:
        """Make one attempt: return with a live link, or raise why there is none.

        Raises :class:`ThalovantHubRefusedError` when the hub turns the
        credentials away and :class:`ThalovantConnectionError` (or
        :class:`ThalovantTimeoutError`) for everything else.
        """
        async with self._guard():
            if self._closed:
                raise ThalovantConnectionError("Hub session is closed")
            if self._client is not None:
                if _alive_now(self._client):
                    return
                await self._drop()
            log.debug("hub link: connecting")
            client = await self._connect_fn()
            try:
                for event_name, handler in self._subscriptions:
                    self._bind(client, event_name, handler)
                await self._settle(client)
            except BaseException:
                await self._retire(client)
                raise
            self._client = client
            self._supervisor.after("up", self._clock())
            log.debug("hub link: up")
            self._set_state(True)

    async def _settle(self, client: Any) -> None:
        stopped = _stopped_event(client)
        if stopped is None or self.settle_seconds <= 0:
            return
        try:
            await asyncio.wait_for(asyncio.shield(stopped.wait()), self.settle_seconds)
        except asyncio.TimeoutError:
            return
        if _refused(client):
            raise ThalovantHubRefusedError(
                "The hub closed the link right after the handshake: it does not accept these credentials, or not yet."
            )
        raise ThalovantConnectionError("The hub closed the link right after the handshake.")

    async def run(self) -> None:
        """Stay connected until :meth:`close`, by policy; see the class docstring."""
        wake = self._waker()
        while not self._closed:
            if self._client is not None and _alive_now(self._client):
                stopped = _stopped_event(self._client)
                waiters = [asyncio.ensure_future(wake.wait())]
                if stopped is not None:
                    waiters.append(asyncio.ensure_future(stopped.wait()))
                try:
                    await asyncio.wait(waiters, timeout=self.policy.probe_seconds,
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for waiter in waiters:
                        waiter.cancel()
                wake.clear()
                if self._closed:
                    break
                if self._client is not None and not _alive_now(self._client):
                    log.debug("hub link: dropped")
                    async with self._guard():
                        await self._drop()
                continue
            try:
                await self.connect()
            except ThalovantHubKeyChangedError as changed:
                log.debug("hub link: the hub's key changed (%s)", changed)
                self._supervisor.after("key_changed", self._clock())
                raise
            except ThalovantHubRefusedError as refusal:
                log.debug("hub link: refused (%s)", refusal)
                decision = self._supervisor.after("refused", self._clock())
                if decision.action == "give_up":
                    raise
            except (ThalovantConnectionError, ThalovantTimeoutError, OSError) as failure:
                log.debug("hub link: attempt failed (%s)", failure)
                decision = self._supervisor.after("failed", self._clock())
            else:
                continue
            if self._closed or (self._client is not None and _alive_now(self._client)):
                continue
            log.debug("hub link: next attempt in %.0fs", decision.wait_seconds)
            try:
                await asyncio.wait_for(wake.wait(), decision.wait_seconds)
            except asyncio.TimeoutError:
                pass
            wake.clear()

    async def _drop(self) -> None:
        client, self._client = self._client, None
        self._set_state(False)
        if client is not None:
            await self._retire(client)

    async def _retire(self, client: Any) -> None:
        self._bound = [bound for bound in self._bound if bound[3] is not client]
        with contextlib.suppress(Exception):
            await client.close()

    async def close(self) -> None:
        """Close the link and stop :meth:`run`. A closed session cannot be reopened."""
        self._closed = True
        self._waker().set()
        async with self._guard():
            await self._drop()

    # -- calls ---------------------------------------------------------------

    async def ask(self, text: str, **kwargs: Any) -> Any:
        """``client.ask`` on a live link, without replaying an ambiguous call."""
        return await self._call("ask", text, **kwargs)

    async def emit(self, event_type: str, data: Any = None, context: Any = None) -> Any:
        """``client.emit`` on a live link; a dead socket is dropped, not retried."""
        return await self._call("emit", event_type, data, context)

    async def reply(self, event: Any, msg_type: str, data: Any = None, context: Any = None) -> Any:
        """``client.reply``: answer a message back along the route it came."""
        return await self._call("reply", event, msg_type, data, context)

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._client is None or not _alive_now(self._client):
            await self.connect()
        client = self._client
        try:
            return await getattr(client, method)(*args, **kwargs)
        except ThalovantRuntimeError:
            # A remote refusal proves a live, authenticated session.
            raise
        except Exception:
            async with self._guard():
                if self._client is client:
                    await self._drop()
            raise


def _alive_now(client: Any) -> bool:
    """Whether *client* still has a live link, without dialling or awaiting."""
    link = getattr(client, "_link", None)
    if link is not None:
        try:
            phase = link.connection_info().phase
        except Exception:  # noqa: BLE001 - optimistic by design, as alive()
            return True
        return phase not in ("closed", "error")
    return alive(client) if not inspect.iscoroutinefunction(getattr(client, "connection_info", None)) else True


def _stopped_event(client: Any) -> asyncio.Event | None:
    link = getattr(client, "_link", None)
    stopped = getattr(link, "stopped", None)
    return stopped() if callable(stopped) else None


def _refused(client: Any) -> bool:
    transport = getattr(getattr(client, "_link", None), "transport", None)
    return bool(getattr(transport, "closed_refused", False))


def _close_subscription(subscription: Any) -> None:
    close = getattr(subscription, "close", subscription)
    if callable(close):
        with contextlib.suppress(Exception):
            close()


# -- one origin before the public path ------------------------------------


def hub_hostname(default_master: Any) -> str:
    """The bare hostname out of an identity's master address.

    It arrives as a URL (``wss://<uuid>.thalovant.io``) and the resolver needs
    the host on its own. ``""`` for anything unrecognisable, which a caller
    treats as "no shortcut available" rather than guessing.
    """
    text = str(default_master or "").strip()
    if not text:
        return ""
    try:
        return urlsplit(text if "://" in text else "wss://" + text).hostname or ""
    except ValueError:
        return ""


@contextlib.contextmanager
def preferred_origin(host: str, address: str) -> Iterator[None]:
    """Resolve one hostname to one address, for the life of this block.

    Scoped to the process and to the block rather than written into
    ``/etc/hosts``, because a pin has no way back: when an origin stopped
    completing handshakes for two hours, a hosts entry would have been a
    two-hour outage. The hostname is untouched, so SNI, the Host header, the
    certificate and the gateway route are identical to the public path; only
    the hop through the tunnel is skipped. Every other host falls through to
    the real resolver.
    """
    # The resolver is process-wide, so the override is taken one block at a
    # time: two overlapping blocks would otherwise restore each other's
    # wrapper and leave one installed for good.
    with _RESOLVER_LOCK:
        real = socket.getaddrinfo

        def resolve(node: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
            target = address if node == host else node
            return real(target, port, *args, **kwargs)

        socket.getaddrinfo = resolve  # type: ignore[assignment]
        try:
            yield
        finally:
            socket.getaddrinfo = real


class OriginPreference:
    """Try the hub through one address first, then the public path.

    A LAN origin answers in a fraction of the tunnel's time but is
    intermittent, and a failed attempt costs its whole handshake timeout
    before the fallback begins. So the short path gets its own, shorter
    handshake budget, and after a failure it is left alone for
    ``cooldown_seconds``. It is an optimisation, never a dependency: it once
    accepted the socket and sent no HELLO for two hours while the long way
    round answered in two seconds.
    """

    def __init__(
        self,
        address: str,
        *,
        handshake_seconds: float = 1.5,
        cooldown_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        for value in (handshake_seconds, cooldown_seconds):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("origin budgets must be finite and positive")
        self._connect_lock = threading.Lock()
        self._retired: Any = None
        self.address = str(address or "").strip()
        self.handshake_seconds = handshake_seconds
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._quiet_until = 0.0

    @property
    def cooling_down(self) -> bool:
        return self._clock() < self._quiet_until

    def connect(
        self,
        build: Callable[[float | None], Any],
        *,
        host: str,
        connect_timeout: float,
        handshake_seconds: float | None = None,
    ) -> Any:
        """A connected client: through the preferred address when it is worth
        trying, through the public path otherwise.

        ``build(handshake_seconds)`` returns an unconnected client.
        """
        with self._connect_lock:
            self._cleanup()
            if not self.address or not host or self.cooling_down:
                return self._dial(build, handshake_seconds, connect_timeout)
            started = self._clock()
            try:
                with preferred_origin(host, self.address):
                    client = self._dial(build, self.handshake_seconds, connect_timeout)
            except Exception as error:
                # Failed retirement still owns a transport. Do not open a
                # replacement until close succeeds, including on the next call.
                if self._retired is not None:
                    raise
                self._quiet_until = self._clock() + self.cooldown_seconds
                log.warning(
                    "hub origin %s did not answer (%s); falling back to DNS for %ds",
                    self.address, type(error).__name__, int(self.cooldown_seconds),
                )
                return self._dial(build, handshake_seconds, connect_timeout)
            self._quiet_until = 0.0
            log.info("hub via %s in %dms", self.address, int((self._clock() - started) * 1000))
            return client

    def _dial(self, build: Callable[[float | None], Any], handshake: float | None, timeout: float) -> Any:
        client = build(handshake)
        try:
            client.connect(timeout=timeout)
            return client
        except BaseException:
            self._retired = client
            self._cleanup()
            raise

    def _cleanup(self) -> None:
        if self._retired is not None:
            self._retired.close()
            self._retired = None

    def close(self) -> None:
        """Retry retirement of a failed attempt; successful clients belong to the caller."""
        with self._connect_lock:
            self._cleanup()
