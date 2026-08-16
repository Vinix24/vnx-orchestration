#!/usr/bin/env python3
"""Tests for scripts/check_status_vocab_drift.py — the CI drift check.

The ledger-vocab consolidation's CI guard: a second hard-coded
completion-outcome status collection in the repo must fail the drift check.
These tests prove the scanner (1) is clean on the real tree, (2) catches a
planted copy, (3) does not false-positive on a distinct vocabulary, and (4)
that the converter reads the canonical vocabulary instead of keeping its own
copy.
"""

from __future__ import annotations

import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

for p in (str(SCRIPTS_DIR), str(LIB_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from check_status_vocab_drift import find_hardcoded_outcome_vocab  # noqa: E402


def test_real_tree_has_no_hardcoded_outcome_vocab_copy():
    violations = find_hardcoded_outcome_vocab(VNX_ROOT)
    assert violations == [], (
        "found hard-coded copies of the completion-outcome vocabulary:\n"
        + "\n".join(f"  {v.path}:{v.line} {v.name} = {v.literals}" for v in violations)
    )


def test_planted_second_collection_is_detected(tmp_path):
    (tmp_path / "scripts" / "lib").mkdir(parents=True)
    planted = tmp_path / "scripts" / "lib" / "planted_copy.py"
    planted.write_text(
        "_TERMINAL_SUCCESS_STATUSES = frozenset({'success', 'done', 'complete', 'completed'})\n"
        "_TERMINAL_FAILURE_STATUSES = frozenset({'failed', 'failure', 'heartbeat_killed'})\n",
        encoding="utf-8",
    )
    violations = find_hardcoded_outcome_vocab(tmp_path)
    names = {v.name for v in violations}
    assert "_TERMINAL_SUCCESS_STATUSES" in names, names
    assert "_TERMINAL_FAILURE_STATUSES" in names, names


def test_distinct_vocabulary_is_not_flagged(tmp_path):
    (tmp_path / "scripts" / "lib").mkdir(parents=True)
    (tmp_path / "scripts" / "lib" / "distinct.py").write_text(
        "REVIEW_VERDICT_STATUSES = frozenset({'pass', 'fail', 'pending'})\n",
        encoding="utf-8",
    )
    assert find_hardcoded_outcome_vocab(tmp_path) == []


def test_converter_reads_canonical_not_own_copy():
    src = (LIB_DIR / "report_to_receipt_converter.py").read_text(encoding="utf-8")
    # No terminal-status hand-copy remains.
    assert "_TERMINAL_SUCCESS_STATUSES" not in src
    assert "_TERMINAL_FAILURE_STATUSES" not in src
    # It delegates every declared status to the canonical resolver.
    assert "from event_outcome_semantics import UnknownStatusError, resolve_status_category" in src
    assert "resolve_status_category(status_raw)" in src


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
