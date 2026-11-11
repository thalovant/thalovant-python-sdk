Last Edit: Claude Sonnet 5.5 - 2026-11-11 - Motive: Intercom (0.12.0): announce, speakers and settings on the control plane.

# Quick facts: thalovant (Python SDK)

| Fact | Value |
| :--- | :--- |
| Package | `thalovant` on PyPI |
| Version | 0.12.0 (`pyproject.toml`, `src/thalovant/_version.py`) |
| Python | 3.10+ |
| Role | Reference implementation for the SDK parity contract (`contracts/sdk-parity.json`) |
| Entry points | console script `thalovant` (`thalovant.cli:main`); import `thalovant` |
| Data plane | `ThalovantClient`, `AsyncThalovantClient`, `ThalovantAgent`, `AsyncHubSession` |
| Control plane | `ThalovantControlPlane`, `AsyncThalovantControlPlane` (`src/thalovant/control.py`) |
| Errors | `ThalovantAPIError` (`status_code`, `code`, `detail`, `problem`, `fields`), `ThalovantPlanError`, `ThalovantAuthError` (`src/thalovant/errors.py`) |
| Release | merge to `main` with a new version -> `auto-release.yml` tags and dispatches `publish.yml` (PyPI trusted publishing) |
| Parity reference digest | `690784dbb9af67d32523958808442d2b6ec5ea2f09ab881e56940b35333952a3` (0.12.0 candidate; `6dfb3bb31ac7...` on 0.11.0) |

## Sign-in (0.11.0)

| Method | What it does |
| :--- | :--- |
| `interactive_login(method="auto", scopes=None, device_name=None, open_browser=True, timeout=None)` | Browser sign-in when `can_open_browser()` and `GET /v1/auth/native/config` is usable, device login otherwise; `"browser"`/`"device"` force one |
| `browser_login(scopes=None, device_name=None, open_browser=True, timeout=300.0, prompt=None)` | PKCE (S256) on `http://127.0.0.1:<port>/callback`, client `thalovant-sdk`, exchange `POST /v1/auth/native/token` |
| `login_with_browser(...)`, `begin_device_login`, `poll_device_login` | Device login (RFC 8628), unchanged |
| `login(email, password, ...)` | Password sign-in, unchanged |

| Fact | Value |
| :--- | :--- |
| Capability | `browser-login` in `contracts/sdk-parity.json` (scope matrix of `device-login`; embedded-c not applicable) |
| Vectors | `contracts/conformance/browser-login-vectors.json` (119 cases), run by `tests/test_browser_login_vectors.py` |
| Code | `src/thalovant/control.py` (`browser_login`, `interactive_login`), `src/thalovant/native_auth.py` (URL, redirect, state, config, browser rule), `src/thalovant/_loopback.py` (listener) |
| Errors | `ThalovantDeviceLoginDenied` (denied), `ThalovantDeviceLoginExpired` (timeout, 300 s default), `ThalovantAPIError` (`invalid_grant`, config 404) |
| Porting brief | `docs/browser-login-porting.md` |

## Chat apps (0.10.0)

| Method | Route |
| :--- | :--- |
| `list_chat_app_connectors()` | `GET /v1/bridges/connectors` |
| `get_bridge_networks(hub_id=None)` | `GET /v1/bridges/networks` |
| `list_bridges(hub_id=None, *, owner_id=None, limit=None)` | `GET /v1/bridges` |
| `get_bridge(bridge_id)` | `GET /v1/bridges/{id}` |
| `create_bridge(hub_id, connector, *, token=None, secrets=None, settings=None)` | `POST /v1/bridges` |
| `update_bridge(bridge_id, *, allowed=None, require_mention=None, session_scope=None, paused=None, settings=None, etag=None)` | `PATCH /v1/bridges/{id}` (`If-Match` only when `etag` is given) |
| `replace_bridge_token(bridge_id, token)` | `PUT /v1/bridges/{id}/token` |
| `replace_bridge_secrets(bridge_id, secrets)` | `PUT /v1/bridges/{id}/secrets` |
| `list_bridge_chats(bridge_id)` | `GET /v1/bridges/{id}/chats` |
| `send_bridge_test_message(bridge_id, chat_id)` | `POST /v1/bridges/{id}/test` |
| `get_bridge_avatar(bridge_id)` | `GET /v1/bridges/{id}/avatar` |
| `delete_bridge(bridge_id)` | `DELETE /v1/bridges/{id}` |

Models and `SecretValue` live in `src/thalovant/chat_apps.py`; the guide is `docs/guides/chat-apps.md`.

### Intercom (0.12.0; scopes `intercom:read`, `intercom:send`, `hubs:write`)

| Call | Endpoint |
|---|---|
| `get_intercom_settings(hub_id)` | `GET /v1/hubs/{id}/intercom/settings` |
| `set_intercom_settings(hub_id, enabled)` | `PUT /v1/hubs/{id}/intercom/settings` |
| `list_intercom_speakers(hub_id)` | `GET /v1/hubs/{id}/intercom/speakers` |
| `update_intercom_speaker(hub_id, client_id, *, receive, room, groups, dnd)` | `PATCH /v1/hubs/{id}/intercom/speakers/{client_id}` |
| `announce(hub_id, text, targets, *, priority, repeat, lang, idempotency_key, wait)` | `POST /v1/hubs/{id}/intercom/announce` |
| `get_announcement(hub_id, id)` / `list_announcements(hub_id, *, limit, cursor)` | `GET /v1/hubs/{id}/intercom/announcements[/{id}]` |

Models in `src/thalovant/intercom.py`; guide `docs/guides/intercom.md`. Announce-only, not a certified safety system; the text is never stored by the platform.
