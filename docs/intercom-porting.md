# Porting intercom

Last Edit: Claude Sonnet 5.5 - 2026-11-11 - Motive: porting brief for the intercom capability (0.12.0), amendment 2 (contract section J): cancel, key replay docs, ids, timeout, wording.

The Python SDK 0.12.0 adds the `intercom` capability: announce-only text, spoken aloud
on a household hub's speakers. Source of truth: `src/thalovant/intercom.py` (models and
request builders) and the intercom methods of `src/thalovant/control.py`
(`announce`, `cancel_announcement`, `get_announcement`, `list_announcements`,
`list_intercom_speakers`, `update_intercom_speaker`, `get_intercom_settings`,
`set_intercom_settings`), with
`tests/test_intercom.py` as the executable reference (a loopback fake API).

| Required in | Not applicable in |
|---|---|
| Node, Go, Rust, MCP | Kotlin, Swift, .NET (a phone app or a hub data plane does not send announcements; recorded in each parity file), embedded-c (no HTTP client) |

## Rules to keep

- **Not a certified safety system.** Every user-facing doc says so, and that it is no
  substitute for smoke or CO alarms, medical alert devices or emergency services.
- **The text is never stored or logged.** Do not put it in an error, a log line, a
  trace or a debug dump (in Rust a derived `Debug`, in Go a `String()` or `%v`, in Node a
  `console.log(options)` print the struct: redact the text field and print only its length).
  A request's body holds it; nothing the SDK raises may. The API keeps an HMAC-SHA256 of it
  (server-side secret) and its length.
- **Wording.** Never say urgent text is "spoken once"; never say the system "never listens"
  or "nothing listens": say intercom does not record or store speech and that, while an
  alert sounds, the hub only checks whether a reply is an acknowledgement. Never say "stops
  every announcement at once": name the real controls (`cancel_announcement`, the hub
  switch, the platform kill switch, a speaker's `receive`, voice acknowledgement).
- **Names.** `announce*` and `*_intercom_*`; never a bare `intercom` (the mesh frame).
- **Paths.** `/v1/hubs/{hub_id}/intercom/...`, the hub id and the speaker or announcement
  id each one percent-escaped path segment. **Refuse, with a validation error and before any
  request,** an id that is not a string, is empty or blank, is `.` or `..` (dot segments), or
  holds `/`, `?`, `#`, a backslash or a control character (C0, DEL, C1).
- **Validate before sending.** Text trimmed, 1-280 characters, no control characters
  (C0, DEL, C1, U+2028/2029). Targets: exactly one of `all` (true), `speakers`
  (at most 50), `rooms`, `groups`. `priority` `normal` or `urgent`; `repeat`
  (`times` 1-10, `interval_seconds` 5-120) only with `urgent`. Speaker patch: only the
  fields passed (`room` may be null to clear it, so "not passed" is distinct from null),
  at least one, room at most 64 characters, at most 8 groups of at most 32, quiet hours
  `HH:MM`.
- **`Idempotency-Key`** only when the caller gives one. No automatic retry. Key scope is
  `(hub, key)` for 24 h; the same key replays the stored announcement (response header
  `Idempotency-Replayed: true`) and NEVER sends again, whatever its status: a replayed
  `failed` stays `failed`. Every example and doc uses a FRESH unique key (a uuid) per
  intended announcement, never a constant such as `dinner-2026-11-11`, and says: to send
  the same text again on purpose use a NEW key; after a 502 `hub_unreachable` read the
  announcement (`announcement_id` in the problem body) before sending again; reuse a key
  only for the same attempt after a network drop or timeout.
- **Cancel.** `cancel_announcement(hub_id, announcement_id)`: `POST
  /v1/hubs/{hub}/intercom/announcements/{id}/cancel`, no body, scope `intercom:send`;
  200 with the announcement (status `cancelled`, optional `hub_confirmed`: `false` when the
  hub could not confirm). `Announcement.hub_confirmed` is optional (absent on every other
  answer). 409 `not_cancellable` (only a `sending` announcement can be cancelled) is a typed
  API error. The status enum gains `cancelled` (still an open enum).
- **502 `hub_unreachable`** exposes `announcement_id` (top of the problem or inside an object
  `detail`) wherever the SDK exposes problem fields.
- **Timeout.** The API waits up to 10 s for the hub: an SDK with a request timeout never
  uses less than 20 s (Python: 25 s) for the announce POST. Node, Go and Rust set none.
- **Waiting.** `wait` polls `get_announcement` while the status is `sending`, every
  `poll_interval` (default 2 s), until `wait_timeout` (default `times * interval + 15` s);
  on timeout it returns the last answer, still `sending`; it does not fail. Validate
  `wait_timeout` (a finite number, not negative) and `poll_interval` (finite, above zero)
  BEFORE the POST: a bad value must never surface after the announcement was accepted.
  The deadline bounds the sleep (abortable by the caller's cancellation) and each status
  read: after a sleep, if no time is left, start no read; otherwise bound the read by the
  time left and, if it runs out, return the last answer. `wait` needs `intercom:read` as
  well as `intercom:send`; a poll can fail after the POST was accepted, so document that
  and tell callers to call again with the same `Idempotency-Key`, which replays the stored
  announcement (with its id) instead of sending again.
- **Targets and do-not-disturb.** A target list must be a sequence of strings: refuse a
  bare string (`{"speakers": "ab"}` must never become `["a", "b"]`) and a non-list. A DND
  `enabled` must be a real boolean and must be present (the API replaces the whole DND
  object and stores a missing `enabled` as false): raise a validation error, never
  coerce it to false.
- **Open enums.** Announcement status, delivery status, priority and the settings'
  `unavailable_reason` are strings; unknown values survive.
- **Errors.** `intercom_disabled` (any status), 502 `hub_unreachable`, 429 (with
  `retry_after_seconds`) have their own kind; the plan error is HTTP 402 and also HTTP 403
  with code `plan_limit`; the rest are the usual API error with status, code and detail.
  Do not say `hub_unreachable` is safe to retry: whether the text was spoken is not
  guaranteed, and the same `Idempotency-Key` replays the stored `failed` record without
  sending, so a deliberate second send needs a new key. `not_cancellable` (409) has its own kind.
- **Shapes.** Speakers `{"items": [...]}`; announcements list `{"items": [...], "next_cursor"}`
  (the contract names `limit` and `cursor`; `data` and `cursor` are accepted too).

## Scopes

`intercom:read` (settings, speakers, announcements), `intercom:send` (announce, cancel),
`hubs:write` (set settings, update a speaker).
