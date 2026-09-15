"""What a phone calls a hub. It called them slugs until 2026-09-15."""

from __future__ import annotations

from thalovant.hubs import hub_display_name


def test_a_hub_is_called_what_a_person_was_shown_not_what_the_row_is_keyed_by() -> None:
    # Exactly what a phone was offered: name IS the slug, and the readable
    # title sits in the catalog entry.
    assert hub_display_name(
        {"name": "ops-copilot", "slug": "ops-copilot", "spec": {"catalog": {"title": "Ops Copilot"}}}
    ) == "Ops Copilot"


def test_a_real_name_wins_when_there_is_no_catalog_entry() -> None:
    assert hub_display_name({"name": "The Kitchen", "slug": "kitchen"}) == "The Kitchen"


def test_a_hub_with_nothing_but_a_slug_is_made_readable_rather_than_shown_raw() -> None:
    assert hub_display_name({"slug": "daily-desk"}) == "Daily Desk"
    assert hub_display_name({"name": "news-stream", "slug": "news-stream"}) == "News Stream"
    assert hub_display_name({"slug": "local_pulse"}) == "Local Pulse"


def test_a_hub_described_with_nothing_at_all_still_says_something() -> None:
    assert hub_display_name({"id": "1"}) == "A Thalovant hub"
    assert hub_display_name({"name": "", "slug": "   "}) == "A Thalovant hub"
    assert hub_display_name({"spec": {"catalog": {"title": "  "}}, "slug": "x"}) == "X"


def test_a_spec_that_is_not_shaped_like_a_catalog_does_not_throw() -> None:
    assert hub_display_name({"spec": "nonsense", "slug": "kitchen"}) == "Kitchen"
    assert hub_display_name({"spec": {"catalog": []}, "name": "Kitchen"}) == "Kitchen"
