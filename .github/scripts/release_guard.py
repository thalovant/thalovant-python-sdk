#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tomllib


def run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def output_git(*args: str, check: bool = True) -> str:
    return run_git(*args, check=check).stdout.strip()


def version_path(kind: str) -> str:
    return {
        "python": "pyproject.toml",
        "node": "package.json",
        "rust": "Cargo.toml",
        "go": "VERSION",
    }[kind]


def parse_version(kind: str, content: str) -> str:
    if kind == "python":
        return tomllib.loads(content)["project"]["version"]
    if kind == "node":
        return json.loads(content)["version"]
    if kind == "rust":
        match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', content)
        if not match:
            raise ValueError("Cargo.toml does not contain a package version")
        return match.group(1)
    if kind == "go":
        return content.strip().removeprefix("v")
    raise ValueError(f"Unsupported package kind: {kind}")


def current_version(kind: str) -> str:
    return parse_version(kind, pathlib.Path(version_path(kind)).read_text())


def version_at(kind: str, ref: str) -> str | None:
    result = run_git("show", f"{ref}:{version_path(kind)}", check=False)
    if result.returncode != 0:
        return None
    return parse_version(kind, result.stdout)


def latest_version_tag() -> str | None:
    result = run_git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def tag_exists(tag: str) -> bool:
    return run_git("rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False).returncode == 0


def changed_files(base: str, head: str, triple_dot: bool) -> list[str]:
    separator = "..." if triple_dot else ".."
    diff_range = f"{base}{separator}{head}"
    return [
        line.strip()
        for line in output_git("diff", "--name-only", diff_range).splitlines()
        if line.strip()
    ]


def relevant_files(files: list[str], patterns: list[str]) -> list[str]:
    matches: list[str] = []
    normalized = [pattern.strip().rstrip("/") for pattern in patterns if pattern.strip()]
    for file_path in files:
        for pattern in normalized:
            if file_path == pattern or file_path.startswith(f"{pattern}/"):
                matches.append(file_path)
                break
    return matches


def main() -> int:
    package_kind = os.environ["PACKAGE_KIND"]
    relevant_patterns = os.environ["RELEASE_RELEVANT_PATHS"].splitlines()
    head = os.environ.get("GITHUB_SHA", "HEAD")
    base_sha = os.environ.get("BASE_REF_SHA", "")
    version = current_version(package_kind)
    current_tag = f"v{version}"

    if base_sha:
        files = changed_files(base_sha, head, triple_dot=True)
        relevant = relevant_files(files, relevant_patterns)
        if not relevant:
            print("No release-relevant SDK files changed.")
            return 0

        base_version = version_at(package_kind, base_sha)
        if base_version == version:
            print("Release-relevant files changed without a version bump:")
            for file_path in relevant:
                print(f"  - {file_path}")
            print(f"Current version is still {version}. Bump {version_path(package_kind)}.")
            return 1

        print(f"Release-relevant changes include a version bump: {base_version} -> {version}.")
        return 0

    latest_tag = latest_version_tag()
    if latest_tag is None:
        print("No version tag exists yet; release guard has no published baseline.")
        return 0

    files = changed_files(latest_tag, head, triple_dot=False)
    relevant = relevant_files(files, relevant_patterns)
    if not relevant:
        print(f"No release-relevant SDK files changed since {latest_tag}.")
        return 0

    if tag_exists(current_tag):
        if relevant == [version_path(package_kind)] and current_tag == latest_tag:
            print(f"{version_path(package_kind)} establishes the existing {current_tag} baseline.")
            return 0

        print(f"Release-relevant files changed since {latest_tag}, but {current_tag} already exists:")
        for file_path in relevant:
            print(f"  - {file_path}")
        print(f"Bump {version_path(package_kind)} so auto-release can publish a new version.")
        return 1

    print(f"Release-relevant changes will be published as {current_tag}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
