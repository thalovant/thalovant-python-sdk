"""The CI coordination gate must reject silent drift and unbound evidence."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

pytest.importorskip("tomllib", reason="The standalone parity CI job uses Python 3.13")
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sdk_parity", ROOT / "scripts/check-sdk-parity.py")
parity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parity)


def test_committed_reference_is_current():
    assert parity.check(ROOT) == []


@pytest.fixture
def reference(tmp_path):
    for name in ("src", "contracts"):
        shutil.copytree(ROOT / name, tmp_path / name, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "pyproject.toml", tmp_path)
    return tmp_path


@pytest.mark.parametrize("change", ["new_api", "private_behavior", "new_module", "dependency"])
def test_unacknowledged_reference_changes_fail(reference, change):
    if change == "new_module":
        (reference / "src/thalovant/new_feature.py").write_text("ENABLED = True\n")
    elif change == "dependency":
        path = reference / "pyproject.toml"
        path.write_text(path.read_text().replace('requests>=2.33.0', 'requests>=2.34.0'))
    else:
        path = reference / "src/thalovant/session.py"
        path.write_text(path.read_text() + ("\ndef new_api(value): return value\n" if change == "new_api" else "\ndef _private(): return 123\n"))
    assert "Unreviewed Python SDK change" in parity.check(reference)[0]


def test_comments_and_release_versions_do_not_invent_obligations(reference):
    path = reference / "src/thalovant/session.py"
    path.write_text(path.read_text() + "\n# Review explanation\n")
    path = reference / "src/thalovant/_version.py"
    path.write_text('__version__ = "99.0.0"\n')
    assert parity.check(reference) == []


def test_missing_consumers_fail_closed(reference, tmp_path):
    errors = parity.check(reference, tmp_path / "empty")
    assert len(errors) == 8
    assert all(any(repo in error for error in errors) for repo in parity.REPOSITORIES[1:])


def test_new_capability_requires_every_consumer_and_explicit_scope(reference):
    path = reference / parity.MANIFEST
    manifest = json.loads(path.read_text())
    del manifest["capabilities"]["session"]["scope"]["thalovant-mcp"]
    path.write_text(json.dumps(manifest))
    assert "all eight SDKs and MCP" in parity.check(reference)[0]


def test_consumer_evidence_is_bound_to_reference_and_actual_files(tmp_path):
    contract = {"reference": {"revision": 1}, "capabilities": {"feature": {"scope": {"consumer": {"status": "required"}}}}}
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/code.txt").write_text("implementation")
    (tmp_path / "tests/test.txt").write_text("regression")
    acceptance = {"schema_version": 1, "reference_digests": [parity.digest(contract["reference"])],
                  "capabilities": {"feature": {"status": "required", "reason": "Ported and tested", "implementation": {"src/code.txt": parity.file_hash(tmp_path, "src/code.txt")}, "tests": {"tests/test.txt": parity.file_hash(tmp_path, "tests/test.txt")}}}}
    path = tmp_path / parity.MANIFEST
    path.parent.mkdir()
    path.write_text(json.dumps(acceptance))
    parity.validate_consumer(contract, tmp_path, "consumer")
    changed = copy.deepcopy(contract)
    changed["reference"]["revision"] = 2
    with pytest.raises(ValueError, match="reference changed"):
        parity.validate_consumer(changed, tmp_path, "consumer")
    (tmp_path / "tests/test.txt").write_text("disabled")
    with pytest.raises(ValueError, match="tests changed"):
        parity.validate_consumer(contract, tmp_path, "consumer")


def test_evidence_cannot_escape_checkout(tmp_path):
    with pytest.raises(ValueError):
        parity.file_hash(tmp_path, "../outside.txt")
    (tmp_path / "link").symlink_to(ROOT / "pyproject.toml")
    with pytest.raises(ValueError):
        parity.file_hash(tmp_path, "link")
