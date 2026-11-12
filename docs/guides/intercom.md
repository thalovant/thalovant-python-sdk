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
| `announce`, `cancel_announcement` | `intercom:send`, which implies `intercom:read`: `announce(wait=True)` polls `get_announcement` with it and needs nothing more |
| `announce(priority="urgent", ...)` (every `repeat` is urgent) | `intercom:urgent` **in addition to** `intercom:send` |
| `set_intercom_settings`, `update_intercom_speaker` | `hubs:write` (owner, manager or administrator of the hub) |

`intercom:urgent` is not implied by anything and implies nothing (`intercom:send` still
implies only `intercom:read`). A token without it is refused before anything is spent, with
`ThalovantUrgentScopeRequiredError` (403 `urgent_scope_required`): no idempotency lookup, no
rate token, no hub call. Free plans cannot mint it, so request it only when you offer an urgent
control, and hide that control when the token lacks it.

Writing needs the tenant owner, a manager or a platform administrator acting as themselves: a
plain member can read but not enable, change a speaker or announce, and an impersonated session
cannot announce (`ThalovantImpersonationNotAllowedError`). Switching intercom off and
`cancel_announcement` stay open to both, and to a locked hub (a locked hub otherwise refuses
with `ThalovantHubLockedError`).

### Dark by default: the platform switch

Until the platform operator turns intercom on, `get_intercom_settings` reads `enabled=False,
unavailable_reason="platform"` for every hub, and every write and every announcement is refused
with `ThalovantIntercomPlatformDisabledError` (503 `intercom_disabled_platform`; it is a kind of
`ThalovantIntercomDisabledError`). Nothing is stored or sent. Treat it as "show the platform
message", not as an error toast; reads and `cancel_announcement` keep working. A platform that is
not configured yet answers `ThalovantIntercomNotConfiguredError` (503 `intercom_not_configured`).

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

# The speaker is addressed by its client_id (from the list), not by a room or a name. A connection
# with can_play=False (a Home Assistant link, say) is listed but cannot receive: pick one that can.
speaker = next((s for s in speakers if s.can_play), None)
if speaker is None:
    raise SystemExit("This hub has no connection that can play speech.")
api.update_intercom_speaker(hub, speaker.client_id, receive=True, room="kitchen", groups=["downstairs"],
                            dnd=IntercomDnd(True, IntercomQuietHours("22:00", "07:00", "Europe/Paris")))

# A fresh unique key for each announcement you mean to send (see "Idempotency keys" below).
result = api.announce(hub, "Dinner is ready", IntercomTargets.for_rooms("kitchen"),
                      idempotency_key=str(uuid4()))
print(result.status, [(d.name, d.status) for d in result.deliveries])

urgent = api.announce(hub, "Please answer", IntercomTargets.everyone(), priority="urgent",
                      repeat=IntercomRepeat(times=3, interval_seconds=20),
                      idempotency_key=str(uuid4()), wait=True)
print(urgent.status, urgent.acked_by)

# priority="urgent" (and so every repeat) needs the scope intercom:urgent as well as intercom:send.
# Changed your mind, or sent it to the wrong room? Stop it while it is still repeating.
sent = api.announce(hub, "Wake up", IntercomTargets.everyone(), priority="urgent",
                    repeat=IntercomRepeat(times=10, interval_seconds=60), idempotency_key=str(uuid4()))
stopped = api.cancel_announcement(hub, sent.id)          # intercom:send
print(stopped.status, stopped.hub_confirmed)             # "cancelled", True when the hub confirmed
```

### Speakers: `kind`, `can_play` and `policy_synced`

`list_intercom_speakers` lists every connection of the hub (the platform's own Intercom
client is not listed). Each `IntercomSpeaker` has:

- `kind`: the connection kind (`voice_satellite`, `web_chat`, `developer`, `embedded`,
  `home_assistant`; anything else reads `voice_satellite`).
- `can_play`: the **effective** "can this connection play speech". True by default for a
  `voice_satellite` and `web_chat`; false for `home_assistant`, `developer` and `embedded`
  unless its owner declared `spec.intercom.can_play: true` on the **connection**
  (`PATCH /v1/clients/{id}` with `clients:write`; never on the speaker). A connection that cannot
  play is listed (grey it out) but: `receive=True` is refused with `ThalovantNotASpeakerError`
  (422 `not_a_speaker`, `error.kind`), it is excluded from `all`, rooms and groups, and naming
  it in `targets.speakers` is the same error with `error.not_speakers`.
- `policy_synced`: set on the answer to `update_intercom_speaker` (`None` on list items). The list
  itself has `policy_synced` and `unavailable_reason`. `False` means the hub may hold an
  out-of-date consent map (a push failed and is retried in the background), so a speaker whose
  `receive` was just turned off can still be spoken to by a routine until it clears: show a quiet
  "Changes may take a moment to reach the home".

`update_intercom_speaker` takes exactly `receive`, `room`, `groups` and `dnd`; send only what
changed. Do-not-disturb that is switched on needs `quiet_hours` with a valid IANA `timezone`
(at most 64 characters, e.g. `Europe/Paris`) and a `start` that differs from the `end`; a
mapping with any other key is refused. All of that is checked before anything is sent.

```python
from thalovant import ThalovantNotASpeakerError

for speaker in api.list_intercom_speakers(hub).items:
    if not speaker.can_play:
        print(speaker.name, "cannot play speech (kind:", speaker.kind + ")")   # grey it out
try:
    api.update_intercom_speaker(hub, "<a Home Assistant link's client id>", receive=True)
except ThalovantNotASpeakerError as error:
    print("not a speaker:", error.kind)
```

### The text

`announce` checks the text with the API's own rule before anything is sent, so a refusal never
costs a request and never echoes the text: NFC-normalised and trimmed, 1 to 280 characters (counted
after that), no `<` or `>` (a speaker's text-to-speech may read them as markup), no control
characters (a newline or a tab included), no bidirectional overrides or isolates and no other
invisible format characters (the zero-width joiner and non-joiner are allowed), no lone surrogates,
no private-use characters and no line or paragraph separators. `lang`, when given, is a language
tag (`en`, `en-US`). Target lists hold at most 50 names of 1 to 64 characters each; `all`, a room or
a group that resolves to more than 50 speakers is `ThalovantTooManyTargetsError` (422
`too_many_targets`, `error.matched`). Naming the same room or group resolves to the same speakers:
choose a narrower target or name at most 50 speakers.

### What an answer means

`delivered` means the hub handed the speech to that speaker's connection. There is **no playback
receipt**: do not say "heard" or "played". Only `acked` (an urgent announcement somebody answered)
is a person's answer, and `failed` is not proof that nothing was spoken. A delivery's `detail` is
a fixed code from `thalovant.intercom.DELIVERY_DETAIL_CODES` (`hub_no_response`, `rate_limited`,
`skill_not_installed`, ...) or `None`; treat a code you do not know as opaque. `acked_by` is the
display name of a client of the hub, or the literal `"unknown"`. A hub that does not answer within
5 seconds ends `failed` with every delivery `hub_no_response`. A repeating urgent announcement is
failed on read once it is older than `times x interval + 90 s`.

At most two urgent announcements repeat on one hub at a time (the platform has a pool too): the next
is `ThalovantUrgentBusyError` (429 `intercom_urgent_busy`, `error.scope` `hub` or `platform`,
`retry_after_seconds`) with nothing stored and no rate token spent: cancel one that is running, or
wait. An urgent announcement on a hub whose runtime has no active Intercom skill is
`ThalovantSkillNotInstalledError` (409 `skill_not_installed`): nothing was sent.

A normal announcement waits up to ten seconds for the hub and returns `completed`,
`partial` or `failed`, with one delivery per speaker (`delivered`, `acked`, `offline`,
`skipped_dnd`, `skipped_not_receiving`, `failed`). An urgent one with `repeat` returns at
once as `sending`; poll `get_announcement`, or pass `wait=True`. Statuses are open: a
value this SDK does not know is kept as sent (`cancelled` is one the SDK knows).

### Idempotency keys: one fresh key per announcement

Pass `idempotency_key=str(uuid4())` for every announcement you mean to send. The API scopes
a key to `(hub, key)` and remembers it for 24 hours:

- **The same key replays the stored announcement** (the response header is
  `Idempotency-Replayed: true`, which the SDK reads into `announcement.replayed`) and **never
  sends again**, whatever its status. A replayed `failed` announcement stays `failed`; it is not
  retried. The same key with a different request is a 409. A key is at most 128 printable ASCII
  characters. Do not clear what a person typed, or say "sent", on `replayed=True` without saying
  that nothing new was sent.
- **A constant key silences you.** A literal key such as `"dinner"` sends once. For the next 24
  hours a later announcement with that key and the **same** request replays the first one's stored
  answer, with the status it stored (`completed`, `failed`, ...), and nothing is spoken; one with a
  **different** request is a 409. Never copy a literal key into real code; never derive the key from
  the text or the date.
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
    # The hub may have spoken it. If you still choose to say it again, weigh the risk of a repeat
    # and use a NEW key on purpose: api.announce(..., idempotency_key=str(uuid4()))
```

### Stopping an announcement

An urgent announcement that repeats can be stopped, and these are the real controls:

- **`cancel_announcement(hub_id, announcement_id)`** (`intercom:send`; the hub's owner, a manager,
  a plain member of the tenant or an administrator, and an impersonated session too: stopping
  speech is not restricted to the people who may announce) asks the hub to stop repeating and returns the announcement as `cancelled`. It works
  only while the status is `sending`; for an announcement that already completed, failed or
  was cancelled the API answers 409 `not_cancellable` (`ThalovantNotCancellableError`). If the
  hub cannot confirm the cancel, the record is still `cancelled` and `hub_confirmed` is
  `False`: speech already queued on the hub may still play. `ThalovantNotCancellableError`
  carries `announcement_status` (the current one): refresh the result instead of showing an error.
  Stopping is never refused for platform, hub or plan reasons: it works while the platform switch
  is off (the answer then has `hub_confirmed=False` and no hub was called).
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

`wait=True` polls with `get_announcement` (`intercom:read`), which a token with `intercom:send`
already holds because `intercom:send` implies `intercom:read`. `wait_timeout` (finite, not negative) and `poll_interval` (finite,
above zero) are validated before anything is sent, and the deadline bounds each poll as
well as the sleep between polls: on timeout the latest answer comes back, still
`sending`.

A poll can fail after the POST was accepted: a 429 or the network interrupts it. The exception then reaches you after the announcement
exists, and you have no announcement id. Always pass a fresh `idempotency_key` with
`wait=True`, create the key **before** the first call and keep that exact key: a new key is a
different attempt and can announce twice. To get the id back, call again with that same key and
`wait=False`: the POST
replays the stored announcement, with its id, makes no status read and does not announce a
second time; then poll it with `get_announcement` once reads work again. The same call with
`wait=True` polls again while the announcement is still `sending`, so it can raise the same
poll error.

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

Do-not-disturb skips normal announcements inside quiet hours (in the zone you gave); urgent ones
bypass it.
Limits: 280 characters, 50 named speakers, 10 repeats at 5 to 120 seconds, and per hub
20 normal announcements a minute and 10 urgent ones an hour (a `ThalovantRateLimitedError`
with `retry_after_seconds`).

## Errors

`IntercomProblemCode` holds each `code` as a constant. Problems are `application/problem+json`:
read `code`, then `problem["message"]` (or `detail`); the typed error says which it is.

| Error | When |
|---|---|
| `ThalovantIntercomDisabledError` | code `intercom_disabled`: the hub's switch is off (the two kinds below are subclasses: check them first, since `except ThalovantIntercomDisabledError` catches them too) |
| `ThalovantIntercomUnavailableError` | 422 `intercom_unavailable`: never available on a kid hub or a public hub; `reason` is `kid_hub` or `public_hub` (a kind of `ThalovantIntercomDisabledError`) |
| `ThalovantIntercomPlatformDisabledError` | 503 `intercom_disabled_platform`: switched off for the whole platform (a kind of `ThalovantIntercomDisabledError`); nothing was written or sent |
| `ThalovantIntercomNotConfiguredError` | 503 `intercom_not_configured`: the operator has not finished setting it up |
| `ThalovantUrgentScopeRequiredError` | 403 `urgent_scope_required`: an urgent announcement needs `intercom:urgent` as well (a kind of `ThalovantAuthError`) |
| `ThalovantUrgentBusyError` | 429 `intercom_urgent_busy`: the urgent pool is full; `scope` (`hub` or `platform`), `retry_after_seconds` (a kind of `ThalovantRateLimitedError`) |
| `ThalovantSkillNotInstalledError` | 409 `skill_not_installed`: an urgent announcement on a hub with no Intercom skill |
| `ThalovantNotASpeakerError` | 422 `not_a_speaker`: `receive=True` (`kind`) or a `speakers` target (`not_speakers`) on a connection that cannot play speech |
| `ThalovantTooManyTargetsError` | 422 `too_many_targets`: more than 50 speakers resolved; `matched` |
| `ThalovantReservedNameError` | 422 `reserved_name`: a connection named like the platform's Intercom client |
| `ThalovantManagedClientError` | 403 `managed_client`: the platform's Intercom client cannot be managed or given credentials; `refused_client_refs` for an access policy that named it |
| `ThalovantHubLockedError` | 403 `hub_locked`: nothing was changed |
| `ThalovantImpersonationNotAllowedError` | 403 `impersonation_not_allowed`: an impersonated session cannot announce |
| `ThalovantHubUnreachableError` | 502 `hub_unreachable`: the hub did not answer; the text may or may not have been spoken. `announcement_id` names the `failed` record: read it before sending again, and use a **new** key to send again (the same key replays the failed record) |
| `ThalovantNotCancellableError` | 409 `not_cancellable`: `cancel_announcement` on an announcement that is not `sending`; `announcement_status` |
| `ThalovantRateLimitedError` | 429 (a hub: 20 normal a minute, 10 urgent an hour) |
| `ThalovantPlanError` | the plan has no intercom: HTTP 402, or 403 with code `plan_limit` |
| `ThalovantAuthError` | missing scope or token |

`owner_locked` and `account_locked` (403) arrive as a plain `ThalovantAPIError` with `code` set.
