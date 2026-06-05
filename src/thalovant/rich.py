"""Structured display helpers for rich assistant responses."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable, Mapping


_SSML_RE = re.compile(r"<{1}/?[^>]*>{1}")


@dataclass(frozen=True)
class ThalovantDisplayItem:
    """UI-friendly representation of text, media, tables, and choices."""

    kind: str
    text: str | None = None
    data: Any = None
    title: str | None = None
    payload: str | None = None
    url: str | None = None
    silent: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "data": self.data,
            "title": self.title,
            "payload": self.payload,
            "url": self.url,
            "silent": self.silent,
        }


def strip_ssml(text: str) -> str:
    """Remove simple SSML/XML tags from display text."""

    return _SSML_RE.sub("", text)


def rich_media_from_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize common rich media payload shapes into a mapping."""

    raw = data.get("rich_media_data") or data.get("rich_media") or data.get("display")
    media = _coerce_mapping(raw)
    if media:
        return media

    direct: dict[str, Any] = {}
    for key in ("table", "attachment", "attachments", "quick_replies", "buttons", "image", "images"):
        if key in data:
            direct[key] = data[key]
    return direct


def display_items_from_event_data(
    data: Mapping[str, Any],
    *,
    event_name: str | None = None,
    max_text_chars: int | None = None,
) -> tuple[ThalovantDisplayItem, ...]:
    """Extract display items from common OVOS/HiveMind assistant payloads."""

    items: list[ThalovantDisplayItem] = []
    text = _text_from_data(data)
    silent = bool(data.get("silent")) or event_name == "write"
    if text:
        for chunk in _chunks(strip_ssml(text), max_text_chars):
            items.append(ThalovantDisplayItem(kind="text", text=chunk, silent=silent))

    media = rich_media_from_data(data)
    table = _coerce_json(media.get("table"))
    if table is not None:
        items.append(ThalovantDisplayItem(kind="table", data=table))

    for attachment in _attachments(media):
        kind = str(attachment.get("type") or "attachment")
        payload = _coerce_mapping(attachment.get("payload"))
        url = (
            payload.get("src")
            or payload.get("url")
            or attachment.get("src")
            or attachment.get("url")
        )
        items.append(
            ThalovantDisplayItem(
                kind="image" if kind == "image" else "attachment",
                data=attachment,
                title=_string(attachment.get("title")),
                url=_string(url),
            )
        )

    quick_replies = media.get("quick_replies") or media.get("buttons")
    choices = [_choice(choice) for choice in _as_iterable(quick_replies)]
    choices = [choice for choice in choices if choice]
    if choices:
        items.append(ThalovantDisplayItem(kind="choices", data=choices))

    for image in _as_iterable(media.get("image") or media.get("images")):
        url = _string(image.get("src") or image.get("url")) if isinstance(image, Mapping) else _string(image)
        if url:
            items.append(ThalovantDisplayItem(kind="image", url=url, data=image))

    return tuple(items)


def _text_from_data(data: Mapping[str, Any]) -> str:
    raw = data.get("utterance") or data.get("text")
    if isinstance(raw, str):
        return raw
    utterances = data.get("utterances")
    if isinstance(utterances, str):
        return utterances
    if isinstance(utterances, (list, tuple)):
        return " ".join(item for item in utterances if isinstance(item, str))
    return ""


def _coerce_mapping(raw: Any) -> dict[str, Any]:
    value = _coerce_json(raw)
    return dict(value) if isinstance(value, Mapping) else {}


def _coerce_json(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _attachments(media: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    raw = media.get("attachments") or media.get("attachment")
    if isinstance(raw, Mapping):
        return (raw,)
    return tuple(item for item in _as_iterable(raw) if isinstance(item, Mapping))


def _choice(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, Mapping):
        title = raw.get("title") or raw.get("label") or raw.get("text")
        payload = raw.get("payload") or raw.get("value") or title
        return {
            "title": _string(title) or "",
            "payload": _string(payload) or "",
            "data": dict(raw),
        }
    if isinstance(raw, str):
        return {"title": raw, "payload": raw, "data": raw}
    return None


def _as_iterable(raw: Any) -> tuple[Any, ...]:
    value = _coerce_json(raw)
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def _string(raw: Any) -> str | None:
    return str(raw) if raw is not None else None


def _chunks(text: str, max_chars: int | None) -> tuple[str, ...]:
    if not max_chars or len(text) <= max_chars:
        return (text,)
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        index = remaining.rfind(" ", 0, max_chars + 1)
        if index <= 0:
            index = max_chars
        chunks.append(remaining[:index].strip())
        remaining = remaining[index:].strip()
    if remaining:
        chunks.append(remaining)
    return tuple(chunk for chunk in chunks if chunk)
