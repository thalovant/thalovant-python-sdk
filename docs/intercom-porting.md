# Porting intercom

Last Edit: Claude Sonnet 5.5 - 2026-11-12 - Motive: Intercom review round 2 (CodeRabbit PR 114): requests-style session read off the loop; wait scope (send implies read), key replay, cancel permissions.

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
- **Validate before sending (one text rule, the API's own).** Text NFC-normalised then
  trimmed, 1-280 characters (counted after that); refuse `<` and `>`, every character of the
  categories Cc, Cf, Cs, Co, Zl and Zp except U+200C and U+200D (so controls incl. newline and
  tab, bidi overrides and isolates, zero-width space, word joiner, BOM, tag characters, lone
  surrogates, private use, U+2028/2029); never echo the text in the error. `lang`, when given,
  matches `^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$`. Targets: exactly one of `all` (true), `speakers`,
  `rooms`, `groups`; each list at most 50 names of 1-64 characters. `Idempotency-Key` at most 128
  printable ASCII characters. `priority` `normal` or `urgent`; `repeat`
  (`times` 1-10, `interval_seconds` 5-120) only with `urgent`. Speaker patch: only the
  fields passed (`room` may be null to clear it, so "not passed" is distinct from null),
  at least one, room at most 64 characters, at most 8 groups of at most 32, quiet hours
  `HH:MM`. Only `receive`, `room`, `groups` and `dnd` exist: refuse any other key (also inside
  `dnd` and `quiet_hours`). `dnd.enabled: true` needs `quiet_hours` with a `timezone` that is
  a valid IANA name (at most 64 characters, not padded) and `start != end`; any non-null
  `timezone` is checked even when `dnd` is off. Where the platform has no timezone database,
  check only the shape and let the API decide.
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
  time left and, if it runs out, return the last answer. The polls are reads (`intercom:read`,
  which `intercom:send` implies, so `wait` needs no further scope); a poll can fail after the
  POST was accepted, so document that and tell callers to call again with the same
  `Idempotency-Key` and `wait=False`: the POST replays the stored announcement (with its id,
  no status read) instead of sending again. A repeat call with `wait` polls again.
  Blocking transports: do the read off the event loop (a worker thread) so the deadline can
  abandon a stalled read.
- **Targets and do-not-disturb.** A target list must be a sequence of strings: refuse a
  bare string (`{"speakers": "ab"}` must never become `["a", "b"]`) and a non-list. A DND
  `enabled` must be a real boolean and must be present (the API replaces the whole DND
  object and stores a missing `enabled` as false): raise a validation error, never
  coerce it to false.
- **Open enums.** Announcement status, delivery status, priority and the settings'
  `unavailable_reason` (`platform`, `plan`, `kid_hub`, `public_hub`) are strings; unknown values
  survive.
- **Models.** `IntercomSpeaker` gains `kind` (default `voice_satellite`), `can_play` (default
  true; only an explicit false is false) and `policy_synced` (optional: present on the PATCH
  answer); the speaker list gains `policy_synced` and `unavailable_reason`; a delivery gains
  `detail` (a fixed code or null; keep unknown codes); an announcement gains `replayed`, read
  from the response header `Idempotency-Replayed: true` of `announce` (false otherwise, and kept
  after `wait`). `hub_confirmed` stays optional (the cancel answer only).
- **Scope `intercom:urgent`.** Needed in addition to `intercom:send` for `priority: "urgent"`
  (every `repeat` is urgent); implied by nothing, implies nothing. Document it wherever the
  scopes are listed; the SDK cannot see a token's scopes, so the 403 `urgent_scope_required`
  is a typed error.
- **Errors.** Typed kinds (and constants) for the codes `intercom_disabled` (any status),
  `intercom_unavailable` (422, `reason`; a kind of disabled), `intercom_disabled_platform` (503; a
  kind of disabled), `intercom_not_configured` (503), `urgent_scope_required` (403; a kind of the
  auth error), `intercom_urgent_busy` (429, `scope`; a kind of the rate-limit error),
  `skill_not_installed` (409), `not_a_speaker` (422; `kind`, `not_speakers`), `reserved_name`
  (422), `hub_locked` (403), `impersonation_not_allowed` (403), `too_many_targets` (422;
  `matched`), `managed_client` (403; `refused_client_refs`), `not_cancellable` (409;
  `announcement_status`), 502 `hub_unreachable` (`announcement_id`), and 429 (with
  `retry_after_seconds`). The members live at the top of the problem or inside an object `detail`;
  a value of the wrong type reads as absent. The code decides, whatever the status; the plan error is HTTP 402 and also HTTP 403
  with code `plan_limit`; the rest are the usual API error with status, code and detail.
  `delivered` is not "heard": there is no playback receipt. Do not say `hub_unreachable` is safe to retry: whether the text was spoken is not
  guaranteed, and the same `Idempotency-Key` replays the stored `failed` record without
  sending, so a deliberate second send needs a new key. `not_cancellable` (409) has its own kind.
- **Shapes.** Speakers `{"items": [...]}`; announcements list `{"items": [...], "next_cursor"}`
  (the contract names `limit` and `cursor`; `data` and `cursor` are accepted too).

## Scopes

`intercom:read` (settings, speakers, announcements), `intercom:send` (announce, cancel),
`intercom:urgent` (urgent announcements, in addition to `intercom:send`), `hubs:write` (set
settings, update a speaker).
