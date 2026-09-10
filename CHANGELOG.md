# Changelog

## 0.5.14

- Compare validated Noise pins by their hexadecimal key value, accepting uppercase and lowercase spellings of the same authenticated server key. Reconfirming an existing pin preserves the identity file unchanged; a different key still fails closed.
- Add real TLS/Noise regressions for uppercase pinned KK reconnect, encrypted replies, and rejection of an authenticated replacement key without changing saved trust.

## 0.5.13

- Require an actual definition before ignoring partial describe timeouts within or across batches. Fully answered empty/unknown-intent responses remain successful.

- Preserve an initial listener setup failure when the deadline retires its subscription first; timeout no longer races into a clean end-of-stream. Caller cancellation and normal post-setup expiry retain their behavior.

- Strip normalized legacy crypto-key fields from bootstrap requests before an API error can echo them; retain reference fields and unrelated spec metadata.

- Preserve describe-batch timeouts when earlier replies contain no usable definitions. Unknown individual descriptions still return an empty result.
- Normalize secret-bearing field names in default bootstrap output, preserving explicit persistence and reference fields.
- Reject overlapping Ask calls with the same request ID and overlapping Query calls with the same query ID on one client before either can mix replies. Reservations retire with their collectors; distinct generated IDs remain the default.
- Correct hub-create retry guidance, example ordering, runtime-config concurrency limitations, and historical release notes found in older CodeRabbit reviews.

## 0.5.12

- Accept the upstream HTTP disconnect endpoint's exact `{"error": "Already Disconnected"}` acknowledgment so an explicit close retry can finish after successful remote cleanup lost its response. Other errors, contradictory replies, and non-success HTTP statuses still retain admission.
- Add a real TLS/Noise lost-response retry regression, successful idempotent close and XX-to-KK reconnect checks, and endpoint-specific rejection tests.

## 0.5.11

- Require a positive HTTP disconnect acknowledgment before releasing admission. Preserve the failed session and replica cookie for explicit close retry; close and wait_closed report failures, and reconnect stays blocked until cleanup succeeds.
- Serialize HTTP admission publication against cancellation and close, preserving remote-session ownership when a connect response arrives late. Retain the primary connection failure while exposing cleanup errors separately; successful WSS/MQTT cleanup remains idempotent.
- Sanitize HTTP request exceptions and refuse to copy arbitrary server error bodies into diagnostics. Add real TLS/Noise regressions for refused, empty, malformed and contradictory acknowledgments, pending admission, concurrent cleanup, sync/async failure observation, explicit retry and XX-to-KK reconnect.

## 0.5.10

- Preserve a healthy pending write after a reply completes; lifecycle ownership and the original send deadline still prevent premature reuse.

- Apply one deadline to direct query connection, authenticated readiness, send, and reply collection; completed queries return immediately. Expired raw I/O retains lifecycle ownership until cleanup finishes; delayed connections cannot register handlers or send after timeout.
- Treat intent misses as provisional until query completion, allowing later fallback speech to recover. Freeze replies on completion or hard policy/query-timeout failures, preserve failed partial replies, and ignore subsequent events or write errors.
- Exercise both routed cascade and direct query replies, query correlation, blocked connection/write cleanup, invalid budgets, and deadline-capped settling with synthetic transport regressions.
- Apply the caller deadline to ask connection/send/reconnect/settling and event wait/listen connection/registration. Async ask/query/wait/listen cancellation removes handlers and retires owned work without closing another queued caller's session.
- Bound sync/async listener buffers with `max_buffered_events=256` by default; overflow raises `ThalovantRuntimeError` and retires the subscription. Keep transport predicate polling off the waiting caller thread.
- Align Ask's delayed fallback behavior: first speech starts a fixed 250ms settle window; first handled/soft miss without speech starts a fixed 5s empty-reply window. Windows are configurable on the client and clipped to the caller deadline; no empty success or post-hard-failure recovery is allowed.
- Retire listeners at their deadline even while the consumer is paused. Reconnect only during pre-publication preparation; never automatically replay a request after an application write starts.
- Return the first accepted nonblank runtime session ID from both Ask and Query, falling back to the requested session.

## 0.5.9

- Reject control-plane redirects before parsing a response or forwarding password-login bodies. Require HTTPS for credentials except explicit loopback HTTP used in local development; reject credentials embedded in API URLs.
- Suppress credential-bearing Requests exception chains and verify redirect rejection with real HTTP servers for bearer and password requests.

## 0.5.8

- Treat only `None` as the default connect/close budget; invalid explicit timeouts expire immediately before lifecycle changes. Disable persisted checkout credentials in CI.

- Make authenticated readiness part of one caller connection deadline. An unready session now raises `ThalovantConnectionError`; 0.5.7's extra best-effort settle allowance is removed.
- Return promptly on connect timeout or async connect cancellation while retaining ownership of unfinished connect/cleanup work. Replacement sessions wait within their own budget, concurrent connects share one admitted session, and late completion is retired before reconnect. Close also has a caller deadline; `wait_closed()` observes the actual retained cleanup.
- Include reconnect and send in each intent query deadline; a blocked optional fallback probe retains send/cleanup ownership until retired. Explicit failed fallback responses remain unknown.
- Preserve typed timeouts when Requests raises a socket/TLS timeout at the HTTPS handshake deadline, with admission cleanup before reconnect.
- Match policy denials to the querying request and reject intent descriptions carrying a different request ID, while preserving compatibility with ID-less replies.
- Add regressions for hung cleanup, late completion, concurrent connects, cancellation, and cross-request discovery events across the existing Python 3.10–3.14 CI matrix, plus a runtime dependency audit.

## 0.5.7

- `connect()` waits for the transport to admit the session it just built.
  It could return before `is_connected()` was true, and every operation runs
  through `_with_reconnect`, which calls `connect()` first: on that race the
  client closed a working session and dialled a second one. A hub that admits
  one session per identity refused the second, and the caller saw
  `HiveMind WSS handshake timed out` for a query that was never sent. The
  wait is bounded and quiet -- a predicate that never settles leaves the
  connection alone and lets the operation report the fault.
- `intents()` falls back to the engines' manifests when the hub never answers
  `ovos.intent.list`, not only when it refuses it. A connection allowed to
  publish the query still gets nothing from a runtime that does not implement
  it, and the caller cannot tell silence from refusal: both leave an empty
  listing, and the manifests answer either way. `source` says
  `engine-manifests` and `denied` names the query, as it already did for a
  refusal. `fallback=False` still raises, and so does a hub whose engines are
  silent too.

## 0.5.6

- Reject malformed Noise pin containers and values with `ThalovantConnectionError`, preserving the existing trust file.
- Preserve `ThalovantTimeoutError` when HTTPS Noise negotiation expires, after releasing the failed connection and admission.

## 0.5.5

- Implement HiveMind v3 Noise on HTTPS and MQTT using the published handshake primitives. HTTPS retains replica affinity and uses binary encrypted send/poll endpoints; MQTT carries raw Noise frames after admission. Both cipher suites and XXpsk2/KKpsk0 are supported.
- Verify HTTPS/WSS server certificates by default. Explicit `self_signed=True` on a transport remains available for intentionally configured development environments.
- Add `noise_state_dir` to the client and all transports. Preserve existing HiveMind identity/key paths by default; write private identity state atomically and retain server pins after authentication failures.
- Reset session keys and readiness on disconnect, reject unauthenticated application traffic, serialize chunked sends, and increase the default Noise handshake budget to 20 seconds.
- Add real TLS HTTP and threaded MQTT encrypted request/reply, large-message concurrency, reconnect, invalid-offer, wrong-password, pin and certificate regressions.

## 0.5.3

- `client.intents("en-us")` asks for one language again. A `str` satisfies the
  `Iterable[str]` hint, so a bare tag went through `list()` and became
  `["e", "n", "-", "u", "s"]`: the client sent five nonsense manifest queries
  and built an inventory from the answers to none of them. Both the sync and
  async methods take the same path.
- A fallback row whose `priority` is `NaN` or infinity no longer aborts intent
  discovery. Both are floats, and `int()` raises `ValueError` and
  `OverflowError` on them, so one malformed row took the whole inventory with
  it. The row is skipped and the rest are kept. A large but perfectly finite
  integer priority is kept too: `math.isfinite()` raises `OverflowError`
  converting one to a float, so guarding with it alone would have swapped one
  crash for another.
- Discovering fallbacks no longer costs the caller's whole timeout. An
  ovos-core without `ovos.skills.fallback.list` never answers it, and
  `inventory()` asks on every call, so each listing against an older hub waited
  out the full budget only to report the answer as unknowable. The probe now
  has its own short bound and is never longer than the caller asked for.

## 0.5.0

- **Breaking.** `ThalovantIdentity.crypto_key` is gone. Hubs stopped issuing a
  crypto key with HiveMind protocol v3, which derives its Noise pre-shared key
  from the client `password`, so the field named a credential that no longer
  exists. A `crypto_key` in an older identity file, config profile or API
  payload is still accepted and ignored, and both spellings stay in the
  bootstrap redaction set so an older stored payload carrying one cannot be
  logged. `create_client_identity` no longer mints one.
- **Breaking.** The WSS transport no longer installs a protocol subclass that
  short-circuits the pre-shared handshake. That override is what stopped this
  SDK reaching a HiveMind-core 5.x hub, which accepts only the v3 Noise
  handshake and closes anything else with `1008`; the bus client performs it
  from the identity password.
- The MQTT transport clears its session key, password handshake and handshake
  event at the start of each connection attempt. The key is now derived per
  connection from the password handshake rather than read from the identity, so
  carrying it into a reconnect encrypted the next hello with the previous
  session's key and the broker could not read it.
- **Breaking.** The HTTPS transport refuses a hub endpoint that is not
  `https://`. Removing the crypto key took the separate payload cipher with it,
  so TLS is the only confidentiality left on that hop, and the access key
  travels in the `authorization` query.
- `create_client_identity` drops `cryptoKey` and `crypto_key` from a
  caller-supplied `spec` rather than passing them through. The error redaction
  covers only what the SDK mints, so a legacy value left in by a caller could
  otherwise be echoed back inside an API error.
- The HTTPS transport no longer wraps outgoing messages in the crypto-key JSON
  envelope, and the MQTT transport no longer seeds its cipher from the identity.
  MQTT still derives a key from the password handshake where the hub offers one,
  and TLS protects both paths.
- `_runtime_crypto_key` is removed. It truncated an identity crypto key to the
  HiveMind runtime key size, and nothing derives a key from an identity field
  any more.

## 0.4.42

- Documentation: `ovos.intent.describe` is required only when the client has to ask for definitions separately. A runtime that honours `include_definitions` attaches them to `ovos.intent.list` and no describe is sent, so the permission is not needed at all there — which is about to be the common case. The previous wording said the permission was needed whenever `describe=True`. Reported by the Rust port's review.

## 0.4.41

- `ThalovantPolicyDeniedError.allowed` drops blank entries and trims the rest, alongside the non-string entries 0.4.40 already dropped. An empty string is no more a message type than `3` is, and printing one gives an operator a blank line among the types to allow. Settled with the .NET port, which had it right first.

## 0.4.40

- `list_intents` raises `ThalovantRuntimeError` when the hub answers `ovos.intent.list` with `ok: false`, instead of reading the missing `intents` key as an empty list. A refused listing is not an empty hub, and reporting it as no intents showed a person a device that can do nothing. `describe_intent` keeps returning an empty list for `ok: false`, which is a real answer: the hub does not know that registration. Reported by the Kotlin port's review.
- `ThalovantPolicyDeniedError.allowed` keeps only string entries. A number or a null in the hub's `allowed` list is not a message type, and stringifying one put `"3"` or `"None"` in front of an operator reading which types to allow. Reported by the Kotlin port's review.

## 0.4.39

- Keep partial results across describe batches. 0.4.38 sends describes in windows of 32, and a window that received no reply at all raised, discarding every earlier window's definitions. Windows are contiguous slices of the work, so a skill that stops answering can own a whole window: an unresponsive skill with more than 32 intents turned the entire inventory into a timeout, while the same skill with fewer intents only lost its sentences. A window with nothing now contributes nothing, and the call fails only when no window produced a definition — so a hub silent from the start still fails at the first window rather than after all of them. Reported by the Rust port's review.

## 0.4.38

- Send describes in batches of at most 32 (`intents.DESCRIBE_BATCH`), each batch its own subscription window, instead of putting every request in flight at once. A hub with 69 intents in two languages is 138 requests and, with every reply delivered twice, 276 inbound events; an SDK whose reply queue is bounded — the Rust port's bus channel holds 64 — dropped replies past its capacity and returned an inventory missing sentences. Reported by the Rust port's review. `describe_many(batch=0)` restores the old behaviour, and the per-batch deadline means a hub that answers nothing now fails after one batch rather than holding every request open.

## 0.4.37

- `HubIntentInventory.has_phrases` is true only when at least one intent carries at least one sentence; a manifest-path inventory whose describes all came back empty no longer reads as having phrases. Ports (Node, Swift, .NET) had already chosen this reading; the reference now matches them.
- `intents(languages)` trims each tag and drops a repeat of a language already asked for under another spelling (`en-us`, `en-US`, `en_us` are one language), so the hub is asked once per language. Reported by the Node port's review.
- An intent registered under both engines in one language keeps the template row's sentences; the keyword row, which carries none, no longer overwrites them. On the names-only fallback the first engine to name an intent decides its `engine`, as on the manifest path. Both reported by the Go port's review.

## 0.4.36

- Add the intent inventory: `ThalovantClient.intents(languages)` reads the hub runtime's intent manifest (OVOS-INTENT-4 §10) over the client's own session and returns a `HubIntentInventory` — every intent each skill registered, per language, with the sentences a person says to reach it as the skill's locale files wrote them, `{slot}` placeholders included. No control-plane credential is involved. `list_intents(lang)` and `describe_intent(skill_id, intent_name, lang)` expose the two underlying queries (`ovos.intent.list` / `ovos.intent.describe`); `AsyncThalovantClient` mirrors all three; the CLI gains `thalovant intents`.
- Queries are correlated by `context.request_id` like every other request, and a reply delivered more than once is taken once. Describes are sent together and matched by request id, or by the definition's own `skill_id`/`intent_name`/`lang` for a hub that does not echo the id.
- Add `ThalovantPolicyDeniedError` (a `ThalovantRuntimeError`), raised at once from the hub's `hive.policy.denied` with `denied_type`, `code`, `reason` and the `allowed` list, instead of waiting for a timeout. `intents(fallback=True)`, the default, falls back to the engines' own manifests (`intent.service.adapt.manifest.get` / `intent.service.padatious.manifest.get`) when `ovos.intent.list` is refused; the result then carries names only and `source="engine-manifests"`.
- A runtime that attaches each row's `definition` to `ovos.intent.list` when asked with `include_definitions` is used as such; one that does not is described row by row.

## Unreleased

Security hardening, plus a HiveMind MQTT data-plane topic migration. The security fixes below change no wire-protocol or identity-file behavior: API request bodies, `as_dict(include_secrets=True)`, and identity-file round-trips still carry the real secret values.

- **BREAKING**: migrate the HiveMind MQTT data-plane to the live topic scheme. The client now derives its three channels by appending a suffix to the credential's `topic_prefix` (the full plaintext base, including the client segment): publish requests to `<topic_prefix>/in`, subscribe for replies on `<topic_prefix>/out`, and retained presence / LWT on `<topic_prefix>/status`. This replaces the dead `c2s`/`s2c` derivation, including the access-key/`hub_id` topic construction and the optional SHA-256 topic hashing. `mqtt_topics_for_identity` now requires a non-empty `topic_prefix` and raises `ThalovantConnectionError` otherwise; it also normalizes the prefix before deriving the three channels — stripping surrounding whitespace and `/` characters, so a configured `/prefix/` yields `prefix/in` rather than `//in` — and rejects any prefix containing an MQTT wildcard (`#` or `+`) or a control character (`ord(c) < 0x20`, including NUL) with a `ThalovantConnectionError`. `MqttTopicSet` renames its `c2s`/`s2c` fields to `inbound`/`outbound` (`status` unchanged). `MqttBrokerCredentials` is trimmed to `{endpoint, username, password, topic_prefix, qos, tls}`; the `hub_id`, `c2s_topic`, `s2c_topic`, `status_topic`, and `hash_topics` fields — with their JSON/env parsing and serialization — are removed.
- **BREAKING**: remove the `admin` parameter (and the `owner_id` parameter it gated) from `ThalovantControlPlane.get_analytics_overview`, along with the `GET /v1/admin/analytics/overview` branch. This SDK serves non-admin Thalovant customers, for whom the admin route can only answer HTTP 403. The method now always calls `GET /v1/analytics/overview`; calls passing `admin=` or `owner_id=` now raise `TypeError`.
- Redact the `client` resource in `BootstrapIdentityResult.as_dict()` (the default `include_secrets=False`). The raw `POST /v1/clients` response carries every credential twice — the echoed request `spec` (`apiKey`/`password`/`cryptoKey`) and the `initial_identify` block plus `initial_identify_token` — and was returned unredacted, so the "safe" default output leaked the full identity. The default now deep-drops those keys (references such as `apiKeyRef` are kept); `as_dict(include_secrets=True)` still returns everything unchanged, including the raw client resource.
- Hide secret material from `repr()`/`str()` of `ThalovantIdentity` (`access_key`, `password`, `crypto_key`), `MqttBrokerCredentials` (username, password, and the topic fields, which can embed the access key), and `BootstrapIdentityResult` (the raw `client` response). Serialization is unaffected.
- Stop `HubDataPlaneEndpoints.from_mapping` from stringifying an `mqtt` credentials block into the endpoint map. An identity carrying `mqtt` credentials without an explicit `data_plane_endpoints` key leaked the broker username and password through the **default** `identity.as_dict()` and through `repr()`. The endpoint map now takes only the broker URL, so `identity.endpoint_for("mqtt")` returns that URL instead of a stringified credentials dict.
- Strip URL query strings from transport error text before it is stored in `ThalovantConnectionInfo.last_error` / `ThalovantHealth.last_error` or embedded in raised connection errors. Connection failures embed the request URL, whose query carries the data-plane access key (`?authorization=base64(<userAgent>:<accessKey>)`), and `thalovant health` printed it. `Transport.last_error()` still returns the raw exception object. This also covers the response-derived errors *raised* to the caller — `HiveMindHTTPTransport.connect()`, `_raise_for_emit_response()`, and the `doctor()` check detail — not just the stored `last_error`.
- Redact URL userinfo from an MQTT broker `endpoint` in the default (`include_secrets=False`) `MqttBrokerCredentials.as_dict()` and in `repr()`, and from `HubDataPlaneEndpoints` `repr()`. An endpoint of the form `mqtts://user:pass@host` previously showed its `user:pass@` credentials through the default, log-safe serializer and through `repr()`; `include_secrets=True` still returns the full endpoint for the wire and persistence paths.
- Redact secret-keyed entries (`password`, `token`, `api_key`, …) from the free-form `identity.metadata` map in the default `as_dict()` and keep `metadata` out of `repr()`. The map's keys are caller/API-controlled, so a secret-named entry could otherwise leak through the log-safe default; `as_dict(include_secrets=True)` returns metadata verbatim.
- Bound control-plane error messages. `ThalovantAPIError` messages for HTTP error responses no longer include the raw response body — which can echo the request back (for `POST /v1/clients` the request carries freshly generated credentials) and is attacker-sized — and instead carry the HTTP status plus a newline-collapsed server `detail`/`message`/`error` string truncated to 200 characters.
- README: state that `result.as_dict()` (the default) is safe to log and that `result.as_dict(include_secrets=True)` must never be logged. The old note implied the default path was already safe, which it was not before this release.

- Stop a closed WSS bus client from reconnecting forever. A failed `connect()` — or a `close()`/`disconnect()` while the underlying `ovos_bus_client` was in its reconnect backoff — left its `run_forever` thread reconnecting indefinitely, and once the hub was reachable each orphan re-completed HELLO/HANDSHAKE on the same access key and stayed connected for the process life. `_shutdown_wss_client` now sets a `thalovant_closed` flag that suppresses the reconnect (`on_error` no longer sleeps-and-reconnects, and `create_client` raises `WebSocketException` to end the base retry loop), so a thread already asleep in the backoff also stops (#26).

- Wake a WSS bus client's `run_forever` thread when the transport closes it after a hung handshake. When a hub accepts the socket and never completes the WebSocket handshake, the thread blocks in the handshake `recv()`; `close()` closes the file descriptor but does not wake a thread already blocked in `recv()`, so each such failed `connect()` parked one thread and one socket for the process life. `_shutdown_wss_client` now `shutdown(SHUT_RDWR)`s the raw socket before `close()`, which wakes the blocked read (#28, follow-up to #26).

## 0.4.26

- Correct the provisioning contract documentation. 0.4.25 was the reference implementation for six ports, and each port re-verified it against the API; the corrections below were confirmed against the API source before being written here. No runtime logic or method signature changes; the user-agent version advances to 0.4.26.
- `get_hub_runtime_capabilities` does not simply answer HTTP 409 when no client is connected. It first falls back to the hub's runtime-group snapshot (desired skills merged with the last observed inventory) and returns HTTP 200 with `source` set to `ovos-runtime-unavailable` or `ovos-runtime-timeout`, which marks the data stale; only `ovos-runtime` is a live reading. HTTP 409 is answered only when there is no snapshot at all — no runtime group, or a group with no desired and no observed skills. Callers must branch on `source` rather than treating any 200 as live. The route is also rate limited: HTTP 429 carries `Retry-After`.
- Document the marketplace and inventory `source` vocabularies separately, since they differ. A default (non-refreshing) `list_runtime_group_marketplace` returns `runtime-group-cache`, `runtime-group-cache-empty` (unique to that route), or `ovos-runtime-operator`; `ovos-runtime-operator-pending` appears there only with `refresh_inventory=True`, and is otherwise an inventory-route value. `list_runtime_group_inventory` never returns `runtime-group-cache-empty`.
- Note that the marketplace route's `data` is catalog-driven — the catalog unioned with the group's desired and observed skills — so it stays populated when nothing is reporting. Its `source` describes only the observation, so an empty-sounding `source` must not be read as an empty `data`.
- `install_runtime_group_skill` returns HTTP 200, not 201; the route upserts, so a repeat install updates the entry in place.
- `source_type` on skill install is a free-form string of 1–32 characters, not an enum of `catalog` and `git`. Only those two values are interpreted specially; any other value is stripped, lower-cased, and stored in that normalized form.
- Skill install has two distinct HTTP 402s: the plan-level API gate (`API access requires a paid plan.`) and a per-skill marketplace check (`This skill requires paid marketplace access for the tenant plan.`) on catalog entries whose `access_tier` is `paid`. A paid plan clears the first and can still fail the second.
- Document that `name`, `namespace`, and `domain` are immutable on `PATCH /v1/hubs/{id}`: a changed value fails with HTTP 400, while a value equal to the stored one is accepted and dropped. `update_hub` deliberately does **not** reject them client-side (some ports do) — the SDK cannot know the stored values without a second read, and refusing them outright would reject patches the API accepts. Patch only the fields you mean to change.
- Document that the `etag` for `update_hub` and `delete_hub` comes only from the `etag` **body** field of the hub resource. The API emits no `ETag` response header, so a prior `get_hub` is mandatory and there is nothing to read off the response headers.
- Document that the scope gate precedes the plan gate, which changes what a free-tier user actually sees: free-plan API tokens can only be minted with `hubs:read`, `clients:read`, and `clients:write`, so they can never carry `hubs:write` and therefore never see the HTTP 402 at all — every provisioning call fails with HTTP 403 `Insufficient scopes`. The 402 is reachable only from a dashboard session token or from a token minted on a paid plan and kept after a downgrade. `hubs:read` implies `hubs:inspect`, so the inspection reads do work on a free-plan token.
- Document that `list_runtime_groups`' `owner_id` is enforced (HTTP 403 `Ownership required` for a non-admin passing another tenant's id), unlike `list_marketplace_skills`, whose `owner_id` is silently overridden.
- Document that a hub's `spec` requires a non-empty `version` string (HTTP 422 otherwise) and fix the README's `create_hub` example, which omitted it and could not have succeeded as written.

## 0.4.25

- Add hub provisioning to `ThalovantControlPlane`. The hub surface was read-only, so the `hubs:write` scope the dashboard sells ("Create and update your hubs") had no SDK method that could use it. New: `create_hub`, `update_hub`, `delete_hub`, `release_hub`, `set_hub_rating`, `clear_hub_rating`, and `get_hub_runtime_capabilities`.
- Add runtime group and skill management: `list_runtime_groups`, `get_runtime_group`, `create_runtime_group`, `update_runtime_group`, `get_runtime_group_config`, `update_runtime_group_config`, `release_runtime_group`, `delete_runtime_group`, `install_runtime_group_skill`, and `uninstall_runtime_group_skill`.
- Honor the API's optimistic locking on the hub write routes. `update_hub` and `delete_hub` take a required `etag` keyword and send it as `If-Match`; the API rejects a stale or missing value with HTTP 412 and changes nothing. Runtime group routes do not use `If-Match`. `create_hub` sends an `Idempotency-Key` header, generated unless you pass `idempotency_key`. Retain the key before the first call and reuse it on every retry; omitting it generates a different key per invocation and can create another hub.
- Document the gates these routes sit behind. Everything except the rating, config-read, list/get, and runtime-capabilities methods needs a paid plan and a `hubs:write` token; the ratings need `hubs:write` only; `get_hub_runtime_capabilities` needs `hubs:inspect`; the runtime group reads need `hubs:read`. Both gates surface as the usual `ThalovantAPIError` (HTTP 402 `API access requires a paid plan.`, HTTP 403 `Insufficient scopes`).
- Add skill discovery, which closes the gap left by shipping `install_runtime_group_skill` with no way to learn what is installable. New: `list_marketplace_skills` (`GET /v1/marketplace/skills`), `list_runtime_group_marketplace` (`GET /v1/runtime-groups/{id}/marketplace`), and `list_runtime_group_inventory` (`GET /v1/runtime-groups/{id}/inventory`).
- The discovery reads are deliberately not paid-gated, matching the API: the catalog needs only `hubs:read` and the two group-scoped reads only `hubs:inspect`, so a free-plan token can browse the marketplace and inspect a runtime group before upgrading. Only the install itself needs a paid plan.
- `list_runtime_group_marketplace` resolves the catalog against one group (desired state, observed state, and the `installable` / `purchase_required` plan verdict per entry), while `list_runtime_group_inventory` reports only what the group is observed running. Neither answers HTTP 409 when nothing is reporting, unlike `get_hub_runtime_capabilities`; the inventory route returns an empty list with a pending `source`.
- Add a hub-provisioning walkthrough to the README (runtime group, hub, discover, skill, release) plus a "Discover Skills" section, and document every new method in `docs/api-reference.md`.
- No existing method signature changed.

## 0.4.24

- Fix the stale data-plane user agent. `thalovant.client.DEFAULT_USERAGENT` was pinned at `ThalovantPythonSDK/0.4.19`, so the 0.4.20, 0.4.21, 0.4.22, and 0.4.23 releases all identified themselves as 0.4.19 to hubs. The control-plane user agent was correct but only because it was hand-maintained every release.
- Derive every user agent from a single source of truth. `thalovant/_version.py` now owns `__version__` and builds `USER_AGENT` from it; `client.DEFAULT_USERAGENT` and `control.DEFAULT_CONTROL_USER_AGENT` are both that value, and `thalovant.__version__` re-exports it, so no version literal can drift again. Public names and values are unchanged.
- Add `tests/test_version.py`, which pins each user agent to `__version__`, matches the package version against `pyproject.toml`, and rejects any hard-coded version literal in a user-agent string.
- Document `retry_after_seconds` on the `token_quota_exceeded` bullet in the README. Both API-token 429s carry it, but only `token_rate_limited` said so, which left callers of the quota error without the programmatic retry field.

## 0.4.23

- Document token auth for CI and automation in the README: pass a scoped API token as `ThalovantControlPlane(access_token=os.environ["THALOVANT_API_TOKEN"])` to skip the login call entirely. Tokens come from the dashboard's API Tokens page or from `login_with_browser()`, and are durable, scoped, and revocable. The SDK does not read the environment variable itself, so the example passes it explicitly.
- Document the two API-token 429 responses under Common Issues: `token_rate_limited` for the plan's per-minute request rate (60 per minute on the free plan) and `token_quota_exceeded` for the daily or monthly call quota, which names the `quota`, `limit`, and `used`. Both carry `Retry-After` and `retry_after_seconds`; the SDK does not retry either automatically.

## 0.4.22

- Add `ThalovantControlPlane.login_with_browser()`: RFC 8628-style browser device-flow sign-in for accounts without a password (for example Google sign-in). It prints a short user code and verification URL (or hands the authorization payload to a custom `prompt` callable), optionally opens the browser at `verification_uri_complete`, polls the token endpoint at the server-provided interval (adding five seconds on `slow_down`), and stores the resulting scoped API token on `access_token` exactly like `login()`. Denied and expired requests raise `ThalovantAPIError`; exceeding `timeout` raises `ThalovantTimeoutError`.

## 0.4.21

- Add the `OperationStatus` literal type (`requested`, `committed`, `applied`, `ready`, `failed`, `timed_out`) and use it for `OperationResource.status`; export it from the package root.
- Add optional `otp_code` and `recovery_code` parameters to `ThalovantControlPlane.login()` for MFA-enabled accounts; they are sent only when provided.
- Fix the documented `preferred_protocols` default in the API reference to the actual `("wss", "https", "mqtt")` order.

## 0.4.20

- Add `OperationResource` and `ThalovantControlPlane.get_operation()` for polling durable control-plane commands.
