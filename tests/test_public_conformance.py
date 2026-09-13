"""Keep committed cross-language expectations bound to executable Python behavior."""
import importlib.util
from pathlib import Path


def test_public_conformance_vectors_match_current_dependencies():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("conformance", root / "scripts/generate-sdk-conformance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.check(root / "contracts/conformance")
