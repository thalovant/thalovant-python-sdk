<!-- Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Document chat apps (connectors, allowance, the /v1/bridges owner routes) for 0.10.0. -->

# Chat apps

A hub can answer in a chat app: the owner connects a bot of their own to the
hub, chooses the chats, channels or rooms it may answer in, and Thalovant runs
the connection. The API calls one connection a **bridge** and each network a
**connector**. Telegram and Discord are offered; Matrix, Mattermost and Twitch
are in beta.

Everything here is on `ThalovantControlPlane` and, awaited, on
`AsyncThalovantControlPlane`. Reads need `hubs:read`; everything that changes a
bridge, and listing its chats, needs `hubs:write`.

## Before you start: the allowance

Bridges count against the hub owner's plan (`max_chat_bridges`). Free plans
allow none.

```python
from thalovant import ThalovantControlPlane

api = ThalovantControlPlane(access_token="...")
networks = api.get_bridge_networks(hub_id)
for offer in networks.networks:
    print(offer.network, offer.status, "offered" if offer.offered else "not yet")
allowance = networks.allowance
print(f"{allowance.used} of {allowance.limit} used on {allowance.plan}")
if not allowance.can_create:
    print("Upgrade the plan, or remove a bridge, first.")
```

`list_chat_app_connectors()` says what each connector needs: its secrets by key
(`connector.secrets`), its settings form (`connector.config_schema`), how its
chats are named (`connector.targets`), and its setup steps in English and
French (`connector.setup_steps.text("fr")`).

## Credentials

Credentials go in a `SecretValue`. It prints as `<redacted>` in `str()`,
`repr()`, f-strings and logs, cannot be pickled, and is read only to build the
one request that carries it. The API never sends a credential back: a bridge
lists the keys it holds in `secrets_set`, never the values. Plain strings are
accepted too, and wrapped on the way in.

## Telegram (a bot token)

```python
import os
from thalovant import SecretValue

bridge = api.create_bridge(hub_id, "telegram", token=SecretValue(os.environ["TELEGRAM_BOT_TOKEN"]))
print(bridge.invite_links.add_to_group)   # add the bot to a group, then mention it there

chats = api.list_bridge_chats(bridge.id)  # chats with recent activity
print(chats.note)
bridge = api.update_bridge(bridge.id, allowed=[chat for chat in chats if chat.selectable][:1], etag=bridge.etag)
api.send_bridge_test_message(bridge.id, bridge.allowed[0].id)
```

The first non-empty `allowed` starts the bridge. Discord works the same way
with its bot token; a Discord bridge always answers only what mentions it
(`connector.options.require_mention_fixed`).

## Matrix (homeserver, user and access token)

```python
bridge = api.create_bridge(
    hub_id,
    "matrix",
    secrets={"access_token": SecretValue(os.environ["MATRIX_ACCESS_TOKEN"])},
    settings={"homeserver": "https://matrix.example.org", "user_id": "@kitchen-hub:example.org"},
)
chats = api.list_bridge_chats(bridge.id)
assert chats.manual_entry                 # Matrix rooms are typed, not listed
bridge = api.update_bridge(bridge.id, allowed=["!abcdefgh:example.org"])
```

The homeserver must be a public `https://` address on port 443; anything else
is refused with `bridge_url_*` and `error.fields == ("homeserver",)`.

## Twitch (bot login and client id, refresh token and client secret)

```python
bridge = api.create_bridge(
    hub_id,
    "twitch",
    secrets={
        "refresh_token": SecretValue(os.environ["TWITCH_REFRESH_TOKEN"]),
        "client_secret": SecretValue(os.environ["TWITCH_CLIENT_SECRET"]),
    },
    settings={"bot_login": "kitchenhubbot", "client_id": os.environ["TWITCH_CLIENT_ID"]},
)
bridge = api.update_bridge(bridge.id, allowed=["#MyChannel"])  # stored as "mychannel"

# Later, after rotating the refresh token in Twitch's console:
api.replace_bridge_secrets(bridge.id, {"refresh_token": SecretValue(new_refresh_token)})
```

## Pause, resume, settings

```python
api.update_bridge(bridge.id, paused=True)    # stops it; credentials and chats stay
api.update_bridge(bridge.id, paused=False)   # resumes it, and ends a plan_limit hold once the plan has room
api.update_bridge(bridge.id, require_mention=False, session_scope="chat", etag=bridge.etag)
```

Pass `etag` to refuse a change made on a stale read: the API answers HTTP 412
and nothing changes. `delete_bridge()` removes the bridge and its stored
credentials and cannot be undone; pausing is usually what you want.

## Status

Word a bridge's state from `bridge.status.reason`:

| `reason` | Meaning |
| :--- | :--- |
| `Setup` | No chats chosen yet. |
| `Starting` | Starting, or not reported yet. |
| `Connected` | Running and answering. |
| `Paused` | Stopped, by the owner or by a hold. |
| `Unavailable` | Started but not running, or a problem on the platform side. |
| `TokenMissing` | The stored credentials have not reached the bridge yet. |
| `TokenRejected` | The network refused the credentials: replace them. |

`bridge.status.applied` is true once the running bridge carries the last
change. `bridge.hold` is set when the platform paused the bridge:
`plan_limit` (the plan allows fewer running bridges; resume it once it allows
this one) or `account_suspended`. A reason, phase or hold this SDK does not
know yet is kept as the string the API sent.

## Errors

Refusals raise `ThalovantAPIError` with `status_code`, `code`, `detail` and
`fields` (the settings or secrets a refusal names, never their values). A plan
refusal (402, or 403 `plan_limit` with `problem["resource"] == "chat_bridge"`)
is a `ThalovantPlanError`; a token without the scope is a `ThalovantAuthError`.

| Status | `code` |
| :--- | :--- |
| 409 | `bridge_bot_in_use`, `bridge_helper_unavailable`, `bridge_token_missing`, `bridge_token_other_bot`, `bridge_running`, `bridge_telegram_updates_busy` |
| 412 | (none) a stale `etag` |
| 422 | `bridge_token_malformed`, `bridge_token_invalid`, `bridge_token_not_a_bot`, `bridge_network_refused`, `bridge_allowed_required`, `bridge_chat_id_invalid`, `bridge_chat_not_allowed`, `bridge_settings_invalid`, `bridge_secrets_invalid`, `bridge_url_invalid`, `bridge_url_port_not_allowed`, `bridge_url_not_public`, `bridge_url_unresolvable`, `bridge_too_many_targets`, `bridge_mention_required` |
| 429 | `bridge_rate_limited`, `bridge_network_rate_limited` (`retry_after_seconds` is set) |
| 502 / 504 | `bridge_network_unavailable`, `bridge_secret_store_failed` / `bridge_network_timeout` |
| 503 | `bridge_network_not_offered`, `bridge_secret_store_unavailable` |
