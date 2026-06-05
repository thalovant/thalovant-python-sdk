# Thalovant Python SDK

The Thalovant Python SDK is the developer layer for direct Thalovant hub access.
Thalovant provisions client identities and policy; this SDK connects directly to
the hub endpoint over the HTTPS data plane.

```text
Thalovant API / dashboard -> provision identity and policy
Python SDK / CLI          -> direct hub HTTPS data-plane connection
Hub runtime               -> skills, events, and replies
```

Full documentation: <https://docs.thalovant.com/developers/sdks/python/>

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

The identity file uses the same fields produced for Thalovant hub clients:

```json
{
  "access_key": "client-access-key",
  "password": "client-password",
  "crypto_key": "optional-preshared-key",
  "site_id": "my-client-site",
  "default_master": "https://hub.example.com",
  "default_port": 443,
  "default_path": "/public"
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
export THALOVANT_HUB_HTTP_PATH=/public
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

## Client Context

Use `build_client_context` when a web, mobile, kiosk, or service client needs
to pass user, device, channel, and platform metadata to skills:

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
```

Provider-specific fields can still be passed through `context` or `metadata`.
The SDK keeps the public helper generic.

## Actions And Exact Inputs

Use `send_action` for button/quick-reply payloads, and `send_code` for exact
values such as QR codes, serial numbers, asset IDs, or scanned labels:

```python
with client.conversation(session_id="work-session") as convo:
    convo.send_action('/choose{"id":"42"}', title="Choose item")
    convo.send_code("SN-001-XYZ", kind="qr", label="serial")
```

Both helpers still emit normal `recognizer_loop:utterance` events, but add
generic `input` metadata so downstream skills can distinguish typed/scanned
values from speech transcription.

## Rich Responses

Assistant responses can include text, choices, images, attachments, and tables.
Use `reply.display_items()` or `event.display_items()` to render a UI without
hand-parsing common rich media payloads:

```python
reply = client.ask("Show matching parts.")

for item in reply.display_items(max_text_chars=600):
    if item.kind == "text":
        print(item.text)
    elif item.kind == "choices":
        print([choice["title"] for choice in item.data])
```

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

Use `emit` when you already know the hub event shape:

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

The canonical public documentation lives on the Thalovant docs site:

- Website: <https://docs.thalovant.com/developers/sdks/python/>

This repository also keeps generated API reference material for maintainers:

- Local preview: `pip install -e ".[docs]" && mkdocs serve`
- Build check: `mkdocs build --strict`

## Notes

- This SDK is the developer convenience layer. It does not proxy messages
  through the Thalovant API.
- The Thalovant API remains the control plane for creating clients, rotating or
  revoking identity material, and managing ACL/policy.
- The data plane is direct hub protocol traffic from this SDK to the hub
  listener.

## Publishing

This repository is configured for PyPI trusted publishing through
`.github/workflows/publish.yml`. Use these values in the PyPI publisher form:

- PyPI Project Name: `thalovant`
- Owner: `thalovant`
- Repository name: `thalovant-python-sdk`
- Workflow name: `publish.yml`
- Environment name: `pypi`
