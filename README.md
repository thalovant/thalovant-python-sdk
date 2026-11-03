# Thalovant Python SDK

[![PyPI](https://img.shields.io/pypi/v/thalovant)](https://pypi.org/project/thalovant/) [![CI](https://github.com/thalovant/thalovant-python-sdk/actions/workflows/ci.yml/badge.svg)](https://github.com/thalovant/thalovant-python-sdk/actions/workflows/ci.yml) [![Licence](https://img.shields.io/github/license/thalovant/thalovant-python-sdk)](LICENSE) [![Docs](https://img.shields.io/badge/docs-docs.thalovant.com-5c6bc0)](https://docs.thalovant.com/developers/sdks/python/)

Python SDK for connecting apps, services, kiosks, and agents to Thalovant hubs.

The control API is used to discover hubs and provision a client identity. After
that, the SDK talks directly to the hub data plane over HTTPS, WSS, or MQTTS.

```text
Thalovant API      -> discover hubs, create client identity
Python SDK         -> connect to the hub data plane
Hub runtime        -> skills, events, replies
```

The SDK is asyncio at its core. `AsyncThalovantClient` and
`AsyncThalovantControlPlane` are the implementation; `ThalovantClient` and
`ThalovantControlPlane` run the same code on a private event-loop thread, so a
script, a CLI or a voice satellite can call them without an event loop of its
own.

## Requirements

- Python 3.10 or newer.
- A Thalovant account with API access for authenticated control-plane actions.
- A hub id or slug, and a client identity for that hub. You can create one
  through the API or use one downloaded from the dashboard.

## Install

```bash
pip install thalovant
```

Optional extras: `thalovant[mqtt]`, `thalovant[yaml]` and `thalovant[listing]`.
The documentation says what each one adds.

## Quick start

```python
from thalovant import ThalovantClient, ThalovantControlPlane

api = ThalovantControlPlane()

# Public hub discovery does not require auth.
public_hubs = api.list_public_hubs(limit=12)
for hub in public_hubs["data"]:
    print(hub["id"], hub["slug"], hub["title"])

# Auth is required when creating a client identity.
api.login("you@example.com", "password")

result = api.create_client_identity(
    "hub-id",
    name="python-demo-client",
    preferred_protocols=("wss", "https", "mqtt"),
)

with ThalovantClient(result.identity, protocol="wss") as client:
    info = client.connection_info()
    print("connected in", info.connect_ms, "ms")
    reply = client.ask("Tell me a short clean joke.")
    print(reply.text)
```

Keep `result` secret: `result.identity` and the raw `result.client` API resource
carry the client credentials. `result.as_dict()` redacts every secret and is
safe to log; `result.as_dict(include_secrets=True)` returns the real
credentials, so use it only to save the identity and never log it.

## Chat apps

A hub can answer in Telegram, Discord, Matrix, Mattermost or Twitch. Read the
owner's allowance first (Free plans allow none), connect the bot, then choose
where it answers. Credentials go in a `SecretValue`, which never prints.

```python
import os
from thalovant import SecretValue, ThalovantControlPlane

api = ThalovantControlPlane(access_token=os.environ["THALOVANT_API_TOKEN"])
if not api.get_bridge_networks(hub_id).allowance.can_create:
    raise SystemExit("The plan has no room for another chat app.")

# Telegram: a bot token.
telegram = api.create_bridge(hub_id, "telegram", token=SecretValue(os.environ["TELEGRAM_BOT_TOKEN"]))
chats = api.list_bridge_chats(telegram.id)
telegram = api.update_bridge(telegram.id, allowed=list(chats)[:1], etag=telegram.etag)
api.send_bridge_test_message(telegram.id, telegram.allowed[0].id)

# Matrix: a homeserver and user, and an access token. Room ids are typed.
matrix = api.create_bridge(
    hub_id, "matrix",
    secrets={"access_token": SecretValue(os.environ["MATRIX_ACCESS_TOKEN"])},
    settings={"homeserver": "https://matrix.example.org", "user_id": "@kitchen-hub:example.org"},
)
api.update_bridge(matrix.id, allowed=["!abcdefgh:example.org"])

# Twitch: bot login and client id, refresh token and client secret.
twitch = api.create_bridge(
    hub_id, "twitch",
    secrets={
        "refresh_token": SecretValue(os.environ["TWITCH_REFRESH_TOKEN"]),
        "client_secret": SecretValue(os.environ["TWITCH_CLIENT_SECRET"]),
    },
    settings={"bot_login": "kitchenhubbot", "client_id": os.environ["TWITCH_CLIENT_ID"]},
)
api.update_bridge(twitch.id, allowed=["#MyChannel"])

api.update_bridge(twitch.id, paused=True)        # pause; paused=False resumes
print(api.get_bridge(telegram.id).status.reason)  # Setup, Starting, Connected, Paused, ...
```

A refusal is a `ThalovantAPIError` with `code` and `fields` (the settings or
secrets it names); see the [chat apps guide](docs/guides/chat-apps.md) for the
status words, holds and every code.

## Documentation

| Topic | Link |
| :--- | :--- |
| Python SDK: install, sign-in, identities, protocols, conversations, events, asyncio, Home Assistant, provisioning, errors, upgrading to 0.9 | <https://docs.thalovant.com/developers/sdks/python/> |
| The Thalovant product | <https://docs.thalovant.com> |

## Not yet in the online documentation

These parts of the old README have no counterpart on the documentation page.
They are described in the `docs/` folder of this repository and in
[CHANGELOG.md](CHANGELOG.md).

- Listing your hubs, workspace analytics and durable memory.
- Rich responses (`reply.display_items`) and skill sounds (`reply.media_events`).
- Actions and exact inputs (`send_action`, `send_code`).
- Reading an API error and the API shape overview.
- CLI diagnostics: `thalovant --identity _identity.json doctor` checks identity
  shape, endpoint selection, authentication, handshake and transport health.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Security

See [SECURITY.md](SECURITY.md).

## Licence

MIT. See [LICENSE](LICENSE).
