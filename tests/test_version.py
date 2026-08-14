"""Pin the user agents to the package version.

Mirrors the Rust SDK's ``user_agents_match_crate_version``. Shipped 0.4.20
through 0.4.22 sent a stale data-plane user agent because the constant was
hand-maintained; these tests fail loudly if a version literal ever creeps back
into a user-agent string.
"""

from pathlib import Path
import re

import pytest

import thalovant
from thalovant import ThalovantControlPlane, __version__
from thalovant._version import USER_AGENT, USER_AGENT_PRODUCT
from thalovant.client import DEFAULT_USERAGENT
from thalovant.control import DEFAULT_CONTROL_USER_AGENT


def test_user_agents_match_package_version():
    expected = f"ThalovantPythonSDK/{__version__}"

    assert USER_AGENT == expected
    assert DEFAULT_USERAGENT == expected
    assert DEFAULT_CONTROL_USER_AGENT == expected
    assert ThalovantControlPlane().user_agent == expected


def test_user_agent_constants_are_the_same_object():
    # Both surfaces deliberately share one product token and one version, so a
    # future bump cannot move one without the other.
    assert DEFAULT_USERAGENT is USER_AGENT
    assert DEFAULT_CONTROL_USER_AGENT is USER_AGENT
    assert USER_AGENT.startswith(f"{USER_AGENT_PRODUCT}/")


def test_no_source_file_hard_codes_a_user_agent_version():
    package_root = Path(thalovant.__file__).resolve().parent
    hard_coded = re.compile(rf"{USER_AGENT_PRODUCT}/\d")

    offenders = sorted(
        module.name
        for module in package_root.glob("*.py")
        if hard_coded.search(module.read_text(encoding="utf-8"))
    )

    assert offenders == [], (
        "user agents must be derived from thalovant._version.USER_AGENT, "
        f"but a pinned version literal was found in: {', '.join(offenders)}"
    )


def test_pyproject_version_matches_package_version():
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.exists():
        pytest.skip("pyproject.toml is unavailable outside the repository checkout")

    declared = re.search(
        r'^version = "([^"]+)"', pyproject.read_text(encoding="utf-8"), re.MULTILINE
    )

    assert declared is not None, "pyproject.toml does not declare a project version"
    assert declared.group(1) == __version__
