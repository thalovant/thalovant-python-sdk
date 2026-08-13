# Thalovant Python SDK

Python SDK for connecting apps, services, kiosks, and agents to Thalovant hubs.

The control API is used to discover hubs and provision a client identity. After
that, the SDK talks directly to the hub data plane over HTTPS, WSS, or MQTTS.

```text
Thalovant API      -> discover hubs, create client identity
Python SDK         -> connect to the hub data plane
Hub runtime        -> skills, events, replies
```

Full docs: <https://docs.thalovant.com/developers/sdks/python/>

## What You Need

- A Thalovant account with API access for authenticated control-plane actions.
- A hub id or slug.
- A client identity for that hub. You can create one through the API or use one
  downloaded from the dashboard.

## Install

```bash
pip install thalovant
```

For local SDK development:

```bash
pip install -e ".[dev]"
```

## Quick Start

This is the normal first integration flow.

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

`ThalovantControlPlane()` uses `https://api.thalovant.com` by default. Pass a
different URL only for local development or a self-hosted control plane.

Keep `result.identity` secret. It contains the client credentials used by the
hub. Do not log `result.identity.as_dict(include_secrets=True)`.

## List Your Hubs

Authenticated accounts can list owned or visible hubs:

```python
api = ThalovantControlPlane()
api.login("you@example.com", "password")

page = api.list_hubs(limit=50)
for hub in page["data"]:
    print(hub["id"], hub["slug"], hub["title"])
```

## Workspace Analytics

Authenticated accounts can read the same overview used by the dashboard:

```python
overview = api.get_analytics_overview(range="7d", hub_id="hub-id")
print(overview["totals"])
```

## Durable Memory

Private Daily Desk and workspace assistants can manage explicit opt-in memory:

```python
memory = api.create_memory_item(
    {
        "scope": "workspace",
        "kind": "preference",
        "content": "Prefer America/Toronto for scheduling.",
        "tags": ["timezone"],
    }
)
print(memory["id"])

items = api.list_memory_items(scope="workspace", query="timezone")
print(items["data"])
```

## Use An Existing Identity

For local development, store one or more identities in the protected SDK config:

```bash
mkdir -p ~/.config/thalovant
chmod 700 ~/.config/thalovant
$EDITOR ~/.config/thalovant/config.yaml
chmod 600 ~/.config/thalovant/config.yaml
```

```yaml
profile: prod
profiles:
  prod:
    identity:
      access_key: ...
      password: ...
      site_id: demo-agent
      default_master: https://jokes.thalovant.io
      data_plane_endpoints:
        wss: wss://jokes.thalovant.io/public
        https: https://jokes.thalovant.io/public
        mqtt: mqtts://mqtt.thalovant.com:8883
      mqtt:
        endpoint: mqtts://mqtt.thalovant.com:8883
        username: ...
        password: ...
        topic_prefix: hubs/hub-id/clients/client-id
        tls: true
```

```python
from thalovant import ThalovantClient

with ThalovantClient.from_config(profile="prod") as client:
    reply = client.ask("What can this hub do?")
    print(reply.text)
```

SDKs reject config files that are readable or writable by other users on Linux
and macOS. Keep this file out of git.

Raw identity files are supported too:

```python
from thalovant import ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    reply = client.ask("What can this hub do?")
    print(reply.text)
```

Environment variables are supported too:

```bash
export THALOVANT_ACCESS_KEY=...
export THALOVANT_PASSWORD=...
export THALOVANT_CRYPTO_KEY=...
export THALOVANT_SITE_ID=...
export THALOVANT_HUB_HTTPS_HOST=https://hub.example.com
export THALOVANT_HUB_WSS_HOST=wss://hub.example.com
export THALOVANT_HUB_MQTT_HOST=mqtts://mqtt.thalovant.com:8883
export THALOVANT_MQTT_USERNAME=...
export THALOVANT_MQTT_PASSWORD=...
export THALOVANT_MQTT_TOPIC_PREFIX=hivemind/hub-id/client-id
```

```python
from thalovant import ThalovantClient

with ThalovantClient.from_env(protocol="https") as client:
    print(client.ask("Say hello.").text)
```

## Save A Provisioned Identity

Only save identities in a secret store or local developer file that is ignored
by git.

```python
import json
from pathlib import Path

Path("_identity.json").write_text(
    json.dumps(result.identity.as_dict(include_secrets=True), indent=2),
    encoding="utf-8",
)
```

## Protocols

Hubs may expose one or more public data-plane protocols:

- `wss`: secure realtime WebSocket, the default public path and SDK preference.
- `https`: request/response HTTP protocol exposed as HTTPS.
- `mqtt`: broker-mediated MQTT over TLS. Requires per-client broker credentials.

Inspect what an identity supports:

```python
identity = result.identity

print(identity.enabled_protocols())
print(identity.endpoint_for("wss"))
print(identity.endpoint_for("https"))
print(identity.endpoint_for("mqtt"))
print(identity.mqtt.endpoint if identity.mqtt else None)
```

Connect with a specific protocol:

```python
from thalovant import ThalovantClient

for protocol in ("wss", "https", "mqtt"):
    if not result.identity.supports_protocol(protocol):
        continue
    if protocol == "mqtt" and result.identity.mqtt is None:
        continue

    with ThalovantClient(result.identity, protocol=protocol) as client:
        print(protocol, client.ask(f"Reply over {protocol}.").text)
```

Use `client.connect_with_info()` when you need connection telemetry for
benchmarks or health dashboards. The returned snapshot includes phase,
socket/open time, handshake time, total connect time, and last error.

Use `client.query(...)` for the direct HiveMind query frame path when the hub
supports it. It avoids broad bus fanout and is the preferred request/reply API
for low-latency app integrations.

```python
reply = client.query("What time is it in Toronto?")
print(reply.text)
```

MQTT identities include a broker endpoint, username, password, TLS flag, and
topic prefix. The broker credentials are scoped to that client and should be
treated like a password. Public identities should use `mqtts://`; the SDK also
honors an explicit `tls: true` flag from the identity.

## Conversations

Use a conversation when several turns should share one session.

```python
from thalovant import ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    with client.conversation(lang="en-us") as convo:
        print(convo.ask("Remember that my favorite color is blue.").text)
        print(convo.ask("What color did I mention?").text)
```

## Events

You can wait for, stream, or subscribe to hub events.

```python
from thalovant import EVENT_SPEAK, ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    for event in client.listen(EVENT_SPEAK, timeout=30, max_events=1):
        print(event.text)
```

Use timeouts in scripts so they do not wait forever.

## Client Context

Context lets skills know which app, device, user, or channel made the request.

```python
from thalovant import ThalovantClient, build_client_context

context = build_client_context(
    user_id="user-42",
    user_name="Ada",
    auth_provider="oidc",
    roles=["member"],
    platform="kiosk",
    source="checkout-kiosk",
    channel="chat",
)

with ThalovantClient.from_identity_file("_identity.json") as client:
    reply = client.ask("Show the next instruction.", context=context)
    print(reply.text)
```

## Actions And Exact Inputs

Use actions for button payloads and codes for exact typed or scanned values.

```python
with client.conversation(session_id="work-session") as convo:
    convo.send_action('/choose{"id":"42"}', title="Choose item")
    convo.send_code("SN-001-XYZ", kind="qr", label="serial")
```

## Rich Responses

Replies can include text, choices, tables, images, or attachments.

```python
reply = client.ask("Show matching parts.")

for item in reply.display_items(max_text_chars=600):
    if item.kind == "text":
        print(item.text)
    elif item.kind == "choices":
        print([choice["title"] for choice in item.data])
```

## Async Apps

```python
import asyncio
from thalovant import AsyncThalovantClient


async def main():
    async with AsyncThalovantClient.from_config(profile="prod") as client:
        reply = await client.ask("What time is it?")
        print(reply.text)


asyncio.run(main())
```

## CLI Diagnostics

```bash
thalovant --identity _identity.json doctor
```

The doctor command checks identity shape, endpoint selection, authentication,
handshake, and transport health.

## Common Issues

- `Missing Thalovant API access token`: call `api.login(...)` before private
  control-plane actions, or pass `access_token=` to `ThalovantControlPlane`.
- `API access requires a paid plan`: upgrade the workspace before using the SDK
  control-plane API to provision private resources.
- `Unsupported protocol`: the hub does not expose that protocol, or the
  identity was created before that protocol was enabled.
- MQTT fails immediately: create or download a fresh client identity after MQTT
  is enabled. MQTT needs the per-client `identity.mqtt` credentials.
- A request times out: increase `timeout` on `ask(...)` or check `doctor()`.

## API Shape

- `ThalovantControlPlane()`
- `ThalovantControlPlane(api_url, access_token=...)` for local or self-hosted control planes
- `control.login(email, password, scope=None, otp_code=None, recovery_code=None)`
  (MFA accounts pass a TOTP `otp_code` or a one-time `recovery_code`)
- `control.list_public_hubs(limit=...)`
- `control.get_public_hub(hub_ref)`
- `control.list_hubs(limit=..., owner_id=...)`
- `control.get_hub(hub_id)`
- `control.get_operation(operation_id)`
- `control.get_analytics_overview(...)`
- `control.list_memory_items(...)`
- `control.get_memory_summary(owner_id=...)`
- `control.create_memory_item(payload)`
- `control.get_memory_item(memory_id)`
- `control.update_memory_item(memory_id, payload)`
- `control.delete_memory_item(memory_id)`
- `control.create_client_identity(hub_id, ...)`
- `ThalovantIdentity.from_config(path=None, profile=None)`
- `ThalovantIdentity.from_file(path)`
- `ThalovantClient.from_config(path=None, profile=None)`
- `ThalovantClient.from_identity_file(path)`
- `ThalovantClient.from_env()`
- `ThalovantClient(identity, protocol="wss")`
- `client.connect_with_info()`
- `client.connection_info()`
- `client.query(text, context=...)`
- `client.ask(text, context=...)`
- `client.send_utterance(text, context=...)`
- `client.send_action(payload, ...)`
- `client.send_code(value, ...)`
- `client.listen(event_name, ...)`
- `client.conversation(...)`

## Development

```bash
pip install -e ".[dev]"
pytest
```
