# Concepts

## Control Plane And Data Plane

Thalovant is the control plane for identity and policy. The Python SDK is not a
message proxy. It uses Thalovant-provisioned identity material and then connects
directly to a HiveMind hub listener through the selected hub data-plane path.

```text
Thalovant API / dashboard -> identity, ACL, policy, lifecycle
Python SDK / CLI          -> direct HiveMind hub data plane
hivemind-listener         -> OVOS bus and skills runtime
```

## Identity

The SDK accepts the identity fields already used by HiveMind clients:

- `access_key`
- `password`
- `crypto_key`
- `site_id`
- `default_master`
- `default_port`

`default_master` remains the backward-compatible hub endpoint. Newer payloads
may also include `data_plane_endpoints` for `https`, `wss`, and `mqtt`, plus
`protocols.wss/http/mqtt.enabled` flags.

The current Python runtime transport uses the HTTPS HTTP-protocol endpoint.

## Sessions And Request Correlation

Public hubs can serve multiple clients. The SDK adds request and session metadata
for high-level helpers such as `ask`, `send_utterance`, and conversations.

When a hub echoes session or request metadata, SDK event listeners use it to
ignore unrelated events. Missing metadata is treated as compatible so existing
HiveMind hubs that do not echo context continue to work.

## API Layers

Use the highest-level API that fits the job:

- `client.ask(...)` for one-off request/reply usage.
- `client.conversation(...)` for related turns sharing one session.
- `ThalovantAgent` or `AsyncThalovantAgent` for long-running workers.
- `client.listen(...)` and `client.on(...)` for event consumers.
- `client.emit(...)` only when you need low-level OVOS/HiveMind event control.

## Errors

All SDK exceptions inherit from `ThalovantError`.

- `ThalovantIdentityError` means identity material is missing or invalid.
- `ThalovantConnectionError` means the hub endpoint, auth, handshake, or
  transport failed.
- `ThalovantTimeoutError` means the hub did not answer in time.
- `ThalovantRuntimeError` means the hub reported a failed request.
