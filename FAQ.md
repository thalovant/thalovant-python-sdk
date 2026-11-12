Last Edit: Claude Sonnet 5.5 - 2026-11-11 - Motive: Intercom (0.12.0): wait scope and retry questions after the PR 114 review; final API questions (urgent scope, can_play, replayed, platform switch).

# FAQ: thalovant (Python SDK)

**Why is there no `intercom()` method, and is intercom a safety system?**
The HiveMind mesh already has an `intercom` frame (hub-routed messages between clients), so the HTTP calls are `announce`, `get_announcement`, `list_announcements` and `*_intercom_*`. Intercom is announce-only text spoken by each speaker's own TTS; it is **not a certified safety system** and does not replace smoke or CO alarms, medical alert devices or emergency services. The API does not store the text (a keyed hash and the length only), and intercom does not record or store speech; while an alert sounds it only checks whether a reply is an acknowledgement.

**Why did `announce(wait=True)` raise after the announcement was accepted?**
`wait=True` polls `get_announcement`, which needs `intercom:read` as well as `intercom:send`; a poll can also fail on a 429 or the network. The announcement exists and the exception has no id. Pass a fresh `idempotency_key` (`str(uuid4())`) and call again with that same key: the API replays the stored announcement, with its id, and does not say it twice.

**Why did my second announcement say nothing, and the result is the first one?**
The same `idempotency_key` replays the stored announcement (`Idempotency-Replayed: true`) for 24 hours and never sends again, whatever its status. You reused a key (or a constant like `"dinner"`). Use a fresh `str(uuid4())` per announcement you mean to send; to send the same text again on purpose, use a new key.

**`announce` raised `ThalovantHubUnreachableError`. Can I send again with the same key?**
Not to send again: a replayed `failed` announcement stays `failed`. The hub may have spoken it. Read `get_announcement(hub, error.announcement_id)`, and only if nothing was spoken use a new key.

**How do I stop an urgent announcement that is repeating?**
`cancel_announcement(hub_id, announcement_id)` (`intercom:send`): status `cancelled`, `hub_confirmed=False` when the hub could not confirm. Only while `sending`, else `ThalovantNotCancellableError` (409). Other controls: the hub's switch, the platform kill switch, a speaker's `receive`, and somebody acknowledging by voice.

**Why does `announce` wait longer than my client's `timeout`?**
The API waits up to 10 seconds for the hub, so `announce` never uses a request timeout under 25 s (`INTERCOM_ANNOUNCE_MIN_TIMEOUT`); a longer client `timeout` is kept.

**Why is `get_announcement(hub, "..")` a `ValueError`?**
An id that is `.`, `..`, empty, or holds `/`, `?`, `#`, a backslash or a control character is refused before any request: a dot segment would change the route.

**Why does an urgent `announce` raise `ThalovantUrgentScopeRequiredError`?**
`priority="urgent"` (and so every `repeat`) needs the scope `intercom:urgent` in addition to `intercom:send`. Nothing implies it. Nothing was spent: no rate token, no hub call. Sign in again with the scope (a paid plan), or send a normal announcement.

**Why is a Home Assistant link in the speaker list but `receive=True` fails with `ThalovantNotASpeakerError`?**
A connection that cannot play speech (`can_play` false: `home_assistant`, `developer` and `embedded` unless its owner declared `spec.intercom.can_play: true` on the connection) is listed so it can be greyed out, but it cannot receive and is never part of `all`, a room or a group.

**What does `announcement.replayed` mean?**
The API answered with `Idempotency-Replayed: true`: this is the stored announcement for your key, nothing new was sent, whatever its status. Do not tell the person it was sent anew.

**Why does everything raise `ThalovantIntercomPlatformDisabledError`?**
Intercom ships dark: until the platform operator turns it on, `get_intercom_settings` reads `unavailable_reason="platform"` for every hub and every write and announcement is a 503 `intercom_disabled_platform`. Reads and `cancel_announcement` still work.

**Why was nobody spoken to?**
A speaker is skipped unless its `receive` is true (`skipped_not_receiving`, even for urgent), a normal announcement is skipped inside quiet hours (`skipped_dnd`; urgent bypasses them), and an offline speaker is `offline`. `ThalovantIntercomDisabledError` means the hub's switch is off; `ThalovantIntercomUnavailableError` (a kind of it) means a kid or public hub. `delivered` is not "heard": there is no playback receipt.

**How do I sign in without a password?**
`api.interactive_login()`. On a machine with a browser it opens the Thalovant consent page and waits for the browser to come back to `127.0.0.1` (browser sign-in, `browser_login()`). Elsewhere it prints a device code to approve from any browser (`login_with_browser()`).

**Why did `interactive_login()` print a code instead of opening my browser?**
One of these holds: `CI`, `SSH_CONNECTION` or `SSH_TTY` is set; you are on Linux with neither `DISPLAY` nor `WAYLAND_DISPLAY`; or the API answered `GET /v1/auth/native/config` with a 404 (an API without browser sign-in) or something unusable. `thalovant.native_auth.can_open_browser()` tells you the first part. `interactive_login(method="browser")` forces the browser.

**The browser did not open. What now?**
Open the address the SDK printed (`Opening your browser to sign in to Thalovant. If it does not open, visit:`) in a browser on the same computer. The sign-in waits 300 seconds (`timeout=`).

**Can I approve a browser sign-in from my phone?**
No. That is the point of it: the browser that approves delivers the code to its own computer's `127.0.0.1`. To approve from another device, use `interactive_login(method="device")`.

**What does "This answer does not belong to the sign-in in progress." mean in a tab?**
That tab came back with another sign-in's `state`, for example an old attempt. The sign-in in progress keeps waiting for its own tab.

**Does a browser sign-in token behave like a device login token?**
Yes. Both are API tokens that last 365 days, are listed in the dashboard and count against the plan. Each sets `access_token` and `token_id`, and `revoke_api_token()` revokes either one the same way.

**Why do I get `invalid_grant` after approving?**
The code was not exchanged within 60 seconds, or it was already used. Codes are single-use, and presenting one twice revokes the token it minted. Start the sign-in again; never retry the exchange.


**How do I connect a Telegram bot to a hub?**
`api.create_bridge(hub_id, "telegram", token=SecretValue(token))`, then `api.list_bridge_chats(bridge.id)` and `api.update_bridge(bridge.id, allowed=[...])`. The first non-empty `allowed` starts the bridge. See `docs/guides/chat-apps.md`.

**Why does `create_bridge` raise `ThalovantPlanError`?**
The owner's plan has no room: Free plans allow no chat apps, and paid plans allow `max_chat_bridges`. Read `api.get_bridge_networks(hub_id).allowance` first.

**How do I pause or resume a chat app?**
`api.update_bridge(bridge_id, paused=True)` or `paused=False`. A `plan_limit` hold lifts on its own once the plan has room again; resuming it yourself works only then (otherwise `ThalovantPlanError`).

**Which credentials does each connector need?**
`api.list_chat_app_connectors()` lists them by key (`connector.secrets`): Telegram, Discord and Mattermost take `token`; Matrix `access_token`; Twitch `refresh_token` and `client_secret`. Non-secret settings (a Matrix `homeserver`, a Twitch `bot_login` and `client_id`) follow `connector.config_schema`.

**Can a credential end up in my logs?**
Not through the SDK: wrap it in `SecretValue`, which prints `<redacted>` and cannot be pickled. The API never returns a credential; `bridge.secrets_set` lists only the keys.

**What does `status.reason == "TokenRejected"` mean?**
The network refused the bridge's credentials. Replace them with `replace_bridge_token()` or `replace_bridge_secrets()`.

**Why did my settings change fail with `bridge_url_not_public`?**
An address setting (a Matrix homeserver, a Mattermost URL) must be a public `https://` host on port 443. `error.fields` names the setting.

**Why does `list_bridge_chats` answer `bridge_running` on Telegram?**
Telegram lets only one reader fetch updates. Pause the bridge, wait a minute, then list the chats.
