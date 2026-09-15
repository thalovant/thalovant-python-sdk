"""What to call a hub on a screen somebody is reading.

Every control-plane read in this SDK returns raw JSON, so each caller picks its
own fields -- and on 2026-09-15 a phone offered somebody a list of rooms called
"ops-copilot", "daily-desk", "news-stream". Those are slugs. The app was not
careless: it read ``name`` and preferred it over ``slug``, and on that
deployment ``name`` *holds* the slug. The name a person was shown when the hub
was made lives in ``spec.catalog.title``.

One place to get that wrong is better than one per app.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["hub_display_name"]


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def hub_display_name(hub: Mapping[str, Any]) -> str:
    """The readable name of a hub, never a slug when anything better exists."""

    spec = hub.get("spec")
    if isinstance(spec, Mapping):
        catalog = spec.get("catalog")
        if isinstance(catalog, Mapping):
            title = _text(catalog.get("title"))
            if title:
                return title

    name = _text(hub.get("name"))
    slug = _text(hub.get("slug"))
    # A name that is exactly the slug is the slug.
    if name and name != slug:
        return name

    identifier = name or slug
    if not identifier:
        return "A Thalovant hub"
    words = [word for word in identifier.replace("_", "-").split("-") if word]
    return " ".join(word[:1].upper() + word[1:] for word in words) or "A Thalovant hub"
