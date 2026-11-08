Last Edit: Claude Opus 5.5 - 2026-11-08 - Motive: Note the port in native sign-in's redirect check, seen while re-checking the #78 review item.

# Suggestions: thalovant (Python SDK)

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
