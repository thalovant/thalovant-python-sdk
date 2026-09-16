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
    if not target:
        return
    # Not `or not _RESULTS`. Returning early on an empty run left whatever was
    # at that path alone, so a suite that executed no conformance case at all
    # could present last week's artifact as this run's output -- which is
    # exactly the "names a vector without running it" hole this whole
    # mechanism exists to close, wearing a different hat. An empty run writes
    # an empty result, and the checker reports it as having recorded nothing.
    vectors_dir = Path(__file__).resolve().parents[1] / "contracts" / "conformance"
    results = {}
    for vector_file, cases in sorted(_RESULTS.items()):
        source = vectors_dir / vector_file
        results[vector_file] = {
            # Ties the outputs to the exact input that produced them: a vector
            # change invalidates the record rather than silently outliving it.
            #
            # The parsed JSON, not the bytes. check-sdk-parity accepts a
            # vendored vector by hashing what it parses to, so indentation and
            # line endings are deliberately allowed to differ between
            # consumers -- and hashing bytes here made those same consumers
            # fail with "a different copy of the vectors" for a file that had
            # already been accepted as the right one.
            "digest": canonical_digest(json.loads(source.read_text(encoding="utf-8"))),
            "cases": dict(sorted(cases.items())),
        }
    Path(target).write_text(
        json.dumps({"schema_version": 1, "results": results}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


atexit.register(_write)
