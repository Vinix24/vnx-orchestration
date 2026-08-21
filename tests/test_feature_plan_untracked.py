"""Regression guard for OI-1376: FEATURE_PLAN.md must stay untracked.

scripts/build_feature_plan.py (and eleven other state-sync scripts) rewrite
FEATURE_PLAN.md as a side effect of routine dispatch/reconcile runs. While the
file was git-tracked, every rewrite turned the worktree dirty and complete
work got scored as a failure by the dirty check — even though nothing about
the actual change was wrong. The fix is structural: the file stays on disk as
a generated artifact, but git no longer tracks it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FEATURE_PLAN = _REPO_ROOT / "FEATURE_PLAN.md"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_feature_plan_is_not_tracked() -> None:
    result = _git("ls-files", "FEATURE_PLAN.md")
    assert result.stdout.strip() == "", (
        f"FEATURE_PLAN.md must not be tracked (OI-1376); "
        f"git ls-files returned: {result.stdout!r}"
    )


def test_feature_plan_matches_gitignore_rule() -> None:
    result = _git("check-ignore", "-q", "FEATURE_PLAN.md")
    assert result.returncode == 0, (
        "FEATURE_PLAN.md must match a .gitignore rule (OI-1376); "
        f"git check-ignore exited {result.returncode}"
    )


def test_rewriting_feature_plan_leaves_working_tree_clean() -> None:
    """Simulates any of the twelve generator scripts rewriting the file."""
    original = _FEATURE_PLAN.read_text(encoding="utf-8") if _FEATURE_PLAN.exists() else None
    try:
        _FEATURE_PLAN.write_text(
            "<!-- AUTO-GENERATED — DO NOT EDIT -->\n# VNX Feature Plan\ntest rewrite\n",
            encoding="utf-8",
        )
        result = _git("status", "--porcelain", "--", "FEATURE_PLAN.md")
        assert result.stdout == "", (
            f"Rewriting FEATURE_PLAN.md must not dirty git status (OI-1376); "
            f"got: {result.stdout!r}"
        )
    finally:
        if original is not None:
            _FEATURE_PLAN.write_text(original, encoding="utf-8")
        elif _FEATURE_PLAN.exists():
            _FEATURE_PLAN.unlink()
