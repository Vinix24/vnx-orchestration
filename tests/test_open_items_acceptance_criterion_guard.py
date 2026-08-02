#!/usr/bin/env python3
"""Acceptance-criterion rejection guard for open items — prevents tracking
gate-steps and check-off items that describe SUCCESS rather than a problem.

Each test must fail against the unpatched code. A test that passes without
the guard measures nothing.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"


def _load_oim(tmp_path: Path):
    """Reload open_items_manager with a clean per-test STATE_DIR."""
    env_patch = {
        "VNX_DATA_DIR": str(tmp_path / "data"),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_STATE_DIR": str(tmp_path / "data" / "state"),
        "VNX_HOME": str(TESTS_DIR.parent),
    }
    (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)

    mod_name = f"open_items_manager_test_{tmp_path.name}"
    with patch.dict(os.environ, env_patch):
        spec = importlib.util.spec_from_file_location(
            mod_name, SCRIPTS_DIR / "open_items_manager.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            del sys.modules[mod_name]
            raise
    return mod


# ---------------------------------------------------------------------------
# Each individual pattern from _ACCEPTANCE_CRITERION_PATTERNS
# ---------------------------------------------------------------------------

ACCEPTANCE_CRITERION_TITLES = [
    # (1) pytest|npm run test… groen
    "pytest tests/test_foo.py -v groen",
    "npm run test -- groen",
    "npm run test:unit -- groen",
    # (2) check + build + test:unit … groen
    "check + build + test:unit groen",
    # (3) gh pr checks … groen
    "gh pr checks groen",
    # (4) codex gate green
    "codex gate green",
    # (5) gemini gate green
    "gemini gate green",
    # (6) CI green
    "CI green",
    # (7) manual smoke:
    "manual smoke: login flow works",
    # (8) Migration apply
    "Migration apply",
    # (9) codex- + gemini-gate … passed/completed
    "codex- + gemini-gate passed",
    "codex-+gemini-gate completed",
]


@pytest.mark.parametrize("title", ACCEPTANCE_CRITERION_TITLES)
def test_is_acceptance_criterion_rejects_pattern(title: str, tmp_path: Path):
    """Every pattern in _ACCEPTANCE_CRITERION_PATTERNS must reject the title."""
    oim = _load_oim(tmp_path)
    assert oim.is_acceptance_criterion(title) is True, (
        f"is_acceptance_criterion({title!r}) returned False — "
        "this title should match an acceptance-criterion pattern"
    )


# ---------------------------------------------------------------------------
# Valid problem title — must NOT be rejected
# ---------------------------------------------------------------------------

VALID_PROBLEM_TITLES = [
    "codex gate FAILED: 3 findings in scripts/foo.py",
    "pytest fails: 5 errors in test suite",
    "Memory leak in dispatch processor under high concurrency",
    "npm build broken on main branch",
    "ci pipeline broken: docker build timeout",
]


@pytest.mark.parametrize("title", VALID_PROBLEM_TITLES)
def test_is_acceptance_criterion_accepts_problem(title: str, tmp_path: Path):
    """A real problem title must NOT match any acceptance-criterion pattern."""
    oim = _load_oim(tmp_path)
    assert oim.is_acceptance_criterion(title) is False, (
        f"is_acceptance_criterion({title!r}) returned True — "
        "this is a genuine problem, not a passing gate-step"
    )


# ---------------------------------------------------------------------------
# _validate_title_not_acceptance_criterion
# ---------------------------------------------------------------------------

def test_validate_raises_valueerror_for_acceptance_criterion(tmp_path: Path):
    """_validate_title_not_acceptance_criterion raises ValueError on a gate-step title."""
    oim = _load_oim(tmp_path)
    with pytest.raises(ValueError, match="REJECTED"):
        oim._validate_title_not_acceptance_criterion("codex gate green")


def test_validate_passes_for_problem_title(tmp_path: Path):
    """_validate_title_not_acceptance_criterion does nothing for a real problem."""
    oim = _load_oim(tmp_path)
    oim._validate_title_not_acceptance_criterion("memory leak in dispatch worker")


def test_validate_force_bypasses_guard(tmp_path: Path):
    """force=True skips the guard even for an acceptance-criterion title."""
    oim = _load_oim(tmp_path)
    oim._validate_title_not_acceptance_criterion("codex gate green", force=True)


# ---------------------------------------------------------------------------
# add_item_programmatic raises ValueError
# ---------------------------------------------------------------------------

def test_add_item_programmatic_raises_valueerror(tmp_path: Path):
    """add_item_programmatic raises ValueError on acceptance-criterion title."""
    oim = _load_oim(tmp_path)
    with pytest.raises(ValueError, match="REJECTED"):
        oim.add_item_programmatic(
            title="pytest tests/ -v groen",
            severity="info",
            dispatch_id="DISP-TEST-001",
        )


def test_add_item_programmatic_accepts_problem(tmp_path: Path):
    """add_item_programmatic succeeds for a genuine problem title."""
    oim = _load_oim(tmp_path)
    # Guard must classify this as a non-criterion (fails AttributeError without patch)
    assert oim.is_acceptance_criterion(
        "codex gate FAILED: 3 findings in scripts/foo.py"
    ) is False
    item_id, created = oim.add_item_programmatic(
        title="codex gate FAILED: 3 findings in scripts/foo.py",
        severity="blocker",
        dispatch_id="DISP-TEST-002",
    )
    assert created is True
    assert item_id.startswith("OI-")


# ---------------------------------------------------------------------------
# CLI add path respects --force
# ---------------------------------------------------------------------------

class _Args:
    """Minimal argparse.Namespace stub for testing add_item directly."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_add_item_rejects_acceptance_criterion(tmp_path: Path):
    """The CLI add_item function raises ValueError on acceptance-criterion title."""
    oim = _load_oim(tmp_path)
    args = _Args(
        title="gemini gate green",
        severity="info",
        dispatch="DISP-CLI-001",
        pr=None,
        report=None,
        details=None,
    )
    with pytest.raises(ValueError, match="REJECTED"):
        oim.add_item(args)


def test_add_item_force_bypasses_guard(tmp_path: Path):
    """--force flag bypasses the acceptance-criterion rejection."""
    oim = _load_oim(tmp_path)
    # Guard must classify this as a criterion (fails AttributeError without patch)
    assert oim.is_acceptance_criterion("gemini gate green") is True
    args = _Args(
        title="gemini gate green",
        severity="info",
        dispatch="DISP-CLI-002",
        pr=None,
        report=None,
        details=None,
        force=True,
    )
    oim.add_item(args)  # does not raise → guard was bypassed
