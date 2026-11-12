Last Edit: Claude Sonnet 5.5 - 2026-11-11 - Motive: Intercom final API (platform security review): urgent scope, speaker kind/can_play/policy_synced, typed errors, one text rule, DND timezone, replayed; new parity digest.

# Maintenance Report

## 2026-11-11: Intercom final API (platform security review)

Aligned the Python reference with the final Intercom API (`thalovant-api` `feature/intercom-console`, `CONSOLE_API_NOTES.md` section 9, contract sections I and J). **Models** (`src/thalovant/intercom.py`): `IntercomSpeaker` gains `kind`, `can_play` and `policy_synced`; `IntercomSpeakerList` gains `policy_synced` and `unavailable_reason` (now also `platform`); `IntercomDelivery` gains `detail`; `Announcement` gains `replayed`. **Scope** `intercom:urgent` is documented everywhere the scopes are listed (guide, API reference, README, QUICK_FACTS, FAQ, porting brief) with constants `INTERCOM_READ_SCOPE`, `INTERCOM_SEND_SCOPE`, `INTERCOM_URGENT_SCOPE`. **Errors** (`src/thalovant/errors.py`, `_INTERCOM_ERROR_KINDS` in `src/thalovant/control.py`): `IntercomProblemCode` and a typed error for each of `urgent_scope_required`, `intercom_urgent_busy`, `skill_not_installed`, `not_a_speaker`, `reserved_name`, `hub_locked`, `impersonation_not_allowed`, `too_many_targets`, `intercom_unavailable`, `intercom_disabled_platform`, `intercom_not_configured`, `managed_client`; `ThalovantNotCancellableError.announcement_status`; the code decides the kind whatever the status. **Validation**: `announcement_text()` now applies the API's rule (NFC, trimmed, 1-280, no `<` `>`, Cc/Cf/Cs/Co/Zl/Zp refused except the two joiners); quiet hours on need a valid IANA timezone and `start != end` (`_timezone()`, `IntercomDnd.as_dict()`); unknown `dnd` keys are refused; target lists are capped at 50 names of 64 characters; `lang` is checked; `idempotency_key()` caps the key at 128 printable ASCII characters. **`announce`** reads the `Idempotency-Replayed` header through the new `_request_with_headers` and keeps `replayed` after `wait`.

Verification: `python -m pytest -q` on Python 3.12, 1828 passed, 1 skipped (the earlier 1730 plus 98 new, all in `tests/test_intercom.py`; one earlier run under heavy machine load failed 6 timing tests that pass alone and in the clean run); `ruff check src tests scripts` and `mypy` clean; `scripts/check-sdk-parity.py` verifies the reference. The reference digest is `69badc4fb072703e675ce50ae3a09765bf49cbf65efcc2fef5befb46f2b32a8c`; Node, Go, Rust, MCP, Swift, Kotlin, .NET and embedded-c acknowledge it in their own parity files.

Not verified against a live API: the loopback fake API is built from the producer's notes and the API source (`app/schemas/intercom.py`, `app/routes/intercom.py`).

Transparency Report: AI model Claude Sonnet 5.5; actions: implemented the final API for the Python SDK (code, tests, docs, parity reference); oversight: none yet, review pending.

## 2026-11-11: Intercom amendment 2 (client-side security review, contract section J)

Added `cancel_announcement` (sync and async; `POST .../announcements/{id}/cancel`), `Announcement.hub_confirmed` (optional), the `cancelled` status, `ThalovantNotCancellableError` (409 `not_cancellable`) and `ThalovantHubUnreachableError.announcement_id`. `announce` sends its POST with a timeout of at least 25 s (`INTERCOM_ANNOUNCE_MIN_TIMEOUT`, `_request(timeout=...)`), because the API waits up to 10 s for the hub (review M2). New `thalovant.intercom.intercom_id` refuses `.`, `..`, empty ids and ids holding `/`, `?`, `#`, a backslash or a control character before any request, for hub, client and announcement ids (review L1); `_intercom_path` and `_intercom_id` in `src/thalovant/control.py` use it. Docs: every example uses a fresh `uuid4` key; the guide, README, FAQ, api-reference and porting brief say that the same key replays the stored result and never re-sends, that a replayed `failed` stays `failed`, that a deliberate second send needs a new key, and to read the announcement after a 502 (review H3); the wording no longer says "nothing listens" or "stops every announcement at once" (contract J7). Tests (`tests/test_intercom.py`, 108 now): cancel route/body/scope/409, `hub_confirmed` parsing, 502 `announcement_id`, the timeout floor (a spy on the sender, and a slow loopback reply), unsafe ids on every call (sync and async), a replayed failed announcement, and two doc guards (no literal idempotency key in README or docs; no over-promising phrases). Verification: full suite, `ruff`, `mypy` and `scripts/check-sdk-parity.py`; the reference digest moved, see `contracts/sdk-parity.json`. Not done (not in section J): review L5 and L6, a `replayed` flag (`SUGGESTIONS.md`).

Transparency Report: AI model Claude Sonnet 5.5; actions: implemented contract section J for the Python SDK (code, tests, docs, parity record); oversight: none yet, review pending.

## 2026-11-11: Intercom review fixes (CodeRabbit, PR 114)

`announce(wait=True)` validates `wait_timeout` and `poll_interval` (finite numbers; a bool, string, NaN, infinity or a negative `wait_timeout` is refused) before the POST, and the deadline now bounds each status read (`asyncio.wait_for` on the remaining time) and starts no read once the sleep used up the wait. Target lists must be sequences of names, never a bare string (`{"speakers": "ab"}` is refused, not split), and a speaker's `dnd` needs a real boolean `enabled` (an invalid or missing one raises `ValueError`; the API replaces the whole DND object and stores a missing `enabled` as false). Docs: `wait=True` needs `intercom:read` too and a poll can fail after the POST (reuse the idempotency key), 402 is listed with 403 `plan_limit`, and the 502 `hub_unreachable` docstring no longer says a retry is safe. Tests: `tests/test_intercom.py` asserts status codes separately from optional codes, plus new cases for each change. Verification: the full suite, `ruff`, `mypy` (only the missing PyYAML stubs of this environment remain) and `scripts/check-sdk-parity.py`; the reference digest moved, see `contracts/sdk-parity.json`.

Transparency Report: AI model Claude Sonnet 5.5; actions: fixed the review findings, added tests, refreshed the parity reference; oversight: none yet, review pending.

## 2026-11-11: Intercom (0.12.0)

Added `thalovant.intercom` and seven control-plane methods (sync and async) against section B of the intercom contract, three typed errors, the `intercom` parity capability, docs (`docs/guides/intercom.md`, `docs/reference/intercom.md`, `docs/intercom-porting.md`, api-reference, README, QUICK_FACTS, FAQ, CHANGELOG). Verification: `tests/test_intercom.py` (loopback fake API), the full suite, and `scripts/check-sdk-parity.py` (see SDK_NOTES for totals). The API was built in parallel: the announcement-list shape (`items`/`next_cursor`) is an assumption, accepted with `data`/`cursor` too.

Transparency Report: AI model Claude Sonnet 5.5; actions: wrote the module, methods, tests, docs and parity record from the contract; oversight: none yet, review pending.

## 2026-11-08: Final producer revision of #113, from the consumer ports

While fixing their own review threads, the Node (#110), Go (#90), .NET (#54) and Rust ports found gaps in the reference. The lead passed them on. This revision folds them in and gives the final candidate.

| From | Finding | Outcome |
| :--- | :--- | :--- |
| Node | A path joined with `urljoin(api, path.lstrip("/"))` can leave the API's origin | Fixed in the request layer. `_send` refuses every request whose resolved origin (scheme, host, port) is not the API's. Every caller passes an API path, ids quoted and `links.self` reduced to an id, so nothing legitimate is refused. Test: `test_no_request_leaves_the_api_origin`. There is no vector: the configuration rule refuses such a `token_endpoint` first, so no browser-login case can reach the request layer. |
| Node | `authorization_endpoint` on port 0 was usable | Fixed. Config vector added. |
| Node | A callback `error` that contains the state | Already covered by the RFC 6749 allow-list. Pure case added: `callbacks/an error that contains the state is not kept`. |
| Node | In `auto`, fall back to device login when the listener cannot bind | Not adopted. Binding `127.0.0.1:0` on a machine with a browser essentially never fails. When it does, the loopback stack is broken, and a silent device login would hide it. Every port would also need bind-failure injection. Recorded in the porting brief. |
| Node | `interactive_login` has no `prompt` | Not changed, as agreed. It needs a design decision (a URL for the browser, a code for the device). Recorded in the porting brief. |
| Go | A broken percent-escape in `token_endpoint` (`/v1/%zz`) | Fixed. Every `%` must be followed by two hex digits, so `auto` falls back before the consent page. Three config vectors. |
| Go | The tab's 60-second bound, which the first brief stated | The brief's "What changed since 49744ee385bf" now says outright: drop it, and the tab waits until the sign-in ends. |
| Go | HTTP/2 can replay the exchange's body | One paragraph in the brief: check for transport-level retries of the exchange. aiohttp (HTTP/1.1) never resends a POST. A caller's own requests session would resend one only with a retry policy that allows POST. |
| .NET | A host that will not parse to a real hostname (`https://%/authorize`) | Fixed. `_real_host` accepts only these: IPv6 between brackets; IPv4, whenever the last label is numeric; or ASCII letter, digit and inner-hyphen labels (`xn--` for international names, at most 253 characters). Eight config vectors, three of them usable. |
| Rust | A connection closed with bytes unread resets, and macOS loses the answer (an oversized head's 400) | Confirmed on Linux too: from a 200 KB head the 400 arrives but the connection ends in `ECONNRESET`, and a 404 left with an unread POST body does the same. Fixed in `_answer` (`_loopback.py`): after every answer the listener shuts its write side, then reads and drops what the browser still sends until it closes, at most 2 s / 1 MiB. Tests: `test_an_early_answer_is_not_lost_to_a_reset` (oversized head, unread body; both fail with the drain removed) and `test_draining_a_connection_is_bounded`. No vector: this is transport behaviour. The brief has a "Drain before closing" rule. |
| CodeRabbit (on `fafc96d`) | A percent-encoded secret in an error field would not be scrubbed | Answered, no change to `src/`. Every hidden value consists only of RFC 3986 unreserved characters: the verifier and the state are base64url, and the API mints codes with `token_urlsafe`. Their percent-encoded form is the raw form, which is matched. The API never percent-encodes an error: its 400 is the fixed `invalid_grant`, and FastAPI's 422 echoes raw JSON, which is scrubbed after parsing. Pinned by `test_every_secret_is_written_the_same_way_percent_encoded`. |
| .NET | "Cannot be relayed" is too absolute | Reworded in the README, the brief, the docstrings, the CHANGELOG and the manifest's capability description. The claim is now: a link or code sent to someone on another computer delivers nothing to the sender. A forwarded callback port or another user of the same computer can still complete a sign-in (AUDIT.md). |

**Vectors and digest:**

- `browser-login-vectors.json` has 119 cases.
  - The first head had 98: the review round added 8 and this round 13.
  - The 98 original cases still record identical results.
- The file's sha256 is `b0c73f808c4f26328c27dd4d04eebfbbc3e7c0c81b8d63a666f63737d0259e73`.
- The final candidate reference digest is `6dfb3bb31ac7590591febef1c4d8372984ca6cdad4700381f869db30c38452df`.
  - `reference_history` ends `57c7e71f…, 6dfb3bb3…`.
  - The intermediate candidates `49744ee385bf…`, `50af6731e276…`, `0bb293e23686…` and `1374f68fee6c…` were never published and are not kept.

**Verification:**

- Python 3.12, `python -m pytest -q --cov=thalovant` with `THALOVANT_CONFORMANCE_OUT`: 1621 passed, 1 skipped. Total coverage is 90%; `native_auth.py` 100%, `_loopback.py` 98% (the drain's defensive error returns). The 106 cases recorded before this round still record identical results.
- Python 3.10 at CI's floors (`aiohttp==3.11.0`, `cryptography==48.0.1`, `THALOVANT_MINIMUM_VERSIONS=1`): 1574 passed, 1 skipped.
- Python 3.14: 1621 passed, 1 skipped. `mkdocs build --strict` passes. The live config fetch, URL build and `invalid_grant` exchange against `https://api.thalovant.com` pass on the final code.
- `mypy`, `ruff`, `scripts/check-sdk-parity.py` and `scripts/generate-sdk-conformance.py --check` pass, and `--snapshot` digests to the final candidate.
- Each of the seven new rules was reverted in turn, and every revert failed a test: port 0, the broken escape, the request-layer origin, the host check, the numeric-label check, the label pattern and the drain.

### Transparency Report

- **AI Model:** Claude Opus 5.5 (Claude Code agent).
- **Actions Taken:**
  - Checked each consumer finding against the reference, and every `_send`/`_request` caller before guarding the request layer.
  - Wrote the fixes, tests and vectors.
  - Mutation-checked the new rules.
  - Re-ran the suites and the gate.
  - Updated the brief, the docs and the records.
- **Oversight:** Done at the request of the browser sign-in train's lead, who relays the final digest to the six ports. The merge stays with the lead, after the consumers.

## 2026-11-08: CodeRabbit review of #113 (browser sign-in)

CodeRabbit left nine threads on #113. Eight are fixed, with tests. One is answered without a change.

| # | Thread | Outcome |
| :--- | :--- | :--- |
| 1 | `docs/browser-login-porting.md`: the Go rune slice can panic | Fixed. The Go line bounds the slice by its length. Docs only. |
| 2 | `_loopback.py`: HEAD answers carry a body | Fixed. `response_bytes(head=True)` sends headers only. `Content-Length` is what a GET would carry. |
| 3 | `_loopback.py`: the tab reports failure at 60 s while the exchange may still succeed | Fixed. The tab has no deadline of its own and waits until the sign-in ends. The exchange's HTTP timeout and `close()` bound that wait. |
| 4 | `control.py`: session credentials on the config and token requests | Not changed. `auth=False` means "no SDK credential", as for device login. A caller's own session header goes only to this API's origin, and neither route authenticates with it. Stripping it would make browser sign-in unlike device login and the other SDKs, and aiohttp cannot do it per request. |
| 5 | `control.py`: `device_name=""` sends the hostname | Fixed. Only `None` gets the default. An empty name is left out of the URL, and `interactive_login` sends device login no `client_name`. |
| 6 | `control.py`: a callback `error` can repeat a secret | Fixed. `classify_request` keeps only RFC 6749's error codes (`OAUTH_ERRORS`). |
| 7 | `control.py`: truncation leaves a secret prefix | Fixed. `_redacted_response` scrubs the answer before `_api_error` builds the message. `_scrub` makes one pass, longest value first, and never edits inside `[redacted]`. |
| 8 | `control.py`: a transport error keeps the request body in `__context__` | Fixed. `_without_secrets` always builds a new error, raised outside the handler: no cause, no context. |
| 9 | `native_auth.py`: the token endpoint can leave the API origin (Major) | Fixed. A colon anywhere in `token_endpoint` makes the configuration unusable. `_exchange_browser_code` also refuses a resolved URL off the API's origin. |

**Behaviour that changed for the consumers** (`docs/browser-login-porting.md`, "What changed since 49744ee385bf"):

- the token-endpoint colon rule and the origin check;
- the RFC 6749 error allow-list;
- the empty `device_name`;
- a `HEAD` without a body;
- the tab's wait;
- redaction before the message is built, and a new error with no context.

**Vectors and digest:**

- `browser-login-vectors.json` grows from 98 to 106 cases:
  - `config`: 2 more;
  - `callbacks`: 2 more;
  - `sign_ins`: 3 more;
  - `chooser`: 1 more.
- The vectors file's sha256 changes from `9058adf2ad30fd8b7f255882ce718865170d0d4b84d36f8fbfa23bde51d1651e` to `deac6fb8d4f5e999194a8dd47777278921035cf806ab509790de8874a08e77c5`.
- The candidate reference digest changes from `49744ee385bfce57439dadbef1e20e6116f72a6817649843adba7a450d2fc56b` (never published, so removed from `reference_history`) to `50af6731e276caf72d0eac51681466579f4ce9fff2596714035663c2693f4c9c`, itself superseded by the final revision above.

**Verification:**

- Python 3.12, `python -m pytest -q --cov=thalovant` with `THALOVANT_CONFORMANCE_OUT`: 1599 passed, 1 skipped (1579 before the review, plus 20 new). Coverage is 90% in total, and 100% for `_loopback.py` and `native_auth.py`. The 98 original browser-login cases record identical results; 8 are new.
- Python 3.10 at CI's floors (`aiohttp==3.11.0`, `cryptography==48.0.1`, `THALOVANT_MINIMUM_VERSIONS=1`): 1552 passed, 1 skipped.
- Python 3.14: 1599 passed, 1 skipped. `mkdocs build --strict`, `python -m build` and `twine check` pass.
- `mypy` and `ruff` pass. `scripts/check-sdk-parity.py` verifies the reference, and `--snapshot` digests to the new candidate.
- Mutation check: each of the ten review fixes was reverted in turn, and every revert failed at least one test.
- Live check against `https://api.thalovant.com`, now that API #501 is deployed:
  - `GET /v1/auth/native/config` answers 200 with exactly the contract's six members, and `NativeSignInConfig.from_dict` accepts them.
  - The authorization URL built on a bound loopback port has the contract's parameters in order, and `https://dash.thalovant.com/authorize?...` answers 200 HTML.
  - `POST /v1/auth/native/token` with a made-up code answers `ThalovantAPIError` 400, code `None`, detail `invalid_grant`, with no secret in the message and no token kept.
  - No sign-in was completed; that needs a person in the browser.

### Transparency Report

- **AI Model:** Claude Opus 5.5 (Claude Code agent).
- **Actions Taken:**
  - Read the nine threads.
  - Confirmed each finding against the code, and the API's rate-limit middleware for thread 4.
  - Wrote the fixes, the tests and the new vector cases.
  - Mutation-checked the fixes, re-ran the suites, the gate and the live API checks.
  - Answered and resolved every thread.
- **Oversight:** Done at the request of the browser sign-in train's lead. The merge stays with the lead, after the consumers.

## 2026-11-08: Browser sign-in (0.11.0)

The owner's goal is that a sign-in can be approved only by whoever started it, with no friction. A device code can be relayed to somebody else. With a loopback redirect, a link or code sent to someone on another computer delivers nothing to the sender, because the browser that approves delivers the code to its own computer. A forwarded port or another user of the same computer is not covered. thalovant-api #501 adds `GET /v1/auth/native/config` and the `thalovant-sdk` client on `http://127.0.0.1:<port>/callback`. This SDK is the reference the six other SDKs and MCP port from. The contract is `browser-sign-in-SPEC.md` from the lead.

**Changed:**

- `src/thalovant/control.py`:
  - On both control-plane classes: `browser_login()` and `interactive_login()`.
  - Helpers: `_native_sign_in_config`, `_browser_login`, `_exchange_browser_code`, `_native_token_request`, `_present_browser_login`, `_open_browser_quietly` and `_without_secrets`.
  - `LoginMethod` and `DEFAULT_DEVICE_LOGIN_TIMEOUT`.
  - `complete_native_sign_in` now builds its body with `_native_token_request`. Its behaviour is unchanged.
- `src/thalovant/native_auth.py`: `browser_authorization_url`, `loopback_redirect_uri`, `new_state` (now also used by `begin_native_sign_in`, unchanged at 24 bytes), `NativeSignInConfig`, `can_open_browser`, `default_device_name`, and the constants.
- New `src/thalovant/_loopback.py`: `LoopbackListener`, `classify_request` and `response_bytes`.
- `src/thalovant/__init__.py`: the exports.
- The version: 0.11.0.
- `contracts/sdk-parity.json`:
  - The new `browser-login` capability, with `device-login`'s scope matrix and embedded-c's caller-owned networking reason.
  - The reviewed reference snapshot.
  - The candidate digest appended to `reference_history`.
- `contracts/conformance/browser-login-vectors.json` (new, 98 cases).
- `contracts/conformance-results.json`: the reference's results for those cases. The existing records are byte-identical.
- New tests: `tests/test_browser_login_vectors.py` and `tests/test_browser_login.py`.
- Docs:
  - `docs/browser-login-porting.md` (new).
  - The README's sign-in section and quick start.
  - `docs/api-reference.md`, `docs/index.md` and `docs/quickstart.md`.
- Records: the CHANGELOG under 0.11.0, AGENTS.md, QUICK_FACTS.md, FAQ.md, AUDIT.md and SUGGESTIONS.md.

**Decisions the porters inherit** (all in `docs/browser-login-porting.md`):

- **Return type.** `browser_login` returns what the one-call device login returns: the raw token answer, so `ai_disclosure` survives.
- **Errors.** The device-login denied and expired types are reused; there are no new error types.
- **Configuration.** It is strict, and anything unusable is an error with no status. `auto` then uses device login. An explicit `browser_login()` raises a config 404 rather than switching method.
- **The tab.** It is answered after the exchange, so the page is true. Five headers go with every listener answer.
- **Callbacks.** `error` wins over `code`. A right-state callback without exactly one code fails the sign-in.
- **Scopes and device name.** Empty scope strings are dropped. `device_name` is cut at 120 code points.
- **Environment.** A variable is set only when it is non-empty.
- **Exchange errors.** They are redacted and raised outside their handler, so neither `__cause__` nor `__context__` holds the original.
- **CLI.** It gains no `login` command, because it has nowhere to keep a token (SUGGESTIONS.md).

**Reference digest:** `57c7e71f6456344877b34bb3093399f813c7a21fde3ae14f48c8b3fa04be43b9` (current) to `49744ee385bfce57439dadbef1e20e6116f72a6817649843adba7a450d2fc56b` (candidate at the first head; superseded by `50af6731e276...` after the review, above). Consumers accept both, merge and publish first; this PR merges last.

**Verification:**

- Baseline, on `main` 44b6e2b with `python -m pytest -q --cov=thalovant` on Python 3.12: 1397 passed, 1 skipped, total coverage 90%.
- Python 3.12, `python -m pytest -q --cov=thalovant` with `THALOVANT_CONFORMANCE_OUT`: 1579 passed, 1 skipped.
  - Total coverage is 90% (90.4% against 90.0%).
  - `control.py` went from 92% to 93%.
  - `native_auth.py` and `_loopback.py` are at 100%.
- Python 3.10 at CI's floors (`aiohttp==3.11.0`, `cryptography==48.0.1`, `THALOVANT_MINIMUM_VERSIONS=1`): 1532 passed, 1 skipped. `tests/test_sdk_parity_contract.py` skips itself without `tomllib`.
- Python 3.14: 1579 passed, 1 skipped.
- Lint, types and the parity gate:
  - `python -m mypy` (strict) and `python -m ruff check src tests scripts` pass.
  - `python scripts/check-sdk-parity.py` reports "SDK parity reference verified".
  - `--snapshot` digests to the candidate above.
  - `scripts/generate-sdk-conformance.py --check` passes.
- Packaging and docs:
  - `python -m build` builds 0.11.0, and `twine check` passes both distributions.
  - `mkdocs build --strict` passes.
- The vectors:
  - The RFC 7636 challenge and two more were checked with `openssl dgst -sha256`.
  - The encoded device name was checked with Node's `encodeURIComponent` (plus `!'()*`).
- Mutations: 14 hand mutations of the new code, of which 13 failed the new tests. The fourteenth replaced the token answer's shape check with `_accept_token({})`, which raises the same error with no status, so it is equivalent.
- Not run: Windows and macOS (CI is `ubuntu-latest` only), the whole-workspace gate (`--workspace`), which needs the consumers' acceptances, and a sign-in against a live API, because #501 is not deployed yet.

### Transparency Report

- **AI Model:** Claude Opus 5.5 (Claude Code agent).
- **Actions Taken:**
  - Read the contract, the device-login implementation, the API's #501 branch (`app/routes/auth_native.py`, `app/auth/native_clients.py`) and the consent page's parser (thalovant-frontend `src/utils/native-authorization.ts`).
  - Wrote the implementation, the vectors (inputs by hand, expectations computed by the reference and cross-checked independently), the tests, the porting brief and the records.
  - Ran the suites, the gate, the build, the docs build and the mutations.
- **Oversight:** Done for the lead of the cross-SDK browser sign-in train, under the owner's goal above. The lead reviews and merges; this agent does not merge, tag or release.

## 2026-11-08: CodeRabbit review-body items on merged PRs #86 and #90

Two review-body items ("outside diff range" comments, which are not threads) had no answer. Each was checked against `main` bd4061f. The ten items on #19, #38, #44, #53, #59, #60 and #78 that the 2026-11-03 sweeps answered were checked again on the same commit, and each fix and test they cite is still in place.

| Old PR | Item | Outcome |
| :--- | :--- | :--- |
| #86 | `scripts/check-sdk-parity.py`: reject non-object capability entries before the conformance comparison | Still valid; fixed here. A `null`, list, string or number entry raised `AttributeError` in `validate_consumer` (a known capability) or `compare_conformance` (one the reference does not know yet), and `check()` catches neither, so a whole-workspace run stopped at the first malformed consumer with a traceback. The new `acceptance_entries()` checks the manifest, `capabilities`, each entry and its `reason`, `implementation`, `tests` and `vectors` (and that every path and hash in them is a string), plus `reference_digests` (a string there was searched as text), and raises the gate's usual `ValueError`. `file_hash()` checks its argument's type before joining it to the checkout path. |
| #90 | `intents.py`: include `listed_in` in `HubIntentInventory.as_dict()` | Already fixed in f8688da (#105, 0.9.7): `as_dict()` carries `"listed_in": list(self.listed_in)`. Covered by `tests/test_intents.py::test_the_serialised_inventory_says_which_language_it_listed_in`. |

**Changed:** `scripts/check-sdk-parity.py` and `tests/test_sdk_parity_contract.py`. Nothing under `src/`, so the parity reference digest does not move and `auto-release.yml` finds no release path.

**Verification:**

- `python -m pytest -q`: 1397 passed, 1 skipped (1376 before, plus 21 new). `ruff check src tests scripts` and `mypy` pass.
- The 21 new tests fail against the script on `main` (an uncaught `AttributeError` or `TypeError`, or the string `reference_digests` passing) and pass with the fix. Five of them come from CodeRabbit's review of this change: a path or hash that is not a string, and `file_hash()` given a number, `None` or a list.
- `scripts/check-sdk-parity.py` verifies the reference, and `--workspace ... --consumer thalovant-node-sdk` verifies the Node consumer. All eight consumer manifests on `main` were read: every entry and field already has the shape the gate now requires, so no consumer changes outcome.

### Transparency Report

- **AI Model:** Claude Opus 5.5 (Claude Code agent).
- **Actions Taken:**
  - Read every review-body item on the nine PRs and checked each against current code.
  - Reproduced the #86 crash against a copy of the Node consumer.
  - Wrote the shape check and its tests.
  - Answered #86 and #90 on the old PRs.
- **Oversight:** The work was done under the platform owner's instruction to close out every CodeRabbit finding. Hosted CI gates the merge. No release was cut.

## 2026-11-06: the listing review and the store's licensing refusal, in the reference docs

thalovant-api's launch data-protection declaration (`.changes/api-impact-launch-data-protection.yaml`, 2026-09-28) added `listing_review_status`, `listing_reviewed_at`, `listing_reviewed_by` and `listing_review_note` to the hub resource, `unavailable_reason` and `installable` to the marketplace entry, and the HTTP 409 `skill_unavailable_licensing` refusal to both skill-install routes. The SDK hands hubs and catalog entries back as the API's own mappings, so every field already reaches a caller unchanged, and a refusal already surfaces as `ThalovantAPIError` with its `code`; what was missing was any word of them in `docs/api-reference.md`. The entries for `get_hub`, `list_marketplace_skills`, `install_runtime_group_skill` and `install_hub_skill` now say what the fields and the refusal mean.

**Changed:** `docs/api-reference.md` only. Nothing under `src/`: the docstrings of the same four methods can say the same when the next functional release touches `control.py`, because any `src/` change since the latest tag makes `auto-release.yml` cut a patch release, and a release for prose alone is not worth a version. The parity reference digest does not move.

**Verification:** `python scripts/check-sdk-parity.py --workspace ..` verifies the reference and every consumer.

**Not done here, by the later declarations' own reading:** the consumer-law billing calls (`withdrawals/me`, `cancellations/me`, `contracts`), `legal.versions()` and `auth.accept_terms()`, which `api-impact-launch-consumer-law.yaml` and `api-impact-launch-catalogue.yaml` asked for as follow-ups. `api-impact-billing-contract-country.yaml` (2026-09-29) and `api-impact-account-age-band.yaml` (2026-10-20) since record those routes as browser and dashboard actions no SDK wraps; the owner's decision closes or reopens them. The `ai_disclosure` string on `DeviceTokenResponse`, `NativeTokenResponse` and the identify payloads is the one launch field this SDK still drops (`ApiToken.from_dict` keeps five members); carrying it is a reference change every consumer re-reviews, so it waits for a round of its own.

### Transparency Report

- **AI Model:** Claude Fable 5.1 (Claude Code agent).
- **Actions Taken:** Audited the SDKs and the MCP against thalovant-api's impact declarations; wrote the four reference entries and this note; ran the parity gate; opened the pull request.
- **Oversight:** Run on the repository owner's request to bring the SDKs and MCP up to date with the platform's features; the pull request goes through the normal CI gates, and its merge waits for the owner.

## 2026-11-03: CodeRabbit review-body items on merged PRs #38, #53, #59 and #60

Four more review-body items were never answered. They were "outside the diff" comments, written as quoted blocks that the first sweep's matcher missed. Each was checked against `main` ced77a4f. All four were fixed before their PR merged.

| Old PR | Item | Outcome |
| :--- | :--- | :--- |
| #38 | `transport.py`: require HTTPS before the HTTP transport sends anything (CWE-319) | Fixed in #38's own squash (43f7e66). `_preflight` (`src/thalovant/_hive.py` 1392-1399) refuses any endpoint that is not `https://` before the first request. Covered by `tests/test_client.py::test_http_transport_refuses_a_cleartext_endpoint`. |
| #53 | `control.py`: drop every normalized spelling of the legacy crypto key (CWE-532) | Fixed in #53's own squash (b47584f) with `_normalize_secret_key` (`control.py` 1906-1910). The test covered only `cryptoKey` and `crypto_key`, so this PR adds `Crypto-Key`, `CRYPTO_KEY` and `cryptokey` to `test_create_client_identity_drops_a_callers_legacy_crypto_key`. With the old two-key `pop` the test now fails on those three spellings. |
| #59 | `control.py`: keep `status_code` on device sign-in and redirect errors | Fixed in #59's own squash (ed2acef). Covered by `tests/test_config_concurrency.py::test_device_sign_in_errors_keep_their_http_status` and by the 302/307 cases of `::test_only_revision_conflicts_are_retried`. |
| #60 | `control.py`: deep-copy the `merge=False` payload before dispatch | Fixed in #60's own squash (b2b0122): `control.py` 1391-1393. Covered by `tests/test_config_concurrency.py::test_replacement_snapshots_nested_values_before_transport_dispatch`. |

**Changed:** `tests/test_control.py`, one test extended. There is no `src/` change, so the parity reference digest does not move, and `auto-release.yml` finds no release path.

**Verification:** the extended test passes on the branch. With `control.py` reverted locally to the two-key `pop`, it fails with `a legacy crypto key spelt ['Crypto-Key', 'CRYPTO_KEY', 'cryptokey'] reached /v1/clients`.

### Transparency Report

- **AI Model:** Claude Opus 5.5 (Claude Code agent).
- **Actions Taken:**
  - Read each item and checked it against current code.
  - Found the fixing commit and test for each.
  - Extended the one test that did not pin the normalization.
  - Posted one comment per old PR.
- **Oversight:** The work was done under the platform owner's instruction to handle every CodeRabbit comment. Hosted CI and the CodeRabbit review gate the merge. No release was cut.

## 2026-11-03: CodeRabbit review-body items on merged PRs #19, #44 and #78

Six review-body items (nitpick and duplicate comments, which are not threads) were never answered on three merged PRs. Each was checked against `main` ced77a4f.

| Old PR | Item | Outcome |
| :--- | :--- | :--- |
| #19 | `tests/test_client.py`: MQTT coverage for query redaction | Already covered since b47584f (#53): `tests/test_noise_lifecycle.py::test_mqtt_callback_failure_diagnostics_redact_authorization_queries` (connect and subscribe failures, health and connection info) and `::test_mqtt_wait_timeout_reports_the_phase_without_credentials` (subscription and handshake timeouts). |
| #19 | `control.py`: normalize key names before the secret denylist | Already fixed in b47584f (#53): `_normalize_secret_key`. Covered by the parametrized test at `tests/test_control.py:1492` (`apikey`, `Crypto_Key`, `INITIAL-IDENTIFY-TOKEN`, ...). |
| #19 | `tests/test_control.py`: two test details | The `next(...)` half landed in b47584f (#53). The other half is done here: `_bootstrap_mqtt_result` now asserts `result.identity.mqtt is not None` before reading the MQTT password. |
| #44 | `_noise_runtime.py`: domain error for a corrupt pin store | Already fixed in a87614b (#45): `_validated_noise_pins`. Covered by `tests/test_noise_store.py::test_malformed_pin_state_raises_domain_error_and_preserves_file`. |
| #44 | `transport.py`: keep `ThalovantTimeoutError` on HTTP connect | Already fixed in a87614b (#45). Covered by `tests/test_noise_transports.py::test_https_handshake_deadline_preserves_timeout_and_releases_admission`. |
| #78 | `native_auth.py`: bind `code_from` to the redirect URI | Already fixed before #78 merged (b9c7c30). `NativeSignIn.code_from` compares scheme, host and path with `redirect_uri`. Covered by `tests/test_native_auth.py::test_a_callback_arriving_somewhere_else_is_refused`. |

**Changed:** one line in `tests/test_control.py`. There is no `src/` change, so the parity reference digest does not move, and `auto-release.yml` finds no release path.

**Verification:** `python -m pytest -q` passes on the branch. This change only narrows a type in a test helper and changes no behaviour, so there is no revert-fails test for it.

### Transparency Report

- **AI Model:** Claude Opus 5.5 (Claude Code agent).
- **Actions Taken:**
  - Read each review-body item and checked it against current code.
  - Found the fixing commits and tests for five of them.
  - Applied the remaining one-line test change.
  - Posted one comment per old PR listing the outcome of each item.
- **Oversight:** The work was done under the platform owner's instruction to handle every CodeRabbit comment. Hosted CI and the CodeRabbit review gate the merge. No release was cut.

## 2026-11-03 — Chat apps (0.10.0)

**What changed:**

- `src/thalovant/chat_apps.py` (new): the typed models of the chat apps routes (`ChatAppConnector`, `BridgeNetworks`, `BridgeAllowance`, `Bridge`, `BridgeStatus`, `BridgeHold`, `BridgeChatList`, `BridgeTestResult`, `BridgeAvatar` ...), the value sets (`BridgeStatusReason`, `BridgePhase`, `BridgeHoldReason` ...; unknown values kept as sent), `SecretValue`, and the request-body builders.
- `src/thalovant/control.py`: twelve methods on `AsyncThalovantControlPlane` and `ThalovantControlPlane` for the owner routes under `/v1/bridges` (`list_chat_app_connectors`, `get_bridge_networks`, `list_bridges`, `get_bridge`, `create_bridge`, `update_bridge`, `replace_bridge_token`, `replace_bridge_secrets`, `list_bridge_chats`, `send_bridge_test_message`, `get_bridge_avatar`, `delete_bridge`). The senders can read a bounded binary body (`max_bytes`), for the avatar. Staff routes (`/v1/admin/chat-apps`) are not covered, as no other admin route is.
- `src/thalovant/errors.py`: `ThalovantAPIError.fields`.
- `contracts/sdk-parity.json`: a `chat-apps` capability (required for Python, Node, Go, Rust and MCP; not applicable to Kotlin, Swift and .NET, whose absence the API's impact records already state, and to embedded C), and the new reference digest `57c7e71f6456`.
- Docs: `docs/guides/chat-apps.md`, `docs/reference/chat-apps.md`, `docs/api-reference.md`, README, CHANGELOG. Version 0.10.0.

**Why:** the platform owner asked that the SDKs and the MCP server support chat apps, live in the API since 2026-11-03.

**Review (CodeRabbit on #108):** fixed: `SecretValue` equality survives a lone surrogate; a mapping `allowed` entry with a blank id is refused locally like a blank string; a `SecretValue` nested anywhere in `settings` is refused locally; the requests-style sender streams the avatar and stops at `MAX_BRIDGE_AVATAR_BYTES`; `ThalovantAPIError(fields="name")` is one name; README, guide and quick facts examples define `hub_id`, guard an empty chat list and show the optional defaults. Declined: refusing an empty `allowed` locally (the API's 422 `bridge_allowed_required` is the contract and every port sends it through). The reference digest moved from `1e56099da5db` to `57c7e71f6456` with these fixes.

**Verification:**

- `python -m pytest -q`: 1376 passed, 1 skipped (50 new in `tests/test_chat_apps.py`, including a real aiohttp server for the avatar cap and `If-Match`).
- `mypy` and `ruff check src tests scripts` clean; `python -m build` builds 0.10.0; `mkdocs build --strict` passes.
- `scripts/check-sdk-parity.py` verifies the reference; with the workspace, every consumer is one reference behind until it records `57c7e71f6456` (expected during the rollout).
- `scripts/generate-sdk-conformance.py --check` passes.
- Every field and value the models declare was checked against the API's OpenAPI document of the deployed release.

### Transparency Report

- **AI Model:** Claude Opus 5.5 (claude-opus-5-5), as a Claude Code agent.
- **Actions Taken:** read the API's schemas and routes; wrote the module, the methods, the tests and the docs; updated the parity contract; coordinated the Node, Go, Rust and MCP ports.
- **Oversight:** automated tests, hosted CI and the CodeRabbit review gate the merge; the platform owner's coordinating agent decides when it merges and releases.

## 2026-11-03: CodeRabbit follow-ups on merged PRs #81, #83 and #94

Ten CodeRabbit review threads were still unresolved on three merged PRs. Each was checked against `main` 51069339 rather than taken on trust.

| Old PR | Thread | Outcome |
| :--- | :--- | :--- |
| #94 | `_hive.py`: keep and log fire-and-forget tasks | Already fixed in 402e27d (#96): `_loop.spawn`. Tests in `tests/test_background_tasks.py`. |
| #94 | `_wire.py`: bound decompression | Already fixed in 402e27d (#96): `MAX_INFLATED`, `_inflate`. Tests in `tests/test_wire.py`. |
| #94 | `_yaml.py`: wrap reader failures | Already fixed in 402e27d (#96): `safe_load`. Test: `test_every_reader_failure_is_a_yaml_error`. |
| #94 | `_yaml.py`: apostrophe in a plain scalar | Already fixed in 402e27d (#96): `_opens_quote`, `_quote_end`. Test: `SAMPLES` in `tests/test_yaml_reader.py`. |
| #94 | `errors.py`: `ThalovantAuthError` docstring | Already fixed in 402e27d (#96). |
| #94 | `home.py`: bound a synchronous handler | Already fixed in 402e27d (#96): `_HandlerThreads`. Tests in `tests/test_background_tasks.py`. |
| #83 | parity checker: a substring is not execution evidence | Already fixed in 22d0551 (#84) by `names_vector` and in 2bfe44a (#85) by `compare_conformance`. |
| #83 | `client.py`: bound aliases per group | Fixed in 22d0551 (#84). **No test until now.** Test added in this change. |
| #83 | `client.py`: late carry under the returned id | Fixed in 22d0551 (#84). **No test until now.** Test added in this change. |
| #81 | `transport.py`: dispatch binary subscribers off the receive loop | Done for WSS and HTTPS since 0.9.0 (215ac7d, #94), through the FIFO `CallbackThread`. MQTT and custom transports still dispatch inline by design. Recorded in [AUDIT.md](AUDIT.md). |

**Changed:** `tests/test_conversation_continuity.py` gains two tests:

- `test_one_conversation_keeps_a_bounded_number_of_aliases`
- `test_a_late_handled_event_files_the_carry_under_the_id_the_reply_returned`

`AUDIT.md` and this file are new. No `src/` file changes. The parity reference digest, which covers `src/thalovant/*.py`, does not move, and `auto-release.yml` has no relevant path to release on.

**Review follow-up on #107:** CodeRabbit's two review-body items are applied.

- The alias test also asserts that the inherited alias right behind the newest one survives.
- The late-handled fixture now waits on a release event set after `ask()` returns, instead of a fixed sleep, so the post-completion path is always the one exercised.
- The new fixture methods have docstrings.

**Verification:**

- Each new test was run against `main` with its fix reverted locally, and failed:
  - removing `del keys[aliases:]` gives a 33-name group against a cap of 8;
  - dropping the `returned_session_id` alias means the follow-up sends no `converse_handlers`.
- With the fix restored, the full suite passes: `python -m pytest -q` gives 1326 passed and 1 skipped.
- `ruff check src tests scripts` and `mypy` are clean.
- `scripts/check-sdk-parity.py` prints "SDK parity reference verified".
- `scripts/generate-sdk-conformance.py --check` passes.

### Transparency Report

- **AI Model:** Claude Opus 5.5 (Claude Code agent).
- **Actions Taken:**
  - Read each unresolved CodeRabbit thread in full and checked it against current code.
  - Found the fixing commits and their tests.
  - Wrote the two missing regression tests, and proved that each fails with its fix reverted.
  - Wrote `AUDIT.md` and this report.
  - Replied on and resolved the old threads.
- **Oversight:**
  - The work was done under the platform owner's instruction to handle every CodeRabbit comment.
  - Hosted CI and the CodeRabbit review on the follow-up PR gate the merge.
  - No release was cut and no version was bumped.
