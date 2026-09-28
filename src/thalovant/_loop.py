"""The threads that let synchronous code sit on the asyncio core.

- :class:`LoopThread` runs one private event loop for one sync object, so a
  ``ThalovantClient`` works the same whether or not its caller has a loop.
- :class:`CallbackThread` runs a sync client's handlers, in arrival order,
  off the loop. Handlers used to run on the transport's receive thread, and
  callers rely on being able to call the client from inside one --
  thalovant-voice emits from a handler while its main thread waits in
  ``ask()``. On the loop that call would wait on itself.
- :class:`OffLoop` marks a sync handler that must not run on the loop.
- :func:`in_thread` runs a blocking call on a daemon thread of its own, for
  transports written synchronously. A daemon thread rather than the loop's
  executor: a transport that never returns must not hold up interpreter exit
  or ``asyncio.run``'s shutdown.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import os
import queue
import threading
import weakref
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, TypeVar

__all__ = ["CallbackThread", "LoopThread", "OffLoop", "in_thread", "on_loop_thread"]

log = logging.getLogger("thalovant.transport")

T = TypeVar("T")


def on_loop_thread(loop: asyncio.AbstractEventLoop | None) -> bool:
    """Whether the calling thread is the one running *loop*."""
    if loop is None:
        return False
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


class LoopThread:
    """One private event loop on one daemon thread, started on first use."""

    def __init__(self, name: str = "thalovant-loop") -> None:
        self._name = name
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pid = 0

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and self._pid == os.getpid()

    def loop(self) -> asyncio.AbstractEventLoop:
        """The loop, started if it is not running (again, after a fork)."""
        with self._lock:
            if self._loop is not None and self.running:
                return self._loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def run() -> None:
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set)
                try:
                    loop.run_forever()
                finally:
                    try:
                        _cancel_all(loop)
                        loop.run_until_complete(loop.shutdown_asyncgens())
                    finally:
                        loop.close()

            thread = threading.Thread(target=run, name=self._name, daemon=True)
            thread.start()
            ready.wait()
            self._loop, self._thread, self._pid = loop, thread, os.getpid()
            return loop

    def on_thread(self) -> bool:
        return self._thread is not None and threading.current_thread() is self._thread

    def submit(self, coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        return asyncio.run_coroutine_threadsafe(coro, self.loop())

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run *coro* on the loop and wait for it. Never call from the loop."""
        if self.on_thread():
            coro.close()
            raise RuntimeError(
                "A synchronous Thalovant call was made from the SDK's own event loop; "
                "use the async client there."
            )
        future = self.submit(coro)
        try:
            return future.result()
        except BaseException:
            # KeyboardInterrupt or similar in the waiting thread: stop the work
            # it was waiting for rather than leave it running unowned.
            if not future.done():
                future.cancel()
            raise

    def call(self, fn: Callable[..., T], *args: Any) -> T:
        """Run a plain function on the loop thread and wait for its result."""
        if self.on_thread():
            return fn(*args)

        async def invoke() -> T:
            return fn(*args)

        return self.run(invoke())

    def stop(self, final: Callable[[], Awaitable[Any]] | None = None, *, timeout: float = 5.0) -> None:
        """Run *final* on the loop, then stop it, cancelling what is left. Safe to repeat.

        Waits for that from an ordinary thread, never from one that runs an
        event loop or the SDK's handlers. A garbage collection can run this --
        through the finalizer of a client nobody holds any more -- on any
        thread that happens to allocate, and a loop thread that waited here
        for another loop stood still meanwhile, every connection on it with it.
        """
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop, self._thread = None, None
        if loop is None or thread is None or not thread.is_alive():
            return

        async def shutdown() -> None:
            try:
                if final is not None:
                    await asyncio.wait_for(final(), timeout)
            except BaseException:  # noqa: BLE001 - stopping is best effort
                pass
            finally:
                loop.stop()

        try:
            future = asyncio.run_coroutine_threadsafe(shutdown(), loop)
        except RuntimeError:
            return
        if threading.current_thread() is not thread and not _must_not_block():
            with contextlib.suppress(Exception):
                future.result(timeout + 1)
            thread.join(timeout)


def _must_not_block() -> bool:
    """Whether the calling thread runs an event loop or the SDK's handlers."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return threading.current_thread().name.startswith("thalovant-")
    return True


def _cancel_all(loop: asyncio.AbstractEventLoop) -> None:
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))


def stop_on_collect(owner: object, thread: LoopThread) -> None:
    """Stop *thread* when *owner* is garbage collected."""
    weakref.finalize(owner, thread.stop)


class CallbackThread:
    """Runs submitted handlers one at a time, in order, on one daemon thread."""

    _STOP = object()

    def __init__(self, name: str = "thalovant-handlers") -> None:
        self._name = name
        self._lock = threading.Lock()
        self._queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None

    def submit(self, fn: Callable[[Any], Any], value: Any) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._queue = queue.SimpleQueue()
                self._thread = threading.Thread(
                    target=self._run, args=(self._queue,), name=self._name, daemon=True
                )
                self._thread.start()
            self._queue.put((fn, value))

    def _run(self, work: queue.SimpleQueue[Any]) -> None:
        while True:
            item = work.get()
            if item is self._STOP:
                return
            fn, value = item
            try:
                fn(value)
            except Exception:
                log.exception("A subscriber raised; continuing.")

    def stop(self) -> None:
        """Let queued handlers finish, then end the thread."""
        with self._lock:
            thread, self._thread = self._thread, None
            if thread is not None:
                self._queue.put(self._STOP)


class OffLoop:
    """A synchronous handler that is run off the loop.

    A native transport posts it to its :class:`CallbackThread`. A transport
    written synchronously calls it directly on whatever thread delivers the
    frame, as it always did.
    """

    __slots__ = ("fn", "runner")

    def __init__(self, fn: Callable[[Any], Any], runner: CallbackThread) -> None:
        self.fn = fn
        self.runner = runner

    def post(self, value: Any) -> None:
        self.runner.submit(self.fn, value)

    def __call__(self, value: Any) -> Any:
        return self.fn(value)


def in_thread(fn: Callable[..., T], *args: Any) -> asyncio.Future[T]:
    """Run a blocking call on its own daemon thread; await the result.

    Cancelling the awaiting task abandons the result but not the call: a
    thread cannot be interrupted, and whoever owns the call must decide what
    to do once it returns.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()

    def settle(outcome: Callable[[], None]) -> None:
        try:
            loop.call_soon_threadsafe(outcome)
        except RuntimeError:
            pass  # the loop is gone, and so is anyone who could care

    def run() -> None:
        try:
            result = fn(*args)
        except BaseException as error:  # noqa: BLE001 - handed to the awaiting task
            captured = error

            def fail() -> None:
                if not future.done():
                    future.set_exception(captured)

            settle(fail)
        else:

            def succeed() -> None:
                if not future.done():
                    future.set_result(result)

            settle(succeed)

    threading.Thread(target=run, name="thalovant-transport-call", daemon=True).start()
    return future


async def wait_future(future: Awaitable[T], timeout: float | None) -> T:
    """``asyncio.wait_for`` without cancelling *future* when the wait ends."""
    return await asyncio.wait_for(asyncio.shield(future), timeout)
