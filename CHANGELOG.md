# Changelog

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
