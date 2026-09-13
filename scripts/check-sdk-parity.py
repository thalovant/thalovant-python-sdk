#!/usr/bin/env python3
"""Fail closed on unreviewed SDK reference or consumer drift (stdlib only).

The reference snapshot records normalized Python implementation ASTs, public
signatures and runtime dependencies. Comments/docstrings/version bumps alone do
not create a parity obligation. All other changes, including new private helper
modules, do. Consumer acknowledgments are bound to that snapshot and concrete
source/test content; normal language CI must execute the named regression suites.
This is a coordination gate, not a claim that a hash proves semantic equivalence.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import sys
import tomllib

REPOSITORIES = (
    "thalovant-python-sdk", "thalovant-node-sdk", "thalovant-go-sdk",
    "thalovant-rust-sdk", "thalovant-kotlin-sdk", "thalovant-dotnet-sdk",
    "thalovant-swift-sdk", "thalovant-embedded-c", "thalovant-mcp",
)
MANIFEST = Path("contracts/sdk-parity.json")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


class WithoutDocumentation(ast.NodeTransformer):
    def visit_Module(self, node):
        return self.clean(node)

    def visit_ClassDef(self, node):
        return self.clean(node)

    def visit_FunctionDef(self, node):
        return self.clean(node)

    def visit_AsyncFunctionDef(self, node):
        return self.clean(node)

    def clean(self, node):
        self.generic_visit(node)
        if node.body and isinstance(node.body[0], ast.Expr):
            value = node.body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                node.body.pop(0)
        return node


def normalized_tree(value):
    """Stable across AST field additions and ast.dump formatting versions."""
    if isinstance(value, ast.AST):
        return {"node": type(value).__name__, "fields": {
            key: normalized_tree(item) for key, item in ast.iter_fields(value)
            if item is not None and item != []
        }}
    if isinstance(value, list):
        return [normalized_tree(item) for item in value]
    if isinstance(value, (bytes, complex)) or value is Ellipsis:
        return {"literal_type": type(value).__name__, "value": repr(value)}
    return value


def snapshot(reference):
    files = {}
    for path in sorted((reference / "src/thalovant").rglob("*.py")):
        if path.name == "_version.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        normalized = normalized_tree(WithoutDocumentation().visit(tree))
        files[str(path.relative_to(reference))] = digest(normalized)
    if not files:
        raise ValueError("Python source tree is missing")
    project = tomllib.loads((reference / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    # Developer/docs/publisher tooling has no runtime semantic impact.
    dependencies = {"requires-python": project["requires-python"],
                    "dependencies": sorted(project.get("dependencies", [])),
                    "extras": {k: sorted(v) for k, v in project.get("optional-dependencies", {}).items()
                               if k not in {"dev", "docs", "publish"}}}
    return {"files": files, "dependencies": dependencies}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def file_hash(root, name):
    path = root / name
    if not isinstance(name, str) or not name or path.is_symlink():
        raise ValueError("Evidence must be a regular repository file")
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"Missing or escaping evidence file: {name}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_reference(reference):
    manifest = read(reference / MANIFEST)
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported SDK parity schema")
    actual = snapshot(reference)
    if manifest.get("reference") != actual:
        before = manifest.get("reference", {}).get("files", {})
        changed = sorted(k for k in before.keys() | actual["files"].keys()
                         if before.get(k) != actual["files"].get(k))
        if manifest.get("reference", {}).get("dependencies") != actual["dependencies"]:
            changed.append("pyproject.toml runtime requirements")
        raise ValueError("Unreviewed Python SDK change: " + ", ".join(changed) +
                         ". Update the reference contract and every consumer impact record; "
                         "port applicable behavior and run its conformance tests before release.")
    covered = []
    capabilities = manifest.get("capabilities", {})
    if not capabilities:
        raise ValueError("No SDK capabilities declared")
    for name, capability in capabilities.items():
        if not capability.get("description") or not capability.get("files"):
            raise ValueError(f"Incomplete capability: {name}")
        covered.extend(capability["files"])
        scopes = capability.get("scope", {})
        if set(scopes) != set(REPOSITORIES):
            raise ValueError(f"Capability {name} must address all eight SDKs and MCP")
        for repo, scope in scopes.items():
            if scope.get("status") not in {"required", "not-applicable"}:
                raise ValueError(f"Invalid scope for {name}/{repo}")
            if scope["status"] == "not-applicable" and not scope.get("reason", "").strip():
                raise ValueError(f"Missing scope explanation for {name}/{repo}")
    if sorted(covered) != sorted(actual["files"]):
        raise ValueError("Every Python implementation file must belong to exactly one capability")
    return manifest


def validate_consumer(reference_manifest, root, repo):
    acceptance = read(root / MANIFEST)
    expected = digest(reference_manifest["reference"])
    if acceptance.get("schema_version") != 1 or expected not in acceptance.get("reference_digests", []):
        raise ValueError(f"{repo}: reference changed; port or explicitly review every capability")
    capabilities = reference_manifest["capabilities"]
    entries = acceptance.get("capabilities", {})
    if set(entries) != set(capabilities):
        raise ValueError(f"{repo}: missing or unknown capability acceptance")
    for name, capability in capabilities.items():
        entry = entries[name]
        scope = capability["scope"][repo]
        if entry.get("status") != scope["status"] or not entry.get("reason", "").strip():
            raise ValueError(f"{repo}/{name}: incorrect scope or missing review explanation")
        if scope["status"] == "not-applicable":
            continue
        for kind in ("implementation", "tests"):
            evidence = entry.get(kind, {})
            if not evidence:
                raise ValueError(f"{repo}/{name}: missing {kind} evidence")
            for path, expected_hash in evidence.items():
                if file_hash(root, path) != expected_hash:
                    raise ValueError(f"{repo}/{name}: {kind} changed: {path}; rerun conformance and refresh evidence")
            if kind == "tests" and set(evidence) & set(entry.get("implementation", {})):
                raise ValueError(f"{repo}/{name}: tests must be separate from implementation")


def check(reference, workspace=None, consumer=None):
    errors = []
    try:
        manifest = validate_reference(reference)
    except (OSError, ValueError, KeyError, TypeError, SyntaxError) as error:
        return [str(error)]
    if workspace is not None:
        for repo in ([consumer] if consumer else REPOSITORIES):
            # The producer has its own frozen reference plus executable tests.
            if repo == "thalovant-python-sdk":
                continue
            try:
                validate_consumer(manifest, workspace / repo, repo)
            except (OSError, ValueError, KeyError, TypeError) as error:
                errors.append(f"{repo}: {error}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--workspace", type=Path,
                        help="Require acceptance from all consumers; omitted only for local producer checks")
    parser.add_argument("--consumer", choices=REPOSITORIES[1:], help="Validate one consumer during additive rollout; producer/release coordination uses the whole workspace")
    parser.add_argument("--snapshot", action="store_true", help="Print candidate snapshot; never updates acceptance")
    args = parser.parse_args()
    if args.snapshot:
        print(json.dumps(snapshot(args.reference), indent=2, sort_keys=True))
        return 0
    if args.consumer and args.workspace is None:
        parser.error("--consumer requires --workspace")
    errors = check(args.reference, args.workspace, args.consumer)
    for error in errors:
        print("SDK parity: " + error, file=sys.stderr)
    if errors:
        return 1
    print("SDK parity reference" + (" and " + (args.consumer or "all consumers") + " acceptance records" if args.workspace else "") + " verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
