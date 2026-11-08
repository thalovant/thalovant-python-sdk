# Repository instructions

This repository owns the published Python client and agent SDK for supported Thalovant public API and HiveMind runtime contracts. Read the platform contracts in `../infra-manifests/docs/thalovant-platform/` when available.

Rules:

- Preserve compatibility with the documented Python and Thalovant API support window.
- Update models, implementation, examples, tests, changelog, version, and public documentation together for observable contract changes.
- Consume additive server behavior only after compatible server support exists.
- Never publish credentials, registry tokens, identity files, or generated secrets.
- Do not create a release for internal platform changes with no Python SDK impact; record `no SDK impact` in the coordinated change instead.
- Validate the built distribution and an install from PyPI before declaring a release complete.
- Update affected `docs.thalovant.com` SDK pages in the same release train.
- Sign-in: keep device login (`login_with_browser`, `begin_device_login`, `poll_device_login`) unchanged; `interactive_login()` prefers browser sign-in and falls back to device login only before the browser is opened. Never log or raise the authorization code, the PKCE verifier, the state or a token. `contracts/conformance/browser-login-vectors.json` is the shared contract (`docs/browser-login-porting.md`).

Validate with `python -m pip install -e ".[dev]"`, `python -m pytest -q`, `python -m build`, and `python -m twine check dist/*`. A published release also requires a clean virtual-environment install of `thalovant==<version>` from PyPI and a basic import smoke test.

Rollback by publishing a corrected patch release; PyPI releases are immutable. Yank a broken release only when the package owner has confirmed the compatibility impact.
