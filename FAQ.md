Last Edit: Claude Sonnet 5.5 - 2026-11-11 - Motive: Intercom (0.12.0): wait scope and retry questions after the PR 114 review.

# FAQ: thalovant (Python SDK)

**Why is there no `intercom()` method, and is intercom a safety system?**
The HiveMind mesh already has an `intercom` frame (hub-routed messages between clients), so the HTTP calls are `announce`, `get_announcement`, `list_announcements` and `*_intercom_*`. Intercom is announce-only text spoken by each speaker's own TTS; it is **not a certified safety system** and does not replace smoke or CO alarms, medical alert devices or emergency services. The platform never stores the text.

**Why did `announce(wait=True)` raise after the announcement was accepted?**
`wait=True` polls `get_announcement`, which needs `intercom:read` as well as `intercom:send`; a poll can also fail on a 429 or the network. The announcement exists and the exception has no id. Pass an `idempotency_key` and reuse it to retry, so the text is not said twice.

**Why was nobody spoken to?**
A speaker is skipped unless its `receive` is true (`skipped_not_receiving`, even for urgent), a normal announcement is skipped inside quiet hours (`skipped_dnd`; urgent bypasses them), and an offline speaker is `offline`. `ThalovantIntercomDisabledError` means the hub's switch is off or it is a kid or public hub.

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
