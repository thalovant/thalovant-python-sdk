# Thalovant Python SDK

The Thalovant Python SDK is the developer layer for direct HiveMind hub access.
Thalovant provisions client identities and policy; this SDK connects directly to
the hub listener over the HiveMind HTTP/HTTPS data plane through
`hivemind-http-protocol`.

```text
Thalovant API / dashboard -> provision identity and policy
Python SDK / CLI          -> direct HiveMind HTTPS/HTTP data-plane connection
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
    reply = client.ask("Tell me a short clean joke.")
    print(reply.text)
```

For async apps and agent runtimes:

```python
import asyncio
from thalovant import AsyncThalovantClient


async def main():
    async with AsyncThalovantClient.from_identity_file("_identity.json") as client:
        reply = await client.ask("What time is it?")
        print(reply.text)


asyncio.run(main())
```

## Identity

The identity file uses the same fields already produced for HiveMind clients:

```json
{
  "access_key": "client-access-key",
  "password": "client-password",
  "crypto_key": "optional-preshared-key",
  "site_id": "my-client-site",
  "default_master": "https://hub.example.com",
  "default_port": 443
}
```

Environment variables are also supported:

```bash
export THALOVANT_ACCESS_KEY=...
export THALOVANT_PASSWORD=...
export THALOVANT_CRYPTO_KEY=...
export THALOVANT_SITE_ID=...
export THALOVANT_HUB_HTTP_HOST=https://hub.example.com
export THALOVANT_HUB_HTTP_PORT=443
```

```python
from thalovant import ThalovantClient

with ThalovantClient.from_env() as client:
    print(client.ask("Tell me a joke").text)
```

Keep identity files secret. They are client credentials, not public API keys.

## Conversations

Use a conversation when multiple messages should share a stable session and
correlation context:

```python
from thalovant import ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    with client.conversation(lang="en-us") as convo:
        first = convo.ask("Remember that my favorite color is blue.")
        second = convo.ask("What color did I mention?")
        print(second.text)
```

Conversation helpers add session and request metadata automatically. When the
hub echoes that metadata, SDK listeners filter unrelated events from other
sessions.

## Agents

Use `ThalovantAgent` for long-running synchronous workers:

```python
from thalovant import ThalovantAgent, EVENT_SPEAK

agent = ThalovantAgent.from_identity_file("_identity.json")


@agent.on(EVENT_SPEAK)
def handle_speak(event):
    print(event.text)


agent.run_forever()
```

Async agents work the same way:

```python
import asyncio
from thalovant import AsyncThalovantAgent, EVENT_SPEAK

agent = AsyncThalovantAgent.from_identity_file("_identity.json")


@agent.on(EVENT_SPEAK)
async def handle_speak(event):
    print(event.text)


asyncio.run(agent.run_forever())
```

## CLI

The package installs a `thalovant` command for smoke tests and operational
debugging:

```bash
thalovant --identity _identity.json doctor
thalovant --identity _identity.json health
thalovant --identity _identity.json ask "Tell me a joke"
thalovant --identity _identity.json listen speak --timeout 30 --max-events 3
thalovant --identity _identity.json emit recognizer_loop:utterance \
  --data '{"utterances":["hello"],"lang":"en-us"}'
```

Add `--json` to commands that return structured output.

## Events

For common flows, prefer helpers over raw event strings:

```python
from thalovant import EVENT_SPEAK, ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    for event in client.listen(EVENT_SPEAK, timeout=30, max_events=1):
        print(event.text)
```

Use `emit` when you already know the OVOS/HiveMind event shape:

```python
from thalovant import EVENT_RECOGNIZER_LOOP_UTTERANCE, ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    client.emit(
        EVENT_RECOGNIZER_LOOP_UTTERANCE,
        {"utterances": ["turn on the lights"], "lang": "en-us"},
    )
```

`ThalovantEvent` normalizes common fields:

- `event.text`
- `event.utterances`
- `event.session_id`
- `event.request_id`
- `event.is_failure`
- `event.is_policy_denied`

## Diagnostics

Use `doctor()` before debugging application code:

```python
with ThalovantClient.from_identity_file("_identity.json") as client:
    report = client.doctor()
    print(report.format())
```

The report checks identity shape, endpoint configuration, hub connection,
handshake completion, and the live HTTP polling thread.

## Documentation

The documentation website is built with MkDocs Material:

- Website: <https://thalovant.github.io/thalovant-python-sdk/>
- Local preview: `pip install -e ".[docs]" && mkdocs serve`
- Build check: `mkdocs build --strict`

## Notes

- This SDK is the developer convenience layer. It does not proxy messages
  through the Thalovant API.
- The Thalovant API remains the control plane for creating clients, rotating or
  revoking identity material, and managing ACL/policy.
- The data plane is direct `hivemind-http-protocol` traffic from this SDK to the
  hub listener.

## Publishing

This repository is configured for PyPI trusted publishing through
`.github/workflows/publish.yml`. Use these values in the PyPI publisher form:

- PyPI Project Name: `thalovant`
- Owner: `thalovant`
- Repository name: `thalovant-python-sdk`
- Workflow name: `publish.yml`
- Environment name: `pypi`
