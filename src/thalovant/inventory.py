"""A hub's skill inventory as a listing keeps it: skills, their intents, the
sentences each accepts per language, and a cache of the last reading.

Moved from thalovant-voice's `intents` module, where it served one client.
The hub protocol's own view is :class:`thalovant.intents.HubIntentInventory`;
this is the merged, presentable view a listing tool or a harness prints,
with the control plane's titles and locales folded in, and it survives a
process: the cache keeps the last reading for an hour, which keeps a repeated
look instant without hiding this morning's install for long.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ovos_spec_tools.language import closest_lang

from .listing import rank

__all__ = [
    "CACHE_TTL_SECONDS",
    "CACHE_VERSION",
    "HUB_SOURCE",
    "LIVE_SOURCES",
    "Intent",
    "Inventory",
    "InventoryCache",
    "Skill",
    "common_affix",
    "friendly_title",
    "humanize",
    "identity_host",
    "languages_present",
    "sort_key",
    "strip_affix",
]

#: The source a listing carries when the hub itself answered.
HUB_SOURCE = "hub"
#: Sources that mean the hub was actually asked, not a cache or a catalogue.
LIVE_SOURCES = (HUB_SOURCE, "ovos-runtime")
CACHE_VERSION = 1
CACHE_TTL_SECONDS = 3600.0
_SKILL_PREFIXES = ("thalovant-skill-", "ovos-skill-", "skill-")


@dataclass(frozen=True)
class Intent:
    id: str
    name: str
    skill_id: str
    engine: str
    phrases: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def examples(self, language: str | None, limit: int) -> tuple[str, ...]:
        """A few phrases worth printing: whole sentences before ones with slots.

        Adapt intents register keyword prefixes as well as sentences, so a
        skill's phrase list contains things like "weather in" and "uv index
        in" -- correct as patterns, and read as a sentence that got cut off.
        They rank last, so they appear only when a skill has nothing better.
        """
        pool: tuple[str, ...] = ()
        if language:
            match = closest_lang(language, list(self.phrases)) if self.phrases else None
            if match is not None:
                pool = self.phrases[match]
        elif self.phrases:
            pool = next(iter(self.phrases.values()))
        if limit <= 0:
            return pool
        return rank(pool, language)[:limit]


@dataclass(frozen=True)
class Skill:
    id: str
    title: str
    locales: tuple[str, ...]
    intents: tuple[Intent, ...]

    @property
    def declares_locales(self) -> bool:
        return bool(self.locales)

    def speaks(self, language: str) -> bool | None:
        """True, False, or None for "the catalogue does not say"."""
        if not self.locales:
            return None
        return closest_lang(language, list(self.locales)) is not None


@dataclass(frozen=True)
class Inventory:
    hub_id: str
    hub_name: str
    source: str
    generated_at: str
    skills: tuple[Skill, ...]
    notes: tuple[str, ...] = ()

    @property
    def has_phrases(self) -> bool:
        return any(intent.phrases for intent in self.intents)

    @property
    def live(self) -> bool:
        return self.source in LIVE_SOURCES

    @property
    def intents(self) -> tuple[Intent, ...]:
        return tuple(intent for skill in self.skills for intent in skill.intents)

    def as_dict(self) -> dict[str, Any]:
        """The whole inventory, losslessly, for the cache."""
        return {
            "cache_version": CACHE_VERSION,
            "hub_id": self.hub_id,
            "hub_name": self.hub_name,
            "source": self.source,
            "generated_at": self.generated_at,
            "notes": list(self.notes),
            "skills": [
                {
                    "id": skill.id,
                    "title": skill.title,
                    "locales": list(skill.locales),
                    "intents": [
                        {
                            "id": intent.id,
                            "name": intent.name,
                            "skill_id": intent.skill_id,
                            "engine": intent.engine,
                            "phrases": {lang: list(texts) for lang, texts in intent.phrases.items()},
                        }
                        for intent in skill.intents
                    ],
                }
                for skill in self.skills
            ],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Inventory:
        """Rebuild what as_dict wrote, or refuse.

        Refusing is the point: a cache file from an older release has a
        different shape, and reading it optimistically would show somebody a
        listing that silently lost half its fields.
        """
        if not isinstance(raw, dict) or raw.get("cache_version") != CACHE_VERSION:
            raise ValueError("not a current inventory cache")
        skills = []
        for skill in raw.get("skills") or ():
            intents = tuple(
                Intent(
                    id=str(intent.get("id", "")),
                    name=str(intent.get("name", "")),
                    skill_id=str(intent.get("skill_id", "")),
                    engine=str(intent.get("engine", "")),
                    phrases={
                        str(lang): tuple(str(text) for text in sentences)
                        for lang, sentences in (intent.get("phrases") or {}).items()
                    },
                )
                for intent in (skill.get("intents") or ())
            )
            skills.append(
                Skill(
                    id=str(skill.get("id", "")),
                    title=str(skill.get("title", "")),
                    locales=tuple(str(x) for x in (skill.get("locales") or ())),
                    intents=intents,
                )
            )
        return cls(
            hub_id=str(raw.get("hub_id", "")),
            hub_name=str(raw.get("hub_name", "")),
            source=str(raw.get("source", "")),
            generated_at=str(raw.get("generated_at", "")),
            skills=tuple(skills),
            notes=tuple(str(note) for note in (raw.get("notes") or ())),
        )


def identity_host(identity_path: Path | str | None) -> str | None:
    """The hostname an identity dials, read from the one public part of the file."""
    if not identity_path:
        return None
    try:
        raw = json.loads(Path(identity_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    master = raw.get("default_master") or "" if isinstance(raw, dict) else ""
    if not isinstance(master, str) or not master:
        return None
    return urlparse(master).hostname


class InventoryCache:
    """The last listing per hub, kept on disk for an hour.

    A cache is an optimisation, and an optimisation that can break the
    command it speeds up is not one: every failure to read returns None and
    every failure to write is swallowed.
    """

    def __init__(self, directory: Path | str | None = None, *, ttl: float = CACHE_TTL_SECONDS,
                 app: str = "thalovant") -> None:
        if directory is None:
            root = os.environ.get("XDG_CACHE_HOME") or ""
            base = Path(root) if root else Path.home() / ".cache"
            directory = base / app
        self.directory = Path(directory)
        self.ttl = ttl

    @staticmethod
    def key(mode: str, identity: Path | str | None) -> str:
        """Which listing this is, so two never overwrite each other.

        The host is kept readable so the cache directory can be understood at
        a glance; the digest separates two identities that resolve to the
        same host or to none.
        """
        identity_text = str(identity or "")
        host = identity_host(Path(identity_text)) if identity_text else None
        digest = hashlib.sha256(f"{mode}|{identity_text}".encode()).hexdigest()[:8]
        readable = re.sub(r"[^A-Za-z0-9._-]", "-", host or "local")[:40]
        return f"{mode}-{readable}-{digest}"

    def path(self, key: str) -> Path:
        return self.directory / f"intents-{key}.json"

    def load(self, key: str) -> Inventory | None:
        """The last listing for this key, if it is recent enough to still be true."""
        path = self.path(key)
        try:
            if time.time() - path.stat().st_mtime > self.ttl:
                return None
            return Inventory.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def store(self, key: str, inventory: Inventory) -> None:
        """Store a listing for next time. Failing to is not worth an error."""
        path = self.path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Written beside and moved, so an interrupted write never leaves a
            # half-file that the next run has to detect.
            scratch = path.with_suffix(".partial")
            scratch.write_text(json.dumps(inventory.as_dict()), encoding="utf-8")
            # It names the hub and its skills: not a secret, not world-readable.
            scratch.chmod(0o600)
            scratch.replace(path)
        except (OSError, TypeError, ValueError):
            with contextlib.suppress(OSError):
                path.with_suffix(".partial").unlink()


def languages_present(inventory: Inventory) -> tuple[str, ...]:
    """Every language this listing can actually show, from the listing itself."""
    found: set[str] = set()
    for skill in inventory.skills:
        found.update(skill.locales)
        for intent in skill.intents:
            found.update(intent.phrases)
    return tuple(sorted(found))


def friendly_title(skill_id: str) -> str:
    """A name for someone who did not choose the package name.

    An OVOS skill id is `<package>.<author>`; the author is not part of the name.
    """
    name = skill_id
    for prefix in _SKILL_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    name = name.rsplit(".", 1)[0] if "." in name else name
    return name.replace("-", " ").replace("_", " ").strip().title() or skill_id


def _tokens(name: str) -> list[str]:
    return [part for part in re.split(r"[._]+", name) if part]


def common_affix(names: list[str]) -> tuple[str | None, str]:
    """The token every one of these intents shares, at the front or the back.

    Skill authors name intents in families -- every weather intent ends
    `.weather`, every Custos one begins `custos.` -- and printing that token
    seventeen times spends the reader's attention on the one word that carries
    no information. Found rather than configured. Returns
    ("suffix"|"prefix", token) or (None, "").
    """
    if len(names) < 2:
        return None, ""
    split = [_tokens(name) for name in names]
    if not all(len(parts) > 1 for parts in split):
        return None, ""
    trailing = {parts[-1] for parts in split}
    if len(trailing) == 1:
        return "suffix", trailing.pop()
    leading = {parts[0] for parts in split}
    if len(leading) == 1:
        return "prefix", leading.pop()
    return None, ""


def strip_affix(name: str, kind: str | None, token: str) -> str:
    if kind is None:
        return name
    parts = _tokens(name)
    if kind == "suffix" and parts[-1] == token:
        parts = parts[:-1]
    elif kind == "prefix" and parts[0] == token:
        parts = parts[1:]
    return " ".join(parts) or name


def humanize(name: str) -> str:
    """`high_low.weather` -> `high low weather`, in lower case: a spoken phrase."""
    return " ".join(_tokens(name))


def sort_key(name: str):
    """Alphabetical, but with runs of digits compared as numbers."""
    return [int(chunk) if chunk.isdigit() else chunk.lower() for chunk in re.split(r"(\d+)", name) if chunk]
