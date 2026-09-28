"""Stopping one client's loop never stalls the thread that asked.

A client nobody holds any more is stopped by its finalizer, and a garbage
collection runs finalizers on whichever thread happens to allocate -- another
client's event loop thread included, or an application's. That thread must
not wait there for the other loop to wind down.
"""

from __future__ import annotations

import asyncio
import threading
import time

from thalovant._loop import LoopThread


def _slow_runner() -> tuple[LoopThread, threading.Thread]:
    runner = LoopThread("thalovant-test-loop")
    runner.loop()
    thread = runner._thread
    assert thread is not None
    return runner, thread


async def _slow_final() -> None:
    await asyncio.sleep(0.5)


def test_a_loop_thread_does_not_wait_for_another_loop_to_stop() -> None:
    runner, thread = _slow_runner()

    async def from_a_loop() -> float:
        started = time.monotonic()
        runner.stop(_slow_final, timeout=5)
        return time.monotonic() - started

    assert asyncio.run(from_a_loop()) < 0.2
    thread.join(5)  # it still stops, on its own time
    assert not thread.is_alive()


def test_an_ordinary_thread_still_waits_for_the_loop_to_stop() -> None:
    runner, thread = _slow_runner()
    started = time.monotonic()
    runner.stop(_slow_final, timeout=5)
    assert time.monotonic() - started >= 0.4
    assert not thread.is_alive()
