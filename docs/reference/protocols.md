# Protocols

### Noise authentication and persistent identity

From 0.5.5, WSS, HTTPS and MQTT all complete the HiveMind v3 Noise handshake
before reporting readiness. The client derives its PSK from the identity
password and the hub node ID using Argon2id. Both `25519_ChaChaPoly_SHA256` and
`25519_AESGCM_SHA256` are supported: XXpsk2 on first contact, or KKpsk0 when a
trusted server key is available. Older non-Noise offers are refused.

The SDK uses the published `hivemind-bus-client` and `poorman-handshake`
primitives; HTTPS and MQTT do not require a private or patched client wheel.
HTTPS preserves the replica-affinity cookie and exchanges ciphertext through
the binary endpoints. MQTT uses the identity's broker credentials and topics,
then exchanges raw Noise ciphertext. After broker loss, reconnect the transport
(or use the client's normal reconnect-on-send behavior).

Keep the client static key and server pins between reconnects and restarts.
The default location remains the existing HiveMind identity under the XDG
configuration directory. To use a dedicated private directory:

```python
client = ThalovantClient(
    identity,
    protocol="https",
    noise_state_dir="/var/lib/my-agent/thalovant-noise",
)
client.connect()
```

Changing state directories creates a different client identity unless you
migrate the existing key and pins. Authentication failure never deletes a
trusted server pin automatically. An intentional server-key replacement
requires verifying the new identity before removing the saved pin.
Malformed stored pins raise `ThalovantConnectionError`; restore the verified
state instead of deleting it to retry. From 0.5.6, an expired HTTPS Noise
handshake raises `ThalovantTimeoutError` after cleaning up the failed connection.

HTTPS and WSS verify server certificates by default. Configure a trusted CA for
private certificates; HTTPS also honors Requests' `REQUESTS_CA_BUNDLE`. For an
explicit development-only exception, construct a transport with
`self_signed=True` and pass it as the client's `transport`. This opt-in disables
certificate verification and should not be used for public hubs.

::: thalovant.protocols
