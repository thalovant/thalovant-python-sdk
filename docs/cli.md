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

### `intents`

```bash
thalovant --identity _identity.json intents
thalovant --identity _identity.json intents --lang en-us --lang fr-fr --all
thalovant --identity _identity.json --json intents
```

Lists what the hub can be asked, grouped by skill, with the sentences a person
says to reach each intent in each language, as the skill wrote them (`{location}`
marks a slot). Two sentences per intent by default, `--all` for every one. The
hub's connection must be allowed to publish `ovos.intent.list` and
`ovos.intent.describe`; a hub allowed for only the engine manifests lists names
and says so.

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
