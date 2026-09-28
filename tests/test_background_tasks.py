"""Work nobody awaits: kept alive until it ends, and its failure logged.

The event loop holds only a weak reference to a task, so a coroutine a
handler returned could be collected while suspended; and one that raised
surfaced later as "Task exception was never retrieved", outside the SDK's
own logging.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import time

import pytest

from thalovant import _loop
from thalovant.home import HomeAnswer, answer_home_request
from thalovant.events import ThalovantEvent


def test_a_spawned_task_is_held_until_it_ends() -> None:
    async def exercise() -> None:
        release = asyncio.Event()
        finished: list[bool] = []

        async def work() -> None:
            await release.wait()
            finished.append(True)

        task = _loop.spawn(work(), "work failed")
        del task
        gc.collect()
        assert len(_loop._SPAWNED) == 1  # nobody else holds it, and it is still there
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
        assert finished == [True]
        assert not _loop._SPAWNED

    asyncio.run(exercise())


def test_a_spawned_failure_is_logged_with_its_traceback(caplog: pytest.LogCaptureFixture) -> None:
    async def fails() -> None:
        raise RuntimeError("boom")

    async def exercise() -> None:
        _loop.spawn(fails(), "A subscriber raised; continuing.")
        for _ in range(5):
            await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR, logger="thalovant.transport"):
        asyncio.run(exercise())
    [entry] = [r for r in caplog.records if r.getMessage() == "A subscriber raised; continuing."]
    assert entry.exc_info is not None and "boom" in str(entry.exc_info[1])


def test_quiet_work_fails_at_debug_only(caplog: pytest.LogCaptureFixture) -> None:
    async def fails() -> None:
        raise OSError("socket already gone")

    async def exercise() -> None:
        _loop.spawn(fails(), None)
        for _ in range(5):
            await asyncio.sleep(0)

    with caplog.at_level(logging.DEBUG, logger="thalovant.transport"):
        asyncio.run(exercise())
    levels = {r.levelno for r in caplog.records if "socket already gone" in r.getMessage()}
    assert levels == {logging.DEBUG}


def test_a_coroutine_handler_that_raises_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    from thalovant.client import _run_handler

    async def handler(_value: object) -> None:
        raise ValueError("handler failed")

    async def exercise() -> None:
        _run_handler(handler, object())
        for _ in range(5):
            await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR, logger="thalovant.client"):
        asyncio.run(exercise())
    assert any(r.getMessage() == "An event handler raised; continuing." for r in caplog.records)


def test_a_state_callback_coroutine_that_raises_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    from thalovant import AsyncHubSession

    async def callback(_up: bool) -> None:
        raise ValueError("callback failed")

    async def exercise() -> None:
        async def connect() -> object:
            raise AssertionError("never dialled")

        session = AsyncHubSession(connect)
        session.on_state_change(callback)
        session._set_state(True)
        for _ in range(5):
            await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR, logger="thalovant.session"):
        asyncio.run(exercise())
    assert any(r.getMessage() == "a state callback raised" for r in caplog.records)


# -- home handlers ----------------------------------------------------------------------


class _Replies:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def reply(self, _event: object, _msg_type: str, data: dict[str, object]) -> None:
        self.sent.append(data)


def _request(request_id: str) -> ThalovantEvent:
    return ThalovantEvent(name="thalovant.home.request", data={"request_id": request_id, "utterance": "x"},
                          context={"source": "skill"}, raw=None)


def test_a_blocking_handler_is_bounded_and_the_loop_keeps_running() -> None:
    """A plain function runs off the loop, so its time bounds it and nothing else waits."""

    def blocking(_request: object) -> HomeAnswer:
        time.sleep(1.0)  # a synchronous call into a slow home controller
        return HomeAnswer(speech="Too late.")

    async def exercise() -> tuple[object, float, int]:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        running = asyncio.ensure_future(ticker())
        started = time.monotonic()
        sent = await answer_home_request(_Replies(), _request("b1"), blocking, timeout=0.1, hub_timeout=2.0)
        took = time.monotonic() - started
        running.cancel()
        return sent, took, ticks

    sent, took, ticks = asyncio.run(exercise())
    assert isinstance(sent, dict) and sent["error_code"] == "timeout"
    assert took < 0.6
    assert ticks >= 3  # the loop was never blocked


def test_a_plain_handler_runs_off_the_loop_thread() -> None:
    import threading

    seen: list[int] = []

    def handler(_request: object) -> str:
        seen.append(threading.get_ident())
        return "Done."

    async def exercise() -> tuple[object, int]:
        sent = await answer_home_request(_Replies(), _request("t1"), handler)
        return sent, threading.get_ident()

    sent, loop_thread = asyncio.run(exercise())
    assert isinstance(sent, dict) and sent["speech"] == "Done."
    assert seen and seen[0] != loop_thread


def test_an_awaitable_from_a_plain_handler_is_awaited_within_the_handler_time() -> None:
    async def slow() -> HomeAnswer:
        await asyncio.sleep(1.0)
        return HomeAnswer(speech="Too late.")

    async def quick() -> HomeAnswer:
        return HomeAnswer(speech="Done.")

    async def exercise() -> tuple[object, object]:
        late = await answer_home_request(_Replies(), _request("a1"), lambda _r: slow(), timeout=0.1, hub_timeout=2.0)
        fine = await answer_home_request(_Replies(), _request("a2"), lambda _r: quick(), timeout=1.0, hub_timeout=2.0)
        return late, fine

    late, fine = asyncio.run(exercise())
    assert isinstance(late, dict) and late["error_code"] == "timeout"
    assert isinstance(fine, dict) and fine["speech"] == "Done."


def test_a_plain_handler_that_raises_is_failed_to_handle() -> None:
    def broken(_request: object) -> str:
        raise RuntimeError("gone")

    sent = asyncio.run(answer_home_request(_Replies(), _request("r1"), broken))
    assert isinstance(sent, dict) and sent["error_code"] == "failed_to_handle"
