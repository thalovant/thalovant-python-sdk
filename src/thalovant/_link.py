"""What the client drives: a native asyncio transport, or a synchronous one.

The client speaks to one interface. :class:`NativeLink` passes straight
through to :class:`thalovant._hive.AsyncHiveMindTransport`. :class:`SyncLink`
presents a transport written synchronously -- the ``transport=`` argument,
MQTT, and every test double -- to the same interface:

- each blocking call runs on a thread of its own, so a transport that hangs
  holds up that call and nothing else;
- a status probe has at most one call in flight, and a caller waits for it no
  longer than its own deadline;
- a handler that belongs on the loop is reached from the transport's thread
  through the loop, and that thread waits until the loop has run it, so a
  transport that delivers inline still sees its handlers finish in order;
- a synchronous handler (:class:`~thalovant._loop.OffLoop`) is handed to the
  transport as it is and runs on the transport's own thread, as it always did.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from typing import Any, Callable, Protocol

from ._hive import AsyncHiveMindTransport
from ._loop import OffLoop, in_thread, on_loop_thread, wait_future
from .models import ThalovantConnectionInfo, ThalovantHealth

__all__ = ["Link", "NativeLink", "SyncLink", "link_for"]

log = logging.getLogger("thalovant.transport")

#: How a subscription is addressed: a bus event by name, a hive frame by kind,
#: or every binary frame.
BUS, HIVE, BINARY = "bus", "hive", "binary"


class Link(Protocol):
    native: bool
    transport: Any

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def retire(self) -> None: ...
    async def probe(self, timeout: float | None) -> bool | None: ...
    def stopped(self) -> asyncio.Event | None: ...
    def last_error(self) -> BaseException | None: ...
    def connection_info(self) -> ThalovantConnectionInfo: ...
    def healthcheck(self) -> ThalovantHealth: ...
    def session_token(self) -> Any: ...
    def supports_query(self) -> bool: ...
    async def bind(self, channel: str, name: str, handler: Callable[[Any], Any]) -> None: ...
    def bind_now(self, channel: str, name: str, handler: Callable[[Any], Any]) -> None: ...
    def unbind(self, channel: str, name: str, handler: Callable[[Any], Any]) -> None: ...
    def discard(self, channel: str, name: str, handler: Callable[[Any], Any]) -> None: ...
    async def emit_event(self, event_type: str, data: dict[str, Any], context: dict[str, Any]) -> Any: ...
    async def send_hive_message(self, message: dict[str, Any], *, encrypt: bool = True) -> Any: ...
    async def send_hive_frame(
        self, kind: str, event_type: str, data: dict[str, Any], context: dict[str, Any]
    ) -> Any: ...
    async def release(self) -> None: ...


class NativeLink:
    native = True

    def __init__(self, transport: AsyncHiveMindTransport) -> None:
        self.transport = transport

    async def connect(self) -> None:
        await self.transport.connect()

    async def disconnect(self) -> None:
        await self.transport.disconnect()

    async def retire(self) -> None:
        await self.transport._retire_connection()

    async def probe(self, timeout: float | None) -> bool | None:
        return self.transport.is_connected()

    def stopped(self) -> asyncio.Event | None:
        return self.transport.stopped

    def last_error(self) -> BaseException | None:
        return self.transport.last_error()

    def connection_info(self) -> ThalovantConnectionInfo:
        return self.transport.connection_info()

    def healthcheck(self) -> ThalovantHealth:
        return self.transport.healthcheck()

    def session_token(self) -> Any:
        return self.transport.session_token()

    def supports_query(self) -> bool:
        return True

    async def bind(self, channel: str, name: str, handler: Callable[[Any], Any]) -> None:
        if channel == BUS:
            self.transport.on_mycroft(name, handler)
        elif channel == HIVE:
            self.transport.on_hive_message(name, handler)
        else:
            self.transport.on_binary(handler)

    def bind_now(self, channel: str, name: str, handler: Callable[[Any], Any]) -> None:
        if channel == BUS:
            self.transport.on_mycroft(name, handler)
        elif channel == HIVE:
            self.transport.on_hive_message(name, handler)
        else:
            self.transport.on_binary(handler)

    def unbind(self, channel: str, name: str, handler: Callable[[Any], Any]) -> None:
        if channel == BUS:
            self.transport.remove_mycroft(name, handler)
        elif channel == HIVE:
            self.transport.remove_hive_message(name, handler)
        else:
            self.transport.remove_binary(handler)

    def discard(self, channel: str, name: str, handler: Callable[[Any], Any]) -> None:
        self.unbind(channel, name, handler)

    async def emit_event(self, event_type: str, data: dict[str, Any], context: dict[str, Any]) -> Any:
        return await self.transport.emit_event(event_type, data, context)

    async def send_hive_message(self, message: dict[str, Any], *, encrypt: bool = True) -> Any:
        return await self.transport.send_hive_message(message, encrypt=encrypt)

    async def send_hive_frame(
        self, kind: str, event_type: str, data: dict[str, Any], context: dict[str, Any]
    ) -> Any:
        return await self.transport.send_hive_frame(kind, event_type, data, context)

    async def release(self) -> None:
        await self.transport.release_session()


class _Registration:
    """One registration of ours with a synchronous transport."""

    __slots__ = ("discarded", "pending", "target")

    def __init__(self) -> None:
        self.target: Callable[[Any], Any] = _nothing
        self.pending = True
        self.discarded = False


def _nothing(_message: Any) -> None:
    return None


class SyncLink:
    native = False

    def __init__(self, transport: Any) -> None:
        self.transport = transport
        self._probe: asyncio.Future[Any] | None = None
        # The callable registered with the transport for each of ours, so
        # removal hands back the very object that was added.
        self._registered: dict[tuple[str, str, Any], list[_Registration]] = {}
        self._lock = threading.Lock()

    async def connect(self) -> None:
        await in_thread(self.transport.connect)

    async def disconnect(self) -> None:
        await in_thread(self.transport.disconnect)

    async def retire(self) -> None:
        await in_thread(getattr(self.transport, "_retire_connection", self.transport.disconnect))

    async def probe(self, timeout: float | None) -> bool | None:
        """``is_connected()``; ``None`` when it has not answered by *timeout*."""
        loop = asyncio.get_running_loop()
        probe = self._probe
        if probe is None or probe.done() or probe.get_loop() is not loop:
            probe = in_thread(self.transport.is_connected)
            self._probe = probe
        try:
            return bool(await wait_future(probe, timeout))
        except asyncio.TimeoutError:
            return None

    def stopped(self) -> asyncio.Event | None:
        return None

    def last_error(self) -> BaseException | None:
        error = self.transport.last_error()
        return error if isinstance(error, BaseException) else None

    def connection_info(self) -> ThalovantConnectionInfo:
        return self.transport.connection_info()  # type: ignore[no-any-return]

    def healthcheck(self) -> ThalovantHealth:
        return self.transport.healthcheck()  # type: ignore[no-any-return]

    def session_token(self) -> Any:
        probe = getattr(self.transport, "session_token", None)
        return probe() if callable(probe) else None

    def supports_query(self) -> bool:
        return all(
            callable(getattr(self.transport, name, None))
            for name in ("send_hive_message", "on_hive_message", "remove_hive_message")
        )

    def _methods(self, channel: str) -> tuple[Callable[..., Any], Callable[..., Any]]:
        if channel == BUS:
            return self.transport.on_mycroft, self.transport.remove_mycroft
        if channel == HIVE:
            return self.transport.on_hive_message, self.transport.remove_hive_message
        return self.transport.on_binary, self.transport.remove_binary

    def _bridge(self, handler: Callable[[Any], Any], entry: _Registration) -> Callable[[Any], Any]:
        loop = asyncio.get_running_loop()

        def bridge(message: Any) -> None:
            if entry.discarded:
                return  # retired while its registration was still in flight
            if on_loop_thread(loop):
                handler(message)
                return
            finished = threading.Event()

            def run() -> None:
                try:
                    if not entry.discarded:
                        handler(message)
                except Exception:
                    log.exception("A subscriber raised; continuing.")
                finally:
                    finished.set()

            try:
                loop.call_soon_threadsafe(run)
            except RuntimeError:
                return  # the loop is gone; nobody is listening any more
            # Wait until the loop has run it: a transport that delivers inline
            # expects its handlers to have finished when the call returns.
            while not finished.wait(0.1):
                if loop.is_closed() or not loop.is_running():
                    return

        return bridge

    def _entry(self, channel: str, name: str, handler: Callable[[Any], Any]) -> _Registration:
        entry = _Registration()
        entry.target = handler.fn if type(handler) is OffLoop else self._bridge(handler, entry)
        with self._lock:
            self._registered.setdefault((channel, name, handler), []).append(entry)
        return entry

    def _forget(self, channel: str, name: str, handler: Any, entry: _Registration) -> None:
        with self._lock:
            entries = self._registered.get((channel, name, handler), [])
            if entry in entries:
                entries.remove(entry)
            if not entries:
                self._registered.pop((channel, name, handler), None)

    def _call(self, method: Callable[..., Any], channel: str, name: str, target: Any) -> None:
        if channel == BINARY:
            method(target)
        else:
            method(name, target)

    async def bind(self, channel: str, name: str, handler: Callable[[Any], Any]) -> None:
        add, remove = self._methods(channel)
        entry = self._entry(channel, name, handler)

        def register() -> None:
            self._call(add, channel, name, entry.target)
            if entry.discarded:
                # Retired while the registration was in flight: take it back
                # off on the same thread, the moment there is something to
                # take off.
                with contextlib.suppress(Exception):
                    self._call(remove, channel, name, entry.target)

        try:
            await in_thread(register)
        except BaseException:
            self._forget(channel, name, handler, entry)
            raise
        entry.pending = False
        if entry.discarded:
            self._forget(channel, name, handler, entry)

    def bind_now(self, channel: str, name: str, handler: Callable[[Any], Any]) -> None:
        """Register on the calling thread, for a caller that cannot wait."""
        add, _ = self._methods(channel)
        entry = self._entry(channel, name, handler)
        try:
            self._call(add, channel, name, entry.target)
        except BaseException:
            self._forget(channel, name, handler, entry)
            raise
        entry.pending = False

    def _take(self, channel: str, name: str, handler: Any) -> _Registration | None:
        with self._lock:
            entries = self._registered.get((channel, name, handler))
            live = [entry for entry in entries or () if not entry.discarded]
            if not live:
                return None
            entry = live[-1]
            if not entry.pending:
                entries.remove(entry)  # type: ignore[union-attr]
                if not entries:
                    self._registered.pop((channel, name, handler), None)
            entry.discarded = True
            return entry

    def unbind(self, channel: str, name: str, handler: Callable[[Any], Any]) -> None:
        """Remove a registration; the transport's own error when it has none."""
        _, remove = self._methods(channel)
        entry = self._take(channel, name, handler)
        if entry is None:
            self._call(remove, channel, name, handler.fn if type(handler) is OffLoop else handler)
        elif not entry.pending:
            self._call(remove, channel, name, entry.target)

    def discard(self, channel: str, name: str, handler: Callable[[Any], Any]) -> None:
        """Remove a registration if there is one, including one still in flight."""
        _, remove = self._methods(channel)
        entry = self._take(channel, name, handler)
        if entry is not None and not entry.pending:
            with contextlib.suppress(Exception):
                self._call(remove, channel, name, entry.target)

    async def emit_event(self, event_type: str, data: dict[str, Any], context: dict[str, Any]) -> Any:
        return await in_thread(self.transport.emit_event, event_type, data, context)

    async def send_hive_message(self, message: dict[str, Any], *, encrypt: bool = True) -> Any:
        send = self.transport.send_hive_message
        return await in_thread(lambda: send(message, encrypt=encrypt))

    async def send_hive_frame(
        self, kind: str, event_type: str, data: dict[str, Any], context: dict[str, Any]
    ) -> Any:
        return await in_thread(self.transport.send_hive_frame, kind, event_type, data, context)

    async def release(self) -> None:
        return None


def link_for(transport: Any) -> NativeLink | SyncLink:
    """The link for *transport*, unwrapping the SDK's own sync facades."""
    native = getattr(transport, "_async_transport", None)
    if isinstance(native, AsyncHiveMindTransport):
        return NativeLink(native)
    if isinstance(transport, AsyncHiveMindTransport):
        return NativeLink(transport)
    return SyncLink(transport)
