# Intercom

Say a short text aloud on a household hub's speakers: "dinner is ready" in the kitchen,
or an urgent "wake up" on every speaker that repeats until somebody answers.

!!! warning "Not a safety system"
    Intercom is announce-only. It is **not a certified safety system** and does not
    replace smoke or CO alarms, medical alert devices or emergency services. There is
    no two-way audio and nothing listens: the text is spoken by each client's own
    text-to-speech.

## What is on and what is not

Everything is off until somebody opts in, at every level:

- the hub's switch (`set_intercom_settings(hub_id, True)`), which needs a plan with
  intercom and is refused on kid hubs and on public hubs;
- each speaker's `receive`, which is `False` until somebody sets it (a speaker that does
  not receive is skipped, even for an urgent announcement).

The platform does not keep the text. It keeps the text's SHA-256 and length, the targets,
each speaker's result, and who sent it.

## Scopes

| Call | Scope |
|---|---|
| `get_intercom_settings`, `list_intercom_speakers`, `get_announcement`, `list_announcements` | `intercom:read` |
| `announce` | `intercom:send` (and `intercom:read` too with `wait=True`, which polls `get_announcement`) |
| `set_intercom_settings`, `update_intercom_speaker` | `hubs:write` (owner or admin of the hub) |

## Names: `announce_*` and `intercom_*`, not `intercom`

The HiveMind mesh already has an `intercom` frame (a client talking to another client
through the hub). That is a different feature. The HTTP calls here are named
`announce`, `get_announcement`, `list_announcements` and `*_intercom_*`, never
plain `intercom`.

## Example

```python
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

result = api.announce(hub, "Dinner is ready", IntercomTargets.for_rooms("kitchen"))
print(result.status, [(d.name, d.status) for d in result.deliveries])

urgent = api.announce(hub, "Please answer", IntercomTargets.everyone(), priority="urgent",
                      repeat=IntercomRepeat(times=3, interval_seconds=20),
                      idempotency_key="alarm-2026-11-11", wait=True)
print(urgent.status, urgent.acked_by)
```

A normal announcement waits up to ten seconds for the hub and returns `completed`,
`partial` or `failed`, with one delivery per speaker (`delivered`, `acked`, `offline`,
`skipped_dnd`, `skipped_not_receiving`, `failed`). An urgent one with `repeat` returns at
once as `sending`; poll `get_announcement`, or pass `wait=True`. Statuses are open: a
value this SDK does not know is kept as sent.

### Waiting, and what can fail after the send

`wait=True` needs **`intercom:read` as well as `intercom:send`**, because the polls are
`get_announcement`. `wait_timeout` (finite, not negative) and `poll_interval` (finite,
above zero) are validated before anything is sent, and the deadline bounds each poll as
well as the sleep between polls: on timeout the latest answer comes back, still
`sending`.

A poll can fail after the POST was accepted: a token with only `intercom:send` is
refused at the first poll (403), or a 429 or the network interrupts it. The exception
then reaches you after the announcement exists, and you have no announcement id. Always
pass an `idempotency_key` with `wait=True` and reuse it to retry, so a retry is not
announced a second time.

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
| `ThalovantHubUnreachableError` | 502 `hub_unreachable`: the hub did not answer; the text may or may not have been spoken, so retry with the same `idempotency_key` |
| `ThalovantRateLimitedError` | 429 |
| `ThalovantPlanError` | the plan has no intercom: HTTP 402, or 403 with code `plan_limit` |
| `ThalovantAuthError` | missing scope or token |
