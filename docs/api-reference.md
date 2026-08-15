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

Payload arguments accept the API's snake_case field names and their camelCase
spellings, which are converted before the request is sent.

- `create_hub(payload, idempotency_key=None)` — `POST /v1/hubs`, HTTP 201.
  `payload` requires `name` and `spec`; `slug`, `namespace`,
  `runtime_group_id`, `domain`, `active`, `visibility`, `capacity_profile`,
  and `owner_id` are optional. An `Idempotency-Key` header is always sent
  (generated when not supplied), so a retried create returns the original hub.
- `update_hub(hub_id, payload, etag=...)` — `PATCH /v1/hubs/{hub_id}`. The
  route enforces optimistic locking, so `etag` is required and sent as
  `If-Match`; a stale or missing value fails with HTTP 412 `ETag mismatch` and
  changes nothing. Read the hub again with `get_hub` and retry with the fresh
  `etag`. `name`, `slug`, `domain`, `active`, `visibility`,
  `capacity_profile`, `runtime_group_id`, and `spec` are patchable;
  `is_locked` is admin-only, and the API rejects changes to immutable fields.
- `delete_hub(hub_id, etag=...)` — `DELETE /v1/hubs/{hub_id}`, HTTP 204. Also
  requires `etag` as `If-Match`. Deletes the hub's clients and ACLs with it.
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
  instead of `hubs:write`, and no paid plan. Returns the live skill and intent
  inventory with a `counts` summary; the API answers HTTP 409 when no
  connected client can report inventory.

#### Runtime groups and skills

- `list_runtime_groups(owner_id=None)` and `get_runtime_group(runtime_group_id)`
  — `GET /v1/runtime-groups[/{id}]`. Read-only, needing only `hubs:read`.
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
  `POST /v1/runtime-groups/{id}/skills`. The default `source_type="catalog"`
  installs a marketplace skill and fails with HTTP 404 when the catalog has no
  such skill; `source_type="git"` requires a `source_ref` repository URL.
  Installing a skill that is already present updates that entry. Paid
  marketplace skills additionally require marketplace access on the tenant
  plan (HTTP 402).
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
  `include_inactive` apply to admin tokens only — the API scopes a non-admin
  caller to their own tenant and to active entries regardless of what is sent.
  `force_refresh=True` re-syncs the global catalog from source before
  answering.
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
- `list_runtime_group_inventory(runtime_group_id, refresh=False)` —
  `GET /v1/runtime-groups/{id}/inventory`, scope `hubs:inspect`, **no paid
  plan**. Reports what the group is observed running, not what could be
  installed: each entry has `skill_id`, `version`, `source`, `active`,
  `adapt_intents`, `padatious_intents`, `total_intents`, and `observed_at`.
  The envelope's `source` names the provenance —
  `ovos-runtime-operator`, `runtime-group-cache`, or
  `ovos-runtime-operator-pending`. `refresh=True` forces a live operator read;
  the API also refreshes on its own when it holds no cached snapshot. Unlike
  `get_hub_runtime_capabilities`, this route does **not** answer HTTP 409 when
  nothing is reporting — it returns an empty `data` list with a pending
  `source`.

Both group-scoped reads answer HTTP 404 for an unknown runtime group and HTTP
403 when the caller neither owns the group nor is an admin.

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
