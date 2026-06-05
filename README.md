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
pip install .
```

## Quick Start

Download or copy the client identity created in Thalovant, then:

```python
from thalovant import ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    reply = client.ask("What time is it?")
    print(reply.text)
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

## Notes

- This SDK is the developer convenience layer. It does not proxy messages through
  the Thalovant API.
- The Thalovant API remains the control plane for creating clients, rotating or
  revoking identity material, and managing ACL/policy.
- The data plane is direct `hivemind-http-protocol` traffic from this SDK to the
  hub listener.
