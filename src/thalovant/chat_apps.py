"""Chat apps: connect a chat network to a hub, and the hub answers there.

A hub owner connects a bot of their own -- a Telegram or Discord bot, a Matrix
user, a Twitch account -- to one of their hubs, chooses the chats, channels or
rooms it may answer in, and the platform runs the bridge between the two. The
API calls each connection a *bridge* (``/v1/bridges``), and each network a
*connector* (``GET /v1/bridges/connectors``).

These are the typed answers of those routes, and :class:`SecretValue`, the
only way a credential reaches them. The calls live on
:class:`~thalovant.control.AsyncThalovantControlPlane` and
:class:`~thalovant.control.ThalovantControlPlane`.

Every model keeps what the API sent: a status reason, a phase or a hold reason
this SDK does not know yet is kept as the string it was, never refused, so a
new value on the server does not break an old client.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hmac
from typing import TYPE_CHECKING, Any, Literal, Mapping, NoReturn, Sequence, Union, cast

from .errors import ThalovantAPIError

if TYPE_CHECKING:  # pragma: no cover - typing only; control imports this module
    from .control import OperationResource

#: ``setup`` until the owner chooses the first chats, then ``active``.
BridgeState = Literal["setup", "active"]
#: One conversation per person in a chat, or one shared by the whole chat.
BridgeSessionScope = Literal["person", "chat"]
#: ``Setup`` before the first chats are chosen, then what the running bridge reports.
BridgePhase = Literal["Setup", "Pending", "Ready", "Degraded", "Paused", "Error"]
#: The bridge's state in words a customer can be shown:
#:
#: * ``Setup`` -- no chats chosen yet;
#: * ``Starting`` -- rolling out, or not reported yet;
#: * ``Connected`` -- running and answering;
#: * ``Paused`` -- stopped, by the owner or by a hold;
#: * ``Unavailable`` -- rolled out but not running, or a platform-side problem;
#: * ``TokenMissing`` -- the stored credentials have not reached the bridge;
#: * ``TokenRejected`` -- the network refused the bridge's credentials: replace them.
BridgeStatusReason = Literal[
    "Setup", "Starting", "Connected", "Paused", "Unavailable", "TokenMissing", "TokenRejected"
]
#: Where the status came from.
BridgeStatusSource = Literal["lifecycle", "operator", "watcher", "unavailable"]
#: Why the platform paused a bridge: the plan allows fewer running bridges
#: (the hold lifts on its own once the plan has room), or the account was
#: deactivated (it resumes when the account is reactivated).
BridgeHoldReason = Literal["plan_limit", "account_suspended"]
#: ``offered`` and ``beta`` connectors are listed; ``hidden`` ones are not.
ChatAppStatus = Literal["offered", "beta", "hidden"]
#: What the API can do for a connector itself: check its token, list the
#: chats its bot is in, post a test message, serve its bot's picture.
ChatAppHelper = Literal["validate", "discover", "test_message", "avatar"]

#: What a redacted credential prints as.
REDACTED = "<redacted>"

#: The most bytes :meth:`~thalovant.control.AsyncThalovantControlPlane.get_bridge_avatar`
#: reads. The API serves a picture of at most 1 MiB.
MAX_BRIDGE_AVATAR_BYTES = 4 * 1024 * 1024


class SecretValue:
    """A credential that never prints: a bot token, an access token, a client secret.

    ``str()``, ``repr()``, f-strings and ``format()`` all show ``<redacted>``,
    and it cannot be pickled or copied into a log by accident. :meth:`reveal`
    returns the value; the SDK calls it only to build the request body that
    carries it to the API, and the API never sends a credential back.

    Plain strings are accepted wherever a :class:`SecretValue` is, and wrapped
    on the way in.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str | SecretValue) -> None:
        if isinstance(value, SecretValue):
            value = value.reveal()
        if not isinstance(value, str):
            raise TypeError("SecretValue wraps a string.")
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        """The credential itself. Only for sending it; never print what this returns."""
        return cast(str, object.__getattribute__(self, "_value"))

    def __setattr__(self, name: str, value: Any) -> NoReturn:
        raise AttributeError("SecretValue is immutable.")

    def __repr__(self) -> str:
        return f"SecretValue({REDACTED!r})"

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, format_spec: str) -> str:
        return format(REDACTED, format_spec)

    def __bool__(self) -> bool:
        return bool(self.reveal())

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretValue):
            return NotImplemented
        return hmac.compare_digest(self.reveal().encode(), other.reveal().encode())

    def __hash__(self) -> int:
        return hash((SecretValue, self.reveal()))

    def __reduce__(self) -> NoReturn:
        raise TypeError("A SecretValue cannot be pickled.")

    def __copy__(self) -> SecretValue:
        return self

    def __deepcopy__(self, memo: Any) -> SecretValue:
        return self


#: A credential as the bridge calls take it.
SecretInput = Union[str, SecretValue]


@dataclass(frozen=True)
class LocalizedText:
    """Copy the API ships in English, and in French when it has it."""

    en: str
    fr: str | None = None

    def text(self, language: str = "en") -> str:
        """The copy in ``language`` (``fr``, ``fr-CA`` ...), else in English."""
        if self.fr and str(language).lower().split("-", 1)[0].split("_", 1)[0] == "fr":
            return self.fr
        return self.en

    @classmethod
    def from_dict(cls, payload: Any, *, what: str = "chat app copy") -> LocalizedText:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("en"), str):
            raise ThalovantAPIError(f"Thalovant API returned {what} without English text.")
        return cls(en=payload["en"], fr=_optional_str(payload.get("fr")))


@dataclass(frozen=True)
class ChatAppSecretField:
    """One credential a connector needs, by key: never its value."""

    key: str
    label: LocalizedText
    description: LocalizedText | None = None
    required: bool = True

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ChatAppSecretField:
        return cls(
            key=_required(payload, "key", "chat app secret"),
            label=LocalizedText.from_dict(payload.get("label"), what="a chat app secret label"),
            description=_optional_text(payload.get("description")),
            required=payload.get("required") is not False,
        )


@dataclass(frozen=True)
class ChatAppTargets:
    """How a connector names the places a bridge answers in.

    ``pattern`` is the regular expression an id must match once
    ``strip_prefix`` is dropped and, when ``lowercase``, after folding: a
    Twitch ``#Name`` is stored as ``name``. ``max`` is how many a bridge takes.
    """

    label: LocalizedText
    pattern: str
    max: int
    lowercase: bool = False
    strip_prefix: str | None = None

    @classmethod
    def from_dict(cls, payload: Any) -> ChatAppTargets:
        if not isinstance(payload, Mapping):
            raise ThalovantAPIError("Thalovant API returned a chat app without targets.")
        return cls(
            label=LocalizedText.from_dict(payload.get("label"), what="chat app targets"),
            pattern=_required(payload, "pattern", "chat app targets"),
            max=_int(payload.get("max"), 0),
            lowercase=payload.get("lowercase") is True,
            strip_prefix=_optional_str(payload.get("strip_prefix")),
        )


@dataclass(frozen=True)
class ChatAppOptions:
    """Which bridge options a connector allows."""

    #: True when the network only lets the bot read what mentions it
    #: (Discord): ``require_mention`` cannot be turned off.
    require_mention_fixed: bool = False
    session_scopes: tuple[str, ...] = ("person", "chat")

    @classmethod
    def from_dict(cls, payload: Any) -> ChatAppOptions:
        if not isinstance(payload, Mapping):
            return cls()
        scopes = payload.get("session_scopes")
        return cls(
            require_mention_fixed=payload.get("require_mention_fixed") is True,
            session_scopes=_strings(scopes) if isinstance(scopes, list) else ("person", "chat"),
        )


@dataclass(frozen=True)
class ChatAppRelease:
    """The connector release new bridges run."""

    version: str
    released_at: str | None = None

    @classmethod
    def from_dict(cls, payload: Any) -> ChatAppRelease | None:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("version"), str):
            return None
        return cls(version=payload["version"], released_at=_optional_str(payload.get("released_at")))


@dataclass(frozen=True)
class ChatAppConnector:
    """One chat app a bridge can connect to (``GET /v1/bridges/connectors``).

    ``config_schema`` is the JSON Schema of its settings form (``title`` in
    English, ``x-title-fr`` in French); ``secrets`` the credentials it needs,
    by key; ``offered`` whether a bridge can be created for it now.
    """

    id: str
    name: str
    mark: str
    status: ChatAppStatus
    description: LocalizedText
    setup_steps: LocalizedText
    config_schema: dict[str, Any] = field(default_factory=dict)
    secrets: tuple[ChatAppSecretField, ...] = ()
    targets: ChatAppTargets | None = None
    options: ChatAppOptions = field(default_factory=ChatAppOptions)
    helpers: tuple[ChatAppHelper, ...] = ()
    offered: bool = False
    version: ChatAppRelease | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ChatAppConnector:
        schema = payload.get("config_schema")
        return cls(
            id=_required(payload, "id", "chat app"),
            name=_required(payload, "name", "chat app"),
            mark=_optional_str(payload.get("mark")) or "",
            status=cast(ChatAppStatus, _required(payload, "status", "chat app")),
            description=LocalizedText.from_dict(payload.get("description"), what="a chat app description"),
            setup_steps=LocalizedText.from_dict(payload.get("setup_steps"), what="chat app setup steps"),
            config_schema=dict(schema) if isinstance(schema, Mapping) else {},
            secrets=tuple(ChatAppSecretField.from_dict(row) for row in _rows(payload, "secrets", "chat app secret")),
            targets=ChatAppTargets.from_dict(payload.get("targets")),
            options=ChatAppOptions.from_dict(payload.get("options")),
            helpers=cast("tuple[ChatAppHelper, ...]", _strings(payload.get("helpers"))),
            offered=payload.get("offered") is True,
            version=ChatAppRelease.from_dict(payload.get("version")),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BridgeNetworkOffer:
    """A network this deployment offers, and whether a bridge can be created for it now."""

    network: str
    offered: bool
    name: str | None = None
    mark: str | None = None
    #: ``offered`` or ``beta``.
    status: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BridgeNetworkOffer:
        return cls(
            network=_required(payload, "network", "bridge network"),
            offered=payload.get("offered") is True,
            name=_optional_str(payload.get("name")),
            mark=_optional_str(payload.get("mark")),
            status=_optional_str(payload.get("status")),
        )


@dataclass(frozen=True)
class BridgeAllowance:
    """How many bridges the owner may have: the plan's ``max_chat_bridges``.

    Free plans get none (``paid_plan`` false, ``can_create`` false).
    ``can_create`` says whether creating one would pass the plan checks now;
    it says nothing about the token or the network.
    """

    owner_id: str
    plan: str
    paid_plan: bool
    limit: int
    used: int
    remaining: int
    can_create: bool

    @classmethod
    def from_dict(cls, payload: Any) -> BridgeAllowance:
        if not isinstance(payload, Mapping):
            raise ThalovantAPIError("Thalovant API returned bridge networks without an allowance.")
        return cls(
            owner_id=_required(payload, "owner_id", "bridge allowance"),
            plan=_optional_str(payload.get("plan")) or "",
            paid_plan=payload.get("paid_plan") is True,
            limit=_int(payload.get("limit"), 0),
            used=_int(payload.get("used"), 0),
            remaining=_int(payload.get("remaining"), 0),
            can_create=payload.get("can_create") is True,
        )


@dataclass(frozen=True)
class BridgeNetworks:
    """``GET /v1/bridges/networks``: what is offered, and the owner's allowance."""

    networks: tuple[BridgeNetworkOffer, ...]
    allowance: BridgeAllowance

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BridgeNetworks:
        return cls(
            networks=tuple(BridgeNetworkOffer.from_dict(row) for row in _rows(payload, "networks", "bridge network")),
            allowance=BridgeAllowance.from_dict(payload.get("allowance")),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BridgeConnectorRef:
    """The connector a bridge runs, and the release it was last started with."""

    id: str
    name: str
    version: str | None = None

    @classmethod
    def from_dict(cls, payload: Any) -> BridgeConnectorRef | None:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("id"), str):
            return None
        return cls(
            id=payload["id"],
            name=_optional_str(payload.get("name")) or payload["id"],
            version=_optional_str(payload.get("version")),
        )


@dataclass(frozen=True)
class BridgeBot:
    """Who the bridge's bot is on its network.

    ``avatar_url`` is the API's own address for the bot's picture; read it with
    :meth:`~thalovant.control.AsyncThalovantControlPlane.get_bridge_avatar`.
    """

    user_id: str
    username: str | None = None
    display_name: str | None = None
    application_id: str | None = None
    avatar_url: str | None = None

    @classmethod
    def from_dict(cls, payload: Any) -> BridgeBot:
        if not isinstance(payload, Mapping):
            raise ThalovantAPIError("Thalovant API returned a bridge without its bot.")
        return cls(
            user_id=_optional_str(payload.get("user_id")) or "",
            username=_optional_str(payload.get("username")),
            display_name=_optional_str(payload.get("display_name")),
            application_id=_optional_str(payload.get("application_id")),
            avatar_url=_optional_str(payload.get("avatar_url")),
        )


@dataclass(frozen=True)
class BridgeInviteLinks:
    """Links that add the bot to a group or server (Telegram, Discord)."""

    add_to_group: str | None = None
    direct_chat: str | None = None

    @classmethod
    def from_dict(cls, payload: Any) -> BridgeInviteLinks | None:
        if not isinstance(payload, Mapping):
            return None
        return cls(
            add_to_group=_optional_str(payload.get("add_to_group")),
            direct_chat=_optional_str(payload.get("direct_chat")),
        )


@dataclass(frozen=True)
class BridgeAllowedChat:
    """A chat, channel or room the bridge may answer in.

    Only ``id`` matters to the bridge; ``title`` and ``kind`` are what the
    picker showed, and ``guild_id``/``guild_name`` group Discord channels by
    server.
    """

    id: str
    title: str | None = None
    kind: str | None = None
    guild_id: str | None = None
    guild_name: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BridgeAllowedChat:
        return cls(
            id=_required(payload, "id", "allowed chat"),
            title=_optional_str(payload.get("title")),
            kind=_optional_str(payload.get("kind")),
            guild_id=_optional_str(payload.get("guild_id")),
            guild_name=_optional_str(payload.get("guild_name")),
        )

    def as_dict(self) -> dict[str, Any]:
        """The request form: ``id`` and whichever display fields are set."""
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True)
class BridgeHold:
    """A pause the platform put on a bridge (``None`` on the owner's own pause)."""

    reason: BridgeHoldReason
    since: str | None = None

    @classmethod
    def from_dict(cls, payload: Any) -> BridgeHold | None:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("reason"), str):
            return None
        return cls(reason=cast(BridgeHoldReason, payload["reason"]), since=_optional_str(payload.get("since")))


@dataclass(frozen=True)
class BridgeStatus:
    """Where a bridge is. Word it from ``reason``; see :data:`BridgeStatusReason`.

    ``message`` and ``operator_reason`` are platform internals, sent to
    platform admins only and ``None`` for everybody else. ``applied`` is true
    once the running bridge carries the last change, false while one is on its
    way, and ``None`` in setup.
    """

    phase: BridgePhase
    reason: BridgeStatusReason
    ready: bool = False
    source: BridgeStatusSource = "unavailable"
    message: str | None = None
    operator_reason: str | None = None
    generation: int | None = None
    observed_generation: int | None = None
    applied: bool | None = None

    @classmethod
    def from_dict(cls, payload: Any) -> BridgeStatus:
        if not isinstance(payload, Mapping):
            raise ThalovantAPIError("Thalovant API returned a bridge without its status.")
        applied = payload.get("applied")
        return cls(
            phase=cast(BridgePhase, _required(payload, "phase", "bridge status")),
            reason=cast(BridgeStatusReason, _required(payload, "reason", "bridge status")),
            ready=payload.get("ready") is True,
            source=cast(BridgeStatusSource, _optional_str(payload.get("source")) or "unavailable"),
            message=_optional_str(payload.get("message")),
            operator_reason=_optional_str(payload.get("operator_reason")),
            generation=_optional_int(payload.get("generation")),
            observed_generation=_optional_int(payload.get("observed_generation")),
            applied=applied if isinstance(applied, bool) else None,
        )


@dataclass(frozen=True)
class Bridge:
    """One hub's connection to a chat network (``/v1/bridges/{id}``).

    ``etag`` is what :meth:`~thalovant.control.AsyncThalovantControlPlane.update_bridge`
    takes to refuse a change made on a stale read. ``settings`` holds the
    non-secret settings; ``secrets_set`` the keys of the stored credentials,
    never their values. ``hold`` is set when the platform paused the bridge.
    ``operation`` is the change a write queued, when it queued one.
    """

    id: str
    hub_id: str
    owner_id: str
    network: str
    state: BridgeState
    status: BridgeStatus
    etag: str
    bot: BridgeBot
    connector: BridgeConnectorRef | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    secrets_set: tuple[str, ...] = ()
    resource_name: str | None = None
    invite_links: BridgeInviteLinks | None = None
    allowed: tuple[BridgeAllowedChat, ...] = ()
    require_mention: bool = True
    session_scope: BridgeSessionScope = "person"
    paused: bool = False
    hold: BridgeHold | None = None
    token_updated_at: str | None = None
    activated_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    operation: OperationResource | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Bridge:
        from .control import OperationResource  # control imports this module

        settings = payload.get("settings")
        operation = payload.get("operation")
        return cls(
            id=_required(payload, "id", "bridge"),
            hub_id=_required(payload, "hub_id", "bridge"),
            owner_id=_optional_str(payload.get("owner_id")) or "",
            network=_required(payload, "network", "bridge"),
            state=cast(BridgeState, _required(payload, "state", "bridge")),
            status=BridgeStatus.from_dict(payload.get("status")),
            etag=_required(payload, "etag", "bridge"),
            bot=BridgeBot.from_dict(payload.get("bot")),
            connector=BridgeConnectorRef.from_dict(payload.get("connector")),
            settings=dict(settings) if isinstance(settings, Mapping) else {},
            secrets_set=_strings(payload.get("secrets_set")),
            resource_name=_optional_str(payload.get("resource_name")),
            invite_links=BridgeInviteLinks.from_dict(payload.get("invite_links")),
            allowed=tuple(BridgeAllowedChat.from_dict(row) for row in _rows(payload, "allowed", "allowed chat")),
            require_mention=payload.get("require_mention") is not False,
            session_scope=cast(BridgeSessionScope, _required(payload, "session_scope", "bridge")),
            paused=payload.get("paused") is True,
            hold=BridgeHold.from_dict(payload.get("hold")),
            token_updated_at=_optional_str(payload.get("token_updated_at")),
            activated_at=_optional_str(payload.get("activated_at")),
            created_at=_optional_str(payload.get("created_at")),
            updated_at=_optional_str(payload.get("updated_at")),
            operation=OperationResource.from_dict(operation) if isinstance(operation, Mapping) else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BridgeChat:
    """A chat or channel the bot can answer in, to choose from (``GET .../chats``)."""

    id: str
    title: str
    kind: str
    guild_id: str | None = None
    guild_name: str | None = None
    guild_icon_url: str | None = None
    #: Whether the bridge already answers there.
    allowed: bool = False
    #: An end-to-end encrypted room the bridge cannot read.
    encrypted: bool = False
    #: Whether it can be chosen.
    selectable: bool = True

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BridgeChat:
        return cls(
            id=_required(payload, "id", "bridge chat"),
            title=_optional_str(payload.get("title")) or "",
            kind=_optional_str(payload.get("kind")) or "",
            guild_id=_optional_str(payload.get("guild_id")),
            guild_name=_optional_str(payload.get("guild_name")),
            guild_icon_url=_optional_str(payload.get("guild_icon_url")),
            allowed=payload.get("allowed") is True,
            encrypted=payload.get("encrypted") is True,
            selectable=payload.get("selectable") is not False,
        )

    def as_allowed(self) -> BridgeAllowedChat:
        """This chat as an ``allowed`` entry, display fields included."""
        return BridgeAllowedChat(
            id=self.id, title=self.title or None, kind=self.kind or None,
            guild_id=self.guild_id, guild_name=self.guild_name,
        )


@dataclass(frozen=True)
class BridgeChatList:
    """The chats to choose from, and what to tell the owner beside them.

    ``manual_entry`` is true for a connector the API cannot list chats for
    (Matrix, Mattermost, Twitch): ``data`` is empty and the owner types the
    ids, which must match the connector's ``targets.pattern``. Iterating goes
    over ``data``.
    """

    network: str
    data: tuple[BridgeChat, ...] = ()
    note: str | None = None
    manual_entry: bool = False

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BridgeChatList:
        return cls(
            network=_required(payload, "network", "bridge chat listing"),
            data=tuple(BridgeChat.from_dict(row) for row in _rows(payload, "data", "bridge chat")),
            note=_optional_str(payload.get("note")),
            manual_entry=payload.get("manual_entry") is True,
        )

    def __iter__(self) -> Any:
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BridgeTestResult:
    """A test message was posted; ``language`` is the one it was posted in."""

    sent: bool
    chat_id: str
    language: str = "en"

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BridgeTestResult:
        return cls(
            sent=payload.get("sent") is True,
            chat_id=_required(payload, "chat_id", "bridge test result"),
            language=_optional_str(payload.get("language")) or "en",
        )


@dataclass(frozen=True)
class BridgeAvatar:
    """The bot's picture, as the API serves it."""

    content: bytes = field(repr=False)
    content_type: str = "application/octet-stream"


#: An ``allowed`` entry as :meth:`~thalovant.control.AsyncThalovantControlPlane.update_bridge`
#: takes it: a :class:`BridgeAllowedChat`, a chat from the listing, a mapping
#: with an ``id``, or the id itself.
AllowedChatInput = Union[BridgeAllowedChat, BridgeChat, Mapping[str, Any], str]


def bridge_create_body(
    hub_id: str,
    connector: str,
    *,
    token: SecretInput | None = None,
    secrets: Mapping[str, SecretInput] | None = None,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``POST /v1/bridges`` body. It holds credentials: never log it."""
    _require_text(hub_id, "hub_id")
    _require_text(connector, "connector")
    if token is not None and secrets:
        raise ValueError("Send the connector's token or its secrets, not both.")
    if token is None and not secrets:
        raise ValueError("A bridge needs the connector's token or its secrets.")
    body: dict[str, Any] = {"hub_id": hub_id, "connector": connector}
    if token is not None:
        body["token"] = _reveal(token, "token")
    else:
        body["secrets"] = _secret_map(secrets or {})
    if settings is not None:
        body["settings"] = _settings(settings)
    return body


def bridge_update_body(
    *,
    allowed: Sequence[AllowedChatInput] | None = None,
    require_mention: bool | None = None,
    session_scope: str | None = None,
    paused: bool | None = None,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``PATCH /v1/bridges/{id}`` body: only what the caller set."""
    body: dict[str, Any] = {}
    if allowed is not None:
        if isinstance(allowed, (str, bytes)) or not isinstance(allowed, Sequence):
            raise ValueError("allowed is a list of chats.")
        body["allowed"] = [_allowed_entry(chat) for chat in allowed]
    for name, value in (("require_mention", require_mention), ("paused", paused)):
        if value is not None:
            if not isinstance(value, bool):
                raise ValueError(f"{name} is True or False.")
            body[name] = value
    if session_scope is not None:
        _require_text(session_scope, "session_scope")
        body["session_scope"] = session_scope
    if settings is not None:
        body["settings"] = _settings(settings)
    return body


def bridge_secrets_body(secrets: Mapping[str, SecretInput]) -> dict[str, Any]:
    """The ``PUT /v1/bridges/{id}/secrets`` body. It holds credentials: never log it."""
    if not secrets:
        raise ValueError("Replace at least one secret.")
    return {"secrets": _secret_map(secrets)}


def bridge_token_body(token: SecretInput) -> dict[str, Any]:
    """The ``PUT /v1/bridges/{id}/token`` body. It holds a credential: never log it."""
    return {"token": _reveal(token, "token")}


def _allowed_entry(chat: AllowedChatInput) -> dict[str, Any]:
    if isinstance(chat, str):
        _require_text(chat, "an allowed chat id")
        return {"id": chat}
    if isinstance(chat, BridgeChat):
        return chat.as_allowed().as_dict()
    if isinstance(chat, BridgeAllowedChat):
        return chat.as_dict()
    if isinstance(chat, Mapping) and isinstance(chat.get("id"), str) and chat["id"]:
        return {key: value for key, value in chat.items() if value is not None}
    raise ValueError("Each allowed chat is an id, or a chat with an id.")


def _settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(settings, Mapping):
        raise ValueError("settings maps setting names to values.")
    for key, value in settings.items():
        if isinstance(value, SecretValue):
            raise ValueError(f"Setting {key!r} is a credential: send it as one of the connector's secrets.")
    return dict(settings)


def _secret_map(secrets: Mapping[str, SecretInput]) -> dict[str, str]:
    if not isinstance(secrets, Mapping):
        raise ValueError("secrets maps the connector's secret keys to their values.")
    out: dict[str, str] = {}
    for key, value in secrets.items():
        _require_text(key, "a secret key")
        out[key] = _reveal(value, f"secret {key!r}")
    return out


def _reveal(value: SecretInput, name: str) -> str:
    if isinstance(value, SecretValue):
        return value.reveal()
    if isinstance(value, str):
        return value
    # The value itself is never named: it may be the credential.
    raise ValueError(f"{name} must be a string or a SecretValue.")


def _require_text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")


def _required(payload: Mapping[str, Any], key: str, what: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ThalovantAPIError(f"Thalovant API returned a {what} without {key}.")
    return value


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_text(value: Any) -> LocalizedText | None:
    if isinstance(value, Mapping) and isinstance(value.get("en"), str):
        return LocalizedText(en=value["en"], fr=_optional_str(value.get("fr")))
    return None


def _int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()


def _rows(payload: Mapping[str, Any], key: str, what: str) -> list[Mapping[str, Any]]:
    rows = payload.get(key)
    if rows is None:
        return []
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ThalovantAPIError(f"Thalovant API returned an unexpected {what} listing shape.")
    return rows


def connectors_from_payload(payload: Mapping[str, Any]) -> list[ChatAppConnector]:
    """The ``{data: [...]}`` envelope of ``GET /v1/bridges/connectors``."""
    if not isinstance(payload.get("data"), list):
        raise ThalovantAPIError("Thalovant API returned an unexpected chat app listing shape.")
    return [ChatAppConnector.from_dict(row) for row in _rows(payload, "data", "chat app")]


def bridges_from_payload(payload: Mapping[str, Any]) -> list[Bridge]:
    """The ``{data: [...]}`` envelope of ``GET /v1/bridges``."""
    if not isinstance(payload.get("data"), list):
        raise ThalovantAPIError("Thalovant API returned an unexpected bridge listing shape.")
    return [Bridge.from_dict(row) for row in _rows(payload, "data", "bridge")]


__all__ = [
    "MAX_BRIDGE_AVATAR_BYTES",
    "REDACTED",
    "AllowedChatInput",
    "Bridge",
    "BridgeAllowance",
    "BridgeAllowedChat",
    "BridgeAvatar",
    "BridgeBot",
    "BridgeChat",
    "BridgeChatList",
    "BridgeConnectorRef",
    "BridgeHold",
    "BridgeHoldReason",
    "BridgeInviteLinks",
    "BridgeNetworkOffer",
    "BridgeNetworks",
    "BridgePhase",
    "BridgeSessionScope",
    "BridgeState",
    "BridgeStatus",
    "BridgeStatusReason",
    "BridgeStatusSource",
    "BridgeTestResult",
    "ChatAppConnector",
    "ChatAppHelper",
    "ChatAppOptions",
    "ChatAppRelease",
    "ChatAppSecretField",
    "ChatAppStatus",
    "ChatAppTargets",
    "LocalizedText",
    "SecretInput",
    "SecretValue",
]
