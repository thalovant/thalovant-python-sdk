"""Scoped conversation helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterator, Iterator

from .events import (
    EventHandler,
    EventPredicate,
    _context_with_correlation,
    _merge_context,
    _new_session_id,
)
from .models import ThalovantReply
from .subscriptions import ThalovantSubscription

if TYPE_CHECKING:
    from .client import AsyncThalovantClient, ThalovantClient
    from .events import ThalovantEvent


class ThalovantConversation:
    """Scoped conversation helper with a stable session context."""

    def __init__(
        self,
        client: "ThalovantClient",
        *,
        session_id: str | None = None,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
    ) -> None:
        self.client = client
        self.session_id = session_id or _new_session_id()
        self.lang = lang
        self.context = dict(context or {})

    def __enter__(self) -> "ThalovantConversation":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def ask(
        self,
        text: str,
        *,
        timeout: float = 12.0,
        lang: str | None = None,
        context: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> ThalovantReply:
        return self.client.ask(
            text,
            timeout=timeout,
            lang=lang or self.lang,
            context=self._merged_context(context),
            session_id=self.session_id,
            request_id=request_id,
        )

    def send_utterance(
        self,
        text: str,
        *,
        lang: str | None = None,
        context: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any:
        return self.client.send_utterance(
            text,
            lang=lang or self.lang,
            context=self._merged_context(context),
            session_id=self.session_id,
            request_id=request_id,
        )

    def send_action(
        self,
        payload: str,
        *,
        title: str | None = None,
        lang: str | None = None,
        context: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any:
        return self.client.send_action(
            payload,
            title=title,
            lang=lang or self.lang,
            context=self._merged_context(context),
            session_id=self.session_id,
            request_id=request_id,
        )

    def send_code(
        self,
        value: str,
        *,
        kind: str = "code",
        label: str | None = None,
        lang: str | None = None,
        context: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any:
        return self.client.send_code(
            value,
            kind=kind,
            label=label,
            lang=lang or self.lang,
            context=self._merged_context(context),
            session_id=self.session_id,
            request_id=request_id,
        )

    def emit(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Any:
        scoped_context = _context_with_correlation(
            self._merged_context(context),
            session_id=self.session_id,
            site_id=self.client.identity.site_id,
            lang=self.lang,
        )
        return self.client.emit(event_type, data, scoped_context)

    def on(
        self,
        event_name: str,
        handler: EventHandler,
        *,
        predicate: EventPredicate | None = None,
        context: dict[str, Any] | None = None,
    ) -> ThalovantSubscription:
        return self.client.on(
            event_name,
            handler,
            session_id=self.session_id,
            context=self._merged_context(context),
            predicate=predicate,
        )

    def wait_for_event(
        self,
        event_name: str,
        *,
        timeout: float = 12.0,
        predicate: EventPredicate | None = None,
        context: dict[str, Any] | None = None,
    ) -> "ThalovantEvent":
        return self.client.wait_for_event(
            event_name,
            timeout=timeout,
            predicate=predicate,
            session_id=self.session_id,
            context=self._merged_context(context),
        )

    def listen(
        self,
        event_name: str,
        *,
        timeout: float | None = None,
        max_events: int | None = None,
        predicate: EventPredicate | None = None,
        context: dict[str, Any] | None = None,
    ) -> Iterator["ThalovantEvent"]:
        return self.client.listen(
            event_name,
            timeout=timeout,
            max_events=max_events,
            predicate=predicate,
            session_id=self.session_id,
            context=self._merged_context(context),
        )

    def _merged_context(self, extra: dict[str, Any] | None) -> dict[str, Any]:
        return _merge_context(self.context, extra)


class AsyncThalovantConversation:
    """Async scoped conversation helper with a stable session context."""

    def __init__(
        self,
        client: "AsyncThalovantClient",
        *,
        session_id: str | None = None,
        lang: str = "en-us",
        context: dict[str, Any] | None = None,
    ) -> None:
        self.client = client
        self.session_id = session_id or _new_session_id()
        self.lang = lang
        self.context = dict(context or {})

    async def __aenter__(self) -> "AsyncThalovantConversation":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def ask(
        self,
        text: str,
        *,
        timeout: float = 12.0,
        lang: str | None = None,
        context: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> ThalovantReply:
        return await self.client.ask(
            text,
            timeout=timeout,
            lang=lang or self.lang,
            context=self._merged_context(context),
            session_id=self.session_id,
            request_id=request_id,
        )

    async def send_utterance(
        self,
        text: str,
        *,
        lang: str | None = None,
        context: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any:
        return await self.client.send_utterance(
            text,
            lang=lang or self.lang,
            context=self._merged_context(context),
            session_id=self.session_id,
            request_id=request_id,
        )

    async def send_action(
        self,
        payload: str,
        *,
        title: str | None = None,
        lang: str | None = None,
        context: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any:
        return await self.client.send_action(
            payload,
            title=title,
            lang=lang or self.lang,
            context=self._merged_context(context),
            session_id=self.session_id,
            request_id=request_id,
        )

    async def send_code(
        self,
        value: str,
        *,
        kind: str = "code",
        label: str | None = None,
        lang: str | None = None,
        context: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Any:
        return await self.client.send_code(
            value,
            kind=kind,
            label=label,
            lang=lang or self.lang,
            context=self._merged_context(context),
            session_id=self.session_id,
            request_id=request_id,
        )

    async def emit(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Any:
        scoped_context = _context_with_correlation(
            self._merged_context(context),
            session_id=self.session_id,
            site_id=self.client.identity.site_id,
            lang=self.lang,
        )
        return await self.client.emit(event_type, data, scoped_context)

    def on(
        self,
        event_name: str,
        handler: EventHandler,
        *,
        predicate: EventPredicate | None = None,
        context: dict[str, Any] | None = None,
    ) -> ThalovantSubscription:
        return self.client.on(
            event_name,
            handler,
            session_id=self.session_id,
            context=self._merged_context(context),
            predicate=predicate,
        )

    async def wait_for_event(
        self,
        event_name: str,
        *,
        timeout: float = 12.0,
        predicate: EventPredicate | None = None,
        context: dict[str, Any] | None = None,
    ) -> "ThalovantEvent":
        return await self.client.wait_for_event(
            event_name,
            timeout=timeout,
            predicate=predicate,
            session_id=self.session_id,
            context=self._merged_context(context),
        )

    async def listen(
        self,
        event_name: str,
        *,
        timeout: float | None = None,
        max_events: int | None = None,
        predicate: EventPredicate | None = None,
        context: dict[str, Any] | None = None,
    ) -> AsyncIterator["ThalovantEvent"]:
        async for event in self.client.listen(
            event_name,
            timeout=timeout,
            max_events=max_events,
            predicate=predicate,
            session_id=self.session_id,
            context=self._merged_context(context),
        ):
            yield event

    def _merged_context(self, extra: dict[str, Any] | None) -> dict[str, Any]:
        return _merge_context(self.context, extra)
