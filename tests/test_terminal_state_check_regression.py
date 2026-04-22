#!/usr/bin/env python3
"""
Regression tests for scripts/lib/terminal_state_check.py.

Guard against re-deletion (see commit c90615e chore: purge 100 dead files).
dispatch_lifecycle.sh invokes this script for every dispatch — if it goes missing
the entire dispatcher halts with a "can't open file" Python error.

Path A chosen: file is needed (dispatch_lifecycle.sh line ~29 calls it).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
_MODULE_PATH = _LIB / "terminal_state_check.py"


# ---------------------------------------------------------------------------
# Importability guard
# ---------------------------------------------------------------------------

def test_terminal_state_check_file_exists():
    """Regression: file must exist so dispatch_lifecycle.sh can call it."""
    assert _MODULE_PATH.exists(), (
        f"{_MODULE_PATH} missing — dispatcher will crash. "
        "Restore from git show c90615e^:scripts/lib/terminal_state_check.py"
    )


def test_terminal_state_check_importable():
    """Module must load without ImportError."""
    spec = importlib.util.spec_from_file_location("terminal_state_check", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert callable(mod.check_terminal)
    assert callable(mod.main)


# ---------------------------------------------------------------------------
# Functional tests via check_terminal()
# ---------------------------------------------------------------------------

def _load_check_terminal():
    spec = importlib.util.spec_from_file_location("terminal_state_check", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.check_terminal


def _write_state(tmp_path: Path, terminals: dict) -> Path:
    state_file = tmp_path / "terminal_state.json"
    state_file.write_text(
        json.dumps({"schema_version": 1, "terminals": terminals}),
        encoding="utf-8",
    )
    return state_file


def _future_iso(seconds: int = 600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _past_iso(seconds: int = 60) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


check_terminal = _load_check_terminal()


class TestAllowCases:
    def test_no_terminal_entry_allows(self, tmp_path):
        f = _write_state(tmp_path, {})
        result = check_terminal(str(f), "T1", "dispatch-X")
        assert not result.startswith("BLOCK:"), f"Expected ALLOW, got {result}"

    def test_idle_terminal_allows(self, tmp_path):
        f = _write_state(tmp_path, {"T1": {"status": "idle", "claimed_by": None}})
        result = check_terminal(str(f), "T1", "dispatch-X")
        assert not result.startswith("BLOCK:"), f"Expected ALLOW, got {result}"

    def test_working_terminal_claimed_by_same_dispatch_allows(self, tmp_path):
        f = _write_state(tmp_path, {
            "T1": {
                "status": "working",
                "claimed_by": "dispatch-X",
                "lease_expires_at": _future_iso(),
            }
        })
        result = check_terminal(str(f), "T1", "dispatch-X")
        assert not result.startswith("BLOCK:"), f"Expected ALLOW, got {result}"

    def test_expired_lease_allows_new_dispatch(self, tmp_path):
        f = _write_state(tmp_path, {
            "T1": {
                "status": "working",
                "claimed_by": "dispatch-old",
                "lease_expires_at": _past_iso(120),
                "last_activity": _past_iso(1000),
            }
        })
        result = check_terminal(str(f), "T1", "dispatch-new")
        assert not result.startswith("BLOCK:"), f"Expected ALLOW for expired lease, got {result}"

    def test_missing_state_file_blocks_fail_closed(self, tmp_path):
        # dispatch_lifecycle.sh guards missing files before calling this script.
        # When called directly with a nonexistent path it returns BLOCK (fail-closed).
        result = check_terminal(str(tmp_path / "nonexistent.json"), "T1", "dispatch-X")
        assert result.startswith("BLOCK:"), f"Expected fail-closed BLOCK for missing file, got {result}"

    def test_stale_unclaimed_working_allows(self, tmp_path):
        # Unclaimed working with last_activity > 900s ago -> ALLOW
        f = _write_state(tmp_path, {
            "T1": {
                "status": "working",
                "claimed_by": None,
                "last_activity": _past_iso(950),
            }
        })
        result = check_terminal(str(f), "T1", "dispatch-X")
        assert not result.startswith("BLOCK:"), f"Expected ALLOW for stale unclaimed working, got {result}"


class TestBlockCases:
    def test_active_claim_by_other_dispatch_blocks(self, tmp_path):
        f = _write_state(tmp_path, {
            "T1": {
                "status": "working",
                "claimed_by": "dispatch-other",
                "lease_expires_at": _future_iso(),
            }
        })
        result = check_terminal(str(f), "T1", "dispatch-new")
        assert result.startswith("BLOCK:active_claim:"), f"Expected BLOCK:active_claim, got {result}"
        assert "dispatch-other" in result

    def test_recent_unclaimed_working_blocks(self, tmp_path):
        # Unclaimed working with last_activity < 900s ago -> BLOCK
        f = _write_state(tmp_path, {
            "T1": {
                "status": "working",
                "claimed_by": None,
                "last_activity": _past_iso(60),
            }
        })
        result = check_terminal(str(f), "T1", "dispatch-X")
        assert result.startswith("BLOCK:recent_working_without_claim:"), (
            f"Expected BLOCK:recent_working_without_claim, got {result}"
        )

    def test_corrupt_state_file_blocks(self, tmp_path):
        bad_file = tmp_path / "terminal_state.json"
        bad_file.write_text("{not valid json}", encoding="utf-8")
        result = check_terminal(str(bad_file), "T1", "dispatch-X")
        assert result.startswith("BLOCK:"), f"Expected BLOCK for corrupt file, got {result}"


class TestArgContract:
    def test_claim_active_returns_expected_shape(self, tmp_path):
        """claim_active returns ALLOW or BLOCK with correct prefix."""
        f = _write_state(tmp_path, {
            "T1": {
                "status": "working",
                "claimed_by": "dispatch-A",
                "lease_expires_at": _future_iso(300),
            }
        })
        result_same = check_terminal(str(f), "T1", "dispatch-A")
        result_other = check_terminal(str(f), "T1", "dispatch-B")

        assert result_same in ("ALLOW:clear", "ALLOW:no_record") or not result_same.startswith("BLOCK:")
        assert result_other.startswith("BLOCK:active_claim:")
