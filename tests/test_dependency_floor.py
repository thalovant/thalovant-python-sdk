"""The declared dependency floors are the versions CI actually tests.

The 3.10 CI job installs the floors and runs the whole suite on them, so a
floor is a tested version rather than a guess. These checks keep the three
places in step: the floors in pyproject.toml, the pins the CI job installs,
and, inside that job (``THALOVANT_MINIMUM_VERSIONS=1``), what is really
installed. The cryptography floor also stays low enough for Home Assistant
2026.9, which pins cryptography==48.0.1.
"""

from __future__ import annotations

import os
import re
from importlib.metadata import version
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOME_ASSISTANT_STABLE_CRYPTOGRAPHY = "48.0.1"


def _floors() -> dict[str, str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = text[text.index("dependencies = [") : text.index("]", text.index("dependencies = ["))]
    floors: dict[str, set[str]] = {}
    for name, floor in re.findall(r'^\s*"([A-Za-z0-9_.-]+)>=([0-9][0-9.]*)', block, re.MULTILINE):
        floors.setdefault(name.lower(), set()).add(floor)
    for name, found in floors.items():
        assert len(found) == 1, f"{name} has a different floor per platform: {sorted(found)}"
    return {name: found.pop() for name, found in floors.items()}


def _ci_pins() -> dict[str, str]:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    step = workflow[workflow.index("Exercise the minimum supported") :]
    line = next(row for row in step.splitlines() if "pip install" in row)
    return {name.lower(): pin for name, pin in re.findall(r"'([A-Za-z0-9_.-]+)==([0-9.]+)'", line)}


def _parts(value: str) -> tuple[int, ...]:
    """A release as numbers, trailing zeros dropped: 3.11 and 3.11.0 are one version."""
    parts = [int(part) for part in value.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def test_the_ci_minimum_job_installs_exactly_the_declared_floors():
    floors = _floors()
    assert set(floors) == {"aiohttp", "cryptography"}
    pins = _ci_pins()
    assert {name: _parts(pin) for name, pin in pins.items()} == {
        name: _parts(floor) for name, floor in floors.items()
    }


def test_the_cryptography_floor_admits_home_assistant_stable():
    assert _parts(_floors()["cryptography"]) <= _parts(HOME_ASSISTANT_STABLE_CRYPTOGRAPHY)


@pytest.mark.skipif(
    os.environ.get("THALOVANT_MINIMUM_VERSIONS") != "1",
    reason="only inside the CI job that installs the floors",
)
def test_this_run_is_really_on_the_floors():
    for name, floor in _floors().items():
        assert _parts(version(name)) == _parts(floor), f"{name} {version(name)} is not the floor {floor}"
