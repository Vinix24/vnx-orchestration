"""Regression test for OI-908 ronde 2: a non-live ``-m`` expression must not bypass f39 deselection.

The collection hook in ``tests/f39/conftest.py`` deselects ``live``-marked replay
tests unless the user opts in explicitly. Round 1 treated ANY ``-m`` expression as
an opt-in, so ``pytest tests/f39 -m "not integration"`` silently collected all 31
replay tests — each a real headless ``claude -p`` call. This pins the matrix:

    pytest tests/f39                      -> 0 replay tests collected
    pytest tests/f39 -m live              -> 31 replay tests collected (explicit opt-in)
    pytest tests/f39 -m "not integration" -> 0 replay tests collected (unrelated filter)

Verification uses ``--collect-only`` so the test never executes a replay scenario
and never triggers LLM inference.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_F39 = REPO_ROOT / "tests" / "f39"


def _collect_count(marker_expr: str | None = None) -> int:
    """Number of f39 replay tests pytest collects for the given ``-m`` expression."""
    cmd = [sys.executable, "-m", "pytest", str(TESTS_F39), "--collect-only", "-q"]
    if marker_expr is not None:
        cmd += ["-m", marker_expr]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    # Exit 5 = "no tests collected", the expected result for a fully deselected run.
    assert proc.returncode in (0, 5), f"pytest collect failed:\n{proc.stdout}\n{proc.stderr}"
    return sum(
        1
        for line in proc.stdout.splitlines()
        if line.startswith("tests/f39/") and "::" in line
    )


def test_default_run_deselects_replay_tests() -> None:
    assert _collect_count() == 0


def test_explicit_live_opt_in_collects_replay_tests() -> None:
    assert _collect_count("live") == 31


def test_unrelated_marker_expression_does_not_bypass_deselection() -> None:
    assert _collect_count("not integration") == 0
