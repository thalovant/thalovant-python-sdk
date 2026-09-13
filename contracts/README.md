# SDK parity contract

Python is the reference implementation for shared SDK behavior. The versioned
`sdk-parity.json` snapshot covers every Python implementation module and runtime
requirement, including private helpers. New methods, argument defaults, model
fields, behavior changes, dependencies and newly added modules change the
snapshot. Comments, docstrings, release versions and developer tooling do not.

Run `python scripts/check-sdk-parity.py` with Python 3.11+ before opening a PR.
The pytest suite runs this check on supported interpreters with `tomllib`.
An unreviewed change fails with the paths that created the parity obligation.
`--snapshot` prints a candidate snapshot; it does not update any acceptance.

Each capability names all eight SDKs and MCP. Required consumers must implement
and test the applicable behavior. An exclusion must explain an established
platform boundary (for example, caller-owned C networking or MCP's per-tool
connection lifetime). A feature cannot disappear behind a missing matrix row.

The checker also supports `--workspace <sibling-checkout-root>`. This requires
all eight downstream repositories to acknowledge the current reference digest
and bind each applicable capability to implementation and regression-test file
hashes. A missing consumer, stale digest, removed test or changed evidence fails
closed. A consumer can accept both the current and candidate digests during a
coordinated additive rollout; merge compatible consumers before the producer.

This initial change installs the producer guard. Consumer records and the
cross-repository PR/release workflow are being introduced in the coordinated
0.7 feature rollout. The standalone guard does not yet claim fleet parity.
Hashes establish review obligations; each language's normal CI and executable
reference cases still have to prove behavior. Never refresh hashes merely to
make a failing check green.
