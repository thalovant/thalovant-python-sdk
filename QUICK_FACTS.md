Last Edit: Claude Sonnet 5.5 - 2026-11-11 - Motive: Intercom (0.12.0): announce, speakers and settings on the control plane; final API (urgent scope, speaker kind/can_play/policy_synced, typed errors, replayed).

# Quick facts: thalovant (Python SDK)

| Fact | Value |
| :--- | :--- |
| Package | `thalovant` on PyPI |
| Version | 0.13.0 (`pyproject.toml`, `src/thalovant/_version.py`) |
| Python | 3.10+ |
| Role | Reference implementation for the SDK parity contract (`contracts/sdk-parity.json`) |
| Entry points | console script `thalovant` (`thalovant.cli:main`); import `thalovant` |
| Data plane | `ThalovantClient`, `AsyncThalovantClient`, `ThalovantAgent`, `AsyncHubSession` |
| Control plane | `ThalovantControlPlane`, `AsyncThalovantControlPlane` (`src/thalovant/control.py`) |
| Errors | `ThalovantAPIError` (`status_code`, `code`, `detail`, `problem`, `fields`), `ThalovantPlanError`, `ThalovantAuthError` (`src/thalovant/errors.py`) |
| Release | merge to `main` with a new version -> `auto-release.yml` tags and dispatches `publish.yml` (PyPI trusted publishing) |
| Parity reference digest | `8687c8429e2827ce098a22bfcdeee6545b8bfdf4a18d2ff4c1371eeb477b487c` (0.13.0 candidate; `690784dbb9af...` on 0.12.0) |

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

### Intercom (0.12.0; scopes `intercom:read`, `intercom:send`, `intercom:urgent`, `hubs:write`)

| Call | Endpoint |
|---|---|
| `get_intercom_settings(hub_id)` | `GET /v1/hubs/{id}/intercom/settings` |
| `set_intercom_settings(hub_id, enabled)` | `PUT /v1/hubs/{id}/intercom/settings` |
| `list_intercom_speakers(hub_id)` (speakers have `kind`, `can_play`; the list `policy_synced`) | `GET /v1/hubs/{id}/intercom/speakers` |
| `update_intercom_speaker(hub_id, client_id, *, receive, room, groups, dnd)` | `PATCH /v1/hubs/{id}/intercom/speakers/{client_id}` |
| `announce(hub_id, text, targets, *, priority, repeat, lang, idempotency_key, wait)` (use a fresh `uuid4` key per announcement; urgent needs `intercom:urgent` too; `Announcement.replayed`) | `POST /v1/hubs/{id}/intercom/announce` |
| `get_announcement(hub_id, id)` / `list_announcements(hub_id, *, limit, cursor)` | `GET /v1/hubs/{id}/intercom/announcements[/{id}]` |
| `cancel_announcement(hub_id, id)` | `POST /v1/hubs/{id}/intercom/announcements/{id}/cancel` (`intercom:send`; 409 `not_cancellable` with `announcement_status`) |

Typed errors (constants in `IntercomProblemCode`): `urgent_scope_required`, `intercom_urgent_busy`, `skill_not_installed`, `not_a_speaker`, `reserved_name`, `hub_locked`, `impersonation_not_allowed`, `too_many_targets`, `intercom_unavailable` (`reason`), `intercom_disabled_platform`, `intercom_not_configured`, `managed_client`, `not_cancellable`, `hub_unreachable` (`announcement_id`). Text rule: NFC, 1-280, no `<` `>`, control, bidi/format characters or lone surrogates; DND on needs a valid IANA timezone and start != end; at most 50 names per target list.
Models in `src/thalovant/intercom.py`; guide `docs/guides/intercom.md`. Announce-only, not a certified safety system; the API does not store the text (keyed hash and length only).
`announce` uses a request timeout of at least 25 s (`INTERCOM_ANNOUNCE_MIN_TIMEOUT`). Idempotency: one fresh `uuid4` key per announcement; the same key replays the stored result and never re-sends (a replayed `failed` stays `failed`); after a 502 read `ThalovantHubUnreachableError.announcement_id`. Ids (hub, client, announcement) that are `.`, `..`, empty or hold `/ ? #`, a backslash or a control character are refused before any request.
