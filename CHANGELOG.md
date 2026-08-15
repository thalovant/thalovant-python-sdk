# Changelog

## 0.4.25

- Add hub provisioning to `ThalovantControlPlane`. The hub surface was read-only, so the `hubs:write` scope the dashboard sells ("Create and update your hubs") had no SDK method that could use it. New: `create_hub`, `update_hub`, `delete_hub`, `release_hub`, `set_hub_rating`, `clear_hub_rating`, and `get_hub_runtime_capabilities`.
- Add runtime group and skill management: `list_runtime_groups`, `get_runtime_group`, `create_runtime_group`, `update_runtime_group`, `get_runtime_group_config`, `update_runtime_group_config`, `release_runtime_group`, `delete_runtime_group`, `install_runtime_group_skill`, and `uninstall_runtime_group_skill`.
- Honor the API's optimistic locking on the hub write routes. `update_hub` and `delete_hub` take a required `etag` keyword and send it as `If-Match`; the API rejects a stale or missing value with HTTP 412 and changes nothing. Runtime group routes do not use `If-Match`. `create_hub` sends an `Idempotency-Key` header, generated unless you pass `idempotency_key`, so a retried create cannot make a second hub.
- Document the gates these routes sit behind. Everything except the rating, config-read, list/get, and runtime-capabilities methods needs a paid plan and a `hubs:write` token; the ratings need `hubs:write` only; `get_hub_runtime_capabilities` needs `hubs:inspect`; the runtime group reads need `hubs:read`. Both gates surface as the usual `ThalovantAPIError` (HTTP 402 `API access requires a paid plan.`, HTTP 403 `Insufficient scopes`).
- Add a hub-provisioning walkthrough to the README (runtime group, hub, skill, release) and document every new method in `docs/api-reference.md`.
- No existing method signature changed.

## 0.4.24

- Fix the stale data-plane user agent. `thalovant.client.DEFAULT_USERAGENT` was pinned at `ThalovantPythonSDK/0.4.19`, so the 0.4.20, 0.4.21, 0.4.22, and 0.4.23 releases all identified themselves as 0.4.19 to hubs. The control-plane user agent was correct but only because it was hand-maintained every release.
- Derive every user agent from a single source of truth. `thalovant/_version.py` now owns `__version__` and builds `USER_AGENT` from it; `client.DEFAULT_USERAGENT` and `control.DEFAULT_CONTROL_USER_AGENT` are both that value, and `thalovant.__version__` re-exports it, so no version literal can drift again. Public names and values are unchanged.
- Add `tests/test_version.py`, which pins each user agent to `__version__`, matches the package version against `pyproject.toml`, and rejects any hard-coded version literal in a user-agent string.
- Document `retry_after_seconds` on the `token_quota_exceeded` bullet in the README. Both API-token 429s carry it, but only `token_rate_limited` said so, which left callers of the quota error without the programmatic retry field.

## 0.4.23

- Document token auth for CI and automation in the README: pass a scoped API token as `ThalovantControlPlane(access_token=os.environ["THALOVANT_API_TOKEN"])` to skip the login call entirely. Tokens come from the dashboard's API Tokens page or from `login_with_browser()`, and are durable, scoped, and revocable. The SDK does not read the environment variable itself, so the example passes it explicitly.
- Document the two API-token 429 responses under Common Issues: `token_rate_limited` for the plan's per-minute request rate (60 per minute on the free plan) and `token_quota_exceeded` for the daily or monthly call quota, which names the `quota`, `limit`, and `used`. Both carry `Retry-After` and `retry_after_seconds`; the SDK does not retry either automatically.

## 0.4.22

- Add `ThalovantControlPlane.login_with_browser()`: RFC 8628-style browser device-flow sign-in for accounts without a password (for example Google sign-in). It prints a short user code and verification URL (or hands the authorization payload to a custom `prompt` callable), optionally opens the browser at `verification_uri_complete`, polls the token endpoint at the server-provided interval (adding five seconds on `slow_down`), and stores the resulting scoped API token on `access_token` exactly like `login()`. Denied and expired requests raise `ThalovantAPIError`; exceeding `timeout` raises `ThalovantTimeoutError`.

## 0.4.21

- Add the `OperationStatus` literal type (`requested`, `committed`, `applied`, `ready`, `failed`, `timed_out`) and use it for `OperationResource.status`; export it from the package root.
- Add optional `otp_code` and `recovery_code` parameters to `ThalovantControlPlane.login()` for MFA-enabled accounts; they are sent only when provided.
- Fix the documented `preferred_protocols` default in the API reference to the actual `("wss", "https", "mqtt")` order.

## 0.4.20

- Add `OperationResource` and `ThalovantControlPlane.get_operation()` for polling durable control-plane commands.
