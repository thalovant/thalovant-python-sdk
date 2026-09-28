# Porting the Home Assistant link

The Python SDK 0.9 adds four capabilities that together let Home Assistant (or any
home controller) link to a Thalovant hub: sign in on a device, create a connection
of a named kind, wait for the hub to admit it, and answer the hub's requests on the
data plane. Each capability is a behaviour written down as shared vectors. Every SDK
marked `required` in `contracts/sdk-parity.json` implements it, runs the vectors,
and records its results.

| Capability | Vectors | Required in | Not applicable in |
|---|---|---|---|
| `device-login` | `device-login-vectors.json` | Node, Go, Rust, Kotlin, Swift, .NET, MCP | embedded-c |
| `connection-kinds` | `connection-kinds-vectors.json` | Node, Go, Rust, Kotlin, Swift, .NET, MCP | embedded-c |
| `connection-admission` | `connection-admission-vectors.json` | Node, Go, Rust, Kotlin, Swift, .NET, MCP | embedded-c |
| `home-link` | `home-link-vectors.json`, `link-keeping-vectors.json` | Node, Go, Rust, Kotlin, Swift, .NET | embedded-c, MCP |

embedded-c has no HTTP client, and it leaves the socket, the loop and every handler
to its caller. MCP holds a hub connection only for the length of one tool call, and
has no channel to hand an unsolicited hub request to a model. The manifest says the
same thing in each capability's `scope`.

## What each capability does

### device-login

The device flow (RFC 8628), one step at a time. The caller runs the loop, so a Home
Assistant config flow can show the code and poll on its own schedule.

- **Begin.** Send `POST /v1/auth/device/authorize` with the scopes and the client name.
  Leave out an empty scope list, exactly as you leave out none: the API requires at least
  one scope and answers `[]` with a 422, and a missing field asks for its default.
  Expose `user_code`, `verification_uri`, `verification_uri_complete` (absent is
  null), `interval` and `expires_in`. Refuse a verification URL that is not http(s),
  has no host, or carries credentials: it is about to be opened in a browser. Home
  Assistant asks for `hubs:read`, `clients:read` and `clients:write`, which is also
  all that a Free plan can approve. Python exports these as `HOME_ASSISTANT_SCOPES`.
- **Poll.** Send one `POST /v1/auth/device/token` with `{device_code}`. The API answers
  a pending sign-in with 400 and an `error` code, and each code has an outcome:

  | `error` | Outcome |
  |---|---|
  | `authorization_pending` | pending, with the interval to wait |
  | `slow_down` | pending, with the interval five seconds longer; it stays longer for later polls of the same code |
  | `expired_token` | expired |
  | `access_denied` | denied |

  A 2xx is approved. Keep the token and its `token_id`, and expose `token_type`,
  `scopes` and `expires_at`. A 2xx with no `access_token` is an error. So is any other
  failure, which carries the `api-errors` fields.
- **Revoke.** Send `DELETE /v1/auth/api-tokens/{token_id}` for the token the SDK signed in
  with. A token may always revoke itself, whatever its scopes. Forget it locally
  afterwards.
  - Revoking the token in use is idempotent. A token already revoked (or expired) cannot
    authenticate its own revoke, so the API answers 401. That is success too: forget the
    token either way.
  - Every sign-in sets `token_id` from its own answer: a password sign-in has none, so it
    clears the id a device login left. Otherwise a later default revoke would reach a
    token the SDK no longer holds.
- **Secrets.** Neither the device code nor the token ever appears in an error message.

### connection-kinds

Create a connection whose kind is `spec.connection_type`, and delete one.

- Create sends `POST /v1/clients` with `spec.version: "1"` and
  `spec.connection_type: <kind>`, plus the connection's own generated credentials. The
  API ignores a top-level `kind`.
- **Check the echo.** The answer must repeat the kind in `spec.connection_type`. When it
  does not, the API made an ordinary satellite. Delete it (`DELETE` with `If-Match:` the
  answer's `etag`), then fail as *unsupported*.
- Map each refusal to a kind the caller can branch on:

  | Answer | Kind |
  |---|---|
  | 422 that names `connection_type` in its `detail` or `code`, or in the `loc` or `msg` of a validation error (under `errors`, or under `detail` when that is a list) | unsupported |
  | 402, or 403 with code `plan_limit` | plan |
  | 409 `home_assistant_already_linked` | already linked, with the `client_id` the problem names |
  | 401, 423, or 403 with detail `Insufficient scopes` | authentication: sign in again |
  | anything else | an ordinary API error |

  Every kind keeps `status`, `code` and `detail`. Never search the whole body for
  `connection_type`: a validation error about any other field echoes the request,
  `spec.connection_type` included, as its `input`.
- Delete sends `DELETE /v1/clients/{id}` with `If-Match`. With no etag given, it reads one
  with `GET` first. A 412 means the client changed underneath, so read the etag again and
  retry once. A 404 on either request means the client is already deleted.

### connection-admission

A new connection is admitted about ninety seconds after it is created. The create
answer carries an `operation`. Follow its `links.self`, polling
`GET /v1/operations/{id}`:

| Operation status or answer | Outcome |
|---|---|
| `ready` | admitted |
| `failed`, `timed_out` | failed, with the operation's `error_code` (and no status) |
| `requested`, `committed`, `applied` | keep polling |
| 404, or no operation at all | admitted at once |
| 5xx | ride it out and keep polling |
| 429 | wait, then poll again (below) |
| 401, 403 | the API's own authentication error, passed through unchanged: never a failed admission |
| any other refusal of the wait itself | failed, keeping the `api-errors` status, code and detail |
| the API cannot be reached | your SDK's unreachable/network error, as it is: never a failed admission |

- **A 429's wait.** Read `retry_after_seconds` from the problem. The API puts it *inside*
  the `detail` object (FastAPI's envelope around a structured refusal:
  `{"detail": {"code": "token_rate_limited", "message": …, "retry_after_seconds": N}}`),
  so look there and at the top. Without it, read the `Retry-After` header, then
  `RateLimit-Reset`: the API's own rate limiter answers in plain text (`Too Many
  Requests`) with only `RateLimit-*` headers. Use the poll interval when it is longer or
  nothing says. When the wait is longer than the time left, it is a timeout at once.
- **Deadlines.** No read may run past the wait's own deadline: bound each poll by what is
  left. When the deadline passes first, the outcome is a *timeout*. Make it both a
  connection error and a timeout in your language's error model, and end its message
  with "it may still admit it later": the connection may still be admitted.
- **Origins.** A `links.self` whose origin is not the API's is never fetched, because the
  token goes nowhere else. An origin is the scheme, the host and the port, with the
  scheme's default port spelled out (`https://h` and `https://h:443` are one origin;
  `http://h` and `https://h` are two).

### home-link

On the data plane, the hub sends `thalovant.home.request`:

```json
{"request_id": "…", "utterance": "…", "lang": "…", "conversation_id": "…"}
```

Every request gets at most one `thalovant.home.response`, and never after the hub's 10
seconds:

```json
{"request_id": "…", "speech": "…", "response_type": "…", "error_code": "…",
 "continue_conversation": false, "conversation_id": "…"}
```

- **The response is a reply** (OVOS-MSG-1 §5.2), built from the request's context *as the
  hub sent it*. Deep-copy that context, set `destination` to the old `source`, and set
  `source` to the old `destination`, taking its first entry when it is a list. A context
  with a destination and no source gets a reply with **no** `destination`: keeping the
  old one would address the reply to its own sender.
  - Python takes a bus message in the way a HiveMind node does: it maps `destination` to
    `source` and drops `hivemind_verified_source_peer`. It therefore keeps the wire
    context aside to build the reply from. An SDK that delivers the raw context needs no
    such copy.
- **`speech` is plain text**, made in this order, with nothing from your platform's HTML
  library (their entity tables differ):
  1. Remove markup. A tag is `<` or `</` immediately followed by an ASCII letter, then
     everything up to the next `>` that is not inside a quoted attribute value. A comment
     is `<!--` to `-->`, a processing instruction `<?` to `?>`. Any other `<` is text:
     "5 < 6 and 7 > 3" stays whole, and an unclosed `<b` is text.
  2. Decode character references once, left to right: numeric ones (`&#72;`, `&#x48;`,
     `&#X48;`) except 0, surrogates (D800–DFFF) and anything above 10FFFF, which stay as
     written; the five XML entities `&amp; &lt; &gt; &quot; &apos;`; and `&nbsp;`. Nothing
     else: `&eacute;` and `&copy;` stay as written, and a reference needs its `;`.
  3. Collapse every run of Unicode White_Space characters (not your regex's `\s`, which
     differs by language) to one space, and trim both ends.
- **The code sets are fixed.** `response_type` is one of `action_done`, `query_answer` or
  `error`. `error_code` appears only with `error`, and is one of `no_intent_match`,
  `no_valid_targets`, `failed_to_handle`, `unknown`, `timeout` or `agent_unavailable`.
- **The SDK always answers.** When the handler cannot, the SDK replies with an error code
  and speech `""`: the hub speaks its own sentence for the code, in the device's
  language, which an SDK does not know.

  | Handler | Reply |
  |---|---|
  | raises | `failed_to_handle` |
  | does not answer within the timeout (9 s, a second inside the hub's 10) | `timeout` |
  | answers outside the two code sets | `unknown` |

- **Missing fields.** A request with no `request_id` is answered with `request_id: ""`.
  `conversation_id` is echoed when the handler gives none.
- **The hub's bound covers the reply too.** Count 10 s from the request's arrival. The
  handler gets its timeout or what is left of that, whichever is less; the reply's sending
  gets what the handler left. Never start a reply after the bound, and withdraw one that
  is still queued when it passes. So after a 9 s handler the reply has about 1 s, and a
  reply the hub has already given up on is never sent. Send the answer at the deadline
  whether or not the handler has returned: a handler that ignores cancellation must not
  hold the reply back.

### Keeping the link up (`link-keeping-vectors.json`)

These rules are part of `home-link` and have their own vector file, whose `policy` block
holds every constant.

- **Which closes are refusals.** A close whose RFC 6455 code is 1000, 1005 or 1008 is the
  hub refusing the credentials, when it happens during the handshake -- any step of it,
  including between the hub's HELLO and its offer -- or within 750 ms after it. 1005 includes a close frame with no status at all, which is what hivemind-core
  sends for an unknown access key and after a Noise abort (aiohttp reports it as 0; map it
  to 1005). Everything else is a drop: 1001, 1011, 1013, a socket that ended with no close
  frame (1006, or no code), and any close after the window.
- **Late codes.** A transport may learn a close's code after it learns of the close
  (URLSession does). Wait up to 250 ms for the code before calling the close a drop. The
  close's own time, not when its code arrived, decides whether it fell inside the window.
- **Failed handshakes.** A Noise handshake message that does not authenticate under the
  key the password derives is a refusal: a wrong password. A KK attempt that fails that
  way, or that the hub closes with a refusal code, is followed at once, inside the same
  connect, by one XX attempt, and the XX attempt's outcome is the connect's. Only XX tells
  a changed password (a refusal) from a changed hub key. The XX attempt is not a
  downgrade: the pinned key is still checked when XX completes, so a hub that is not the
  pinned one still fails. A hub whose static key is not the one pinned for it is a
  connection error, not a refusal; never replace the pin yourself.
  A WebSocket upgrade answered 401 or 403 is a refusal; any other failed upgrade is a
  connection failure.
- **The supervisor** (Python's `LinkSupervisor`, which `AsyncHubSession.run()` asks after
  every attempt):

  | Outcome | Decision |
  |---|---|
  | up | hold; reset the ladder and the refusal clock |
  | dropped (an established link went down) | dial again at once |
  | failed | wait the ladder's step (10 s, doubling to 120 s); reset the refusal clock |
  | refused | wait the ladder's step, until refusals have lasted 600 s since the first (inclusive); then give up |
  | key changed | give up at once: retrying cannot change it |

  While a link is up, probe it every 60 s, and every 5 s while none is held.
- **Logging.** Every attempt is logged at debug level only; the application decides what
  deserves more.

## The Python reference

| Behaviour | Python |
|---|---|
| begin / poll / revoke | `AsyncThalovantControlPlane.begin_device_login`, `.poll_device_login`, `.revoke_api_token` (and the sync class) |
| pending / expired / denied | `ThalovantDeviceLoginPending(interval)`, `ThalovantDeviceLoginExpired`, `ThalovantDeviceLoginDenied`, all `ThalovantAPIError` |
| create with a kind | `create_client_identity(hub, name=…, connection_type="home_assistant")` → `BootstrapIdentityResult` with `.operation`, `.client_id`, `.connection_type` |
| refusal kinds | `ThalovantUnsupportedConnectionTypeError`, `ThalovantPlanError`, `ThalovantAlreadyLinkedError(client_id)`, `ThalovantAuthError` |
| delete | `delete_client(client_id, etag=None)` |
| admission | `wait_for_admission(result, timeout=180)`; `ThalovantAdmissionTimeoutError` (a `ThalovantConnectionError` and a `ThalovantTimeoutError`), `ThalovantAdmissionFailedError(error_code, status_code, code, detail, problem)`, `ThalovantAuthError` passed through, `ThalovantAPIUnreachableError`; `ThalovantAPIError.retry_after_seconds` |
| reply | `AsyncThalovantClient.reply(event, msg_type, data)`, `reply_context(context)` |
| home requests | `thalovant.home`: `HomeRequest`, `HomeAnswer`, `home_response`, `answer_home_request(client, event, handler, timeout=9, hub_timeout=10)`, `answer_home_requests(client, handler)`, `plain_speech`, `decode_references`; `thalovant.rich.strip_ssml` |
| closes and handshakes | `thalovant._hive.close_refuses(code, closed_after_handshake_ms=…, code_late_ms=…)`, `REFUSAL_CLOSE_CODES`, `REFUSAL_SETTLE_MS`, `CLOSE_CODE_GRACE_MS`; `ThalovantHubRefusedError`, `ThalovantHubKeyChangedError` (a `ThalovantConnectionError`) |
| the supervisor | `thalovant.session.LinkSupervisor(policy).after(outcome, now)` → `LinkDecision(action, wait_seconds, reason)` |
| a kept link | `AsyncHubSession(connect)` / `.for_identity(identity, session=…)`: `connect()`, `run()`, `on()`, `on_state_change()`, `reply()`, `close()` |

## Idioms per language

Each SDK keeps its own idioms. What has to be the same is the behaviour in the vectors.
Three rules hold in every language:

- **No new error hierarchies are required.** Classify a refusal, a changed hub key, an
  authentication failure or an unreachable API with whatever your SDK already has -- a
  subclass, an enum case, a `kind` field, a predicate -- as long as existing calls keep
  returning and throwing what they did. Go, Rust, Kotlin and Swift did it that way. (Python
  adds subclasses of the errors it raised before, which is the same thing in Python.)
- **Send the answer at the deadline whatever the handler is doing.** Racing the handler
  against a timer is not enough when the race waits for the loser: a Swift task group
  that races a `Task.sleep` still waits for a child that ignores cancellation, so a
  handler stuck in a non-cancellable call holds the reply past the hub's bound. Start the
  handler detached, and when the timer fires, send the timeout answer and let the handler
  finish (or not) on its own; its late result is dropped.
- **Timers wait at least the whole duration, on a monotonic clock.** A timer that can wake
  early reads "waited 1000 ms" as 999 and fails a `waited_at_least_ms` case, or ends a
  wait a poll before its deadline. .NET's `Task.Delay` woke 1 ms early on Windows: measure
  the elapsed time on a monotonic clock (`Stopwatch`, `Instant`, `performance.now`,
  `ContinuousClock`, `time.Monotonic`) and wait again for what is left.

Per language:

- **Node (TypeScript).** Promise-returning methods; an `AbortSignal` in the options of
  anything that waits (`waitForAdmission`, the link). Poll with `setTimeout` inside a
  promise, re-arming when it fires early. Accept an injected `fetch` for the control
  plane. Handlers may be `async`; start one, and on the timer send the timeout answer
  without awaiting it.
- **Rust.** `async fn`s on the runtime the SDK already uses; classify with the
  `ThalovantError` it has (its `api_problem()`, `api_code()` and `api_detail()` stay).
  Run the handler on a spawned task and send the answer when `tokio::time::sleep` (a
  monotonic `Instant` deadline) wins; the task is left to finish. The Rust scanner fix in
  `check-sdk-parity.py` means a lifetime such as `&'static str` no longer hides the
  vector names after it.
- **Kotlin.** `suspend` functions; run the handler in its own `async` and send the answer
  when `withTimeoutOrNull` expires rather than waiting for a handler that is not
  cooperating. `deleteClient(clientId, etag)` reads the etag first, retries once on 412
  and treats 404 as deleted.
- **Swift.** `async throws` functions and the `ThalovantError` it has. Do not bound the
  handler with a task group: start it as an unstructured `Task`, and when a
  `ContinuousClock` sleep ends first, send the timeout answer and move on. URLSession can
  report a close before its code; wait up to 250 ms for `closeCode` before calling the
  close a drop.
- **.NET.** `Task`-returning `…Async` methods with a `CancellationToken`, exceptions the
  SDK already has. Measure waits with `Stopwatch` and wait again for any remainder,
  because `Task.Delay` can wake early. Send the handler's timeout answer from the timer's
  continuation rather than awaiting the handler.
- **Go.** Blocking calls that take a `context.Context` first, typed errors checked with
  `errors.As`. The link runs in a goroutine the caller starts. Handlers are
  `func(context.Context, HomeRequest) (HomeAnswer, error)`, run in their own goroutine
  under a derived context with the 9 s deadline; answer from a `select` on the result
  channel and the deadline, never by waiting for the handler to return.
- **embedded-c.** Not applicable, for the reasons in the manifest. A caller that runs its
  own loop can still apply the home-link rules with the wire helpers. The vectors are
  the specification it would follow.
- **MCP.** Device login becomes two tools, `begin` and `poll` (a tool call cannot wait
  for a person). Create, admission and delete are tools over the control plane.
  home-link, and so keeping a link up, is not applicable.

## Rolling it out

1. Vendor the five vector files where your test runner reads fixtures. They are compared
   as parsed JSON, so formatting is free. Every duration in them is whole milliseconds
   (`*_ms`), since only whole numbers compare equal across languages; the one exception
   is `retry_after_seconds`, which is the API's own field inside a response body.
2. Serve each HTTP case from a loopback server, in order, and check every request against
   the one the case names: method, path, body or body subset, `If-Match`,
   `Authorization`. Answer with the case's status, content type, body and `headers` (a
   429 carries `Retry-After` or `RateLimit-Reset`). Fill `{api_host}` and `{api_port}` in
   an operation's `links.self` with the loopback server's, and point the SDK at a port
   nothing listens on for `"api": "unreachable"`. Then compare what your SDK produced with
   `expect`, shaped exactly as the case shapes it. `tests/test_home_link_vectors.py` and
   `tests/test_link_keeping_vectors.py` are the Python runners; the second drives a real
   Noise handshake against an in-process hub for the `handshake` cases, and the pure
   close rule and supervisor for the others.
3. Record the results (`THALOVANT_CONFORMANCE_OUT`) and declare the capability in your
   `contracts/sdk-parity.json`, with implementation and test evidence and the vendored
   paths.
4. Acknowledge the new reference digest. Until then the gate reports your SDK as one
   reference behind, and a Python release waits for it.
