<!-- Last Edit: Claude Opus 5.5 - 2026-11-08 - Motive: CodeRabbit review of #113: token endpoint origin rule, RFC 6749 error allow-list, HEAD without a body, the tab waits for the end, empty device name, redact before formatting, the Go rune slice; digest 50af6731e276. -->

# Porting browser sign-in (`browser-login`)

Python 0.11.0 adds browser sign-in: the OAuth authorization code grant with PKCE
(RFC 7636, S256 only) on a loopback redirect (RFC 8252 section 7.3). A program on a
machine with a browser signs its user in through that browser. Device login stays
as it is, and it is the fallback for a machine with no browser.

Why it matters: anybody can relay a device code. Somebody reads the code off one
screen and types it in anywhere (device-code phishing). A loopback redirect cannot
be relayed, because the browser that approves delivers the code to its own
computer. A sign-in link sent to somebody else delivers nothing to the sender.

The behaviour is written down in `contracts/conformance/browser-login-vectors.json`.
Every SDK that `contracts/sdk-parity.json` marks `required` implements it, runs the
vectors, records its results, and acknowledges the reference.

| Capability | Vectors | Required in | Not applicable in |
|---|---|---|---|
| `browser-login` | `browser-login-vectors.json` (106 cases) | Node, Go, Rust, Kotlin, Swift, .NET, MCP | embedded-c |

The scope matrix is `device-login`'s. embedded-c is a caller-buffer wire library
with no control-plane HTTP client. Its caller owns every request it would make and
every socket it would listen on. MCP is required, as it is for device login; see
[MCP](#mcp) for where the platform boundary sits.

The API side is thalovant-api #501 (`feat/browser-sign-in-sdk-home-assistant`). It
ships before any SDK release. Until it is live, `GET /v1/auth/native/config` answers
404 and the chooser falls back to device login.

## Public API to mirror

The names follow each language's conventions; the shapes and defaults must match.

| | Python reference |
|---|---|
| Browser sign-in | `AsyncThalovantControlPlane.browser_login(*, scopes=None, device_name=None, open_browser=True, timeout=300.0, prompt=None) -> dict[str, Any]` |
| The chooser | `AsyncThalovantControlPlane.interactive_login(*, method="auto", scopes=None, device_name=None, open_browser=True, timeout=None) -> dict[str, Any]` |
| Method names | `LoginMethod = Literal["auto", "browser", "device"]` |
| Sync forms | `ThalovantControlPlane.browser_login(...)`, `.interactive_login(...)`: the same arguments, run on the SDK's loop thread |
| The browser rule | `thalovant.native_auth.can_open_browser(environ=None, platform=None) -> bool` |
| The configuration | `NativeSignInConfig.from_dict(payload)` (raises `ValueError` when unusable): `authorization_endpoint`, `token_endpoint`, `sdk_client_id`, `device_authorization_endpoint` |
| The URL | `browser_authorization_url(authorization_endpoint, *, redirect_uri, code_challenge, state, scopes=None, device_name=None, client_id="thalovant-sdk") -> str` |
| The redirect | `loopback_redirect_uri(port) -> "http://127.0.0.1:<port>/callback"` (a port outside 1024-65535 raises `ValueError`) |
| Secrets | `new_verifier()` (64 random bytes, so 86 characters), `new_state()` (24 random bytes, so 32 characters), `challenge_for(verifier)` |
| Device name | `default_device_name()` returns `thalovant-python-sdk on <hostname>`, cut to 120 code points |
| Constants | `SDK_CLIENT_ID = "thalovant-sdk"`, `DEFAULT_BROWSER_LOGIN_TIMEOUT = 300.0`, `MAX_DEVICE_NAME_LENGTH = 120`, `LOOPBACK_CALLBACK_PATH = "/callback"` |
| What a callback means | `thalovant._loopback.classify_request(method, target, state) -> (status, Callback or None)` (private, kept apart so it can be tested without a socket) |
| Errors | `ThalovantDeviceLoginDenied` (denied), `ThalovantDeviceLoginExpired` (timeout), `ThalovantAPIError` (everything else, with the api-errors fields). There are no new error types. |

These rules apply in every language:

- **Return what your one-call device login returns.** `browser_login` returns what
  `login_with_browser` (`loginWithBrowser`) returns in your SDK: the raw token answer
  in Python, Node, Go and Kotlin (`dict`, `JsonRecord`, `map[string]any`,
  `JsonObject`), and the device-login result type in .NET and Swift
  (`DeviceLoginResult`). `interactive_login` returns the same type from both paths.
  The answer's `ai_disclosure` stays in the raw answer, where your SDK keeps one. The
  vectors do not read it, and `ApiToken` does not gain it in this round.
- **Keep the token as a device login keeps it.** Set the access token and the token
  id from the exchange answer, through the same code path as device login (Python:
  `_accept_token`). `revoke_api_token` then revokes it the same way, including the
  idempotent 401.
- **Reuse your device-login error types.** Denied is your "device login denied" and
  a timeout is your "device login expired". Python raises both with
  `status_code=None`, because the API did not answer either one.
- **Leave device login exactly as it is.** `login_with_browser`, `begin_device_login`,
  `poll_device_login`, `describe_device_login` and `revoke_api_token` keep their
  shapes, defaults and behaviour.
- **Accept a cancellation in every waiting call**: an `AbortSignal` (Node), a
  `context.Context` (Go), a `CancellationToken` (.NET), task cancellation (Swift,
  Kotlin, Rust). A cancelled sign-in closes its port.
- **Accept an injectable browser opener**: a function from URL to nothing. Tests
  need it to play the browser. Python patches `webbrowser.open`; an SDK that cannot
  patch a global takes an option.

## Behaviour

### 1. Configuration

Send `GET {api}/v1/auth/native/config` with no authorization. The API answers
something like this:

```json
{"authorization_endpoint": "https://dash.thalovant.com/authorize",
 "token_endpoint": "/v1/auth/native/token",
 "device_authorization_endpoint": "/v1/auth/device/authorize",
 "sdk_client_id": "thalovant-sdk",
 "loopback_redirect_uri": "http://127.0.0.1:{port}/callback",
 "code_challenge_methods_supported": ["S256"]}
```

- **404**: the API does not offer browser sign-in. In Python this is a
  `ThalovantAPIError` with status 404 and the body's `code` and `detail`.
- **Any other non-2xx**: the api-errors error, as usual.
- **A 2xx the SDK cannot use** is an error with **no status**, because the API did
  not refuse. The configuration is unusable when any of these holds:
  - The body is not a JSON object.
  - `authorization_endpoint` is not `https`, or `http` on `localhost`, `127.0.0.1`
    or `::1`.
  - `authorization_endpoint` has no host, carries credentials, a query (or a bare
    `?`), a fragment, whitespace or control characters, or a port no browser can
    open.
  - `token_endpoint` is not a path on the API: one leading `/`, not `//`, and no
    `:`, `?`, `#`, `\` or whitespace. A colon is refused anywhere because a URL
    joiner can read what follows the slash as a URL of its own:
    `/https://collector.example/token` and `/collector.example:443/token` leave
    the API in Python's `urljoin` once the slash is stripped.
  - `sdk_client_id` is not 1-64 characters of `[A-Za-z0-9._-]`.
  - `code_challenge_methods_supported` is not a list that contains `S256`.

  The SDK ignores the other members. It never uses `loopback_redirect_uri`: the
  redirect is fixed (below).
- **Cleartext**: check before the configuration request. If the API URL is neither
  `https` nor a loopback `http`, refuse before anything is asked or opened, because a
  code and its verifier never cross the network in cleartext. Python reuses the
  `_require_secure_token_exchange` that `complete_native_sign_in` has.

### 2. PKCE and state

- **Verifier**: 43-128 characters from `A-Z a-z 0-9 - . _ ~`. Python uses 64 random
  bytes in base64url without padding (86 characters). 32 bytes (43 characters) is
  also fine. Neither length is in the vectors.
- **Challenge**: `base64url(sha256(ascii(verifier)))`, without padding.
- **State**: at least 128 bits of OS randomness, in base64url. Python uses 24 bytes
  (32 characters).
- A fresh verifier and state for every sign-in.

### 3. The listener

- **Binding**: bind `127.0.0.1` on port `0`, **before** building the URL, because the
  redirect names the port. Never bind `0.0.0.0`, `::1`, or a name such as
  `localhost`. On Windows, set exclusive address use (`SO_EXCLUSIVEADDRUSE`, or .NET's
  `ExclusiveAddressUse = true`) so that no other process can share the port.
- **Redirect**: `redirect_uri` is exactly `http://127.0.0.1:<port>/callback`, with no
  trailing slash and no query. The API accepts only ports 1024-65535, which an
  ephemeral port always is. Refuse anything else rather than send it.
- **What it serves**: only `GET` on exactly `/callback`. Anything else is a 404: another
  path, a trailing slash, another case, `POST`, `HEAD`. The answer to a `HEAD` carries
  the headers and no body (RFC 9110); its `Content-Length` is what the page would have
  been. A request head that cannot be read (no request line, or larger than 16 KiB) is
  a 400.
- **State**: a missing `state`, one given twice, or one that is not this sign-in's is a
  **400, and the wait goes on**. A stray tab (an old attempt's, another program's, a
  page probing local ports) must not end a real sign-in. Compare in constant time, as
  UTF-8 bytes, so that a non-ASCII value is a 400 rather than a crash.
- **The first callback with the right state is accepted**, and its outcome is fixed:
  - Any `error` parameter wins, even alongside a code. `error=access_denied` (exactly
    one value) is denied. Any other error is a failed sign-in. **Keep the error value
    only when it is one of RFC 6749's codes** (`invalid_request`,
    `unauthorized_client`, `access_denied`, `unsupported_response_type`,
    `invalid_scope`, `server_error`, `temporarily_unavailable`). Anything else
    becomes `null`, because a value the page chose could be the state or a code.
  - Otherwise exactly one non-empty `code` is a code.
  - Anything else (no code, an empty code, two codes) is a failed sign-in.
  - Query values are percent-decoded once, the way a form decodes them, so
    `a%2Bb%2Fc%3D%2541` is `a+b/c=%41`. `error_description` is not read.
- **Stop listening** once a callback is accepted. A second right-state callback on a
  connection that was already open is a 400. A new connection is refused.
- **Answer the accepted tab after the sign-in has ended**, so that the page tells the
  truth:
  - `Signed in to Thalovant. You can close this tab.` once the exchange stored the
    token.
  - `Sign-in was not completed.` for denied, failed, a refused exchange, or a
    cancelled sign-in.

  The tab waits **as long as the sign-in takes**, with no deadline of its own. A
  separate deadline shorter than the exchange's HTTP timeout would report a failure
  for a sign-in that then succeeds. The wait is bounded anyway, because the exchange
  has its HTTP timeout and closing the listener (which always happens) answers the
  tab at once.
- **Headers on every answer** (200, 400 and 404): `Content-Type: text/html;
  charset=utf-8`, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`,
  `Content-Security-Policy: default-src 'none'` and `X-Content-Type-Options: nosniff`.
  The page loads nothing: no script, no style sheet, no image, no link. Python also
  sends `Connection: close` and closes each connection after one answer.
- **Closing**: when the sign-in ends (accepted, timed out, cancelled or failed), stop
  listening, give the accepted tab its page (Python waits at most 1 second for it),
  and close every other connection **without waiting for it**. Browsers open
  connections they never send anything on, and the sign-in must not wait on them.
- **Logging**: never log a request line, because it carries the code and the state.
  Turn off your server framework's access log.

### 4. The URL, the prompt and the browser

The authorization URL is `{authorization_endpoint}?` followed by these parameters,
**in this order**:

1. `response_type=code`
2. `client_id` (the configuration's `sdk_client_id`)
3. `redirect_uri`
4. `code_challenge`
5. `code_challenge_method=S256`
6. `state`
7. `scope`
8. `device_name`

Rules for the values:

- **Encoding**: every value is percent-encoded as RFC 3986 has it. `A-Z a-z 0-9 - . _
  ~` stay as they are. Every other byte of the UTF-8 form becomes `%XX` with
  uppercase hex, and a space becomes `%20`.
- **`scope`**: drop empty strings from the requested scopes and join the rest with
  one space. Leave the parameter out when nothing is left; the API then grants
  `hubs:read clients:write`. Any non-admin scope may be asked for. The API refuses
  admin or unknown scopes when the person approves.
- **`device_name`**: what the consent page and the token name show. When the caller
  gives none (null, `nil`, `None`, an absent option), use `<sdk-package-name> on
  <hostname>`: `thalovant-python-sdk on workstation`, `thalovant-node-sdk on …`,
  `thalovant-go-sdk on …`, and so on. An **empty** name is the caller's way of sending
  none: leave the parameter out, and do not put the hostname in. The chooser passes
  the same empty name to device login, which then sends no `client_name`. Cut the
  name to **120 code points**. The API refuses a longer one (422).

**Show the URL every time.** Opening a browser can fail without a word. With
`open_browser` true (the default), print this, with a line break after the colon:

```text
Opening your browser to sign in to Thalovant. If it does not open, visit:
<url>
```

Then open the browser. With `open_browser` false, print `To sign in to Thalovant,
visit:`, then the URL on the next line, and open nothing. A `prompt` callback, when
given, receives the URL and takes the print's place. It does not stop the browser
from opening.

**Open the browser without blocking the listener.** A console browser can hold the
call until it exits. Python opens it on a daemon thread and ignores failures. Use the
platform default:

- macOS: `open <url>`.
- Linux: `xdg-open <url>`.
- Windows: `ShellExecute`, or `rundll32 url.dll,FileProtocolHandler <url>`. **Never**
  `cmd /c start <url>`: the `&` in the query splits the command line.

### 5. The exchange

Send `POST {api}{token_endpoint}` with a JSON body of **exactly**
`{"code", "code_verifier", "client_id", "redirect_uri"}`. `redirect_uri` is the same
string the URL carried, and `client_id` is the configuration's.

Before sending, resolve the URL the way your HTTP client will, and refuse it, with an
error with no status, unless its origin is the API's. An origin is the scheme, the
host and the port. The configuration rule already refuses such an endpoint; this is
the last word before the code and the verifier go on the wire.

| Answer | Outcome |
|---|---|
| 2xx with a JSON object that has an `access_token` | approved: keep the token and its `token_id` |
| 2xx that is not a JSON object, or has no `access_token` | an error with **no status** (as device login reads its token answer). This holds even if your HTTP helper would attach the 200. |
| 400 `{"detail": "invalid_grant"}` | the api-errors fields: status 400, code null, detail `invalid_grant` |

The API gives the same answer for every way an exchange can fail. A code is
single-use and lives 60 seconds, so **never retry an exchange**: presenting a code
twice revokes the token the first exchange minted (RFC 9700).

### 6. Timeout

No accepted callback within `timeout` (default 300 seconds) is **expired**. Measure
the timeout on a monotonic clock, and never wake early: if a timer fires before the
deadline, wait again for what is left. A wrong-state request neither ends the wait
nor restarts it.

### 7. Secrets in errors

The code, the verifier, the state and the token never appear in an error's message,
its string form, or its problem body.

An API that echoes a request back, as a FastAPI 422 lists each field's `input`, would
put the code and the verifier in the problem. Python handles this in three steps:

1. **Redact the answer before any message is made of it.** The message is cut to
   200 characters, and a cut can leave a prefix of a secret that a later
   replacement cannot match.
2. **Replace each secret with `[redacted]`** in the message and anywhere in the
   problem, keys included. Do it in one pass, longest value first, and never inside
   a `[redacted]` already there.
3. **Raise a new error**, never the original. It has no cause and no context:
   a transport's own exception keeps the request, and a requests one keeps its
   body (the code and the verifier) on `.request`. Python raises the new one
   outside the handler, `from None`.

### 8. The chooser

`interactive_login(method=...)` picks the method:

- `"device"`: device login, and the configuration is never requested. Pass
  `device_name` as the device login's `client_name`.
- `"browser"`: browser sign-in, whatever the machine. A configuration failure (404
  included) is raised. It never becomes a device login.
- `"auto"` (the default): browser sign-in when **both** of these hold:
  1. The machine can show a browser (`can_open_browser`):
     - never when `CI`, `SSH_CONNECTION` or `SSH_TTY` is set;
     - otherwise always on `macos` and `windows`;
     - anywhere else (Linux, the BSDs) only when `DISPLAY` or `WAYLAND_DISPLAY` is set.

     A variable counts as set when it has a non-empty value. `CI=false` is set, and
     an empty `DISPLAY` is not.
  2. The configuration is usable. Ask for it only when the first condition holds.

  Otherwise use device login. A 404, another failure, an unusable answer and a
  cleartext API URL all count as "not usable". Nothing has been opened yet, so this
  is not a downgrade.
- **Never fall back after the browser was opened.** Once the URL is shown, the browser
  sign-in's own outcome stands, whether denied, expired, or a refused exchange.
- `timeout=None` means each method's own default: 300 seconds for the browser and 900
  for device login. Device login's own timeout error is unchanged; only browser
  sign-in raises expired on a timeout.

On a platform the rule does not name, decide which name it maps to. **Android** is
`linux` without a display, so `auto` picks device login. The app signs in as
`thalovant-android` through its own flow. **iOS** is neither `macos` nor `windows`,
so `auto` picks device login. The app signs in as `thalovant-ios` with
`ASWebAuthenticationSession`. Do not advertise a forced browser sign-in on either.

## The vectors

`contracts/conformance/browser-login-vectors.json`, with top-level members:

| Member | What it is |
|---|---|
| `client_id`, `redirect_uri`, `default_timeout_ms`, `device_name_max_code_points` | Constants |
| `verifier`, `state` | The rules a generated verifier and state must meet (length, alphabet, randomness) |
| `pages`, `page_headers`, `prompt` | The two page texts, the headers on every listener answer, and the two printed prompts |
| `message_excludes`, `message_excludes_generated` | Synthetic secrets that must never reach an error, plus "the verifier and the state your SDK generated" |
| `placeholders` | `{port}`, `{state}`, `{challenge}` and `{verifier}`: what your runner substitutes, and what it must prove first |
| `record_key`, `results` | How to name a recorded case (`<section>/<name>`), and the shape of every `result` (the table below) |

The cases are in eight sections. Record each one under **`<section>/<name>`**:

| Section | Run it against | Produced (must equal `expect`) |
|---|---|---|
| `pkce` (3) | your challenge function. The first case is RFC 7636 Appendix B. | `{"challenge"}` |
| `redirect_uris` (6) | your redirect builder | `{"redirect_uri"}` or `{"error": "invalid_port"}` |
| `authorization_urls` (9) | your URL builder, with the case's fixed `code_challenge` and `state` | `{"url"}`, compared as an exact string |
| `config` (18) | your configuration parser | `{"usable": false}` or `{"usable": true, "authorization_endpoint", "token_endpoint", "sdk_client_id"}` |
| `environment` (13) | your browser rule, with the case's `platform` and `env` | `{"browser": bool}` |
| `callbacks` (26) | your callback classifier, with the case's target and `{state}` replaced by a fixed state | `{"status", "outcome": "ignored"}`, `{"status": 200, "outcome": "code", "code"}`, `{"status": 200, "outcome": "denied"}` or `{"status": 200, "outcome": "failed", "error"}`. Python also runs every case through a real listener. |
| `sign_ins` (18) | `browser_login`, for real (below) | `{"authorization_url", "browser", "closed", "exchange", "result"}`. The first four are present only when the SDK got that far. |
| `chooser` (13) | `interactive_login`, for real, on the case's `platform` and `env` | `{"method": "browser" or "device", "result"}` |

Every result has one of these shapes (the file's `results` member says the same):

| Result | Shape |
|---|---|
| approved | `{"outcome": "approved", "token_type", "scopes", "expires_at", "token_id"}` |
| revoked | `{"outcome": "revoked"}` |
| denied | `{"outcome": "denied", "status": null}` |
| expired | `{"outcome": "expired", "status": null}` |
| error | `{"outcome": "error", "status", "code"}`, plus `"detail"` when `status` is not null |

How to run `sign_ins` and `chooser` (Python: `tests/test_browser_login_vectors.py`):

1. **Serve the API on loopback.** Serve the case's `exchanges` from a loopback HTTP
   server, in order, and check every request: method, path, `json` and
   `authorization`. Answer each with the case's status, content type and body. A
   request the case does not list is a mismatch. A device-login fallback is caught
   exactly this way.
2. **Get the URL.** In `sign_ins`, pass a `prompt` and `open_browser: false`. In
   `chooser`, let the SDK open its "browser" (your injected opener) and take the URL
   it was given. Ignore the device-login verification URL, which your opener also
   receives.
3. **Read the URL.** Take the port, the state and the challenge from it. Check that
   the state matches the `state` rule, that the challenge is 43 base64url
   characters, and that `redirect_uri` is `http://127.0.0.1:<port>/callback`. Record
   `{"endpoint": scheme://host/path, "query": [[key, value], ...]}` in the URL's
   order, with the actual port, state and challenge put back as `{port}`, `{state}`
   and `{challenge}`.
4. **Play the browser.** Send each of the case's `requests` on its own connection, as
   raw HTTP, with `{state}` replaced. Check every answer's `page_headers`. Record
   `{"status"}`, plus `"page": "signed_in"` or `"not_completed"` for a 200, decided
   by which `pages` text the body contains.
5. **Check the exchange.** When the token request arrives, check that it carries
   exactly the four fields, and that `code_verifier` is 43-128 unreserved characters
   whose S256 challenge is the URL's. Only then replace it with `{verifier}` and put
   the port back as `{port}`. That is the `exchange` record.
6. **Record the result.** Wait for the sign-in to end. Check that no error carries a
   `message_excludes` value, the verifier or the state, and record `result`. For an
   approved sign-in, also check that the client now holds that access token and
   token id. `"then": "revoke"` revokes twice; the second revoke must send nothing.
7. **Check the port.** `closed` is whether a new connection to the port is refused
   after the sign-in ended.
8. **Run both forms.** Run sync and async (or your SDK's equivalent forms), and record
   one of them.

In `chooser`, `method` is `"device"` exactly when the SDK sent `POST
/v1/auth/device/authorize`. Set the case's `env` exactly: clear `CI`,
`SSH_CONNECTION`, `SSH_TTY`, `DISPLAY` and `WAYLAND_DISPLAY`, set the case's
variables, and make your platform detection answer the case's `platform`. In the
device cases the device token answers on the first poll (`interval` 0).

Durations in the cases are whole milliseconds (`timeout_ms`). The expired cases use
400 ms.

## What changed since `49744ee385bf`

A port of the PR's first head (accepted as `49744ee385bf…`) needs these changes. The
vectors file changed, so re-vendor it (98 to 106 cases) and re-record.

| Change | Vector cases |
|---|---|
| A `token_endpoint` with a `:` anywhere is unusable, and the exchange refuses a URL off the API's origin | `config/a token endpoint that is another URL behind its slash is unusable`, `config/a token endpoint with a colon anywhere is unusable`, `sign_ins/a token endpoint that would leave the API is refused before anything is opened` |
| Only RFC 6749's error codes are kept from a callback; anything else is `null` | `callbacks/an error outside RFC 6749's codes fails the sign-in and is not kept`, `callbacks/the state repeated as the error is not kept`, `sign_ins/an error that repeats the state never reaches the error` |
| Only a missing `device_name` gets the default. An empty one is left out of the URL, and the device login sends no `client_name`. | `sign_ins/an empty device name keeps the hostname off the consent page`, `chooser/an empty device name is not sent to a device login either` |
| A `HEAD` answer carries no body | none (Python: `test_a_head_request_gets_the_headers_and_no_body`) |
| The accepted tab waits until the sign-in ends, with no separate deadline | none (`test_the_accepted_tab_waits_as_long_as_the_sign_in_takes`) |
| The exchange's answer is redacted before its message is built, and the error raised is a new one with no cause or context | none (`test_a_message_cut_to_length_keeps_no_part_of_a_secret`, `test_a_transport_failure_during_the_exchange_carries_nothing_of_the_request`) |

## Parity acceptance

The reference digest moves.

| Reference | Digest |
|---|---|
| Current (`main`, 0.10.0) | `57c7e71f6456344877b34bb3093399f813c7a21fde3ae14f48c8b3fa04be43b9` |
| Candidate (this change, after the review) | `50af6731e276caf72d0eac51681466579f4ce9fff2596714035663c2693f4c9c` |
| Superseded, never published | `49744ee385bfce57439dadbef1e20e6116f72a6817649843adba7a450d2fc56b` (the PR's first head; replace it) |

The candidate is the digest of `python scripts/check-sdk-parity.py --snapshot` on the
Python PR's head. It moves again if that PR changes anything under `src/` or
`contracts/conformance/` before it merges. Re-read it from the PR before you record
it. It is also the last entry of that PR's `reference_history`. `--snapshot` prints
the snapshot itself; its digest is computed the way the checker computes it:

```bash
python scripts/check-sdk-parity.py --snapshot | python -c 'import hashlib, json, sys; print(hashlib.sha256(json.dumps(json.load(sys.stdin), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest())'
```

Each consumer's `contracts/sdk-parity.json`:

- **`reference_digests`** keeps the current digest and adds the candidate, so that
  your PR is green before and after the Python merge. Drop the old one in a later
  change, once Python's `main` carries the candidate.
- **`capabilities["browser-login"]`**:

  ```json
  {"status": "required",
   "reason": "<what you implemented and how the tests run the vectors>",
   "implementation": {"<file>": "<sha256>", "...": "..."},
   "tests": {"<test file>": "<sha256>", "...": "..."},
   "vectors": {"browser-login-vectors.json": "<where you vendored it>"}}
  ```

  - `vectors` names where you keep the file. It is compared as parsed JSON, so
    formatting is free.
  - A test file must pass the stem `browser-login-vectors` to a call, for example
    `load("browser-login-vectors.json")`.
  - Tests must be separate files from the implementation.
  - embedded-c records `{"status": "not-applicable", "reason": "..."}` with the
    manifest's reason.
- **`contracts/conformance-results.json`**: run your suite with
  `THALOVANT_CONFORMANCE_OUT` and commit the record. Every case digest is the
  SHA-256 of the canonical JSON described in
  [Recording results canonically](home-link-porting.md#recording-results-canonically).
  The file digest is that of the parsed vector file.
- **Order**: consumers merge and publish first, and the Python producer merges last,
  after its whole-workspace gate passes. Never refresh a hash to turn a check green.
  Re-run the suite and record what it produced.

A consumer can check itself against the Python PR's head before the merge:

```bash
python <python-sdk>/scripts/check-sdk-parity.py --workspace <checkouts> --consumer thalovant-node-sdk
```

## Per language

- **Node (TypeScript).**
  - API: `browserLogin(options?: BrowserLoginOptions): Promise<JsonRecord>` and
    `interactiveLogin(options?: InteractiveLoginOptions)`, with `scopes`,
    `deviceName`, `openBrowser`, `timeoutMs`, `prompt`, `signal`, `method` and an
    `opener`.
  - Listener: `http.createServer()` and `server.listen(0, "127.0.0.1")`. Call
    `server.close()` on acceptance, and `server.closeAllConnections()` (Node 18.2+)
    at the end so that idle keep-alive sockets do not hold it.
  - Encoding: `encodeURIComponent` leaves `! ' ( ) *` alone, so encode them as well:
    `.replace(/[!'()*]/g, c => "%" + c.charCodeAt(0).toString(16).toUpperCase())`.
  - Code points: `Array.from(name).slice(0, 120).join("")`.
  - State: compare with `crypto.timingSafeEqual` on `Buffer.from(…, "utf8")`, after
    checking the lengths match (it throws on unequal lengths).
- **Go.**
  - API: `(c *ControlPlane) BrowserLogin(ctx, BrowserLoginOptions) (map[string]any,
    error)` and `InteractiveLogin(ctx, InteractiveLoginOptions)`, with `LoginMethod`
    constants and `CanOpenBrowser(env map[string]string, platform string) bool`.
  - Listener: `net.Listen("tcp", "127.0.0.1:0")` with an `http.Server` whose
    `ErrorLog` discards. Use `Shutdown` with a short context once the page is
    written.
  - Encoding: `url.QueryEscape` writes a space as `+`, so use
    `strings.ReplaceAll(url.QueryEscape(v), "+", "%20")`. A literal `+` is already
    `%2B`.
  - Code points: `if r := []rune(name); len(r) > 120 { name = string(r[:120]) }`. A
    bare `[]rune(name)[:120]` panics when the name is shorter.
  - State: `subtle.ConstantTimeCompare`.
- **Rust.**
  - API: `async fn browser_login(&self, options: BrowserLoginOptions)` and
    `interactive_login`, on the runtime the SDK already uses.
  - Listener: a `tokio::net::TcpListener` bound to `127.0.0.1:0`, with a small HTTP
    reader. hyper is fine too, without its tracing of request URIs.
  - Encoding: `percent_encoding` with an `AsciiSet` of everything except `A-Za-z0-9
    -._~`. Not `form_urlencoded`, which writes `+`.
  - Code points: `name.chars().take(120)`.
  - State: `subtle::ConstantTimeEq`. Record the colocated test module as test
    evidence, as `contracts/README.md` says.
- **Kotlin (JVM desktop and server).**
  - API: `suspend fun browserLogin(options: BrowserLoginOptions = …): JsonObject` and
    `interactiveLogin`.
  - Listener: bind `InetAddress.getByName("127.0.0.1")`, not
    `getLoopbackAddress()`, which can answer `::1`. The JDK `HttpServer` works, or a
    `ServerSocket` with a small reader.
  - Encoding: `URLEncoder.encode` writes `+` and `%7E`, and leaves `*`. Encode the
    bytes yourself.
  - Code points: `name.codePoints().limit(120)`.
  - State: `MessageDigest.isEqual`.
  - Browser: open it with `Desktop.browse` where `Desktop.isDesktopSupported()`, else
    the platform command. On Android the platform is `linux` with no display (see
    the chooser).
- **Swift (macOS and Linux).**
  - API: `func browserLogin(options: BrowserLoginOptions = …) async throws ->
    DeviceLoginResult` and `interactiveLogin`.
  - Listener: Network.framework (`NWListener` on `127.0.0.1`) or a POSIX socket on
    Linux.
  - Encoding: `.urlQueryAllowed` leaves `& = + ? /` unescaped, so build a
    `CharacterSet` of the unreserved characters only.
  - Decoding: `URLComponents.queryItems` does not turn `+` into a space. The vectors
    only use `%2B`, but decode the way a form does.
  - Code points: `name.unicodeScalars.prefix(120)`. `String.count` counts grapheme
    clusters, which is not the API's limit.
- **.NET.**
  - API: `Task<DeviceLoginResult> BrowserLoginAsync(BrowserLoginOptions? options =
    null, CancellationToken cancellationToken = default)` and `InteractiveLoginAsync`.
  - Listener: a `TcpListener(IPAddress.Loopback, 0)` with `ExclusiveAddressUse =
    true` and a small reader. `HttpListener` needs URL ACLs on Windows.
  - Encoding: `Uri.EscapeDataString` is RFC 3986; avoid `HttpUtility.UrlEncode`,
    which writes `+` and lowercase hex.
  - Code points: enumerate `Rune`s.
  - State: `CryptographicOperations.FixedTimeEquals`.
  - Timing: measure the wait with `Stopwatch`, because `Task.Delay` can wake early.
- **embedded-c.** Not applicable. A caller that runs its own loop can follow the
  vectors with its own sockets.

### MCP

As with device login, a tool call cannot wait for a person, so browser sign-in
becomes tools:

- `thalovant_begin_browser_login`:
  - binds the listener and builds the URL;
  - opens the browser when the server can;
  - answers the URL and an opaque `loginId` bound to the principal.
- `thalovant_poll_browser_login`: answers `pending`, `approved`, `denied`, `expired` or
  `error`. The token is kept server-side for that principal and never returned, as
  device login's is.
- Sign-out is the existing revoke.

The platform boundary is the **transport**. Over stdio the server runs on the
person's computer, and the loopback redirect reaches it. Over Streamable HTTP, a
hosted server's `127.0.0.1` is not the person's computer. There the begin tool
answers that browser sign-in needs a local server, and the interactive sign-in uses
device login: the chooser's "can show a browser" is false.

Run the vectors as for device login:

- through `@thalovant/sdk`'s `browserLogin` and `interactiveLogin`;
- through the tools over stdio, recorded under the same case names.

The API's impact record also asks for a dependency bump to the Node SDK release and a
README line.

## The Python reference

| Behaviour | Python |
|---|---|
| sign in | `AsyncThalovantControlPlane.browser_login`, `ThalovantControlPlane.browser_login` (`src/thalovant/control.py`) |
| configuration | `_native_sign_in_config` (control.py), `NativeSignInConfig.from_dict` (`src/thalovant/native_auth.py`) |
| URL, redirect, state, browser rule, device name | `browser_authorization_url`, `loopback_redirect_uri`, `new_state`, `can_open_browser`, `default_device_name` (native_auth.py) |
| listener | `LoopbackListener`, `classify_request`, `response_bytes` (`src/thalovant/_loopback.py`) |
| exchange | `_exchange_browser_code`, `_native_token_request` (control.py) |
| secrets out of errors | `_without_secrets` (control.py) |
| chooser | `interactive_login` (control.py) |
| tests | `tests/test_browser_login_vectors.py` (the vectors), `tests/test_browser_login.py` (prints, opener thread, idle connections, oversized heads, cancellation, the 422 echo) |
