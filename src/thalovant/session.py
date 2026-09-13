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
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from .errors import ThalovantConnectionError, ThalovantRuntimeError

log = logging.getLogger("thalovant.session")

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


def _is_dead_socket(error: BaseException) -> bool:
    """Whether an exception says the session's socket is gone."""
    if isinstance(error, ConnectionError | ThalovantConnectionError):
        return True
    text = str(error).lower()
    return "closed" in text or "socket" in text


class HubSession:
    """One long-lived hub connection, reconnected only when it breaks.

    ``connect`` builds and connects a client (a :class:`ThalovantClient` or
    anything with ``ask``, ``emit``, ``on``, ``connection_info`` and
    ``close``). A broken connection costs one failed call: it is torn down and
    rebuilt, and the call retried once. Subscriptions made with :meth:`on` are
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
        """Subscribe on the current client and on every one built after it."""
        with self._lock:
            self._subscriptions.append((event_name, handler))
            client = self._client
        if client is not None:
            client.on(event_name, handler)

    # -- lifecycle ---------------------------------------------------------

    def _ensure(self) -> Any:
        with self._lock:
            if self._client is None:
                started = self._clock()
                try:
                    client = self._connect_fn()
                except Exception:
                    # Back off the unattended probe; the next question still
                    # tries for itself.
                    self._retry_at = self._clock() + self._retry_wait
                    self._retry_wait = self.policy.next_wait(self._retry_wait)
                    raise
                for event_name, handler in self._subscriptions:
                    client.on(event_name, handler)
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
        if self._clock() < self._retry_at:
            return
        if not self._warming.acquire(blocking=False):
            return

        def _connect() -> None:
            try:
                try:
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
            self._client = None
        with contextlib.suppress(Exception):
            client.close()

    def close(self) -> None:
        """Close the held client, if any."""
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()

    # -- calls -------------------------------------------------------------

    def ask(self, text: str, **kwargs: Any) -> Any:
        """``client.ask`` on a live session, rebuilt once if the socket is dead."""
        return self._call("ask", text, **kwargs)

    def emit(self, event_type: str, data: Any = None, context: Any = None) -> Any:
        """``client.emit`` on a live session, rebuilt once if the socket is dead."""
        return self._call("emit", event_type, data, context)

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        # A socket that died since the last call is cheaper to replace now
        # than to discover by waiting out the reply timeout.
        with self._lock:
            held = self._client
            stale = held is not None and not alive(held)
        if stale:
            self._drop(held)
        # Outside the try: a connect that fails is not a stale socket, and the
        # retry below would spend a second full handshake learning the same.
        client = self._ensure()
        with self._busy:
            try:
                return getattr(client, method)(*args, **kwargs)
            except ThalovantRuntimeError:
                # A refusal (quota, policy, an intent that raised) arrives on a
                # socket that just carried a message both ways, so it is
                # alive; tearing it down cost every denied request a reconnect.
                raise
            except Exception as error:
                if _is_dead_socket(error):
                    self._drop(client)
                    return getattr(self._ensure(), method)(*args, **kwargs)
                # Anything else, a hub timeout above all, may be a socket that
                # died quietly; keeping it stranded a client on a dead session
                # forever, so drop it and let the next call rebuild.
                self._drop(client)
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
    if "://" in text:
        return urlsplit(text).hostname or ""
    return text.split("/")[0].split(":")[0]


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
        if not self.address or not host or self.cooling_down:
            client = build(handshake_seconds)
            client.connect(timeout=connect_timeout)
            return client
        started = self._clock()
        client = build(self.handshake_seconds)
        try:
            with preferred_origin(host, self.address):
                client.connect(timeout=connect_timeout)
        except Exception as error:
            self._quiet_until = self._clock() + self.cooldown_seconds
            log.warning(
                "hub origin %s did not answer (%s); falling back to DNS for %ds",
                self.address, type(error).__name__, int(self.cooldown_seconds),
            )
            with contextlib.suppress(Exception):
                client.close()
            client = build(handshake_seconds)
            client.connect(timeout=connect_timeout)
            return client
        self._quiet_until = 0.0
        log.info("hub via %s in %dms", self.address, int((self._clock() - started) * 1000))
        return client
