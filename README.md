# Thalovant Python SDK

The Thalovant Python SDK is a thin developer layer over HiveMind's HTTP data plane.
It uses client identities provisioned by Thalovant, then connects directly to the
hub listener through `hivemind-http-protocol`.

```text
Thalovant API / dashboard -> provision identity and policy
Python SDK                -> direct HiveMind HTTP data-plane connection
hivemind-listener         -> OVOS / skills / hub runtime
```

## Install

```bash
pip install thalovant
```

For local SDK development:

```bash
pip install -e ".[dev]"
```

## Quick Start

Download or copy the client identity created in Thalovant, then:

```python
from thalovant import ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    reply = client.ask("What time is it?")
    print(reply.text)
```

For async apps and agent runtimes:

```python
import asyncio
from thalovant import AsyncThalovantClient


async def main():
    async with AsyncThalovantClient.from_identity_file("_identity.json") as client:
        reply = await client.ask("Tell me a short clean joke.")
        print(reply.text)


asyncio.run(main())
```

The identity file uses the same fields already produced for HiveMind clients:

```json
{
  "access_key": "client-access-key",
  "password": "client-password",
  "crypto_key": "optional-preshared-key",
  "site_id": "my-client-site",
  "default_master": "http://hub.example.com",
  "default_port": 5679
}
```

Environment variables are also supported:

```bash
export THALOVANT_ACCESS_KEY=...
export THALOVANT_PASSWORD=...
export THALOVANT_CRYPTO_KEY=...
export THALOVANT_SITE_ID=...
export THALOVANT_HUB_HTTP_HOST=http://hub.example.com
export THALOVANT_HUB_HTTP_PORT=5679
```

```python
from thalovant import ThalovantClient

client = ThalovantClient.from_env()
reply = client.ask("Tell me a joke")
```

## Low-Level Events

Use `emit` when you already know the OVOS/HiveMind event shape:

```python
from thalovant import ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    client.emit(
        "recognizer_loop:utterance",
        {"utterances": ["turn on the lights"], "lang": "en-us"},
    )
```

## Agent Events

Long-running agents can subscribe to hub events and receive normalized
`ThalovantEvent` objects:

```python
from thalovant import ThalovantClient


def on_speak(event):
    print(event.data.get("utterance", ""))


with ThalovantClient.from_identity_file("_identity.json") as client:
    with client.on("speak", on_speak):
        client.emit(
            "recognizer_loop:utterance",
            {"utterances": ["tell me a joke"], "lang": "en-us"},
        )
        client.wait_for_event("ovos.utterance.handled", timeout=20)
```

For simple blocking workflows, `listen` yields events until a timeout or a limit:

```python
with ThalovantClient.from_identity_file("_identity.json") as client:
    for event in client.listen("speak", timeout=30, max_events=3):
        print(event.data.get("utterance", ""))
```

Async agents can listen the same way:

```python
from thalovant import AsyncThalovantClient

async with AsyncThalovantClient.from_identity_file("_identity.json") as client:
    await client.emit(
        "recognizer_loop:utterance",
        {"utterances": ["tell me a joke"], "lang": "en-us"},
    )
    async for event in client.listen("speak", timeout=30, max_events=1):
        print(event.data.get("utterance", ""))
```

## Health And Recovery

The SDK validates the HTTPS transport, handshake, and polling thread state:

```python
with ThalovantClient.from_identity_file("_identity.json") as client:
    health = client.healthcheck()
    assert health.ok, health
```

`ThalovantClient` enables one reconnect attempt by default for sends and asks.
If the HiveMind HTTP polling thread stops, SDK calls surface
`ThalovantConnectionError` instead of silently timing out.

## Notes

- This SDK is the developer convenience layer. It does not proxy messages through
  the Thalovant API.
- The Thalovant API remains the control plane for creating clients, rotating or
  revoking identity material, and managing ACL/policy.
- The data plane is direct `hivemind-http-protocol` traffic from this SDK to the
  hub listener.
