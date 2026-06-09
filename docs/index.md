# Thalovant Python SDK

Build Python clients and agents that connect directly to Thalovant hubs.

```bash
pip install thalovant
```

```python
from thalovant import ThalovantClient, ThalovantControlPlane

api = ThalovantControlPlane("https://dash.thalovant.com/api")
api.login("you@example.com", "password")

result = api.create_client_identity("hub-id", name="python-demo-client")

with ThalovantClient(result.identity, protocol="wss") as client:
    print(client.ask("Tell me a short clean joke.").text)
```

<div class="thalovant-home-grid" markdown>

[:material-rocket-launch-outline: Quickstart](quickstart.md)
: Discover a hub, create a client identity, connect, and ask.

[:material-account-voice: Agents](guides/conversations-and-agents.md)
: Build long-running sync or async workers with decorators and scoped sessions.

[:material-console: CLI](cli.md)
: Smoke-test hub access with `thalovant doctor`, `ask`, `listen`, and `emit`.

[:material-code-braces: API Reference](api-reference.md)
: Explore clients, conversations, events, diagnostics, and generated reference docs.

</div>

## How It Works

1. Use `ThalovantControlPlane` to discover public hubs and create a client identity.
2. Store the returned identity securely.
3. Use `ThalovantClient` or `AsyncThalovantClient` to connect directly to the hub.
4. Choose `wss`, `https`, or `mqtt` based on what the hub exposes.

The Thalovant API provisions identities, policies, and endpoints. Runtime
messages go directly from the SDK to the hub data plane.

Authenticated control-plane API actions require API access on the workspace.

## Common Workflows

=== "Discover"

    ```python
    from thalovant import ThalovantControlPlane

    api = ThalovantControlPlane("https://dash.thalovant.com/api")
    page = api.list_public_hubs(limit=12)

    for hub in page["data"]:
        print(hub["id"], hub["slug"], hub["title"])
    ```

=== "Ask"

    ```python
    from thalovant import ThalovantClient

    with ThalovantClient.from_identity_file("_identity.json") as client:
        print(client.ask("What time is it?").text)
    ```

=== "Protocols"

    ```python
    identity = result.identity

    print(identity.enabled_protocols())

    with ThalovantClient(identity, protocol="mqtt") as client:
        print(client.ask("Reply over MQTT.").text)
    ```

=== "Agent"

    ```python
    from thalovant import EVENT_SPEAK, ThalovantAgent

    agent = ThalovantAgent.from_identity_file("_identity.json")

    @agent.on(EVENT_SPEAK)
    def handle_speak(event):
        print(event.text)

    agent.run_forever()
    ```

## What The SDK Gives You

- Public hub discovery through the Thalovant API.
- Client identity provisioning with secrets generated locally.
- Direct HTTPS, WSS, and MQTT hub transports.
- `ask`, `send_utterance`, `listen`, and `emit` primitives.
- Conversation-scoped sessions and request correlation.
- Generic user, device, auth, channel, and platform context helpers.
- Sync and async clients.
- Sync and async long-running agent runners.
- CLI diagnostics and smoke tests.
