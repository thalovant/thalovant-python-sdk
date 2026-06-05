"""Subscription handles used by clients, conversations, and agents."""

from __future__ import annotations

from typing import Any, Callable


class ThalovantSubscription:
    """Handle returned by event subscription methods."""

    def __init__(
        self,
        client: Any,
        event_name: str,
        handler: Callable[[Any], None],
    ) -> None:
        self._client = client
        self.event_name = event_name
        self._handler = handler
        self._closed = False

    def __enter__(self) -> "ThalovantSubscription":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._client._remove_subscription(self.event_name, self._handler)
        self._closed = True

    unsubscribe = close
