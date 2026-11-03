Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Chat apps (0.10.0): connectors, allowance, the /v1/bridges owner routes, SecretValue and ThalovantAPIError.fields; and record the CodeRabbit review-body sweep of merged PRs #19, #44 and #78.

# Maintenance Report

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
