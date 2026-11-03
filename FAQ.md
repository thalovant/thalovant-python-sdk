Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Create the FAQ with chat apps questions.

# FAQ: thalovant (Python SDK)

**How do I connect a Telegram bot to a hub?**
`api.create_bridge(hub_id, "telegram", token=SecretValue(token))`, then `api.list_bridge_chats(bridge.id)` and `api.update_bridge(bridge.id, allowed=[...])`. The first non-empty `allowed` starts the bridge. See `docs/guides/chat-apps.md`.

**Why does `create_bridge` raise `ThalovantPlanError`?**
The owner's plan has no room: Free plans allow no chat apps, and paid plans allow `max_chat_bridges`. Read `api.get_bridge_networks(hub_id).allowance` first.

**How do I pause or resume a chat app?**
`api.update_bridge(bridge_id, paused=True)` or `paused=False`. Resuming also ends a `plan_limit` hold once the plan has room again.

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
