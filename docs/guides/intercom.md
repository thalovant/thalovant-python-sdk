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
| `announce` | `intercom:send` |
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
for speaker in api.list_intercom_speakers(hub).items:
    print(speaker.name, "online" if speaker.online else "offline", speaker.intercom.receive)

api.update_intercom_speaker(hub, "kitchen", receive=True, room="kitchen", groups=["downstairs"],
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

Do-not-disturb skips normal announcements inside quiet hours; urgent ones bypass it.
Limits: 280 characters, 50 named speakers, 10 repeats at 5 to 120 seconds, and per hub
20 normal announcements a minute and 10 urgent ones an hour (a `ThalovantRateLimitedError`
with `retry_after_seconds`).

## Errors

| Error | When |
|---|---|
| `ThalovantIntercomDisabledError` | code `intercom_disabled`: the hub's switch is off, or it is a kid or public hub |
| `ThalovantHubUnreachableError` | 502 `hub_unreachable`: nothing was spoken |
| `ThalovantRateLimitedError` | 429 |
| `ThalovantPlanError` | the plan has no intercom (`plan_limit`) |
| `ThalovantAuthError` | missing scope or token |
