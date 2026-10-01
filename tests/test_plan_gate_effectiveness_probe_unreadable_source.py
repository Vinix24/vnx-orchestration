"""Regression: an unreadable OI-PLAN blocker source is a gap, not zero blockers.

Defect (measured 24-09): ``PlanGateEffectivenessProbe.probe()`` skipped the
``track_open_items`` query silently when the coordination DB existed but had no
``track_open_items`` table (or no ``resolved_at`` column). The three blocker
counters stayed 0 and ``health()`` said ``ok`` — so a stale checkout DB hid 90
unresolved plan-gate blockers.

A source the probe could not read must be named: the counters become ``None``
(never 0), ``oi_plan_source`` reads ``"unreadable"``, the signal says so, and
``health()`` returns ``degraded`` (not ``ok``).

These tests are red on the pre-fix code (no ``oi_plan_source`` key; status
``ok``). Resolves OI-1840.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB_DIR = _REPO_ROOT / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from ndjson_hash_chain import append_chained_entry  # noqa: E402
from plan_gate_effectiveness_probe import (  # noqa: E402
    COORDINATION_DB_FILENAME,
    PlanGateEffectivenessProbe,
)

# The contract strings, written literally so this module imports (and therefore
# fails behaviorally, not at collection) against the pre-fix probe.
_SOURCE_KEY = "oi_plan_source"
_SOURCE_REASON_KEY = "oi_plan_source_reason"
_SOURCE_READ = "read"
_SOURCE_UNREADABLE = "unreadable"
_COUNTERS = ("oi_plan_unresolved", "oi_plan_stale_unresolved", "oi_plan_resolved")


def _seed_run_ledger(repo_root: Path, count: int = 1) -> None:
    """A non-empty ledger of organic (resolver: run) passes — the 'active' signal
    that previously forced status ``ok`` over the unreadable blocker source."""
    ledger = repo_root / ".vnx-attest" / "plan-gates.ndjson"
    for i in range(count):
        append_chained_entry(ledger, {
            "type": "plan_gate_pass", "track_id": f"t{i}", "resolver": "run",
        })


def _make_db(state_dir: Path, ddl: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / COORDINATION_DB_FILENAME))
    conn.execute(ddl)
    conn.commit()
    conn.close()


def _assert_unreadable(result) -> None:
    assert result.status == "degraded"  # NOT ok — the whole point of the fix
    # Not 0: a counter of 0 would read as a real "no blockers" measurement.
    for key in _COUNTERS:
        assert result.detail.get(key) is None, f"{key} must be None, not 0"
    assert result.detail.get(_SOURCE_KEY) == _SOURCE_UNREADABLE
    assert "UNREADABLE" in result.signal
    assert "0 unresolved OI-PLAN" not in result.signal
    assert "no plan-gate activity yet" not in result.signal


def test_missing_table_is_unreadable_not_zero_blockers(tmp_path):
    _seed_run_ledger(tmp_path)
    state_dir = tmp_path / "state"
    # The 15-08 stale-checkout shape: a coordination DB with an unrelated table.
    _make_db(state_dir, "CREATE TABLE dispatches (id TEXT)")

    result = PlanGateEffectivenessProbe(repo_root=tmp_path, state_dir=state_dir).run()

    _assert_unreadable(result)
    assert "track_open_items" in result.detail.get(_SOURCE_REASON_KEY, "")


def test_missing_resolved_at_column_is_unreadable_not_zero_blockers(tmp_path):
    _seed_run_ledger(tmp_path)
    state_dir = tmp_path / "state"
    _make_db(
        state_dir,
        "CREATE TABLE track_open_items (track_id TEXT, project_id TEXT, oi_id TEXT, "
        "link_type TEXT, linked_at TEXT)",
    )

    result = PlanGateEffectivenessProbe(repo_root=tmp_path, state_dir=state_dir).run()

    _assert_unreadable(result)
    assert "resolved_at" in result.detail.get(_SOURCE_REASON_KEY, "")


def test_unreadable_source_with_empty_ledger_is_not_no_activity(tmp_path):
    """An unreadable blocker source must not fall through to the no-activity
    branch, which would return ``unknown`` + 'no plan-gate activity yet' and hide
    the gap."""
    state_dir = tmp_path / "state"
    _make_db(state_dir, "CREATE TABLE dispatches (id TEXT)")

    result = PlanGateEffectivenessProbe(repo_root=tmp_path, state_dir=state_dir).run()

    _assert_unreadable(result)


def test_readable_db_reports_source_read_and_keeps_counts(tmp_path):
    """A DB that does have the table keeps today's counts/health — the fix only
    changes the unreadable case."""
    state_dir = tmp_path / "state"
    _make_db(
        state_dir,
        "CREATE TABLE track_open_items (track_id TEXT, project_id TEXT, oi_id TEXT, "
        "link_type TEXT, linked_at TEXT, resolved_at TEXT)",
    )
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(state_dir / COORDINATION_DB_FILENAME))
    conn.execute(
        "INSERT INTO track_open_items VALUES (?,?,?,?,?,?)",
        ("t1", "p", "OI-PLAN-t1", "blocks", now, None),
    )
    conn.commit()
    conn.close()

    result = PlanGateEffectivenessProbe(repo_root=tmp_path, state_dir=state_dir).run()

    assert result.detail.get(_SOURCE_KEY) == _SOURCE_READ
    assert result.detail["oi_plan_unresolved"] == 1
    assert result.detail["oi_plan_stale_unresolved"] == 0
    assert result.status == "ok"


def test_missing_db_file_is_db_absent_with_zero_counts(tmp_path):
    """No DB at all is not the same as unreadable: there is no source yet, so the
    counters stay 0 and the existing health rules apply."""
    result = PlanGateEffectivenessProbe(repo_root=tmp_path, state_dir=tmp_path / "state").run()

    assert result.detail.get(_SOURCE_KEY) == "db_absent"
    assert result.detail["oi_plan_unresolved"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
