# Quickstart

This guide takes you from discovery to a live hub request.

## 1. Install

```bash
pip install thalovant
```

## 2. Discover Public Hubs

Public discovery does not require a login.

```python
from thalovant import ThalovantControlPlane

api = ThalovantControlPlane("https://dash.thalovant.com/api")

page = api.list_public_hubs(limit=12)
for hub in page["data"]:
    print(hub["id"], hub["slug"], hub["title"])
```

Use `api.get_public_hub("joke-garden")` to load one hub by slug or id.

## 3. Create A Client Identity

Authenticate with an account that has API access, then create a client identity
for the hub you want to use.

```python
api.login("you@example.com", "password")

result = api.create_client_identity(
    "hub-id",
    name="python-demo-client",
    preferred_protocols=("wss", "https", "mqtt"),
)

identity = result.identity
```

The SDK generates client secrets locally and sends them to the API once. Treat
the returned identity like a password.

To list hubs visible to the authenticated account:

```python
page = api.list_hubs(limit=50)
for hub in page["data"]:
    print(hub["id"], hub["slug"], hub["title"])
```

## 4. Ask The Hub

```python
from thalovant import ThalovantClient

with ThalovantClient(identity, protocol="wss") as client:
    reply = client.ask("Tell me a short clean joke.")
    print(reply.text)
```

## 5. Save The Identity

Save identities only in a secret store or a local file ignored by git.

```python
import json
from pathlib import Path

Path("_identity.json").write_text(
    json.dumps(identity.as_dict(include_secrets=True), indent=2),
    encoding="utf-8",
)
```

Later:

```python
from thalovant import ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    print(client.ask("What can you help with?").text)
```

## 6. Choose A Protocol

```python
print(identity.enabled_protocols())
print(identity.endpoint_for("wss"))
print(identity.endpoint_for("https"))
print(identity.endpoint_for("mqtt"))
```

Connect explicitly:

```python
with ThalovantClient(identity, protocol="https") as client:
    print(client.ask("Reply over HTTPS.").text)
```

MQTT requires `identity.mqtt`. If it is missing, create or download a fresh
identity after MQTT is enabled on the hub.

## 7. Use A Conversation

Use a conversation when related turns should share one session.

```python
with ThalovantClient.from_identity_file("_identity.json") as client:
    with client.conversation(lang="en-us") as convo:
        print(convo.ask("Remember that my favorite color is blue.").text)
        print(convo.ask("What color did I mention?").text)
```

## 8. Listen For Events

```python
from thalovant import EVENT_SPEAK, ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    for event in client.listen(EVENT_SPEAK, timeout=30, max_events=1):
        print(event.text)
```

Use `timeout` and `max_events` in scripts so they do not block forever.

## 9. Run Diagnostics

```bash
thalovant --identity _identity.json doctor
```

`doctor` checks the identity, endpoint selection, authentication, handshake,
and transport health.
