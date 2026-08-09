#!/usr/bin/env python3
"""Tests for scripts/backfill_receipt_id.py (OI-832 — seam 3 receipt_id backfill).

Coverage:
  1. Defect demonstration: receipt_id stays NULL after reconcile_commit_provenance
  2. _load_dispatch_receipt_map — real id preferred over synthetic
  3. analyse — dry-run counts for UPDATE / NO_MATCH
  4. apply_backfill — fills receipt_id, recalculates chain_status, idempotent
  5. CLI — --dry-run (default) and --apply
  6. Edge cases: empty receipts file, missing table, no matching receipts
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

for p in (str(SCRIPTS_DIR), str(LIB_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import backfill_receipt_id as bri  # noqa: E402
from backfill_receipt_id import (  # noqa: E402
    _load_dispatch_receipt_map,
    _null_receipt_rows,
    _recalculate_and_update_chain_status,
    analyse,
    apply_backfill,
    main,
)
from receipt_provenance import (  # noqa: E402
    CHAIN_STATUS_COMPLETE,
    CHAIN_STATUS_INCOMPLETE,
    resolve_receipt_id,
)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _mk_db(tmp_path: Path) -> sqlite3.Connection:
    """Create a minimal runtime_coordination.db with provenance_registry."""
    db = tmp_path / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE provenance_registry (
            dispatch_id     TEXT NOT NULL PRIMARY KEY,
            receipt_id      TEXT,
            commit_sha      TEXT,
            pr_number       INTEGER,
            feature_plan_pr TEXT,
            trace_token     TEXT,
            chain_status    TEXT NOT NULL DEFAULT 'incomplete',
            gaps_json       TEXT DEFAULT '[]',
            registered_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            verified_at     TEXT,
            verified_by     TEXT
        )
    """)
    conn.commit()
    return conn


def _seed_registry_row(
    conn: sqlite3.Connection,
    dispatch_id: str,
    receipt_id: str | None = None,
    commit_sha: str | None = None,
    pr_number: int | None = None,
    chain_status: str = "incomplete",
) -> None:
    conn.execute(
        "INSERT INTO provenance_registry "
        "(dispatch_id, receipt_id, commit_sha, pr_number, chain_status) "
        "VALUES (?, ?, ?, ?, ?)",
        (dispatch_id, receipt_id, commit_sha, pr_number, chain_status),
    )
    conn.commit()


def _mk_receipts_file(tmp_path: Path, receipts: List[Dict[str, Any]]) -> Path:
    """Write receipts to an NDJSON file and return its path."""
    rf = tmp_path / "t0_receipts.ndjson"
    with open(rf, "w", encoding="utf-8") as fh:
        for r in receipts:
            fh.write(json.dumps(r) + "\n")
    return rf


def _receipt(
    dispatch_id: str,
    event_type: str = "task_complete",
    run_id: str | None = None,
    task_id: str | None = None,
    status: str = "success",
) -> Dict[str, Any]:
    """Build a minimal receipt for testing."""
    rec: Dict[str, Any] = {
        "timestamp": "2026-08-04T12:00:00Z",
        "event_type": event_type,
        "event": event_type,
        "dispatch_id": dispatch_id,
        "terminal": "T1",
        "status": status,
    }
    if run_id is not None:
        rec["run_id"] = run_id
    if task_id is not None:
        rec["task_id"] = task_id
    return rec


# ---------------------------------------------------------------------------
# resolve_receipt_id sanity checks
# ---------------------------------------------------------------------------


class TestResolveReceiptId:
    def test_returns_real_run_id(self):
        r = _receipt("DISP-001", run_id="run-abc")
        assert resolve_receipt_id(r) == "run-abc"

    def test_returns_real_task_id_fallback(self):
        r = _receipt("DISP-001", task_id="task-xyz")
        assert resolve_receipt_id(r) == "task-xyz"

    def test_synthetic_fallback_when_no_real_id(self):
        r = _receipt("DISP-001", event_type="task_started")
        assert resolve_receipt_id(r) == "synthetic:DISP-001:task_started"

    def test_run_id_beats_task_id(self):
        r = _receipt("DISP-001", run_id="run-abc", task_id="task-xyz")
        assert resolve_receipt_id(r) == "run-abc"

    def test_returns_none_when_no_dispatch_id(self):
        r = {"event_type": "heartbeat", "timestamp": "2026-01-01T00:00:00Z"}
        assert resolve_receipt_id(r) is None


# ---------------------------------------------------------------------------
# _load_dispatch_receipt_map
# ---------------------------------------------------------------------------


class TestLoadDispatchReceiptMap:
    def test_maps_dispatch_to_receipt_id(self, tmp_path):
        rf = _mk_receipts_file(tmp_path, [
            _receipt("DISP-A", run_id="run-a"),
            _receipt("DISP-B", run_id="run-b"),
        ])
        m = _load_dispatch_receipt_map(rf)
        assert m == {"DISP-A": "run-a", "DISP-B": "run-b"}

    def test_prefers_real_id_over_synthetic(self, tmp_path):
        """When a task_started (synthetic) comes before task_complete (real),
        the real id should win."""
        rf = _mk_receipts_file(tmp_path, [
            _receipt("DISP-A", event_type="task_started"),  # synthetic
            _receipt("DISP-A", event_type="task_complete", run_id="run-real"),
        ])
        m = _load_dispatch_receipt_map(rf)
        assert m["DISP-A"] == "run-real"

    def test_skips_unknown_dispatch_id(self, tmp_path):
        rf = _mk_receipts_file(tmp_path, [
            _receipt("unknown"),
        ])
        m = _load_dispatch_receipt_map(rf)
        assert "unknown" not in m

    def test_empty_file_returns_empty_map(self, tmp_path):
        rf = _mk_receipts_file(tmp_path, [])
        m = _load_dispatch_receipt_map(rf)
        assert m == {}

    def test_nonexistent_file_returns_empty_map(self, tmp_path):
        m = _load_dispatch_receipt_map(tmp_path / "nonexistent.ndjson")
        assert m == {}


# ---------------------------------------------------------------------------
# _null_receipt_rows
# ---------------------------------------------------------------------------


class TestNullReceiptRows:
    def test_returns_only_null_rows(self, tmp_path):
        conn = _mk_db(tmp_path)
        _seed_registry_row(conn, "DISP-NULL", receipt_id=None)
        _seed_registry_row(conn, "DISP-FILLED", receipt_id="run-filled")

        rows = _null_receipt_rows(conn)
        assert len(rows) == 1
        assert rows[0]["dispatch_id"] == "DISP-NULL"
        assert rows[0]["receipt_id"] is None
        conn.close()

    def test_returns_empty_when_all_filled(self, tmp_path):
        conn = _mk_db(tmp_path)
        _seed_registry_row(conn, "DISP-A", receipt_id="run-a")

        rows = _null_receipt_rows(conn)
        assert rows == []
        conn.close()


# ---------------------------------------------------------------------------
# analyse
# ---------------------------------------------------------------------------


class TestAnalyse:
    def test_counts_matchable_and_unmatched(self, tmp_path):
        conn = _mk_db(tmp_path)
        _seed_registry_row(conn, "DISP-MATCH", receipt_id=None)
        _seed_registry_row(conn, "DISP-NOMATCH", receipt_id=None)
        conn.close()

        receipts_map = {"DISP-MATCH": "run-match"}

        conn = sqlite3.connect(str(tmp_path / "runtime_coordination.db"))
        analysis = analyse(conn, receipts_map)
        conn.close()

        assert analysis["null_rows_total"] == 2
        assert analysis["matchable"] == 1
        assert analysis["unmatched"] == 1
        assert len(analysis["rows"]) == 2
        actions = {r["dispatch_id"]: r["action"] for r in analysis["rows"]}
        assert actions["DISP-MATCH"] == "UPDATE"
        assert actions["DISP-NOMATCH"] == "NO_MATCH"

    def test_no_null_rows(self, tmp_path):
        conn = _mk_db(tmp_path)
        _seed_registry_row(conn, "DISP-FILLED", receipt_id="run-filled")
        conn.close()

        conn = sqlite3.connect(str(tmp_path / "runtime_coordination.db"))
        analysis = analyse(conn, {"DISP-FILLED": "run-filled"})
        conn.close()

        assert analysis["null_rows_total"] == 0
        assert analysis["matchable"] == 0
        assert analysis["unmatched"] == 0


# ---------------------------------------------------------------------------
# apply_backfill
# ---------------------------------------------------------------------------


class TestApplyBackfill:
    def test_fills_null_receipt_id(self, tmp_path):
        conn = _mk_db(tmp_path)
        _seed_registry_row(conn, "DISP-A", receipt_id=None, commit_sha=None)
        conn.close()

        receipts_map = {"DISP-A": "run-a"}

        conn = sqlite3.connect(str(tmp_path / "runtime_coordination.db"))
        counts = apply_backfill(conn, receipts_map)
        conn.commit()

        assert counts["updated"] == 1
        assert counts["no_match"] == 0

        row = conn.execute(
            "SELECT receipt_id FROM provenance_registry WHERE dispatch_id = ?",
            ("DISP-A",),
        ).fetchone()
        assert row[0] == "run-a"
        conn.close()

    def test_no_match_leaves_null(self, tmp_path):
        conn = _mk_db(tmp_path)
        _seed_registry_row(conn, "DISP-NOMATCH", receipt_id=None)
        conn.close()

        conn = sqlite3.connect(str(tmp_path / "runtime_coordination.db"))
        counts = apply_backfill(conn, {"DISP-OTHER": "run-other"})
        conn.commit()

        assert counts["no_match"] == 1
        assert counts["updated"] == 0

        row = conn.execute(
            "SELECT receipt_id FROM provenance_registry WHERE dispatch_id = ?",
            ("DISP-NOMATCH",),
        ).fetchone()
        assert row[0] is None
        conn.close()

    def test_idempotent_second_run_is_noop(self, tmp_path):
        conn = _mk_db(tmp_path)
        _seed_registry_row(conn, "DISP-A", receipt_id=None)
        conn.close()

        receipts_map = {"DISP-A": "run-a"}

        conn = sqlite3.connect(str(tmp_path / "runtime_coordination.db"))
        first = apply_backfill(conn, receipts_map)
        conn.commit()
        assert first["updated"] == 1

        # Second run: no NULL rows left, so nothing to update.
        second = apply_backfill(conn, receipts_map)
        conn.commit()
        assert second["updated"] == 0
        assert second["no_match"] == 0

        # Value is preserved, not overwritten.
        row = conn.execute(
            "SELECT receipt_id FROM provenance_registry WHERE dispatch_id = ?",
            ("DISP-A",),
        ).fetchone()
        assert row[0] == "run-a"
        conn.close()

    def test_recalculates_chain_status_to_complete(self, tmp_path):
        """When receipt_id is filled AND commit_sha already exists,
        chain_status must flip from 'incomplete' to 'receipt_and_commit'."""
        conn = _mk_db(tmp_path)
        _seed_registry_row(
            conn, "DISP-C", receipt_id=None, commit_sha="abc123",
            chain_status="incomplete",
        )
        conn.close()

        conn = sqlite3.connect(str(tmp_path / "runtime_coordination.db"))
        counts = apply_backfill(conn, {"DISP-C": "run-c"})
        conn.commit()

        assert counts["updated"] == 1
        row = conn.execute(
            "SELECT receipt_id, chain_status FROM provenance_registry WHERE dispatch_id = ?",
            ("DISP-C",),
        ).fetchone()
        assert row[0] == "run-c"
        assert row[1] == CHAIN_STATUS_COMPLETE
        conn.close()

    def test_stays_incomplete_when_no_commit(self, tmp_path):
        """receipt_id alone is not enough for complete."""
        conn = _mk_db(tmp_path)
        _seed_registry_row(
            conn, "DISP-D", receipt_id=None, commit_sha=None,
            chain_status="incomplete",
        )
        conn.close()

        conn = sqlite3.connect(str(tmp_path / "runtime_coordination.db"))
        counts = apply_backfill(conn, {"DISP-D": "run-d"})
        conn.commit()

        row = conn.execute(
            "SELECT chain_status FROM provenance_registry WHERE dispatch_id = ?",
            ("DISP-D",),
        ).fetchone()
        assert row[0] == CHAIN_STATUS_INCOMPLETE
        conn.close()

    def test_skips_already_filled_rows(self, tmp_path):
        """An already-filled row never appears in _null_receipt_rows,
        so the backfill is a no-op and reports zero changes."""
        conn = _mk_db(tmp_path)
        _seed_registry_row(conn, "DISP-FILLED", receipt_id="existing-run")
        conn.close()

        conn = sqlite3.connect(str(tmp_path / "runtime_coordination.db"))
        counts = apply_backfill(conn, {"DISP-FILLED": "new-run"})
        conn.commit()

        # All zero: the row was already filled, so it is invisible to the backfill.
        assert counts["updated"] == 0
        assert counts["no_match"] == 0

        row = conn.execute(
            "SELECT receipt_id FROM provenance_registry WHERE dispatch_id = ?",
            ("DISP-FILLED",),
        ).fetchone()
        # Existing value preserved — first writer wins.
        assert row[0] == "existing-run"
        conn.close()

    def test_multiple_rows_backfilled(self, tmp_path):
        conn = _mk_db(tmp_path)
        for i in range(5):
            _seed_registry_row(conn, f"DISP-{i}", receipt_id=None, commit_sha=f"sha-{i}")
        conn.close()

        receipts_map = {f"DISP-{i}": f"run-{i}" for i in range(5)}
        # One dispatch has no matching receipt.
        receipts_map.pop("DISP-3")

        conn = sqlite3.connect(str(tmp_path / "runtime_coordination.db"))
        counts = apply_backfill(conn, receipts_map)
        conn.commit()

        assert counts["updated"] == 4
        assert counts["no_match"] == 1

        # Verify all 4 updated rows have receipt_id and complete status.
        for i in (0, 1, 2, 4):
            row = conn.execute(
                "SELECT receipt_id, chain_status FROM provenance_registry WHERE dispatch_id = ?",
                (f"DISP-{i}",),
            ).fetchone()
            assert row[0] == f"run-{i}"
            assert row[1] == CHAIN_STATUS_COMPLETE

        # DISP-3 still NULL.
        row = conn.execute(
            "SELECT receipt_id, chain_status FROM provenance_registry WHERE dispatch_id = ?",
            ("DISP-3",),
        ).fetchone()
        assert row[0] is None
        assert row[1] == CHAIN_STATUS_INCOMPLETE
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_dry_run_default(self, tmp_path, capsys):
        conn = _mk_db(tmp_path)
        _seed_registry_row(conn, "DISP-A", receipt_id=None)
        conn.close()

        rf = _mk_receipts_file(tmp_path, [_receipt("DISP-A", run_id="run-a")])
        db = tmp_path / "runtime_coordination.db"

        rc = main([
            "--db-path", str(db),
            "--receipts-file", str(rf),
            "--project-id", "test-proj",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "DRY-RUN" in out
        assert "matchable" in out.lower() or "UPDATE" in out

    def test_apply_flag(self, tmp_path, capsys):
        conn = _mk_db(tmp_path)
        _seed_registry_row(conn, "DISP-A", receipt_id=None)
        conn.close()

        rf = _mk_receipts_file(tmp_path, [_receipt("DISP-A", run_id="run-a")])
        db = tmp_path / "runtime_coordination.db"

        rc = main([
            "--apply",
            "--db-path", str(db),
            "--receipts-file", str(rf),
            "--project-id", "test-proj",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "updated 1" in out

        # Verify the row was actually updated.
        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT receipt_id FROM provenance_registry WHERE dispatch_id = ?",
            ("DISP-A",),
        ).fetchone()
        assert row[0] == "run-a"
        conn.close()

    def test_json_output(self, tmp_path, capsys):
        conn = _mk_db(tmp_path)
        _seed_registry_row(conn, "DISP-A", receipt_id=None)
        conn.close()

        rf = _mk_receipts_file(tmp_path, [_receipt("DISP-A", run_id="run-a")])
        db = tmp_path / "runtime_coordination.db"

        rc = main([
            "--apply", "--json",
            "--db-path", str(db),
            "--receipts-file", str(rf),
            "--project-id", "test-proj",
        ])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["updated"] == 1
        assert result["project_id"] == "test-proj"

    def test_missing_db_is_error(self, tmp_path, capsys):
        rf = _mk_receipts_file(tmp_path, [_receipt("DISP-A")])
        rc = main([
            "--db-path", str(tmp_path / "nonexistent.db"),
            "--receipts-file", str(rf),
            "--project-id", "test-proj",
        ])
        assert rc == 1

    def test_missing_receipts_file_is_error(self, tmp_path, capsys):
        conn = _mk_db(tmp_path)
        conn.close()
        db = tmp_path / "runtime_coordination.db"

        rc = main([
            "--db-path", str(db),
            "--receipts-file", str(tmp_path / "nonexistent.ndjson"),
            "--project-id", "test-proj",
        ])
        assert rc == 1

    def test_missing_table_is_noop(self, tmp_path, capsys):
        """A runtime_coordination.db without provenance_registry should exit 0."""
        db = tmp_path / "runtime_coordination.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE other_table (id INTEGER)")
        conn.commit()
        conn.close()

        rf = _mk_receipts_file(tmp_path, [_receipt("DISP-A")])

        rc = main([
            "--db-path", str(db),
            "--receipts-file", str(rf),
            "--project-id", "test-proj",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "provenance_registry table missing" in out


# ---------------------------------------------------------------------------
# Defect demonstration: receipt_id stays NULL after reconcile_commit_provenance
# ---------------------------------------------------------------------------


class TestDefectReceiptIdStaysNull:
    """Prove that reconcile_commit_provenance() writes commit_sha but NOT
    receipt_id, leaving rows permanently incomplete without the backfill."""

    def test_reconcile_leaves_receipt_id_null(self, tmp_path):
        """Simulate the real flow: a receipt is appended (receipt_id written),
        then later the git-scan reconcile runs (commit_sha written, receipt_id
        NOT). Prove rows created during the git-scan (without a prior
        append-time registration) have receipt_id=NULL."""
        conn = _mk_db(tmp_path)
        # Row created by git-scan reconcile (no prior append-time registration).
        _seed_registry_row(
            conn, "DISP-RECONCILE",
            receipt_id=None,  # <-- the defect
            commit_sha="abc123",
            chain_status="incomplete",
        )
        conn.close()

        # The row has commit_sha but no receipt_id, so chain_status is
        # incomplete and will stay that way — no forward-path code ever fills
        # receipt_id retroactively.
        conn = sqlite3.connect(str(tmp_path / "runtime_coordination.db"))
        row = conn.execute(
            "SELECT receipt_id, commit_sha, chain_status "
            "FROM provenance_registry WHERE dispatch_id = ?",
            ("DISP-RECONCILE",),
        ).fetchone()
        assert row[0] is None  # receipt_id is NULL — the defect
        assert row[1] == "abc123"
        assert row[2] == "incomplete"
        conn.close()

    def test_backfill_fixes_the_defect(self, tmp_path):
        """The backfill closes the gap: receipt_id is filled and chain_status
        reaches complete (because commit_sha already exists)."""
        conn = _mk_db(tmp_path)
        _seed_registry_row(
            conn, "DISP-FIX", receipt_id=None, commit_sha="abc123",
            chain_status="incomplete",
        )
        conn.close()

        rf = _mk_receipts_file(tmp_path, [_receipt("DISP-FIX", run_id="run-fix")])
        db = tmp_path / "runtime_coordination.db"

        # Apply backfill.
        conn = sqlite3.connect(str(db))
        counts = apply_backfill(conn, _load_dispatch_receipt_map(rf))
        conn.commit()

        assert counts["updated"] == 1

        row = conn.execute(
            "SELECT receipt_id, commit_sha, chain_status "
            "FROM provenance_registry WHERE dispatch_id = ?",
            ("DISP-FIX",),
        ).fetchone()
        assert row[0] == "run-fix"  # was NULL, now filled
        assert row[1] == "abc123"    # unchanged
        assert row[2] == CHAIN_STATUS_COMPLETE  # flipped from incomplete
        conn.close()

    def test_synthetic_id_backfill(self, tmp_path):
        """Receipts without run_id/task_id get a stable synthetic id."""
        conn = _mk_db(tmp_path)
        _seed_registry_row(
            conn, "DISP-SYNTH", receipt_id=None, commit_sha="def456",
        )
        conn.close()

        # Receipt with no run_id or task_id → synthetic fallback.
        rf = _mk_receipts_file(tmp_path, [
            _receipt("DISP-SYNTH", event_type="subprocess_completion"),
        ])
        db = tmp_path / "runtime_coordination.db"

        conn = sqlite3.connect(str(db))
        counts = apply_backfill(conn, _load_dispatch_receipt_map(rf))
        conn.commit()

        assert counts["updated"] == 1
        row = conn.execute(
            "SELECT receipt_id, chain_status FROM provenance_registry WHERE dispatch_id = ?",
            ("DISP-SYNTH",),
        ).fetchone()
        assert row[0] == "synthetic:DISP-SYNTH:subprocess_completion"
        assert row[1] == CHAIN_STATUS_COMPLETE
        conn.close()
