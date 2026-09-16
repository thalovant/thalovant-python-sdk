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
    # The tests a capability names are part of the reference now: it owes the
    # same "something here actually runs these vectors" evidence it asks of
    # every consumer, so a fixture without them is not a reference.
    (tmp_path / "tests").mkdir(exist_ok=True)
    for capability in json.loads((ROOT / parity.MANIFEST).read_text())["capabilities"].values():
        for name in capability.get("tests", []):
            shutil.copy(ROOT / name, tmp_path / name)
    return tmp_path


@pytest.mark.parametrize("change", ["new_api", "private_behavior", "new_module", "dependency", "conformance"])
def test_unacknowledged_reference_changes_fail(reference, change):
    if change == "conformance":
        (reference / "contracts/conformance/question-vectors.json").write_text('{"cases": []}')
    elif change == "new_module":
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
    parity.validate_consumer(contract, tmp_path, "consumer", [])
    # A digest this reference has never published is not a rollout state.
    changed = copy.deepcopy(contract)
    changed["reference"]["revision"] = 2
    with pytest.raises(ValueError, match="never published|no reference"):
        parity.validate_consumer(changed, tmp_path, "consumer", [])
    # One it has published is: reported on every run, refused at release.
    rolling = copy.deepcopy(changed)
    rolling["reference_history"] = [parity.digest(contract["reference"])]
    planned = []
    parity.validate_consumer(rolling, tmp_path, "consumer", planned)
    assert planned and "one reference behind" in planned[0]
    (tmp_path / "tests/test.txt").write_text("disabled")
    with pytest.raises(ValueError, match="tests changed"):
        parity.validate_consumer(contract, tmp_path, "consumer", [])


def test_evidence_cannot_escape_checkout(tmp_path):
    with pytest.raises(ValueError):
        parity.file_hash(tmp_path, "../outside.txt")
    (tmp_path / "link").symlink_to(ROOT / "pyproject.toml")
    with pytest.raises(ValueError):
        parity.file_hash(tmp_path, "link")


def test_empty_version_specific_ast_fields_do_not_change_digest():
    import ast
    first = ast.parse("def example(): return b'bytes'")
    second = ast.parse("def example(): return b'bytes'")
    second.body[0]._fields = (*second.body[0]._fields, "future_optional_field")
    second.body[0].future_optional_field = []
    assert parity.normalized_tree(first) == parity.normalized_tree(second)



def test_snapshot_paths_are_portable_to_windows(monkeypatch):
    from pathlib import PureWindowsPath
    original = Path.relative_to
    def windows_relative(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        return PureWindowsPath(*result.parts)
    monkeypatch.setattr(Path, "relative_to", windows_relative)
    assert parity.snapshot(ROOT) == json.loads((ROOT / parity.MANIFEST).read_text())["reference"]


def test_evidence_hashes_ignore_checkout_line_endings(tmp_path):
    path = tmp_path / "source.txt"
    path.write_bytes(b"one\ntwo\n")
    expected = parity.file_hash(tmp_path, "source.txt")
    path.write_bytes(b"one\r\ntwo\r\n")
    assert parity.file_hash(tmp_path, "source.txt") == expected


def test_a_capability_must_name_a_reference_test_that_runs_its_vectors(reference):
    """The obligation used to point outward only.

    Consumers had to prove a test read each vector; the reference did not, and
    shipped `binary` answering two of the six payload types its own vectors
    name with every gate green.
    """

    path = reference / parity.MANIFEST
    manifest = json.loads(path.read_text())
    del manifest["capabilities"]["binary"]["tests"]
    path.write_text(json.dumps(manifest))
    assert "no test that runs them" in parity.check(reference)[0]


def test_a_reference_test_that_never_mentions_the_vectors_is_not_evidence(reference):
    path = reference / "tests/test_conformance_vectors.py"
    path.write_text("def test_nothing():\n    pass\n")
    assert "no reference test reads" in parity.check(reference)[0]


def test_a_vector_file_no_capability_claims_is_refused(reference):
    # Snapshot it first, so this is the orphan rule talking and not the
    # unreviewed-change rule that fires on any vector edit.
    (reference / "contracts/conformance/orphan-vectors.json").write_text('{"cases": []}\n')
    path = reference / parity.MANIFEST
    manifest = json.loads(path.read_text())
    manifest["reference"] = parity.snapshot(reference)
    path.write_text(json.dumps(manifest))
    errors = parity.check(reference)
    assert any("belong to no capability" in error for error in errors), errors


def test_release_mode_refuses_a_partial_view(reference, tmp_path):
    assert parity.check(reference, release=True) == ["--release must check the whole consumer workspace"]
    assert parity.check(reference, tmp_path / "ws", consumer="thalovant-mcp", release=True) == [
        "--release must check the whole consumer workspace"]


@pytest.fixture
def consumer(tmp_path):
    """A minimal consumer whose capability is pinned to a shared vector file."""

    vectors = {"cases": [{"name": "only", "expected": True}]}
    contract = {"reference": {"revision": 1, "conformance": {"shared-vectors.json": parity.digest(vectors)}},
                "capabilities": {"feature": {"vectors": ["shared-vectors.json"],
                                             "scope": {"consumer": {"status": "required"}}}}}
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "testdata").mkdir()
    (tmp_path / "src/code.txt").write_text("implementation")
    # A loader call, not prose: naming the vector in a comment is no longer
    # evidence that a test runs it.
    (tmp_path / "tests/test.txt").write_text('for case in load("shared-vectors.json"): run(case)')
    (tmp_path / "testdata/shared-vectors.json").write_text(json.dumps(vectors, indent=2))
    acceptance = {"schema_version": 1, "reference_digests": [parity.digest(contract["reference"])],
                  "capabilities": {"feature": {
                      "status": "required", "reason": "Ported and tested",
                      "vectors": {"shared-vectors.json": "testdata/shared-vectors.json"},
                      "implementation": {"src/code.txt": parity.file_hash(tmp_path, "src/code.txt")},
                      "tests": {"tests/test.txt": parity.file_hash(tmp_path, "tests/test.txt")}}}}
    path = tmp_path / parity.MANIFEST
    path.parent.mkdir()
    path.write_text(json.dumps(acceptance))
    return contract, tmp_path


def test_a_consumer_running_the_shared_vectors_passes(consumer):
    contract, root = consumer
    parity.validate_consumer(contract, root, "consumer", [])


def test_a_consumer_that_predates_the_shared_vectors_is_behind_not_broken(consumer):
    contract, root = consumer
    path = root / parity.MANIFEST
    acceptance = json.loads(path.read_text())
    del acceptance["capabilities"]["feature"]["vectors"]
    path.write_text(json.dumps(acceptance))
    planned = []
    parity.validate_consumer(contract, root, "consumer", planned)
    assert planned and "not yet re-reviewed against the shared vectors" in planned[0]


def test_a_consumer_that_has_adopted_the_format_owes_every_vector(consumer):
    contract, root = consumer
    contract["capabilities"]["feature"]["vectors"].append("second-vectors.json")
    contract["reference"]["conformance"]["second-vectors.json"] = parity.digest({})
    path = root / parity.MANIFEST
    acceptance = json.loads(path.read_text())
    acceptance["reference_digests"] = [parity.digest(contract["reference"])]
    path.write_text(json.dumps(acceptance))
    with pytest.raises(ValueError, match="does not say where"):
        parity.validate_consumer(contract, root, "consumer", [])


def test_a_consumer_cannot_run_its_own_edited_copy_of_the_vectors(consumer):
    contract, root = consumer
    (root / "testdata/shared-vectors.json").write_text('{"cases": []}')
    with pytest.raises(ValueError, match="is not the reference's"):
        parity.validate_consumer(contract, root, "consumer", [])


def test_naming_a_test_that_never_reads_the_vectors_is_not_enough(consumer):
    contract, root = consumer
    (root / "tests/test.txt").write_text("nothing to see")
    path = root / parity.MANIFEST
    acceptance = json.loads(path.read_text())
    acceptance["capabilities"]["feature"]["tests"] = {"tests/test.txt": parity.file_hash(root, "tests/test.txt")}
    path.write_text(json.dumps(acceptance))
    with pytest.raises(ValueError, match="no test names"):
        parity.validate_consumer(contract, root, "consumer", [])


def test_only_a_name_reaching_a_call_is_evidence():
    """The check said a test "has to be run against them, not merely declared"
    while accepting the name anywhere in the file.

    Tightening it to "inside a quoted string" was not enough either: a trailing
    `# "binary-vectors"` is a quoted string to anything that strips only
    whole-line comments, and so is an unused constant. It scans the source now
    and asks where the literal goes.

    It still cannot prove execution; only a recorded conformance result could,
    and consumers do not produce one yet.
    """
    names_vector = parity.names_vector
    for text in (
        "// see binary-vectors.json for the cases",
        "# binary-vectors.json describes these",
        "/* binary-vectors.json */",
        '/* a\n   "binary-vectors"\n */',
        'assert ok  # "binary-vectors"',
        'assert ok  // "binary-vectors"',
        'const unused = "binary-vectors"; assert(1);',
    ):
        assert not names_vector(text, "binary-vectors"), text

    for text in (
        'const v = load("binary-vectors.json");',
        'let v = include_str!("../contracts/conformance/binary-vectors.json");',
        'vectors("binary-vectors")',
        'Bundle.module.url(forResource: "binary-vectors", withExtension: "json")',
        'read(path, "binary-vectors.json")',
        # A URL's // must not be mistaken for a comment and eat the rest.
        'let u = "https://example.com//x"; load("binary-vectors.json")',
    ):
        assert names_vector(text, "binary-vectors"), text
