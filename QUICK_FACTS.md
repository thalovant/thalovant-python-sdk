Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Create the quick facts with the 0.10.0 chat apps surface.

# Quick facts: thalovant (Python SDK)

| Fact | Value |
| :--- | :--- |
| Package | `thalovant` on PyPI |
| Version | 0.10.0 (`pyproject.toml`, `src/thalovant/_version.py`) |
| Python | 3.10+ |
| Role | Reference implementation for the SDK parity contract (`contracts/sdk-parity.json`) |
| Entry points | console script `thalovant` (`thalovant.cli:main`); import `thalovant` |
| Data plane | `ThalovantClient`, `AsyncThalovantClient`, `ThalovantAgent`, `AsyncHubSession` |
| Control plane | `ThalovantControlPlane`, `AsyncThalovantControlPlane` (`src/thalovant/control.py`) |
| Errors | `ThalovantAPIError` (`status_code`, `code`, `detail`, `problem`, `fields`), `ThalovantPlanError`, `ThalovantAuthError` (`src/thalovant/errors.py`) |
| Release | merge to `main` with a new version -> `auto-release.yml` tags and dispatches `publish.yml` (PyPI trusted publishing) |

## Chat apps (0.10.0)

| Method | Route |
| :--- | :--- |
| `list_chat_app_connectors()` | `GET /v1/bridges/connectors` |
| `get_bridge_networks(hub_id=None)` | `GET /v1/bridges/networks` |
| `list_bridges(hub_id=None, *, owner_id=None, limit=None)` | `GET /v1/bridges` |
| `get_bridge(bridge_id)` | `GET /v1/bridges/{id}` |
| `create_bridge(hub_id, connector, *, token=None, secrets=None, settings=None)` | `POST /v1/bridges` |
| `update_bridge(bridge_id, *, allowed, require_mention, session_scope, paused, settings, etag)` | `PATCH /v1/bridges/{id}` (`If-Match`) |
| `replace_bridge_token(bridge_id, token)` | `PUT /v1/bridges/{id}/token` |
| `replace_bridge_secrets(bridge_id, secrets)` | `PUT /v1/bridges/{id}/secrets` |
| `list_bridge_chats(bridge_id)` | `GET /v1/bridges/{id}/chats` |
| `send_bridge_test_message(bridge_id, chat_id)` | `POST /v1/bridges/{id}/test` |
| `get_bridge_avatar(bridge_id)` | `GET /v1/bridges/{id}/avatar` |
| `delete_bridge(bridge_id)` | `DELETE /v1/bridges/{id}` |

Models and `SecretValue` live in `src/thalovant/chat_apps.py`; the guide is `docs/guides/chat-apps.md`.
