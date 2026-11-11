Last Edit: Claude Sonnet 5.5 - 2026-11-11 - Motive: Intercom (0.12.0): announce, speakers and settings on the control plane.

# Suggestions: thalovant (Python SDK)

## Shared intercom vectors

- **Problem:** the `intercom` capability has no conformance vectors; each SDK proves it with its own loopback tests, so text rules (control characters, trimming) can drift.
- **Proposal:** a `intercom-vectors.json` of text/target/repeat validation cases and the error-kind mapping, claimed by the capability.
- **Impact:** low effort; adds one file to every consumer's obligations.

## Carry `ai_disclosure` on `ApiToken`

- **Problem:** `NativeTokenResponse` and `DeviceTokenResponse` both carry `ai_disclosure`, the AI disclosure an app shows at first contact. `ApiToken.from_dict` (`src/thalovant/control.py`) keeps five members and drops it. `browser_login()` and `login_with_browser()` return the raw answer, where it survives. `poll_device_login()` returns an `ApiToken`, where it does not. The 0.11.0 browser-login round left `ApiToken` alone so that device login stayed unchanged.
- **Proposal:** add `ai_disclosure: str | None = None` to `ApiToken` (omitted from `as_dict()` when `None`) and a case to the device-login and browser-login vectors, in a coordinated round.
- **Impact:** low. It touches the reference digest and every consumer's token type.

## A `thalovant login` command with a credential store

- **Problem:** the CLI reads `THALOVANT_API_TOKEN` or `--token` (`src/thalovant/cli.py` `_control_plane_from_args`) and has no way to sign in. `interactive_login()` is the right engine for one, but the SDK has nowhere to keep the token. Printing it for the caller to export would put a credential on a terminal and in shell history.
- **Proposal:** `thalovant login [--browser | --device] [--scope ...]`, which keeps the token in the platform keychain (or a `0600` file under `~/.config/thalovant/`) with its `token_id`, and `thalovant logout`, which revokes it. Then have the `skills` commands read the stored token when neither variable nor option is given.
- **Impact:** medium. It needs an owner decision on where a CLI credential lives. It would also be the "combined CLI entry" the browser sign-in contract describes, which only the MCP server has today.

## A typed "browser sign-in unavailable" error

- **Problem:** `browser_login()` raises a plain `ThalovantAPIError` with status 404 when the API does not offer browser sign-in. A caller that wants its own fallback has to read `status_code`. `interactive_login()` already does that fallback.
- **Proposal:** a `ThalovantBrowserLoginUnavailable(ThalovantAPIError)` subclass, raised for the 404 and for an unusable configuration.
- **Impact:** low. The contract deliberately added no error types this round.


## Compare the port when a native sign-in callback is matched to its redirect URI

- **Problem:** `NativeSignIn.code_from` (`src/thalovant/native_auth.py`) compares the callback's scheme, host and path with `redirect_uri`, but not its port. The Node SDK's `sameTarget()` (`src/native-auth.ts`) compares `host`, which includes the port. A loopback callback that reached `http://127.0.0.1:9999/callback` is read for a sign-in that asked for `http://127.0.0.1:53682/callback`. Callback parsing only checks where the answer arrived and that it carries this attempt's state. It does not prove where the code came from. Anyone who has the state can build a matching URL with the right port as easily as with a wrong one, so the port check guards against a misrouted callback, not a forged one. A code issued to another attempt fails at the token exchange instead, because `complete_native_sign_in` sends this attempt's PKCE verifier. The gap is between two SDKs that should behave the same, and between the code and CodeRabbit's "scheme, authority and path" on #78.
- **Proposal:** compare `parts.port` as well, and add the shared case to a native sign-in vector file, in the next coordinated reference change. It changes `src/`, so it moves the reference digest and cuts a release.
- **Impact:** low. A tighter match on a path that is already state-bound, and one less parity difference.

## Give `GET /v1/bridges` a cursor

- **Problem:** one answer holds at most 500 bridges and there is no cursor (`list_bridges`, `src/thalovant/control.py`).
- **Proposal:** add `cursor`/`next_cursor` to the API route, then an iterator in the SDK.
- **Impact:** low today (owners have a handful of bridges); matters for platform-wide admin tooling.

## Codes on the 412 and the deactivated-owner 403

- **Problem:** a stale `etag` and a deactivated owner's account answer without a `code`, unlike every other chat apps refusal.
- **Proposal:** `code: "etag_mismatch"` and `code: "account_deactivated"` in the API; the SDK needs no change to read them.
- **Impact:** callers could branch on `code` everywhere instead of on status and prose.

## A shared vector file for chat apps

- **Problem:** the `chat-apps` capability has no conformance vectors, so each port's parsing (unknown values, required members, `fields`) is checked only by its own tests.
- **Proposal:** add `contracts/conformance/chat-apps-vectors.json` (payload in, model or refusal out) and have each consumer run it.
- **Impact:** medium: makes drift between the five implementations visible in CI.
