"""What a hub can be asked: the intent inventory, over the client's own session.

The hub runtime keeps an intent manifest (OVOS-INTENT-4 section 10): every
intent a skill registered, per language, and on request the registration
itself, which for a template intent carries the sentences from the skill's
locale files, slots and all -- ``what is the weather in {location}``. This
module asks that manifest and shapes the answer, so a satellite, an installer
or an agent shows a person what they can say without a control-plane token.

Two queries, correlated by ``context.request_id`` like every other request:

- ``ovos.intent.list`` ``{"lang": <tag>}`` ->
  ``ovos.intent.list.response`` ``{"ok", "intents": [{skill_id, intent_name,
  lang, method, enabled, session_id}]}``. ``method`` is ``template`` (sample
  sentences) or ``keyword`` (keyword sets). A runtime may attach each entry's
  ``definition`` when asked with ``include_definitions``; when it does not,
  the client describes each intent individually.
- ``ovos.intent.describe`` ``{"skill_id", "intent_name", "lang"}`` ->
  ``ovos.intent.describe.response`` ``{"ok", "definitions": [{method,
  definition}]}`` or ``{"ok": false, "error"}``.

A hub whose connection may not publish a type answers ``hive.policy.denied``
naming it; that becomes :class:`ThalovantPolicyDeniedError` at once rather
than a timeout. The engines' own manifests (``intent.service.adapt.manifest.get``
and ``intent.service.padatious.manifest.get``, names only, no language) are the
fallback for a hub allowed for those alone.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

from .errors import ThalovantPolicyDeniedError, ThalovantTimeoutError
from .events import (
    EVENT_ADAPT_MANIFEST,
    EVENT_ADAPT_MANIFEST_GET,
    EVENT_INTENT_DESCRIBE,
    EVENT_INTENT_DESCRIBE_RESPONSE,
    EVENT_INTENT_LIST,
    EVENT_INTENT_LIST_RESPONSE,
    EVENT_PADATIOUS_MANIFEST,
    EVENT_PADATIOUS_MANIFEST_GET,
    EVENT_POLICY_DENIED,
    ThalovantEvent,
    _new_request_id,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import ThalovantClient

SOURCE_MANIFEST = "intent-manifest"
SOURCE_ENGINES = "engine-manifests"

_ENGINE_BY_METHOD = {"template": "padatious", "keyword": "adapt"}


def same_language(a: str, b: str) -> bool:
    """``fr-fr`` and ``fr_FR`` are the same language tag."""

    return a.strip().lower().replace("_", "-") == b.strip().lower().replace("_", "-")


@dataclass(frozen=True)
class IntentRegistration:
    """One row of the hub's intent manifest."""

    skill_id: str
    intent_name: str
    lang: str
    method: str
    enabled: bool = True
    session_id: str = "default"
    definition: dict[str, Any] | None = None

    @property
    def engine(self) -> str:
        return _ENGINE_BY_METHOD.get(self.method, self.method or "unknown")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "IntentRegistration | None":
        skill_id = str(raw.get("skill_id") or "").strip()
        intent_name = str(raw.get("intent_name") or "").strip()
        if not skill_id or not intent_name:
            return None
        definition = raw.get("definition")
        return cls(
            skill_id=skill_id,
            intent_name=intent_name,
            lang=str(raw.get("lang") or ""),
            method=str(raw.get("method") or ""),
            enabled=raw.get("enabled") is not False,
            session_id=str(raw.get("session_id") or "default"),
            definition=dict(definition) if isinstance(definition, dict) else None,
        )


@dataclass(frozen=True)
class IntentDefinition:
    """A registration as the skill made it, from ``ovos.intent.describe``."""

    skill_id: str
    intent_name: str
    lang: str
    method: str
    samples: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def engine(self) -> str:
        return _ENGINE_BY_METHOD.get(self.method, self.method or "unknown")

    @classmethod
    def from_mapping(cls, item: Mapping[str, Any]) -> "IntentDefinition | None":
        definition = item.get("definition")
        if not isinstance(definition, dict):
            return None
        skill_id = str(definition.get("skill_id") or "").strip()
        intent_name = str(definition.get("intent_name") or "").strip()
        if not skill_id or not intent_name:
            return None
        return cls(
            skill_id=skill_id,
            intent_name=intent_name,
            lang=str(definition.get("lang") or ""),
            method=str(item.get("method") or definition.get("method") or ""),
            samples=_samples(definition),
            raw=dict(definition),
        )


def _samples(definition: Mapping[str, Any]) -> tuple[str, ...]:
    raw = definition.get("samples")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(
        text.strip() for text in raw if isinstance(text, str) and text.strip()
    )


@dataclass(frozen=True)
class HubIntent:
    """One thing a hub can be asked, with the sentences that ask it, per language."""

    skill_id: str
    name: str
    engine: str
    phrases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    enabled: bool = True

    @property
    def id(self) -> str:
        return f"{self.skill_id}:{self.name}"

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(self.phrases)

    def phrases_for(self, lang: str) -> tuple[str, ...]:
        for candidate, sentences in self.phrases.items():
            if same_language(candidate, lang):
                return sentences
        return ()

    def examples(self, lang: str | None = None, limit: int = 2) -> tuple[str, ...]:
        """A few sentences worth showing: whole ones before ones with a slot."""

        pool = self.phrases_for(lang) if lang else next(iter(self.phrases.values()), ())
        if limit <= 0:
            return pool
        return tuple(sorted(pool, key=lambda text: ("{" in text, len(text)))[:limit])

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "skill_id": self.skill_id,
            "name": self.name,
            "engine": self.engine,
            "enabled": self.enabled,
            "phrases": {lang: list(texts) for lang, texts in self.phrases.items()},
        }


@dataclass(frozen=True)
class HubSkillIntents:
    skill_id: str
    intents: tuple[HubIntent, ...]

    @property
    def languages(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for intent in self.intents:
            for lang in intent.phrases:
                seen.setdefault(lang, None)
        return tuple(seen)

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "languages": list(self.languages),
            "intents": [intent.as_dict() for intent in self.intents],
        }


@dataclass(frozen=True)
class HubIntentInventory:
    """Everything a hub can be asked, grouped by skill.

    ``source`` says how it was read: ``intent-manifest`` carries sentences per
    language; ``engine-manifests`` is the names-only fallback, and ``denied``
    then names the query the hub refused.
    """

    languages: tuple[str, ...]
    skills: tuple[HubSkillIntents, ...]
    source: str = SOURCE_MANIFEST
    denied: tuple[str, ...] = ()

    @property
    def intents(self) -> tuple[HubIntent, ...]:
        return tuple(intent for skill in self.skills for intent in skill.intents)

    @property
    def has_phrases(self) -> bool:
        """True when at least one intent carries at least one sentence."""

        return any(
            sentences for intent in self.intents for sentences in intent.phrases.values()
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "languages": list(self.languages),
            "source": self.source,
            "denied": list(self.denied),
            "skills": [skill.as_dict() for skill in self.skills],
        }


# ----------------------------------------------------------------------------
# the wire
# ----------------------------------------------------------------------------


class _Denials:
    """Collect ``hive.policy.denied`` while a request is out."""

    def __init__(self) -> None:
        self.by_type: dict[str, ThalovantEvent] = {}
        self.event = threading.Event()

    def __call__(self, event: ThalovantEvent) -> None:
        denied_type = str(event.data.get("denied_type") or "")
        if denied_type:
            self.by_type[denied_type] = event
            self.event.set()

    def raise_if(self, *types: str) -> None:
        for denied_type in types:
            event = self.by_type.get(denied_type)
            if event is not None:
                raise ThalovantPolicyDeniedError.from_event(event)


def _wait(
    done: threading.Event,
    denials: _Denials,
    *,
    timeout: float,
    denied_types: tuple[str, ...],
    what: str,
) -> None:
    """Block until the reply, a matching denial, or the deadline."""

    started = time.monotonic()
    while True:
        denials.raise_if(*denied_types)
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise ThalovantTimeoutError(f"Hub did not answer {what} within {timeout:g}s.")
        if done.wait(min(0.05, remaining)):
            return


def request_reply(
    client: "ThalovantClient",
    query_type: str,
    reply_type: str,
    data: dict[str, Any],
    *,
    lang: str | None = None,
    timeout: float,
) -> ThalovantEvent:
    """Send one bus query and return its reply, matched by request id.

    A reply may arrive more than once; the first one wins and repeats are
    dropped. A ``hive.policy.denied`` naming the query raises at once.
    """

    request_id = _new_request_id()
    context: dict[str, Any] = {"request_id": request_id}
    if lang:
        context["lang"] = lang
    done = threading.Event()
    answer: list[ThalovantEvent] = []

    def keep(event: ThalovantEvent) -> None:
        if not answer:
            answer.append(event)
            done.set()

    denials = _Denials()
    client.connect()
    with client.on(EVENT_POLICY_DENIED, denials), client.on(
        reply_type, keep, request_id=request_id
    ):
        client.emit(query_type, data, context)
        _wait(done, denials, timeout=timeout, denied_types=(query_type,), what=query_type)
    return answer[0]


def list_intents(
    client: "ThalovantClient",
    lang: str,
    *,
    timeout: float = 5.0,
    include_definitions: bool = False,
) -> list[IntentRegistration]:
    """The hub's intent manifest for one language."""

    data: dict[str, Any] = {"lang": lang}
    if include_definitions:
        data["include_definitions"] = True
    event = request_reply(
        client, EVENT_INTENT_LIST, EVENT_INTENT_LIST_RESPONSE, data, lang=lang, timeout=timeout
    )
    rows = event.data.get("intents")
    if not isinstance(rows, list):
        return []
    entries = (IntentRegistration.from_mapping(row) for row in rows if isinstance(row, dict))
    return [entry for entry in entries if entry is not None]


def describe_intent(
    client: "ThalovantClient",
    skill_id: str,
    intent_name: str,
    lang: str,
    *,
    timeout: float = 5.0,
) -> list[IntentDefinition]:
    """Every registration behind one intent in one language, keyword ones first."""

    event = request_reply(
        client,
        EVENT_INTENT_DESCRIBE,
        EVENT_INTENT_DESCRIBE_RESPONSE,
        {"skill_id": skill_id, "intent_name": intent_name, "lang": lang},
        lang=lang,
        timeout=timeout,
    )
    if event.data.get("ok") is False:
        return []
    items = event.data.get("definitions")
    if not isinstance(items, list):
        return []
    found = (IntentDefinition.from_mapping(item) for item in items if isinstance(item, dict))
    return [definition for definition in found if definition is not None]


def describe_many(
    client: "ThalovantClient",
    wanted: Iterable[tuple[str, str, str]],
    *,
    timeout: float = 5.0,
) -> dict[tuple[str, str, str], list[IntentDefinition]]:
    """Describe many registrations with the requests in flight together.

    One subscription, one request id per registration, replies matched by
    that id, repeats dropped. The deadline covers the whole batch.
    """

    wanted = list(dict.fromkeys(wanted))
    if not wanted:
        return {}
    by_request: dict[str, tuple[str, str, str]] = {}
    found: dict[tuple[str, str, str], list[IntentDefinition]] = {}
    lock = threading.Lock()
    done = threading.Event()

    def keep(event: ThalovantEvent) -> None:
        items = event.data.get("definitions")
        definitions = [
            definition
            for definition in (
                IntentDefinition.from_mapping(item)
                for item in (items if isinstance(items, list) else ())
                if isinstance(item, dict)
            )
            if definition is not None
        ]
        key = by_request.get(event.request_id or "")
        if key is None and definitions:
            # No request id came back: the definition names what it describes.
            first = definitions[0]
            key = next(
                (
                    candidate for candidate in wanted
                    if candidate[0] == first.skill_id and candidate[1] == first.intent_name
                    and same_language(candidate[2], first.lang)
                ),
                None,
            )
        if key is None:
            return
        with lock:
            if key in found:
                return
            found[key] = [] if event.data.get("ok") is False else definitions
            if len(found) == len(wanted):
                done.set()

    denials = _Denials()
    client.connect()
    with client.on(EVENT_POLICY_DENIED, denials), client.on(
        EVENT_INTENT_DESCRIBE_RESPONSE, keep
    ):
        for key in wanted:
            skill_id, intent_name, lang = key
            request_id = _new_request_id()
            by_request[request_id] = key
            client.emit(
                EVENT_INTENT_DESCRIBE,
                {"skill_id": skill_id, "intent_name": intent_name, "lang": lang},
                {"request_id": request_id, "lang": lang},
            )
        try:
            _wait(
                done,
                denials,
                timeout=timeout,
                denied_types=(EVENT_INTENT_DESCRIBE,),
                what=EVENT_INTENT_DESCRIBE,
            )
        except ThalovantTimeoutError:
            if not found:
                raise
            # A partial answer is still an answer: the intents the hub did not
            # describe in time simply carry no sentences.
    return found


def intent_names(
    client: "ThalovantClient",
    lang: str,
    *,
    timeout: float = 5.0,
) -> dict[str, list[str]]:
    """The engines' own manifests: ``{"adapt": [names], "padatious": [names]}``.

    Names only, and the same names whatever the language asked, because an
    intent's name is the same in every language. The fallback for a hub
    allowed for these queries but not the intent manifest.
    """

    names: dict[str, list[str]] = {}
    for engine, query_type, reply_type in (
        ("adapt", EVENT_ADAPT_MANIFEST_GET, EVENT_ADAPT_MANIFEST),
        ("padatious", EVENT_PADATIOUS_MANIFEST_GET, EVENT_PADATIOUS_MANIFEST),
    ):
        event = request_reply(client, query_type, reply_type, {"lang": lang}, lang=lang, timeout=timeout)
        raw = event.data.get("intents")
        names[engine] = [
            text for text in (raw if isinstance(raw, list) else ()) if isinstance(text, str) and text
        ]
    return names


def _inventory_from_names(names: dict[str, list[str]], languages: tuple[str, ...], denied: str) -> HubIntentInventory:
    by_skill: dict[str, dict[str, HubIntent]] = {}
    for engine, entries in names.items():
        for raw in entries:
            skill_id, _, intent_name = raw.partition(":")
            if not intent_name:
                skill_id, intent_name = "", raw
            # First engine to name it wins, as on the manifest path.
            by_skill.setdefault(skill_id, {}).setdefault(
                intent_name, HubIntent(skill_id=skill_id, name=intent_name, engine=engine)
            )
    skills = tuple(
        HubSkillIntents(skill_id=skill_id, intents=tuple(sorted(intents.values(), key=lambda i: i.name)))
        for skill_id, intents in sorted(by_skill.items())
    )
    return HubIntentInventory(languages=languages, skills=skills, source=SOURCE_ENGINES, denied=(denied,))


def inventory(
    client: "ThalovantClient",
    languages: Iterable[str],
    *,
    timeout: float = 5.0,
    describe: bool = True,
    fallback: bool = True,
) -> HubIntentInventory:
    """Everything the hub can be asked, in each language, grouped by skill.

    Asks the intent manifest per language and, unless the runtime attached
    definitions to the listing, describes every registration at once. When
    the hub refuses ``ovos.intent.list`` and ``fallback`` is on, the engines'
    manifests give the names and the result says so.
    """

    asked: tuple[str, ...] = ()
    for lang in languages:
        tag = str(lang).strip()
        if tag and not any(same_language(tag, seen) for seen in asked):
            asked += (tag,)
    if not asked:
        raise ValueError("inventory() needs at least one language.")

    listed: dict[str, list[IntentRegistration]] = {}
    try:
        for lang in asked:
            listed[lang] = list_intents(
                client, lang, timeout=timeout, include_definitions=describe
            )
    except ThalovantPolicyDeniedError as denied:
        if not fallback or denied.denied_type != EVENT_INTENT_LIST:
            raise
        names = intent_names(client, asked[0], timeout=timeout)
        return _inventory_from_names(names, asked, denied.denied_type)

    wanted = []
    for lang, entries in listed.items():
        for entry in entries:
            if entry.enabled and entry.definition is None and entry.method == "template":
                wanted.append((entry.skill_id, entry.intent_name, lang))
    described = describe_many(client, wanted, timeout=timeout) if describe and wanted else {}

    phrases: dict[tuple[str, str], dict[str, tuple[str, ...]]] = {}
    engines: dict[tuple[str, str], str] = {}
    enabled: dict[tuple[str, str], bool] = {}
    for lang, entries in listed.items():
        for entry in entries:
            key = (entry.skill_id, entry.intent_name)
            engines.setdefault(key, entry.engine)
            enabled[key] = enabled.get(key, False) or entry.enabled
            if entry.definition is not None:
                sentences = _samples(entry.definition)
            else:
                sentences = ()
                for definition in described.get((entry.skill_id, entry.intent_name, lang), ()):
                    if definition.samples:
                        sentences = definition.samples
                        break
            # An intent registered under both engines has two rows for the
            # language; the keyword row carries no sentences and must not
            # erase the template row's.
            per_language = phrases.setdefault(key, {})
            if sentences or lang not in per_language:
                per_language[lang] = sentences

    by_skill: dict[str, list[HubIntent]] = {}
    for (skill_id, intent_name), per_language in phrases.items():
        by_skill.setdefault(skill_id, []).append(HubIntent(
            skill_id=skill_id,
            name=intent_name,
            engine=engines[(skill_id, intent_name)],
            phrases=dict(per_language),
            enabled=enabled[(skill_id, intent_name)],
        ))
    skills = tuple(
        HubSkillIntents(skill_id=skill_id, intents=tuple(sorted(intents, key=lambda i: i.name)))
        for skill_id, intents in sorted(by_skill.items())
    )
    return HubIntentInventory(languages=asked, skills=skills, source=SOURCE_MANIFEST)


__all__ = [
    "SOURCE_ENGINES",
    "SOURCE_MANIFEST",
    "HubIntent",
    "HubIntentInventory",
    "HubSkillIntents",
    "IntentDefinition",
    "IntentRegistration",
    "describe_intent",
    "describe_many",
    "intent_names",
    "inventory",
    "list_intents",
    "request_reply",
    "same_language",
]
