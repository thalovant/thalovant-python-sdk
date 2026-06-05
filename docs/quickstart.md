# Quickstart

This guide verifies a Thalovant identity, sends an utterance, and listens for a
hub event.

## 1. Install

```bash
pip install thalovant
```

For local development from this repository:

```bash
pip install -e ".[dev]"
```

## 2. Save An Identity

Save the identity created by Thalovant as `_identity.json`:

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

The identity is secret. Do not commit it.

## 3. Run Diagnostics

```bash
thalovant --identity _identity.json doctor
```

The doctor command checks identity shape, endpoint configuration, authentication,
handshake completion, and the live polling thread.

## 4. Ask The Hub

```python
from thalovant import ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    reply = client.ask("Tell me a short clean joke.")
    print(reply.text)
```

## 5. Use A Conversation

Use a conversation when a client or agent sends related turns:

```python
from thalovant import ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    with client.conversation(lang="en-us") as convo:
        print(convo.ask("Remember that my favorite color is blue.").text)
        print(convo.ask("What color did I mention?").text)
```

The conversation reuses a stable session id and adds request correlation
metadata automatically.

## 6. Listen For Events

```python
from thalovant import EVENT_SPEAK, ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    for event in client.listen(EVENT_SPEAK, timeout=30, max_events=1):
        print(event.text)
```

Use `timeout` and `max_events` in scripts so they do not block forever.
