"""Long-running agent runners."""

from __future__ import annotations

import asyncio
from pathlib import Path
import threading
from typing import Any, Callable, Union, overload

from .events import EVENT_SPEAK, EventHandler
from .identity import ThalovantIdentity
from .subscriptions import ThalovantSubscription

#: What registering a handler gives back: its subscription once the agent
#: runs, the handler itself before.
Registered = Union[EventHandler, ThalovantSubscription]


class ThalovantAgent:
    """Small runner for long-lived synchronous agents."""

    def __init__(self, identity: ThalovantIdentity, **client_kwargs: Any) -> None:
        from .client import ThalovantClient

        self.client = ThalovantClient(identity, **client_kwargs)
        self._registrations: list[tuple[str, EventHandler]] = []
        self._subscriptions: list[ThalovantSubscription] = []
        self._stop_event = threading.Event()
        self._running = False

    @classmethod
    def from_identity_file(cls, path: str | Path, **kwargs: Any) -> "ThalovantAgent":
        return cls(ThalovantIdentity.from_file(path), **kwargs)

    @classmethod
    def from_config(
        cls,
        path: str | Path | None = None,
        *,
        profile: str | None = None,
        **kwargs: Any,
    ) -> "ThalovantAgent":
        return cls(ThalovantIdentity.from_config(path, profile=profile), **kwargs)

    @classmethod
    def from_env(cls, **kwargs: Any) -> "ThalovantAgent":
        return cls(ThalovantIdentity.from_env(), **kwargs)

    def __enter__(self) -> "ThalovantAgent":
        self.client.connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @overload
    def on(self, event_name: str, handler: None = None) -> Callable[[EventHandler], Registered]: ...

    @overload
    def on(self, event_name: str, handler: EventHandler) -> Registered: ...

    def on(
        self,
        event_name: str,
        handler: EventHandler | None = None,
    ) -> Registered | Callable[[EventHandler], Registered]:
        """Register a handler now, or use as a decorator before `run_forever`.

        Returns the subscription (or the handler, before the agent runs), or
        with no handler the decorator that registers one.
        """

        if handler is None:
            def decorator(callback: EventHandler) -> Registered:
                return self._register(event_name, callback)

            return decorator

        return self._register(event_name, handler)

    def _register(self, event_name: str, handler: EventHandler) -> Registered:
        if self._running:
            subscription = self.client.on(event_name, handler)
            self._subscriptions.append(subscription)
            return subscription
        self._registrations.append((event_name, handler))
        return handler

    def on_speak(
        self, handler: EventHandler | None = None
    ) -> Registered | Callable[[EventHandler], Registered]:
        if handler is None:
            return self.on(EVENT_SPEAK)
        return self.on(EVENT_SPEAK, handler)

    def subscribe(
        self,
        event_name: str,
        handler: EventHandler,
    ) -> ThalovantSubscription:
        """Subscribe immediately when the agent is already connected."""

        subscription = self.client.on(event_name, handler)
        self._subscriptions.append(subscription)
        return subscription

    def conversation(self, **kwargs: Any) -> Any:
        return self.client.conversation(**kwargs)

    def ask(self, text: str, **kwargs: Any) -> Any:
        return self.client.ask(text, **kwargs)

    def send_utterance(self, text: str, **kwargs: Any) -> Any:
        return self.client.send_utterance(text, **kwargs)

    def run_forever(self, *, poll_interval: float = 0.25) -> None:
        self._stop_event.clear()
        self._running = True
        self.client.connect()
        for event_name, handler in self._registrations:
            self._subscriptions.append(self.client.on(event_name, handler))
        try:
            while not self._stop_event.wait(poll_interval):
                self.client.healthcheck()
        except KeyboardInterrupt:
            self.stop()
        finally:
            self._running = False
            self.close()

    def stop(self) -> None:
        self._stop_event.set()

    def close(self) -> None:
        while self._subscriptions:
            self._subscriptions.pop().close()
        self.client.close()


class AsyncThalovantAgent:
    """Small runner for long-lived asyncio agents."""

    def __init__(self, identity: ThalovantIdentity, **client_kwargs: Any) -> None:
        from .client import AsyncThalovantClient

        self.client = AsyncThalovantClient(identity, **client_kwargs)
        self._registrations: list[tuple[str, EventHandler]] = []
        self._subscriptions: list[ThalovantSubscription] = []
        self._stop_event: asyncio.Event | None = None
        self._running = False

    @classmethod
    def from_identity_file(cls, path: str | Path, **kwargs: Any) -> "AsyncThalovantAgent":
        return cls(ThalovantIdentity.from_file(path), **kwargs)

    @classmethod
    def from_config(
        cls,
        path: str | Path | None = None,
        *,
        profile: str | None = None,
        **kwargs: Any,
    ) -> "AsyncThalovantAgent":
        return cls(ThalovantIdentity.from_config(path, profile=profile), **kwargs)

    @classmethod
    def from_env(cls, **kwargs: Any) -> "AsyncThalovantAgent":
        return cls(ThalovantIdentity.from_env(), **kwargs)

    async def __aenter__(self) -> "AsyncThalovantAgent":
        await self.client.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    @overload
    def on(self, event_name: str, handler: None = None) -> Callable[[EventHandler], Registered]: ...

    @overload
    def on(self, event_name: str, handler: EventHandler) -> Registered: ...

    def on(
        self,
        event_name: str,
        handler: EventHandler | None = None,
    ) -> Registered | Callable[[EventHandler], Registered]:
        if handler is None:
            def decorator(callback: EventHandler) -> Registered:
                return self._register(event_name, callback)

            return decorator

        return self._register(event_name, handler)

    def _register(self, event_name: str, handler: EventHandler) -> Registered:
        if self._running:
            subscription = self.client.on(event_name, handler)
            self._subscriptions.append(subscription)
            return subscription
        self._registrations.append((event_name, handler))
        return handler

    def on_speak(
        self, handler: EventHandler | None = None
    ) -> Registered | Callable[[EventHandler], Registered]:
        if handler is None:
            return self.on(EVENT_SPEAK)
        return self.on(EVENT_SPEAK, handler)

    def subscribe(
        self,
        event_name: str,
        handler: EventHandler,
    ) -> ThalovantSubscription:
        """Subscribe immediately when the agent is already connected."""

        subscription = self.client.on(event_name, handler)
        self._subscriptions.append(subscription)
        return subscription

    def conversation(self, **kwargs: Any) -> Any:
        return self.client.conversation(**kwargs)

    async def ask(self, text: str, **kwargs: Any) -> Any:
        return await self.client.ask(text, **kwargs)

    async def send_utterance(self, text: str, **kwargs: Any) -> Any:
        return await self.client.send_utterance(text, **kwargs)

    async def run_forever(self, *, poll_interval: float = 0.25) -> None:
        self._stop_event = asyncio.Event()
        self._running = True
        await self.client.connect()
        for event_name, handler in self._registrations:
            self._subscriptions.append(self.client.on(event_name, handler))
        try:
            while not self._stop_event.is_set():
                await self.client.healthcheck()
                await asyncio.sleep(poll_interval)
        finally:
            self._running = False
            await self.close()

    def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    async def close(self) -> None:
        while self._subscriptions:
            self._subscriptions.pop().close()
        await self.client.close()
