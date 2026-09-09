# Thalovant Python SDK

Python SDK for connecting apps, services, kiosks, and agents to Thalovant hubs.

The control API is used to discover hubs and provision a client identity. After
that, the SDK talks directly to the hub data plane over HTTPS, WSS, or MQTTS.

```text
Thalovant API      -> discover hubs, create client identity
Python SDK         -> connect to the hub data plane
Hub runtime        -> skills, events, replies
```

Full docs: <https://docs.thalovant.com/developers/sdks/python/>

## What You Need

- A Thalovant account with API access for authenticated control-plane actions.
- A hub id or slug.
- A client identity for that hub. You can create one through the API or use one
  downloaded from the dashboard.

## Install

```bash
pip install thalovant
```

For local SDK development:

```bash
pip install -e ".[dev]"
```

## Quick Start

This is the normal first integration flow.

```python
from thalovant import ThalovantClient, ThalovantControlPlane

api = ThalovantControlPlane()

# Public hub discovery does not require auth.
public_hubs = api.list_public_hubs(limit=12)
for hub in public_hubs["data"]:
    print(hub["id"], hub["slug"], hub["title"])

# Auth is required when creating a client identity.
api.login("you@example.com", "password")

result = api.create_client_identity(
    "hub-id",
    name="python-demo-client",
    preferred_protocols=("wss", "https", "mqtt"),
)

with ThalovantClient(result.identity, protocol="wss") as client:
    info = client.connection_info()
    print("connected in", info.connect_ms, "ms")
    reply = client.ask("Tell me a short clean joke.")
    print(reply.text)
```

Accounts created through Google sign-in have no password. Use the browser
device flow instead of `login(...)`:

```python
api.login_with_browser()
```

This prints a short code and a verification URL, opens your browser to the
approval page, and waits for you to approve the request in the dashboard. On
approval the SDK stores a scoped, revocable API token, exactly like
`login(...)`.

`ThalovantControlPlane()` uses `https://api.thalovant.com` by default. Pass a
different URL only for local development or a self-hosted control plane.

Keep `result` secret: `result.identity` and the raw `result.client` API
response both carry the client credentials. `result.as_dict()` (the default)
redacts every secret — identity and client alike — and is safe to log.
`result.as_dict(include_secrets=True)` returns the real credentials for
persisting an identity file and must never be logged; the same rule applies to
`result.identity.as_dict(include_secrets=True)`.

## Token Auth For CI And Automation

Headless environments (CI jobs, AI agents, cron tasks) should skip login
entirely: mint a scoped API token once, then pass it to the constructor.

```python
import os

from thalovant import ThalovantControlPlane

api = ThalovantControlPlane(access_token=os.environ["THALOVANT_API_TOKEN"])

# Ready immediately; no login call needed.
page = api.list_hubs(limit=50)
```

```bash
# CI configuration
export THALOVANT_API_TOKEN="tvpat_..."  # store in your CI secret manager
```

Tokens come from the dashboard's API Tokens page or from
`login_with_browser()`. Either way the token is durable, scoped, and
revocable: grant only the scopes the job needs, and revoke it from the same
page when the job is retired. The SDK never reads `THALOVANT_API_TOKEN` on its
own, so pass it explicitly as shown above.

## List Your Hubs

Authenticated accounts can list owned or visible hubs:

```python
api = ThalovantControlPlane()
api.login("you@example.com", "password")

page = api.list_hubs(limit=50)
for hub in page["data"]:
    print(hub["id"], hub["slug"], hub["title"])
```

## Provision Hubs

Hubs, runtime groups, and skills can be created and managed from code. These
routes need a **paid plan** and a token with the **`hubs:write`** scope
("Create and update your hubs" on the dashboard's API Tokens page).

The scope is checked before the plan, which decides what you actually see.
Free-plan API tokens can only be minted with `hubs:read`, `clients:read`, and
`clients:write`, so a free-plan API token can never carry `hubs:write` and
**never reaches the plan gate**: every call below fails with HTTP 403
`Insufficient scopes`, not HTTP 402. The 402 `API access requires a paid plan.`
shows up only for a dashboard session token, or for an API token that was
minted on a paid plan and kept after a downgrade.

```python
api = ThalovantControlPlane(access_token=os.environ["THALOVANT_API_TOKEN"])

# 1. Create a runtime group to run the skills.
group = api.create_runtime_group({"name": "kiosks", "description": "Lobby kiosks"})

# 2. Create a hub attached to it. spec.version is required.
hub = api.create_hub(
    {
        "name": "joke-garden",
        "runtime_group_id": group["id"],
        "spec": {"version": "1", "protocols": {"wss": {"enabled": True}}},
    }
)

# 3. Discover what is installable before installing anything.
for skill in api.list_marketplace_skills()["data"]:
    print(skill["skill_id"], skill["title"], skill["access_tier"])

# 4. Install a skill from the marketplace catalog.
api.install_runtime_group_skill(group["id"], "skill-weather")

# 5. Release: roll the runtime and the hub onto a release channel.
api.release_runtime_group(group["id"], channel="stable")
api.release_hub(hub["id"], channel="stable")
```

A hub's `spec` is schema-validated and **must carry a non-empty `version`
string**; omitting it fails with HTTP 422 `Schema validation failed` rather
than defaulting.

To retry hub creation safely, generate and retain an `idempotency_key` before
the first call and pass that same key with the same payload on every retry.
Omitting it generates a
new `Idempotency-Key` for each invocation, so calling again after a timeout
can create a second hub. Reusing the original key returns the original hub.

Updating and deleting a hub use optimistic locking. Pass the `etag` from the
hub resource you read; the SDK sends it as `If-Match`, and the API rejects a
stale or missing value with HTTP 412 without changing anything:

```python
hub = api.get_hub(hub["id"])
hub = api.update_hub(hub["id"], {"active": False}, etag=hub["etag"])
```

The `get_hub` first is mandatory, and it is the *body* you need: the validator
lives only in the hub resource's `etag` field. The API sends **no `ETag`
response header**, so there is nothing to read off the response.

`name`, `namespace`, and `domain` are immutable after creation. Sending a
*different* value for one of them fails with HTTP 400
`<Field> cannot be changed after hub creation`; sending the value the hub
already has is accepted and ignored. Patch only the fields you mean to change
rather than feeding a whole hub resource back in. The SDK deliberately does not
reject these client-side — it cannot know the stored values, and refusing them
outright would reject patches the API accepts.

Deleting a hub also deletes its clients and ACLs. Runtime groups have no
`If-Match` requirement, but the API refuses to delete the workspace default
group or a group that still has hubs attached (HTTP 409).

The Python helper reads and merges the stored runtime configuration before
replacing it. The API provides no revision or conditional-write token for this
route: serialize updates across callers to avoid overwriting a concurrent
change. Pass `merge=False` only when replacing the complete configuration:

```python
api.update_runtime_group_config(group["id"], {"lang": "en-us"})
print(api.get_runtime_group_config(group["id"])["config"])
```

Reading what a hub is running needs the `hubs:inspect` scope instead (which
`hubs:read` implies, so a free-plan token has it). The answer is not always
live, so branch on `source`:

```python
capabilities = api.get_hub_runtime_capabilities(hub["id"])
if capabilities["source"] == "ovos-runtime":
    print("live:", capabilities["counts"]["total_intents"])
else:
    # ovos-runtime-unavailable / ovos-runtime-timeout: the runtime group's
    # snapshot, i.e. what the group is configured to run, not what is running.
    print("stale:", capabilities["source"])
```

HTTP 409 comes back only when there is no snapshot to fall back on either —
the hub belongs to no runtime group, or that group has no desired and no
observed skills. A hub with a configured group returns a stale HTTP 200 far
more often than a 409. The route is rate limited per caller and hub; HTTP 429
carries a `Retry-After` header.

Delete the example hub only after the capability reads are complete:

```python
hub = api.get_hub(hub["id"])
api.delete_hub(hub["id"], etag=hub["etag"])
```

## Discover Skills

The marketplace catalog is readable with the **`hubs:read`** scope and, unlike
the provisioning routes above, is **not paid-gated** — a free-plan token can
browse the whole catalog before upgrading, and only the install needs a paid
plan.

```python
for skill in api.list_marketplace_skills()["data"]:
    print(skill["skill_id"], skill["category"], skill["access_tier"])
```

Each entry carries what an install needs (`skill_id`, `source_type`,
`source_ref`, `config_schema`, `secret_schema`) next to presentation fields
(`title`, `summary`, `tags`, `verified`). Admin tokens can additionally pass
`owner_id=` to read another tenant's catalog and `include_inactive=True` to see
retired entries; both are ignored for non-admin callers — a non-admin's
`owner_id` is silently replaced with their own rather than rejected, unlike
`list_runtime_groups`, where the same mistake is a hard HTTP 403.
`force_refresh=True` re-syncs the global catalog from source first, which is
slower.

Two group-scoped reads need the **`hubs:inspect`** scope and are likewise not
paid-gated. The first resolves the catalog against one runtime group, so each
entry reports whether it is already desired, whether it was observed running,
and whether the tenant plan allows installing it:

```python
view = api.list_runtime_group_marketplace(group["id"])
for entry in view["data"]:
    if entry["installable"] and not entry.get("active"):
        print("available:", entry["skill_id"])
```

The second answers what the group is actually running right now, rather than
what could be installed:

```python
inventory = api.list_runtime_group_inventory(group["id"], refresh=True)
print(inventory["source"], len(inventory["data"]))
```

Both answer from a cached inventory snapshot by default; pass
`refresh_inventory=True` or `refresh=True` to force a live read from the
runtime operator.

The two routes report `source` from different vocabularies. A default
(non-refreshing) `list_runtime_group_marketplace` returns `runtime-group-cache`
or `runtime-group-cache-empty` (or `ovos-runtime-operator` when the operator's
status differed and the API re-synced while serving);
`ovos-runtime-operator-pending` appears there only when you pass
`refresh_inventory=True`. `list_runtime_group_inventory` returns
`ovos-runtime-operator`, `runtime-group-cache`, or
`ovos-runtime-operator-pending`, and never `runtime-group-cache-empty`.

The `source` on the marketplace route describes the *observation*, not the
listing: its `data` is the catalog unioned with the group's desired and
observed skills, so it stays populated even when the snapshot is empty. Never
read an empty-sounding `source` as an empty `data`. Only
`list_runtime_group_inventory` returns an empty `data` when nothing is
reporting, and it does so with `source="ovos-runtime-operator-pending"` rather
than failing.

## Install Skills

`install_runtime_group_skill` answers HTTP 200, not 201 — it upserts, so
installing a skill that is already present updates that entry in place.

`source_type` is a free-form string of 1 to 32 characters, not an enum. Only
`catalog` (the default) and `git` are interpreted specially: `catalog` resolves
the skill against the marketplace and fails with HTTP 404 when it is not there,
`git` requires `source_ref` to be a valid repository URL. Anything else is
stored as sent.

Two different HTTP 402s can come back. `API access requires a paid plan.` is the
plan-level API gate on every provisioning route; `This skill requires paid
marketplace access for the tenant plan.` is a per-skill check on a catalog entry
whose `access_tier` is `paid`. A paid plan clears the first and can still fail
the second, so check `installable` and `purchase_required` from
`list_runtime_group_marketplace` first:

```python
view = api.list_runtime_group_marketplace(group["id"])
for entry in view["data"]:
    if entry["installable"]:
        api.install_runtime_group_skill(group["id"], entry["skill_id"])
    elif entry["purchase_required"]:
        print("needs marketplace access:", entry["skill_id"], entry["access_message"])
```

## Skills On One Hub

A hub can also carry skills of its own, next to whatever its runtime group
provides, and it can start with none at all. The four hub-skill calls address
**one hub by id** (the authenticated hub routes do not take slugs) and every
change applies live on that hub, typically within about fifteen seconds and
without restarting it.

```python
listing = api.list_hub_skills(hub["id"])
print(listing.source, listing.observed_at)
for skill in listing.data:
    print(skill.skill, skill.installed_version, skill.state, skill.update_available)

# Accepted at once: HTTP 202 with an operation_id, state "installing".
accepted = api.install_hub_skill(hub["id"], "skill-weather")
print(accepted.operation_id, accepted.state, accepted.previous_version)

# Or poll the operation until it converges (default timeout 120 s).
done = api.install_hub_skill(hub["id"], "skill-weather", version="1.2.0", wait=True)
print(done.state)  # "installed"

api.update_hub_skill(hub["id"], "skill-weather", version="latest", wait=True)
api.remove_hub_skill(hub["id"], "skill-weather", wait=True)  # state "removed"
```

`list_hub_skills` returns a typed `HubSkillList`: the envelope says where the
reading came from (`hub_id`, `runtime_group_id`, `observed_at`, `source`, the
runtime's phase and message) and `data` holds one `HubSkill` row per skill
(`skill`, `title`, `version`, `installed_version`, `observed_version`,
`latest_version`, `update_available`, `active`, `state`, the runtime's last
error, and more). A row's `state` is `pending`, `installed`, `failed`,
`removing`, `drifted`, `quarantined`, or `unmanaged`; a change in progress
shows as `pending`.

The writes return a typed `HubSkillOperation` (`operation_id`, `hub_id`,
`runtime_group_id`, `skill`, `version`, `previous_version`, `state`).
Installing a skill the hub already carries at another version performs an
update. With `wait=True` a `failed` or `timed_out` operation raises
`ThalovantAPIError` carrying the operation's error message, and running past
`timeout` raises `ThalovantTimeoutError`. The API answers HTTP 409
`skill_version_already_installed` for the same version, HTTP 404
`hub_without_runtime_group` when the hub has no runtime group yet (a plain
404 for an unknown hub or a skill that is not installed), and HTTP 422 for an
unresolvable `"latest"` or an invalid version; the problem `code` is appended
to the error message, for example
`HTTP 409: Skill version already installed. (skill_version_already_installed)`.
Listing needs `hubs:inspect` (`hubs:read` implies it); the writes need
`hubs:write` and a paid plan. Hub-restricted tokens are honoured.

The same four commands are on the CLI, authenticated with
`THALOVANT_API_TOKEN` (or `--token`) against `THALOVANT_API_URL` (or
`--api-url`), with `--json` for machine-readable output:

```bash
thalovant skills list --hub <hub-id>
thalovant skills add --hub <hub-id> skill-weather --version latest --wait
thalovant skills update --hub <hub-id> skill-weather --version 1.2.0
thalovant skills remove --hub <hub-id> skill-weather
```

## Workspace Analytics

Authenticated accounts can read the same overview used by the dashboard:

```python
overview = api.get_analytics_overview(range="7d", hub_id="hub-id")
print(overview["totals"])
```

## Durable Memory

Private Daily Desk and workspace assistants can manage explicit opt-in memory:

```python
memory = api.create_memory_item(
    {
        "scope": "workspace",
        "kind": "preference",
        "content": "Prefer America/Toronto for scheduling.",
        "tags": ["timezone"],
    }
)
print(memory["id"])

items = api.list_memory_items(scope="workspace", query="timezone")
print(items["data"])
```

## What Can I Ask?

A connected client can ask its hub what it can be asked, over its own session,
with no control-plane token:

```python
from thalovant import ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    inventory = client.intents(["en-us", "fr-fr"])
    for skill in inventory.skills:
        for intent in skill.intents:
            print(intent.id, intent.examples("fr-fr"))
```

Each intent carries the sentences a person says to reach it, per language, as
the skill wrote them (`{location}` marks a slot). The hub's connection must be
allowed to publish `ovos.intent.list`; `ovos.intent.describe` is needed only
when the client has to ask for the definitions separately, which a runtime
attaching them to the listing never makes it do. A hub that refuses
`ovos.intent.list` raises `ThalovantPolicyDeniedError` naming the type, or with
the default `fallback=True` lists intent names only from the engines' manifests
and marks the result `source="engine-manifests"`, `denied=("ovos.intent.list",)`.
From the CLI: `thalovant --identity _identity.json intents`.

## Use An Existing Identity

For local development, store one or more identities in the protected SDK config:

```bash
mkdir -p ~/.config/thalovant
chmod 700 ~/.config/thalovant
$EDITOR ~/.config/thalovant/config.yaml
chmod 600 ~/.config/thalovant/config.yaml
```

```yaml
profile: prod
profiles:
  prod:
    identity:
      access_key: ...
      password: ...
      site_id: demo-agent
      default_master: https://jokes.thalovant.io
      data_plane_endpoints:
        wss: wss://jokes.thalovant.io/public
        https: https://jokes.thalovant.io/public
        mqtt: mqtts://mqtt.thalovant.com:8883
      mqtt:
        endpoint: mqtts://mqtt.thalovant.com:8883
        username: ...
        password: ...
        topic_prefix: hubs/hub-id/clients/client-id
        tls: true
```

```python
from thalovant import ThalovantClient

with ThalovantClient.from_config(profile="prod") as client:
    reply = client.ask("What can this hub do?")
    print(reply.text)
```

SDKs reject config files that are readable or writable by other users on Linux
and macOS. Keep this file out of git.

Raw identity files are supported too:

```python
from thalovant import ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    reply = client.ask("What can this hub do?")
    print(reply.text)
```

Environment variables are supported too:

```bash
export THALOVANT_ACCESS_KEY=...
export THALOVANT_PASSWORD=...
export THALOVANT_CRYPTO_KEY=...
export THALOVANT_SITE_ID=...
export THALOVANT_HUB_HTTPS_HOST=https://hub.example.com
export THALOVANT_HUB_WSS_HOST=wss://hub.example.com
export THALOVANT_HUB_MQTT_HOST=mqtts://mqtt.thalovant.com:8883
export THALOVANT_MQTT_USERNAME=...
export THALOVANT_MQTT_PASSWORD=...
export THALOVANT_MQTT_TOPIC_PREFIX=hivemind/hub-id/client-id
```

```python
from thalovant import ThalovantClient

with ThalovantClient.from_env(protocol="https") as client:
    print(client.ask("Say hello.").text)
```

## Save A Provisioned Identity

Only save identities in a secret store or local developer file that is ignored
by git.

```python
import json
from pathlib import Path

Path("_identity.json").write_text(
    json.dumps(result.identity.as_dict(include_secrets=True), indent=2),
    encoding="utf-8",
)
```

## Protocols

Hubs may expose one or more public data-plane protocols:

- `wss`: secure realtime WebSocket, the default public path and SDK preference.
- `https`: request/response HTTP protocol exposed as HTTPS.
- `mqtt`: broker-mediated MQTT over TLS. Requires per-client broker credentials.

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
Uppercase and lowercase hexadecimal spellings of the same server key are
equivalent; reconfirming that key preserves the saved identity file unchanged.
Malformed stored pins raise `ThalovantConnectionError`; restore the verified
state instead of deleting it to retry. From 0.5.6, an expired HTTPS Noise
handshake raises `ThalovantTimeoutError` after cleaning up the failed connection.

HTTPS and WSS verify server certificates by default. Configure a trusted CA for
private certificates; HTTPS also honors Requests' `REQUESTS_CA_BUNDLE`. For an
explicit development-only exception, construct a transport with
`self_signed=True` and pass it as the client's `transport`. This opt-in disables
certificate verification and should not be used for public hubs.

Inspect what an identity supports:

```python
identity = result.identity

print(identity.enabled_protocols())
print(identity.endpoint_for("wss"))
print(identity.endpoint_for("https"))
print(identity.endpoint_for("mqtt"))
print(identity.mqtt.endpoint if identity.mqtt else None)
```

Connect with a specific protocol:

```python
from thalovant import ThalovantClient

for protocol in ("wss", "https", "mqtt"):
    if not result.identity.supports_protocol(protocol):
        continue
    if protocol == "mqtt" and result.identity.mqtt is None:
        continue

    with ThalovantClient(result.identity, protocol=protocol) as client:
        print(protocol, client.ask(f"Reply over {protocol}.").text)
```

From 0.5.8, `client.connect(timeout=...)` uses one deadline for waiting for a
previous attempt, transport setup and authenticated readiness. It raises
`ThalovantConnectionError` if the session is not ready when that budget expires.
This replaces 0.5.7's extra, best-effort readiness allowance, which could return
without an admitted session. Increase the caller's timeout for slow hubs.
Timeout and async connect cancellation start cleanup without waiting beyond the
caller's budget. A replacement waits for the retired connect and cleanup to
finish, preventing a late attempt from replacing or closing a new session.
`close(timeout=...)` uses its own caller deadline (defaulting to the configured
connection budget). If close times out, `wait_closed()` observes actual retained
cleanup; await that before passing the identity to another client instance.
From 0.5.11, a refused or unacknowledged HTTP disconnect raises
`ThalovantConnectionError` and retains the session's admission and replica cookie.
`wait_closed()` also reports that failure, and reconnect is blocked. Retry
`close()` on the same client after the endpoint recovers; only a successful
disconnect acknowledgment releases the admission. From 0.5.12, the upstream
`{"error": "Already Disconnected"}` reply also confirms cleanup when a prior
successful response was lost. This exception applies only to `/disconnect`.
A close that encounters
cleanup already owned by a direct transport call reports that cleanup is still
in progress; it cannot claim completion for the other call.
Intent query deadlines also cover reconnect and send. The optional fallback-skill
probe uses at most 1.5 seconds, preserving unknown state when unavailable or
explicitly failed, and retains ownership of any timed-out send until it retires.

Use `client.connect_with_info()` when you need connection telemetry for
benchmarks or health dashboards. The returned snapshot includes phase,
socket/open time, handshake time, total connect time, and last error.

Use `client.query(...)` for the direct HiveMind query frame path when the hub
supports it. It avoids broad bus fanout and is the preferred request/reply API
for low-latency app integrations.

```python
reply = client.query("What time is it in Toronto?")
print(reply.text)
```

Since 0.5.10, `query(timeout=...)` includes connection, authenticated readiness,
send, and reply collection in one deadline. Direct `query`
and routed `cascade` replies share the same query ID filter. Intent misses are
provisional until `hive.query.complete`; later speech clears a provisional
failure. Completion and hard policy/query-timeout failures stop collection, so
later events cannot change the result. A hard failure after speech preserves
the partial text with `handled=False`. An unanswered query still times out.
Blocked I/O retains session ownership until cleanup finishes, and an expired
connection attempt cannot send a query later. Completion returns immediately
without the Ask settlement delay.

`ask()` also includes connect, send, preparation retries, and settling in its
caller deadline. The first nonempty speech starts a fixed settle window
(`reply_settle_seconds=0.25`); a handled event or soft intent miss without speech
starts a fixed empty-reply window (`empty_reply_wait_seconds=5.0`). Later speech
switches the empty window to settling and recovers the soft miss. Neither repeated
events nor later fragments extend these windows, and both are clipped by the
original deadline. A hard policy/query-timeout failure freezes the result
immediately. Empty results raise a runtime failure or timeout. Ask requires a
matching request ID; ambient or differently correlated replies cannot satisfy
the request. Both Ask and Query report the first accepted nonblank runtime
session ID, falling back to the requested session when none is reported. Cancelling an
async ask, query, event wait, or listener removes its handlers and retires any
active connection/write it owns; a queued caller cannot close another caller's
session. Transport status checks run outside the waiting caller's thread.

`wait_for_event(timeout=...)` and `listen(timeout=...)` include connection and
subscription setup in the deadline. A listener without a timeout uses the normal
connect budget and can then listen indefinitely. Both sync and async listeners
accept `max_buffered_events=256`; it must be a positive integer. Overflow raises
`ThalovantRuntimeError` and retires the subscription. A one-event wait keeps the
first correlated match and ignores subsequent events.

Application requests are never automatically replayed after publication starts,
including when a local write error leaves the remote outcome uncertain.
`auto_reconnect` and `reconnect_attempts` apply to connection preparation before
publication. This corrects earlier behavior that could emit the same application
request twice after a connection error.

MQTT identities include a broker endpoint, username, password, TLS flag, and
topic prefix. The broker credentials are scoped to that client and should be
treated like a password. Public identities should use `mqtts://`; the SDK also
honors an explicit `tls: true` flag from the identity.

Use a fresh request ID for each logical Ask and a fresh query ID for each
logical Query. One client rejects overlapping collectors with the same ID
before dispatch. Ask request IDs and scoped Query IDs are separate namespaces.
Cancellation or completion releases the reservation after listeners retire;
a new logical operation should still use a new ID to exclude late replies.

## Conversations

Use a conversation when several turns should share one session.

```python
from thalovant import ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    with client.conversation(lang="en-us") as convo:
        print(convo.ask("Remember that my favorite color is blue.").text)
        print(convo.ask("What color did I mention?").text)
```

## Events

You can wait for, stream, or subscribe to hub events.

```python
from thalovant import EVENT_SPEAK, ThalovantClient

with ThalovantClient.from_identity_file("_identity.json") as client:
    for event in client.listen(EVENT_SPEAK, timeout=30, max_events=1):
        print(event.text)
```

Use timeouts in scripts so they do not wait forever.

## Client Context

Context lets skills know which app, device, user, or channel made the request.

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
    print(reply.text)
```

## Actions And Exact Inputs

Use actions for button payloads and codes for exact typed or scanned values.

```python
with client.conversation(session_id="work-session") as convo:
    convo.send_action('/choose{"id":"42"}', title="Choose item")
    convo.send_code("SN-001-XYZ", kind="qr", label="serial")
```

## Rich Responses

Replies can include text, choices, tables, images, or attachments.

```python
reply = client.ask("Show matching parts.")

for item in reply.display_items(max_text_chars=600):
    if item.kind == "text":
        print(item.text)
    elif item.kind == "choices":
        print([choice["title"] for choice in item.data])
```

## Async Apps

```python
import asyncio
from thalovant import AsyncThalovantClient


async def main():
    async with AsyncThalovantClient.from_config(profile="prod") as client:
        reply = await client.ask("What time is it?")
        print(reply.text)


asyncio.run(main())
```

## CLI Diagnostics

```bash
thalovant --identity _identity.json doctor
```

The doctor command checks identity shape, endpoint selection, authentication,
handshake, and transport health.

## Common Issues

- `Missing Thalovant API access token`: call `api.login(...)` (or
  `api.login_with_browser()` for accounts without a password) before private
  control-plane actions, or pass `access_token=` to `ThalovantControlPlane`.
- `API access requires a paid plan`: upgrade the workspace before using the SDK
  control-plane API to provision private resources.
- `Unsupported protocol`: the hub does not expose that protocol, or the
  identity was created before that protocol was enabled.
- MQTT fails immediately: create or download a fresh client identity after MQTT
  is enabled. MQTT needs the per-client `identity.mqtt` credentials.
- A request times out: increase `timeout` on `ask(...)` or check `doctor()`.
- `token_rate_limited`: the API token exceeded its plan's per-minute request
  rate (60 requests per minute on the free plan). The response is HTTP 429 with
  a `Retry-After` header and a matching `retry_after_seconds`; wait that long
  and resend.
- `token_quota_exceeded`: the API token used up its plan's daily or monthly
  call quota. The response names which in `quota`, alongside `limit` and
  `used`, and carries a `Retry-After` header and a matching
  `retry_after_seconds` pointing at the next UTC day or month. The SDK does
  not retry either 429 for you.

## API Shape

- `ThalovantControlPlane()`
- `ThalovantControlPlane(access_token=...)` to authenticate with an API token instead of logging in
- `ThalovantControlPlane(api_url, access_token=...)` for local or self-hosted control planes
- `control.login(email, password, scope=None, otp_code=None, recovery_code=None)`
  (MFA accounts pass a TOTP `otp_code` or a one-time `recovery_code`)
- `control.login_with_browser(scopes=None, client_name=None, open_browser=True, prompt=None, timeout=900.0)`
  (browser device-flow sign-in for accounts without a password)
- `control.list_public_hubs(limit=...)`
- `control.get_public_hub(hub_ref)`
- `control.list_hubs(limit=..., owner_id=...)`
- `control.get_hub(hub_id)`
- `control.create_hub(payload, idempotency_key=None)`
- `control.update_hub(hub_id, payload, etag=...)`
- `control.delete_hub(hub_id, etag=...)`
- `control.release_hub(hub_id, channel=..., mode=..., version=..., images=..., reason=...)`
- `control.set_hub_rating(hub_id, rating)`
- `control.clear_hub_rating(hub_id)`
- `control.get_hub_runtime_capabilities(hub_id)`
- `control.list_runtime_groups(owner_id=...)`
- `control.get_runtime_group(runtime_group_id)`
- `control.create_runtime_group(payload)`
- `control.update_runtime_group(runtime_group_id, payload)`
- `control.get_runtime_group_config(runtime_group_id)`
- `control.update_runtime_group_config(runtime_group_id, config, personas=None)`
- `control.release_runtime_group(runtime_group_id, channel=..., ...)`
- `control.delete_runtime_group(runtime_group_id)`
- `control.install_runtime_group_skill(runtime_group_id, skill_id, ...)`
- `control.uninstall_runtime_group_skill(runtime_group_id, skill_id)`
- `control.list_hub_skills(hub_id)`
- `control.install_hub_skill(hub_id, skill, version="latest", wait=False, timeout=120.0)`
- `control.update_hub_skill(hub_id, skill, version=..., wait=False, timeout=120.0)`
- `control.remove_hub_skill(hub_id, skill, wait=False, timeout=120.0)`
- `control.get_operation(operation_id)`
- `control.get_analytics_overview(...)`
- `control.list_memory_items(...)`
- `control.get_memory_summary(owner_id=...)`
- `control.create_memory_item(payload)`
- `control.get_memory_item(memory_id)`
- `control.update_memory_item(memory_id, payload)`
- `control.delete_memory_item(memory_id)`
- `control.create_client_identity(hub_id, ...)`
- `ThalovantIdentity.from_config(path=None, profile=None)`
- `ThalovantIdentity.from_file(path)`
- `ThalovantClient.from_config(path=None, profile=None)`
- `ThalovantClient.from_identity_file(path)`
- `ThalovantClient.from_env()`
- `ThalovantClient(identity, protocol="wss")`
- `client.connect_with_info()`
- `client.connection_info()`
- `client.query(text, context=...)`
- `client.ask(text, context=...)`
- `client.send_utterance(text, context=...)`
- `client.send_action(payload, ...)`
- `client.send_code(value, ...)`
- `client.listen(event_name, ...)`
- `client.conversation(...)`

## Development

```bash
pip install -e ".[dev]"
pytest
```

Control-plane requests reject redirects. Credential-bearing requests require HTTPS;
explicit `http://localhost`, `http://127.0.0.1` and `http://[::1]` endpoints remain
available for local development. API URLs must not contain embedded credentials.
