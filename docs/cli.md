# CLI

The `thalovant` command is intended for quick smoke tests, operational checks,
and debugging identities outside application code.

## Identity

Pass an identity file:

```bash
thalovant --identity _identity.json doctor
```

Or use environment variables:

```bash
export THALOVANT_ACCESS_KEY=...
export THALOVANT_PASSWORD=...
export THALOVANT_CRYPTO_KEY=...
export THALOVANT_SITE_ID=...
export THALOVANT_HUB_HTTP_HOST=https://hub.example.com
export THALOVANT_HUB_HTTP_PORT=443

thalovant doctor
```

You can override host or port without editing the identity file:

```bash
thalovant --identity _identity.json --host https://hub.example.com --port 443 health
```

## Commands

### `doctor`

```bash
thalovant --identity _identity.json doctor
thalovant --identity _identity.json --json doctor
```

Runs identity, endpoint, connect, handshake, and transport checks.

### `health`

```bash
thalovant --identity _identity.json health
```

Checks the live transport state.

### `ask`

```bash
thalovant --identity _identity.json ask "Tell me a joke"
thalovant --identity _identity.json --json ask "Tell me a joke"
```

Sends a text utterance and waits for the hub reply.

### `utter`

```bash
thalovant --identity _identity.json utter "Turn on the lights"
```

Sends an utterance without waiting for a reply.

### `listen`

```bash
thalovant --identity _identity.json listen speak --timeout 30 --max-events 3
```

Prints matching events until `timeout` expires or `max-events` is reached.

### `emit`

```bash
thalovant --identity _identity.json emit recognizer_loop:utterance \
  --data '{"utterances":["hello"],"lang":"en-us"}' \
  --context '{"source":"demo"}'
```

Emits a raw OVOS/HiveMind event.
