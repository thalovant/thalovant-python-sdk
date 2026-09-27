# Async core: plan

Branch `feat/async-core`. This is a working document for the change. It is not
published with the docs site.

## Goals

1. **An asyncio core.** The HiveMind v3 Noise transport and the control-plane HTTP
   client are written natively on `asyncio` and `aiohttp`. `AsyncThalovantClient`
   and the new `AsyncThalovantControlPlane` are the implementation. The sync
   `ThalovantClient` and `ThalovantControlPlane` wrap them.
2. **Sync over async.** Each sync object owns one private event-loop thread and runs
   the async core on it. Every public sync name, signature and behaviour stays.
3. **Two core dependencies:** `aiohttp` and `cryptography`. `import thalovant` never
   imports the OVOS or HiveMind stacks.
4. **Home Assistant as a first-class consumer.**
   - Device login in single steps.
   - Hub listing.
   - Connections created with a `connection_type` and an echo check.
   - Waiting for admission, and deleting a connection with its etag.
   - Revoking the API token.
   - An async hub link: per-type handlers, a reply that keeps routing, and
     supervised reconnects.

   Every call accepts the caller's `aiohttp.ClientSession`.
5. **Parity for the other eight SDKs.** The new surface is declared as named
   capabilities, with shared vectors and a porting brief.

Acceptance, from the owner:
- no public API break;
- no regression for thalovant-voice;
- no latency or resource regression, measured;
- mypy `--strict` and ruff clean;
- coverage not lower;
- every vector green;
- a blocking-call guard;
- docs;
- a release that is ready but not published.

## Today (origin/main `cbc36ef`, 0.8.7)

| Module | Role | Third-party code it pulls in |
|---|---|---|
| `transport.py` | WSS (`HiveMessageBusClient`, a websocket-client thread), HTTP, MQTT | hivemind-bus-client, ovos-bus-client, ovos-utils, websocket-client, requests, paho-mqtt |
| `_noise_runtime.py` | `NoiseChannel` for HTTP and MQTT; identity and pin store | hivemind-bus-client (noise, `NodeIdentity`), poorman-handshake, json_database |
| `_http_runtime.py` | HTTPS polling carrier | requests |
| `control.py` | `ThalovantControlPlane` | requests |
| `client.py` | `ThalovantClient`: threaded orchestration (connect lifecycle, ask/query collectors, listen, conversation carry, refusals). `AsyncThalovantClient` wraps it with `asyncio.to_thread` | — |
| `identity.py` | identity files, env, and the YAML config | PyYAML |
| `intents.py`, `inventory.py` | the hub's intent manifest, language matching | ovos-spec-tools, langcodes |
| `session.py` | `HubSession` (reconnect ladder, probe), `OriginPreference` (a LAN origin via a scoped `getaddrinfo` override) | — |

A clean `pip install thalovant` resolves 57 packages today; the "Dependencies"
section below has the measured numbers.

## Target layers

```
L5  home.py                 home-link request/response rules; answering loop
L4  session.py              HubSession (sync, unchanged)  +  AsyncHubSession (new)
L3  control.py              AsyncThalovantControlPlane (native)  ->  ThalovantControlPlane (wrapper)
L2  client.py               AsyncThalovantClient (native orchestration)  ->  ThalovantClient (wrapper)
L1  _hive.py  _aiohttp.py   async HiveMind sessions (WSS, HTTP polling); control-plane sender; TLS policy
    _loop.py                private loop thread; sync handler dispatch thread; sync-transport adapter
L0  _noise.py  _wire.py  _noise_store.py  _language.py  _yaml.py      pure code, no I/O except the stores
```

### L0: pure pieces

- **`_noise.py`**
  - Noise `XXpsk2` and `KKpsk0` over ChaCha20-Poly1305 and AES-GCM, with an argon2id
    PSK from `cryptography`.
  - Ported from the aiothalovant prototype.
  - Checked against the reference vectors (PSK, AEAD nonce layout, canonical JSON,
    prologue), and against `noiseprotocol` itself in both roles.
- **`_wire.py`**
  - The HiveMind envelope (`HiveMessage`, attribute-compatible with
    hivemind-bus-client's for `on_hive` handlers).
  - An OVOS bus message (`msg_type`, `data`, `context`, `serialize()`).
  - The WIRE-1 binary frame decoder, ported from the Go SDK's `wire.go` and checked
    by `binary-frames.json`.
- **`_noise_store.py`** reads and writes the identity files where hivemind-bus-client
  kept them, in the same format:
  - `$XDG_CONFIG_HOME/hivemind/_identity.json`, or `<noise_state_dir>/_identity.json`;
  - the static key at `<name>_noise.key`;
  - `pinned_noise_keys`;
  - the PSK cache `<key>_psks.json`, keyed `sha256(access_key)[:16]@<node_id>`.

  An appliance that upgrades keeps the static key its hub pinned, and its own pins.
  The same refusals are kept: symlinks, corrupt state, a changed pin, and
  permissions of 0600 and 0700. Cross-process safety uses `fcntl.flock` on a
  sibling lock file (on Windows, `msvcrt`).
- **`_language.py`**
  - The OVOS-INTENT-2 language distance and CLDR likely subtags, ported from the Go
    SDK (`language_matching.go` plus its data file, langcodes 3.5.1).
  - It replaces `ovos_spec_tools.language.closest_lang` and the `langcodes` use in
    `usual_form`.
  - Proven by the 990 shared `language-matching-vectors.json` cases, which Go and
    Node already run, and by a differential test against ovos-spec-tools in the dev
    environment.
  - Because it is exact, **no `intents` extra is needed**: nothing thalovant-voice
    installs changes.
- **`_yaml.py`**: the config file reader.
  - Uses PyYAML when it is installed, so behaviour matches today.
  - Otherwise a safe subset reader for what the config holds: block mappings and
    sequences, flow lists and maps, plain and quoted scalars, and YAML 1.1
    booleans, ints and null. It is differentially tested against PyYAML.
  - Anything outside the subset raises `ThalovantIdentityError` and names
    `pip install thalovant[yaml]`.

### L1: the async I/O core

- **`_hive.py`: `AsyncHiveSession`**, one authenticated HiveMind v3 connection.
  - **Carriers:**
    - WSS over `aiohttp` (a heartbeat, and the hub's own pings answered);
    - HTTPS polling (`/connect`, `/get_messages`, `/get_binary_messages`,
      `/send_message`, `/disconnect`, with the replica-affinity cookie).
  - **Handshake:**
    - HELLO, then the offer, then Noise message 1, 2 and 3, then the encrypted HELLO;
    - the pin checked against the store;
    - one `XXpsk2` retry after a failed `KKpsk0`, keeping the pin (as
      hivemind-bus-client does);
    - the PSK cache used and forgotten on failure;
    - argon2id run in the loop's default executor.
  - **Dispatch:** bus messages by type, hive kinds by kind, binary frames by kind.
  - **Sends:** serialized under a lock. A write is shielded, so a cancelled caller
    never leaves half a chunked message on the wire.
  - **Refusal classification:** a close before HELLO, or right after message 1 or 3,
    with no code, 1000, 1005 or 1008, becomes `ThalovantHubRefusedError`. Anything
    else is `ThalovantConnectionError`.
  - **Pin id:** `wss://host:443` for WSS and `endpoint_base()` for HTTP, byte for
    byte as today.
- **`_aiohttp.py`**, the control-plane sender and TLS policy:
  - An SDK-owned session sets `use_dns_cache=False` and uses `ThreadedResolver`, so
    `preferred_origin`'s `getaddrinfo` override still applies.
  - Trust: `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE` and `SSL_CERT_FILE`, then `certifi`
    when importable (what requests used), then the system store.
  - `self_signed=True` still turns verification off.
  - A caller's session is used as given and never closed.
- **`_loop.py`:**
  - **`LoopThread`:** one daemon thread and loop per sync object.
  - **`CallbackThread`:** one per sync client. Sync `on()`, `on_hive()` and
    `on_binary()` handlers run on it in arrival order, never on the loop. This is
    today's "transport receive thread" contract, and it is what lets thalovant-voice
    call `client.emit()` from inside a handler while the main thread is blocked in
    `ask()`. On the loop, that call would deadlock.
  - **`SyncTransportAdapter`:** a custom sync `Transport` (the `transport=` argument
    and the test doubles) presented to the async core.
    - Its I/O runs in the default executor.
    - Its callbacks come back from its own thread into the loop and wait until
      processed. When a callback arrives on a foreign thread, the sync handlers it
      triggers run inline on that same thread, as today.

### L2: the client

- **`AsyncThalovantClient` is native:**
  - The connect lifecycle, with generations, a hard deadline, and cleanup that
    keeps ownership past a timeout.
  - The `ask` and `query` collectors, with the same settle, empty-reply and carry
    windows, and the same refusal attribution (`refusal_belongs_to_ask`, the
    untracked-send grace).
  - `listen`, `wait_for_event`, the conversation memory, subscriptions that outlive
    sessions, and `intents`, `list_intents` and `describe_intent`.
  - Every error text is kept word for word, because thalovant-voice classifies
    failures by substrings of these messages.
  - It accepts `session=aiohttp.ClientSession` and defaults to an owned one.
- **`ThalovantClient`** owns a `LoopThread` and an `AsyncThalovantClient` bound to
  it. Each method runs the coroutine under the same budget semantics:
  - `close(timeout)` raises when the budget runs out and cleanup continues;
  - `wait_closed` observes the actual end;
  - `listen()` is a sync generator over the async one, with its own deadline.

  Sync handlers go to the `CallbackThread`.
- The **transport classes stay importable** with the same constructors. The new
  `AsyncHiveMindWSSTransport` and `AsyncHiveMindHTTPTransport` are the
  implementation. `HiveMindWSSTransport` and `HiveMindHTTPTransport` are sync
  facades over them, for code that builds a transport itself.
- **`HiveMindMQTTTransport`** keeps paho, moved to the `thalovant[mqtt]` extra with a
  clear error when it is missing. Its Noise channel moves to the native one. The
  client runs it through the adapter.

### L3: the control plane

- **`AsyncThalovantControlPlane(api_url, *, access_token, session=None, ...)`** has
  every method of the sync class, as coroutines, and the same errors (`api-errors`
  contract).
- **`ThalovantControlPlane`:**
  - With no `session=`, it runs the async class on its `LoopThread`. `self.session`
    is a small handle with `close()` and `headers`.
  - With a caller's requests-style session (the documented `session=` argument), it
    keeps using that session synchronously, as today, without importing requests.
  - The private polling loops with injectable `sleep` and `clock` stay, because
    tests drive them.
- **New, on both classes:**
  - `begin_device_login(scopes=, client_name=) -> DeviceAuthorization`
  - `poll_device_login(grant)`: one poll. It raises
    `ThalovantDeviceLoginPending(interval)`, `...Expired` or `...Denied`, all
    subclasses of `ThalovantAPIError`, so `login_with_browser`'s callers see no
    change.
  - `get_client`, `list_clients`, `delete_client(client_id, *, etag=None)`. Without
    an etag it reads one first, retries once on 412, and treats 404 as deleted.
  - `create_client_identity(..., connection_type=...)`:
    - sends `spec.connection_type`;
    - a 422 about it raises `ThalovantUnsupportedConnectionTypeError`;
    - a response without the echo is deleted, then raises the same error;
    - 402 and a 403 `plan_limit` raise `ThalovantPlanError`;
    - a 409 `home_assistant_already_linked` raises `ThalovantAlreadyLinkedError`
      with `client_id`.
  - `BootstrapIdentityResult.operation` and `client_id`.
  - `wait_for_operation(operation, *, timeout, poll_interval)` and
    `wait_for_admission(result, *, timeout=180)`. A timeout raises
    `ThalovantAdmissionTimeoutError`, a subclass of `ThalovantConnectionError` and
    `ThalovantTimeoutError`.
  - `revoke_api_token(token_id=None)` calls `DELETE /v1/auth/api-tokens/{id}`, which
    a token may call on itself. It defaults to the `token_id` of the last device
    login.
  - `get_profile()` calls `/v1/users/profile`.

### L4 and L5: session and home link

- **`HubSession`:** the same class and threads, and its log lines at the same levels
  (voice asserts on them).
- **`AsyncHubSession(connect, *, policy=None)`** is the async twin: `on`,
  `on_state_change`, `connect()`, `run()`, `ask`, `emit`, `reply`, `close()`,
  `connected`, `held`.
  - `run()` supervises with the same `HubSessionPolicy` ladder: 10 s, doubling to
    120 s; a probe every 60 s while held and every 5 s while down.
  - `run()` reuses a client that `connect()` already opened.
  - It raises `ThalovantHubRefusedError` after `refusal_grace_seconds` of unbroken
    refusals. This is a new policy field, 600 s by default.
  - Each attempt logs at DEBUG. The integration logs one INFO line per drop and one
    per recovery.
- **`reply(event, msg_type, data=None)`** on both clients sends the OVOS-MSG-1 §5.2
  reply. It deep-copies the context, then sets `destination` to the old `source`,
  and `source` to the old `destination` (its first entry when it is a list).
- **`home.py`** carries the `thalovant.home.request` → `thalovant.home.response`
  rules:
  - `HomeRequest.from_event` and `home_response(...)`;
  - plain-text speech (SSML stripped);
  - the `response_type` and `error_code` sets;
  - `answer_home_requests(client, handler, *, timeout=10)`, which always replies,
    with `timeout` or `failed_to_handle` when the handler does not answer.

## Dependencies

- **Core:** `aiohttp>=3.11`. `cryptography` keeps today's markers: the
  `>=50.0.0` security floor, and the Intel-Mac `>=48.0.1,<49` pin, which exists
  because no x86_64 macOS wheel ships after 48.0.1.
- **Extras:**
  - `mqtt`: paho-mqtt
  - `yaml`: PyYAML
  - `listing`: thalovant-languages, unchanged
  - `dev`: adds the reference libraries the tests compare against (noiseprotocol,
    hivemind-bus-client, ovos-spec-tools, PyYAML, requests)
- **Removed from the core:**
  - hivemind-bus-client (and ovos-bus-client, ovos-utils, websocket-client and
    json_database with it)
  - poorman-handshake (and pycryptodomex, argon2-cffi, zxcvbn)
  - ovos-spec-tools and langcodes
  - requests (and urllib3, idna, charset-normalizer, certifi)
  - paho-mqtt
  - PyYAML
- **Check:** `uv pip compile` against Home Assistant's `package_constraints.txt`
  (Python 3.14, `aiohttp==3.14.3`, `cryptography==50.0.1`).

## Compatibility risks and what answers each

| # | Risk | Answer |
|---|---|---|
| 1 | Sync handlers used to run on the transport's receive thread; voice calls `emit()` from inside one while `ask()` blocks the main thread | `CallbackThread`; voice's `test_volume` (answer under 1 s) and a dedicated SDK test |
| 2 | Custom sync transports (`transport=`), and handlers they call synchronously | `SyncTransportAdapter`; the existing fake-transport suites run unchanged |
| 3 | `on_hive` handlers receive a hivemind-bus-client `HiveMessage` today | `_wire.HiveMessage` with the same attributes (`msg_type`, `payload`, `metadata`, `route`, `node`, `target_site_id`, `target_pubkey`, `source_peer`, `bin_type`, `as_dict`, `serialize`) |
| 4 | `reply.raw_messages`, and `event.raw` as ovos-bus-client `Message` objects | `_wire.BusMessage` with `msg_type`, `data`, `context`, `serialize()`, `as_dict` |
| 5 | The static key, pins and PSK cache on disk | the file-compatible store; tested against files hivemind-bus-client wrote |
| 6 | Pin id strings | kept byte for byte (`wss://host:port`, `endpoint_base()`) |
| 7 | `preferred_origin` overrides `socket.getaddrinfo` | `ThreadedResolver`, no DNS cache; a test dials through the override |
| 8 | TLS trust used to be requests plus certifi | the same order of sources as requests; `self_signed` kept |
| 9 | Voice classifies failures by message substrings | error texts kept word for word; a test pins the messages voice reads |
| 10 | `ThalovantControlPlane(session=requests.Session)` and `api.session` | the legacy session path; a `session` handle with `close()` and `headers` |
| 11 | `thalovant.transport.HiveMindMQTTTransport` without paho | an import error only at `connect()`, naming `thalovant[mqtt]` |
| 12 | Language matching must equal ovos-spec-tools | 990 shared vectors plus a differential sweep |
| 13 | YAML config parsing | PyYAML when installed; the subset reader differentially tested |
| 14 | Python 3.10 support | no `asyncio.timeout`, no `datetime.UTC`; CI stays 3.10 to 3.14 |
| 15 | `HubSession` must still build with no running loop, call `warm()` in `__init__`, and keep `_warming` and its log levels | untouched |
| 16 | `thalovant.session.alive(client)` reads `connection_info().phase` | the phase strings are kept |
| 17 | Voice's test hushes the `hivemind_bus_client` logger | that test already `importorskip`s; nothing to do |

## Test strategy

- **The existing suite is the regression oracle.** Tests of public behaviour run
  unchanged. Tests of removed internals are rewritten against their replacements,
  with the same assertions. They are listed in the PR:
  - WSS through `WebSocketApp`;
  - `requests.Session` monkeypatches;
  - hivemind-bus-client protocol classes.
- **New tests:**
  - an aiohttp in-process hub that follows HiveMind-core 5.x, for WSS end to end
    (reconnect, refusal, pinning, KK→XX, binary, chunking);
  - the HTTPS peer the suite already has, now spoken to by aiohttp;
  - vector suites for noise, binary frames, language matching and the four new
    capabilities;
  - the async client's semantics mirrored from the sync suites;
  - `AsyncHubSession` and `home`.
- **Blocking-call guard:** an async-path test runs the loop in debug mode with
  `slow_callback_duration=0.05`, and patches `time.sleep` and blocking socket calls
  to fail when they run on the loop thread.
- **thalovant-voice:** its full suite runs against this branch in a fresh venv.
  Its reconnect ladder and mid-turn answer tests are called out.
- **Benchmarks:** `scripts/bench_async_core.py` runs against a local fake hub, on
  0.8.7 and on this branch on the same machine. It measures:
  - connect time;
  - first reply;
  - reconnect after a drop;
  - idle CPU and RSS;
  - import time;
  - package count and size.

## Parity

New reference capabilities, each with vectors in the `api-errors` format (HTTP
exchanges and expected outcomes) and Python results in the conformance record:

- `device-login`: begin, and one poll step with pending, `slow_down`, expired,
  denied and approved; caller-chosen scopes; token revocation.
- `connection-kinds`: create with `spec.connection_type` and the echo check;
  delete on a missing echo; 402, 409 and 422 mapping; delete by etag.
- `connection-admission`: wait on the operation; ready, failed, untracked and
  timeout.
- `home-link`: the reply routing, the home request/response rules (always reply,
  the error codes, the 10 s bound, plain text), and per-type handlers on a
  reconnecting connection.

Scope:
- **embedded-c:** `not-applicable` for all four. It has no HTTP client, and its
  networking belongs to the caller.
- **mcp:** `not-applicable` for `home-link`. A connection lives for one tool call,
  and MCP has no channel to deliver an unsolicited request.
- Every other SDK is `required`.

Consumers first declare `planned`. `docs/home-link-porting.md` is the brief.

The reference digest changes in any case, because the source changes. Every
consumer has to acknowledge it: node, go, rust, kotlin, swift, dotnet,
embedded-c and mcp.

## Stages (one branch, one draft PR)

1. L0 pure pieces and their vectors.
2. L1: `_hive` and the transports, WSS and HTTP, plus the fake hub.
3. L3: the async control plane, the sync wrapper, and the new control-plane calls.
4. L2: the native async client, the sync wrapper, the adapter and the callback
   thread.
5. L4 and L5: `AsyncHubSession`, `reply` and `home`.
6. Dependencies, extras, and import hygiene.
7. Quality: mypy `--strict`, ruff, coverage, the blocking guard.
8. Parity: capabilities, vectors, record, snapshot, porting brief.
9. Docs: README, migration note, CHANGELOG, the docs.thalovant.com page; version
   0.9.0 (a minor: dependency changes and additive API).
10. Verification: the SDK suite, voice's suite, benchmarks, the HA constraints
    resolve, and the public-API diff.
