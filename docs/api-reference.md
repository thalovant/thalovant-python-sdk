# API Reference

This is a concise reference for the public SDK surface. For generated API docs,
open the reference pages under **Generated Reference** in the navigation.

## Clients

### `ThalovantClient`

Synchronous client.

Constructors:

- `ThalovantClient(identity, **options)`
- `ThalovantClient.from_identity_file(path, **options)`
- `ThalovantClient.from_env(**options)`

Common methods:

- `connect()`
- `close()` / `disconnect()`
- `healthcheck() -> ThalovantHealth`
- `doctor() -> ThalovantDoctorReport`
- `ask(text, timeout=12.0, lang="en-us", context=None, session_id=None, request_id=None) -> ThalovantReply`
- `send_utterance(text, lang="en-us", context=None, session_id=None, request_id=None)`
- `send_action(payload, title=None, lang="en-us", context=None, session_id=None, request_id=None)`
- `send_code(value, kind="code", label=None, lang="en-us", context=None, session_id=None, request_id=None)`
- `conversation(session_id=None, lang="en-us", context=None) -> ThalovantConversation`
- `on(event_name, handler, context=None, session_id=None, request_id=None, predicate=None) -> ThalovantSubscription`
- `wait_for_event(event_name, timeout=12.0, predicate=None, context=None, session_id=None, request_id=None) -> ThalovantEvent`
- `listen(event_name, timeout=None, max_events=None, predicate=None, context=None, session_id=None, request_id=None)`
- `emit(event_type, data=None, context=None)`
- `intents(languages=None, timeout=5.0, describe=True, fallback=True) -> HubIntentInventory`
- `list_intents(lang=None, timeout=5.0, include_definitions=False) -> list[IntentRegistration]`
- `describe_intent(skill_id, intent_name, lang=None, timeout=5.0) -> list[IntentDefinition]`

`intents()` reads the hub runtime's intent manifest over this session: every
intent each skill registered, per language, with the sentences a person says
to reach it as the skill's locale files wrote them, `{slot}` placeholders
included. No control-plane credential is involved. The hub's connection must
be allowed to publish `ovos.intent.list` and `ovos.intent.describe`; a refusal
raises `ThalovantPolicyDeniedError` naming the type, or with `fallback=True`
(the default) falls back to the engines' own manifests, which carry names and
no language, and marks the result `source="engine-manifests"`.

## Control Plane

### `ThalovantControlPlane`

Authenticated helper for the Thalovant API.

Methods:

- `login(email, password, scope=None, otp_code=None, recovery_code=None)`
- `login_with_browser(scopes=None, client_name=None, open_browser=True, prompt=None, timeout=900.0)`
- `list_hubs(limit=100, cursor=None, owner_id=None)`
- `list_public_hubs(limit=24, cursor=None)`
- `get_hub(hub_id)`
- `get_public_hub(hub_ref)`
- `get_operation(operation_id)`
- `create_client(payload, idempotency_key=None)`
- `create_client_identity(hub, name, site_id=None, spec=None, owner_id=None, active=True, preferred_protocols=("wss", "https", "mqtt"))`

Hub provisioning:

- `create_hub(payload, idempotency_key=None)`
- `update_hub(hub_id, payload, etag=...)`
- `delete_hub(hub_id, etag=...)`
- `release_hub(hub_id, channel=None, mode=None, version=None, images=None, reason=None)`
- `set_hub_rating(hub_id, rating)`
- `clear_hub_rating(hub_id)`
- `get_hub_runtime_capabilities(hub_id)`

Skill discovery:

- `list_marketplace_skills(owner_id=None, include_inactive=False, force_refresh=False)`
- `list_runtime_group_marketplace(runtime_group_id, refresh_inventory=False)`
- `list_runtime_group_inventory(runtime_group_id, refresh=False)`

Runtime groups and skills:

- `list_runtime_groups(owner_id=None)`
- `get_runtime_group(runtime_group_id)`
- `create_runtime_group(payload)`
- `update_runtime_group(runtime_group_id, payload)`
- `get_runtime_group_config(runtime_group_id)`
- `update_runtime_group_config(runtime_group_id, config, personas=None)`
- `release_runtime_group(runtime_group_id, channel=None, mode=None, version=None, images=None, reason=None)`
- `delete_runtime_group(runtime_group_id)`
- `install_runtime_group_skill(runtime_group_id, skill_id, marketplace_skill_id=None, source_type="catalog", source_ref=None, version_pin=None, active=True)`
- `uninstall_runtime_group_skill(runtime_group_id, skill_id)`

MFA-enabled accounts must pass a TOTP `otp_code` or a one-time `recovery_code`
to `login(...)`; without one the API rejects the login with `mfa_required`.

`login_with_browser` signs in accounts that have no password (for example
Google sign-in) with an RFC 8628-style device flow. It requests a device
authorization from `/v1/auth/device/authorize` (`scopes` defaults server-side
to `["hubs:read", "clients:write"]`; `client_name` labels the token), prints
`To sign in, visit <verification_uri> and enter the code <user_code>`, and,
when `open_browser` is true, also tries to open the browser at
`verification_uri_complete` (failures to open a browser are ignored). Pass a
`prompt` callable to present the authorization payload yourself instead of the
default print, for example in a GUI. The SDK then polls
`/v1/auth/device/token` at the server-provided interval, adding five seconds
whenever the API answers `slow_down`, until the request is approved. A denied
or expired request raises `ThalovantAPIError`; waiting longer than `timeout`
seconds (default 900) raises `ThalovantTimeoutError`.

On approval `login_with_browser` returns the token payload and stores its
`access_token` on the client exactly like `login(...)`. The token is a
durable, scoped, revocable API token: it appears in the dashboard's API token
list (the payload's `token_id` identifies it, `scopes` and `expires_at`
describe it) and can be revoked there at any time.

`get_operation` returns a typed `OperationResource` whose `status` field is the
`OperationStatus` literal: `requested`, `committed`, `applied`, `ready`,
`failed`, or `timed_out`.

#### Hub provisioning

Every provisioning method below needs a paid plan and a token with the
`hubs:write` scope, except where noted. Both gates surface as
`ThalovantAPIError`: HTTP 402 `API access requires a paid plan.` for the plan
gate, HTTP 403 `Insufficient scopes` for the scope gate. All of them return the
API's JSON body as a `dict`, except `delete_hub`, `delete_runtime_group`, and
`uninstall_runtime_group_skill`, which return `None`.

The scope is checked **before** the plan, so a token lacking `hubs:write` never
reaches the 402. Because free-plan API tokens can only be minted with
`hubs:read`, `clients:read`, and `clients:write`, a free-plan API token can
never carry `hubs:write` and so never sees the 402 at all — every provisioning
call fails with HTTP 403. The 402 is reachable from a dashboard session token
(whose scopes are not plan-capped) or from an API token minted on a paid plan
and kept after a downgrade, since existing tokens retain their scopes.

`hubs:read` implies `hubs:inspect` and `hubs:preview`, so the inspection reads
below work on a free-plan API token.

Payload arguments accept the API's snake_case field names and their camelCase
spellings, which are converted before the request is sent.

- `create_hub(payload, idempotency_key=None)` — `POST /v1/hubs`, HTTP 201.
  `payload` requires `name` and `spec`; `slug`, `namespace`,
  `runtime_group_id`, `domain`, `active`, `visibility`, `capacity_profile`,
  and `owner_id` are optional. `spec` is schema-validated and **requires a
  non-empty `version` string** — omitting it fails with HTTP 422
  `Schema validation failed`, it is not defaulted. An `Idempotency-Key` header
  is always sent (generated when not supplied), so a retried create returns
  the original hub.
- `update_hub(hub_id, payload, etag=...)` — `PATCH /v1/hubs/{hub_id}`. The
  route enforces optimistic locking, so `etag` is required and sent as
  `If-Match`; a stale or missing value fails with HTTP 412 `ETag mismatch` and
  changes nothing. Read the hub again with `get_hub` and retry with the fresh
  `etag`. The prior read is mandatory and must be a **body** read: the
  validator exists only as the `etag` field of the hub resource, and the API
  emits **no `ETag` response header**.
  `slug`, `active`, `visibility`, `capacity_profile`, `runtime_group_id`, and
  `spec` are patchable; `is_locked` is admin-only. `name`, `namespace`, and
  `domain` are **immutable after creation**: a *different* value fails with
  HTTP 400 `<Field> cannot be changed after hub creation`, while a value equal
  to the stored one (or `None`) is accepted and dropped from the patch. Send
  only the fields you mean to change instead of round-tripping a whole hub
  resource. The SDK forwards these fields rather than refusing them locally —
  it cannot know the stored values without another read, and refusing them
  would reject patches the API accepts.
- `delete_hub(hub_id, etag=...)` — `DELETE /v1/hubs/{hub_id}`, HTTP 204. Also
  requires `etag` as `If-Match`, from the same body field. Deletes the hub's
  clients and ACLs with it.
- `release_hub(hub_id, ...)` — `POST /v1/hubs/{hub_id}/release`. Applies a
  release policy and returns the updated hub. Omitted options fall back to the
  workspace release policy; passing `images` implies `custom` mode unless
  `mode` is given.
- `set_hub_rating(hub_id, rating)` / `clear_hub_rating(hub_id)` —
  `PUT`/`DELETE /v1/hubs/{hub_id}/rating`, both returning the updated hub.
  These need the `hubs:write` scope but **no paid plan**. Only public hubs can
  be rated, and owners cannot rate their own hub.
- `get_hub_runtime_capabilities(hub_id)` —
  `GET /v1/hubs/{hub_id}/runtime-capabilities`. Needs the `hubs:inspect` scope
  instead of `hubs:write`, and no paid plan. Returns skills, intents, and a
  `counts` summary. **Branch on the envelope's `source`**: `ovos-runtime` is a
  live reading from a connected client (the only one the API caches), while
  `ovos-runtime-unavailable` and `ovos-runtime-timeout` mean no client
  answered and the API fell back to the hub's runtime group snapshot — its
  desired skills merged with the last observed inventory — and still returned
  HTTP 200 with **stale** data. HTTP 409 comes back only when that fallback
  has nothing to serve either: the hub is attached to no runtime group, or the
  group has no desired and no observed skills. The route is rate limited per
  caller and hub; HTTP 429 carries a `Retry-After` header in seconds.

#### Runtime groups and skills

- `list_runtime_groups(owner_id=None)` and `get_runtime_group(runtime_group_id)`
  — `GET /v1/runtime-groups[/{id}]`. Read-only, needing only `hubs:read`.
  `owner_id` is **enforced**: a non-admin passing another tenant's id gets HTTP
  403 `Ownership required` (members of that tenant are allowed). This differs
  from `list_marketplace_skills`, whose `owner_id` is silently overridden for
  non-admins. An admin omitting `owner_id` lists every tenant's groups.
- `create_runtime_group(payload)` — `POST /v1/runtime-groups`, HTTP 201.
  `payload` requires `name`; `description`, `environment`, `owner_id`, and
  `clone_from_default` are optional. `clone_from_default` seeds the new group
  from the workspace default.
- `update_runtime_group(runtime_group_id, payload)` —
  `PATCH /v1/runtime-groups/{id}`, taking `name`, `description`, and a `spec`
  patch of `replicas` and container `resources`. No `If-Match` is used.
- `get_runtime_group_config(runtime_group_id)` (needs only `hubs:read`) and
  `update_runtime_group_config(runtime_group_id, config, personas=None)` —
  `GET`/`PATCH /v1/runtime-groups/{id}/config`. The update **merges** `config`
  into the stored configuration rather than replacing it, and marks the group
  pending for the runtime operator. `personas` is replaced only when passed.
- `release_runtime_group(runtime_group_id, ...)` —
  `POST /v1/runtime-groups/{id}/release`, with the same options as
  `release_hub`.
- `delete_runtime_group(runtime_group_id)` — `DELETE /v1/runtime-groups/{id}`,
  HTTP 204. The API answers HTTP 409 for the default group and for a group
  that still has hubs attached.
- `install_runtime_group_skill(runtime_group_id, skill_id, ...)` —
  `POST /v1/runtime-groups/{id}/skills`, HTTP **200** (not 201): the route
  upserts, so installing a skill that is already present updates that entry in
  place. `source_type` is a **free-form string of 1–32 characters, not an
  enum**; only two values are interpreted specially. The default `"catalog"`
  resolves the skill against the marketplace and fails with HTTP 404
  `Marketplace skill not found.`; `"git"` requires `source_ref` to be a valid
  repository URL (HTTP 422 otherwise). Any other value is lower-cased,
  stripped, and stored as given, with `source_ref` defaulting to `skill_id`.
  A deactivated catalog entry answers HTTP 409.

  Two distinct HTTP 402s exist on this route: the plan-level API gate
  (`API access requires a paid plan.`) and a per-skill marketplace check
  (`This skill requires paid marketplace access for the tenant plan.`) that
  fires when the catalog entry's `access_tier` is `paid` and the tenant plan
  lacks marketplace access. A paid plan clears the first and can still fail the
  second, so read `installable` / `purchase_required` from
  `list_runtime_group_marketplace` before installing.
- `uninstall_runtime_group_skill(runtime_group_id, skill_id)` —
  `DELETE /v1/runtime-groups/{id}/skills/{skill_id}`, HTTP 204.

#### Skill discovery

None of the discovery reads are paid-gated — a free-plan token can browse the
catalog and inspect a runtime group before upgrading; only the install behind
`install_runtime_group_skill` needs a paid plan. All three return the API's JSON
body as a `dict`, and omit query parameters that are `None`, blank, or `False`.

- `list_marketplace_skills(owner_id=None, include_inactive=False, force_refresh=False)`
  — `GET /v1/marketplace/skills`, scope `hubs:read`, **no paid plan**. Returns
  `{"data": [...]}` with the global catalog plus the caller's tenant entries.
  Each entry carries the install inputs (`skill_id`, `source_type`,
  `source_ref`, `package_name`, `compatibility`, `config_schema`,
  `secret_schema`) and the presentation and access fields (`title`, `summary`,
  `category`, `tags`, `verified`, `support_level`, `access_tier`,
  `billing_sku`, `rating_average`, `is_active`). `owner_id` and
  `include_inactive` apply to admin tokens only — the API **silently** scopes a
  non-admin caller to their own tenant and to active entries regardless of what
  is sent, rather than raising (contrast `list_runtime_groups`, which answers
  HTTP 403). `force_refresh=True` re-syncs the global catalog from source
  before answering.
- `list_runtime_group_marketplace(runtime_group_id, refresh_inventory=False)` —
  `GET /v1/runtime-groups/{id}/marketplace`, scope `hubs:inspect`, **no paid
  plan**. Resolves the catalog against one runtime group: every entry adds the
  group's desired state (`active`, `version_pin`, `source_type`), the observed
  state (`observed_source`, `observed_at`, `adapt_intents`,
  `padatious_intents`, `total_intents`), operator fields
  (`operator_phase`, `operator_message`, `operator_last_error`,
  `operator_retry_count`), and the plan verdict (`purchase_required`,
  `installable`, `access_message`). The envelope carries `runtime_group_id`,
  `observed_at`, `source`, `operator_phase`, and `operator_message`.
  `refresh_inventory=True` forces a live operator read instead of the cached
  snapshot.

  `data` is catalog-driven — the catalog unioned with the group's desired and
  observed skills — so it stays populated even when nothing is reporting.
  `source` describes only the *observation*, so an empty-sounding `source` does
  not imply an empty `data`. On a default (non-refreshing) read `source` is
  `runtime-group-cache`, `runtime-group-cache-empty` (unique to this route), or
  `ovos-runtime-operator` when the operator's published status differed from
  the stored snapshot and the API re-synced while serving.
  `ovos-runtime-operator-pending` appears here **only** with
  `refresh_inventory=True`.
- `list_runtime_group_inventory(runtime_group_id, refresh=False)` —
  `GET /v1/runtime-groups/{id}/inventory`, scope `hubs:inspect`, **no paid
  plan**. Reports what the group is observed running, not what could be
  installed: each entry has `skill_id`, `version`, `source`, `active`,
  `adapt_intents`, `padatious_intents`, `total_intents`, and `observed_at`.
  The envelope's `source` names the provenance —
  `ovos-runtime-operator`, `runtime-group-cache`, or
  `ovos-runtime-operator-pending`, never `runtime-group-cache-empty`.
  `refresh=True` forces a live operator read; the API also refreshes on its own
  when it holds no cached snapshot. When nothing is reporting this route
  returns an empty `data` list with `source="ovos-runtime-operator-pending"`
  rather than failing.

Both group-scoped reads answer HTTP 404 for an unknown runtime group and HTTP
403 `Ownership required` when the caller neither owns the group nor is an
admin.

`create_client_identity` generates client secrets locally, sends them once to
the API, and returns `BootstrapIdentityResult.identity`. API responses may
contain only Vault-backed secret references on normal resources. One-time
identity responses include MQTT broker credentials on `identity.mqtt` when the
hub exposes MQTT.

### `AsyncThalovantClient`

Async wrapper for asyncio applications. It mirrors the synchronous client with
`async` methods where the operation can block.

## Conversations

### `ThalovantConversation`

Created with `client.conversation(...)`.

Methods:

- `ask(...)`
- `send_utterance(...)`
- `send_action(...)`
- `send_code(...)`
- `emit(...)`
- `on(...)`
- `wait_for_event(...)`
- `listen(...)`

The conversation owns a stable `session_id` and default `lang`.

### `AsyncThalovantConversation`

Created with `async_client.conversation(...)`. It mirrors the synchronous
conversation with async methods.

## Agents

### `ThalovantAgent`

Long-running synchronous runner.

Constructors:

- `ThalovantAgent(identity, **client_options)`
- `ThalovantAgent.from_identity_file(path, **client_options)`
- `ThalovantAgent.from_env(**client_options)`

Methods:

- `on(event_name, handler=None)`
- `on_speak(handler=None)`
- `run_forever(poll_interval=0.25)`
- `stop()`
- `close()`
- `ask(...)`
- `send_utterance(...)`
- `conversation(...)`

Use `agent.on(event_name)` as a decorator before `run_forever`.

### `AsyncThalovantAgent`

Asyncio runner with async `run_forever`, `ask`, `send_utterance`, and `close`.

## Data Models

### `ThalovantEvent`

Fields:

- `name`
- `data`
- `context`
- `raw`

Helpers:

- `text`
- `display_text`
- `utterances`
- `session_id`
- `site_id`
- `request_id`
- `lang`
- `is_failure`
- `is_policy_denied`
- `matches_context(...)`
- `as_dict()`
- `rich_media`
- `display_items(max_text_chars=None)`

### `HubIntentInventory`

What a hub can be asked, from `ThalovantClient.intents()`.

- `languages: tuple[str, ...]` — the languages asked for
- `skills: tuple[HubSkillIntents, ...]` — each with `skill_id`, `intents`, `languages`
- `intents: tuple[HubIntent, ...]` — every intent across skills
- `source: str` — `intent-manifest` (sentences per language) or `engine-manifests` (names only)
- `denied: tuple[str, ...]` — the query the hub refused when `source` is the fallback
- `has_phrases: bool`
- `as_dict() -> dict`

A `HubIntent` has `skill_id`, `name`, `id` (`skill_id:name`), `engine`
(`padatious` or `adapt`), `enabled`, `phrases: dict[str, tuple[str, ...]]` keyed
by language, `phrases_for(lang)`, `languages`, and `examples(lang=None, limit=2)`,
which prefers whole sentences over ones with a slot.

`IntentRegistration` is one row of the manifest (`skill_id`, `intent_name`,
`lang`, `method`, `enabled`, `session_id`, optional `definition`).
`IntentDefinition` is one registration as the skill made it (`skill_id`,
`intent_name`, `lang`, `method`, `samples`, `raw`).

### `ThalovantReply`

Fields and helpers:

- `text`
- `display_text`
- `utterances`
- `handled`
- `ok`
- `session_id`
- `request_id`
- `events`
- `failure_event`
- `as_dict()`
- `display_items(max_text_chars=None)`

### `ThalovantDisplayItem`

UI-friendly output item:

- `kind`
- `text`
- `data`
- `title`
- `payload`
- `url`
- `silent`
- `as_dict()`

## Context Helpers

- `build_client_context(...)`

Builds generic user/auth/device/channel/platform metadata for web, mobile,
kiosk, service, and enterprise clients.

### `ThalovantHealth`

Transport health snapshot:

- `connected`
- `handshake_complete`
- `transport_alive`
- `last_error`
- `ok`
- `as_dict()`

### `ThalovantDoctorReport`

Diagnostic report:

- `identity`
- `checks`
- `ok`
- `as_dict()`
- `format()`

## Event Constants

- `EVENT_RECOGNIZER_LOOP_UTTERANCE`
- `EVENT_SPEAK`
- `EVENT_UTTERANCE_HANDLED`
- `EVENT_INTENT_FAILURE`
- `EVENT_POLICY_DENIED`

## Exceptions

- `ThalovantError`
- `ThalovantAPIError`
- `ThalovantIdentityError`
- `ThalovantConnectionError`
- `ThalovantTimeoutError`
- `ThalovantRuntimeError`
- `ThalovantUnsupportedProtocolError`
