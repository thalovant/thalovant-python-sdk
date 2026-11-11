# Porting intercom

Last Edit: Claude Sonnet 5.5 - 2026-11-11 - Motive: porting brief for the intercom capability (0.12.0).

The Python SDK 0.12.0 adds the `intercom` capability: announce-only text, spoken aloud
on a household hub's speakers. Source of truth: `src/thalovant/intercom.py` (models and
request builders) and the intercom methods of `src/thalovant/control.py`
(`announce`, `get_announcement`, `list_announcements`, `list_intercom_speakers`,
`update_intercom_speaker`, `get_intercom_settings`, `set_intercom_settings`), with
`tests/test_intercom.py` as the executable reference (a loopback fake API).

| Required in | Not applicable in |
|---|---|
| Node, Go, Rust, MCP | Kotlin, Swift, .NET (a phone app or a hub data plane does not send announcements; recorded in each parity file), embedded-c (no HTTP client) |

## Rules to keep

- **Not a certified safety system.** Every user-facing doc says so, and that it is no
  substitute for smoke or CO alarms, medical alert devices or emergency services.
- **The text is never stored or logged.** Do not put it in an error, a log line, a
  trace or a debug dump. A request's body holds it; nothing the SDK raises may.
- **Names.** `announce*` and `*_intercom_*`; never a bare `intercom` (the mesh frame).
- **Paths.** `/v1/hubs/{hub_id}/intercom/...`, the hub id and the speaker or announcement
  id percent-escaped as one segment (`/` becomes `%2F`); a blank id is refused locally.
- **Validate before sending.** Text trimmed, 1-280 characters, no control characters
  (C0, DEL, C1, U+2028/2029). Targets: exactly one of `all` (true), `speakers`
  (at most 50), `rooms`, `groups`. `priority` `normal` or `urgent`; `repeat`
  (`times` 1-10, `interval_seconds` 5-120) only with `urgent`. Speaker patch: only the
  fields passed (`room` may be null to clear it, so "not passed" is distinct from null),
  at least one, room at most 64 characters, at most 8 groups of at most 32, quiet hours
  `HH:MM`.
- **`Idempotency-Key`** only when the caller gives one. No automatic retry.
- **Waiting.** `wait` polls `get_announcement` while the status is `sending`, every
  `poll_interval` (default 2 s), until `wait_timeout` (default `times * interval + 15` s);
  on timeout it returns the last answer, still `sending`; it does not fail.
- **Open enums.** Announcement status, delivery status, priority and the settings'
  `unavailable_reason` are strings; unknown values survive.
- **Errors.** `intercom_disabled` (any status), 502 `hub_unreachable`, 429 (with
  `retry_after_seconds`) have their own kind; 403 `plan_limit` is the plan error; the
  rest are the usual API error with status, code and detail.
- **Shapes.** Speakers `{"items": [...]}`; announcements list `{"items": [...], "next_cursor"}`
  (the contract names `limit` and `cursor`; `data` and `cursor` are accepted too).

## Scopes

`intercom:read` (settings, speakers, announcements), `intercom:send` (announce),
`hubs:write` (set settings, update a speaker).
