#!/usr/bin/env python3
"""OI-1429 — _build_frontmatter must resolve task_class in the same order as
the spawn call: explicit --task-class flag on args first, then VNX_TASK_CLASS
env, then the "implementation" default. Before this fix, _build_frontmatter
read ONLY the env var, so any dispatch that got its task_class via the
explicit flag (never setting the env var) had its report frontmatter stamp
the default regardless of what the spawn actually received.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import provider_dispatch  # noqa: E402


def _make_args(task_class: str | None) -> argparse.Namespace:
    return argparse.Namespace(
        dispatch_id="oi1429-test-dispatch",
        terminal_id="T1",
        role="backend-developer",
        pr_id=None,
        task_class=task_class,
    )


class _FakeResult:
    """Bare result stub — no frontmatter_fields(), matches hasattr() miss path."""

    returncode = 0


def _call(args: argparse.Namespace) -> dict:
    return provider_dispatch._build_frontmatter(
        args,
        "claude",
        "sonnet",
        _FakeResult(),
        1.23,
        {"input": 10, "output": 20, "cache_hit": 0},
        0.0,
    )


def test_explicit_args_task_class_wins_over_env(monkeypatch):
    """Case 1: args carries an explicit class, env is empty/different -> args wins."""
    monkeypatch.setenv("VNX_TASK_CLASS", "research_structured")
    args = _make_args("review")

    fm = _call(args)

    print(f"OI-1429 case1 frontmatter['task_class'] = {fm['task_class']!r}")
    assert fm["task_class"] == "review"


def test_no_args_no_env_falls_back_to_default(monkeypatch):
    """Case 2: neither args nor env carry a class -> visible 'implementation' default."""
    monkeypatch.delenv("VNX_TASK_CLASS", raising=False)
    args = _make_args(None)

    fm = _call(args)

    print(f"OI-1429 case2 frontmatter['task_class'] = {fm['task_class']!r}")
    assert fm["task_class"] == "implementation"


def test_empty_args_task_class_falls_back_to_env(monkeypatch):
    """Case 3: args has no class but env does -> auto-routing path must still work."""
    monkeypatch.setenv("VNX_TASK_CLASS", "research_structured")
    args = _make_args("")

    fm = _call(args)

    print(f"OI-1429 case3 frontmatter['task_class'] = {fm['task_class']!r}")
    assert fm["task_class"] == "research_structured"


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
