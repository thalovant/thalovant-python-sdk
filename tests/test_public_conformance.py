"""Keep committed cross-language expectations bound to executable Python behavior."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "contracts/conformance"
# Named here rather than discovered, because the parity contract asks which test
# reads each vector file and a glob answers with nothing it can check.
GENERATED = ("question-vectors.json", "inventory-vectors.json", "reply-claim-vectors.json",
             "binary-frames.json")


def conformance():
    spec = importlib.util.spec_from_file_location(
        "conformance", ROOT / "scripts/generate-sdk-conformance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", GENERATED)
def test_each_generated_vector_file_still_describes_this_SDK(name):
    import json

    module = conformance()
    module.CHECKS[name](json.loads((VECTORS / name).read_text(encoding="utf-8")))


def test_public_conformance_vectors_match_current_dependencies():
    conformance().check(VECTORS)


def test_every_generated_vector_file_has_a_check_that_runs_it():
    """A vector file the generator writes and nobody executes is an expectation
    that rewrites itself on the next regeneration instead of failing."""

    assert set(conformance().CHECKS) == set(GENERATED)
    assert set(conformance().vectors()) == set(GENERATED)
