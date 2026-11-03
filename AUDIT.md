Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Chat apps (0.10.0): connectors, allowance, the /v1/bridges owner routes, SecretValue and ThalovantAPIError.fields; and add the review-body sweeps of merged PRs #19, #44, #78, #38, #53, #59 and #60 to the closed findings.

# Audit

Known issues, technical debt and security findings for `thalovant` (the Python SDK and the parity reference). Each entry cites the code and the test that show it. Line numbers are at `main` 51069339 (2026-11-03).

Test run behind this file: `python -m pytest -q` on Python 3.12, 1326 passed, 1 skipped. `ruff`, `mypy`, `scripts/check-sdk-parity.py` and `scripts/generate-sdk-conformance.py --check` also pass.

## Open: known limitations

### A blocking handler on MQTT or a custom transport holds up that transport's receive worker

- **What:** WSS and HTTPS are native asyncio transports. A synchronous handler on them is posted to the client's own FIFO `CallbackThread` (`src/thalovant/_hive.py` `_call`, lines 409-419; `src/thalovant/_loop.py` `CallbackThread` 219 and `OffLoop` 259). The receive loop never runs user code. MQTT (`HiveMindMQTTTransport`) and a caller's own `transport=` go through `SyncLink`, which hands a synchronous handler to the transport "as it is" (`src/thalovant/_link.py` lines 15-16). `_ConnectionLifecycle._deliver_binary` (`src/thalovant/transport.py` 327) and the bus and hive dispatch beside it then call it inline on the MQTT `_receive_loop` worker (955).
- **Effect:** On MQTT, a handler that blocks delays every later frame. Paho keeps queueing frames meanwhile. Once 256 frames are waiting (`transport.py` 581/700), `_on_message` (939) fails the session.
- **Docs:** The sync client's `on_hive()` and `on_binary()` docstrings (`src/thalovant/client.py` 2671, 2692) say the handler "runs on this client's handler thread". That holds on WSS and HTTPS. On MQTT the handler runs on the transport's receive worker. Both docstrings correctly say a blocking handler holds up the next frame.
- **Status:** This is a design decision. Moving MQTT onto the callback thread would change the reference AST digest that every consumer SDK acknowledges, so it belongs in a coordinated release. The decision came from CodeRabbit's thread on #81 (`transport.py`, "Dispatch binary subscribers outside transport receive loops"), which is now resolved, so the limitation is recorded here.
- **Covered by:** `tests/test_wss_end_to_end.py::test_a_sync_handler_runs_off_the_loop_and_may_call_the_client_mid_turn` asserts that a WSS handler runs on `thalovant-handlers`.

## Closed in this sweep

- **Review-body items on #38, #53, #59 and #60 (2026-11-03).** All four were fixed before their PRs merged, and each is covered by a test listed in [MAINTENANCE_REPORT.md](MAINTENANCE_REPORT.md). One test did not pin the fix it covered. `test_create_client_identity_drops_a_callers_legacy_crypto_key` checked only `cryptoKey` and `crypto_key`, so reverting the normalized match in `control.py` 1906-1910 to two exact `pop`s passed. It now also sends `Crypto-Key`, `CRYPTO_KEY` and `cryptokey`. No open issue came out of these items.

- **Review-body items on #19, #44 and #78 (2026-11-03).** Five of the six were already fixed and are covered by tests, which are listed in [MAINTENANCE_REPORT.md](MAINTENANCE_REPORT.md). The sixth is a type narrowing in a test helper: `tests/test_control.py` `_bootstrap_mqtt_result` read `result.identity.mqtt.password` without checking that `mqtt` is set, and it now asserts that first. No open issue came out of these items.

- **The alias cap per conversation had no test.** `_remember_conversation` caps a group at `MAX_CONVERSATION_ALIASES` (`client.py` 372 and 1705, from 22d0551 / #84). Nothing failed when the cap was removed. `tests/test_conversation_continuity.py::test_one_conversation_keeps_a_bounded_number_of_aliases` now fails without it, with a 33-name group against a cap of 8.
- **The late carry under the id the reply returned had no test.** The late `ovos.utterance.handled` callback also files the carried session under the id `ask()` returned (`client.py` 1993-1998 and 2196-2201, from 22d0551 / #84). The existing late-handled test continued under the request's own id, so this path was never exercised. `test_a_late_handled_event_files_the_carry_under_the_id_the_reply_returned` now fails without the fix: the follow-up `ask(session_id=reply.session_id)` sends no `converse_handlers`.

## Chat apps (0.10.0)

- **`GET /v1/bridges` has no cursor.** `list_bridges()` returns at most 500 bridges (`MAX_BRIDGE_LIST_LIMIT` in `src/thalovant/control.py`); a platform admin listing every bridge cannot page past that. `tests/test_chat_apps.py::test_list_bridges_refuses_a_blank_filter_and_a_bad_limit_before_sending` pins the bound.
- **The routes' 412 and the deactivated-owner 403 carry no `code`.** A stale `etag` answers `detail: "ETag mismatch"` only (`test_a_stale_etag_is_the_apis_412`), so callers branch on `status_code`.
- **No zeroing of credentials.** `SecretValue` (`src/thalovant/chat_apps.py`) keeps the credential as a Python `str` until it is garbage collected; Python cannot wipe one. It only guarantees that nothing prints it (`test_a_secret_never_prints`).
- **Kotlin, Swift and .NET do not cover chat apps.** Recorded as not applicable in `contracts/sdk-parity.json` because the API's impact records name no obligation for them; a port would need that decision first.
- **The language-matching reference moved upstream.** `ovos-spec-tools` 1.17.0a1 (2026-11-03) matches `yue` to `zh-HK`; `closest_lang` here returns `None` for it, so `tests/test_language_matching.py::test_matching_agrees_with_ovos_spec_tools_over_a_sweep` failed with `('yue', ['fr-CA', 'zh-HK', 'zh-cmn'])`. The `dev` extra now caps it below 1.17 (`pyproject.toml`). Adopting the new match is a coordinated parity change to the `listing` vectors in every SDK, not a test fix.

