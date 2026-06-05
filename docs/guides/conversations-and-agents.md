# Conversations And Agents

Use conversations and agents when your application needs more than one isolated
request.

## Conversations

Conversations keep a stable session id and default language across turns.

```python
from thalovant import ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    with client.conversation(lang="en-us") as convo:
        reply = convo.ask("Remember the word lighthouse.")
        follow_up = convo.ask("What word did I ask you to remember?")
        print(follow_up.text)
```

You can provide your own session id when integrating with an external user or
tenant model:

```python
with client.conversation(session_id="tenant-42-user-7") as convo:
    reply = convo.ask("Start a troubleshooting session.")
```

!!! note
    When a hub echoes session or request metadata, SDK listeners filter unrelated
    events. Missing metadata remains compatible with existing HiveMind hubs.

## Synchronous Agents

Use `ThalovantAgent` for long-running workers outside an asyncio runtime:

```python
from thalovant import EVENT_SPEAK, ThalovantAgent

agent = ThalovantAgent.from_identity_file("_identity.json")


@agent.on(EVENT_SPEAK)
def handle_speak(event):
    print(event.text)


agent.run_forever()
```

`agent.on_speak` is available for the common `speak` event:

```python
@agent.on_speak
def handle_speak(event):
    print(event.text)
```

## Async Agents

Use `AsyncThalovantAgent` inside asyncio services:

```python
import asyncio
from thalovant import AsyncThalovantAgent, EVENT_SPEAK

agent = AsyncThalovantAgent.from_identity_file("_identity.json")


@agent.on(EVENT_SPEAK)
async def handle_speak(event):
    print(event.text)


asyncio.run(agent.run_forever())
```

## Shutdown

Agents close active subscriptions and disconnect the underlying client when
`run_forever` exits.

```python
agent.stop()
```

For service managers, call `stop()` from your signal handler and let
`run_forever()` finish cleanly.
