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
| `home-link` | `home-link-vectors.json` | Node, Go, Rust, Kotlin, Swift, .NET | embedded-c, MCP |

embedded-c has no HTTP client, and it leaves the socket, the loop and every handler
to its caller. MCP holds a hub connection only for the length of one tool call, and
has no channel to hand an unsolicited hub request to a model. The manifest says the
same thing in each capability's `scope`.

## What each capability does

### device-login

The device flow (RFC 8628), one step at a time. The caller runs the loop, so a Home
Assistant config flow can show the code and poll on its own schedule.

- **Begin.** Send `POST /v1/auth/device/authorize` with the scopes and the client name.
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
| `failed`, `timed_out` | failed, with the operation's `error_code` |
| `requested`, `committed`, `applied` | keep polling |
| 404, or no operation at all | admitted at once |
| 5xx | ride it out and keep polling |
| 429 | wait the problem's `retry_after_seconds` (top level, or inside a `detail` object), or the poll interval when that is longer, then poll again; if that is more than the time left, it is a timeout at once |

When the deadline passes first, the outcome is a *timeout*. Make it both a connection
error and a timeout in your language's error model: the connection may still be
admitted later. A `links.self` on another origin than the API's is never fetched,
because the token goes nowhere else.

### home-link

On the data plane, the hub sends `thalovant.home.request`:

```json
{"request_id": "…", "utterance": "…", "lang": "…", "conversation_id": "…"}
```

Every request gets exactly one `thalovant.home.response`, within the hub's 10 seconds:

```json
{"request_id": "…", "speech": "…", "response_type": "…", "error_code": "…",
 "continue_conversation": false, "conversation_id": "…"}
```

- **The response is a reply** (OVOS-MSG-1 §5.2), built from the request's context *as the
  hub sent it*. Deep-copy that context, set `destination` to the old `source`, and set
  `source` to the old `destination`, taking its first entry when it is a list.
  - Python takes a bus message in the way a HiveMind node does: it maps `destination` to
    `source` and drops `hivemind_verified_source_peer`. It therefore keeps the wire
    context aside to build the reply from. An SDK that delivers the raw context needs no
    such copy.
- **`speech` is plain text.** Remove markup, decode entities, and collapse whitespace.
- **The code sets are fixed.** `response_type` is one of `action_done`, `query_answer` or
  `error`. `error_code` appears only with `error`, and is one of `no_intent_match`,
  `no_valid_targets`, `failed_to_handle`, `unknown`, `timeout` or `agent_unavailable`.
- **The SDK always answers.** When the handler cannot, the SDK replies with an error code
  and speech `""`: the hub speaks its own sentence for the code, in the device's
  language, which an SDK does not know.

  | Handler | Reply |
  |---|---|
  | raises | `failed_to_handle` |
  | does not answer within the timeout (Python: 9 s, a second inside the hub's 10) | `timeout` |
  | answers outside the two code sets | `unknown` |

- **Missing fields.** A request with no `request_id` is answered with `request_id: ""`.
  `conversation_id` is echoed when the handler gives none.
- **Keeping the link.** A long-lived link reconnects on a ladder. Python's policy is 10 s,
  doubling to 120 s, with a probe every 60 s while connected and every 5 s while down.
  - It treats refusals as "not admitted yet" for a grace period (600 s), then gives up
    with a refusal error.
  - A hub that does not know the client's static key says so only by closing right after
    the handshake. A close with no status, 1000, 1005 or 1008 inside a short settle
    window (0.75 s) is therefore a refusal, not a drop.
  - Every attempt is logged at debug level only; the application decides what deserves
    more.

### Not covered by vectors yet

Keeping a link up has no shared vectors so far: which close codes count as a refusal,
the settle window after the handshake, and the refusal grace. The rules are described
above; vectors for them are planned.

## The Python reference

| Behaviour | Python |
|---|---|
| begin / poll / revoke | `AsyncThalovantControlPlane.begin_device_login`, `.poll_device_login`, `.revoke_api_token` (and the sync class) |
| pending / expired / denied | `ThalovantDeviceLoginPending(interval)`, `ThalovantDeviceLoginExpired`, `ThalovantDeviceLoginDenied`, all `ThalovantAPIError` |
| create with a kind | `create_client_identity(hub, name=…, connection_type="home_assistant")` → `BootstrapIdentityResult` with `.operation`, `.client_id`, `.connection_type` |
| refusal kinds | `ThalovantUnsupportedConnectionTypeError`, `ThalovantPlanError`, `ThalovantAlreadyLinkedError(client_id)`, `ThalovantAuthError` |
| delete | `delete_client(client_id, etag=None)` |
| admission | `wait_for_admission(result, timeout=180)`; `ThalovantAdmissionTimeoutError` (a `ThalovantConnectionError` and a `ThalovantTimeoutError`), `ThalovantAdmissionFailedError(error_code)` |
| reply | `AsyncThalovantClient.reply(event, msg_type, data)`, `reply_context(context)` |
| home requests | `thalovant.home`: `HomeRequest`, `HomeAnswer`, `home_response`, `answer_home_requests(client, handler)` |
| a kept link | `AsyncHubSession(connect)` / `.for_identity(identity, session=…)`: `connect()`, `run()`, `on()`, `on_state_change()`, `reply()`, `close()` |

## Idioms per language

Each SDK keeps its own idioms. What has to be the same is the behaviour in the vectors.

- **Node (TypeScript).** Promise-returning methods; an `AbortSignal` in the options of
  anything that waits (`waitForAdmission`, the link). Model the outcomes as `Error`
  subclasses of `ThalovantApiError`: `DeviceLoginPendingError` carries `interval`, and
  the admission timeout extends both the connection error and the timeout error (or
  sets a `kind` both checks recognise). Poll with `setTimeout` inside a promise. Accept
  an injected `fetch` for the control plane. Handlers may be `async`; bound them with
  `Promise.race` against a timer, then reply.
- **Rust.** `async fn`s on the runtime the SDK already uses. Extend `ThalovantError`
  with variants such as `DeviceLoginPending { interval }`, `DeviceLoginExpired`,
  `DeviceLoginDenied`, `Plan`, `AlreadyLinked { client_id }`, `UnsupportedKind`,
  `Auth` and `AdmissionTimeout`. Keep `api_problem()`, `api_code()` and `api_detail()`
  on each. Bound the handler with `tokio::time::timeout`. Fix one thing on the way: the
  device poll's unexpected-error path drops `status` and `problem` today; route it
  through `api_response_error()`.
- **Kotlin.** `suspend` functions, `withTimeout` for the 10 s bound, and a sealed error
  hierarchy under `ThalovantApiException`. Kotlin already has
  `deleteClient(clientId, etag)` and `listClients`. Move the misplaced KDoc, then add
  read-the-etag-first, 412-retry-once and 404-is-deleted to `deleteClient`. The link's
  handlers are `suspend (HomeRequest) -> HomeAnswer`.
- **Swift.** `async throws` functions and a `ThalovantError` enum with associated values
  (`.deviceLoginPending(interval:)`, `.alreadyLinked(clientId:)`). Bound the handler with
  a task group that races a `Task.sleep`. The device-login and provisioning code sits
  outside the hashed `control` evidence today; name those files in the acceptance
  record's `implementation`.
- **.NET.** `Task`-returning `…Async` methods with a `CancellationToken`, and exceptions
  deriving from `ThalovantApiException`. The admission timeout derives from the
  connection exception and implements a timeout marker interface, or carries
  `IsTimeout`. Bound the handler with `WaitAsync(TimeSpan)`. As in Swift, bring
  device-login and provisioning under the hashed evidence.
- **Go.** Blocking calls that take a `context.Context` first, and typed errors checked
  with `errors.As`: `*DeviceLoginPendingError` has `Interval time.Duration`, and
  `*AdmissionTimeoutError` answers `true` to both `IsConnectionError()` and
  `Timeout()`. The link runs in a goroutine the caller starts. Handlers are
  `func(context.Context, HomeRequest) (HomeAnswer, error)`, called under a derived
  context with the 9 s deadline. Go's session streams every event today, so add
  per-type handlers or a filter.
- **embedded-c.** Not applicable, for the reasons in the manifest. A caller that runs its
  own loop can still apply the home-link rules with the wire helpers. The vectors are
  the specification it would follow.
- **MCP.** Device login becomes two tools, `begin` and `poll` (a tool call cannot wait
  for a person). Create, admission and delete are tools over the control plane.
  home-link is not applicable.

## Rolling it out

1. Vendor the four vector files where your test runner reads fixtures. They are compared
   as parsed JSON, so formatting is free. Every duration in them is whole milliseconds
   (`*_ms`), since only whole numbers compare equal across languages; the one exception
   is `retry_after_seconds`, which is the API's own field inside a response body.
2. Serve each HTTP case from a loopback server, in order, and check every request against
   the one the case names: method, path, body or body subset, `If-Match`,
   `Authorization`. Then compare what your SDK produced with `expect`, shaped exactly as
   the case shapes it. `tests/test_home_link_vectors.py` is the Python runner.
3. Record the results (`THALOVANT_CONFORMANCE_OUT`) and declare the capability in your
   `contracts/sdk-parity.json`, with implementation and test evidence and the vendored
   paths.
4. Acknowledge the new reference digest. Until then the gate reports your SDK as one
   reference behind, and a Python release waits for it.
