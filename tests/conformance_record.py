"""Record what this SDK produced for each conformance case.

The parity gate can check that a test *names* a vector file. It cannot check
that the test ran it: a name reaching a loader call is evidence of intent, not
of execution, and every tightening of that check has only moved the bar for
how convincingly a consumer can decline to run anything.

The way out is to stop asking about the test and ask about its output. A
consumer records what it computed for each case; the checker compares that to
what the reference computed. Hand-writing the file is still possible -- but a
hand-written file has to contain the right answers, and the moment a vector
changes it is wrong, which a stale declaration never was.

Set ``THALOVANT_CONFORMANCE_OUT`` to a path and run the suite; the recorded
results are written there on exit.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
from pathlib import Path
from typing import Any

_RESULTS: dict[str, dict[str, Any]] = {}


def canonical_digest(value: Any) -> str:
    """A stable digest of a produced value.

    Sorted keys and no insignificant whitespace, so the same value recorded by
    two SDKs in two languages digests the same. Values that are not JSON --
    bytes from a binary frame, say -- are named by their own content rather
    than by a repr that would differ per language.
    """

    if isinstance(value, (bytes, bytearray)):
        return "bytes:" + hashlib.sha256(bytes(value)).hexdigest()
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def record(vector_file: str, case: str, produced: Any) -> None:
    """Record what this SDK produced for one case of one vector file."""

    entry = _RESULTS.setdefault(vector_file, {})
    digest = canonical_digest(produced)
    previous = entry.get(case)
    if previous is not None and previous != digest:
        raise AssertionError(
            f"{vector_file}/{case}: recorded twice with different outputs")
    entry[case] = digest


def _write() -> None:
    target = os.environ.get("THALOVANT_CONFORMANCE_OUT")
    if not target or not _RESULTS:
        return
    vectors_dir = Path(__file__).resolve().parents[1] / "contracts" / "conformance"
    results = {}
    for vector_file, cases in sorted(_RESULTS.items()):
        source = vectors_dir / vector_file
        results[vector_file] = {
            # Ties the outputs to the exact input that produced them: a vector
            # change invalidates the record rather than silently outliving it.
            "digest": hashlib.sha256(source.read_bytes()).hexdigest(),
            "cases": dict(sorted(cases.items())),
        }
    Path(target).write_text(
        json.dumps({"schema_version": 1, "results": results}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


atexit.register(_write)
