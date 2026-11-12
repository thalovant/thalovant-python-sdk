Last Edit: Claude Sonnet 5.5 - 2026-11-12 - Motive: Intercom review round 2 (CodeRabbit PR 114): requests-style session read off the loop; wait scope (send implies read), key replay, cancel permissions.

# Audit

Known issues, technical debt and security findings for `thalovant` (the Python SDK and the parity reference). Each entry cites the code and the test that show it. Line numbers are at `main` 51069339 (2026-11-03).

Test run behind this file: `python -m pytest -q` on Python 3.12, 1828 passed, 1 skipped (branch `feature/intercom-v1`, final-API round, 2026-11-11). `ruff`, `mypy` and `scripts/check-sdk-parity.py` also pass. (The 0.11.0 round before it: 1621 passed, 1 skipped.)

## Open: known limitations

### Intercom: the SDK keeps the whole API body in `ThalovantAPIError.problem` (security review L5)

`_api_error()` (`src/thalovant/control.py`) hands the parsed error body to `ThalovantAPIError.problem` as it is. The API strips `input` and `ctx` from `/intercom/` validation errors (`app/middleware/errors.py`), so the text does not reach it today; a regression there would put the text into the exception. Not changed: the contract's section J does not ask for it. Proposed in `SUGGESTIONS.md`.

### Intercom: the timezone and Unicode checks use this machine's databases

`_timezone()` (`src/thalovant/intercom.py`) asks `zoneinfo`, so a name is valid when this machine's tz database knows it; the API asks its own (case-sensitive on Linux). Where no database exists (Windows without the `tzdata` package) only the shape is checked and the API decides (`tests/test_intercom.py::test_the_zone_shape_is_the_fallback_only_where_there_is_no_timezone_database`). `announcement_text()` uses `unicodedata`, whose version follows the Python build: a character assigned in a newer Unicode than the SDK's (or the API's) can pass here and be refused there, or the reverse. The API's answer stays authoritative; the 422 is the fallback.

### Intercom: `ThalovantAPIError.detail` does not read `message`

The final intercom problems carry their sentence in `message`; `_problem_fields()` (`src/thalovant/errors.py`) reads `detail` only, so `error.detail` is `None` for them (the sentence is in `problem["message"]` and in the exception message). Changing it moves the shared api-error behaviour in every SDK; proposed in `SUGGESTIONS.md`.

### Fixed in the review round 2 (PR 114)

- **A requests-style session blocked the event loop** (and so the `announce(wait=True)` deadline): `_BlockingSender.send` (`src/thalovant/control.py`) now reads on a worker thread (`tests/test_intercom.py::test_the_wait_deadline_bounds_a_stalled_read_through_a_requests_style_session`, `::test_a_requests_style_session_is_read_off_the_event_loop_thread`). A cancelled read is abandoned, not interrupted: the thread ends with the session's own `timeout`.
- **A 403 carrying `urgent_scope_required` and the generic "Insufficient scopes" sentence was a plain `ThalovantAuthError`**, and a top-level `null` hid the nested `announcement_id`, `matched`, `not_speakers`: `_error_kind` and `_problem_member` fixed (`tests/test_intercom.py::test_the_urgent_scope_code_wins_over_the_generic_insufficient_scopes_refusal`, `::test_a_top_level_null_or_wrong_type_does_not_hide_the_member_inside_detail`).
- **Docs claimed `wait=True` needs `intercom:read` as well, and that cancel is owner/admin only**: both were wrong against the API (`app/auth/utils.py:232`, `app/routes/intercom.py` `cancel_announcement`); corrected in the guide, README, docstrings and porting notes.

### Fixed in this round (final API)

- **Rooms, groups and the key length** (was L6): `IntercomTargets.as_dict()` caps every list at 50 names of 64 characters and `idempotency_key()` caps the key at 128 printable ASCII characters (`tests/test_intercom.py::test_a_target_list_holds_at_most_50_names_of_at_most_64_characters`, `::test_an_idempotency_key_is_at_most_128_printable_ascii_characters`).
- **A replayed announcement is marked** (`Announcement.replayed`, from `Idempotency-Replayed: true`; `::test_a_replayed_announcement_says_so_and_a_first_answer_does_not`, `::test_a_replayed_sending_announcement_is_202_and_stays_replayed_after_wait`).
- **The text rule is the API's** (was: 1-280 and control characters only): `<` `>`, format characters, bidi controls, lone surrogates, private use and separators are refused first (`::test_a_text_the_api_would_refuse_is_refused_first_and_never_echoed`).
- **Quiet hours** that are on need a real IANA zone and `start != end`; unknown `dnd` keys are refused (`::test_an_unusable_do_not_disturb_is_refused_before_anything_is_sent`).

### Intercom: a poll that fails after the POST leaves an accepted announcement

`AsyncThalovantControlPlane.announce(wait=True)` (`src/thalovant/control.py`) sends the POST, then polls `get_announcement` (`intercom:read`, which `intercom:send` implies: `app/auth/utils.py` `expand_granted_scopes`). A 429 or a network error fails a poll after the announcement was accepted, and the exception carries no announcement id (`tests/test_intercom.py::test_a_failed_poll_surfaces_after_the_post_was_accepted`). The callers' answer is a fresh `idempotency_key`: calling again with the same key replays the stored announcement (with its id) and does not send again. Not fixed in the SDK because an exception cannot also return the accepted announcement; the docs say so (`docs/guides/intercom.md`).

### Intercom: the announcement text is held in memory and in the request body

`announce_body()` (`src/thalovant/intercom.py`) builds the request body, which holds the text, and `AsyncThalovantControlPlane.announce()` (`src/thalovant/control.py`) sends it. Nothing in the SDK logs or raises it, and `Announcement` never carries it (`tests/test_intercom.py::test_announce_normal_sends_the_contract_body_and_reads_deliveries`). It is not redacted from an error the API echoes it in: `ThalovantAPIError.problem` keeps the whole body, so a server that echoed the text in a refusal would put it there. The contract says the API never does. Also unverified against a live API: the announcement-list shape (`items` and `next_cursor`) is read from the contract's description, with `data` and `cursor` accepted as well.

### A blocking handler on MQTT or a custom transport holds up that transport's receive worker

- **What:** WSS and HTTPS are native asyncio transports. A synchronous handler on them is posted to the client's own FIFO `CallbackThread` (`src/thalovant/_hive.py` `_call`, lines 409-419; `src/thalovant/_loop.py` `CallbackThread` 219 and `OffLoop` 259). The receive loop never runs user code. MQTT (`HiveMindMQTTTransport`) and a caller's own `transport=` go through `SyncLink`, which hands a synchronous handler to the transport "as it is" (`src/thalovant/_link.py` lines 15-16). `_ConnectionLifecycle._deliver_binary` (`src/thalovant/transport.py` 327) and the bus and hive dispatch beside it then call it inline on the MQTT `_receive_loop` worker (955).
- **Effect:** On MQTT, a handler that blocks delays every later frame. Paho keeps queueing frames meanwhile. Once 256 frames are waiting (`transport.py` 581/700), `_on_message` (939) fails the session.
- **Docs:** The sync client's `on_hive()` and `on_binary()` docstrings (`src/thalovant/client.py` 2671, 2692) say the handler "runs on this client's handler thread". That holds on WSS and HTTPS. On MQTT the handler runs on the transport's receive worker. Both docstrings correctly say a blocking handler holds up the next frame.
- **Status:** This is a design decision. Moving MQTT onto the callback thread would change the reference AST digest that every consumer SDK acknowledges, so it belongs in a coordinated release. The decision came from CodeRabbit's thread on #81 (`transport.py`, "Dispatch binary subscribers outside transport receive loops"), which is now resolved, so the limitation is recorded here.
- **Covered by:** `tests/test_wss_end_to_end.py::test_a_sync_handler_runs_off_the_loop_and_may_call_the_client_mid_turn` asserts that a WSS handler runs on `thalovant-handlers`.

## Closed in this sweep

- **The parity gate stopped on a consumer acceptance of the wrong shape (2026-11-08, CodeRabbit on #86).** `scripts/check-sdk-parity.py` `validate_consumer` and `compare_conformance` read a consumer's manifest and each capability entry with `.get`. A `null`, list, string or number there raised `AttributeError`, which `check()` does not catch, so a whole-workspace run ended in a traceback and the consumers after the malformed one went unchecked. A string `reference_digests` was searched as text, so one that merely contained the current digest passed. `acceptance_entries()` now checks those shapes, and that every evidence path and hash is a string, and raises the gate's usual `ValueError`. `file_hash()` checks its argument's type before `root / name`, which raised `TypeError` for a number or a list. `tests/test_sdk_parity_contract.py::test_an_acceptance_entry_that_is_not_an_object_is_a_controlled_error`, `::test_an_acceptance_field_of_the_wrong_shape_is_a_controlled_error`, `::test_an_acceptance_of_the_wrong_shape_is_a_controlled_error` `::test_one_malformed_consumer_does_not_stop_the_workspace_run` and `::test_evidence_that_is_not_a_path_is_a_controlled_error` fail without it. The other item without an answer, `listed_in` in `HubIntentInventory.as_dict()` (#90), was already fixed in f8688da (#105).

- **Review-body items on #38, #53, #59 and #60 (2026-11-03).** All four were fixed before their PRs merged, and each is covered by a test listed in [MAINTENANCE_REPORT.md](MAINTENANCE_REPORT.md). One test did not pin the fix it covered. `test_create_client_identity_drops_a_callers_legacy_crypto_key` checked only `cryptoKey` and `crypto_key`, so reverting the normalized match in `control.py` 1906-1910 to two exact `pop`s passed. It now also sends `Crypto-Key`, `CRYPTO_KEY` and `cryptokey`. No open issue came out of these items.

- **Review-body items on #19, #44 and #78 (2026-11-03).** Five of the six were already fixed and are covered by tests, which are listed in [MAINTENANCE_REPORT.md](MAINTENANCE_REPORT.md). The sixth is a type narrowing in a test helper: `tests/test_control.py` `_bootstrap_mqtt_result` read `result.identity.mqtt.password` without checking that `mqtt` is set, and it now asserts that first. No open issue came out of these items.

- **The alias cap per conversation had no test.** `_remember_conversation` caps a group at `MAX_CONVERSATION_ALIASES` (`client.py` 372 and 1705, from 22d0551 / #84). Nothing failed when the cap was removed. `tests/test_conversation_continuity.py::test_one_conversation_keeps_a_bounded_number_of_aliases` now fails without it, with a 33-name group against a cap of 8.
- **The late carry under the id the reply returned had no test.** The late `ovos.utterance.handled` callback also files the carried session under the id `ask()` returned (`client.py` 1993-1998 and 2196-2201, from 22d0551 / #84). The existing late-handled test continued under the request's own id, so this path was never exercised. `test_a_late_handled_event_files_the_carry_under_the_id_the_reply_returned` now fails without the fix: the follow-up `ask(session_id=reply.session_id)` sends no `converse_handlers`.

## Browser sign-in (0.11.0)

Read at branch `feat/browser-sign-in` (from `main` 44b6e2b). Behind it: the full suite, 224 browser sign-in tests (`tests/test_browser_login_vectors.py` and `tests/test_browser_login.py`), and 14 hand mutations of the new code, 13 of them killed. The fourteenth replaced the token answer's shape check with `_accept_token({})`, which raises the same error with no status, so it is equivalent.

- **Another user on the same computer can sign the program in as themselves.** The authorization URL reaches the browser as a process argument (`_present_browser_login` and `_open_browser_quietly`, `src/thalovant/control.py` 4344-4370, through `webbrowser.open`). On a shared machine, another local user can read it from the process table. They can open it in their own browser and approve it with their own account, and the redirect lands on the same `127.0.0.1` port with the right `state`. The program is then signed in to *their* account. This is RFC 8252's loopback flow as `gh auth login` and `aws sso login` run it: the state and the challenge sit in the URL, so neither helps here. A forwarded callback port (`ssh -L`, a port-forwarding tool) has the same effect across machines: whoever's browser reaches the forwarded port completes the sign-in. The docs therefore promise only that a link sent to somebody on another computer delivers nothing to the sender. The consent page's `device_name` is what the approver sees. No mitigation in the SDK. A single-user desktop, the case this flow is for, is unaffected.
- **Deny reaches the SDK only once the dashboard sends it.** The SDK raises `ThalovantDeviceLoginDenied` on `error=access_denied` (`classify_request`, `_loopback.py` 91). The consent page sends that only after the dashboard change in the same train. Until then, a Deny never calls back and the sign-in expires after `timeout` (300 s).
- **`can_open_browser()` does not prove a browser exists.** It reads `CI`, `SSH_*`, `DISPLAY` and `WAYLAND_DISPLAY` (`src/thalovant/native_auth.py` 200), as the contract says. A Linux desktop with a display but no `xdg-open` still chooses the browser. The printed URL is the fallback there: once the browser is meant to open, the sign-in does not turn into a device login.
- **Windows and macOS were not run.** This branch ran only on Linux (Python 3.10, 3.12 and 3.14), and CI runs `ubuntu-latest` only. The Windows exclusive-address option (`_loopback.py` 185) is `pragma: no cover`, and macOS's `open` is reached only through `webbrowser`.
- **`ai_disclosure` is still dropped by `ApiToken`.** It stays in the raw token answer that `browser_login()` returns. See [SUGGESTIONS.md](SUGGESTIONS.md).
- **Fixed in review (CodeRabbit on #113):**
  - **The token endpoint, or any path, could leave the API.** `/https://collector.example/token` passed the configuration check, and `_send`'s `lstrip("/")` plus `urljoin` made it a foreign URL. A colon or a broken percent-escape anywhere in `token_endpoint` now makes the configuration unusable (`_usable_token_endpoint`, `native_auth.py`). `_send` refuses every request whose resolved origin is not the API's (`tests/test_browser_login.py::test_no_request_leaves_the_api_origin`). The Node port found the request-layer half.
  - **An `authorization_endpoint` no browser can open was usable.** Port 0 and a host that is not a real hostname or IP address (`https://%/authorize`) are now refused (`_real_host`, `native_auth.py`). These came from the Node and .NET ports.
  - **A callback's `error` could carry the state into a message.** Only RFC 6749's codes are kept (`OAUTH_ERRORS`, `_loopback.py`).
  - **A message cut to 200 characters could keep part of a secret that redaction then missed.** The answer is now redacted before the message is built (`_redacted_response`).
  - **A transport error kept the request body in `__context__`.** `_without_secrets` now always builds a new error, and it is raised outside its handler, so it has no cause and no context.
  - **A `HEAD` answer had a body.** It now carries headers only.
  - **The accepted tab could report failure after 60 s** for an exchange that then succeeded. The tab now waits for the end; `close()` bounds it.
  - **`device_name=""` sent the hostname.** Only `None` gets the default now.
  - **An early answer could end in a TCP reset.** The listener closed with bytes still unread: the rest of an oversized head, or a POST body behind a 404. macOS then loses the answer; Linux shows `ECONNRESET` from a 200 KB head. `_answer` (`_loopback.py`) now drains first (`tests/test_browser_login.py::test_an_early_answer_is_not_lost_to_a_reset`). Found by the Rust port.

  Each fix has a test in `tests/test_browser_login.py` or a case in `browser-login-vectors.json`, and each was checked by reverting it: 10 of 10 reverts failed a test.

## Chat apps (0.10.0)

- **`GET /v1/bridges` has no cursor.** `list_bridges()` returns at most 500 bridges (`MAX_BRIDGE_LIST_LIMIT` in `src/thalovant/control.py`); a platform admin listing every bridge cannot page past that. `tests/test_chat_apps.py::test_list_bridges_refuses_a_blank_filter_and_a_bad_limit_before_sending` pins the bound.
- **The routes' 412 and the deactivated-owner 403 carry no `code`.** A stale `etag` answers `detail: "ETag mismatch"` only (`test_a_stale_etag_is_the_apis_412`), so callers branch on `status_code`.
- **No zeroing of credentials.** `SecretValue` (`src/thalovant/chat_apps.py`) keeps the credential as a Python `str` until it is garbage collected; Python cannot wipe one. It only guarantees that nothing prints it (`test_a_secret_never_prints`).
- **Kotlin, Swift and .NET do not cover chat apps.** Recorded as not applicable in `contracts/sdk-parity.json` because the API's impact records name no obligation for them; a port would need that decision first.
- **The language-matching reference moved upstream.** `ovos-spec-tools` 1.17.0a1 (2026-11-03) matches `yue` to `zh-HK`; `closest_lang` here returns `None` for it, so `tests/test_language_matching.py::test_matching_agrees_with_ovos_spec_tools_over_a_sweep` failed with `('yue', ['fr-CA', 'zh-HK', 'zh-cmn'])`. The `dev` extra now caps it below 1.17 (`pyproject.toml`). Adopting the new match is a coordinated parity change to the `listing` vectors in every SDK, not a test fix.

