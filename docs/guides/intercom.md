# Intercom

Say a short text aloud on a household hub's speakers: "dinner is ready" in the kitchen,
or an urgent "wake up" on every speaker that repeats until somebody answers.

!!! warning "Not a safety system"
    Intercom is announce-only. It is **not a certified safety system** and does not
    replace smoke or CO alarms, medical alert devices or emergency services. There is
    no two-way audio. Intercom does not record or store speech; while an alert sounds,
    the hub only checks whether a reply is an acknowledgement. The text is spoken by
    each client's own text-to-speech.

## What is on and what is not

Everything is off until somebody opts in, at every level:

- the hub's switch (`set_intercom_settings(hub_id, True)`), which needs a plan with
  intercom and is refused on kid hubs and on public hubs;
- each speaker's `receive`, which is `False` until somebody sets it (a speaker that does
  not receive is skipped, even for an urgent announcement).

The API does not store the text. It keeps a keyed hash of it (HMAC-SHA256 with a
server-side secret, so a short sentence cannot be confirmed by guessing) and its length,
the targets, each speaker's result, and who sent it. The hub's own message bus and each
speaker's text-to-speech are outside the platform's control.

## Scopes

| Call | Scope |
|---|---|
| `get_intercom_settings`, `list_intercom_speakers`, `get_announcement`, `list_announcements` | `intercom:read` |
| `announce`, `cancel_announcement` | `intercom:send` (and `intercom:read` too with `announce(wait=True)`, which polls `get_announcement`) |
| `set_intercom_settings`, `update_intercom_speaker` | `hubs:write` (owner or admin of the hub) |

## Names: `announce_*` and `intercom_*`, not `intercom`

The HiveMind mesh already has an `intercom` frame (a client talking to another client
through the hub). That is a different feature. The HTTP calls here are named
`announce`, `cancel_announcement`, `get_announcement`, `list_announcements` and
`*_intercom_*`, never plain `intercom`.

## Example

```python
from uuid import uuid4

from thalovant import IntercomDnd, IntercomQuietHours, IntercomRepeat, IntercomTargets, ThalovantControlPlane

api = ThalovantControlPlane(access_token="...")
hub = "<hub id>"

api.set_intercom_settings(hub, True)                                # hubs:write
speakers = api.list_intercom_speakers(hub).items
for speaker in speakers:
    print(speaker.name, "online" if speaker.online else "offline", speaker.intercom.receive)

# The speaker is addressed by its client_id (from the list), not by a room or a name.
api.update_intercom_speaker(hub, speakers[0].client_id, receive=True, room="kitchen", groups=["downstairs"],
                            dnd=IntercomDnd(True, IntercomQuietHours("22:00", "07:00", "Europe/Paris")))

# A fresh unique key for each announcement you mean to send (see "Idempotency keys" below).
result = api.announce(hub, "Dinner is ready", IntercomTargets.for_rooms("kitchen"),
                      idempotency_key=str(uuid4()))
print(result.status, [(d.name, d.status) for d in result.deliveries])

urgent = api.announce(hub, "Please answer", IntercomTargets.everyone(), priority="urgent",
                      repeat=IntercomRepeat(times=3, interval_seconds=20),
                      idempotency_key=str(uuid4()), wait=True)
print(urgent.status, urgent.acked_by)

# Changed your mind, or sent it to the wrong room? Stop it while it is still repeating.
sent = api.announce(hub, "Wake up", IntercomTargets.everyone(), priority="urgent",
                    repeat=IntercomRepeat(times=10, interval_seconds=60), idempotency_key=str(uuid4()))
stopped = api.cancel_announcement(hub, sent.id)          # intercom:send
print(stopped.status, stopped.hub_confirmed)             # "cancelled", True when the hub confirmed
```

A normal announcement waits up to ten seconds for the hub and returns `completed`,
`partial` or `failed`, with one delivery per speaker (`delivered`, `acked`, `offline`,
`skipped_dnd`, `skipped_not_receiving`, `failed`). An urgent one with `repeat` returns at
once as `sending`; poll `get_announcement`, or pass `wait=True`. Statuses are open: a
value this SDK does not know is kept as sent (`cancelled` is one the SDK knows).

### Idempotency keys: one fresh key per announcement

Pass `idempotency_key=str(uuid4())` for every announcement you mean to send. The API scopes
a key to `(hub, key)` and remembers it for 24 hours:

- **The same key replays the stored announcement** (the response header is
  `Idempotency-Replayed: true`) and **never sends again**, whatever its status. A replayed
  `failed` announcement stays `failed`; it is not retried.
- **A constant key silences you.** A literal key such as `"dinner"` sends once; every later
  announcement with that key, for 24 hours, returns the first one's answer with
  `completed` and nothing is spoken. Never copy a literal key into real code; never derive
  the key from the text or the date.
- **Reuse a key only for the same attempt**, after a network drop or a timeout: the call then
  returns what the first attempt did (and its id) instead of speaking twice.
- **To send the same text again on purpose, use a new key.**
- **After a 502 `hub_unreachable`** (`ThalovantHubUnreachableError`) the hub may or may not
  have spoken. The API recorded the attempt as a `failed` announcement and puts its id in the
  problem body: `error.announcement_id`. Read it with `get_announcement(hub, error.announcement_id)`
  and look at the deliveries before you send again. Resending with the **same** key will not
  retry (it replays the `failed` record); a deliberate second send needs a **new** key.

```python
from thalovant import ThalovantHubUnreachableError

key = str(uuid4())
try:
    result = api.announce(hub, "Dinner is ready", IntercomTargets.for_rooms("kitchen"), idempotency_key=key)
except ThalovantHubUnreachableError as error:
    recorded = api.get_announcement(hub, error.announcement_id)   # what did the hub do?
    print(recorded.status, [(d.name, d.status) for d in recorded.deliveries])
    # Only if nothing was spoken and you still want it said: api.announce(..., idempotency_key=str(uuid4()))
```

### Stopping an announcement

An urgent announcement that repeats can be stopped, and these are the real controls:

- **`cancel_announcement(hub_id, announcement_id)`** (`intercom:send`, owner or admin of the
  hub) asks the hub to stop repeating and returns the announcement as `cancelled`. It works
  only while the status is `sending`; for an announcement that already completed, failed or
  was cancelled the API answers 409 `not_cancellable` (`ThalovantNotCancellableError`). If the
  hub cannot confirm the cancel, the record is still `cancelled` and `hub_confirmed` is
  `False`: speech already queued on the hub may still play.
- **The hub's Intercom switch** (`set_intercom_settings(hub, False)`, `hubs:write`) takes
  effect when the hub's runtime updates, which can take as long as a rollout, and the hub's
  repeat loop also stops by itself when it sees the switch off.
- **The platform kill switch** (operator side) and a speaker whose `receive` is turned off
  also stop the repeats.
- **Somebody answering at a speaker.** An urgent announcement stops being repeated once it
  is acknowledged by voice ("I'm okay", "got it", "stop").

Nothing here is instant for sound that is already playing, and none of it makes intercom a
safety system.

### Waiting, and what can fail after the send

`wait=True` needs **`intercom:read` as well as `intercom:send`**, because the polls are
`get_announcement`. `wait_timeout` (finite, not negative) and `poll_interval` (finite,
above zero) are validated before anything is sent, and the deadline bounds each poll as
well as the sleep between polls: on timeout the latest answer comes back, still
`sending`.

A poll can fail after the POST was accepted: a token with only `intercom:send` is
refused at the first poll (403), or a 429 or the network interrupts it. The exception
then reaches you after the announcement exists, and you have no announcement id. Always
pass a fresh `idempotency_key` with `wait=True`: calling again with that same key replays
the stored announcement (with its id) and does not announce a second time.

### Timeout

The API waits up to ten seconds for the hub to answer a normal announcement. So `announce`
never uses less than 25 seconds for its request (`INTERCOM_ANNOUNCE_MIN_TIMEOUT`), even when
the client was built with a shorter `timeout` (the default is 10 s): a reply that arrives at
nine seconds must not look like a failure. A longer client timeout is kept; every other call
uses the client's own timeout. If `announce` still raises `ThalovantAPIUnreachableError`, the
outcome is unknown: call again with the **same** key to find out, never with a new one.

### Ids

The hub id, a speaker's `client_id` and an announcement id are each one path segment. The SDK
refuses, with `ValueError` and before any request, an id that is empty or blank, is `.` or
`..` (dot segments), or holds `/`, `?`, `#`, a backslash or a control character.

Targets and do-not-disturb are checked before anything is sent: `targets` lists must be
real lists of names (a bare string such as `{"speakers": "ab"}` is refused, never split
into `a` and `b`), and a speaker's `dnd` needs `enabled` as a real boolean (the API
replaces the whole do-not-disturb object, so an omitted or non-boolean `enabled` is
refused rather than stored as `false`).

Do-not-disturb skips normal announcements inside quiet hours; urgent ones bypass it.
Limits: 280 characters, 50 named speakers, 10 repeats at 5 to 120 seconds, and per hub
20 normal announcements a minute and 10 urgent ones an hour (a `ThalovantRateLimitedError`
with `retry_after_seconds`).

## Errors

| Error | When |
|---|---|
| `ThalovantIntercomDisabledError` | code `intercom_disabled`: the hub's switch is off, or it is a kid or public hub |
| `ThalovantHubUnreachableError` | 502 `hub_unreachable`: the hub did not answer; the text may or may not have been spoken. `announcement_id` names the `failed` record: read it before sending again, and use a **new** key to send again (the same key replays the failed record) |
| `ThalovantNotCancellableError` | 409 `not_cancellable`: `cancel_announcement` on an announcement that is not `sending` |
| `ThalovantRateLimitedError` | 429 |
| `ThalovantPlanError` | the plan has no intercom: HTTP 402, or 403 with code `plan_limit` |
| `ThalovantAuthError` | missing scope or token |
