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

import contextlib
import logging
import math
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from .errors import ThalovantConnectionError, ThalovantRuntimeError

log = logging.getLogger("thalovant.session")
_RESOLVER_LOCK = threading.RLock()

__all__ = [
    "HubSession",
    "HubSessionPolicy",
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

    def __post_init__(self) -> None:
        for name in ("retry_seconds", "retry_ceiling_seconds", "probe_seconds", "probe_down_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.retry_ceiling_seconds < self.retry_seconds:
            raise ValueError("retry ceiling must not be below the initial wait")

    def next_wait(self, current: float) -> float:
        return min(current * 2, self.retry_ceiling_seconds)


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
    except Exception:
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
def preferred_origin(host: str, address: str):
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

        def resolve(node, port, *args, **kwargs):
            target = address if node == host else node
            return real(target, port, *args, **kwargs)

        socket.getaddrinfo = resolve
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
