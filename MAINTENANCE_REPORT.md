Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Record the CodeRabbit follow-up sweep of merged PRs #81, #83 and #94.

# Maintenance Report

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
