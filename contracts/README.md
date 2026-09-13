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

PR, main-branch, and release workflows run the coordination gate. Publishing
jobs depend on its success. Every six hours a fresh environment resolves the
Python dependencies and executes committed public behavior vectors, so an
upstream language-package change is visible even without a Python source edit.

`python scripts/generate-sdk-conformance.py --check` executes the committed
question/inventory cases. Without `--check`, it regenerates candidate vectors
for review. Copy reviewed vectors to each managed SDK's fixture directory and
run each language's native suite. Regeneration never approves consumer evidence.
The reference snapshot includes the vectors, so a changed expected result creates
a new consumer obligation.

For a feature rollout, keep both the current and proposed reference digests in
consumer records, merge and publish compatible consumers first, and activate the
producer only after its full cross-repository gate passes. SDK-specific protocol
limits remain explicit; C retains caller-owned buffers/networking and MCP keeps
per-tool identity leases. Never invent a feature solely to fill a matrix cell.

Hashes establish review obligations; each language's normal CI and executable
reference cases still have to prove behavior. Never refresh hashes merely to
make a failing check green. Rust's private unit suites may be colocated with the
implementation: record the complete tested module as test evidence alongside
its public exports/delegates, so changes to either invalidate acceptance.
